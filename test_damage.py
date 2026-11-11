"""Tests for damage overlay, blob extraction, GeoJSON export, and routing avoidance."""

from __future__ import annotations

import os
import sys

# Ensure project root is on path and Django is configured
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

import numpy as np
from PIL import Image
import networkx as nx
from shapely.geometry import box, Polygon

from damage import (
    compute_damage_probability_map,
    probability_map_to_blobs,
    extract_polygon_blobs,
    pixel_polygons_to_svg_paths,
    detect_damage_for_square,
)
from routing import (
    find_evacuation_route,
    generate_svg_overlay,
    ROUTE_LABEL,
)


def test_probability_map_and_morphological_blobs() -> None:
    """Verify thresholding, morphological closing/opening, and small component removal."""
    # Create 500x500 mock probability map
    prob_map = np.zeros((500, 500), dtype=np.float32)

    # Large damage cluster: 30x30 pixels (900 px > 16 min_area)
    prob_map[100:130, 100:130] = 0.85
    # Small isolated noise spikes (4 pixels < 16 min_area)
    prob_map[10:12, 10:12] = 0.90

    # 1. Test blob processing
    blobs = probability_map_to_blobs(prob_map, threshold=0.5, min_area_px=16)

    # Large cluster should remain
    assert blobs[115, 115] == True, "Large damage cluster must be retained"
    # Small noise should be dropped
    assert blobs[10, 10] == False, "Noise smaller than min_area_px must be dropped"
    print("test_probability_map_and_morphological_blobs passed")


def test_contour_extraction_affine_geojson() -> None:
    """Verify contour extraction, simplification, and affine transform into GeoJSON."""
    mask = np.zeros((500, 500), dtype=bool)
    # 50x50 pixel block at pixel (200..250, 200..250)
    mask[200:250, 200:250] = True

    bounds = (30.0, 50.0, 31.0, 51.0)  # west, south, east, north
    geojson, geo_polys, pixel_polys = extract_polygon_blobs(mask, bounds)

    assert geojson["type"] == "FeatureCollection"
    assert len(geojson["features"]) == 1
    feat = geojson["features"][0]
    assert feat["type"] == "Feature"
    assert feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert feat["properties"]["pixel_area"] == 2500
    assert feat["properties"]["area_m2"] == 250000

    # Verify affine transformed coordinates fall within bounds
    geo_p = geo_polys[0]
    minx, miny, maxx, maxy = geo_p.bounds
    assert minx >= bounds[0] and maxx <= bounds[2]
    assert miny >= bounds[1] and maxy <= bounds[3]

    # Verify SVG paths rendering
    svg = pixel_polygons_to_svg_paths(pixel_polys)
    assert 'fill="rgba(220, 38, 38, 0.4)"' in svg
    assert 'stroke="#dc2626"' in svg
    assert 'id="damage-layer"' in svg
    assert '<circle' not in svg, "Do not use pins or markers for damage blobs"
    print("test_contour_extraction_affine_geojson passed")


def test_damage_polygons_block_router() -> None:
    """Verify that damage polygons block the shortest route and Dijkstra diverts around."""
    bounds = (30.0, 50.0, 31.0, 51.0)
    # Path 1: 0 -> 1 -> 3 (via lon 30.6)
    # Path 2: 0 -> 2 -> 3 (via lon 30.4)
    G = nx.MultiDiGraph()
    G.add_node(0, x=30.5, y=50.5)
    G.add_node(1, x=30.6, y=50.6)
    G.add_node(2, x=30.4, y=50.7)
    G.add_node(3, x=30.99, y=50.99)  # outer edge exit

    G.add_edge(0, 1, 0, length=50.0)
    G.add_edge(1, 3, 0, length=50.0)

    G.add_edge(0, 2, 0, length=150.0)
    G.add_edge(2, 3, 0, length=150.0)

    # 1. No damage: Path 1 chosen
    route_clear, _ = find_evacuation_route(G, start_lat=50.5, start_lon=30.5, bounds=bounds)
    assert route_clear is not None
    assert 30.6 in [lon for lat, lon in route_clear]

    # 2. Place damage polygon covering node 1 and edge (0, 1)
    damage_box = box(30.55, 50.55, 30.65, 50.65)
    route_diverted, msg = find_evacuation_route(
        G, start_lat=50.5, start_lon=30.5, bounds=bounds, damage_polygons=[damage_box]
    )
    assert route_diverted is not None
    assert msg == ROUTE_LABEL
    path_lons = [lon for lat, lon in route_diverted]
    assert 30.4 in path_lons, "Router must divert around damage polygon to Path 2"
    assert 30.6 not in path_lons, "Router must avoid damaged road corridor"

    # 3. SVG overlay includes damage layer, blue line, no pins
    svg = generate_svg_overlay(
        route_points=route_diverted,
        hazards=[],
        bounds=bounds,
        damage_pixel_polygons=[box(200, 200, 250, 250)],
    )
    assert 'id="damage-layer"' in svg
    assert 'rgba(220, 38, 38, 0.4)' in svg
    assert '#2563eb' in svg, "Route line must stay blue"
    print("test_damage_polygons_block_router passed")


def test_backend_selection_and_xbd_building_classification() -> None:
    """Verify DAMAGE_BACKEND selection and xbd_s12 building damage classification tagging."""
    from unittest.mock import patch
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        folder = Path(tmpdir)
        # Create minimal latest.tif
        img = Image.new("RGB", (64, 64), color=(100, 100, 100))
        img.save(folder / "latest.tif")

        # Mock damaged_building_mask to return a 64x64 mask with a 20x20 damage blob
        mock_mask = np.zeros((64, 64), dtype=np.float32)
        mock_mask[10:30, 10:30] = 1.0

        with patch("django.conf.settings.DAMAGE_BACKEND", "xbd_s12"), \
             patch("xbd_s12_damage.damaged_building_mask", return_value=mock_mask):
            gj, geo_polygons, pixel_polygons = detect_damage_for_square(folder, (30.0, 50.0, 30.1, 50.1))

            assert gj["properties"]["classification"] == "damaged_building"
            assert gj["properties"]["model"] == "prs-eth/xbd-s12_dmg_seed1"
            assert gj["properties"]["road_passability_assessed"] is False
            for feat in gj["features"]:
                assert feat["properties"]["kind"] == "estimated_building_damage"
                assert "damage_probability" not in feat["properties"]

    print("test_backend_selection_and_xbd_building_classification passed")


if __name__ == "__main__":
    test_probability_map_and_morphological_blobs()
    test_contour_extraction_affine_geojson()
    test_damage_polygons_block_router()
    test_backend_selection_and_xbd_building_classification()
    print("All damage overlay, blob extraction, and router blocking tests passed!")
