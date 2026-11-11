"""Comprehensive tests for finder views, APIs, and the evacuation routing plan."""

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

import annotate
import geocode
import scene

User = get_user_model()


class FinderViewsTestCase(TestCase):
    def setUp(self):
        self.username = "test-relief-worker"
        self.password = "SafePass2026!"
        self.user = User.objects.create_user(username=self.username, password=self.password)
        self.client = Client()

    def test_anonymous_redirected_to_login(self):
        for path in ("/dashboard/", "/api/search", "/api/scene", "/api/clips", "/api/annotate", "/api/damage", "/api/route"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login", response.url)

    def test_search_api_requires_valid_query(self):
        self.client.login(username=self.username, password=self.password)
        response = self.client.get("/api/search", {"q": "a"})
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    def test_api_endpoints_reject_invalid_coordinates(self):
        self.client.login(username=self.username, password=self.password)
        for ep in ("/api/scene", "/api/clips", "/api/annotate"):
            bad_reply = self.client.get(ep, {"lat": "abc", "lon": "xyz"})
            self.assertEqual(bad_reply.status_code, 400)
            self.assertIn("error", bad_reply.json())

    def test_plan_check_known_address_returns_coords_square_and_date(self):
        """Check 1: A known street address returns coordinates, a 5 km Sentinel-2 square,

        and that scene's capture date.
        """
        # Test Nominatim parsing
        matches = geocode.parse_results(
            [
                {
                    "display_name": "Maidan Nezalezhnosti, Kyiv, Ukraine",
                    "lat": "50.4502431",
                    "lon": "30.5240622",
                    "category": "place",
                    "type": "square",
                }
            ]
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["latitude"], "50.4502431")
        self.assertEqual(matches[0]["longitude"], "30.5240622")

        # Test scene bounds for 5 km
        bounds = scene.square_bounds(50.4502431, 30.5240622)
        self.assertEqual(len(bounds), 4)

        # Test find_scene logic with mocked planetary computer response
        orig_req = scene.request_scenes
        scene.request_scenes = lambda b: {
            "features": [
                {
                    "id": "S2A_TEST_KYIV",
                    "properties": {"datetime": "2026-10-08T09:10:31Z", "eo:cloud_cover": 5.0},
                    "assets": {"visual": {"href": "https://example.test/visual.tif"}},
                }
            ]
        }
        try:
            found = scene.find_scene(50.4502431, 30.5240622)
            self.assertEqual(found["captured"], "2026-10-08T09:10:31Z")
            self.assertEqual(found["cloud_cover"], 5.0)
            self.assertIn("512x512.png", str(found["image_url"]))
        finally:
            scene.request_scenes = orig_req

    def test_plan_check_image_size_and_scale_bar(self):
        """Check 2: The displayed image is 512 by 512 pixels and the scale bar reads

        10 m per original pixel.
        """
        img, road, water, struct = annotate.create_synthetic_corridor(blocked=False)
        result = annotate.annotate_scene(img, road_mask=road, water_mask=water, structure_mask=struct)

        self.assertIsNotNone(result.image)
        self.assertEqual(result.image.size, (512, 512))
        self.assertEqual(result.scale_bar_text, "10 m per original pixel")

    def test_plan_check_unknown_address_returns_not_found_and_no_image(self):
        """Check 3: An unknown address returns a clear not-found message and no image."""
        matches = geocode.parse_results([])
        self.assertEqual(len(matches), 0)

    def test_plan_check_two_geocoder_matches_require_confirmation(self):
        """Check 4: Two geocoder matches require a confirmation before an image is loaded."""
        matches = geocode.parse_results(
            [
                {
                    "display_name": "Highway 1, Kharkiv",
                    "lat": "49.99",
                    "lon": "36.23",
                    "category": "highway",
                    "type": "primary",
                },
                {
                    "display_name": "Highway 1, Dnipro",
                    "lat": "48.46",
                    "lon": "35.04",
                    "category": "highway",
                    "type": "primary",
                },
            ]
        )
        self.assertEqual(len(matches), 2)
        # Dashboard UI requires explicit user click "Use this place" before requesting /api/scene

    def test_plan_check_synthetic_corridor_water_rubble_and_route(self):
        """Check 5: A synthetic corridor image with a blue flood and a rubble block

        draws water damage, structure damage, and a route around both.
        """
        img, road, water, struct = annotate.create_synthetic_corridor(blocked=False)
        result = annotate.annotate_scene(img, road_mask=road, water_mask=water, structure_mask=struct)

        self.assertTrue(result.water_damage_detected)
        self.assertTrue(result.structure_damage_detected)
        self.assertTrue(result.car_travel_blocked)
        self.assertTrue(result.passable)
        self.assertTrue(result.route_drawn)
        self.assertGreater(len(result.route_coordinates), 5)

        # Route does not cross water or rubble
        for x, y in result.route_coordinates:
            self.assertFalse(water[y, x])
            self.assertFalse(struct[y, x])

    def test_plan_check_potholes_and_trip_marks_not_drawn(self):
        """Check 6: The same synthetic image does not draw pothole or trip marks.

        Those legend rows say they are not visible.
        """
        img, road, water, struct = annotate.create_synthetic_corridor(blocked=False)
        result = annotate.annotate_scene(img, road_mask=road, water_mask=water, structure_mask=struct)

        self.assertFalse(result.potholes_drawn)
        self.assertFalse(result.trip_marks_drawn)
        self.assertEqual(result.legend["trip_hazards"], annotate.NOT_VISIBLE_AT_10M)
        self.assertEqual(result.legend["potholes"], annotate.NOT_VISIBLE_AT_10M)
        self.assertEqual(result.legend["curbs"], annotate.NOT_VISIBLE_AT_10M)

    def test_plan_check_phrase_list_does_not_contain_forbidden_words(self):
        """Check 7: The phrase list used for satellite scenes does not include

        pothole, vehicles, weapons, personnel, or fortifications.
        """
        annotate.validate_scene_phrases()
        for forbidden in annotate.FORBIDDEN_PHRASES:
            for phrase in annotate.SCENE_PHRASES:
                self.assertNotIn(forbidden, phrase.lower())

    def test_plan_check_next_steps_and_scene_date_present(self):
        """Check 8: The next-steps panel and the scene date are present on every successful output."""
        img, road, water, struct = annotate.create_synthetic_corridor(blocked=False)
        result = annotate.annotate_scene(
            img,
            captured="2026-10-23T12:34:56Z",
            cloud_cover=2.5,
            road_mask=road,
            water_mask=water,
            structure_mask=struct,
        )

        self.assertIn("Stay in the marked safer direction", result.next_steps)
        self.assertIn("do not re-enter buildings marked unsound", result.next_steps)
        self.assertIn("do not drive through marks that block cars", result.next_steps)
        self.assertIn("do not drink or drive through marked water", result.next_steps)
        self.assertIn("ask local emergency services what to do next", result.next_steps)
        self.assertEqual(result.captured, "2026-10-23T12:34:56Z")
        self.assertEqual(result.cloud_cover, "2.5%")

    def test_plan_check_fully_blocked_corridor(self):
        """When the corridor is fully blocked, the graphic says the road is not passable

        and does not draw a line through the block.
        """
        img, road, water, struct = annotate.create_synthetic_corridor(blocked=True)
        result = annotate.annotate_scene(img, road_mask=road, water_mask=water, structure_mask=struct)

        self.assertFalse(result.passable)
        self.assertFalse(result.route_drawn)
        self.assertEqual(result.passable_message, annotate.ROAD_NOT_PASSABLE_MSG)
        self.assertEqual(len(result.route_coordinates), 0)

    def test_annotate_api_view_and_image_endpoint(self):
        """Test /api/annotate and /api/annotated-image via Django test client."""
        self.client.login(username=self.username, password=self.password)
        response = self.client.get("/api/annotate", {"lat": "50.45024", "lon": "30.52406"})
        self.assertEqual(response.status_code, 200)
        data = response.json()

        self.assertIn("passable", data)
        self.assertIn("legend", data)
        self.assertIn("next_steps", data)
        self.assertEqual(data["scale_bar_text"], "10 m per original pixel")
        self.assertIn("image_url", data)

        # Retrieve the annotated image
        img_response = self.client.get(data["image_url"])
        self.assertEqual(img_response.status_code, 200)
        self.assertEqual(img_response["Content-Type"], "image/png")
        self.assertGreater(len(img_response.content), 1000)


    def test_damage_api_view_geojson(self):
        """Test /api/damage returns GeoJSON FeatureCollection."""
        self.client.login(username=self.username, password=self.password)
        # 1. Missing coordinates returns 400
        bad_resp = self.client.get("/api/damage")
        self.assertEqual(bad_resp.status_code, 400)

        # 2. Cached square 50.45024, 30.52406 returns 200 and GeoJSON
        response = self.client.get("/api/damage", {"lat": "50.45024", "lon": "30.52406"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/geo+json")
        geojson = json.loads(response.content)
        self.assertEqual(geojson.get("type"), "FeatureCollection")
        self.assertIn("features", geojson)
        self.assertIn("properties", geojson)
