"""Shelter discovery: OSM Overpass query + Django DB merge with deduplication."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]
USER_AGENT = "civilian-evacuation/0.1 (shelter lookup)"
TIMEOUT_SEC = 5
DEDUP_RADIUS_M = 30.0

_shelter_cache: dict[tuple, tuple[float, list[dict[str, Any]]]] = {}
SHELTER_CACHE_TTL = 3600

SHELTERS_DIR = Path("cache") / "shelters"
SHELTERS_DIR.mkdir(parents=True, exist_ok=True)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _extract_shelter_metadata(tags: dict[str, str]) -> tuple[str, str, str | None]:
    """Extract a human-friendly exact shelter name, type, and street from OSM tags."""
    stype = (
        tags.get("shelter_type")
        or tags.get("social_facility")
        or tags.get("emergency")
        or tags.get("building")
        or tags.get("amenity")
        or ""
    ).strip()

    type_labels = {
        "public_transport": "Transit Shelter",
        "weather_shelter": "Weather Shelter",
        "bomb_shelter": "Civil Defense Bunker",
        "bunker": "Civil Defense Bunker",
        "assembly_point": "Evacuation Assembly Point",
        "basic_hut": "Emergency Hut",
        "picnic_shelter": "Park Shelter",
        "community_centre": "Community Center Shelter",
        "school": "School Shelter",
        "place_of_worship": "Civic Shelter",
        "shelter": "Emergency Shelter",
    }
    type_display = type_labels.get(stype, stype.replace("_", " ").title() if stype else "Shelter")
    if type_display.lower() in ("yes", "no"):
        type_display = "Shelter"

    # 1. Check for explicit name
    for k in ("name", "name:en", "official_name", "alt_name", "loc_name", "int_name"):
        val = tags.get(k, "").strip()
        if val:
            return val, stype or "shelter", tags.get("addr:street")

    # 2. Check for address tags
    street = tags.get("addr:street", "").strip()
    housenumber = tags.get("addr:housenumber", "").strip()
    addr_str = f"{housenumber} {street}".strip() if street else ""

    operator = tags.get("operator", "").strip()
    ref = tags.get("ref", "").strip()
    desc = tags.get("description", "").strip()

    if operator and addr_str:
        return f"{operator} {type_display} ({addr_str})", stype or "shelter", street
    if operator:
        return f"{operator} {type_display}", stype or "shelter", street or None
    if addr_str:
        return f"{type_display} at {addr_str}", stype or "shelter", street
    if ref:
        return f"{type_display} #{ref}", stype or "shelter", street or None
    if desc and len(desc) <= 40:
        return f"{type_display} ({desc})", stype or "shelter", street or None

    return type_display, stype or "shelter", street or None


def _build_overpass_query(
    bbox: tuple[float, float, float, float],
    tags: list[str],
) -> str:
    """Build an Overpass QL query for shelter-related tags inside a bbox."""
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    bb = f"{south},{west},{north},{east}"
    parts = []
    for tag in tags:
        key, _, val = tag.partition("=")
        if val:
            parts.append(f'  node["{key}"="{val}"]({bb});')
            parts.append(f'  way["{key}"="{val}"]({bb});')
        else:
            parts.append(f'  node["{key}"]({bb});')
            parts.append(f'  way["{key}"]({bb});')
    query = "[out:json][timeout:10];\n(\n" + "\n".join(parts) + "\n);\nout center;\n"
    return query


def query_osm_shelters(
    bbox: tuple[float, float, float, float],
    tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Query Overpass API or core OSM for shelters within the bounding box."""
    if tags is None:
        from django.conf import settings
        tags = getattr(settings, "SHELTER_OSM_TAGS", [
            "emergency=assembly_point",
            "social_facility=shelter",
            "building=bunker",
            "amenity=shelter",
        ])

    query = _build_overpass_query(bbox, tags)
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")

    payload: dict[str, Any] = {}
    for url in OVERPASS_URLS:
        req = urllib.request.Request(
            url,
            data=data,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                break
        except Exception:
            continue

    if not payload:
        # Fallback to OpenStreetMap core API (0.6 map endpoint)
        try:
            import xml.etree.ElementTree as ET
            west, south, east, north = bbox
            core_url = f"https://api.openstreetmap.org/api/0.6/map?bbox={west:.6f},{south:.6f},{east:.6f},{north:.6f}"
            core_req = urllib.request.Request(core_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(core_req, timeout=TIMEOUT_SEC) as resp:
                root = ET.fromstring(resp.read())
            results: list[dict[str, Any]] = []
            for node in root.findall("node"):
                tags_dict = {t.get("k"): t.get("v") for t in node.findall("tag")}
                lat_str, lon_str = node.get("lat"), node.get("lon")
                if not lat_str or not lon_str:
                    continue
                has_shelter_tag = any(
                    tags_dict.get(k) in ("shelter", "assembly_point", "bunker")
                    for k in ("emergency", "social_facility", "building", "amenity")
                ) or "shelter_type" in tags_dict
                if has_shelter_tag:
                    name, stype, street = _extract_shelter_metadata(tags_dict)
                    s_entry: dict[str, Any] = {
                        "name": name,
                        "lat": float(lat_str),
                        "lon": float(lon_str),
                        "type": stype,
                        "source": "osm",
                        "verified": False,
                    }
                    if street:
                        s_entry["street"] = street
                    results.append(s_entry)
            if results:
                return results
        except Exception:
            pass
        return []

    results: list[dict[str, Any]] = []
    for element in payload.get("elements", []):
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        etags = element.get("tags", {})
        name, stype, street = _extract_shelter_metadata(etags)

        s_entry = {
            "name": name,
            "lat": float(lat),
            "lon": float(lon),
            "type": stype,
            "source": "osm",
            "verified": False,
        }
        if street:
            s_entry["street"] = street
        results.append(s_entry)

    return results


def get_db_shelters(bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Get shelters from the Django database within the bounding box."""
    from finder.models import Shelter

    west, south, east, north = bbox
    qs = Shelter.objects.filter(
        latitude__gte=south,
        latitude__lte=north,
        longitude__gte=west,
        longitude__lte=east,
    )
    return [
        {
            "name": s.name,
            "lat": s.latitude,
            "lon": s.longitude,
            "type": s.shelter_type,
            "source": s.source,
            "verified": s.verified,
        }
        for s in qs
    ]


def merge_shelters(
    osm_shelters: list[dict[str, Any]],
    db_shelters: list[dict[str, Any]],
    dedup_radius_m: float = DEDUP_RADIUS_M,
) -> list[dict[str, Any]]:
    """Merge OSM and DB shelters, deduplicating within radius."""
    merged = list(db_shelters)

    for osm in osm_shelters:
        is_dup = False
        for existing in merged:
            dist = haversine_m(osm["lat"], osm["lon"], existing["lat"], existing["lon"])
            if dist <= dedup_radius_m:
                is_dup = True
                break
        if not is_dup:
            merged.append(osm)

    return merged


def _query_shelters_for_bbox(bb: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Internal helper to retrieve cached or live shelters for a bbox."""
    key = tuple(round(v, 2) for v in bb)
    now = time.time()
    cached = _shelter_cache.get(key)
    if cached and (now - cached[0]) < SHELTER_CACHE_TTL:
        return cached[1]

    disk_file = SHELTERS_DIR / f"shelters_{key[0]}_{key[1]}_{key[2]}_{key[3]}.json"
    if disk_file.exists():
        try:
            data = json.loads(disk_file.read_text(encoding="utf-8"))
            _shelter_cache[key] = (now, data)
            return data
        except Exception:
            pass

    osm = query_osm_shelters(bb)
    db = get_db_shelters(bb)
    merged = merge_shelters(osm, db)

    _shelter_cache[key] = (now, merged)
    try:
        disk_file.write_text(json.dumps(merged), encoding="utf-8")
    except Exception:
        pass

    return merged


def find_shelters(
    bbox: tuple[float, float, float, float],
    auto_expand_km: float = 0.0,
) -> tuple[list[dict[str, Any]], bool]:
    """Find all shelters in bbox: OSM + DB merged and deduped, with cache.

    If no shelters found and auto_expand_km > 0, expands bbox by auto_expand_km
    and tries again. Returns (shelters_list, was_expanded_bool).
    """
    shelters = _query_shelters_for_bbox(bbox)
    if shelters or auto_expand_km <= 0:
        return shelters, False

    # Auto-expand bbox by auto_expand_km
    west, south, east, north = bbox
    mid_lat = (south + north) / 2
    pad_lat = auto_expand_km / 111.32
    pad_lon = auto_expand_km / (111.32 * max(math.cos(math.radians(mid_lat)), 0.01))
    expanded_bbox = (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)

    expanded_shelters = _query_shelters_for_bbox(expanded_bbox)
    return expanded_shelters, bool(expanded_shelters)
