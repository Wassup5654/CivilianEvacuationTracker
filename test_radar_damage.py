"""Tests for the Sentinel-1 radar damage estimate (radar_damage.py).

Run with: python manage.py test test_radar_damage
"""

import tempfile
import unittest.mock
from datetime import date
from pathlib import Path

import numpy as np
from django.test import TestCase
from shapely.geometry import LineString, box

import official_damage as od
import radar_damage as rd

RNG = np.random.default_rng(42)


def _stack(n, h=40, w=40, mean=-8.0, sd=1.0):
    """(n, 2, h, w) VV/VH backscatter in dB."""
    return RNG.normal(mean, sd, size=(n, 2, h, w))


class StatisticsTests(TestCase):
    def test_welch_t_detects_local_change_only(self):
        base, rec = _stack(20)[:, 0], _stack(10)[:, 0]
        rec[:, 10:20, 10:20] += 6.0  # strong local change
        t = np.abs(rd.welch_t(base, rec))
        self.assertGreater(np.median(t[10:20, 10:20]), 8)
        self.assertLess(np.median(t[25:, 25:]), 1.5)

    def test_zone_wide_shift_is_removed(self):
        """A calibration offset across the whole zone must not look like damage."""
        base, rec = _stack(20)[:, 0], _stack(10)[:, 0] + 3.0
        everywhere = np.ones(base.shape[1:], dtype=bool)
        raw = np.abs(rd.welch_t(base, rec))
        corrected = np.abs(rd.welch_t(base, rec, reference=everywhere))
        self.assertGreater(np.median(raw), 5)
        self.assertLess(np.median(corrected), 1.5)

    def test_change_map_and_features(self):
        base, rec = _stack(20), _stack(10)
        rec[:, :, 5:15, 5:15] += 6.0     # built-up block that changed
        rec[:, :, 25:35, 25:35] += 6.0   # farmland that changed (not built-up)
        built = np.zeros((40, 40), dtype=bool)
        built[:20, :20] = True
        built[22:38, 2:18] = True        # unchanged built-up area
        tmap = rd.change_map([(base, rec)], reference=built)
        feats, stats = rd.change_to_features(tmap, built, (0.0, 0.0, 0.004, 0.004))
        self.assertEqual(len(feats), 1)
        f = feats[0]
        self.assertTrue(f["estimated"])
        self.assertEqual(f["kind"], "area")
        self.assertIn(f["grade"], ("damaged", "possibly_damaged"))
        # The polygon sits over the changed built-up block (north-west quarter), not the farmland
        minx, miny, maxx, maxy = f["geom"].bounds
        self.assertLess(maxx, 0.002)
        self.assertGreater(miny, 0.002)

    def test_systematic_change_is_withheld(self):
        tmap = np.full((40, 40), 6.0)
        built = np.ones((40, 40), dtype=bool)
        feats, stats = rd.change_to_features(tmap, built, (0, 0, 0.004, 0.004))
        self.assertEqual(feats, [])
        self.assertIn("systematic", stats["reason"])

    def test_little_built_up_area_is_withheld(self):
        feats, stats = rd.change_to_features(np.full((40, 40), 6.0), np.zeros((40, 40), bool), (0, 0, 1, 1))
        self.assertEqual(feats, [])
        self.assertIn("built-up", stats["reason"])


class ModelTests(TestCase):
    """The shipped trained model and its fallbacks."""

    def _tracks(self):
        base, rec = _stack(30), _stack(8)
        rec[:, :, 5:15, 5:15] += 6.0
        return [(base, rec)]

    def test_shipped_model_matches_app_features(self):
        model, meta = rd.load_model()
        self.assertIsNotNone(model, "models/radar_damage_model.joblib failed to load")
        self.assertEqual(meta["features"], rd.FEATURE_NAMES)
        self.assertIn("possibly_damaged", meta["thresholds"])

    def test_model_scores_changed_block_higher(self):
        built = np.ones((40, 40), dtype=bool)
        sc = rd.score_map(self._tracks(), built)
        self.assertEqual(sc["method"], "trained model")
        self.assertGreater(sc["score"][7:13, 7:13].mean(), sc["score"][25:35, 25:35].mean())

    def test_fallback_to_tuned_statistic_then_t_test(self):
        built = np.ones((40, 40), dtype=bool)
        meta = {"fallback_thresholds": {"possibly_damaged": 2.0, "damaged": 4.0}}
        with unittest.mock.patch.object(rd, "load_model", return_value=(None, meta)):
            sc = rd.score_map(self._tracks(), built)
        self.assertEqual(sc["method"], "tuned statistic")
        self.assertGreater(sc["score"][7:13, 7:13].mean(), 4 * sc["score"][25:35, 25:35].mean())
        with unittest.mock.patch.object(rd, "load_model", return_value=(None, None)):
            self.assertEqual(rd.score_map(self._tracks(), built)["method"], "t-test")

    def test_feature_mismatch_disables_model(self):
        rd._model_cache.clear()
        try:
            with unittest.mock.patch.object(rd, "FEATURE_NAMES", ["something_else"]):
                model, meta = rd.load_model()
            self.assertIsNone(model)
        finally:
            rd._model_cache.clear()


class PeriodTests(TestCase):
    def test_war_start_layer(self):
        p = rd.layer_periods("war_start", date(2022, 2, 24), date(2026, 11, 19))
        self.assertEqual(p, (date(2021, 2, 24), date(2022, 2, 23), date(2022, 2, 24), date(2022, 5, 25)))

    def test_recent_layer_compares_last_90_days_with_the_year_before(self):
        p = rd.layer_periods("recent", date(2022, 2, 24), date(2026, 11, 19))
        self.assertEqual(p, (date(2025, 8, 21), date(2026, 8, 20), date(2026, 8, 21), date(2026, 11, 19)))

    def test_young_war_has_no_recent_layer_and_short_war_start(self):
        ref, today = date(2026, 9, 1), date(2026, 11, 19)
        self.assertIsNone(rd.layer_periods("recent", ref, today))
        self.assertEqual(rd.layer_periods("war_start", ref, today)[3], today)
        self.assertIsNone(rd.layer_periods("war_start", date(2026, 11, 1), today))


class RegionTests(TestCase):
    """Uses the regions seeded by migration 0005."""

    def _zone(self, lat, lon, d=0.01):
        return (lon - d, lat - d, lon + d, lat + d)

    def test_seeded_regions(self):
        self.assertEqual(rd.match_region(self._zone(50.45, 30.52)).name, "Ukraine")
        self.assertEqual(rd.match_region(self._zone(31.50, 34.45)).name, "Gaza Strip")
        self.assertEqual(rd.match_region(self._zone(35.69, 51.39)).name, "Iran")
        self.assertIsNone(rd.match_region(self._zone(48.85, 2.35)))  # Paris

    def test_smallest_box_wins_where_boxes_overlap(self):
        # Herat lies inside both the Iran and Afghanistan boxes
        self.assertEqual(rd.match_region(self._zone(34.35, 62.20)).name, "Afghanistan")


class EstimateTests(TestCase):
    ZONE = (30.51, 50.44, 30.53, 50.46)  # Kyiv

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.feature = {"kind": "area", "grade": "possibly_damaged", "geom": box(30.515, 50.445, 30.517, 50.447),
                        "label": "Radar change, t ≈ 3.4", "estimated": True}

    def _fake_layer(self, bbox, layer, region_name, periods):
        f = dict(self.feature, layer=layer, source=rd.SOURCE_LABEL, date="x")
        return {"features": [f], "sources": [{"name": layer}], "notes": [], "stats": {}}

    def test_both_layers_and_cache(self):
        with unittest.mock.patch.object(rd, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(rd, "_compute_layer", side_effect=self._fake_layer) as comp:
            first = rd.estimate_for_bbox(self.ZONE, today=date(2026, 11, 19))
            second = rd.estimate_for_bbox(self.ZONE, today=date(2026, 11, 19))
        self.assertEqual(first["region"], "Ukraine")
        self.assertEqual({f["layer"] for f in first["features"]}, {"war_start", "recent"})
        self.assertEqual(comp.call_count, 2)               # cached on the second call
        self.assertEqual(len(second["features"]), 2)
        self.assertTrue(second["features"][0]["geom"].equals(self.feature["geom"]))

    def test_background_mode_returns_immediately_then_caches(self):
        import time as _time
        with unittest.mock.patch.object(rd, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(rd, "_compute_layer", side_effect=self._fake_layer):
            first = rd.estimate_for_bbox(self.ZONE, today=date(2026, 11, 19), wait=False)
            self.assertTrue(first["pending"])
            self.assertEqual(first["features"], [])
            for _ in range(100):  # background threads finish quickly with the fake compute
                if not rd._running:
                    break
                _time.sleep(0.02)
            second = rd.estimate_for_bbox(self.ZONE, today=date(2026, 11, 19), wait=False)
        self.assertFalse(second["pending"])
        self.assertEqual(len(second["features"]), 2)

    def test_outside_conflict_regions_is_empty(self):
        with unittest.mock.patch.object(rd, "_compute_layer") as comp:
            out = rd.estimate_for_bbox((2.34, 48.84, 2.36, 48.86))
        self.assertEqual(out["features"], [])
        comp.assert_not_called()

    def test_missing_data_becomes_a_note(self):
        with unittest.mock.patch.object(rd, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(rd, "_compute_layer", side_effect=rd.RadarUnavailable("no scenes")):
            out = rd.estimate_for_bbox(self.ZONE, today=date(2026, 11, 19))
        self.assertEqual(out["features"], [])
        self.assertEqual(len(out["notes"]), 2)
        self.assertIn("no scenes", out["notes"][0])


class EstimatesNeverRouteTests(TestCase):
    def test_estimates_only_warn(self):
        est = {"kind": "road", "grade": "destroyed", "geom": LineString([(0, 0), (0.002, 0)]),
               "estimated": True, "layer": "recent"}
        self.assertEqual(od.road_grade_polygons([est], (-0.001, -0.001, 0.003, 0.001)), {})

        area = {"kind": "area", "grade": "damaged", "geom": box(0.0005, -0.0005, 0.0015, 0.0005),
                "estimated": True, "layer": "war_start"}
        route = {"geojson": {"geometry": {"coordinates": [[0, 0], [0.002, 0]]}}}
        od.annotate_routes({"routes": [route]}, [area])
        self.assertEqual(
            route["damage_warnings"],
            ["Passes 1 area of estimated damage from the war's first months (radar, low confidence)"],
        )
