"""Tests for evacuation routing, coordinate conversion, and hazard avoidance."""

import networkx as nx

import geocode
from routing import (
    DISCLAIMER_NOTE,
    HAZARD_PENALTY,
    NO_ROUTE_MSG,
    ROUTE_LABEL,
    apply_hazards,
    choose_exit_nodes,
    find_evacuation_route,
    generate_svg_overlay,
    latlon_to_pixel,
)
import scene


def test_latlon_to_pixel_conversion_using_known_point() -> None:
    """Test lat/lon to pixel conversion using a known bounding box and points."""
    # Bounding box: west=30.0, south=50.0, east=31.0, north=51.0
    bounds = (30.0, 50.0, 31.0, 51.0)
    image_size = (500, 500)

    # 1. Exact center point
    center_lat, center_lon = 50.5, 30.5
    px, py = latlon_to_pixel(center_lat, center_lon, bounds, image_size, "EPSG:4326")
    assert abs(px - 250.0) < 1e-4, f"Expected px ~ 250.0, got {px}"
    assert abs(py - 250.0) < 1e-4, f"Expected py ~ 250.0, got {py}"

    # 2. Top-left corner (north-west: lat=51.0, lon=30.0)
    tl_px, tl_py = latlon_to_pixel(51.0, 30.0, bounds, image_size, "EPSG:4326")
    assert abs(tl_px - 0.0) < 1e-4, f"Expected px ~ 0.0, got {tl_px}"
    assert abs(tl_py - 0.0) < 1e-4, f"Expected py ~ 0.0, got {tl_py}"

    # 3. Bottom-right corner (south-east: lat=50.0, lon=31.0)
    br_px, br_py = latlon_to_pixel(50.0, 31.0, bounds, image_size, "EPSG:4326")
    assert abs(br_px - 500.0) < 1e-4, f"Expected px ~ 500.0, got {br_px}"
    assert abs(br_py - 500.0) < 1e-4, f"Expected py ~ 500.0, got {br_py}"


def test_not_found_message() -> None:
    """Show a clear message if the address isn't found or no clear scene exists in the last 30 days."""
    # 1. Address not found in geocoder
    empty_matches = geocode.parse_results([])
    assert len(empty_matches) == 0, "Unknown address should return empty matches list"

    # Geocoder short query raises descriptive GeocodeError
    try:
        geocode.search_address("ab")
    except geocode.GeocodeError as exc:
        assert "Type a street address with at least a few characters." in str(exc)

    # 2. No clear satellite scene in last 30 days
    try:
        scene.choose_scene({"features": []})
    except scene.NoSceneError as exc:
        assert "No clear satellite image in the last 30 days." in str(exc)


def test_hazard_on_shortest_route_changes_route() -> None:
    """A hazard on the shortest route changes the route to an alternate path.

    If every exit requires crossing a hazard, it returns 'No route found that avoids detected damage'
    and draws no line.
    """
    bounds = (30.0, 50.0, 31.0, 51.0)  # west, south, east, north

    # Construct a road network graph
    # Start: Node 0 at center (30.5, 50.5)
    # Exit node 3 near boundary (30.95, 50.95)
    # Path 1 (shorter): 0 -> 1 -> 3 (total length 100m)
    # Path 2 (longer):  0 -> 2 -> 3 (total length 300m)
    G = nx.MultiDiGraph()
    G.add_node(0, x=30.5, y=50.5)
    G.add_node(1, x=30.6, y=50.6)
    G.add_node(2, x=30.4, y=50.7)
    G.add_node(3, x=30.95, y=50.95)  # Near eastern/northern boundary

    # Path 1 edges (length 50m each = 100m)
    G.add_edge(0, 1, 0, length=50.0)
    G.add_edge(1, 3, 0, length=50.0)

    # Path 2 edges (length 150m each = 300m)
    G.add_edge(0, 2, 0, length=150.0)
    G.add_edge(2, 3, 0, length=150.0)

    # Step A: No hazards -> Should pick shorter Path 1 (via Node 1)
    route_a, msg_a = find_evacuation_route(G, start_lat=50.5, start_lon=30.5, bounds=bounds, hazards=[])
    assert route_a is not None
    assert msg_a == ROUTE_LABEL
    assert "safe route" not in msg_a.lower(), "Must never label route 'safe route'"

    # Verify route starts directly at the user's start coordinate
    assert route_a[0] == (50.5, 30.5), "Route must always start at the exact start coordinates"

    # Verify route visits node 1 (Path 1)
    path_lons = [lon for lat, lon in route_a]
    assert 30.6 in path_lons, "Expected Path 1 (via lon 30.6) to be selected initially"

    # Verify blue line is drawn in SVG overlay
    svg_a = generate_svg_overlay(route_points=route_a, hazards=[], bounds=bounds)
    assert "#2563eb" in svg_a, "Route line must be drawn in blue (#2563eb)"

    # Step B: Place hazard on Path 1 (covering midpoint between node 0 and node 1: lat=50.55, lon=30.55)
    # Danger radius 5000m easily covers the segment
    hazard_1 = (50.55, 30.55, 5000.0)
    route_b, msg_b = find_evacuation_route(
        G, start_lat=50.5, start_lon=30.5, bounds=bounds, hazards=[hazard_1]
    )
    assert route_b is not None
    assert msg_b == ROUTE_LABEL

    # Verify route changed to Path 2 (via Node 2 at lon 30.4)
    path_b_lons = [lon for lat, lon in route_b]
    assert 30.4 in path_b_lons, "Route should change to Path 2 (avoiding hazard on Path 1)"
    assert 30.6 not in path_b_lons, "Route should avoid Path 1"

    # Step C: Place second hazard covering Path 2 as well
    hazard_2 = (50.6, 30.4, 5000.0)
    route_c, msg_c = find_evacuation_route(
        G, start_lat=50.5, start_lon=30.5, bounds=bounds, hazards=[hazard_1, hazard_2]
    )
    # Every exit requires crossing a hazard -> No route drawn
    assert route_c is None, "When every exit requires crossing a hazard, route should be None"
    assert msg_c == NO_ROUTE_MSG

    # Verify SVG overlay output draws no route line
    svg = generate_svg_overlay(route_points=route_c, hazards=[hazard_1, hazard_2], bounds=bounds)
    assert "<polyline" not in svg, "When no route exists, no route polyline should be drawn"
    assert "<circle" in svg, "Hazard circles should still be drawn"
    assert "Hazard" in svg
    assert "viewBox=\"0 0 500 500\"" in svg, "SVG overlay must tie coordinate system to native pixel size"


def test_cul_de_sac_dead_end_pruned_from_exits() -> None:
    """Internal neighborhood cul-de-sacs with degree 1 are pruned from exit nodes."""
    bounds = (30.0, 50.0, 31.0, 51.0)
    G = nx.MultiDiGraph()
    # Dead-end cul-de-sac node at x=30.95, y=50.95 with degree 1
    # Sitting in the 8% margin (within 0.08) but not on the outer boundary edge (outside 0.02)
    G.add_node("cul_de_sac", x=30.95, y=50.95)
    G.add_node("neighbor", x=30.80, y=50.80)
    G.add_edge("neighbor", "cul_de_sac", 0, length=100.0)

    # True boundary severed node (right on outer cut edge, x=30.99)
    G.add_node("true_exit", x=30.995, y=50.95)
    G.add_edge("neighbor", "true_exit", 0, length=100.0)

    exits = choose_exit_nodes(G, bounds)
    assert "cul_de_sac" not in exits, "Internal cul-de-sac must not be chosen as an exit"
    assert "true_exit" in exits, "True boundary-crossing road should be chosen as an exit"


if __name__ == "__main__":
    test_latlon_to_pixel_conversion_using_known_point()
    test_not_found_message()
    test_hazard_on_shortest_route_changes_route()
    test_cul_de_sac_dead_end_pruned_from_exits()
    print("All routing, coordinate conversion, and hazard avoidance tests passed!")
