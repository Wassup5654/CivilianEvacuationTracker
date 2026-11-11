"""Opt-in xBD-S12 building-damage backend for finder.damage.

Requires the official prs-eth/xbd-s12 repository installed as a package, torch,
its dependencies, and four aligned 4 m, 128x128-compatible multispectral rasters.
This is building-damage classification, NOT road-passability detection.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np

MODEL_LOCALIZATION = "prs-eth/xbd-s12_loc_seed1"
MODEL_DAMAGE = "prs-eth/xbd-s12_dmg_seed1"
REQUIRED_FILES = ("s1_pre_4m.tif", "s1_post_4m.tif", "s2_pre_4m.tif", "s2_post_4m.tif")


class ModelInputError(RuntimeError):
    pass


def _date(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError, AttributeError) as exc:
        raise ModelInputError(f"Invalid or missing acquisition/event date: {value!r}") from exc


def validate_inputs(folder: Path) -> tuple[Path, Path, Path, Path]:
    """Refuse RGB previews, incomplete pairs, or scenes that straddle no event."""
    files = tuple(folder / name for name in REQUIRED_FILES)
    missing = [p.name for p in files if not p.is_file()]
    if missing:
        raise ModelInputError("xBD-S12 requires prepared aligned 4 m rasters: " + ", ".join(missing))

    meta_path = folder / "square.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelInputError("square.json must contain event_date and s1_pre/s1_post acquisition dates") from exc

    event = _date(meta.get("event_date"))
    s2_pre = _date((meta.get("earlier") or {}).get("captured"))
    s2_post = _date((meta.get("latest") or {}).get("captured"))
    s1_pre = _date(meta.get("s1_pre_captured"))
    s1_post = _date(meta.get("s1_post_captured"))
    if not (s2_pre < event <= s2_post and s1_pre < event <= s1_post):
        raise ModelInputError("Both Sentinel-1 and Sentinel-2 pairs must straddle event_date")

    import rasterio

    with rasterio.open(files[2]) as reference:
        if reference.count != 12 or reference.crs is None or not reference.crs.is_projected:
            raise ModelInputError("S2 must contain 12 reflectance bands and a projected CRS")
        if not 3.0 <= abs(reference.transform.a) <= 5.0 or not 3.0 <= abs(reference.transform.e) <= 5.0:
            raise ModelInputError("Input pixel spacing must be approximately 4 m; RGB 10 m clips are incompatible")
        shape, transform, crs = reference.shape, reference.transform, reference.crs
    for path, channels in ((files[0], 2), (files[1], 2), (files[3], 12)):
        with rasterio.open(path) as raster:
            if (raster.count != channels or raster.shape != shape or
                    raster.transform != transform or raster.crs != crs):
                raise ModelInputError(f"{path.name} must be co-registered with s2_pre_4m.tif and have {channels} bands")
    return files


@lru_cache(maxsize=1)
def _model():
    # Install the audited official source separately. Do not execute code from a Hub model repo.
    from src.inference.from_hub import InferenceFromHub

    return InferenceFromHub(loc_models=[MODEL_LOCALIZATION], dmg_models=[MODEL_DAMAGE])


def damaged_building_mask(folder: Path, output_size: tuple[int, int]) -> np.ndarray:
    """Return an HxW binary damaged-building mask; 1 is NOT calibrated confidence."""
    s1_pre, s1_post, s2_pre, s2_post = validate_inputs(folder)
    classes = np.asarray(_model().run_inference(
        fp_s2_pre=s2_pre, fp_s2_post=s2_post,
        fp_s1_pre=s1_pre, fp_s1_post=s1_post,
    ))
    if classes.ndim != 2 or classes.size == 0:
        raise ModelInputError("xBD-S12 returned an invalid class raster")
    if not np.isin(classes, [0, 1, 2]).all():
        raise ModelInputError("xBD-S12 produced unexpected class IDs")

    # Map the model's 4 m output to the native 10 m GeoJSON grid without smoothing labels.
    # Resizing is valid only if both rasters cover the same bounds (checked below).
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(s2_pre) as src, rasterio.open(folder / "latest.tif") as dst:
        if dst.crs is None:
            raise ModelInputError("latest.tif must have georeferencing to align the model output")
        src_bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        dst_bounds = transform_bounds(dst.crs, "EPSG:4326", *dst.bounds)
        if any(abs(a - b) > 1e-5 for a, b in zip(src_bounds, dst_bounds)):
            raise ModelInputError("Prepared model rasters and latest.tif must cover the same area")
        if classes.shape != src.shape or output_size != (dst.width, dst.height):
            raise ModelInputError("Model output or 10 m clip has an unexpected shape")
        from rasterio.warp import reproject, Resampling
        output = np.zeros((dst.height, dst.width), dtype=np.uint8)
        reproject(
            source=(classes == 2).astype(np.uint8), destination=output,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=dst.transform, dst_crs=dst.crs,
            resampling=Resampling.nearest,
        )
    return output.astype(np.float32)
