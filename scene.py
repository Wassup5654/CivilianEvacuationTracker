"""Latest 5 km Sentinel-2 preview and saved pixel clips for a confirmed point."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
BBOX_URL = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
USER_AGENT = "civilian-evacuation/0.1 (local Sentinel-2 lookup for evacuation routing)"
COLLECTION = "sentinel-2-l2a"
VISUAL_ASSET = "visual"
CLOUD_PROPERTY = "eo:cloud_cover"
TIMEOUT_SEC = 20.0
DOWNLOAD_TIMEOUT_SEC = 120.0
CLOUD_COVER_LIMIT = 20
LOOKBACK_DAYS = 30
EARLIER_GAP_DAYS = 5
EARLIER_LOOKBACK_DAYS = 90
SCENE_LIMIT = 10
HALF_SIDE_M = 2_500
METERS_PER_DEGREE = 111_320
IMAGE_SIZE = 512
NATIVE_PIXEL_M = 10
NATIVE_SIZE = (2 * HALF_SIDE_M) // NATIVE_PIXEL_M
SQUARES_DIR = Path("data") / "squares"
META_NAME = "square.json"
TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
MIN_CLIP_BYTES = 10_000
NO_SCENE_MESSAGE = "No clear satellite image in the last 30 days."
NO_EARLIER_MESSAGE = "No earlier clear image of this square in the 90 days before the latest one."


class SceneError(RuntimeError):
    """The satellite lookup could not be completed."""


class NoSceneError(SceneError):
    """The search worked, but no scene qualified."""


def square_bounds(latitude: float, longitude: float) -> tuple[float, float, float, float]:
    """West, south, east, north of the 5 km square around this point."""
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise SceneError("Those coordinates are not on Earth.")
    if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
        raise SceneError("Those coordinates are not on Earth.")
    east_scale = math.cos(math.radians(latitude))
    if east_scale < 0.02:
        raise SceneError("That point is too close to a pole for a 5 km square.")
    half_lat = HALF_SIDE_M / METERS_PER_DEGREE
    half_lon = HALF_SIDE_M / (METERS_PER_DEGREE * east_scale)
    west = longitude - half_lon
    south = latitude - half_lat
    east = longitude + half_lon
    north = latitude + half_lat
    if south < -90.0 or north > 90.0:
        raise SceneError("That point is too close to a pole for a 5 km square.")
    if west < -180.0 or east > 180.0:
        raise SceneError("That point is too close to the date line for a 5 km square.")
    return (west, south, east, north)


def clip_url(
    item_id: str,
    bounds: tuple[float, float, float, float],
    size: int,
    extension: str,
) -> str:
    """Address of this scene cropped to the square, as a png or tif."""
    box = ",".join(f"{edge:.6f}" for edge in bounds)
    params = urllib.parse.urlencode(
        {
            "collection": COLLECTION,
            "item": item_id,
            "assets": VISUAL_ASSET,
            "asset_bidx": f"{VISUAL_ASSET}|1,2,3",
            "nodata": "0",
        }
    )
    return f"{BBOX_URL}/{box}/{size}x{size}.{extension}?{params}"


def preview_url(item_id: str, bounds: tuple[float, float, float, float]) -> str:
    """PNG address for this scene cropped to the square."""
    return clip_url(item_id, bounds, IMAGE_SIZE, "png")


def choose_scene(
    payload: object, empty_message: str = NO_SCENE_MESSAGE
) -> dict[str, str | float]:
    """Pick the newest returned scene that still has a true-color image."""
    if not isinstance(payload, dict):
        raise SceneError("Satellite lookup returned an unexpected response.")
    features = payload.get("features")
    if not isinstance(features, list):
        raise SceneError("Satellite lookup returned an unexpected response.")
    newest: tuple[datetime, dict[str, str | float]] | None = None
    for feature in features:
        chosen = _read_feature(feature)
        if chosen is None:
            continue
        when, fields = chosen
        if newest is None or when > newest[0]:
            newest = (when, fields)
    if newest is None:
        raise NoSceneError(empty_message)
    return newest[1]


def request_scenes(bounds: tuple[float, float, float, float]) -> object:
    """Ask Planetary Computer for recent clear scenes over this square."""
    end = datetime.now(timezone.utc)
    return request_scenes_between(bounds, end - timedelta(days=LOOKBACK_DAYS), end)


def request_scenes_between(
    bounds: tuple[float, float, float, float], start: datetime, end: datetime
) -> object:
    """Ask Planetary Computer for clear scenes over this square in a UTC window."""
    window = f"{_stamp(start)}/{_stamp(end)}"
    body = {
        "collections": [COLLECTION],
        "bbox": list(bounds),
        "datetime": window,
        "query": {CLOUD_PROPERTY: {"lt": CLOUD_COVER_LIMIT}},
        "sortby": [{"field": "datetime", "direction": "desc"}],
        "limit": SCENE_LIMIT,
    }
    request = urllib.request.Request(
        SEARCH_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SceneError(f"Satellite lookup failed ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise SceneError("Satellite lookup could not reach the imagery service.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, TimeoutError) as exc:
        raise SceneError("Satellite lookup returned an unreadable response.") from exc


def find_scene(latitude: float, longitude: float) -> dict[str, str | float]:
    """Return captured, cloud_cover, and image_url for the latest clear scene."""
    bounds = square_bounds(latitude, longitude)
    chosen = choose_scene(_covering(request_scenes(bounds), bounds))
    return {
        "captured": chosen["captured"],
        "cloud_cover": chosen["cloud_cover"],
        "image_url": preview_url(str(chosen["item_id"]), bounds),
    }


def download_clip(
    item_id: str, bounds: tuple[float, float, float, float], path: Path
) -> None:
    """Save this scene's 500 by 500 square at 10 m as a georeferenced GeoTIFF."""
    request = urllib.request.Request(
        clip_url(item_id, bounds, NATIVE_SIZE, "tif"),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        raise SceneError(f"Saving the satellite pixels failed ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise SceneError("Saving the satellite pixels could not reach the imagery service.") from exc
    except TimeoutError as exc:
        raise SceneError("Saving the satellite pixels timed out.") from exc
    if len(data) < MIN_CLIP_BYTES or data[:4] not in TIFF_MAGIC:
        raise SceneError("The imagery service did not return a usable pixel file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)


def save_clips(
    latitude: float,
    longitude: float,
    root: Path | str = SQUARES_DIR,
    before: str | datetime | None = None,
) -> dict[str, object]:
    """Save the latest clear 5 km square and one earlier clear square of it.

    Both clips use the same box and the same 500 by 500 grid, so they line up
    pixel for pixel. If no earlier clear scene exists, only the latest is saved
    and earlier is None with a plain note.
    """
    bounds = square_bounds(latitude, longitude)
    latest = choose_scene(_covering(request_scenes(bounds), bounds))
    latest_when = _parse_time(str(latest["captured"]))
    if latest_when is None:
        raise SceneError("Satellite lookup returned an unreadable response.")

    earlier: dict[str, str | float] | None = None
    earlier_note: str | None = None
    if before:
        custom_before = _parse_time(before) if isinstance(before, str) else before
        end = custom_before if custom_before is not None else (latest_when - timedelta(days=EARLIER_GAP_DAYS))
    else:
        end = latest_when - timedelta(days=EARLIER_GAP_DAYS)
    start = end - timedelta(days=EARLIER_LOOKBACK_DAYS)
    try:
        earlier = choose_scene(
            _covering(request_scenes_between(bounds, start, end), bounds),
            NO_EARLIER_MESSAGE,
        )
    except NoSceneError as exc:
        earlier_note = str(exc)

    folder = Path(root) / f"{latitude:.5f}_{longitude:.5f}"
    folder.mkdir(parents=True, exist_ok=True)
    previous = _load_meta(folder)
    saved_latest = _store_clip(folder, "latest", latest, bounds, previous)
    saved_earlier = None
    if earlier is not None:
        saved_earlier = _store_clip(folder, "earlier", earlier, bounds, previous)
    else:
        (folder / "earlier.tif").unlink(missing_ok=True)

    result: dict[str, object] = {
        "folder": str(folder),
        "latitude": latitude,
        "longitude": longitude,
        "bounds": list(bounds),
        "size": NATIVE_SIZE,
        "latest": saved_latest,
        "earlier": saved_earlier,
        "earlier_note": earlier_note,
    }
    (folder / META_NAME).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _store_clip(
    folder: Path,
    role: str,
    scene: dict[str, str | float],
    bounds: tuple[float, float, float, float],
    previous: dict[str, object],
) -> dict[str, str | float]:
    name = f"{role}.tif"
    path = folder / name
    old = previous.get(role)
    reuse = (
        isinstance(old, dict)
        and old.get("item_id") == scene["item_id"]
        and path.is_file()
    )
    if not reuse:
        download_clip(str(scene["item_id"]), bounds, path)
    return {
        "item_id": scene["item_id"],
        "captured": scene["captured"],
        "cloud_cover": scene["cloud_cover"],
        "file": name,
    }


def _load_meta(folder: Path) -> dict[str, object]:
    try:
        data = json.loads((folder / META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _covering(
    payload: object, bounds: tuple[float, float, float, float]
) -> object:
    """Drop scenes whose footprint box does not contain the whole square."""
    if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
        return payload
    west, south, east, north = bounds
    kept = []
    for feature in payload["features"]:
        box = feature.get("bbox") if isinstance(feature, dict) else None
        if isinstance(box, list) and len(box) >= 4:
            try:
                f_west, f_south, f_east, f_north = (float(v) for v in box[:4])
            except (TypeError, ValueError):
                continue
            if not (f_west <= west and f_south <= south and f_east >= east and f_north >= north):
                continue
        kept.append(feature)
    return {**payload, "features": kept}


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_feature(feature: object) -> tuple[datetime, dict[str, str | float]] | None:
    if not isinstance(feature, dict):
        return None
    item_id = str(feature.get("id") or "").strip()
    properties = feature.get("properties")
    assets = feature.get("assets")
    if not item_id or not isinstance(properties, dict) or not isinstance(assets, dict):
        return None
    visual = assets.get(VISUAL_ASSET)
    if not isinstance(visual, dict) or not visual.get("href"):
        return None
    captured = str(properties.get("datetime") or "").strip()
    when = _parse_time(captured)
    if when is None:
        return None
    try:
        cloud_cover = float(properties.get(CLOUD_PROPERTY))
    except (TypeError, ValueError):
        return None
    if cloud_cover >= CLOUD_COVER_LIMIT:
        return None
    return (
        when,
        {"item_id": item_id, "captured": captured, "cloud_cover": cloud_cover},
    )


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when
