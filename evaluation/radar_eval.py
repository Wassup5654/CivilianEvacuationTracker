"""Evaluate and tune the Sentinel-1 radar damage estimate against UNOSAT labels.

Data (not in the repo; download into one folder and pass it as --data):
  * unosat_labels.geojson, unosat_aois.geojson
      ETH Zurich, "An open-source tool for mapping war destruction at scale in
      Ukraine using Sentinel-1 time series", Zenodo record 15088349 (CC BY 4.0).
      18,686 UNOSAT building labels from 25 Ukrainian cities (Mar-Oct 2022).
  * elfasher.zip
      UNOSAT El Fasher (Sudan) damage assessment, 31 July 2024 (CC BY-SA).

Usage:
  python evaluation/radar_eval.py fetch --data DIR     # download + cache radar stacks per site
  python evaluation/radar_eval.py run   --data DIR     # experiments on the cached stacks

Evaluation is pixel-level on a 10 m grid, on building pixels only:
  positives  = building pixels within 10 m of a UNOSAT "destroyed" or "severe" label
  negatives  = building pixels inside the analysed area and > 100 m from any damage label
Sites are split into DEV (used for choosing settings / training) and TEST (only
reported). The radar comparison for a site uses the year before the war vs the
90 days ending at the UNOSAT imagery date.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import radar_damage as rd  # noqa: E402
from scipy import ndimage  # noqa: E402

BOX_KM = 2.4
MAX_TRACKS = 3
FETCH_BASE = 30
FETCH_POST = 15
POSITIVE_CLASSES = (1, 2)          # destroyed, severe
ANY_DAMAGE_CLASSES = (1, 2, 3, 4, 5, 7)
NEG_CLEARANCE_M = 100
MIN_POSITIVES = 150

TEST_CITIES = {"Sievierodonetsk", "Lysychansk", "Avdiivka", "Moschun", "Gaza City", "Khan Younis"}

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


# ── Site selection ──

def _box_around(lon, lat, km=BOX_KM):
    dlat = km / 2 / 111.32
    dlon = km / 2 / (111.32 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def _densest(points: np.ndarray, km=BOX_KM) -> tuple[float, float]:
    lat = float(np.median(points[:, 1]))
    dlat = km / 2 / 111.32
    dlon = km / 2 / (111.32 * math.cos(math.radians(lat)))
    best, best_n = points[0], -1
    sample = points[:: max(1, len(points) // 400)]
    for p in sample:
        n = int(((np.abs(points[:, 0] - p[0]) < dlon) & (np.abs(points[:, 1] - p[1]) < dlat)).sum())
        if n > best_n:
            best, best_n = p, n
    return float(best[0]), float(best[1])


def define_sites(data: Path) -> list[dict]:
    import geopandas as gpd

    labels = gpd.read_file(data / "unosat_labels.geojson")
    aois = gpd.read_file(data / "unosat_aois.geojson").set_index("aoi")
    sites = []
    # one site per city: its first assessment layer with enough severe/destroyed labels
    for city, grp in labels.groupby("city"):
        for layer, lg in sorted(grp.groupby("layer"), key=lambda kv: kv[1]["date"].min()):
            pos = lg[lg["damage"].isin(POSITIVE_CLASSES)]
            if len(pos) < MIN_POSITIVES:
                continue
            pts = np.array([[g.x, g.y] for g in pos.geometry])
            lon, lat = _densest(pts)
            bbox = _box_around(lon, lat)
            aoi_name = lg["aoi"].iloc[0]
            label_date = lg["date"].max().date()
            in_box = labels.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]
            sites.append({
                "name": f"{city}", "layer": layer, "bbox": bbox, "ref": "2022-02-24",
                "label_date": label_date.isoformat(),
                "positives": [[g.x, g.y] for g in in_box[in_box["damage"].isin(POSITIVE_CLASSES)].geometry],
                "any_damage": [[g.x, g.y] for g in in_box[in_box["damage"].isin(ANY_DAMAGE_CLASSES)].geometry],
                "aoi": json.loads(gpd.GeoSeries([aois.loc[aoi_name].geometry]).to_json())["features"][0]["geometry"],
                "kind": "points",
            })
            break

    # Gaza Strip (UNOSAT CDA, 26 Nov 2023): comprehensive, so the whole box is analysed.
    # El Fasher (Sudan) was dropped: Sentinel-1 has almost no scenes there before the war.
    import official_damage as od
    gz = od.read_damage_file(data / "gaza_cda_nov2023.zip")
    cls = gz["Main_Damage_Site_Class_3"]
    for name, lat, lon in (("Gaza City", 31.515, 34.455), ("Khan Younis", 31.345, 34.305)):
        bbox = _box_around(lon, lat)
        inb = gz.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]
        c = cls.loc[inb.index]
        sites.append({
            "name": name, "layer": "UNOSAT_GazaStrip_CDA_26Nov2023", "bbox": bbox, "ref": "2023-10-07",
            "label_date": "2023-11-26",
            "positives": [[g.x, g.y] for g in inb[c.isin(POSITIVE_CLASSES)].geometry],
            "any_damage": [[g.x, g.y] for g in inb[c.isin((1, 2, 3))].geometry],
            "aoi": None, "kind": "points",
        })
    return sites


# ── Fetching ──

def _overpass_buildings(bbox) -> list:
    w, s, e, n = bbox
    q = f'[out:json][timeout:60];(way["building"]({s},{w},{n},{e}););out geom;'
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for attempt in range(2):
        for url in OVERPASS_MIRRORS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": rd.USER_AGENT})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    els = json.loads(resp.read())["elements"]
                break
            except Exception as exc:
                last = exc
                time.sleep(3)
        else:
            continue
        break
    else:
        raise RuntimeError(f"all Overpass mirrors failed: {last}")
    return [[(p["lon"], p["lat"]) for p in el["geometry"]] for el in els if el.get("geometry")]


def _rasterize(polys_lonlat, bbox, w, h) -> np.ndarray:
    from PIL import Image, ImageDraw

    W, S, E, N = bbox
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for ring in polys_lonlat:
        if len(ring) < 3:
            continue
        draw.polygon([((x - W) / (E - W) * w, (N - y) / (N - S) * h) for x, y in ring], fill=1, outline=1)
    return np.array(img, dtype=bool)


def _points_mask(points, bbox, w, h, radius_px) -> np.ndarray:
    W, S, E, N = bbox
    m = np.zeros((h, w), dtype=bool)
    for x, y in points:
        c, r = int((x - W) / (E - W) * w), int((N - y) / (N - S) * h)
        if 0 <= r < h and 0 <= c < w:
            m[r, c] = True
    if radius_px > 0:
        yy, xx = np.mgrid[-radius_px:radius_px + 1, -radius_px:radius_px + 1]
        m = ndimage.binary_dilation(m, structure=(xx ** 2 + yy ** 2) <= radius_px ** 2)
    return m


def _rings(geojson_geom) -> list:
    t, c = geojson_geom["type"], geojson_geom["coordinates"]
    if t == "Polygon":
        return [c[0]]
    if t == "MultiPolygon":
        return [p[0] for p in c]
    return []


def fetch_site(site: dict, out: Path) -> None:
    path = out / f"{site['name'].replace(' ', '_')}.npz"
    if path.exists():
        print(f"  {site['name']}: cached")
        return
    bbox = tuple(site["bbox"])
    ref = date.fromisoformat(site["ref"])
    label_date = date.fromisoformat(site["label_date"])
    b0, b1 = ref - timedelta(days=365), ref - timedelta(days=1)
    p1 = label_date
    p0 = max(ref, p1 - timedelta(days=90))
    base = rd._search("sentinel-1-rtc", bbox, b0, b1)
    post = rd._search("sentinel-1-rtc", bbox, p0, p1)
    old = (rd.MIN_BASELINE_SCENES, rd.MIN_RECENT_SCENES, rd.MAX_TRACKS)
    rd.MAX_TRACKS = MAX_TRACKS
    tracks = rd._pick_tracks(base, post)
    rd.MIN_BASELINE_SCENES, rd.MIN_RECENT_SCENES, rd.MAX_TRACKS = old
    if not tracks:
        print(f"  {site['name']}: no usable tracks ({len(base)} base, {len(post)} post)")
        return
    w, h = rd._grid_size(bbox)
    jobs = []
    for ti, trk in enumerate(tracks):
        on = lambda f, trk=trk: (f["properties"].get("sat:orbit_state"), f["properties"].get("sat:relative_orbit")) == trk
        for f in rd._sample([f for f in base if on(f)], FETCH_BASE, most_recent=False):
            jobs.append((ti, "b", f["id"]))
        for f in rd._sample([f for f in post if on(f)], FETCH_POST, most_recent=True):
            jobs.append((ti, "p", f["id"]))

    def get(job):
        ti, role, item = job
        try:
            return ti, role, rd._to_db(rd._fetch_npy("sentinel-1-rtc", item, bbox, w, h, ["vv", "vh"])).astype(np.float16)
        except Exception as exc:
            print(f"    scene {item} failed: {exc}")
            return ti, role, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=rd.WORKERS) as pool:
        res = list(pool.map(get, jobs))
    arrays = {}
    for ti in range(len(tracks)):
        arrays[f"b{ti}"] = np.stack([a for t, r, a in res if t == ti and r == "b" and a is not None])
        arrays[f"p{ti}"] = np.stack([a for t, r, a in res if t == ti and r == "p" and a is not None])
    wc = rd._search("esa-worldcover", bbox, date(2021, 1, 1), date(2021, 12, 31))
    arrays["worldcover"] = rd._fetch_npy("esa-worldcover", wc[0]["id"], bbox, w, h, ["map"])[0].astype(np.uint8)
    try:
        arrays["osm_buildings"] = _rasterize(_overpass_buildings(bbox), bbox, w, h)
    except Exception as exc:
        print(f"    OSM buildings failed: {exc}")
        arrays["osm_buildings"] = np.zeros((h, w), dtype=bool)

    if site["kind"] == "points":
        arrays["pos"] = _points_mask(site["positives"], bbox, w, h, 1)
        arrays["near_damage"] = _points_mask(site["any_damage"], bbox, w, h, int(NEG_CLEARANCE_M / rd.PIXEL_M))
        arrays["aoi"] = (_rasterize(_rings(site["aoi"]), bbox, w, h) if site["aoi"]
                         else np.ones((h, w), dtype=bool))
    else:
        rings = [r for f in site["polygons"] for r in _rings(f["geometry"])]
        dmg = _rasterize(rings, bbox, w, h)
        arrays["pos"] = dmg
        k = int(NEG_CLEARANCE_M / rd.PIXEL_M)
        arrays["near_damage"] = ndimage.binary_dilation(dmg, iterations=k)
        arrays["aoi"] = np.ones((h, w), dtype=bool)
    np.savez_compressed(path, **arrays, meta=json.dumps({
        "site": site["name"], "layer": site["layer"], "bbox": bbox, "tracks": [list(t) for t in tracks],
        "baseline": [b0.isoformat(), b1.isoformat()], "post": [p0.isoformat(), p1.isoformat()],
    }))
    n = {k: v.shape[0] for k, v in arrays.items() if k[0] in "bp" and k[1:].isdigit()}
    print(f"  {site['name']}: {len(tracks)} tracks {n}, {time.time() - t0:.0f}s, "
          f"positives {int(arrays['pos'].sum())} px, OSM building px {int(arrays['osm_buildings'].sum())}")


# ── Scoring ──

def load_site(path: Path) -> dict:
    z = np.load(path, allow_pickle=False)
    d = {k: z[k] for k in z.files if k != "meta"}
    d["meta"] = json.loads(str(z["meta"]))
    d["tracks"] = [(d[f"b{i}"].astype(np.float32), d[f"p{i}"].astype(np.float32))
                   for i in range(len(d["meta"]["tracks"]))]
    return d


def eval_masks(site: dict, mask: str = "osm"):
    """(positive pixels, negative pixels, mask) on building pixels."""
    if mask == "osm" and site["osm_buildings"].sum() > 500:
        bld = site["osm_buildings"]
    else:
        bld = site["worldcover"] == rd.BUILT_UP_CLASS
    pos = site["pos"] & bld
    neg = bld & site["aoi"] & ~site["near_damage"]
    return pos, neg, bld


def auc(pos_scores: np.ndarray, neg_scores: np.ndarray) -> float:
    if len(pos_scores) == 0 or len(neg_scores) == 0:
        return float("nan")
    from scipy.stats import rankdata
    ranks = rankdata(np.concatenate([pos_scores, neg_scores]))
    r_pos = ranks[: len(pos_scores)].sum()
    return float((r_pos - len(pos_scores) * (len(pos_scores) + 1) / 2) / (len(pos_scores) * len(neg_scores)))


def tpr_at_fpr(pos_scores, neg_scores, fpr):
    thr = np.quantile(neg_scores, 1 - fpr)
    return float((pos_scores > thr).mean()), float(thr)


def smooth(tmap: np.ndarray, radii_m) -> np.ndarray:
    layers = [tmap]
    for r in radii_m:
        size = 2 * max(1, round(r / rd.PIXEL_M)) + 1
        layers.append(ndimage.uniform_filter(tmap, size=size, mode="nearest"))
    return np.mean(layers, axis=0)


def score(site: dict, cfg: dict) -> np.ndarray:
    tracks = site["tracks"]
    if cfg.get("tracks") == 1:
        tracks = tracks[:1]
    elif cfg.get("tracks") == 2:
        tracks = tracks[:2]
    ref_mask = (site["worldcover"] == rd.BUILT_UP_CLASS) if cfg.get("median", True) else None
    per_track = []
    for base, post in tracks:
        nb, npost = cfg.get("n_base"), cfg.get("n_post")
        if nb:
            idx = np.linspace(0, len(base) - 1, min(nb, len(base))).round().astype(int)
            base = base[np.unique(idx)]
        if npost:
            post = post[-npost:]
        bands = []
        for b in (0, 1):
            stat = cfg.get("stat", "welch")
            if stat == "welch":
                t = rd.welch_t(base[:, b], post[:, b], ref_mask)
            elif stat == "zpre":  # change relative to pre-war variability
                with np.errstate(invalid="ignore", divide="ignore"):
                    d = np.nanmean(post[:, b], 0) - np.nanmean(base[:, b], 0)
                    if ref_mask is not None:
                        d = d - np.nanmedian(d[ref_mask])
                    t = d / np.nanstd(base[:, b], 0, ddof=1)
            else:  # "diff": plain mean difference in dB
                d = np.nanmean(post[:, b], 0) - np.nanmean(base[:, b], 0)
                if ref_mask is not None:
                    d = d - np.nanmedian(d[ref_mask])
                t = d
            bands.append(t if cfg.get("signed") else np.abs(t))
        pol = cfg.get("pol", "mean")
        with np.errstate(invalid="ignore"):
            v = {"mean": np.nanmean(bands, 0), "max": np.nanmax(bands, 0),
                 "vv": bands[0], "vh": bands[1]}[pol]
        per_track.append(v)
    with np.errstate(invalid="ignore"):
        tm = np.nanmax(per_track, 0) if cfg.get("track_combine", "max") == "max" else np.nanmean(per_track, 0)
    tm = np.nan_to_num(tm, nan=0.0)
    return smooth(tm, cfg.get("radii", rd.SMOOTH_RADII_M))


def evaluate(sites: list[dict], cfg: dict, mask="osm") -> dict:
    out = {}
    for s in sites:
        sm = score(s, cfg)
        pos, neg, _ = eval_masks(s, mask)
        p, n = sm[pos], sm[neg]
        t2, _ = tpr_at_fpr(p, n, 0.02) if len(p) and len(n) else (float("nan"), 0)
        out[s["meta"]["site"]] = {"auc": auc(p, n), "tpr@2%": t2, "pos": int(pos.sum()), "neg": int(neg.sum())}
    return out


def summary(res: dict, names) -> tuple[float, float]:
    a = [res[n]["auc"] for n in names if n in res and not math.isnan(res[n]["auc"])]
    t = [res[n]["tpr@2%"] for n in names if n in res and not math.isnan(res[n]["tpr@2%"])]
    return float(np.mean(a)) if a else float("nan"), float(np.mean(t)) if t else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sites", "fetch", "refill-osm", "run"])
    ap.add_argument("--data", required=True)
    args = ap.parse_args()
    data = Path(args.data)
    cache = data / "stacks"
    cache.mkdir(exist_ok=True)
    if args.cmd in ("sites", "fetch"):
        sites = define_sites(data)
        (data / "sites.json").write_text(json.dumps(sites), encoding="utf-8")
        for s in sites:
            print(f"{s['name']:16s} {s['layer']:45s} labels up to {s['label_date']}  bbox {[round(v, 3) for v in s['bbox']]}")
        if args.cmd == "fetch":
            for s in sites:
                try:
                    fetch_site(s, cache)
                except Exception as exc:
                    print(f"  {s['name']}: FAILED {exc}")
        return
    if args.cmd == "refill-osm":
        for path in sorted(cache.glob("*.npz")):
            z = dict(np.load(path, allow_pickle=False))
            if z["osm_buildings"].sum() > 0:
                continue
            meta = json.loads(str(z["meta"]))
            h, w = z["worldcover"].shape
            try:
                z["osm_buildings"] = _rasterize(_overpass_buildings(tuple(meta["bbox"])), tuple(meta["bbox"]), w, h)
                np.savez_compressed(path, **z)
                print(f"  {meta['site']}: OSM building px {int(z['osm_buildings'].sum())}")
            except Exception as exc:
                print(f"  {meta['site']}: still failing ({exc})")
        return
    print("run experiments from evaluation/experiments.py")


if __name__ == "__main__":
    main()
