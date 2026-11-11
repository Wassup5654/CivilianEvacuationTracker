"""Evacuation routing engine: cost model, dead-end pruning, diverse k-paths.

Replaces the old single-exit Dijkstra in routing.py for the new interactive map.
The old routing.py is kept intact for backward compatibility.
"""

from __future__ import annotations

import logging
import math
import pickle
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import networkx as nx
from shapely.geometry import LineString, Point, mapping
from shapely.strtree import STRtree

from django.conf import settings

logger = logging.getLogger("finder.routing")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ── Settings with safe defaults ──

ALPHA = getattr(settings, "ROUTE_DAMAGE_ALPHA", 5.0)
BLOCK_THRESHOLD = getattr(settings, "ROUTE_BLOCK_THRESHOLD", 0.5)
OVERLAP_MAX = getattr(settings, "ROUTE_OVERLAP_MAX", 0.70)
WALK_SPEED = getattr(settings, "ROUTE_WALK_SPEED_KMH", 5.0)
DRIVE_SPEED = getattr(settings, "ROUTE_DRIVE_SPEED_KMH", 40.0)
MAX_SHELTERS = getattr(settings, "ROUTE_MAX_SHELTERS", 10)
AUTO_EXPAND_KM = getattr(settings, "ROUTE_AUTO_EXPAND_KM", 2.0)
GRAPH_CACHE_TTL = getattr(settings, "GRAPH_CACHE_TTL_SEC", 86400)
GRAPH_PADDING_M = getattr(settings, "GRAPH_PADDING_M", 500)
MAX_BBOX_SPAN_M = getattr(settings, "MAX_BBOX_SPAN_M", 5000.0)  # 5 km max side

DISCLAIMER = "Damage is estimated from satellite imagery and may be wrong. Follow official guidance."

# ── Graph cache (in-memory + disk) ──
_graph_cache: dict[tuple, tuple[float, Any]] = {}
CACHE_DIR = Path("cache")
CACHE_DIR.mkdir(exist_ok=True)
GRAPHS_DIR = CACHE_DIR / "graphs"
GRAPHS_DIR.mkdir(exist_ok=True)

# Reliable Overpass API endpoints
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api",
    "https://lz4.overpass-api.de/api",
    "https://z.overpass-api.de/api",
]


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) points in meters."""
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c


def _round_bbox(bbox: list[float] | tuple[float, ...], decimals: int = 2) -> tuple:
    return tuple(round(v, decimals) for v in bbox)


def _clamp_bbox_size(
    bbox: tuple[float, float, float, float],
    max_span_m: float = MAX_BBOX_SPAN_M,
) -> tuple[float, float, float, float]:
    """Ensure bbox dimensions do not exceed max_span_m (e.g. 5 km)."""
    west, south, east, north = bbox
    center_lat = (south + north) / 2.0
    center_lon = (west + east) / 2.0

    lat_span_m = (north - south) * 111320.0
    cos_lat = max(math.cos(math.radians(center_lat)), 0.01)
    lon_span_m = (east - west) * 111320.0 * cos_lat

    clamped_s, clamped_n = south, north
    clamped_w, clamped_e = west, east

    if lat_span_m > max_span_m:
        half_deg = (max_span_m / 2.0) / 111320.0
        clamped_s = center_lat - half_deg
        clamped_n = center_lat + half_deg

    if lon_span_m > max_span_m:
        half_deg = (max_span_m / 2.0) / (111320.0 * cos_lat)
        clamped_w = center_lon - half_deg
        clamped_e = center_lon + half_deg

    # Enforce valid lat/lon ranges
    clamped_s = max(-89.9, min(89.9, clamped_s))
    clamped_n = max(-89.9, min(89.9, clamped_n))
    clamped_w = max(-179.9, min(179.9, clamped_w))
    clamped_e = max(-179.9, min(179.9, clamped_e))

    if clamped_s >= clamped_n:
        clamped_s, clamped_n = center_lat - 0.005, center_lat + 0.005
    if clamped_w >= clamped_e:
        clamped_w, clamped_e = center_lon - 0.005, center_lon + 0.005

    return (clamped_w, clamped_s, clamped_e, clamped_n)


def _fetch_url_with_backoff(
    url: str,
    headers: dict[str, str],
    timeout: float = 12.0,
    max_retries: int = 2,
    bbox_info: tuple[float, float, float, float] | None = None,
) -> tuple[int, bytes, str]:
    """Fetch URL with short delay & backoff for 429/503/504 only. Never retries 400.

    Logs URL, bbox, HTTP status, and first 500 characters of response body.
    Returns (status_code, response_bytes, body_first_500).
    """
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                elapsed = time.time() - t0
                data = resp.read()
                body_500 = data[:500].decode("utf-8", errors="replace")
                logger.info(
                    f"[OSM Core API Attempt {attempt+1}] URL: {url} | Bbox: {bbox_info} | "
                    f"HTTP {resp.status} in {elapsed:.2f}s | Body preview: {body_500[:120]!r}"
                )
                return resp.status, data, body_500
        except urllib.error.HTTPError as e:
            elapsed = time.time() - t0
            raw_body = e.read()
            body_500 = raw_body.decode("utf-8", errors="replace")[:500]
            logger.warning(
                f"[OSM Core API Attempt {attempt+1}] URL: {url} | Bbox: {bbox_info} | "
                f"HTTP {e.code} ({e.reason}) in {elapsed:.2f}s | Body (first 500 chars): {body_500!r}"
            )
            # Do NOT retry 400!
            if e.code == 400:
                return 400, b"", body_500

            # Retry only on 429, 503, 504
            if e.code in (429, 503, 504) and attempt < max_retries:
                backoff_sec = 1.0 * (2 ** attempt)
                logger.info(f"Transient error HTTP {e.code}. Backing off {backoff_sec:.1f}s before retry...")
                time.sleep(backoff_sec)
                continue

            return e.code, b"", body_500
        except Exception as exc:
            elapsed = time.time() - t0
            logger.warning(
                f"[OSM Core API Attempt {attempt+1}] URL: {url} | Bbox: {bbox_info} | "
                f"Exception {type(exc).__name__}: {exc} in {elapsed:.2f}s"
            )
            if attempt < max_retries:
                time.sleep(1.0)
                continue
            return 0, b"", str(exc)[:500]

    return 0, b"", "Max retries exceeded"


def _download_core_osm_tiles(
    bbox: tuple[float, float, float, float],
    depth: int = 0,
    max_depth: int = 2,
) -> list[bytes]:
    """Download OSM XML for bbox.

    If 400 'too many nodes' or large area, recursively splits into 2x2 tiles.
    """
    west, south, east, north = bbox
    lon_span = east - west
    lat_span = north - south

    # Pre-emptively split into 2x2 if tile span > 0.02 deg (~2.2 km) to avoid hitting 50,000 node limit
    if (lon_span > 0.02 or lat_span > 0.02) and depth < max_depth:
        logger.info(
            f"Bbox span ({lon_span:.4f} x {lat_span:.4f} deg) exceeds safe tile threshold; "
            f"splitting into 2x2 sub-tiles (depth {depth+1})..."
        )
        mid_w = (west + east) / 2.0
        mid_s = (south + north) / 2.0
        sub_results: list[bytes] = []
        for sub_bb in [
            (west, south, mid_w, mid_s),
            (mid_w, south, east, mid_s),
            (west, mid_s, mid_w, north),
            (mid_w, mid_s, east, north),
        ]:
            sub_results.extend(_download_core_osm_tiles(sub_bb, depth=depth + 1, max_depth=max_depth))
        return sub_results

    url = f"https://api.openstreetmap.org/api/0.6/map?bbox={west:.6f},{south:.6f},{east:.6f},{north:.6f}"
    headers = {"User-Agent": "civilian-evacuation/1.0 (emergency planning)"}

    status, data, body_500 = _fetch_url_with_backoff(url, headers=headers, timeout=12.0, bbox_info=bbox)

    if status == 200 and len(data) > 0:
        return [data]

    # If 400 indicates node limit exceeded, split into 2x2 sub-tiles
    if status == 400 and depth < max_depth:
        lowered = body_500.lower()
        if "too many nodes" in lowered or "50000" in lowered or "limit" in lowered or "area" in lowered:
            logger.info(
                f"Core OSM API returned 400 (node limit) for bbox {bbox}. "
                f"Splitting into 2x2 sub-tiles (depth {depth+1})..."
            )
            mid_w = (west + east) / 2.0
            mid_s = (south + north) / 2.0
            sub_results = []
            for sub_bb in [
                (west, south, mid_w, mid_s),
                (mid_w, south, east, mid_s),
                (west, mid_s, mid_w, north),
                (mid_w, mid_s, east, north),
            ]:
                sub_results.extend(_download_core_osm_tiles(sub_bb, depth=depth + 1, max_depth=max_depth))
            return sub_results

    raise RuntimeError(f"Core OSM API HTTP {status}: {body_500}")


def _load_graph_from_core_osm_api(
    padded: tuple[float, float, float, float],
    mode: str = "walk",
) -> nx.MultiDiGraph:
    """Download road network via OpenStreetMap Core API (with auto-tiling) and load via OSMnx.

    Used when Overpass API mirrors are slow, rate-limited, or unavailable.
    """
    import tempfile
    import osmnx as ox

    xml_chunks = _download_core_osm_tiles(padded)
    if not xml_chunks:
        raise RuntimeError("No data received from Core OSM API.")

    bidirectional = (mode == "walk")
    subgraphs: list[nx.MultiDiGraph] = []

    for chunk in xml_chunks:
        with tempfile.NamedTemporaryFile(suffix=".osm", delete=False) as tf:
            tf.write(chunk)
            tf_path = Path(tf.name)
        try:
            g = ox.graph_from_xml(tf_path, bidirectional=bidirectional)
            if len(g.nodes) > 0:
                subgraphs.append(g)
        finally:
            try:
                tf_path.unlink()
            except Exception:
                pass

    if not subgraphs:
        raise RuntimeError("OSM XML contained no graph nodes.")

    if len(subgraphs) == 1:
        G = subgraphs[0]
    else:
        G = nx.compose_all(subgraphs)

    G = ox.distance.add_edge_lengths(G)
    return G


def get_cached_graph(
    bbox: tuple[float, float, float, float],
    mode: str = "walk",
) -> nx.MultiDiGraph:
    """Download road network via OSMnx/OSM, with disk & in-memory caching and padding."""
    import osmnx as ox

    west, south, east, north = bbox
    # Add padding and clamp to safe maximum
    pad_lat = GRAPH_PADDING_M / 111320
    center_lat = (south + north) / 2
    pad_lon = GRAPH_PADDING_M / (111320 * max(math.cos(math.radians(center_lat)), 0.01))

    raw_padded = (west - pad_lon, south - pad_lat, east + pad_lon, north + pad_lat)
    padded = _clamp_bbox_size(raw_padded, max_span_m=MAX_BBOX_SPAN_M)

    cache_key = (_round_bbox(padded, decimals=2), mode)
    now = time.time()

    # 1. In-memory cache
    cached = _graph_cache.get(cache_key)
    if cached and (now - cached[0]) < GRAPH_CACHE_TTL:
        return cached[1]

    # Check if any in-memory graph encloses this bbox
    for k, (t_cached, g) in _graph_cache.items():
        if k[1] == mode and (now - t_cached) < GRAPH_CACHE_TTL:
            cw, cs, ce, cn = k[0]
            if cw <= padded[0] and cs <= padded[1] and ce >= padded[2] and cn >= padded[3]:
                return g

    # 2. Disk cache
    disk_name = f"graph_{mode}_{cache_key[0][0]}_{cache_key[0][1]}_{cache_key[0][2]}_{cache_key[0][3]}.pkl"
    disk_path = GRAPHS_DIR / disk_name
    if disk_path.exists():
        try:
            G = pickle.loads(disk_path.read_bytes())
            _graph_cache[cache_key] = (now, G)
            return G
        except Exception:
            pass

    # Also search disk for any graph enclosing this bbox
    for pkl in GRAPHS_DIR.glob(f"graph_{mode}_*.pkl"):
        try:
            parts = pkl.stem.split("_")
            if len(parts) >= 6:
                dw, ds, de, dn = float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5])
                if dw <= padded[0] and ds <= padded[1] and de >= padded[2] and dn >= padded[3]:
                    G = pickle.loads(pkl.read_bytes())
                    _graph_cache[cache_key] = (now, G)
                    return G
        except Exception:
            continue

    # 3. Download from OSMnx with fallback mirrors
    network_type = "walk" if mode == "walk" else "drive"
    ox.settings.overpass_rate_limit = False
    ox.settings.requests_timeout = 8

    last_err: Exception | None = None
    for mirror in OVERPASS_MIRRORS:
        # Retry only on 429, 503, 504. Do NOT retry 400.
        for attempt in range(2):
            try:
                ox.settings.overpass_url = mirror
                logger.info(
                    f"[Overpass Request] Mirror: {mirror} | Bbox: {padded} | Attempt: {attempt+1}"
                )
                t0 = time.time()
                G = ox.graph_from_bbox(bbox=padded, network_type=network_type)
                logger.info(f"[Overpass OK] Mirror: {mirror} in {time.time()-t0:.2f}s")
                _graph_cache[cache_key] = (now, G)
                try:
                    disk_path.write_bytes(pickle.dumps(G))
                except Exception:
                    pass
                return G
            except Exception as exc:
                last_err = exc
                err_str = str(exc)
                logger.warning(
                    f"[Overpass Error] Mirror: {mirror} | Attempt: {attempt+1} | "
                    f"Error: {err_str[:500]}"
                )
                # Never retry 400
                if "400" in err_str:
                    break
                # Retry transient 429 / 503 / 504
                if any(code in err_str for code in ("429", "503", "504")) and attempt == 0:
                    time.sleep(1.0)
                    continue
                break

    # 4. Fallback: Core OpenStreetMap 0.6 API (with auto-tiling & 400 node limit handling)
    try:
        logger.info(f"Attempting Core OSM API fallback for bbox {padded}...")
        G = _load_graph_from_core_osm_api(padded, mode=mode)
        _graph_cache[cache_key] = (now, G)
        try:
            disk_path.write_bytes(pickle.dumps(G))
        except Exception:
            pass
        return G
    except Exception as exc:
        last_err = exc
        logger.error(f"Core OSM API failed: {exc}")

    raise RuntimeError("Area too large or map servers busy. Try a smaller area.")


# ── Edge geometry helpers ──

def _edge_line(G: nx.MultiDiGraph, u: int, v: int, data: dict) -> LineString | None:
    """Build a Shapely LineString for a graph edge."""
    geom = data.get("geometry")
    if geom is not None and hasattr(geom, "coords"):
        return geom if isinstance(geom, LineString) else LineString(geom.coords)
    u_d, v_d = G.nodes.get(u, {}), G.nodes.get(v, {})
    if "x" in u_d and "y" in u_d and "x" in v_d and "y" in v_d:
        return LineString([(u_d["x"], u_d["y"]), (v_d["x"], v_d["y"])])
    return None


def _damage_fraction(edge_geom: LineString, damage_tree: STRtree, damage_polys: list) -> float:
    """Fraction of edge length intersecting damage polygons."""
    if edge_geom is None or edge_geom.is_empty or not damage_polys:
        return 0.0
    total_len = edge_geom.length
    if total_len == 0:
        return 0.0
    idxs = damage_tree.query(edge_geom)
    damaged_len = 0.0
    for idx in idxs:
        poly = damage_polys[idx]
        try:
            inter = edge_geom.intersection(poly)
            damaged_len += inter.length
        except Exception:
            pass
    return min(damaged_len / total_len, 1.0)


# ── Cost assignment ──

def assign_edge_costs(
    G: nx.MultiDiGraph,
    damage_polys: list,
) -> set[tuple[int, int, int]]:
    """Assign 'cost' to each edge and return blocked edge keys.

    cost = length * (1 + ALPHA * damage_fraction)
    Blocked when damage_fraction >= BLOCK_THRESHOLD or highway=construction / access=no.
    """
    blocked: set[tuple[int, int, int]] = set()

    # Build STRtree from damage polygons
    damage_tree = STRtree(damage_polys) if damage_polys else None

    for u, v, k, data in G.edges(keys=True, data=True):
        base_len = float(data.get("length", 1.0))

        # Check for OSM blocking tags
        highway = str(data.get("highway", ""))
        access = str(data.get("access", ""))
        if highway == "construction" or access == "no":
            data["cost"] = float("inf")
            data["blocked"] = True
            data["damage_frac"] = 0.0
            blocked.add((u, v, k))
            continue

        # Compute damage fraction
        edge_geom = _edge_line(G, u, v, data)
        dfrac = _damage_fraction(edge_geom, damage_tree, damage_polys) if damage_tree else 0.0
        data["damage_frac"] = dfrac

        if dfrac >= BLOCK_THRESHOLD:
            data["cost"] = float("inf")
            data["blocked"] = True
            blocked.add((u, v, k))
        else:
            data["cost"] = base_len * (1.0 + ALPHA * dfrac)
            data["blocked"] = False

    return blocked


# ── Dead-end detection ──

def prune_dead_ends(
    G: nx.MultiDiGraph,
    protected_nodes: set[int],
) -> list[tuple[int, int]]:
    """Iteratively prune degree-1 nodes (except protected ones).

    Returns list of (u, v) edges that form dead-end spurs.
    """
    # Work on undirected view for degree check
    G_undir = G.to_undirected()
    dead_edges: list[tuple[int, int]] = []
    changed = True

    while changed:
        changed = False
        to_remove = []
        for node in list(G_undir.nodes):
            if node in protected_nodes:
                continue
            if G_undir.degree(node) == 1:
                neighbors = list(G_undir.neighbors(node))
                if neighbors:
                    dead_edges.append((node, neighbors[0]))
                to_remove.append(node)

        for node in to_remove:
            if node in G_undir:
                G_undir.remove_node(node)
                changed = True

    return dead_edges


def find_stranded_nodes(
    G: nx.MultiDiGraph,
    blocked_edges: set[tuple[int, int, int]],
    shelter_nodes: set[int],
) -> set[int]:
    """Find nodes that cannot reach any shelter after removing blocked edges.

    Uses reverse reachability from shelter nodes.
    If no shelter nodes exist, no nodes can be considered stranded.
    """
    if not shelter_nodes:
        return set()

    # Build a working copy without blocked edges
    H = G.copy()
    for u, v, k in blocked_edges:
        if H.has_edge(u, v, k):
            H.remove_edge(u, v, k)

    # Reverse reachability: in an undirected view, find all nodes connected
    # to any shelter node
    H_undir = H.to_undirected()
    reachable: set[int] = set()

    for sn in shelter_nodes:
        if sn in H_undir:
            reachable.update(nx.node_connected_component(H_undir, sn))

    return set(G.nodes) - reachable


def dead_end_geojson(
    G: nx.MultiDiGraph,
    dead_edges: list[tuple[int, int]],
    stranded_nodes: set[int],
) -> dict[str, Any]:
    """Build GeoJSON for dead-end and stranded edges with X markers at termini."""
    features: list[dict] = []

    # Dead-end spur edges
    for u, v in dead_edges:
        u_d = G.nodes.get(u, {})
        v_d = G.nodes.get(v, {})
        if "x" in u_d and "y" in u_d and "x" in v_d and "y" in v_d:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[u_d["x"], u_d["y"]], [v_d["x"], v_d["y"]]],
                },
                "properties": {"kind": "dead_end"},
            })
            # X marker at the dead-end terminus (the degree-1 node)
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [u_d["x"], u_d["y"]]},
                "properties": {"kind": "dead_end_terminus"},
            })

    # Stranded edges
    for u, v, _k, data in G.edges(keys=True, data=True):
        if u in stranded_nodes or v in stranded_nodes:
            u_d = G.nodes.get(u, {})
            v_d = G.nodes.get(v, {})
            if "x" in u_d and "y" in u_d and "x" in v_d and "y" in v_d:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[u_d["x"], u_d["y"]], [v_d["x"], v_d["y"]]],
                    },
                    "properties": {"kind": "stranded"},
                })

    return {"type": "FeatureCollection", "features": features}


# ── Shelter snapping ──

GENERIC_SHELTER_NAMES = {
    "shelter",
    "emergency shelter",
    "transit shelter",
    "weather shelter",
    "park shelter",
    "civil defense bunker",
    "bunker",
    "assembly point",
    "evacuation assembly point",
    "emergency hut",
    "school shelter",
    "civic shelter",
    "community center shelter",
}


def _get_node_street_name(G: nx.MultiDiGraph, node: int, max_hops: int = 4) -> str | None:
    """Find the best human-readable street name for a graph node, searching nearby edges if needed."""
    # 1. Immediate incident edges
    streets: list[str] = []
    for _u, _v, d in G.edges(node, data=True):
        name = d.get("name")
        if name:
            if isinstance(name, list):
                streets.extend(name)
            else:
                streets.append(name)
    for _u, _v, d in G.in_edges(node, data=True):
        name = d.get("name")
        if name:
            if isinstance(name, list):
                streets.extend(name)
            else:
                streets.append(name)

    seen = set()
    unique_streets = []
    for st in streets:
        if st and st not in seen:
            seen.add(st)
            unique_streets.append(st)

    if unique_streets:
        if len(unique_streets) == 1:
            return unique_streets[0]
        return f"{unique_streets[0]} & {unique_streets[1]}"

    # 2. BFS nearby nodes up to max_hops to find nearest named street (e.g. for footways in quads)
    visited = {node}
    queue = [node]
    depth = {node: 0}
    while queue:
        curr = queue.pop(0)
        d = depth[curr]
        if d >= max_hops:
            break
        for _u, v, data in G.edges(curr, data=True):
            name = data.get("name")
            if name:
                return name[0] if isinstance(name, list) else name
            if v not in visited:
                visited.add(v)
                depth[v] = d + 1
                queue.append(v)
        for u, _v, data in G.in_edges(curr, data=True):
            name = data.get("name")
            if name:
                return name[0] if isinstance(name, list) else name
            if u not in visited:
                visited.add(u)
                depth[u] = d + 1
                queue.append(u)

    return None


def snap_shelters_to_graph(
    G: nx.MultiDiGraph,
    shelters: list[dict],
) -> list[dict]:
    """Add 'node' key to each shelter dict with the nearest graph node.

    Also enriches generic shelter names with the nearest street name if available.
    """
    import osmnx as ox

    if not shelters:
        return []

    lats = [s["lat"] for s in shelters]
    lons = [s["lon"] for s in shelters]
    try:
        nodes = ox.nearest_nodes(G, X=lons, Y=lats)
        # osmnx returns a bare int when given a single coordinate pair
        if isinstance(nodes, (int, float)):
            nodes = [nodes]
        else:
            nodes = list(nodes)
    except Exception:
        nodes = [None] * len(shelters)

    result = []
    for s, n in zip(shelters, nodes):
        if n is not None:
            node_id = int(n)
            s_copy = dict(s)
            s_copy["node"] = node_id

            # Street name enrichment for generic or missing names
            street_name = _get_node_street_name(G, node_id)
            curr_name = s_copy.get("name", "Shelter").strip()

            if street_name:
                if not s_copy.get("street"):
                    s_copy["street"] = street_name
                if curr_name.lower() in GENERIC_SHELTER_NAMES or not curr_name:
                    s_copy["name"] = f"{curr_name} on {street_name}"

            result.append(s_copy)
    return result


# ── Route extraction ──

def _extract_route_coords(
    G: nx.MultiDiGraph,
    path: list[int],
    start_pt: list[float] | tuple[float, float] | None = None,
    end_pt: list[float] | tuple[float, float] | None = None,
) -> list[list[float]]:
    """Extract [lon, lat] coordinates along a node path, with exact start and end pin connections."""
    coords: list[list[float]] = []

    # Prepend exact start marker [lon, lat]
    if start_pt is not None:
        coords.append([round(float(start_pt[0]), 6), round(float(start_pt[1]), 6)])

    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        edge_data = G.get_edge_data(u, v)
        if edge_data is None:
            continue

        # Pick best key in multigraph
        if isinstance(edge_data, dict):
            best = min(edge_data.values(), key=lambda d: d.get("cost", float("inf")))
        else:
            best = edge_data

        geom = best.get("geometry")
        if geom is not None and hasattr(geom, "coords"):
            edge_coords = list(geom.coords)
            # Check direction
            u_d = G.nodes[u]
            first_dist = (edge_coords[0][0] - u_d.get("x", 0)) ** 2 + \
                         (edge_coords[0][1] - u_d.get("y", 0)) ** 2
            last_dist = (edge_coords[-1][0] - u_d.get("x", 0)) ** 2 + \
                        (edge_coords[-1][1] - u_d.get("y", 0)) ** 2
            if last_dist < first_dist:
                edge_coords = list(reversed(edge_coords))
            for lon, lat in edge_coords:
                pt = [round(float(lon), 6), round(float(lat), 6)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)
        else:
            for n in [u, v]:
                nd = G.nodes[n]
                pt = [round(float(nd.get("x", 0)), 6), round(float(nd.get("y", 0)), 6)]
                if not coords or coords[-1] != pt:
                    coords.append(pt)

    # Append exact shelter [lon, lat]
    if end_pt is not None:
        end_coord = [round(float(end_pt[0]), 6), round(float(end_pt[1]), 6)]
        if not coords or coords[-1] != end_coord:
            coords.append(end_coord)

    return coords


def _route_damage_exposure(
    G: nx.MultiDiGraph,
    path: list[int],
) -> float:
    """Total meters of route passing through damage."""
    total = 0.0
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        edge_data = G.get_edge_data(u, v)
        if edge_data is None:
            continue
        best = min(edge_data.values(), key=lambda d: d.get("cost", float("inf")))
        frac = best.get("damage_frac", 0.0)
        length = float(best.get("length", 0))
        total += frac * length
    return total


def _edge_set(path: list[int]) -> set[tuple[int, int]]:
    """Set of (u, v) edges in a path (direction-independent)."""
    edges = set()
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        edges.add((min(u, v), max(u, v)))
    return edges


def _overlap_fraction(edges_a: set, edges_b: set) -> float:
    """Fraction of edges_a that overlap with edges_b."""
    if not edges_a:
        return 0.0
    return len(edges_a & edges_b) / len(edges_a)


# ── Main routing ──

def compute_routes(
    G: nx.MultiDiGraph,
    start_lat: float,
    start_lon: float,
    shelters: list[dict],
    mode: str = "walk",
    damage_polys: list | None = None,
    damage_assessed: bool = True,
) -> dict[str, Any]:
    """Compute up to 3 best evacuation routes to real shelters.

    Returns a dict ready to be serialized as JSON.
    """
    import osmnx as ox

    damage_polys = damage_polys or []
    speed_kmh = WALK_SPEED if mode == "walk" else DRIVE_SPEED
    speed_ms = speed_kmh / 3.6
    warnings: list[str] = []

    # 1. Assign edge costs
    blocked_edges = assign_edge_costs(G, damage_polys)

    # 2. Snap start
    try:
        start_node = ox.nearest_nodes(G, X=start_lon, Y=start_lat)
    except Exception:
        return {"routes": [], "warnings": ["Could not snap start point to road network."], "disclaimer": DISCLAIMER}

    # 3. Snap shelters to graph
    snapped = snap_shelters_to_graph(G, shelters)
    shelter_nodes = {s["node"] for s in snapped if "node" in s}

    if not shelter_nodes:
        return {
            "routes": [],
            "warnings": ["No shelters found in the selected area. Try expanding the evacuation zone."],
            "disclaimer": DISCLAIMER,
        }

    # 4. Dead-end detection
    protected = {start_node} | shelter_nodes
    dead_edges = prune_dead_ends(G, protected)
    stranded = find_stranded_nodes(G, blocked_edges, shelter_nodes)

    # Check if start is stranded
    if start_node in stranded:
        return {
            "routes": [],
            "warnings": [
                "Your start location cannot reach any shelter. "
                "The area may be cut off by damage. Try enlarging the evacuation zone."
            ],
            "disclaimer": DISCLAIMER,
        }

    # 5. Compute cheapest paths to shelters
    try:
        lengths, paths = nx.single_source_dijkstra(G, source=start_node, weight="cost")
    except nx.NetworkXException:
        return {"routes": [], "warnings": ["Routing failed on this road network."], "disclaimer": DISCLAIMER}

    # Find reachable shelters sorted by cost
    shelter_costs = []
    for s in snapped:
        node = s.get("node")
        if node and node in lengths and math.isfinite(lengths[node]):
            shelter_costs.append((lengths[node], s, paths[node]))

    shelter_costs.sort(key=lambda x: x[0])

    if not shelter_costs:
        return {
            "routes": [],
            "warnings": ["No shelter is reachable from your start location. Try expanding the zone."],
            "disclaimer": DISCLAIMER,
        }

    # 6. Collect candidate routes
    candidates: list[tuple[float, dict, list[int]]] = []

    # Direct cheapest to each of the top N shelters
    for cost, shelter, path in shelter_costs[:MAX_SHELTERS]:
        candidates.append((cost, shelter, path))

    # Yen alternatives to the best shelter (on simplified DiGraph for performance)
    best_shelter_node = shelter_costs[0][1].get("node")
    if best_shelter_node:
        try:
            simple_G = nx.DiGraph()
            for u, v, _k, data in G.edges(keys=True, data=True):
                cost_val = data.get("cost", float("inf"))
                if not simple_G.has_edge(u, v) or simple_G[u][v]["weight"] > cost_val:
                    simple_G.add_edge(u, v, weight=cost_val)

            alt_count = 0
            for alt_path in nx.shortest_simple_paths(simple_G, start_node, best_shelter_node, weight="weight"):
                if alt_count >= 5:
                    break
                alt_cost = sum(simple_G[alt_path[i]][alt_path[i + 1]]["weight"]
                               for i in range(len(alt_path) - 1))
                if math.isfinite(alt_cost):
                    candidates.append((alt_cost, shelter_costs[0][1], alt_path))
                alt_count += 1
        except (nx.NetworkXException, nx.NodeNotFound):
            pass

    # 7. Select diverse top 3
    chosen: list[tuple[float, dict, list[int]]] = []
    chosen_edge_sets: list[set[tuple[int, int]]] = []

    # Sort candidates by cost
    candidates.sort(key=lambda x: x[0])

    for cost, shelter, path in candidates:
        if len(chosen) >= 3:
            break

        # Check path doesn't use blocked edges
        has_blocked = False
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            edge_data = G.get_edge_data(u, v)
            if edge_data:
                for kk, dd in edge_data.items():
                    if dd.get("blocked"):
                        has_blocked = True
                        break
            if has_blocked:
                break
        if has_blocked:
            continue

        es = _edge_set(path)

        # Check overlap with already-chosen routes
        too_similar = False
        for ces in chosen_edge_sets:
            if _overlap_fraction(es, ces) > OVERLAP_MAX:
                too_similar = True
                break
        if too_similar:
            continue

        chosen.append((cost, shelter, path))
        chosen_edge_sets.append(es)

    # 8. Build response
    routes_out: list[dict[str, Any]] = []
    rank_reasons = [
        "Shortest path to nearest shelter",
        "Alternative route",
        "Third best route",
    ]

    for idx, (cost, shelter, path) in enumerate(chosen):
        start_pt = [start_lon, start_lat]
        shelter_pt = [shelter.get("lon", start_lon), shelter.get("lat", start_lat)]
        coords = _extract_route_coords(G, path, start_pt=start_pt, end_pt=shelter_pt)

        # Distance along road edges
        graph_distance_m = 0.0
        for i in range(len(path) - 1):
            ed = G.get_edge_data(path[i], path[i + 1])
            if ed:
                best_k = min(ed.values(), key=lambda d: d.get("cost", float("inf")))
                graph_distance_m += float(best_k.get("length", 0))

        # Connector distances from marker to start_node, and end_node to shelter pin
        start_nd = G.nodes.get(path[0], {})
        end_nd = G.nodes.get(path[-1], {})
        start_conn_m = 0.0
        end_conn_m = 0.0
        if "y" in start_nd and "x" in start_nd:
            start_conn_m = haversine_distance_m(start_lat, start_lon, start_nd["y"], start_nd["x"])
        if "y" in end_nd and "x" in end_nd and "lat" in shelter and "lon" in shelter:
            end_conn_m = haversine_distance_m(shelter["lat"], shelter["lon"], end_nd["y"], end_nd["x"])

        distance_m = start_conn_m + graph_distance_m + end_conn_m
        eta_sec = distance_m / speed_ms if speed_ms > 0 else 0
        dmg_m = _route_damage_exposure(G, path)

        reason = rank_reasons[idx] if idx < len(rank_reasons) else "Alternative route"
        if damage_assessed:
            if dmg_m == 0:
                reason += ", no damage exposure"
            else:
                reason += f", {dmg_m:.0f} m through damage"
        else:
            reason += ", damage not assessed"

        # Different shelter than rank 1?
        if idx > 0 and shelter.get("node") != chosen[0][1].get("node"):
            reason += ", different shelter"

        routes_out.append({
            "rank": idx + 1,
            "geojson": {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"rank": idx + 1},
            },
            "distance_m": round(distance_m),
            "eta_seconds": round(eta_sec),
            "eta_display": _format_eta(eta_sec),
            "shelter": {
                "name": shelter.get("name", "Shelter"),
                "lat": shelter.get("lat"),
                "lon": shelter.get("lon"),
                "type": shelter.get("type", ""),
            },
            "damage_exposure_m": round(dmg_m) if damage_assessed else 0,
            "damage_assessed": damage_assessed,
            "rank_reason": reason,
        })

    return {
        "routes": routes_out,
        "warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


def _format_eta(seconds: float) -> str:
    """Human-friendly ETA string."""
    if seconds < 60:
        return f"{int(seconds)} sec"
    mins = round(seconds / 60)
    if mins < 60:
        return f"{mins} min"
    h = mins // 60
    m = mins % 60
    return f"{h} h {m} min"
