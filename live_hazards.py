"""Live hazards: satellite fire/heat detections (NASA FIRMS) and disaster alerts (GDACS).

Both feeds are downloaded as *global* files, so no user's zone or location is
ever sent to NASA or GDACS. The global downloads are refreshed in the
background every LIVE_HAZARD_REFRESH_SEC and kept in memory (and on disk under
data/hazards/, which holds no user data, so the app can restart offline).

Live hazards are warnings only: they never change routes. A FIRMS detection is
a hot spot seen from orbit (375 m pixels, a few hours old). It can be a strike,
a building fire, a wildfire, field burning or an industrial gas flare, so the
app labels it as "fire or heat detected", never as an attack.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HAZARDS_DIR = Path("data") / "hazards"
USER_AGENT = "civilian-evacuation/1.0 (humanitarian evacuation planner)"

# NASA FIRMS public 24-hour global files (no account or key needed).
FIRMS_FEEDS = {
    "NOAA-20 VIIRS": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_24h.csv",
    "NOAA-21 VIIRS": "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_Global_24h.csv",
}
FIRMS_INFO_URL = "https://firms.modaps.eosdis.nasa.gov/map/"

# GDACS caps each response at 100 events, so each type is fetched separately.
# Wildfires (WF) are left out: FIRMS already shows fires at much finer detail.
GDACS_URL = ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
             "?eventlist={etype}&fromDate={start}&toDate={end}&alertlevel=Green;Orange;Red")
GDACS_TYPES = {
    "EQ": "Earthquake",
    "TC": "Tropical cyclone",
    "FL": "Flood",
    "VO": "Volcano",
    "DR": "Drought",
}
GDACS_LOOKBACK_DAYS = 7
# How far (km) an alert of each type and level is shown from the zone.
GDACS_RADIUS_KM = {
    "EQ": {"Green": 100, "Orange": 300, "Red": 500},
    "TC": {"Green": 300, "Orange": 600, "Red": 1000},
    "FL": {"Green": 100, "Orange": 200, "Red": 300},
    "VO": {"Green": 50, "Orange": 100, "Red": 200},
    "DR": {"Green": 0, "Orange": 500, "Red": 800},   # Green droughts are too broad to be useful
}
LEVEL_ORDER = {"Red": 0, "Orange": 1, "Green": 2}

FIRE_ZONE_PAD_M = 1000      # show detections up to 1 km outside the zone
FIRE_ROUTE_NEAR_M = 500     # route warning if a detection is this close (VIIRS pixel is 375 m)
FIRE_MAX_AGE_H = 48         # the "24 h" files hold detections up to ~2 days old (processing delay)

_lock = threading.Lock()
_state: dict[str, Any] = {"fires": None, "alerts": None, "fetched_at": 0.0, "errors": [], "refreshing": False}


def _refresh_sec() -> int:
    try:
        from django.conf import settings
        return int(getattr(settings, "LIVE_HAZARD_REFRESH_SEC", 1800))
    except Exception:
        return 1800


def enabled() -> bool:
    try:
        from django.conf import settings
        return bool(getattr(settings, "LIVE_HAZARDS", True))
    except Exception:
        return True


# ── Download + parse ────────────────────────────────────────────────────────

def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_firms_csv(text: str, satellite: str) -> list[dict]:
    """FIRMS VIIRS CSV → detections {lat, lon, time (UTC datetime), frp, confidence, satellite}."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            hhmm = row["acq_time"].zfill(4)
            when = datetime.strptime(row["acq_date"] + hhmm, "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc)
            out.append({
                "lat": float(row["latitude"]),
                "lon": float(row["longitude"]),
                "time": when,
                "frp": float(row.get("frp") or 0.0),
                "confidence": (row.get("confidence") or "").strip().lower() or "nominal",
                "satellite": satellite,
                "daynight": row.get("daynight", ""),
            })
        except (KeyError, ValueError, TypeError, AttributeError):
            continue   # malformed or truncated row
    return out


def parse_gdacs(payload: dict) -> list[dict]:
    """GDACS event-list GeoJSON → alerts {type, name, level, lat, lon, bbox, from, to, url, severity}."""
    out = []
    for f in payload.get("features") or []:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or []
        etype = p.get("eventtype")
        if etype not in GDACS_TYPES or len(coords) < 2:
            continue
        if str(p.get("iscurrent", "true")).lower() != "true":
            continue
        url = p.get("url") or {}
        out.append({
            "id": f"{etype}-{p.get('eventid')}",
            "type": etype,
            "type_label": GDACS_TYPES[etype],
            "name": p.get("name") or p.get("eventname") or GDACS_TYPES[etype],
            "level": p.get("alertlevel") or "Green",
            "lon": float(coords[0]),
            "lat": float(coords[1]),
            "bbox": f.get("bbox"),
            "from": p.get("fromdate"),
            "to": p.get("todate"),
            "severity": (p.get("severitydata") or {}).get("severitytext") or "",
            "url": url.get("report") if isinstance(url, dict) else None,
        })
    return out


def _fetch_all() -> dict:
    fires, alerts, errors = [], [], []
    HAZARDS_DIR.mkdir(parents=True, exist_ok=True)
    for sat, url in FIRMS_FEEDS.items():
        try:
            text = _download(url).decode("utf-8", "replace")
            (HAZARDS_DIR / f"firms_{sat.split()[0].lower()}.csv").write_text(text, encoding="utf-8")
            fires += parse_firms_csv(text, sat)
        except Exception as exc:
            errors.append(f"NASA FIRMS {sat}: {exc.__class__.__name__}")
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=GDACS_LOOKBACK_DAYS)).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    gdacs_raw = []
    for etype in GDACS_TYPES:
        try:
            body = _download(GDACS_URL.format(etype=etype, start=start, end=end), timeout=30)
            if body.strip():          # GDACS answers 204 No Content when there are no events
                gdacs_raw += json.loads(body).get("features") or []
        except Exception as exc:
            errors.append(f"GDACS {etype}: {exc.__class__.__name__}")
    if gdacs_raw:
        (HAZARDS_DIR / "gdacs.json").write_text(json.dumps({"features": gdacs_raw}), encoding="utf-8")
    alerts = parse_gdacs({"features": gdacs_raw})
    return {"fires": fires, "alerts": alerts, "errors": errors}


def _load_from_disk() -> bool:
    """Reuse the last global download after a restart (it holds no user data)."""
    files = [HAZARDS_DIR / f"firms_{s.split()[0].lower()}.csv" for s in FIRMS_FEEDS]
    present = [f for f in files if f.exists()]
    if not present:
        return False
    mtime = min(f.stat().st_mtime for f in present)
    if time.time() - mtime > 6 * 3600:
        return False
    fires = []
    for sat, f in zip(FIRMS_FEEDS, files):
        if f.exists():
            fires += parse_firms_csv(f.read_text(encoding="utf-8"), sat)
    alerts = []
    g = HAZARDS_DIR / "gdacs.json"
    if g.exists():
        try:
            alerts = parse_gdacs(json.loads(g.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            pass
    with _lock:
        _state.update(fires=fires, alerts=alerts, fetched_at=mtime, errors=[])
    return True


def refresh(force: bool = False) -> None:
    """Download both feeds now (blocking). Used by the background thread and `manage.py refresh_hazards`."""
    with _lock:
        if _state["refreshing"] and not force:
            return
        _state["refreshing"] = True
    try:
        data = _fetch_all()
        with _lock:
            if data["fires"] or _state["fires"] is None:
                _state["fires"] = data["fires"]
            if data["alerts"] or _state["alerts"] is None:
                _state["alerts"] = data["alerts"]
            _state["errors"] = data["errors"]
            # A failed download (e.g. no internet) is retried after 5 minutes, not a full cycle.
            failed = not data["fires"] and len(data["errors"]) >= len(FIRMS_FEEDS)
            _state["fetched_at"] = time.time() - (max(0, _refresh_sec() - 300) if failed else 0)
        logger.info("Live hazards refreshed: %d fire detections, %d alerts, %d errors",
                    len(data["fires"]), len(data["alerts"]), len(data["errors"]))
    finally:
        with _lock:
            _state["refreshing"] = False


def _ensure_fresh(wait: bool) -> None:
    with _lock:
        have = _state["fires"] is not None
        age = time.time() - _state["fetched_at"]
        busy = _state["refreshing"]
    if not have and _load_from_disk():
        with _lock:
            have, age = True, time.time() - _state["fetched_at"]
    if have and age < _refresh_sec():
        return
    if wait:
        refresh()
    elif not busy:
        threading.Thread(target=refresh, daemon=True, name="live-hazards").start()


# ── Queries ─────────────────────────────────────────────────────────────────

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _hours_ago(when: datetime, now: datetime) -> float:
    return max(0.0, (now - when).total_seconds() / 3600)


def fires_in_bbox(fires: list[dict], bbox, now: datetime | None = None) -> list[dict]:
    """Detections inside the zone (padded by FIRE_ZONE_PAD_M), newest first, near-duplicates merged.

    NOAA-20 and NOAA-21 pass ~50 minutes apart and often see the same fire;
    detections within ~400 m of a newer one are merged into it.
    """
    now = now or datetime.now(timezone.utc)
    w, s, e, n = bbox
    lat_pad = FIRE_ZONE_PAD_M / 111320
    lon_pad = FIRE_ZONE_PAD_M / (111320 * max(0.1, math.cos(math.radians((s + n) / 2))))
    hits = [f for f in fires
            if s - lat_pad <= f["lat"] <= n + lat_pad and w - lon_pad <= f["lon"] <= e + lon_pad
            and _hours_ago(f["time"], now) <= FIRE_MAX_AGE_H]
    hits.sort(key=lambda f: f["time"], reverse=True)
    merged: list[dict] = []
    for f in hits:
        for m in merged:
            if _haversine_m(f["lat"], f["lon"], m["lat"], m["lon"]) < 400:
                m["detections"] += 1
                break
        else:
            merged.append({**f, "detections": 1})
    return merged


def alerts_near(alerts: list[dict], bbox) -> list[dict]:
    """GDACS alerts whose type/level radius reaches the zone, most severe first."""
    w, s, e, n = bbox
    clat, clon = (s + n) / 2, (w + e) / 2
    out = []
    for a in alerts:
        radius_km = GDACS_RADIUS_KM.get(a["type"], {}).get(a["level"], 0)
        if radius_km <= 0:
            continue
        d_km = _haversine_m(clat, clon, a["lat"], a["lon"]) / 1000
        inside = False
        bb = a.get("bbox")
        if bb and len(bb) == 4 and bb[0] != bb[2]:
            inside = bb[0] <= clon <= bb[2] and bb[1] <= clat <= bb[3]
        if inside or d_km <= radius_km:
            out.append({**a, "distance_km": 0 if inside else round(d_km)})
    out.sort(key=lambda a: (LEVEL_ORDER.get(a["level"], 3), a["distance_km"]))
    return out


def _age_text(hours: float) -> str:
    if hours < 1:
        return "under 1 h ago"
    return f"{int(round(hours))} h ago"


def fires_geojson(fires: list[dict], now: datetime) -> dict:
    feats = []
    for f in fires:
        h = _hours_ago(f["time"], now)
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(f["lon"], 5), round(f["lat"], 5)]},
            "properties": {
                "detected_at": f["time"].isoformat(),
                "hours_ago": round(h, 1),
                "age_text": _age_text(h),
                "confidence": f["confidence"],
                "frp_mw": round(f["frp"], 1),
                "satellite": f["satellite"],
                "detections": f["detections"],
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def hazards_for_bbox(bbox, wait: bool = False, now: datetime | None = None) -> dict:
    """Live hazards for a zone: {fires (GeoJSON), alerts, sources, updated, pending, notes}."""
    if not enabled():
        return {"enabled": False, "fires": {"type": "FeatureCollection", "features": []},
                "alerts": [], "sources": [], "updated": None, "pending": False, "notes": []}
    _ensure_fresh(wait)
    now = now or datetime.now(timezone.utc)
    with _lock:
        fires_all = _state["fires"]
        alerts_all = _state["alerts"] or []
        fetched = _state["fetched_at"]
        errors = list(_state["errors"])
        busy = _state["refreshing"]
    pending = fires_all is None
    fires = fires_in_bbox(fires_all or [], bbox, now)
    alerts = alerts_near(alerts_all, bbox)
    notes = []
    if errors and not pending:
        notes.append("Some live feeds could not be refreshed; showing the last download.")
    updated = datetime.fromtimestamp(fetched, timezone.utc).isoformat() if fetched else None
    return {
        "enabled": True,
        "fires": fires_geojson(fires, now),
        "alerts": [{k: v for k, v in a.items() if k != "bbox"} for a in alerts],
        "sources": [
            {"name": "NASA FIRMS (VIIRS, last 48 h)", "url": FIRMS_INFO_URL},
            {"name": "GDACS disaster alerts", "url": "https://www.gdacs.org/"},
        ],
        "updated": updated,
        "pending": pending or (busy and not fires_all),
        "notes": notes,
    }


def annotate_routes_with_hazards(result: dict, hazards: dict) -> None:
    """Add 'fire or heat detected near this route' warnings. Never changes the routes."""
    feats = (hazards or {}).get("fires", {}).get("features") or []
    feats = [f for f in feats if f["properties"].get("confidence") != "low"]
    for route in result.get("routes", []):
        route["hazard_warnings"] = []
        coords = (route.get("geojson") or {}).get("geometry", {}).get("coordinates") or []
        if not feats or len(coords) < 2:
            continue
        near = []
        for f in feats:
            lon, lat = f["geometry"]["coordinates"]
            d = min(_point_segment_m(lat, lon, a, b) for a, b in zip(coords, coords[1:]))
            if d <= FIRE_ROUTE_NEAR_M:
                near.append((d, f["properties"]))
        if not near:
            continue
        near.sort(key=lambda x: x[0])
        d, props = near[0]
        newest = min(p["hours_ago"] for _, p in near)
        if len(near) == 1:
            msg = f"Fire or heat detected {int(round(d / 10) * 10)} m from this route, {props['age_text']}"
        else:
            msg = (f"{len(near)} fire or heat detections within {FIRE_ROUTE_NEAR_M} m of this route, "
                   f"latest {_age_text(newest)}")
        route["hazard_warnings"].append(msg + " (NASA FIRMS satellite, unverified)")
        route["hazards_nearby"] = len(near)


def _point_segment_m(lat, lon, a, b) -> float:
    """Distance (m) from a point to segment a-b ([lon, lat] pairs), equirectangular approximation."""
    k = math.cos(math.radians(lat))
    px, py = lon * k * 111320, lat * 111320
    ax, ay = a[0] * k * 111320, a[1] * 111320
    bx, by = b[0] * k * 111320, b[1] * 111320
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
