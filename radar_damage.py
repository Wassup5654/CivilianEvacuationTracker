"""Estimated war damage from Sentinel-1 radar change, for configured conflict regions.

1. Find the ConflictRegion covering the zone and build two comparisons
   ("layers"; see layer_periods()):
   - war_start: the 12 months before the war vs its first 90 days;
   - recent:    the 12 months before the last 90 days vs the last 90 days.
   (Pre-war vs today does not work for long wars: years of ordinary urban
   change and a changed satellite constellation swamp the damage signal.)
2. Download Sentinel-1 RTC backscatter (VV and VH, 10 m) for the zone from
   Microsoft Planetary Computer: up to 3 orbit tracks with enough scenes in
   both periods, up to 30 + 15 scenes per track.
3. Score every pixel with a gradient-boosted model (models/radar_damage_model.joblib)
   trained on UNOSAT building labels from 7 Ukrainian cities, using 24 per-pixel
   change features (pixel_features()). If the model can't be loaded, fall back to
   the best hand-tuned statistic: change relative to pre-war variability (zpre_map()).
4. Keep only built-up pixels (ESA WorldCover 2021, class 50).
5. Pixels above the calibrated thresholds become "area" features ("possibly
   damaged" / "damaged"). If more than 30% of the built-up area is flagged the
   layer is withheld as systematic noise.

Accuracy (evaluation/, pixel-level AUC on building pixels, 0.5 = chance), on 6
held-out sites never used for tuning or training (4 Ukrainian cities, Gaza City,
Khan Younis): trained model 0.70, tuned statistic 0.68, previous t-test 0.56.
Gaza City 0.72. Thresholds do not transfer evenly between places: at the
"possibly damaged" level held-out sites caught 0-48% of damaged buildings with
0-22% false alarms. These are ESTIMATES: always labelled as such and, being
"area" features, they never change routes; they only add route-card warnings.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import threading
import time
import urllib.error
import warnings
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage
from shapely.geometry import mapping, shape

logger = logging.getLogger("finder.radar")

STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
DATA_BBOX = "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
USER_AGENT = "civilian-evacuation/1.0 (radar damage estimate)"

BASELINE_DAYS = 365
RECENT_DAYS = 90
MAX_BASELINE_SCENES = 30      # per orbit track (matches the evaluation/training setup)
MAX_RECENT_SCENES = 15        # per orbit track
MIN_BASELINE_SCENES = 6
MIN_RECENT_SCENES = 3
MAX_TRACKS = 3                # all usable tracks helped in evaluation
PIXEL_M = 10.0
MAX_PX = 400
SMOOTH_RADII_M = (50, 100, 150)
T_POSSIBLE = 3.0
T_LIKELY = 5.0
MIN_BLOB_PX = 9               # ~900 m^2
MIN_BUILT_PX = 50
MAX_FLAGGED_FRACTION = 0.30   # more than this of the built-up area = systematic, not damage
BUILT_UP_CLASS = 50
WORKERS = 6
HTTP_TIMEOUT = 60
CACHE_TTL = 7 * 24 * 3600

CACHE_DIR = Path("cache") / "radar"
SOURCE_LABEL = "Radar estimate (Sentinel-1)"
ATTRIBUTION = "Estimate from modified Copernicus Sentinel-1 data; built-up mask © ESA WorldCover 2021"

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class RadarUnavailable(Exception):
    """Not enough radar data to produce an estimate."""


# ── Region lookup ──

def match_region(bbox: tuple[float, float, float, float]):
    """The active ConflictRegion whose box contains the zone centre (smallest box wins)."""
    from finder.models import ConflictRegion

    lon = (bbox[0] + bbox[2]) / 2
    lat = (bbox[1] + bbox[3]) / 2
    regions = ConflictRegion.objects.filter(
        active=True, west__lte=lon, east__gte=lon, south__lte=lat, north__gte=lat
    )
    return min(regions, key=lambda r: r.box_area, default=None)


# ── Data access ──

def _http(url: str, data: bytes | None = None, retries: int = 2) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def _search(collection: str, bbox, start: date, end: date) -> list[dict]:
    body = json.dumps({
        "collections": [collection],
        "bbox": list(bbox),
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": 1000,
    }).encode()
    return json.loads(_http(STAC_SEARCH, data=body)).get("features", [])


def _fetch_npy(collection: str, item_id: str, bbox, width: int, height: int, assets: list[str]) -> np.ndarray:
    b = ",".join(f"{v:.6f}" for v in bbox)
    params = f"collection={collection}&item={item_id}" + "".join(f"&assets={a}" for a in assets)
    raw = _http(f"{DATA_BBOX}/{b}/{width}x{height}.npy?{params}")
    return np.load(io.BytesIO(raw), allow_pickle=False)


def _grid_size(bbox) -> tuple[int, int]:
    west, south, east, north = bbox
    lat = math.radians((south + north) / 2)
    width_m = (east - west) * 111_320 * max(math.cos(lat), 0.01)
    height_m = (north - south) * 111_320
    w = int(min(MAX_PX, max(16, round(width_m / PIXEL_M))))
    h = int(min(MAX_PX, max(16, round(height_m / PIXEL_M))))
    return w, h


def _pick_tracks(baseline: list[dict], recent: list[dict]) -> list[tuple[str, int]]:
    def track(f):
        p = f.get("properties", {})
        return (p.get("sat:orbit_state"), p.get("sat:relative_orbit"))

    counts: dict[tuple, list[int]] = {}
    for f in baseline:
        counts.setdefault(track(f), [0, 0])[0] += 1
    for f in recent:
        counts.setdefault(track(f), [0, 0])[1] += 1
    usable = [(k, c) for k, c in counts.items()
              if c[0] >= MIN_BASELINE_SCENES and c[1] >= MIN_RECENT_SCENES]
    usable.sort(key=lambda kc: min(kc[1][0], kc[1][1] * 2), reverse=True)
    return [k for k, _ in usable[:MAX_TRACKS]]


def _sample(items: list[dict], n: int, most_recent: bool) -> list[dict]:
    items = sorted(items, key=lambda f: f["properties"].get("datetime", ""))
    if len(items) <= n:
        return items
    if most_recent:
        return items[-n:]
    idx = np.linspace(0, len(items) - 1, n).round().astype(int)
    return [items[i] for i in sorted(set(idx))]


# ── Statistics ──

def _to_db(arr: np.ndarray) -> np.ndarray:
    """(3, h, w) VV, VH, mask -> (2, h, w) dB with NaN for no-data."""
    vals = arr[:2].astype(np.float64)
    nodata = (arr[2] == 0) if arr.shape[0] > 2 else np.zeros(vals.shape[1:], dtype=bool)
    out = 10.0 * np.log10(np.clip(vals, 1e-6, None))
    out[:, nodata] = np.nan
    out[:, ~np.isfinite(vals).all(axis=0)] = np.nan
    return out


def welch_t(baseline: np.ndarray, recent: np.ndarray, reference: np.ndarray | None = None) -> np.ndarray:
    """Per-pixel Welch t-statistic between stacks (n, h, w); NaN where undefined.

    If `reference` (a boolean mask) is given, the median change over those
    pixels is subtracted first, so a zone-wide shift (sensor calibration, a
    different satellite pair, processing changes, wet vs dry season) is not
    mistaken for damage; only local anomalies remain.
    """
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # pixels with no data -> NaN
        n1 = np.sum(np.isfinite(baseline), axis=0)
        n2 = np.sum(np.isfinite(recent), axis=0)
        m1, m2 = np.nanmean(baseline, axis=0), np.nanmean(recent, axis=0)
        v1, v2 = np.nanvar(baseline, axis=0, ddof=1), np.nanvar(recent, axis=0, ddof=1)
        delta = m2 - m1
        if reference is not None:
            ref_vals = delta[reference & np.isfinite(delta)]
            if ref_vals.size:
                delta = delta - np.median(ref_vals)
        t = delta / np.sqrt(v1 / n1 + v2 / n2)
    t[(n1 < 2) | (n2 < 2)] = np.nan
    return t


def change_map(tracks: list[tuple[np.ndarray, np.ndarray]], reference: np.ndarray | None = None) -> np.ndarray:
    """Combine per-track (baseline_db, recent_db) stacks into one smoothed |t| map.

    Each stack is (n, 2, h, w) with VV and VH in dB. `reference` marks the
    pixels (built-up area) whose median change is removed; see welch_t().
    """
    per_track = []
    for base, rec in tracks:
        t_vv = np.abs(welch_t(base[:, 0], rec[:, 0], reference))
        t_vh = np.abs(welch_t(base[:, 1], rec[:, 1], reference))
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            per_track.append(np.nanmean(np.stack([t_vv, t_vh]), axis=0))
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        tmap = np.nanmax(np.stack(per_track), axis=0)
    tmap = np.nan_to_num(tmap, nan=0.0)
    layers = [tmap]
    for r in SMOOTH_RADII_M:
        size = 2 * max(1, round(r / PIXEL_M)) + 1
        layers.append(ndimage.uniform_filter(tmap, size=size, mode="nearest"))
    return np.mean(layers, axis=0)


FEATURE_NAMES = [
    "abs_t_vv_mean", "abs_t_vh_mean", "abs_t_vv_max", "abs_t_vh_max",
    "t_vv_mean", "t_vh_mean", "delta_vv", "delta_vh",
    "log_std_ratio_vv", "log_std_ratio_vh", "pre_vv", "pre_vh", "pre_vh_minus_vv",
    "abs_t_s50", "abs_t_s150", "delta_vv_s50", "delta_vh_s50", "delta_vv_s150", "delta_vh_s150",
    "built_frac_100m",
    "z_vv_max", "z_vh_max", "z_s50", "z_s150",
]


def pixel_features(tracks: list[tuple[np.ndarray, np.ndarray]], built: np.ndarray) -> np.ndarray:
    """Per-pixel features (len(FEATURE_NAMES), h, w) for the trained damage model.

    `tracks` holds (baseline, compare) stacks of shape (n, 2, h, w), VV/VH in dB.
    Used identically by evaluation/experiments.py (training) and at run time.
    """
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        per = {k: [] for k in ("t", "delta", "lsr", "pre", "z")}
        for base, rec in tracks:
            t_b, d_b, l_b, p_b, z_b = [], [], [], [], []
            for b in (0, 1):
                x1, x2 = base[:, b], rec[:, b]
                m1, m2 = np.nanmean(x1, 0), np.nanmean(x2, 0)
                s1, s2 = np.nanstd(x1, 0, ddof=1), np.nanstd(x2, 0, ddof=1)
                delta = m2 - m1
                ref = delta[built & np.isfinite(delta)]
                if ref.size:
                    delta = delta - np.median(ref)
                n1 = np.sum(np.isfinite(x1), 0)
                n2 = np.sum(np.isfinite(x2), 0)
                t_b.append(delta / np.sqrt(s1 ** 2 / n1 + s2 ** 2 / n2))
                d_b.append(delta)
                l_b.append(np.log(s2 / s1))
                p_b.append(m1)
                z_b.append(np.abs(delta) / s1)
            per["t"].append(t_b)
            per["delta"].append(d_b)
            per["lsr"].append(l_b)
            per["pre"].append(p_b)
            per["z"].append(z_b)
        t = np.array(per["t"])          # (tracks, 2, h, w)
        delta = np.nanmean(np.array(per["delta"]), 0)
        lsr = np.nanmean(np.array(per["lsr"]), 0)
        pre = np.nanmean(np.array(per["pre"]), 0)
        abs_t_mean = np.nanmean(np.abs(t), 0)
        abs_t_max = np.nanmax(np.abs(t), 0)
        t_mean = np.nanmean(t, 0)
        z_max = np.nanmax(np.array(per["z"]), 0)        # (2, h, w): max over tracks
        z_both = np.nanmax(z_max, 0)

    def sm(a, r):
        size = 2 * max(1, round(r / PIXEL_M)) + 1
        return ndimage.uniform_filter(np.nan_to_num(a, nan=0.0), size=size, mode="nearest")

    abs_t_both = np.nanmean(abs_t_mean, 0)
    feats = [
        abs_t_mean[0], abs_t_mean[1], abs_t_max[0], abs_t_max[1],
        t_mean[0], t_mean[1], delta[0], delta[1],
        lsr[0], lsr[1], pre[0], pre[1], pre[1] - pre[0],
        sm(abs_t_both, 50), sm(abs_t_both, 150),
        sm(delta[0], 50), sm(delta[1], 50), sm(delta[0], 150), sm(delta[1], 150),
        sm(built.astype(float), 100),
        z_max[0], z_max[1], sm(z_both, 50), sm(z_both, 150),
    ]
    return np.stack(feats).astype(np.float32)


def zone_robust_z(score: np.ndarray, built: np.ndarray) -> np.ndarray:
    """Score relative to the zone's own built-up pixels: (s - median) / (1.4826 * MAD).

    Removes place-to-place offsets in the model's probability level so the
    same thresholds can be used everywhere.
    """
    vals = score[built & np.isfinite(score)]
    if vals.size < MIN_BUILT_PX:
        return np.zeros_like(score)
    med = float(np.median(vals))
    mad = float(np.median(np.abs(vals - med))) * 1.4826
    return (score - med) / max(mad, 1e-6)


MODEL_PATH = Path(__file__).resolve().parent / "models" / "radar_damage_model.joblib"
_model_cache: dict[str, Any] = {}


def load_model() -> tuple[Any, dict | None]:
    """(trained model or None, metadata or None). Never raises; logs why it fell back."""
    if "entry" not in _model_cache:
        model, meta = None, None
        try:
            meta = json.loads(MODEL_PATH.with_suffix(".json").read_text(encoding="utf-8"))
            if meta.get("features") != FEATURE_NAMES:
                raise ValueError("model was trained on different features")
            import joblib
            model = joblib.load(MODEL_PATH)
        except Exception as exc:
            logger.warning("Radar damage model unavailable, using the tuned statistic: %s", exc)
        _model_cache["entry"] = (model, meta)
    return _model_cache["entry"]


def smooth_layers(arr: np.ndarray, radii_m) -> np.ndarray:
    """Mean of the map and its neighbourhood means at each radius (as used in evaluation)."""
    layers = [arr]
    for r in radii_m:
        size = 2 * max(1, round(r / PIXEL_M)) + 1
        layers.append(ndimage.uniform_filter(arr, size=size, mode="nearest"))
    return np.mean(layers, axis=0)


def zpre_map(tracks: list[tuple[np.ndarray, np.ndarray]], built: np.ndarray) -> np.ndarray:
    """Tuned statistic: |change in mean dB| relative to pre-war variability.

    Per track and polarisation: |mean_after - mean_before - zone median change| /
    std_before; max over VV/VH and over tracks; smoothed at 50/100/150 m.
    Best hand-tuned score in evaluation (mean held-out AUC 0.68 vs 0.56 for Welch's t).
    """
    per_track = []
    with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for base, rec in tracks:
            bands = []
            for b in (0, 1):
                d = np.nanmean(rec[:, b], 0) - np.nanmean(base[:, b], 0)
                ref = d[built & np.isfinite(d)]
                if ref.size:
                    d = d - np.median(ref)
                bands.append(np.abs(d) / np.nanstd(base[:, b], 0, ddof=1))
            per_track.append(np.nanmax(bands, 0))
        tm = np.nanmax(per_track, 0)
    return smooth_layers(np.nan_to_num(tm, nan=0.0), SMOOTH_RADII_M)


def score_map(tracks: list[tuple[np.ndarray, np.ndarray]], built: np.ndarray) -> dict[str, Any]:
    """Damage score per pixel plus the thresholds and description to use with it."""
    model, meta = load_model()
    if model is not None:
        f = pixel_features(tracks, built)
        prob = model.predict_proba(f.reshape(f.shape[0], -1).T)[:, 1].reshape(f.shape[1:])
        thr = meta["thresholds"]
        return {"score": smooth_layers(prob, (meta.get("smooth_m", 50),)), "method": "trained model",
                "possible": thr["possibly_damaged"], "likely": thr["damaged"], "label": "Model score {:.2f}"}
    thr = (meta or {}).get("fallback_thresholds")
    if thr:
        return {"score": zpre_map(tracks, built), "method": "tuned statistic",
                "possible": thr["possibly_damaged"], "likely": thr["damaged"], "label": "Change score {:.1f}"}
    return {"score": change_map(tracks, reference=built), "method": "t-test",
            "possible": T_POSSIBLE, "likely": T_LIKELY, "label": "Radar change, t ≈ {:.1f}"}


def change_to_features(tmap: np.ndarray, built: np.ndarray, bbox, possible: float = T_POSSIBLE,
                       likely: float = T_LIKELY, label: str = "Radar change, t ≈ {:.1f}") -> tuple[list[dict], dict]:
    """Turn a score map into estimated damage areas (built-up pixels only).

    score >= `possible` -> "possibly damaged"; mean score >= `likely` -> "damaged".
    """
    from damage import extract_polygon_blobs, probability_map_to_blobs

    stats = {"built_px": int(built.sum()), "flagged_fraction": 0.0}
    if built.sum() < MIN_BUILT_PX:
        stats["reason"] = "little built-up area in this zone"
        return [], stats
    masked = np.where(built, tmap, 0.0)
    flagged = masked >= possible
    stats["flagged_fraction"] = round(float(flagged.sum()) / float(built.sum()), 4)
    if stats["flagged_fraction"] > MAX_FLAGGED_FRACTION:
        stats["reason"] = (
            f"{stats['flagged_fraction']:.0%} of the built-up area changed, which points to a "
            "systematic difference between periods rather than damage"
        )
        return [], stats

    blobs = probability_map_to_blobs(masked, threshold=possible, min_area_px=MIN_BLOB_PX) & built
    gj, geo_polys, _ = extract_polygon_blobs(blobs, tuple(bbox), prob_map=masked)
    feats = []
    for feat, geom in zip(gj["features"], geo_polys):
        mean_s = float(feat["properties"]["damage_probability"])
        feats.append({
            "kind": "area",
            "grade": "damaged" if mean_s >= likely else "possibly_damaged",
            "geom": geom,
            "label": label.format(mean_s),
            "estimated": True,
        })
    return feats, stats


# ── Layers ──

LAYERS = {
    "war_start": {
        "title": "Estimated damage in the war's first 3 months",
        "cache_ttl": None,  # historic periods never change
    },
    "recent": {
        "title": "Estimated new change in the last 12 months",
        "cache_ttl": CACHE_TTL,
    },
}


def _shift_year(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 February
        return (d - timedelta(days=1)).replace(year=d.year + years)


def layer_periods(layer: str, ref: date, today: date) -> tuple[date, date, date, date] | None:
    """(baseline_start, baseline_end, compare_start, compare_end) for a layer, or None.

    war_start: the full year before the war vs its first RECENT_DAYS (validated
               against UNOSAT in Kharkiv: AUC ~0.70, ~1-2% false change).
    recent:    the last RECENT_DAYS vs the full year before them, the same
               shape of comparison the model was trained on, within a span
               short enough that satellites and the city itself barely change.
               Comparing pre-war data with today fails for long wars: years of
               ordinary urban change swamp the damage signal.
    """
    if layer == "war_start":
        rec_end = min(ref + timedelta(days=RECENT_DAYS), today)
        if (rec_end - ref).days < 30:
            return None  # war too recent for a meaningful window
        return ref - timedelta(days=BASELINE_DAYS), ref - timedelta(days=1), ref, rec_end
    if layer == "recent":
        if (today - ref).days < 365:
            return None  # still covered by the war_start layer
        rec_start = today - timedelta(days=RECENT_DAYS)
        return rec_start - timedelta(days=BASELINE_DAYS), rec_start - timedelta(days=1), rec_start, today
    raise ValueError(layer)


def _compute_layer(bbox, layer: str, region_name: str, periods: tuple[date, date, date, date]) -> dict[str, Any]:
    base_start, base_end, rec_start, rec_end = periods
    period = f"baseline {base_start}–{base_end}, compared with {rec_start}–{rec_end}"
    source = {
        "name": f"{LAYERS[layer]['title']} ({region_name})",
        "provider": "Estimate (Sentinel-1 radar)",
        "date": period,
        "attribution": ATTRIBUTION,
        "url": "",
    }

    baseline = _search("sentinel-1-rtc", bbox, base_start, base_end)
    recent = _search("sentinel-1-rtc", bbox, rec_start, rec_end)
    tracks = _pick_tracks(baseline, recent)
    if not tracks:
        raise RadarUnavailable(
            f"not enough Sentinel-1 radar scenes ({len(baseline)} baseline, {len(recent)} compared)"
        )

    width, height = _grid_size(bbox)
    jobs: list[tuple[int, str, str]] = []   # (track index, role, item id)
    for ti, trk in enumerate(tracks):
        def on_track(f, trk=trk):
            p = f["properties"]
            return (p.get("sat:orbit_state"), p.get("sat:relative_orbit")) == trk
        for f in _sample([f for f in baseline if on_track(f)], MAX_BASELINE_SCENES, most_recent=False):
            jobs.append((ti, "base", f["id"]))
        for f in _sample([f for f in recent if on_track(f)], MAX_RECENT_SCENES, most_recent=True):
            jobs.append((ti, "rec", f["id"]))

    def fetch(job):
        ti, role, item = job
        try:
            return ti, role, _to_db(_fetch_npy("sentinel-1-rtc", item, bbox, width, height, ["vv", "vh"]))
        except Exception as exc:
            logger.warning("Radar scene %s failed: %s", item, exc)
            return ti, role, None

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(fetch, jobs))

    stacks = []
    for ti in range(len(tracks)):
        base = [a for t, r, a in results if t == ti and r == "base" and a is not None]
        rec = [a for t, r, a in results if t == ti and r == "rec" and a is not None]
        if len(base) >= MIN_BASELINE_SCENES and len(rec) >= MIN_RECENT_SCENES:
            stacks.append((np.stack(base), np.stack(rec)))
    if not stacks:
        raise RadarUnavailable("too many radar scene downloads failed; try again later")

    built = _built_up(bbox, width, height)
    sc = score_map(stacks, built)
    feats, stats = change_to_features(sc["score"], built, bbox, sc["possible"], sc["likely"], sc["label"])
    stats["method"] = sc["method"]
    notes = []
    if stats.get("reason"):
        notes.append(f"{LAYERS[layer]['title']} not shown: {stats['reason']}.")
    stats.update({
        "tracks": [list(t) for t in tracks],
        "scenes": sum(len(b) + len(r) for b, r in stacks),
        "period": period,
    })
    for f in feats:
        f.update(source=SOURCE_LABEL, date=f"{rec_start}–{rec_end}", layer=layer)
    return {"features": feats, "sources": [source], "notes": notes, "stats": stats}


def _built_up(bbox, width: int, height: int) -> np.ndarray:
    wc = _search("esa-worldcover", bbox, date(2021, 1, 1), date(2021, 12, 31))
    if not wc:
        raise RadarUnavailable("no ESA WorldCover built-up map for this zone")
    landcover = _fetch_npy("esa-worldcover", wc[0]["id"], bbox, width, height, ["map"])[0]
    return landcover == BUILT_UP_CLASS


def _serialise(result: dict) -> dict:
    out = dict(result)
    out["features"] = [{**f, "geom": mapping(f["geom"])} for f in result["features"]]
    return out


def _deserialise(data: dict) -> dict:
    data["features"] = [{**f, "geom": shape(f["geom"])} for f in data["features"]]
    return data


METHOD_VERSION = "2"  # bump when scoring changes, so permanently cached layers are recomputed


_running: set[str] = set()


def _cache_entry(bbox, layer: str, periods) -> tuple[str, Path, float | None]:
    method = "model" if load_model()[0] is not None else "fallback"
    key_src = f"{layer}|{METHOD_VERSION}|{method}|{[round(v, 3) for v in bbox]}|{periods}"
    key = hashlib.sha1(key_src.encode()).hexdigest()
    return key, CACHE_DIR / f"{layer}_{key}.json", LAYERS[layer]["cache_ttl"]


_mem_cache: dict[str, tuple[float, dict]] = {}


def _disk_cache_enabled() -> bool:
    """settings.CACHE_AREAS_ON_DISK (default True): False keeps radar layers in memory only."""
    from django.conf import settings
    return bool(getattr(settings, "CACHE_AREAS_ON_DISK", True))


def _read_cache(path: Path, ttl: float | None) -> dict | None:
    if not _disk_cache_enabled():
        entry = _mem_cache.get(path.name)
        if entry and (ttl is None or time.time() - entry[0] < ttl):
            return _deserialise(json.loads(json.dumps(entry[1])))
        return None
    if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
        try:
            return _deserialise(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None
    return None


def _compute_and_store(key: str, path: Path, ttl, bbox, layer: str, region_name: str, periods) -> dict[str, Any]:
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:  # never compute the same layer twice at once
        cached = _read_cache(path, ttl)
        if cached is not None:
            return cached
        try:
            result = _compute_layer(bbox, layer, region_name, periods)
        except RadarUnavailable as exc:
            result = {"features": [], "sources": [], "stats": {},
                      "notes": [f"{LAYERS[layer]['title']} unavailable: {exc}."]}
        except Exception as exc:
            logger.warning("Radar estimate (%s) failed: %s", layer, exc)
            return {"features": [], "sources": [], "stats": {},
                    "notes": [f"{LAYERS[layer]['title']} could not be computed (data service unreachable)."]}
        if not _disk_cache_enabled():
            _mem_cache[path.name] = (time.time(), _serialise(result))
            return result
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(_serialise(result)), encoding="utf-8")
        except Exception:
            pass
        return result


def _background(key, *args) -> None:
    try:
        _compute_and_store(key, *args)
    finally:
        with _locks_guard:
            _running.discard(key)


def _layer_cached(bbox, layer: str, region, periods, wait: bool = True) -> dict[str, Any]:
    """A layer's result from cache, computing it now (wait=True) or in a background thread.

    With wait=False an uncached layer returns immediately with "pending": True,
    so map requests are not held up by ~1 minute of radar downloads.
    """
    key, path, ttl = _cache_entry(bbox, layer, periods)
    cached = _read_cache(path, ttl)
    if cached is not None:
        return cached
    if wait:
        return _compute_and_store(key, path, ttl, bbox, layer, region.name, periods)
    with _locks_guard:
        start = key not in _running
        if start:
            _running.add(key)
    if start:
        threading.Thread(target=_background, args=(key, path, ttl, bbox, layer, region.name, periods),
                         daemon=True, name=f"radar-{layer}").start()
    return {"features": [], "sources": [], "stats": {}, "pending": True,
            "notes": [f"{LAYERS[layer]['title']} is being computed from satellite radar "
                      "(about a minute); the map will refresh when it is ready."]}


# ── Main entry point ──

def estimate_for_bbox(bbox: tuple[float, float, float, float], today: date | None = None,
                      wait: bool = True) -> dict[str, Any]:
    """Radar damage estimates for a zone (both layers), or empty outside conflict regions.

    Returns {"features", "sources", "notes", "region", "pending"}. Every feature
    is an estimated "area" with a "layer" key ("war_start" or "recent"). With
    wait=False, uncached layers are computed in the background and "pending" is True.
    """
    out: dict[str, Any] = {"features": [], "sources": [], "notes": [], "region": None, "pending": False}
    region = match_region(bbox)
    if region is None:
        return out
    out["region"] = region.name
    today = today or date.today()
    for layer in LAYERS:
        periods = layer_periods(layer, region.reference_date, today)
        if periods is None:
            continue
        res = _layer_cached(bbox, layer, region, periods, wait=wait)
        out["pending"] = out["pending"] or bool(res.get("pending"))
        out["features"] += res["features"]
        out["sources"] += res["sources"]
        out["notes"] += res["notes"]
    return out
