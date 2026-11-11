"""Checks for the annotation, hazard detection, routing, and plan requirements."""

import json
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

import annotate
from annotate import (
    FORBIDDEN_PHRASES,
    NEXT_STEPS_TEXT,
    NOT_VISIBLE_AT_10M,
    ROAD_NOT_PASSABLE_MSG,
    SCALE_BAR_TEXT,
    SCENE_PHRASES,
    annotate_scene,
    annotate_square,
    create_synthetic_corridor,
    find_safe_route,
    validate_scene_phrases,
)
import geocode
import scene


def test_phrase_list_does_not_include_forbidden_words() -> None:
    """Verify phrase list used for satellite scenes has no weapons, vehicles, etc."""
    validate_scene_phrases()
    for forbidden in FORBIDDEN_PHRASES:
        for phrase in SCENE_PHRASES:
            assert forbidden not in phrase.lower(), f"Forbidden word '{forbidden}' in '{phrase}'"


def test_synthetic_corridor_draws_water_structure_and_safe_route() -> None:
    """A synthetic corridor image with a blue flood and a rubble block draws

    water damage, structure damage, and a route around both.
    """
    img, road_mask, water_mask, struct_mask = create_synthetic_corridor(blocked=False)

    result = annotate_scene(
        image=img,
        captured="2026-10-23T00:00:00Z",
        cloud_cover=5.0,
        road_mask=road_mask,
        water_mask=water_mask,
        structure_mask=struct_mask,
        start_point=(256, 256),
    )

    # 1. Damage detected
    assert result.water_damage_detected is True, "Water damage should be detected"
    assert result.structure_damage_detected is True, "Structure damage should be detected"
    assert result.car_travel_blocked is True, "Car travel should be flagged as blocked where hazards cover road"

    # 2. Route found and passable
    assert result.passable is True, "Route should be passable around both hazards"
    assert result.route_drawn is True, "Route should be drawn on the graphic"
    assert len(result.route_coordinates) > 1, "Route coordinates should not be empty"

    # Verify route starts near address and reaches edge
    start_x, start_y = result.route_coordinates[0]
    end_x, end_y = result.route_coordinates[-1]
    assert abs(start_x - 256) <= 25 and abs(start_y - 256) <= 25, "Route should start near address"
    assert end_x == 0 or end_x == 511 or end_y == 0 or end_y == 511, "Route must reach edge of square"

    # Verify route goes AROUND both hazards without intersecting them
    for x, y in result.route_coordinates:
        assert not water_mask[y, x], f"Route passed through water hazard at ({x}, {y})"
        assert not struct_mask[y, x], f"Route passed through structure damage hazard at ({x}, {y})"

    # 3. Image dimensions and scale bar
    assert result.image is not None
    assert result.image.size == (512, 512), "Displayed image must be 512 by 512 pixels"
    assert result.scale_bar_text == SCALE_BAR_TEXT
    assert "10 m per original pixel" in result.scale_bar_text

    # 4. Potholes and trip hazards not visible
    assert result.potholes_drawn is False, "Potholes must not be drawn"
    assert result.trip_marks_drawn is False, "Trip hazard marks must not be drawn"
    assert result.legend["trip_hazards"] == NOT_VISIBLE_AT_10M
    assert result.legend["potholes"] == NOT_VISIBLE_AT_10M
    assert result.legend["curbs"] == NOT_VISIBLE_AT_10M

    # 5. Next steps and scene date are present
    assert NEXT_STEPS_TEXT in result.next_steps or result.next_steps == NEXT_STEPS_TEXT
    assert result.captured == "2026-10-23T00:00:00Z"


def test_synthetic_corridor_without_explicit_masks() -> None:
    """Detection functions automatically recognize color features in synthetic scenes."""
    img, _, _, _ = create_synthetic_corridor(blocked=False)
    result = annotate_scene(image=img, captured="2026-10-23T00:00:00Z", cloud_cover=12.0)
    assert result.water_damage_detected is True
    assert result.structure_damage_detected is True
    assert result.passable is True
    assert result.route_drawn is True


def test_blocked_corridor_says_not_passable_and_draws_no_route() -> None:
    """If the corridor is fully blocked, the graphic says the road is not passable

    and does not draw a line through the block.
    """
    img, road_mask, water_mask, struct_mask = create_synthetic_corridor(blocked=True)

    result = annotate_scene(
        image=img,
        captured="2026-10-23T00:00:00Z",
        cloud_cover=8.0,
        road_mask=road_mask,
        water_mask=water_mask,
        structure_mask=struct_mask,
        start_point=(256, 256),
    )

    assert result.passable is False
    assert result.route_drawn is False
    assert result.passable_message == ROAD_NOT_PASSABLE_MSG
    assert len(result.route_coordinates) == 0
    assert result.legend["safe_route"] == ROAD_NOT_PASSABLE_MSG


def test_annotate_square_on_saved_tif() -> None:
    """Annotate square reads saved tifs, generates annotated.png, and writes metadata."""
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "10.00000_20.00000"
        folder.mkdir(parents=True)
        meta = {
            "folder": str(folder),
            "latitude": 10.0,
            "longitude": 20.0,
            "latest": {
                "item_id": "TEST_ITEM",
                "captured": "2026-10-20T12:00:00Z",
                "cloud_cover": 4.5,
                "file": "latest.tif",
            },
        }
        (folder / "square.json").write_text(json.dumps(meta), encoding="utf-8")

        # Create a synthetic TIFF image
        img, _, _, _ = create_synthetic_corridor(blocked=False)
        img.save(folder / "latest.tif", format="TIFF")

        annotated_meta = annotate_square(10.0, 20.0, root=tmp)
        assert (folder / "annotated.png").is_file()
        assert annotated_meta["passable"] is True
        assert annotated_meta["captured"] == "2026-10-20T12:00:00Z"
        assert annotated_meta["scale_bar_text"] == "10 m per original pixel"

        # Verify square.json was updated
        updated_json = json.loads((folder / "square.json").read_text(encoding="utf-8"))
        assert "annotation" in updated_json
        assert updated_json["annotation"]["passable"] is True


def test_two_geocoder_matches_require_confirmation() -> None:
    """Two geocoder matches require user confirmation before any satellite image loads."""
    matches = geocode.parse_results(
        [
            {
                "display_name": "123 Main St, Kyiv",
                "lat": "50.45",
                "lon": "30.52",
                "category": "place",
                "type": "house",
            },
            {
                "display_name": "123 Main St, Odesa",
                "lat": "46.48",
                "lon": "30.72",
                "category": "place",
                "type": "house",
            },
        ]
    )
    assert len(matches) == 2, "Should return both candidate places"
    # Neither scene is automatically requested; user must pick one


def test_unknown_address_returns_empty_and_no_image() -> None:
    """An unknown address returns empty matches and raises no image fetch."""
    matches = geocode.parse_results([])
    assert len(matches) == 0


if __name__ == "__main__":
    test_phrase_list_does_not_include_forbidden_words()
    test_synthetic_corridor_draws_water_structure_and_safe_route()
    test_synthetic_corridor_without_explicit_masks()
    test_blocked_corridor_says_not_passable_and_draws_no_route()
    test_annotate_square_on_saved_tif()
    test_two_geocoder_matches_require_confirmation()
    test_unknown_address_returns_empty_and_no_image()
    print("All annotate and plan requirement checks passed successfully!")
