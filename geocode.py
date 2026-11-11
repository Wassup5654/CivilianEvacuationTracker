"""Turn a typed street address into Nominatim matches. No satellite fetch."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "civilian-evacuation/0.1 (local address lookup for evacuation routing)"
RESULT_LIMIT = 5
TIMEOUT_SEC = 20.0


class GeocodeError(RuntimeError):
    """The address lookup could not be completed."""


def parse_results(payload: object) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        raise GeocodeError("Address lookup returned an unexpected response.")
    matches: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        lat = str(item.get("lat") or "").strip()
        lon = str(item.get("lon") or "").strip()
        if not name or not lat or not lon:
            continue
        try:
            latitude = float(lat)
            longitude = float(lon)
        except ValueError:
            continue
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            continue
        category = str(item.get("category") or item.get("class") or "").strip()
        kind = str(item.get("type") or "").strip()
        matches.append(
            {
                "display_name": name,
                "latitude": f"{latitude:.7f}",
                "longitude": f"{longitude:.7f}",
                "kind": " ".join(part for part in (category, kind) if part),
            }
        )
    return matches


def search_address(query: str) -> list[dict[str, str]]:
    text = " ".join(query.split())
    if len(text) < 3:
        raise GeocodeError("Type a street address with at least a few characters.")
    params = urllib.parse.urlencode(
        {
            "q": text,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": RESULT_LIMIT,
        }
    )
    request = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GeocodeError(f"Address lookup failed ({exc.code}).") from exc
    except urllib.error.URLError as exc:
        raise GeocodeError("Address lookup could not reach the map service.") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, TimeoutError) as exc:
        raise GeocodeError("Address lookup returned an unreadable response.") from exc
    return parse_results(payload)
