"""Tests for official damage data (Copernicus EMS live API + imported UNOSAT files).

Run with: python manage.py test test_official_damage
"""

import json
import tempfile
import unittest.mock
from datetime import date
from pathlib import Path

import networkx as nx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from shapely.geometry import LineString, Point, box, mapping

import official_damage as od
from evacuation_engine import assign_edge_costs
from finder.models import DamageDataset, DamageFeature


def _write_geojson(tmp: Path, name: str, features: list[dict], crs: str | None = None) -> Path:
    fc = {"type": "FeatureCollection", "features": features}
    if crs:
        fc["crs"] = {"type": "name", "properties": {"name": crs}}
    path = tmp / name
    path.write_text(json.dumps(fc), encoding="utf-8")
    return path


def _feature(geom, **props) -> dict:
    return {"type": "Feature", "geometry": mapping(geom), "properties": props}


class NormalizeGradeTests(TestCase):
    def test_copernicus_text_grades(self):
        self.assertEqual(od.normalize_grade("Destroyed"), "destroyed")
        self.assertEqual(od.normalize_grade("Damaged"), "damaged")
        self.assertEqual(od.normalize_grade("Possibly damaged"), "possibly_damaged")
        self.assertEqual(od.normalize_grade("No visible damage"), "none")
        self.assertIsNone(od.normalize_grade("Not Analysed"))

    def test_older_copernicus_and_unosat_wording(self):
        self.assertEqual(od.normalize_grade("Completely Destroyed"), "destroyed")
        self.assertEqual(od.normalize_grade("Highly Damaged"), "damaged")
        self.assertEqual(od.normalize_grade("Moderate Damage"), "damaged")
        self.assertEqual(od.normalize_grade("Severe Damage"), "damaged")
        self.assertEqual(od.normalize_grade("Negligible to slight damage"), "none")

    def test_unosat_codes(self):
        self.assertEqual(od.normalize_grade(1), "destroyed")
        self.assertEqual(od.normalize_grade(14), "damaged")
        self.assertEqual(od.normalize_grade(6), "none")
        self.assertIsNone(od.normalize_grade(0))      # not analysed
        self.assertIsNone(od.normalize_grade(99))     # unknown code: skipped, not guessed
        self.assertIsNone(od.normalize_grade(float("nan")))


class ImportTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unosat_grid_in_web_mercator(self):
        """UNOSAT RDA-style 500 m cells with numeric codes across several columns."""
        import geopandas as gpd

        cells = [box(4_030_000 + i * 500, 6_430_000, 4_030_500 + i * 500, 6_430_500) for i in range(4)]
        gdf = gpd.GeoDataFrame(
            {"Main_Damag": [0, 0, 0, 0], "Main_Dam_4": [14, 6, 0, 1]}, geometry=cells, crs=3857
        )
        path = self.tmp / "rda.gpkg"
        gdf.to_file(path)

        ds = DamageDataset.objects.create(name="UNOSAT test", imagery_date=date(2022, 3, 23))
        summary = od.import_damage_file(path, ds)
        ds.refresh_from_db()

        self.assertEqual(summary["analysed"], 3)                 # codes 14, 6, 1
        self.assertEqual(summary["not_analysed_or_unknown"], 1)  # code 0
        self.assertEqual(ds.feature_count, 2)                    # 14 + 1 (6 = no visible damage)
        grades = sorted(DamageFeature.objects.values_list("grade", flat=True))
        self.assertEqual(grades, ["damaged", "destroyed"])
        self.assertTrue(all(k == "area" for k in DamageFeature.objects.values_list("kind", flat=True)))
        # Extent is in WGS84 and covers the three analysed cells
        self.assertTrue(36.0 < ds.west < ds.east < 36.3)
        self.assertTrue(49.9 < ds.south < ds.north < 50.0)

    def test_copernicus_style_file_skips_railways(self):
        feats = [
            _feature(LineString([(0, 0), (0.001, 0)]), damage_gra="Destroyed",
                     obj_type="211-Highways, Streets and Roads", name="Main St"),
            _feature(LineString([(0, 0.001), (0.001, 0.001)]), damage_gra="Damaged",
                     obj_type="212-Railways"),
            _feature(Point(0.0005, 0.0005).buffer(0.00005), damage_gra="Possibly damaged",
                     obj_type="11-Residential Buildings"),
            _feature(Point(0.0002, 0.0002), damage_gra="No visible damage"),
        ]
        path = _write_geojson(self.tmp, "cems.geojson", feats)
        ds = DamageDataset.objects.create(name="CEMS test", source="copernicus")
        od.import_damage_file(path, ds)
        kinds = set(DamageFeature.objects.values_list("kind", "grade"))
        self.assertEqual(kinds, {("road", "destroyed"), ("building", "possibly_damaged")})
        self.assertEqual(DamageFeature.objects.get(kind="road").label, "Main St")

    def test_file_without_damage_column_is_rejected(self):
        path = _write_geojson(self.tmp, "x.geojson", [_feature(Point(0, 0), foo="bar")])
        ds = DamageDataset.objects.create(name="bad")
        with self.assertRaises(ValueError):
            od.import_damage_file(path, ds)

    def test_default_grade_for_files_without_damage_column(self):
        """E.g. UNOSAT El Fasher: every polygon is a destroyed structure, no class column."""
        feats = [_feature(Point(0.001 * i, 0).buffer(0.00004), Date="2024-05-18") for i in range(3)]
        path = _write_geojson(self.tmp, "ef.geojson", feats)
        ds = DamageDataset.objects.create(name="El Fasher")
        summary = od.import_damage_file(path, ds, default_grade="destroyed")
        self.assertEqual(summary["damaged_features"], 3)
        self.assertEqual(set(DamageFeature.objects.values_list("grade", "kind")), {("destroyed", "building")})

    def test_shapefile_inside_a_zip_subfolder(self):
        import zipfile
        import geopandas as gpd

        folder = self.tmp / "Inner_DA"
        folder.mkdir()
        gpd.GeoDataFrame({"Main_Dam_4": [14]}, geometry=[box(36.2, 50.0, 36.207, 50.0045)], crs=4326) \
            .to_file(folder / "Inner_DA.shp")
        zpath = self.tmp / "inner.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            for f in folder.iterdir():
                z.write(f, f"Inner_DA/{f.name}")
        ds = DamageDataset.objects.create(name="zipped")
        self.assertEqual(od.import_damage_file(zpath, ds)["damaged_features"], 1)


class DbLookupTests(TestCase):
    def _dataset(self, active=True):
        ds = DamageDataset.objects.create(name="DS", active=active, west=0, south=0, east=1, north=1)
        DamageFeature.objects.create(
            dataset=ds, kind="building", grade="destroyed",
            geometry=json.dumps(mapping(Point(0.5, 0.5))), west=0.5, south=0.5, east=0.5, north=0.5,
        )
        return ds

    def test_only_active_overlapping_datasets(self):
        self._dataset(active=True)
        self._dataset(active=False)
        with unittest.mock.patch.object(od, "_copernicus_damage", return_value=([], [], False)):
            inside = od.official_damage_for_bbox((0.4, 0.4, 0.6, 0.6))
            outside = od.official_damage_for_bbox((5, 5, 6, 6))
        self.assertTrue(inside["assessed"])
        self.assertEqual(len(inside["features"]), 1)
        self.assertEqual(len(inside["sources"]), 1)
        self.assertFalse(outside["assessed"])
        self.assertEqual(outside["features"], [])

    def test_database_error_is_reported_not_fatal(self):
        """A missing table (unapplied migration) must not break routing."""
        from django.db import OperationalError

        with unittest.mock.patch.object(od, "_db_damage", side_effect=OperationalError("no such table")), \
             unittest.mock.patch.object(od, "_copernicus_damage", return_value=([], [], False)):
            result = od.official_damage_for_bbox((0.4, 0.4, 0.6, 0.6))
        self.assertFalse(result["assessed"])
        self.assertEqual(result["features"], [])
        self.assertTrue(any("migrate" in n for n in result["notes"]))

    def test_copernicus_outage_is_reported_not_fatal(self):
        self._dataset()
        with unittest.mock.patch.object(od, "_copernicus_damage", side_effect=OSError("down")):
            result = od.official_damage_for_bbox((0.4, 0.4, 0.6, 0.6))
        self.assertTrue(result["assessed"])
        self.assertEqual(len(result["features"]), 1)
        self.assertTrue(result["notes"])


class CopernicusTests(TestCase):
    """Copernicus EMS lookup with the HTTP layer mocked."""

    ZONE = (10.0, 20.0, 10.02, 20.02)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        layer = {"features": [
            _feature(LineString([(10.005, 20.01), (10.015, 20.01)]), damage_gra="Destroyed",
                     obj_type="211-Highways, Streets and Roads"),
            _feature(Point(10.01, 20.005), damage_gra="Damaged", obj_type="11-Residential Buildings"),
            _feature(Point(10.01, 20.006), damage_gra="No visible damage"),
            _feature(Point(11.5, 21.5), damage_gra="Destroyed"),  # outside the zone
        ]}

        def product(layer_names, delivery):
            return {
                "type": "GRA", "feasible": True, "version": {"deliveryTime": delivery},
                "imageDate": "2026-06-27",
                "layers": [{"name": f"X/{n}_v1_VT", "json": f"https://example.test/{n}.json"} for n in layer_names],
            }

        self.responses = {
            "public-activations-info": {"results": [
                {"code": "EMSR1", "centroid": "POINT (10.01 20.01)"},
                {"code": "EMSR2", "centroid": "POINT (10.01 20.01)"},   # sensitive
                {"code": "EMSR3", "centroid": "POINT (100 -40)"},       # far away
            ]},
            "code=EMSR1": {"results": [{
                "name": "Earthquake", "sensitive": False, "eventTime": "2026-06-26T00:00:00",
                "aois": [{
                    "name": "Town", "extent": "POLYGON ((9.9 19.9, 10.1 19.9, 10.1 20.1, 9.9 20.1, 9.9 19.9))",
                    "products": [
                        product(["builtUpA", "transportationL"], "2026-06-28T00:00:00"),
                        product(["old_builtUpA"], "2026-06-27T00:00:00"),
                    ],
                }],
            }]},
            "code=EMSR2": {"results": [{"name": "Secret", "sensitive": True, "aois": []}]},
            "builtUpA.json": layer,
            "transportationL.json": layer,
        }

    def _fake_http(self, url, timeout=None):
        for key, value in self.responses.items():
            if key in url:
                return value
        raise AssertionError(f"unexpected URL {url}")

    def test_graded_features_and_coverage(self):
        with unittest.mock.patch.object(od, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(od, "_http_json", side_effect=self._fake_http) as http:
            feats, sources, covered = od._copernicus_damage(self.ZONE)
        self.assertTrue(covered)
        self.assertEqual(len(sources), 1)
        self.assertIn("EMSR1", sources[0]["name"])
        self.assertEqual(sources[0]["date"], "2026-06-27")
        kinds = sorted((f["kind"], f["grade"]) for f in feats)
        # builtUp layer: point building (damaged) + its line counted as a "road" by geometry;
        # transportation layer: destroyed road. "No visible damage" and out-of-zone dropped.
        self.assertIn(("road", "destroyed"), kinds)
        self.assertIn(("building", "damaged"), kinds)
        self.assertNotIn("none", {g for _, g in kinds})
        called = " ".join(str(c.args[0]) for c in http.call_args_list)
        self.assertNotIn("EMSR3", called)          # far activations are not fetched
        self.assertNotIn("old_builtUpA", called)   # only the latest grading product

    def test_responses_are_cached(self):
        with unittest.mock.patch.object(od, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(od, "_http_json", side_effect=self._fake_http) as http:
            od._copernicus_damage(self.ZONE)
            first = http.call_count
            od._copernicus_damage(self.ZONE)
        self.assertEqual(http.call_count, first)


class RoutingWithGradesTests(TestCase):
    def _edge_graph(self):
        G = nx.MultiDiGraph()
        G.add_node(0, x=0.0, y=0.0)
        G.add_node(1, x=0.001, y=0.0)
        G.add_edge(0, 1, 0, length=100.0)
        return G

    def _grades(self, grade):
        feats = [{"kind": "road", "grade": grade, "geom": LineString([(0, 0), (0.001, 0)])}]
        return od.road_grade_polygons(feats, (-0.001, -0.001, 0.002, 0.001))

    def test_destroyed_road_is_blocked(self):
        G = self._edge_graph()
        blocked = assign_edge_costs(G, [], self._grades("destroyed"))
        self.assertIn((0, 1, 0), blocked)

    def test_damaged_road_costs_much_more_than_possibly_damaged(self):
        G1, G2, G3 = self._edge_graph(), self._edge_graph(), self._edge_graph()
        assign_edge_costs(G1, [], self._grades("damaged"))
        assign_edge_costs(G2, [], self._grades("possibly_damaged"))
        assign_edge_costs(G3, [], {})
        damaged, possibly, clean = (g.edges[0, 1, 0]["cost"] for g in (G1, G2, G3))
        self.assertAlmostEqual(clean, 100.0)
        self.assertAlmostEqual(damaged, 600.0, delta=1)    # 1 + 5 x 100% of the edge
        self.assertAlmostEqual(possibly, 200.0, delta=1)   # 1 + 1 x 100%
        self.assertFalse(G1.edges[0, 1, 0]["blocked"])

    def test_buildings_never_change_routing(self):
        G = self._edge_graph()
        feats = [{"kind": "building", "grade": "destroyed", "geom": Point(0.0005, 0.0)}]
        self.assertEqual(od.road_grade_polygons(feats, (-0.001, -0.001, 0.002, 0.001)), {})
        assign_edge_costs(G, [], od.road_grade_polygons(feats, (-0.001, -0.001, 0.002, 0.001)))
        self.assertAlmostEqual(G.edges[0, 1, 0]["cost"], 100.0)


class AnnotateRoutesTests(TestCase):
    def test_nearby_buildings_and_crossed_areas_become_warnings(self):
        route = {"geojson": {"geometry": {"coordinates": [[0, 0], [0.002, 0]]}}}
        result = {"routes": [route]}
        feats = [
            {"kind": "building", "grade": "destroyed", "geom": Point(0.001, 0.0001)},   # ~11 m away
            {"kind": "building", "grade": "destroyed", "geom": Point(0.001, 0.002)},    # ~220 m away
            {"kind": "area", "grade": "damaged", "geom": box(0.0005, -0.001, 0.0015, 0.001)},
            {"kind": "road", "grade": "destroyed", "geom": LineString([(0, 0), (0.002, 0)])},
        ]
        od.annotate_routes(result, feats)
        self.assertEqual(route["damage_nearby"], {"destroyed_building": 1, "damaged_area": 1})
        self.assertEqual(
            route["damage_warnings"],
            ["Passes 1 destroyed building", "Crosses 1 area with damaged buildings"],
        )


class RoutesApiTests(TestCase):
    """End-to-end /api/routes with network-facing pieces mocked."""

    def test_routes_include_official_damage(self):
        user = get_user_model().objects.create_user(username="u", password="pw-123456")
        client = Client()
        client.force_login(user)

        G = nx.MultiDiGraph()
        for i in range(3):
            G.add_node(i, x=i * 0.001, y=0.0)
        for i in range(2):
            G.add_edge(i, i + 1, 0, length=100.0)
            G.add_edge(i + 1, i, 0, length=100.0)
        shelters = [{"name": "S", "lat": 0.0, "lon": 0.002, "type": "shelter"}]
        official = {
            "features": [{"kind": "building", "grade": "destroyed", "geom": Point(0.001, 0.0001),
                          "label": "", "source": "UNOSAT test", "date": "2022-03-23"}],
            "sources": [{"name": "UNOSAT test", "provider": "UNOSAT", "date": "2022-03-23",
                         "attribution": "UNOSAT", "url": ""}],
            "assessed": True,
            "notes": [],
        }
        calls = {"n": 0}

        def nearest(G, X, Y):
            calls["n"] += 1
            return 0 if calls["n"] == 1 else [2]

        with unittest.mock.patch("shelters.find_shelters", return_value=(shelters, False)), \
             unittest.mock.patch("evacuation_engine.get_cached_graph", return_value=G), \
             unittest.mock.patch("official_damage.official_damage_for_bbox", return_value=official), \
             unittest.mock.patch("osmnx.nearest_nodes", side_effect=nearest):
            resp = client.post(
                "/api/routes",
                json.dumps({"start": {"lat": 0.0, "lon": 0.0}, "bbox": [-0.01, -0.01, 0.01, 0.01]}),
                content_type="application/json",
            )
        data = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(data["damage_sources"][0]["name"], "UNOSAT test")
        self.assertTrue(data["routes"])
        self.assertIn("Passes 1 destroyed building", data["routes"][0]["damage_warnings"])
        self.assertTrue(data["routes"][0]["damage_assessed"])
