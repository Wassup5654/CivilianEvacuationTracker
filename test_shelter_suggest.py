"""Tests for suggested shelter sites (shelter_suggest.py).

Run with: python manage.py test test_shelter_suggest
"""

import json
import unittest.mock

import networkx as nx
import numpy as np
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from shapely.geometry import Point, box

import shelter_suggest as ss

BBOX = (0.0, 0.0, 0.01, 0.01)  # ~1.1 km square near (0, 0)


def grid_graph(n=11, step=0.001):
    """n x n walkable grid, ~111 m between neighbours."""
    G = nx.MultiDiGraph()
    for r in range(n):
        for c in range(n):
            G.add_node(r * n + c, x=c * step, y=r * step)
    for r in range(n):
        for c in range(n):
            a = r * n + c
            for b in ([a + 1] if c < n - 1 else []) + ([a + n] if r < n - 1 else []):
                G.add_edge(a, b, 0, length=111.0, blocked=False)
                G.add_edge(b, a, 0, length=111.0, blocked=False)
    return G


def built_with_clusters():
    """100 x 100 px (10 m) mask: a big cluster south-west, a small one north-east."""
    built = np.zeros((100, 100), dtype=bool)
    built[70:95, 5:30] = True   # rows count from the north: south-west corner
    built[5:15, 80:90] = True   # north-east corner
    return built


def node_xy(G, lat, lon):
    return min(G.nodes, key=lambda n: (G.nodes[n]["x"] - lon) ** 2 + (G.nodes[n]["y"] - lat) ** 2)


class DemandTests(TestCase):
    def test_cells_aggregate_built_fraction(self):
        built = np.zeros((10, 10), dtype=bool)
        built[:5, :5] = True
        centres, weights = ss.demand_cells(built, (0, 0, 1, 1), cell_px=5)
        self.assertEqual(len(weights), 1)
        self.assertAlmostEqual(weights[0], 1.0)
        self.assertAlmostEqual(centres[0][0], 0.25)
        self.assertAlmostEqual(centres[0][1], 0.75)


class GreedyTests(TestCase):
    def test_picks_largest_gain_first_and_skips_covered(self):
        weights = np.array([5.0, 5.0, 1.0, 1.0])
        cover = {"a": np.array([0, 1]), "b": np.array([1, 2]), "c": np.array([3])}
        picks = ss.greedy_max_coverage(cover, weights, k=3, min_gain=0)
        self.assertEqual([p[0] for p in picks], ["a", "b", "c"])
        self.assertEqual(picks[1][1], 1.0)  # b only adds cell 2

    def test_already_covered_demand_is_not_counted(self):
        weights = np.array([5.0, 1.0])
        cover = {"a": np.array([0]), "b": np.array([1])}
        picks = ss.greedy_max_coverage(cover, weights, k=1, already=np.array([True, False]), min_gain=0)
        self.assertEqual(picks[0][0], "b")


class SuggestSitesTests(TestCase):
    def test_first_site_serves_the_big_cluster(self):
        G = grid_graph()
        out = ss.suggest_sites(G, BBOX, built_with_clusters(), [], [], k=2, radius=300)
        self.assertEqual(len(out["sites"]), 2)
        first = out["sites"][0]
        self.assertLess(first["lon"], 0.004)
        self.assertLess(first["lat"], 0.004)
        self.assertGreater(first["adds_coverage_pct"], out["sites"][1]["adds_coverage_pct"])
        self.assertEqual(out["coverage"]["existing_shelters_pct"], 0.0)
        self.assertGreater(out["coverage"]["with_suggestions_pct"], 90)

    def test_sites_stay_away_from_damage(self):
        G = grid_graph()
        damage = [{"geom": box(0.0005, 0.0005, 0.003, 0.003), "kind": "area", "grade": "damaged"}]
        out = ss.suggest_sites(G, BBOX, built_with_clusters(), damage, [], k=3, radius=300)
        from official_damage import _projectors
        to_m, _ = _projectors(0.005, 0.005)
        from shapely.ops import transform
        dmg_m = transform(to_m, damage[0]["geom"])
        for s in out["sites"]:
            self.assertGreaterEqual(Point(*to_m(s["lon"], s["lat"])).distance(dmg_m), ss.SAFE_DISTANCE_M - 1)
        self.assertGreater(out["sites"][0]["damaged_area_share_pct"], 0)

    def test_existing_shelter_fills_its_area_first(self):
        G = grid_graph()
        existing = [{"name": "S", "lat": 0.002, "lon": 0.0015}]
        out = ss.suggest_sites(G, BBOX, built_with_clusters(), [], existing, k=1, radius=300)
        self.assertGreater(out["coverage"]["existing_shelters_pct"], 50)
        # the remaining gap is the north-east cluster
        self.assertGreater(out["sites"][0]["lat"], 0.006)

    def test_blocked_roads_are_not_walkable(self):
        G = grid_graph()
        for u, v, k, d in G.edges(keys=True, data=True):
            d["blocked"] = True
        out = ss.suggest_sites(G, BBOX, built_with_clusters(), [], [], k=2)
        self.assertEqual(out["sites"], [])
        self.assertTrue(out["notes"])

    def test_no_built_up_area(self):
        out = ss.suggest_sites(grid_graph(), BBOX, np.zeros((100, 100), bool), [], [], k=2)
        self.assertEqual(out["sites"], [])
        self.assertIn("built-up", out["notes"][0])


class SuggestApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user(username="relief", password="pw-123456"))

    def test_requires_login_and_valid_bbox(self):
        anon = Client().get("/api/suggest-shelters", {"bbox": "0,0,0.01,0.01"})
        self.assertEqual(anon.status_code, 302)
        self.assertEqual(self.client.get("/api/suggest-shelters", {"bbox": "x"}).status_code, 400)

    def test_returns_sites_and_never_routes(self):
        damage = {"geojson": {"type": "FeatureCollection", "features": []}, "geo_polygons": [], "road_grades": {},
                  "features": [], "assessed": False, "sources": [], "notes": [], "radar_pending": False}
        with unittest.mock.patch("evacuation_engine.get_cached_graph", return_value=grid_graph()), \
             unittest.mock.patch("shelter_suggest.built_up_mask", return_value=built_with_clusters()), \
             unittest.mock.patch("finder.views._get_damage_for_bbox", return_value=damage), \
             unittest.mock.patch("shelters.find_shelters", return_value=([], False)):
            resp = self.client.get("/api/suggest-shelters", {"bbox": "0,0,0.01,0.01", "count": "2"})
        data = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(data["sites"]), 2)
        self.assertIn("Suggested site 1", data["sites"][0]["name"])
        self.assertIn("not a population count", data["basis"])
        self.assertNotIn("routes", data)
