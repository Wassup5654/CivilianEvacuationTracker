"""Privacy guarantees: locations stay out of URLs, logs and (optionally) disk.

Run with: python manage.py test test_privacy
"""

import io
import json
import logging
import tempfile
import unittest.mock
from datetime import date
from pathlib import Path

import networkx as nx
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from finder.logging_filters import RedactQueryStringFilter, strip_query

LAT, LON = "50.4501234", "30.5234567"


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user(username="p", password="pw-123456"))


class PostBodyTests(LoggedInTestCase):
    """The map's endpoints accept locations in a JSON POST body."""

    def test_address_search_by_post(self):
        with unittest.mock.patch("finder.views.search_address", return_value=[{"display_name": "X"}]) as m:
            resp = self.client.post("/api/search", json.dumps({"q": "Sumska Street 1, Kharkiv"}),
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        m.assert_called_once_with("Sumska Street 1, Kharkiv")

    def test_street_view_by_post(self):
        resp = self.client.post("/api/streetview", json.dumps({"lat": 50.45, "lon": 30.52}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("50.45", resp.json()["embed_url"])

    def test_zone_endpoints_validate_post_bodies(self):
        for url in ("/api/context", "/api/suggest-shelters"):
            resp = self.client.post(url, json.dumps({"bbox": [1, 2, 3]}), content_type="application/json")
            self.assertEqual(resp.status_code, 400, url)
            resp = self.client.post(url, "not json", content_type="application/json")
            self.assertEqual(resp.status_code, 400, url)

    def test_bbox_as_list_or_string(self):
        from finder.views import _parse_bbox
        self.assertEqual(_parse_bbox([1, 2, 3, 4]), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(_parse_bbox("1,2,3,4"), (1.0, 2.0, 3.0, 4.0))
        with self.assertRaises(ValueError):
            _parse_bbox([1, 2])

    def test_null_coordinates_are_rejected_not_500(self):
        resp = self.client.post("/api/streetview", json.dumps({"lat": None, "lon": None}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)


class LogRedactionTests(TestCase):
    def test_query_strings_are_redacted(self):
        self.assertEqual(strip_query(f'"GET /api/scene?lat={LAT}&lon={LON} HTTP/1.1" 200'),
                         '"GET /api/scene?[redacted] HTTP/1.1" 200')
        record = logging.LogRecord("django.server", logging.INFO, __file__, 1,
                                   '"%s" %s %s', (f"GET /api/route?lat={LAT}&lon={LON} HTTP/1.1", "200", "12"), None)
        RedactQueryStringFilter().filter(record)
        self.assertNotIn(LAT, record.getMessage())
        self.assertIn("[redacted]", record.getMessage())

    def test_request_log_handlers_use_the_filter(self):
        handlers = logging.getLogger("django.server").handlers
        self.assertTrue(handlers)
        self.assertTrue(all(any(isinstance(f, RedactQueryStringFilter) for f in h.filters) for h in handlers))

    def test_road_network_downloads_never_log_coordinates(self):
        import evacuation_engine as ee

        resp = unittest.mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        resp.read.return_value = b"<osm/>"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        ee.logger.addHandler(handler)
        try:
            with unittest.mock.patch("urllib.request.urlopen", return_value=resp):
                ee._fetch_url_with_backoff(
                    f"https://api.openstreetmap.org/api/0.6/map?bbox={LON},{LAT},30.53,50.46",
                    headers={}, bbox_info=(float(LON), float(LAT), 30.53, 50.46))
        finally:
            ee.logger.removeHandler(handler)
        text = stream.getvalue()
        self.assertIn("HTTP 200", text)
        self.assertNotIn(LAT, text)
        self.assertNotIn(LON, text)


@override_settings(CACHE_AREAS_ON_DISK=False)
class NoDiskCacheTests(TestCase):
    """With CACHE_AREAS_ON_DISK = False, no looked-up area is written to disk."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_shelters_stay_in_memory(self):
        import shelters
        with unittest.mock.patch.object(shelters, "SHELTERS_DIR", self.tmp), \
             unittest.mock.patch.object(shelters, "query_osm_shelters",
                                        return_value=[{"name": "S", "lat": 50.45, "lon": 30.52}]):
            shelters._shelter_cache.clear()
            self.assertEqual(len(shelters._cached_osm_shelters((30.51, 50.44, 30.53, 50.46))), 1)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_road_graphs_stay_in_memory(self):
        import evacuation_engine as ee
        G = nx.MultiDiGraph()
        G.add_node(1, x=30.52, y=50.45)
        ee._graph_cache.clear()
        with unittest.mock.patch.object(ee, "GRAPHS_DIR", self.tmp), \
             unittest.mock.patch("osmnx.graph_from_bbox", return_value=G):
            self.assertIs(ee.get_cached_graph((30.51, 50.44, 30.53, 50.46), "walk"), G)
        ee._graph_cache.clear()
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_radar_layers_stay_in_memory_and_are_reused(self):
        import radar_damage as rd

        def fake(bbox, layer, region_name, periods):
            return {"features": [], "sources": [{"name": layer}], "notes": [], "stats": {}}

        rd._mem_cache.clear()
        with unittest.mock.patch.object(rd, "CACHE_DIR", self.tmp), \
             unittest.mock.patch.object(rd, "_compute_layer", side_effect=fake) as comp:
            rd.estimate_for_bbox((30.51, 50.44, 30.53, 50.46), today=date(2026, 11, 19))
            rd.estimate_for_bbox((30.51, 50.44, 30.53, 50.46), today=date(2026, 11, 19))
        self.assertEqual(comp.call_count, 2)  # two layers, computed once each
        self.assertEqual(list(self.tmp.iterdir()), [])
        rd._mem_cache.clear()
