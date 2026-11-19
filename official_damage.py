"""Official damage assessments: Copernicus EMS (live API) and imported files (e.g. UNOSAT).

Graded damage is normalised to three levels: "destroyed", "damaged" and
"possibly_damaged". Each feature is one of three kinds:

* "road"     - a graded road segment (Copernicus transportation layer). Roads
               drive routing: destroyed roads are blocked, damaged roads cost
               much more, possibly damaged roads cost a little more.
* "building" - a graded building footprint or point.
* "area"     - a graded assessment area (e.g. a UNOSAT 500 m grid cell).
               Buildings and areas never change routes; they are shown on the
               map and listed as warnings on the route cards.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import shapely
import shapely.wkt
from shapely.geometry import LineString, box, mapping, shape
from shapely.ops import transform

logger = logging.getLogger("finder.damage")

DAMAGE_GRADES = ("destroyed", "damaged", "possibly_damaged")
GRADE_LABELS = {
    "destroyed": "Destroyed",
    "damaged": "Damaged",
    "possibly_damaged": "Possibly damaged",
}

# UNOSAT "Main_Damage_Site_Class" codes. 14 ("damage visible in cell") and 6
# ("no visible damage") were confirmed against the published North Kharkiv
# RDA counts; 1-3 are UNOSAT's standard destroyed/severe/moderate classes.
# Other codes are reported as unknown and skipped rather than guessed.
UNOSAT_CODES = {
    1: "destroyed",
    2: "damaged",
    3: "damaged",
    6: "none",
    14: "damaged",
}

ROAD_MATCH_M = 10.0     # OSM edge within this distance of a graded road matches it
SITE_NEAR_M = 25.0      # building this close to a route is reported on its card
AREA_MIN_M2 = 10_000.0  # polygons larger than this are "areas", not buildings

COPERNICUS_API = "https://rapidmapping.emergency.copernicus.eu/backend/dashboard-api"
COPERNICUS_ATTRIBUTION = "© European Union, Copernicus Emergency Management Service"
COPERNICUS_SEARCH_KM = 500.0
COPERNICUS_LIST_TTL = 6 * 3600
COPERNICUS_DETAIL_TTL = 6 * 3600
HTTP_TIMEOUT = 15
USER_AGENT = "civilian-evacuation/1.0 (damage lookup)"

CACHE_DIR = Path("cache") / "copernicus"


# ── Grades ──

def normalize_grade(value: Any) -> str | None:
    """Map a source damage value to a canonical grade.

    Returns one of DAMAGE_GRADES, "none" for analysed-but-undamaged, or None
    when the value means "not analysed" or is not recognised.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and math.isnan(value):
            return None
        return UNOSAT_CODES.get(int(value))
    text = str(value).strip().lower()
    if not text:
        return None
    if text.isdigit():
        return UNOSAT_CODES.get(int(text))
    if "not analy" in text or text in ("unknown", "n/a", "na", "not applicable"):
        return None
    if "no visible" in text or "no damage" in text or "negligible" in text or text == "none":
        return "none"
    if "destroy" in text:
        return "destroyed"
    if "possib" in text:
        return "possibly_damaged"
    if any(k in text for k in ("sever", "high", "major", "moderate", "damag")):
        return "damaged"
    return None


# ── Projection helpers (metres around a centre point) ──

def _projectors(lat: float, lon: float):
    from pyproj import Transformer

    local = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +units=m +datum=WGS84"
    to_m = Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
    to_deg = Transformer.from_crs(local, "EPSG:4326", always_xy=True).transform
    return to_m, to_deg


def _classify_kind(geom, to_m) -> str:
    gtype = geom.geom_type
    if "LineString" in gtype:
        return "road"
    if "Point" in gtype:
        return "building"
    return "area" if transform(to_m, geom).area > AREA_MIN_M2 else "building"


def _is_railway(props: dict) -> bool:
    return "rail" in str(props.get("obj_type", "")).lower() or "subway" in str(props.get("info", "")).lower()


def _label(props: dict) -> str:
    for key in ("name", "simplified", "info", "notation", "Neighborho", "Settlement"):
        val = props.get(key)
        if val and str(val).strip() and str(val).strip().lower() not in ("unknown", "not applicable", "none"):
            return str(val).strip()[:200]
    return ""


# ── File import (UNOSAT and other datasets) ──

TEXT_GRADE_COLUMNS = ("damage_gra", "damage_grade", "damage", "damage_status", "dmg_class", "grade", "damage_cl")


def _grade_getter(gdf):
    """Return (description, row -> grade) for the dataset's damage column(s)."""
    cols = [c for c in gdf.columns if c != "geometry"]
    lower = {c.lower(): c for c in cols}

    # 1. Known text columns (Copernicus "damage_gra" etc.)
    for name in TEXT_GRADE_COLUMNS:
        if name in lower:
            col = lower[name]
            return col, lambda row, col=col: normalize_grade(row[col])

    # 2. UNOSAT numeric class columns (possibly several assessment dates):
    #    take the most severe recognised class.
    unosat_cols = [c for c in cols if c.lower().startswith("main_dam")]
    if unosat_cols:
        severity = {"destroyed": 3, "damaged": 2, "possibly_damaged": 1, "none": 0}

        def from_codes(row):
            best = None
            for c in unosat_cols:
                g = normalize_grade(row[c])
                if g is not None and (best is None or severity[g] > severity[best]):
                    best = g
            return best

        return ", ".join(unosat_cols), from_codes

    # 3. Any text column whose values look like damage grades
    for col in cols:
        values = gdf[col].dropna().astype(str).head(200)
        if len(values) and sum(normalize_grade(v) is not None for v in values) / len(values) > 0.5:
            return col, lambda row, col=col: normalize_grade(row[col])

    raise ValueError(
        "Could not find a damage column. Expected Copernicus 'damage_gra', UNOSAT "
        "'Main_Damage_Site_Class' (Main_Dam*), or a column with values like "
        "'Destroyed' / 'Damaged'."
    )


def read_damage_file(path: str | Path):
    """Read a GeoJSON, GeoPackage, KML, File Geodatabase or zipped shapefile in WGS84."""
    import geopandas as gpd

    path = Path(path)
    src = str(path)
    if path.suffix.lower() == ".zip":
        import zipfile

        names = zipfile.ZipFile(path).namelist()
        # Shapefiles are often inside a sub-folder of the zip; point GDAL at the .shp.
        # A zipped File Geodatabase is a "<name>.gdb/" folder.
        shp = next((n for n in names if n.lower().endswith(".shp")), None)
        gdb = next((n.split(".gdb/")[0] + ".gdb" for n in names if ".gdb/" in n.lower()), None)
        inner = shp or gdb
        src = f"zip://{path}!{inner}" if inner else f"zip://{path}"
    gdf = gpd.read_file(src)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    return gdf.to_crs(4326)


def import_damage_file(path: str | Path, dataset, default_grade: str | None = None) -> dict[str, Any]:
    """Parse a damage file into DamageFeature rows for `dataset` (a saved DamageDataset).

    `default_grade` ("destroyed", "damaged" or "possibly_damaged") is used when
    the file has no damage column at all, e.g. UNOSAT's El Fasher assessment,
    where every polygon is a destroyed structure. Replaces any features the
    dataset already had. Returns an import summary.
    """
    from finder.models import DamageFeature

    gdf = read_damage_file(path)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if gdf.empty:
        raise ValueError("The file contains no geometries.")

    try:
        grade_desc, grade_of = _grade_getter(gdf)
    except ValueError:
        if default_grade not in DAMAGE_GRADES:
            raise
        grade_desc, grade_of = f"(none; every feature graded '{default_grade}')", lambda row: default_grade
    west, south, east, north = gdf.total_bounds
    to_m, _ = _projectors((south + north) / 2, (west + east) / 2)

    counts: dict[str, int] = {}
    unknown_rows = 0
    analysed_bounds: list[tuple[float, float, float, float]] = []
    rows: list[DamageFeature] = []

    for _, row in gdf.iterrows():
        grade = grade_of(row)
        if grade is None:
            unknown_rows += 1
            continue
        geom = row.geometry
        analysed_bounds.append(geom.bounds)
        if grade == "none":
            continue
        props = row.drop(labels="geometry").to_dict()
        kind = _classify_kind(geom, to_m)
        if kind == "road" and _is_railway(props):
            continue
        w, s, e, n = geom.bounds
        rows.append(DamageFeature(
            dataset=dataset, kind=kind, grade=grade,
            geometry=json.dumps(mapping(geom)), label=_label(props),
            west=w, south=s, east=e, north=n,
        ))
        counts[f"{grade} {kind}"] = counts.get(f"{grade} {kind}", 0) + 1

    if not analysed_bounds:
        raise ValueError(f"No rows had a recognised damage grade in column(s): {grade_desc}.")

    dataset.features.all().delete()
    DamageFeature.objects.bulk_create(rows, batch_size=500)
    dataset.west = min(b[0] for b in analysed_bounds)
    dataset.south = min(b[1] for b in analysed_bounds)
    dataset.east = max(b[2] for b in analysed_bounds)
    dataset.north = max(b[3] for b in analysed_bounds)
    dataset.feature_count = len(rows)
    dataset.save()

    return {
        "grade_column": grade_desc,
        "rows": int(len(gdf)),
        "analysed": len(analysed_bounds),
        "not_analysed_or_unknown": unknown_rows,
        "damaged_features": len(rows),
        "counts": counts,
    }


def import_uploaded_file(uploaded, dataset, default_grade: str | None = None) -> dict[str, Any]:
    """Import a Django UploadedFile (keeps its extension so the format is detected)."""
    suffix = Path(uploaded.name).suffix or ".geojson"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp) / f"upload{suffix}"
        with open(tmp_path, "wb") as fh:
            for chunk in uploaded.chunks():
                fh.write(chunk)
        return import_damage_file(tmp_path, dataset, default_grade)


def _db_damage(bbox: tuple[float, float, float, float]) -> tuple[list[dict], list[dict]]:
    """Imported features and datasets overlapping the bbox."""
    from finder.models import DamageDataset, DamageFeature

    west, south, east, north = bbox
    datasets = DamageDataset.objects.filter(
        active=True, west__lte=east, east__gte=west, south__lte=north, north__gte=south
    )
    sources = [{
        "name": d.name,
        "provider": d.get_source_display(),
        "date": d.imagery_date.isoformat() if d.imagery_date else "",
        "attribution": d.attribution or d.get_source_display(),
        "url": d.source_url,
    } for d in datasets]
    feats = []
    qs = DamageFeature.objects.filter(
        dataset__in=datasets, west__lte=east, east__gte=west, south__lte=north, north__gte=south
    ).select_related("dataset")
    for f in qs:
        feats.append({
            "kind": f.kind, "grade": f.grade, "geom": shape(json.loads(f.geometry)),
            "label": f.label, "source": f.dataset.name,
            "date": f.dataset.imagery_date.isoformat() if f.dataset.imagery_date else "",
        })
    return feats, sources


# ── Copernicus EMS (live) ──

def _http_json(url: str, timeout: float = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cached_json(url: str, name: str, ttl: float | None) -> Any:
    """Fetch JSON with a disk cache. ttl=None caches forever (versioned files)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and (ttl is None or time.time() - path.stat().st_mtime < ttl):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = _http_json(url)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    return data


def _km_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _latest_grading_product(aoi: dict) -> dict | None:
    gra = [
        p for p in aoi.get("products", [])
        if p.get("type") == "GRA" and p.get("feasible", True)
        and any(l.get("json") for l in (p.get("layers") or []))
    ]
    if not gra:
        return None
    return max(gra, key=lambda p: str((p.get("version") or {}).get("deliveryTime") or ""))


def _copernicus_damage(bbox: tuple[float, float, float, float]) -> tuple[list[dict], list[dict], bool]:
    """Graded damage from Copernicus EMS grading products covering the bbox."""
    zone = box(*bbox)
    clat, clon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
    to_m, _ = _projectors(clat, clon)

    listing = _cached_json(f"{COPERNICUS_API}/public-activations-info/?limit=1000",
                           "activations.json", COPERNICUS_LIST_TTL)
    feats: list[dict] = []
    sources: list[dict] = []
    covered = False

    for act in listing.get("results", []):
        try:
            c = shapely.wkt.loads(act["centroid"])
        except Exception:
            continue
        if _km_between(clat, clon, c.y, c.x) > COPERNICUS_SEARCH_KM:
            continue
        code = act["code"]
        detail = _cached_json(f"{COPERNICUS_API}/public-activations/?code={code}",
                              f"{code}.json", COPERNICUS_DETAIL_TTL)
        if isinstance(detail, dict) and "results" in detail:
            detail = detail["results"][0] if detail["results"] else {}
        if detail.get("sensitive"):
            continue

        for aoi in detail.get("aois", []):
            try:
                if not shapely.wkt.loads(aoi["extent"]).intersects(zone):
                    continue
            except Exception:
                continue
            product = _latest_grading_product(aoi)
            if product is None:
                continue
            covered = True
            date = str(product.get("imageDate") or detail.get("eventTime") or "")[:10]
            sources.append({
                "name": f"Copernicus EMS {code}: {detail.get('name', '')} ({aoi.get('name', '')})",
                "provider": "Copernicus EMS",
                "date": date,
                "attribution": COPERNICUS_ATTRIBUTION,
                "url": f"https://rapidmapping.emergency.copernicus.eu/{code}/",
            })
            for layer in product.get("layers") or []:
                lname = str(layer.get("name", "")).lower()
                url = layer.get("json")
                if not url or not ("builtup" in lname or "transportation" in lname):
                    continue
                cache_name = "layer_" + hashlib.sha1(url.encode()).hexdigest() + ".json"
                gj = _cached_json(url, cache_name, None)
                for f in gj.get("features", []):
                    props = f.get("properties") or {}
                    grade = normalize_grade(props.get("damage_gra"))
                    if grade not in DAMAGE_GRADES or not f.get("geometry"):
                        continue
                    geom = shape(f["geometry"])
                    if not geom.intersects(zone):
                        continue
                    if "transportation" in lname:
                        if _is_railway(props):
                            continue
                        kind = "road"
                    else:
                        kind = _classify_kind(geom, to_m)
                    feats.append({
                        "kind": kind, "grade": grade, "geom": geom, "label": _label(props),
                        "source": f"Copernicus EMS {code}", "date": date,
                    })
    return feats, sources, covered


# ── Public entry points ──

def official_damage_for_bbox(bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """All official damage for a bbox (west, south, east, north).

    Returns {"features", "sources", "assessed", "notes"}. "assessed" is True
    when at least one official assessment covers the area, even if it found
    no damage there.
    """
    notes: list[str] = []
    try:
        features, sources = _db_damage(bbox)
    except Exception as exc:  # e.g. migrations not applied yet; never break routing
        logger.error("Imported damage datasets unavailable: %s", exc)
        features, sources = [], []
        notes.append("Imported damage datasets could not be read (has `manage.py migrate` been run?).")
    assessed = bool(sources)
    try:
        c_feats, c_sources, c_covered = _copernicus_damage(bbox)
        features += c_feats
        sources += c_sources
        assessed = assessed or c_covered
    except Exception as exc:
        logger.warning("Copernicus EMS lookup failed: %s", exc)
        notes.append("Copernicus EMS could not be reached; its damage data may be missing.")
    return {"features": features, "sources": sources, "assessed": assessed, "notes": notes}


def damage_geojson(features: list[dict]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": mapping(f["geom"]),
            "properties": {
                "kind": f["kind"], "grade": f["grade"], "grade_label": GRADE_LABELS[f["grade"]],
                "label": f.get("label", ""), "source": f.get("source", ""), "date": f.get("date", ""),
                "estimated": bool(f.get("estimated")), "layer": f.get("layer", ""),
            },
        } for f in features],
    }


def road_grade_polygons(
    features: list[dict], bbox: tuple[float, float, float, float]
) -> dict[str, list]:
    """Buffer graded roads by ROAD_MATCH_M into polygons (WGS84) per grade, for routing.

    Only official road grades reach routing; estimated features never do.
    """
    roads = [f for f in features if f["kind"] == "road" and not f.get("estimated")]
    if not roads:
        return {}
    to_m, to_deg = _projectors((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2)
    out: dict[str, list] = {}
    for f in roads:
        poly = transform(to_deg, transform(to_m, f["geom"]).buffer(ROAD_MATCH_M))
        out.setdefault(f["grade"], []).append(poly)
    return out


def annotate_routes(result: dict[str, Any], features: list[dict]) -> None:
    """Add nearby damaged buildings/areas to each route (warnings only, no rerouting)."""
    estimates = [f for f in features if f.get("estimated")]
    sites = [f for f in features if f["kind"] in ("building", "area") and not f.get("estimated")]
    for route in result.get("routes", []):
        _annotate_official(route, sites)
        _annotate_estimates(route, estimates)


ESTIMATE_PHRASES = {
    "war_start": "estimated damage from the war's first months",
    "recent": "estimated new change in the last 12 months",
}


def _annotate_estimates(route: dict, estimates: list[dict]) -> None:
    coords = route.get("geojson", {}).get("geometry", {}).get("coordinates") or []
    if not estimates or len(coords) < 2:
        return
    line = LineString(coords)
    to_m, _ = _projectors(line.centroid.y, line.centroid.x)
    corridor = transform(to_m, line).buffer(SITE_NEAR_M)
    for layer, phrase in ESTIMATE_PHRASES.items():
        n = sum(1 for f in estimates
                if f.get("layer") == layer and transform(to_m, f["geom"]).intersects(corridor))
        if n:
            route["damage_nearby"][f"estimated_{layer}"] = n
            noun = "area" if n == 1 else "areas"
            route["damage_warnings"].append(f"Passes {n} {noun} of {phrase} (radar, low confidence)")


def _annotate_official(route: dict, sites: list[dict]) -> None:
    route["damage_nearby"] = {}
    route["damage_warnings"] = []
    coords = route.get("geojson", {}).get("geometry", {}).get("coordinates") or []
    if not sites or len(coords) < 2:
        return
    line = LineString(coords)
    to_m, _ = _projectors(line.centroid.y, line.centroid.x)
    line_m = transform(to_m, line)
    near_buffer = line_m.buffer(SITE_NEAR_M)
    counts: dict[tuple[str, str], int] = {}
    for s in sites:
        geom_m = transform(to_m, s["geom"])
        hit = geom_m.intersects(line_m) if s["kind"] == "area" else geom_m.intersects(near_buffer)
        if hit:
            counts[(s["grade"], s["kind"])] = counts.get((s["grade"], s["kind"]), 0) + 1
    for grade in DAMAGE_GRADES:
        for kind in ("building", "area"):
            n = counts.get((grade, kind), 0)
            if not n:
                continue
            route["damage_nearby"][f"{grade}_{kind}"] = n
            level = GRADE_LABELS[grade].lower()
            if kind == "building":
                noun = "building" if n == 1 else "buildings"
                route["damage_warnings"].append(f"Passes {n} {level} {noun}")
            else:
                noun = "area" if n == 1 else "areas"
                route["damage_warnings"].append(f"Crosses {n} {noun} with {level} buildings")
