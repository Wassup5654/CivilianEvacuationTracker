"""Suggest where relief workers should set up new shelters to reach the most people.

A maximum-coverage facility-location model on the walking network:

* Demand: built-up area (ESA WorldCover 2021, 10 m) aggregated into 50 m cells,
  used as a proxy for where people live (no headcounts are claimed). Cells
  inside damaged areas (official or estimated) count double, because people
  whose homes are damaged need shelter most.
* Candidates: road-network nodes (so supplies can reach them) at least
  SAFE_DISTANCE_M from any damage, in the zone's main connected network,
  thinned to one per 100 m cell.
* Coverage: a cell is covered when it is within COVER_RADIUS_M walking distance
  (along the network, avoiding blocked roads, plus the walk from the cell to
  its nearest road).
* Existing shelters count as already covering their surroundings, so the
  suggestions fill the gaps.
* Selection: greedy maximum coverage (pick the site that covers the most
  still-uncovered demand, repeat), which is within (1 - 1/e) of optimal.

Suggested sites are planning aids for relief workers, never routing
destinations, and are always labelled as suggestions to verify on the ground.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import networkx as nx
import numpy as np
from shapely.geometry import MultiPoint, Point, mapping
from shapely.ops import transform, unary_union

COVER_RADIUS_M = 800.0      # ~10 minutes on foot
CELL_PX = 5                 # 5 x 10 m pixels = 50 m demand cells
CANDIDATE_GRID_M = 100.0
SAFE_DISTANCE_M = 150.0
MAX_SNAP_M = 300.0          # demand further than this from any road is ignored
DAMAGE_WEIGHT = 2.0
MIN_GAIN_FRACTION = 0.01    # stop when a new site would add < 1% coverage
MAX_SITES = 8


def _projectors(lat: float, lon: float):
    from official_damage import _projectors as p
    return p(lat, lon)


def demand_cells(built: np.ndarray, bbox, cell_px: int = CELL_PX) -> tuple[np.ndarray, np.ndarray]:
    """(centres (n, 2) lon/lat, weights (n,)) of built-up demand cells."""
    h, w = built.shape
    hh, ww = h // cell_px, w // cell_px
    frac = built[: hh * cell_px, : ww * cell_px].reshape(hh, cell_px, ww, cell_px).mean(axis=(1, 3))
    rows, cols = np.nonzero(frac > 0)
    west, south, east, north = bbox
    lon = west + (cols + 0.5) * cell_px / w * (east - west)
    lat = north - (rows + 0.5) * cell_px / h * (north - south)
    return np.column_stack([lon, lat]), frac[rows, cols].astype(float)


def walking_graph(G: nx.MultiDiGraph) -> nx.Graph:
    """Undirected simple graph of non-blocked edges weighted by length (metres)."""
    H = nx.Graph()
    for u, v, data in G.edges(data=True):
        if data.get("blocked"):
            continue
        length = float(data.get("length", 1.0))
        if not H.has_edge(u, v) or H[u][v]["length"] > length:
            H.add_edge(u, v, length=length)
    for n in H.nodes:
        H.nodes[n].update(x=G.nodes[n]["x"], y=G.nodes[n]["y"])
    return H


def _nearest(H: nx.Graph, xs, ys):
    """Nearest node and straight-line distance (m) for each point, via a KD-tree."""
    from scipy.spatial import cKDTree

    nodes = list(H.nodes)
    lat0 = float(np.mean(ys))
    k = math.cos(math.radians(lat0)) * 111_320.0
    pts = np.column_stack([[H.nodes[n]["x"] * k for n in nodes], [H.nodes[n]["y"] * 111_320.0 for n in nodes]])
    tree = cKDTree(pts)
    d, idx = tree.query(np.column_stack([np.asarray(xs) * k, np.asarray(ys) * 111_320.0]))
    return [nodes[i] for i in idx], d


def candidate_nodes(H: nx.Graph, bbox, damage_geoms_m, to_m, grid_m: float = CANDIDATE_GRID_M) -> list:
    """Safe road nodes inside the zone, one (best-connected) per grid cell."""
    west, south, east, north = bbox
    main = max(nx.connected_components(H), key=len) if H.number_of_nodes() else set()
    danger = unary_union(damage_geoms_m).buffer(SAFE_DISTANCE_M) if damage_geoms_m else None
    best: dict[tuple[int, int], tuple[int, Any]] = {}
    for n in main:
        x, y = H.nodes[n]["x"], H.nodes[n]["y"]
        if not (west <= x <= east and south <= y <= north):
            continue
        mx, my = to_m(x, y)
        if danger is not None and danger.contains(Point(mx, my)):
            continue
        key = (int(mx // grid_m), int(my // grid_m))
        deg = H.degree(n)
        if key not in best or deg > best[key][0]:
            best[key] = (deg, n)
    return [n for _, n in best.values()]


def coverage_sets(H: nx.Graph, sources: list, cell_nodes: list, cell_snap: np.ndarray,
                  radius: float = COVER_RADIUS_M) -> dict:
    """{source node: array of covered cell indices}."""
    by_node: dict[Any, list[int]] = {}
    for i, n in enumerate(cell_nodes):
        by_node.setdefault(n, []).append(i)
    out = {}
    for s in sources:
        lengths = nx.single_source_dijkstra_path_length(H, s, cutoff=radius, weight="length")
        idx = [i for n, d in lengths.items() for i in by_node.get(n, ()) if d + cell_snap[i] <= radius]
        out[s] = np.array(idx, dtype=int)
    return out


def greedy_max_coverage(cover: dict, weights: np.ndarray, k: int, already: np.ndarray | None = None,
                        min_gain: float = MIN_GAIN_FRACTION) -> list[tuple[Any, float]]:
    """Pick up to k sources maximising newly covered weight. Returns [(source, gain)]."""
    covered = np.zeros(len(weights), dtype=bool) if already is None else already.copy()
    total = float(weights.sum()) or 1.0
    chosen = []
    remaining = dict(cover)
    for _ in range(k):
        best, best_gain = None, 0.0
        for s, idx in remaining.items():
            if idx.size == 0:
                continue
            gain = float(weights[idx][~covered[idx]].sum())
            if gain > best_gain:
                best, best_gain = s, gain
        if best is None or best_gain / total < min_gain:
            break
        covered[remaining.pop(best)] = True
        chosen.append((best, best_gain))
    return chosen


def suggest_sites(
    G: nx.MultiDiGraph,
    bbox: tuple[float, float, float, float],
    built: np.ndarray,
    damage_features: list[dict],
    existing_shelters: list[dict],
    k: int = 3,
    radius: float = COVER_RADIUS_M,
) -> dict[str, Any]:
    """Core model, independent of network access (G must already have edge costs)."""
    from evacuation_engine import _get_node_street_name

    k = max(1, min(int(k), MAX_SITES))
    clat, clon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
    to_m, to_deg = _projectors(clat, clon)
    H = walking_graph(G)
    if H.number_of_nodes() == 0:
        return {"sites": [], "coverage": {}, "notes": ["No walkable roads in this zone."]}

    centres, weights = demand_cells(built, bbox)
    if len(weights) == 0:
        return {"sites": [], "coverage": {}, "notes": ["No built-up area found in this zone."]}

    damage_m = [transform(to_m, f["geom"]) for f in damage_features]
    if damage_m:
        damaged = unary_union(damage_m)
        in_damage = np.array([damaged.contains(Point(*to_m(x, y))) for x, y in centres])
        weights = np.where(in_damage, weights * DAMAGE_WEIGHT, weights)
    else:
        in_damage = np.zeros(len(weights), dtype=bool)

    cell_nodes, cell_snap = _nearest(H, centres[:, 0], centres[:, 1])
    reachable = cell_snap <= MAX_SNAP_M
    weights = np.where(reachable, weights, 0.0)
    total = float(weights.sum())
    if total == 0:
        return {"sites": [], "coverage": {}, "notes": ["Built-up area is too far from walkable roads."]}

    # Existing shelters already cover their surroundings
    already = np.zeros(len(weights), dtype=bool)
    if existing_shelters:
        s_nodes, s_snap = _nearest(H, [s["lon"] for s in existing_shelters], [s["lat"] for s in existing_shelters])
        for node, cov in coverage_sets(H, [n for n, d in zip(s_nodes, s_snap) if d <= MAX_SNAP_M],
                                       cell_nodes, cell_snap, radius).items():
            already[cov] = True
    existing_pct = float(weights[already].sum()) / total

    candidates = candidate_nodes(H, bbox, damage_m, to_m)
    cover = coverage_sets(H, candidates, cell_nodes, cell_snap, radius)
    picks = greedy_max_coverage(cover, weights, k, already)

    covered = already.copy()
    sites = []
    for rank, (node, gain) in enumerate(picks, start=1):
        idx = cover[node]
        newly = idx[~covered[idx]]
        covered[idx] = True
        x, y = H.nodes[node]["x"], H.nodes[node]["y"]
        street = _get_node_street_name(G, node)
        p_m = Point(*to_m(x, y))
        nearest_damage = min((g.distance(p_m) for g in damage_m), default=None)
        hull = MultiPoint([Point(*to_m(*centres[i])) for i in idx]).convex_hull.buffer(25) if len(idx) else None
        sites.append({
            "rank": rank,
            "lat": round(y, 6),
            "lon": round(x, 6),
            "street": street or "",
            "name": f"Suggested site {rank}" + (f" on {street}" if street else ""),
            "adds_coverage_pct": round(100 * gain / total, 1),
            "damaged_area_share_pct": round(100 * float(weights[newly][in_damage[newly]].sum()) / max(gain, 1e-9), 1),
            "nearest_damage_m": round(nearest_damage) if nearest_damage is not None else None,
            "catchment": mapping(transform(to_deg, hull)) if hull is not None else None,
        })

    notes = []
    if not picks:
        notes.append("No safe site would add meaningful coverage"
                     + (" beyond the existing shelters." if existing_shelters else "."))
    return {
        "sites": sites,
        "coverage": {
            "existing_shelters_pct": round(100 * existing_pct, 1),
            "with_suggestions_pct": round(100 * float(weights[covered].sum()) / total, 1),
            "radius_m": radius,
            "existing_shelters": len(existing_shelters),
        },
        "notes": notes,
    }


def built_up_mask(bbox) -> np.ndarray:
    """ESA WorldCover 2021 built-up mask for the bbox at 10 m."""
    import radar_damage as rd

    w, h = rd._grid_size(bbox)
    wc = rd._search("esa-worldcover", bbox, date(2021, 1, 1), date(2021, 12, 31))
    if not wc:
        raise RuntimeError("No ESA WorldCover map for this zone.")
    return rd._fetch_npy("esa-worldcover", wc[0]["id"], bbox, w, h, ["map"])[0] == rd.BUILT_UP_CLASS
