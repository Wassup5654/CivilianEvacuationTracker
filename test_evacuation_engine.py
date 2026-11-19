"""Unit tests for evacuation_engine.py on small synthetic graphs."""

import math
import os
import sys
import unittest

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Minimal Django setup
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()

import networkx as nx
from shapely.geometry import box as shapely_box

from evacuation_engine import (
    assign_edge_costs,
    prune_dead_ends,
    find_stranded_nodes,
    _edge_set,
    _overlap_fraction,
    compute_routes,
    route_dead_ends,
    snap_shelters_to_graph,
)


def _make_grid_graph(rows=4, cols=4, edge_length=100.0):
    """Create a simple grid MultiDiGraph with node coordinates.

    Nodes are numbered 0..(rows*cols-1), laid out on a grid with
    lat/lon-like coordinates starting at (0, 0).
    """
    G = nx.MultiDiGraph()
    for r in range(rows):
        for c in range(cols):
            nid = r * cols + c
            # x = lon-like, y = lat-like
            G.add_node(nid, x=float(c) * 0.001, y=float(r) * 0.001)

    for r in range(rows):
        for c in range(cols):
            nid = r * cols + c
            # Right neighbor
            if c + 1 < cols:
                right = r * cols + (c + 1)
                G.add_edge(nid, right, length=edge_length, cost=edge_length)
                G.add_edge(right, nid, length=edge_length, cost=edge_length)
            # Up neighbor
            if r + 1 < rows:
                up = (r + 1) * cols + c
                G.add_edge(nid, up, length=edge_length, cost=edge_length)
                G.add_edge(up, nid, length=edge_length, cost=edge_length)

    return G


class TestDeadEndPruning(unittest.TestCase):
    """Test iterative degree-1 node pruning."""

    def test_spur_is_flagged(self):
        """A single spur chain off a grid should be pruned."""
        G = _make_grid_graph(3, 3)
        # Add a spur: node 100 -> node 101 off node 0
        G.add_node(100, x=-0.001, y=0.0)
        G.add_node(101, x=-0.002, y=0.0)
        G.add_edge(0, 100, length=100, cost=100)
        G.add_edge(100, 0, length=100, cost=100)
        G.add_edge(100, 101, length=100, cost=100)
        G.add_edge(101, 100, length=100, cost=100)

        protected = {4}  # center node protected
        dead = prune_dead_ends(G, protected)

        # Spur nodes 100 and 101 should appear in dead edges
        dead_nodes = set()
        for u, v in dead:
            dead_nodes.add(u)
            dead_nodes.add(v)
        self.assertIn(101, dead_nodes)
        self.assertIn(100, dead_nodes)

    def test_protected_not_pruned(self):
        """Nodes in the protected set should not be removed even if degree-1."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, length=100, cost=100)
        G.add_edge(1, 0, length=100, cost=100)

        protected = {0, 1}
        dead = prune_dead_ends(G, protected)
        self.assertEqual(len(dead), 0)


class TestStrandedNodes(unittest.TestCase):
    """Test reverse reachability for stranded node detection."""

    def test_disconnected_component_is_stranded(self):
        """Nodes not connected to any shelter should be stranded."""
        G = _make_grid_graph(2, 2)
        # Add isolated node
        G.add_node(99, x=0.01, y=0.01)

        shelter_nodes = {0}
        stranded = find_stranded_nodes(G, set(), shelter_nodes)
        self.assertIn(99, stranded)
        self.assertNotIn(0, stranded)

    def test_blocked_edge_creates_stranded(self):
        """Blocking the only bridge to a shelter should strand nodes."""
        # Linear graph: 0 -- 1 -- 2 (shelter at 2)
        G = nx.MultiDiGraph()
        for i in range(3):
            G.add_node(i, x=float(i) * 0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100, cost=100)
        G.add_edge(1, 0, 0, length=100, cost=100)
        G.add_edge(1, 2, 0, length=100, cost=100)
        G.add_edge(2, 1, 0, length=100, cost=100)

        # Block edge 1->2
        blocked = {(1, 2, 0), (2, 1, 0)}
        shelter_nodes = {2}
        stranded = find_stranded_nodes(G, blocked, shelter_nodes)
        self.assertIn(0, stranded)
        self.assertIn(1, stranded)
        self.assertNotIn(2, stranded)

    def test_empty_shelters_returns_no_stranded(self):
        """When no shelters exist, nodes should not be marked stranded."""
        G = _make_grid_graph(2, 2)
        stranded = find_stranded_nodes(G, set(), set())
        self.assertEqual(stranded, set())


class TestDamageBlocking(unittest.TestCase):
    """Test that damage polygons block edges."""

    def test_high_damage_blocks_edge(self):
        """Edge fully inside a damage polygon should be blocked."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100, cost=100)

        # Damage polygon covering the edge
        damage = [shapely_box(-0.01, -0.01, 0.01, 0.01)]
        blocked = assign_edge_costs(G, damage)

        self.assertIn((0, 1, 0), blocked)
        self.assertTrue(G[0][1][0].get("blocked"))

    def test_no_damage_no_block(self):
        """Edge outside damage polygon should not be blocked."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100, cost=100)

        # Damage polygon far away
        damage = [shapely_box(1.0, 1.0, 1.01, 1.01)]
        blocked = assign_edge_costs(G, damage)

        self.assertEqual(len(blocked), 0)
        self.assertFalse(G[0][1][0].get("blocked"))

    def test_partial_damage_inflates_cost(self):
        """Edge partially in damage should have inflated cost but not blocked."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.010, y=0.0)  # longer edge
        G.add_edge(0, 1, 0, length=1000, cost=1000)

        # Damage covering roughly half the edge
        damage = [shapely_box(-0.001, -0.001, 0.004, 0.001)]
        blocked = assign_edge_costs(G, damage)

        # Should not be blocked (damage_frac < 0.5)
        self.assertNotIn((0, 1, 0), blocked)
        # But cost should be inflated
        self.assertGreater(G[0][1][0]["cost"], 1000)


class TestDiversityFiltering(unittest.TestCase):
    """Test route overlap filtering."""

    def test_identical_paths_filtered(self):
        """Two identical edge sets should have 100% overlap."""
        path_a = [0, 1, 2, 3]
        path_b = [0, 1, 2, 3]
        es_a = _edge_set(path_a)
        es_b = _edge_set(path_b)
        self.assertAlmostEqual(_overlap_fraction(es_a, es_b), 1.0)

    def test_disjoint_paths_no_overlap(self):
        """Completely different paths should have 0% overlap."""
        es_a = _edge_set([0, 1, 2])
        es_b = _edge_set([10, 11, 12])
        self.assertAlmostEqual(_overlap_fraction(es_a, es_b), 0.0)

    def test_partial_overlap(self):
        """Half-shared path should have ~50% overlap."""
        es_a = _edge_set([0, 1, 2, 3])  # edges: (0,1), (1,2), (2,3)
        es_b = _edge_set([0, 1, 4, 5])  # edges: (0,1), (1,4), (4,5)
        overlap = _overlap_fraction(es_a, es_b)
        # Only edge (0,1) is shared → 1/3 ≈ 0.333
        self.assertAlmostEqual(overlap, 1.0 / 3.0, places=2)

    def test_overlap_uses_shorter_route_as_denominator(self):
        """Overlap = |A ∩ B| / min(|A|, |B|), symmetric in A and B."""
        es_short = _edge_set([0, 1, 2])           # 2 edges
        es_long = _edge_set([0, 1, 2, 3, 4, 5])   # 5 edges, contains the short route
        self.assertAlmostEqual(_overlap_fraction(es_short, es_long), 1.0)
        self.assertAlmostEqual(_overlap_fraction(es_long, es_short), 1.0)


def _route_with_spurs():
    """Route 0-1-2-3-4 along y=0, a loop 1-5-6-3 north of it, and spurs.

    Spur 10 hangs off route node 2 (a wrong turn), spur 7 hangs off loop
    node 5 (not on the route), and spur 11 is a stub lying almost exactly on
    top of route segment 2-3 (like a sidewalk mapped beside a street).
    """
    G = nx.MultiDiGraph()
    pts = {
        0: (0.000, 0.0), 1: (0.001, 0.0), 2: (0.002, 0.0), 3: (0.003, 0.0), 4: (0.004, 0.0),
        5: (0.001, 0.002), 6: (0.003, 0.002), 7: (0.001, 0.003),
        10: (0.002, -0.001), 11: (0.0025, 0.00001),
    }
    for n, (x, y) in pts.items():
        G.add_node(n, x=x, y=y)
    for u, v in [(0, 1), (1, 2), (2, 3), (3, 4), (1, 5), (5, 6), (6, 3), (5, 7), (2, 10), (2, 11)]:
        G.add_edge(u, v, 0, length=100, cost=100)
        G.add_edge(v, u, 0, length=100, cost=100)
    route_path = [0, 1, 2, 3, 4]
    route_line = [[pts[n][0], pts[n][1]] for n in route_path]
    return G, route_path, route_line


class TestRouteDeadEnds(unittest.TestCase):
    """Dead ends are marked only where they branch off a route, never on it."""

    def _terminus_nodes(self, fc, G):
        coords = {(G.nodes[n]["x"], G.nodes[n]["y"]): n for n in G.nodes}
        return {
            coords[tuple(f["geometry"]["coordinates"])]
            for f in fc["features"] if f["properties"]["kind"] == "dead_end_terminus"
        }

    def test_only_spurs_off_the_route_are_marked(self):
        G, path, line = _route_with_spurs()
        dead = prune_dead_ends(G, {0, 4})
        fc = route_dead_ends(G, dead, [path], [line])
        marked = self._terminus_nodes(fc, G)
        self.assertIn(10, marked)       # wrong turn off the route
        self.assertNotIn(7, marked)     # dead end elsewhere on the map

    def test_spur_lying_on_route_is_never_marked(self):
        G, path, line = _route_with_spurs()
        dead = prune_dead_ends(G, {0, 4})
        fc = route_dead_ends(G, dead, [path], [line])
        self.assertNotIn(11, self._terminus_nodes(fc, G))

    def test_route_nodes_and_edges_are_never_dead_ends(self):
        G, path, line = _route_with_spurs()
        # Even if pruning output (wrongly) contained route edges, they are rejected.
        bogus = [(3, 2), (4, 3), (1, 0)]
        fc = route_dead_ends(G, bogus, [path], [line])
        self.assertEqual(fc["features"], [])

    def test_dead_end_geometry_never_overlaps_route(self):
        from shapely.geometry import LineString, shape
        from evacuation_engine import ROUTE_CLEARANCE_M
        G, path, line = _route_with_spurs()
        dead = prune_dead_ends(G, {0, 4})
        fc = route_dead_ends(G, dead, [path], [line])
        route = LineString(line)
        self.assertTrue(fc["features"])
        for f in fc["features"]:
            gap_m = shape(f["geometry"]).distance(route) * 111320.0
            self.assertGreaterEqual(gap_m, ROUTE_CLEARANCE_M * 0.99)

    def test_compute_routes_returns_route_dead_ends_without_overlap(self):
        import unittest.mock
        from shapely.geometry import LineString, shape
        G, _path, _line = _route_with_spurs()
        shelters = [{"name": "S1", "lat": 0.0, "lon": 0.004, "type": "shelter"}]
        calls = {"n": 0}

        def mock_nearest(G, X, Y):
            calls["n"] += 1
            return 0 if calls["n"] == 1 else 4

        with unittest.mock.patch("osmnx.nearest_nodes", side_effect=mock_nearest):
            result = compute_routes(G, 0.0, 0.0, shelters, "walk", [])

        self.assertTrue(result["routes"])
        self.assertIn(10, self._terminus_nodes(result["dead_ends"], G))
        for r in result["routes"]:
            route = LineString(r["geojson"]["geometry"]["coordinates"])
            for f in result["dead_ends"]["features"]:
                self.assertFalse(shape(f["geometry"]).intersects(route))


class TestDamageCutNodes(unittest.TestCase):
    """The stranded map overlay shows only areas that damage cut off."""

    def _chain(self):
        # shelter 0 - 1 - 2 - 3, plus fragment 99 never connected
        G = nx.MultiDiGraph()
        for i in range(4):
            G.add_node(i, x=i * 0.001, y=0.0)
        G.add_node(99, x=0.01, y=0.01)
        for i in range(3):
            G.add_edge(i, i + 1, 0, length=100)
            G.add_edge(i + 1, i, 0, length=100)
        return G

    def test_damage_cut_is_shown_but_fragments_are_not(self):
        from evacuation_engine import damage_cut_nodes
        G = self._chain()
        # Damage polygon covering edge 1-2 (both directions)
        blocked = assign_edge_costs(G, [shapely_box(0.0012, -0.001, 0.0018, 0.001)])
        cut = damage_cut_nodes(G, blocked, {0})
        self.assertEqual(cut, {2, 3})

    def test_access_no_roads_are_not_shown_as_stranded(self):
        from evacuation_engine import damage_cut_nodes
        G = self._chain()
        for u, v in [(1, 2), (2, 1)]:
            G.edges[u, v, 0]["access"] = "no"
        blocked = assign_edge_costs(G, [])
        self.assertEqual(damage_cut_nodes(G, blocked, {0}), set())


class TestFewerThanThree(unittest.TestCase):
    """Test that the engine handles fewer than 3 valid routes."""

    def test_single_route(self):
        """Linear graph with one shelter should return 1 route."""
        G = nx.MultiDiGraph()
        for i in range(5):
            G.add_node(i, x=float(i) * 0.001, y=0.0)
        for i in range(4):
            G.add_edge(i, i + 1, 0, length=100, cost=100)
            G.add_edge(i + 1, i, 0, length=100, cost=100)

        shelters = [{"name": "S1", "lat": 0.0, "lon": 0.004, "type": "shelter", "node": 4}]

        # Mock osmnx.nearest_nodes: first call (start snap) returns 0,
        # second call (shelter snap) returns 4
        import unittest.mock
        call_count = {"n": 0}
        def mock_nearest(G, X, Y):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0   # start node
            return 4       # shelter node (single value)

        with unittest.mock.patch("osmnx.nearest_nodes", side_effect=mock_nearest):
            result = compute_routes(G, 0.0, 0.0, shelters, "walk", [])

        self.assertLessEqual(len(result["routes"]), 3)
        self.assertGreaterEqual(len(result["routes"]), 1)

    def test_no_shelter_returns_warning(self):
        """No shelters should return empty routes with a warning."""
        G = _make_grid_graph(2, 2)

        import unittest.mock
        with unittest.mock.patch("osmnx.nearest_nodes", return_value=0):
            result = compute_routes(G, 0.0, 0.0, [], "walk", [])

        self.assertEqual(len(result["routes"]), 0)
        self.assertTrue(len(result["warnings"]) > 0)


class TestShelterEnrichment(unittest.TestCase):
    """Test shelter name enrichment and metadata extraction."""

    def test_snap_shelters_enriches_generic_name_with_street(self):
        """Generic shelter name should be enriched with street name from graph."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100, cost=100, name="Main Street")

        shelters = [{"name": "Shelter", "lat": 0.0, "lon": 0.0, "type": "shelter"}]

        import unittest.mock
        with unittest.mock.patch("osmnx.nearest_nodes", return_value=[0]):
            snapped = snap_shelters_to_graph(G, shelters)

        self.assertEqual(len(snapped), 1)
        self.assertEqual(snapped[0]["name"], "Shelter on Main Street")
        self.assertEqual(snapped[0]["street"], "Main Street")

    def test_snap_shelters_preserves_specific_name(self):
        """Pre-named shelter should retain its specific name."""
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100, cost=100, name="Main Street")

        shelters = [{"name": "Briar Woods High School Shelter", "lat": 0.0, "lon": 0.0, "type": "school"}]

        import unittest.mock
        with unittest.mock.patch("osmnx.nearest_nodes", return_value=[0]):
            snapped = snap_shelters_to_graph(G, shelters)

        self.assertEqual(snapped[0]["name"], "Briar Woods High School Shelter")
        self.assertEqual(snapped[0]["street"], "Main Street")

    def test_metadata_extraction_with_operator_and_address(self):
        """OSM tags with operator and address should build descriptive name."""
        from shelters import _extract_shelter_metadata

        # Tag set 1: operator and street
        tags = {
            "amenity": "shelter",
            "operator": "Red Cross",
            "addr:street": "5th Avenue",
            "addr:housenumber": "42",
        }
        name, stype, street = _extract_shelter_metadata(tags)
        self.assertIn("Red Cross", name)
        self.assertIn("42 5th Avenue", name)
        self.assertEqual(street, "5th Avenue")

        # Tag set 2: transit shelter
        tags2 = {"amenity": "shelter", "shelter_type": "public_transport"}
        name2, stype2, street2 = _extract_shelter_metadata(tags2)
        self.assertEqual(name2, "Transit Shelter")
        self.assertEqual(stype2, "public_transport")


if __name__ == "__main__":
    unittest.main()
