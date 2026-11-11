"""Parser checks for Nominatim results. Does not call the network."""

from geocode import GeocodeError, parse_results


def test_parse_keeps_valid_matches() -> None:
    matches = parse_results(
        [
            {
                "display_name": "10 Main Street, Springfield, USA",
                "lat": "39.7817000",
                "lon": "-89.6501000",
                "category": "place",
                "type": "house",
            },
            {"display_name": "missing coords"},
            {"lat": "10", "lon": "20"},
        ]
    )
    assert len(matches) == 1
    assert matches[0]["display_name"].startswith("10 Main Street")
    assert matches[0]["latitude"] == "39.7817000"
    assert matches[0]["kind"] == "place house"


def test_parse_rejects_non_list() -> None:
    try:
        parse_results({"error": "no"})
    except GeocodeError:
        return
    raise AssertionError("expected GeocodeError")


if __name__ == "__main__":
    test_parse_keeps_valid_matches()
    test_parse_rejects_non_list()
    print("geocode parser checks passed")
