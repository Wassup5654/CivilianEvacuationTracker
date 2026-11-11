"""Routing and SVG map annotation for civilian evacuation using Sentinel-2 clips."""

from __future__ import annotations

import math
from typing import Any

import networkx as nx
import pyproj
from shapely.geometry import LineString, Point

HAZARD_PENALTY = 10_000_000.0
DEFAULT_CRS = "EPSG:4326"
DEFAULT_CLIP_SIZE = (500, 500)
ROUTE_LABEL = "Route avoiding detected damage"
NO_ROUTE_MSG = "No route found that avoids detected damage"
DISCLAIMER_NOTE = "Imagery may be days old and cannot confirm a road is passable."


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two geographic coordinates in meters."""
    r = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def latlon_to_pixel(
    lat: float,
    lon: float,
    bounds: tuple[float, float, float, float],
    image_size: tuple[int, int] = DEFAULT_CLIP_SIZE,
    image_crs: str = DEFAULT_CRS,
) -> tuple[float, float]:
    """Convert a (lat, lon) point to native pixel coordinates in the image.

    Determines bounds and CRS using pyproj transformer and projects to image pixel space.
    """
    west, south, east, north = bounds
    img_w, img_h = image_size

    # Convert from geographic lat/lon (EPSG:4326) to image CRS
    transformer = pyproj.Transformer.from_crs("EPSG:4326", image_crs, always_xy=True)
    x, y = transformer.transform(lon, lat)
    x_min, y_min = transformer.transform(west, south)
    x_max, y_max = transformer.transform(east, north)

    if x_max == x_min or y_max == y_min:
        return 0.0, 0.0

    px = (x - x_min) / (x_max - x_min) * float(img_w)
    py = (y_max - y) / (y_max - y_min) * float(img_h)
    return px, py


def get_edge_midpoint(
    graph: nx.Graph | nx.DiGraph | nx.MultiDiGraph,
    u: Any,
    v: Any,
    data: dict[str, Any],
) -> tuple[float, float]:
    """Return the (lat, lon) midpoint of a road segment edge."""
    geom = data.get("geometry")
    if geom is not None and hasattr(geom, "interpolate"):
        mid = geom.interpolate(0.5, normalized=True)
        return float(mid.y), float(mid.x)

    u_data = graph.nodes[u]
    v_data = graph.nodes[v]
    u_lat, u_lon = float(u_data.get("y", 0.0)), float(u_data.get("x", 0.0))
    v_lat, v_lon = float(v_data.get("y", 0.0)), float(v_data.get("x", 0.0))
    return (u_lat + v_lat) / 2.0, (u_lon + v_lon) / 2.0


def apply_hazards(
    graph: nx.Graph | nx.DiGraph | nx.MultiDiGraph,
    hazards: list[tuple[float, float, float]] | None = None,
) -> None:
    """Apply hazard penalties to graph edges.

    Any road segment whose midpoint is within a hazard's radius gets a very large
    cost added.
    """
    if hazards is None:
        hazards = []

    # Iterate over all edges in the graph
    edges_iter = (
        graph.edges(keys=True, data=True)
        if graph.is_multigraph()
        else [(u, v, None, d) for u, v, d in graph.edges(data=True)]
    )

    for u, v, _k, data in edges_iter:
        base_len = float(data.get("length", 1.0))
        data["cost"] = base_len
        data["hazard_affected"] = False

        if not hazards:
            continue

        mid_lat, mid_lon = get_edge_midpoint(graph, u, v, data)

        for h_lat, h_lon, danger_radius_m in hazards:
            dist = haversine_distance_m(mid_lat, mid_lon, h_lat, h_lon)
            if dist <= danger_radius_m:
                data["cost"] += HAZARD_PENALTY
                data["hazard_affected"] = True
                break


def apply_damage_polygons(
    graph: nx.Graph | nx.DiGraph | nx.MultiDiGraph,
    damage_polygons: list[Any] | None,
) -> None:
    """Add large cost penalty to road segments intersecting damage polygons."""
    if not damage_polygons:
        return

    import shapely.geometry

    edges_iter = (
        graph.edges(keys=True, data=True)
        if graph.is_multigraph()
        else [(u, v, None, d) for u, v, d in graph.edges(data=True)]
    )

    for u, v, _k, data in edges_iter:
        geom = data.get("geometry")
        if geom is None:
            u_node = graph.nodes.get(u, {})
            v_node = graph.nodes.get(v, {})
            if "x" in u_node and "y" in u_node and "x" in v_node and "y" in v_node:
                geom = shapely.geometry.LineString([
                    (u_node["x"], u_node["y"]),
                    (v_node["x"], v_node["y"]),
                ])

        if geom is not None:
            for poly in damage_polygons:
                try:
                    if geom.intersects(poly):
                        data["cost"] = data.get("cost", data.get("length", 1.0)) + HAZARD_PENALTY
                        data["damage_blocked"] = True
                        break
                except Exception:
                    pass


def choose_exit_nodes(
    graph: nx.Graph | nx.DiGraph | nx.MultiDiGraph,
    bounds: tuple[float, float, float, float],
    margin_ratio: float = 0.08,
    damage_polygons: list[Any] | None = None,
) -> list[Any]:
    """Identify exit nodes located near the bounding box boundary.

    Prioritizes outer edge boundary cut nodes (roads that cross outside
    the bounding box) and prunes internal cul-de-sacs and dead ends.
    Filters out any exit nodes falling inside blocked damage polygons.
    """
    west, south, east, north = bounds
    lat_span = north - south
    lon_span = east - west
    lat_margin = lat_span * margin_ratio
    lon_margin = lon_span * margin_ratio

    tight_lat = lat_span * 0.03
    tight_lon = lon_span * 0.03

    outer_edge_nodes = []
    margin_nodes = []

    for node, data in graph.nodes(data=True):
        x = data.get("x")
        y = data.get("y")
        if x is None or y is None:
            continue

        # Filter out nodes in damage polygons
        if damage_polygons:
            import shapely.geometry
            pt = shapely.geometry.Point(x, y)
            if any(poly.contains(pt) for poly in damage_polygons):
                continue

        is_on_outer_edge = (
            x <= west + tight_lon
            or x >= east - tight_lon
            or y <= south + tight_lat
            or y >= north - tight_lat
        )

        in_margin = (
            x <= west + lon_margin
            or x >= east - lon_margin
            or y <= south + lat_margin
            or y >= north - lat_margin
        )

        deg = graph.degree(node) if hasattr(graph, "degree") else 2

        if is_on_outer_edge:
            outer_edge_nodes.append(node)
        elif in_margin and deg > 1:
            margin_nodes.append(node)

    if outer_edge_nodes:
        return outer_edge_nodes
    if margin_nodes:
        return margin_nodes
    return list(graph.nodes)


def get_road_network_for_bounds(
    bounds: tuple[float, float, float, float],
) -> nx.MultiDiGraph:
    """Download drivable road network using OSMnx for a bounding box."""
    import osmnx as ox

    west, south, east, north = bounds
    # OSMnx 2.x expects bbox as (left, bottom, right, top)
    return ox.graph_from_bbox(bbox=(west, south, east, north), network_type="drive")


def find_evacuation_route(
    graph: nx.Graph | nx.DiGraph | nx.MultiDiGraph,
    start_lat: float,
    start_lon: float,
    bounds: tuple[float, float, float, float],
    hazards: list[tuple[float, float, float]] | None = None,
    damage_polygons: list[Any] | None = None,
) -> tuple[list[tuple[float, float]] | None, str]:
    """Find the best route to an exit node avoiding detected damage.

    Always connects the user's start coordinate to the route, testing candidate
    entry roads so the path does not get stuck in a dead-end pocket.

    Returns:
        (route_lat_lon_points, message)
    """
    if hazards is None:
        hazards = []

    if graph.number_of_nodes() == 0:
        return None, NO_ROUTE_MSG

    # 1. Apply hazard costs
    apply_hazards(graph, hazards)
    apply_damage_polygons(graph, damage_polygons)

    # 2. Find exit nodes near boundary (pruning internal dead-ends & damage)
    exit_nodes = choose_exit_nodes(graph, bounds, damage_polygons=damage_polygons)
    if not exit_nodes:
        exit_nodes = list(graph.nodes)

    # 3. Candidate start nodes:
    # Find the nearest road access nodes to the address (within local street proximity)
    orig_node = None
    try:
        import osmnx as ox

        orig_node = ox.nearest_nodes(graph, X=start_lon, Y=start_lat)
    except Exception:
        pass

    nodes_with_dist = []
    for n in graph.nodes:
        n_lat = float(graph.nodes[n].get("y", start_lat))
        n_lon = float(graph.nodes[n].get("x", start_lon))
        d = haversine_distance_m(start_lat, start_lon, n_lat, n_lon)
        nodes_with_dist.append((d, n))
    nodes_with_dist.sort(key=lambda item: item[0])

    closest_dist, closest_node = nodes_with_dist[0]
    candidate_start_nodes: list[Any] = [closest_node]
    if orig_node is not None and orig_node not in candidate_start_nodes:
        candidate_start_nodes.insert(0, orig_node)

    # Only include alternate entry nodes if they are within immediate local street range
    max_cand_dist = min(closest_dist + 200.0, 400.0)
    for d, n in nodes_with_dist[1:5]:
        if d <= max_cand_dist and n not in candidate_start_nodes:
            candidate_start_nodes.append(n)

    # 4. Find best path across candidate start nodes
    best_path: list[Any] | None = None
    min_cost = float("inf")

    for cand_start in candidate_start_nodes:
        try:
            lengths, paths = nx.single_source_dijkstra(graph, source=cand_start, weight="cost")
        except nx.NetworkXException:
            continue

        reachable_exits = [n for n in exit_nodes if n in lengths and n != cand_start]
        if not reachable_exits:
            continue

        cand_best_exit = min(reachable_exits, key=lambda n: lengths[n])
        cand_cost = lengths[cand_best_exit]

        cand_dist = haversine_distance_m(
            start_lat,
            start_lon,
            float(graph.nodes[cand_start].get("y", start_lat)),
            float(graph.nodes[cand_start].get("x", start_lon)),
        )
        total_eval_cost = cand_cost + cand_dist

        if total_eval_cost < min_cost:
            min_cost = total_eval_cost
            best_path = paths[cand_best_exit]
            # If we found an unblocked path avoiding hazards, use it
            if cand_cost < HAZARD_PENALTY:
                break

    if not best_path:
        return None, NO_ROUTE_MSG

    # If the minimum cost path exceeds or equals HAZARD_PENALTY,
    # every available exit requires crossing a hazard.
    if min_cost >= HAZARD_PENALTY:
        return None, NO_ROUTE_MSG

    # 5. Extract coordinates along the path
    route_points: list[tuple[float, float]] = []
    for i in range(len(best_path) - 1):
        u = best_path[i]
        v = best_path[i + 1]
        edge_data = graph.get_edge_data(u, v)

        # In multigraph, choose edge with minimum cost
        best_edge = None
        if isinstance(edge_data, dict):
            if graph.is_multigraph():
                best_edge = min(edge_data.values(), key=lambda d: d.get("cost", 0.0))
            else:
                best_edge = edge_data

        geom = best_edge.get("geometry") if best_edge else None
        if geom is not None and hasattr(geom, "coords"):
            coords = list(geom.coords)
            for lon, lat in coords:
                pt = (float(lat), float(lon))
                if not route_points or route_points[-1] != pt:
                    route_points.append(pt)
        else:
            u_pt = (float(graph.nodes[u]["y"]), float(graph.nodes[u]["x"]))
            v_pt = (float(graph.nodes[v]["y"]), float(graph.nodes[v]["x"]))
            if not route_points or route_points[-1] != u_pt:
                route_points.append(u_pt)
            route_points.append(v_pt)

    # ALWAYS prepend the exact start coordinate so the user's start pin is directly connected
    start_pt = (float(start_lat), float(start_lon))
    if not route_points or route_points[0] != start_pt:
        route_points.insert(0, start_pt)

    return route_points, ROUTE_LABEL


def generate_svg_overlay(
    route_points: list[tuple[float, float]] | None,
    hazards: list[tuple[float, float, float]] | None,
    bounds: tuple[float, float, float, float],
    image_size: tuple[int, int] = DEFAULT_CLIP_SIZE,
    image_crs: str = DEFAULT_CRS,
    start_point: tuple[float, float] | None = None,
    damage_pixel_polygons: list[Any] | None = None,
) -> str:
    """Generate an SVG overlay element tied to native pixel dimensions (viewBox 0 0 500 500)."""
    if hazards is None:
        hazards = []

    img_w, img_h = image_size
    elements: list[str] = [
        f'<svg class="map-svg-overlay" viewBox="0 0 {img_w} {img_h}" '
        'xmlns="http://www.w3.org/2000/svg" style="width: 100%; height: 100%; '
        'position: absolute; top: 0; left: 0; pointer-events: none;">',
        '  <defs>',
        '    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">',
        '      <feDropShadow dx="0" dy="1" stdDeviation="1" flood-color="#000" flood-opacity="0.8"/>',
        '    </filter>',
        '  </defs>',
    ]

    # Draw damage layer (red filled polygons, solid red outline, no pins/markers)
    if damage_pixel_polygons:
        from damage import pixel_polygons_to_svg_paths
        dmg_svg = pixel_polygons_to_svg_paths(damage_pixel_polygons)
        if dmg_svg:
            elements.append(dmg_svg)

    # Draw hazards as marked circles
    for h_lat, h_lon, danger_radius_m in hazards:
        hx, hy = latlon_to_pixel(h_lat, h_lon, bounds, image_size, image_crs)
        # 10 m per pixel at native resolution
        radius_px = max(danger_radius_m / 10.0, 4.0)

        elements.append(
            f'  <circle cx="{hx:.1f}" cy="{hy:.1f}" r="{radius_px:.1f}" '
            'fill="rgba(220, 38, 38, 0.35)" stroke="#dc2626" stroke-width="2" '
            'stroke-dasharray="4,2"/>'
        )
        elements.append(
            f'  <circle cx="{hx:.1f}" cy="{hy:.1f}" r="3.5" fill="#dc2626" stroke="#ffffff" stroke-width="1"/>'
        )
        elements.append(
            f'  <text x="{hx:.1f}" y="{hy + radius_px + 12:.1f}" fill="#fca5a5" font-size="10" '
            'font-family="system-ui, sans-serif" font-weight="600" text-anchor="middle" '
            'filter="url(#shadow)">Hazard</text>'
        )

    # Draw route if found (blue line)
    if route_points and len(route_points) > 1:
        pixel_points = [
            latlon_to_pixel(lat, lon, bounds, image_size, image_crs)
            for lat, lon in route_points
        ]
        pts_attr = " ".join(f"{x:.1f},{y:.1f}" for x, y in pixel_points)

        # Route glow/shadow (navy blue)
        elements.append(
            f'  <polyline points="{pts_attr}" fill="none" stroke="#1e3a8a" stroke-width="6" '
            'stroke-linecap="round" stroke-linejoin="round" opacity="0.6"/>'
        )
        # Route primary line (vibrant blue: #2563eb)
        elements.append(
            f'  <polyline points="{pts_attr}" fill="none" stroke="#2563eb" stroke-width="4" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )

        # Destination marker at the boundary exit
        ex, ey = pixel_points[-1]
        elements.append(
            f'  <circle cx="{ex:.1f}" cy="{ey:.1f}" r="5" fill="#2563eb" stroke="#ffffff" stroke-width="2"/>'
        )
        elements.append(
            f'  <text x="{ex:.1f}" y="{ey - 8:.1f}" fill="#93c5fd" font-size="10" '
            'font-family="system-ui, sans-serif" font-weight="700" text-anchor="middle" '
            'filter="url(#shadow)">Exit</text>'
        )

    # Start coordinate marker
    if start_point is not None:
        sx, sy = latlon_to_pixel(start_point[0], start_point[1], bounds, image_size, image_crs)
        elements.append(
            f'  <circle cx="{sx:.1f}" cy="{sy:.1f}" r="5" fill="#9a6324" stroke="#ffffff" stroke-width="2"/>'
        )
        elements.append(
            f'  <text x="{sx + 8:.1f}" y="{sy + 4:.1f}" fill="#fef3c7" font-size="10" '
            'font-family="system-ui, sans-serif" font-weight="700" filter="url(#shadow)">Start</text>'
        )

    # Scale bar tied to viewBox (100 px = 1000 m = 1 km)
    elements.extend(
        [
            '  <g class="svg-scale-bar">',
            '    <rect x="15" y="455" width="115" height="35" rx="4" fill="rgba(20, 25, 22, 0.75)"/>',
            '    <line x1="22" y1="474" x2="122" y2="474" stroke="#ffffff" stroke-width="2"/>',
            '    <line x1="22" y1="469" x2="22" y2="479" stroke="#ffffff" stroke-width="2"/>',
            '    <line x1="122" y1="469" x2="122" y2="479" stroke="#ffffff" stroke-width="2"/>',
            '    <text x="72" y="468" fill="#ffffff" font-size="10" font-family="system-ui, sans-serif" font-weight="600" text-anchor="middle">1 km</text>',
            '    <text x="72" y="484" fill="#d1d5db" font-size="7.5" font-family="system-ui, sans-serif" text-anchor="middle">10 m / native px</text>',
            '  </g>',
            '</svg>',
        ]
    )

    return "\n".join(elements)
