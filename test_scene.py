"""Checks for the 5 km scene lookup. Does not call the network."""

import math

from scene import SceneError, choose_scene, find_scene, preview_url, square_bounds


def _feature(item_id: str, captured: str, cloud: float, href: str | None = "https://example.test/visual.tif") -> dict:
    assets = {"visual": {"href": href}} if href else {}
    return {
        "id": item_id,
        "properties": {"datetime": captured, "eo:cloud_cover": cloud},
        "assets": assets,
    }


def test_square_is_five_kilometers() -> None:
    west, south, east, north = square_bounds(10.0, 20.0)
    assert abs(((south + north) / 2) - 10.0) < 1e-9
    assert abs(((west + east) / 2) - 20.0) < 1e-9
    half_lat = 2_500 / 111_320
    half_lon = half_lat / math.cos(math.radians(10.0))
    assert abs((north - south) - (2 * half_lat)) < 1e-9
    assert abs((east - west) - (2 * half_lon)) < 1e-9
    assert (east - west) > (north - south)


def test_square_rejects_bad_points() -> None:
    for latitude, longitude in ((91.0, 0.0), (0.0, 181.0), (89.9, 0.0), (0.0, 179.99)):
        try:
            square_bounds(latitude, longitude)
        except SceneError:
            continue
        raise AssertionError(f"expected SceneError for {latitude}, {longitude}")


def test_choose_scene_keeps_the_newest_clear_scene() -> None:
    chosen = choose_scene(
        {
            "features": [
                _feature("older", "2026-09-01T00:00:00Z", 4.0),
                _feature("cloudy", "2026-09-25T00:00:00Z", 40.0),
                _feature("no-picture", "2026-09-26T00:00:00Z", 1.0, href=None),
                _feature("newer", "2026-09-20T08:00:00Z", 12.5),
            ]
        }
    )
    assert chosen["item_id"] == "newer"
    assert chosen["captured"] == "2026-09-20T08:00:00Z"
    assert chosen["cloud_cover"] == 12.5


def test_choose_scene_rejects_an_empty_search() -> None:
    try:
        choose_scene({"features": []})
    except SceneError as exc:
        assert "30 days" in str(exc)
        return
    raise AssertionError("expected SceneError")


def test_preview_url_uses_the_square() -> None:
    url = preview_url("ITEM123", (1.0, 2.0, 3.0, 4.0))
    assert url.startswith("https://planetarycomputer.microsoft.com/api/data/v1/item/bbox/")
    assert "/1.000000,2.000000,3.000000,4.000000/512x512.png?" in url
    assert "item=ITEM123" in url
    assert "collection=sentinel-2-l2a" in url


def test_find_scene_returns_the_dict_without_calling_the_service() -> None:
    import scene

    original = scene.request_scenes

    def fake_request(bounds: tuple[float, float, float, float]) -> dict:
        assert bounds == square_bounds(10.0, 20.0)
        return {"features": [_feature("ITEM123", "2026-09-20T08:00:00Z", 12.5)]}

    scene.request_scenes = fake_request
    try:
        result = find_scene(10.0, 20.0)
    finally:
        scene.request_scenes = original
    assert result["captured"] == "2026-09-20T08:00:00Z"
    assert result["cloud_cover"] == 12.5
    assert "ITEM123" in str(result["image_url"])


def _with_box(feature: dict, box: list[float]) -> dict:
    return {**feature, "bbox": box}


def test_covering_drops_scenes_that_miss_part_of_the_square() -> None:
    import scene

    bounds = (10.0, 20.0, 10.1, 20.1)
    payload = {
        "features": [
            _with_box(_feature("whole", "2026-09-20T08:00:00Z", 1.0), [9.0, 19.0, 11.0, 21.0]),
            _with_box(_feature("partial", "2026-09-21T08:00:00Z", 1.0), [10.05, 19.0, 11.0, 21.0]),
            _feature("no-box", "2026-09-22T08:00:00Z", 1.0),
        ]
    }
    kept = [f["id"] for f in scene._covering(payload, bounds)["features"]]
    assert kept == ["whole", "no-box"]


def _run_save(root, latest_features, earlier_features, calls) -> dict:
    import scene

    saved = (scene.request_scenes, scene.request_scenes_between, scene.download_clip)

    def fake_latest(bounds):
        return {"features": latest_features}

    def fake_between(bounds, start, end):
        calls["window"] = (start, end)
        return {"features": earlier_features}

    def fake_download(item_id, bounds, path):
        calls.setdefault("downloads", []).append(item_id)
        path.write_bytes(b"II*\x00" + b"0" * 20_000)

    scene.request_scenes = fake_latest
    scene.request_scenes_between = fake_between
    scene.download_clip = fake_download
    try:
        return scene.save_clips(10.0, 20.0, root)
    finally:
        scene.request_scenes, scene.request_scenes_between, scene.download_clip = saved


def test_save_clips_keeps_latest_and_an_earlier_scene() -> None:
    import tempfile
    from pathlib import Path

    calls: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_save(
            tmp,
            [_feature("NEW", "2026-09-20T08:00:00Z", 5.0)],
            [_feature("OLD", "2026-08-10T08:00:00Z", 2.0)],
            calls,
        )
        folder = Path(result["folder"])
        assert (folder / "latest.tif").is_file()
        assert (folder / "earlier.tif").is_file()
        assert (folder / "square.json").is_file()
        assert result["latest"]["captured"] == "2026-09-20T08:00:00Z"
        assert result["earlier"]["captured"] == "2026-08-10T08:00:00Z"
        assert result["earlier_note"] is None
        assert result["size"] == 500
        start, end = calls["window"]
        assert end.isoformat().startswith("2026-09-15T08:00:00")
        assert (end - start).days == 90
        assert calls["downloads"] == ["NEW", "OLD"]

        again = _run_save(
            tmp,
            [_feature("NEW", "2026-09-20T08:00:00Z", 5.0)],
            [_feature("OLD", "2026-08-10T08:00:00Z", 2.0)],
            calls,
        )
        assert again["latest"]["item_id"] == "NEW"
        assert calls["downloads"] == ["NEW", "OLD"], "same scenes must not download twice"


def test_save_clips_says_so_when_there_is_no_earlier_scene() -> None:
    import tempfile
    from pathlib import Path

    calls: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        _run_save(
            tmp,
            [_feature("NEW", "2026-09-20T08:00:00Z", 5.0)],
            [_feature("OLD", "2026-08-10T08:00:00Z", 2.0)],
            calls,
        )
        result = _run_save(tmp, [_feature("NEW", "2026-09-20T08:00:00Z", 5.0)], [], calls)
        folder = Path(result["folder"])
        assert result["earlier"] is None
        assert "earlier clear image" in result["earlier_note"]
        assert (folder / "latest.tif").is_file()
        assert not (folder / "earlier.tif").exists(), "a stale earlier clip must be removed"


def test_download_rejects_a_reply_that_is_not_a_tiff() -> None:
    import io
    import tempfile
    import urllib.request
    from pathlib import Path

    import scene

    class Fake(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    original = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: Fake(b"<html>" + b"x" * 20_000)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                scene.download_clip("ITEM", (1.0, 2.0, 3.0, 4.0), Path(tmp) / "latest.tif")
            except SceneError:
                assert not (Path(tmp) / "latest.tif").exists()
                return
            raise AssertionError("expected SceneError")
    finally:
        urllib.request.urlopen = original


if __name__ == "__main__":
    test_square_is_five_kilometers()
    test_square_rejects_bad_points()
    test_choose_scene_keeps_the_newest_clear_scene()
    test_choose_scene_rejects_an_empty_search()
    test_preview_url_uses_the_square()
    test_find_scene_returns_the_dict_without_calling_the_service()
    test_covering_drops_scenes_that_miss_part_of_the_square()
    test_save_clips_keeps_latest_and_an_earlier_scene()
    test_save_clips_says_so_when_there_is_no_earlier_scene()
    test_download_rejects_a_reply_that_is_not_a_tiff()
    print("scene checks passed")
