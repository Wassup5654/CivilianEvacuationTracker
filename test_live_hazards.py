"""Live hazards (NASA FIRMS + GDACS) and offline support (service worker, manifest).

Run with: python manage.py test test_live_hazards
No network: the feeds are replaced with small samples.
"""

import json
import unittest.mock
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

import live_hazards as lh

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
ZONE = (36.22, 49.98, 36.24, 49.995)     # ~1.4 km box in Kharkiv

FIRMS_CSV = """latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight
49.9880,36.2300,330.1,0.4,0.4,2026-11-20,0930,N20,nominal,2.0NRT,290.0,4.2,D
49.9881,36.2302,331.0,0.4,0.4,2026-11-20,0840,N21,nominal,2.0NRT,290.0,3.9,D
49.9900,36.2350,310.0,0.4,0.4,2026-11-20,1000,N20,low,2.0NRT,290.0,0.4,D
49.9885,36.2310,320.0,0.4,0.4,2026-11-17,1000,N20,high,2.0NRT,290.0,9.0,D
50.5000,36.9000,320.0,0.4,0.4,2026-11-20,1000,N20,high,2.0NRT,290.0,9.0,D
bad,row
"""


def gdacs_feature(etype, level, lon, lat, name, current="true"):
    return {"type": "Feature", "bbox": [lon, lat, lon, lat],
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"eventtype": etype, "eventid": 1, "alertlevel": level, "name": name,
                           "iscurrent": current, "fromdate": "2026-11-19T00:00:00", "todate": "2026-11-20T00:00:00",
                           "severitydata": {"severitytext": "Magnitude 6.1M"},
                           "url": {"report": "https://www.gdacs.org/report.aspx?eventid=1"}}}


class FirmsTests(TestCase):
    def setUp(self):
        self.fires = lh.parse_firms_csv(FIRMS_CSV, "NOAA-20 VIIRS")

    def test_parses_rows_and_skips_bad_ones(self):
        self.assertEqual(len(self.fires), 5)
        self.assertEqual(self.fires[0]["time"], datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(self.fires[2]["confidence"], "low")

    def test_zone_filter_drops_old_and_far_detections_and_merges_duplicates(self):
        hits = lh.fires_in_bbox(self.fires, ZONE, now=NOW)
        # 3-day-old and 60 km-away detections dropped; the two satellite passes merged
        self.assertEqual(len(hits), 2)
        merged = [h for h in hits if h["detections"] == 2]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["time"].hour, 9)     # keeps the newest pass

    def test_geojson_reports_age(self):
        gj = lh.fires_geojson(lh.fires_in_bbox(self.fires, ZONE, now=NOW), NOW)
        props = [f["properties"] for f in gj["features"]]
        self.assertIn("2 h ago", [p["age_text"] for p in props])
        self.assertTrue(all(p["hours_ago"] >= 0 for p in props))


class GdacsTests(TestCase):
    def test_radius_depends_on_type_and_level(self):
        alerts = lh.parse_gdacs({"features": [
            gdacs_feature("EQ", "Green", 37.0, 50.0, "Small quake 60 km away"),
            gdacs_feature("EQ", "Green", 40.0, 50.0, "Small quake 270 km away"),
            gdacs_feature("EQ", "Orange", 40.0, 50.0, "Big quake 270 km away"),
            gdacs_feature("DR", "Green", 36.2, 50.0, "Green drought (too broad)"),
            gdacs_feature("TC", "Red", 36.3, 50.0, "Old cyclone", current="false"),
            gdacs_feature("WF", "Red", 36.2, 50.0, "Wildfire (FIRMS covers fires)"),
        ]})
        names = [a["name"] for a in lh.alerts_near(alerts, ZONE)]
        self.assertEqual(names, ["Big quake 270 km away", "Small quake 60 km away"])  # Orange sorts first


class RouteWarningTests(TestCase):
    def route(self, coords):
        return {"routes": [{"geojson": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords}}}]}

    def hazards(self):
        fires = lh.parse_firms_csv(FIRMS_CSV, "NOAA-20 VIIRS")
        return {"fires": lh.fires_geojson(lh.fires_in_bbox(fires, ZONE, now=NOW), NOW)}

    def test_warns_about_nearby_detection_but_keeps_route(self):
        result = self.route([[36.225, 49.988], [36.235, 49.988]])   # passes ~0 m from the merged fire
        before = json.dumps(result["routes"][0]["geojson"])
        lh.annotate_routes_with_hazards(result, self.hazards())
        r = result["routes"][0]
        self.assertEqual(json.dumps(r["geojson"]), before)            # warnings only, never rerouting
        self.assertEqual(len(r["hazard_warnings"]), 1)
        self.assertIn("Fire or heat detected", r["hazard_warnings"][0])
        self.assertIn("unverified", r["hazard_warnings"][0])

    def test_low_confidence_and_distant_detections_are_ignored(self):
        result = self.route([[36.200, 49.982], [36.205, 49.982]])   # ~1.8 km from the fires
        lh.annotate_routes_with_hazards(result, self.hazards())
        self.assertEqual(result["routes"][0]["hazard_warnings"], [])

    def test_point_segment_distance(self):
        d = lh._point_segment_m(50.0, 36.0, [36.0, 50.001], [36.01, 50.001])
        self.assertAlmostEqual(d, 111.3, delta=1.0)


@override_settings(LIVE_HAZARDS=True)
class HazardStateTests(TestCase):
    def setUp(self):
        self.saved = dict(lh._state)

    def tearDown(self):
        lh._state.clear()
        lh._state.update(self.saved)

    def test_pending_until_first_download_then_filtered(self):
        lh._state.update(fires=None, alerts=None, fetched_at=0.0, errors=[], refreshing=False)
        with unittest.mock.patch.object(lh, "_load_from_disk", return_value=False), \
             unittest.mock.patch.object(lh.threading, "Thread") as thread:
            h = lh.hazards_for_bbox(ZONE, now=NOW)
        self.assertTrue(h["pending"])
        thread.assert_called_once()                     # download started in the background
        fires = lh.parse_firms_csv(FIRMS_CSV, "NOAA-20 VIIRS")
        lh._state.update(fires=fires, alerts=[], fetched_at=lh.time.time())
        h = lh.hazards_for_bbox(ZONE, now=NOW)
        self.assertFalse(h["pending"])
        self.assertEqual(len(h["fires"]["features"]), 2)

    def test_failed_download_keeps_previous_data_and_says_so(self):
        fires = lh.parse_firms_csv(FIRMS_CSV, "NOAA-20 VIIRS")
        lh._state.update(fires=fires, alerts=[], fetched_at=0.0, errors=[], refreshing=False)
        with unittest.mock.patch.object(lh, "_fetch_all",
                                        return_value={"fires": [], "alerts": [], "errors": ["a", "b"]}):
            lh.refresh()
        self.assertEqual(len(lh._state["fires"]), len(fires))
        h = lh.hazards_for_bbox(ZONE, now=NOW)
        self.assertTrue(h["notes"])

    def test_feeds_are_global_downloads_with_no_location(self):
        urls = list(lh.FIRMS_FEEDS.values()) + [lh.GDACS_URL]
        for u in urls:
            self.assertNotIn("bbox", u.lower())
            self.assertNotIn("{lat", u)

    @override_settings(LIVE_HAZARDS=False)
    def test_disabled(self):
        self.assertFalse(lh.hazards_for_bbox(ZONE)["enabled"])


class ViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_user(username="h", password="pw-123456"))

    def test_hazards_endpoint(self):
        fake = {"enabled": True, "fires": {"type": "FeatureCollection", "features": []}, "alerts": [],
                "sources": [], "updated": None, "pending": False, "notes": []}
        with unittest.mock.patch("live_hazards.hazards_for_bbox", return_value=fake) as m:
            resp = self.client.post("/api/hazards", json.dumps({"bbox": list(ZONE)}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), fake)
        m.assert_called_once()

    def test_hazards_endpoint_validates_bbox(self):
        resp = self.client.post("/api/hazards", json.dumps({"bbox": [1, 2]}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_feed_outage_is_a_note_not_an_error(self):
        with unittest.mock.patch("live_hazards.hazards_for_bbox", side_effect=OSError("down")):
            resp = self.client.post("/api/hazards", json.dumps({"bbox": list(ZONE)}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["notes"])

    def test_hazards_require_login(self):
        resp = Client().post("/api/hazards", json.dumps({"bbox": list(ZONE)}), content_type="application/json")
        self.assertEqual(resp.status_code, 302)


class OfflineAppTests(TestCase):
    def test_service_worker_served_from_root(self):
        resp = Client().get("/sw.js")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/javascript")
        self.assertEqual(resp["Service-Worker-Allowed"], "/")
        body = resp.content.decode()
        self.assertIn("evac-tiles", body)
        self.assertIn('req.method !== "GET"', body)      # API POSTs are never cached

    def test_manifest(self):
        resp = Client().get("/manifest.webmanifest")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["start_url"], "/dashboard/")
        self.assertTrue(data["icons"])

    def test_dashboard_links_manifest_and_offline_controls(self):
        c = Client()
        c.force_login(get_user_model().objects.create_user(username="o", password="pw-123456"))
        html = c.get("/dashboard/").content.decode()
        for needle in ('rel="manifest"', "pwa.js", 'id="save-offline"', 'id="saved-areas-box"',
                       'id="clear-offline"', 'id="live-hazards"', 'id="toggle-hazards"'):
            self.assertIn(needle, html)
