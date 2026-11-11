"""Homepage, accounts, and the dashboard address search."""

import json
from pathlib import Path

from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from annotate import AnnotationError, SQUARES_DIR, annotate_square
from geocode import GeocodeError, search_address
from routing import (
    DISCLAIMER_NOTE,
    NO_ROUTE_MSG,
    ROUTE_LABEL,
    find_evacuation_route,
    generate_svg_overlay,
    get_road_network_for_bounds,
)
from scene import SceneError, find_scene, save_clips, square_bounds

from .forms import LoginForm, RegisterForm


def home(request):
    return render(request, "finder/home.html")


class FinderLoginView(LoginView):
    template_name = "finder/login.html"
    authentication_form = LoginForm
    redirect_authenticated_user = True


def register(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("dashboard")
    return render(request, "finder/register.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("home")


@login_required
def dashboard(request):
    return render(request, "finder/dashboard.html")


@login_required
def search(request):
    query = request.GET.get("q", "")
    try:
        matches = search_address(query)
    except GeocodeError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"query": " ".join(query.split()), "matches": matches})


@login_required
def scene(request):
    try:
        latitude = float(request.GET.get("lat", ""))
        longitude = float(request.GET.get("lon", ""))
    except ValueError:
        return JsonResponse({"error": "Confirm a place before loading an image."}, status=400)
    try:
        found = find_scene(latitude, longitude)
    except SceneError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(found)


@login_required
def clips(request):
    try:
        latitude = float(request.GET.get("lat", ""))
        longitude = float(request.GET.get("lon", ""))
    except ValueError:
        return JsonResponse({"error": "Confirm a place before saving the pixels."}, status=400)
    try:
        before_param = request.GET.get("before")
        saved = save_clips(latitude, longitude, before=before_param)
    except SceneError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse(saved)


@login_required
def annotate_view(request):
    try:
        latitude = float(request.GET.get("lat", ""))
        longitude = float(request.GET.get("lon", ""))
    except ValueError:
        return JsonResponse({"error": "Confirm a place before annotating the scene."}, status=400)
    try:
        annotated_data = annotate_square(latitude, longitude)
    except (SceneError, AnnotationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    annotated_data["image_url"] = f"/api/annotated-image?lat={latitude:.5f}&lon={longitude:.5f}"
    return JsonResponse(annotated_data)


@login_required
def annotated_image_view(request):
    try:
        latitude = float(request.GET.get("lat", ""))
        longitude = float(request.GET.get("lon", ""))
    except ValueError:
        return JsonResponse({"error": "Invalid coordinates."}, status=400)
    img_path = SQUARES_DIR / f"{latitude:.5f}_{longitude:.5f}" / "annotated.png"
    if not img_path.is_file():
        return JsonResponse({"error": "Annotated image not found. Please run annotation first."}, status=404)
    return HttpResponse(img_path.read_bytes(), content_type="image/png")


@login_required
def route_view(request):
    try:
        latitude = float(request.GET.get("lat", ""))
        longitude = float(request.GET.get("lon", ""))
    except ValueError:
        return JsonResponse({"error": "Confirm a place before computing routing."}, status=400)

    hazards_param = request.GET.get("hazards", "")
    hazards = []
    if hazards_param:
        try:
            parsed = json.loads(hazards_param)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, (list, tuple)) and len(item) >= 3:
                        hazards.append((float(item[0]), float(item[1]), float(item[2])))
                    elif isinstance(item, dict) and "lat" in item and "lon" in item:
                        hazards.append((float(item["lat"]), float(item["lon"]), float(item.get("radius", 100.0))))
        except (json.JSONDecodeError, ValueError):
            pass

    try:
        bounds = square_bounds(latitude, longitude)
    except SceneError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    captured = "Unknown date"
    cloud_cover = "unknown"
    try:
        scene_info = find_scene(latitude, longitude)
        captured = str(scene_info.get("captured", "Unknown date"))
        cloud_cover = scene_info.get("cloud_cover", "unknown")
    except Exception:
        pass

    try:
        graph = get_road_network_for_bounds(bounds)
    except Exception as exc:
        return JsonResponse({"error": f"Road network service could not be reached: {exc}"}, status=502)

    # Load damage polygons for the square to block damaged roads and draw damage overlay
    folder = Path("data") / "squares" / f"{latitude:.5f}_{longitude:.5f}"
    geo_polygons = []
    pixel_polygons = []
    if folder.exists() and (folder / "latest.tif").exists():
        from damage import detect_damage_for_square
        try:
            gj, geo_polygons, pixel_polygons = detect_damage_for_square(folder, bounds)
            if gj.get("properties", {}).get("classification") == "damaged_building":
                geo_polygons = []  # Do not block roads using building labels.
        except Exception:
            pass

    route_points, message = find_evacuation_route(
        graph, latitude, longitude, bounds, hazards=hazards, damage_polygons=geo_polygons
    )
    svg_overlay = generate_svg_overlay(
        route_points,
        hazards,
        bounds,
        start_point=(latitude, longitude),
        damage_pixel_polygons=pixel_polygons,
    )

    return JsonResponse(
        {
            "route_found": route_points is not None,
            "message": message,
            "route_label": ROUTE_LABEL,
            "svg_overlay": svg_overlay,
            "captured": captured,
            "cloud_cover": cloud_cover,
            "disclaimer": DISCLAIMER_NOTE,
            "route_points_count": len(route_points) if route_points else 0,
        }
    )


@login_required
def damage_view(request):
    """Return GeoJSON damage polygons extracted from Sentinel-2 scene."""
    raw_lat = request.GET.get("lat")
    raw_lon = request.GET.get("lon")
    if raw_lat is None or raw_lon is None:
        return JsonResponse({"error": "Coordinates are required."}, status=400)

    try:
        latitude = float(raw_lat)
        longitude = float(raw_lon)
    except ValueError:
        return JsonResponse({"error": "Those coordinates are not valid numbers."}, status=400)

    try:
        bounds = square_bounds(latitude, longitude)
    except SceneError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    before_param = request.GET.get("before")
    threshold_param = request.GET.get("threshold")
    min_area_param = request.GET.get("min_area")

    threshold = float(threshold_param) if threshold_param else 0.40
    min_area = int(min_area_param) if min_area_param else 16

    folder = Path("data") / "squares" / f"{latitude:.5f}_{longitude:.5f}"
    if not (folder / "latest.tif").exists():
        try:
            save_clips(latitude, longitude, before=before_param)
        except Exception as exc:
            return JsonResponse({"error": f"Failed to retrieve satellite clips: {exc}"}, status=502)

    from damage import detect_damage_for_square

    try:
        geojson, _, _ = detect_damage_for_square(
            folder, bounds, threshold=threshold, min_area_px=min_area
        )
    except Exception as exc:
        return JsonResponse({"error": f"Damage analysis failed: {exc}"}, status=500)

    return HttpResponse(json.dumps(geojson), content_type="application/geo+json")



# ── New interactive-map endpoints ──
# These do NOT store user addresses or locations in any model or log.


def _get_damage_for_bbox(bbox: tuple[float, float, float, float]) -> tuple[dict, list, bool]:
    """Retrieve or compute damage polygons for bounding box.

    Returns (damage_geojson, geo_polygons, damage_assessed_bool).
    """
    damage_geojson = {"type": "FeatureCollection", "features": []}
    geo_polygons = []
    squares_dir = Path("data") / "squares"

    # 1. Check existing squares on disk
    found_existing = False
    road_damage_assessed = False
    if squares_dir.is_dir():
        from damage import detect_damage_for_square
        for folder in squares_dir.iterdir():
            if not folder.is_dir():
                continue
            sq_json = folder / "square.json"
            latest = folder / "latest.tif"
            if not latest.exists() or not sq_json.exists():
                continue
            try:
                meta = json.loads(sq_json.read_text(encoding="utf-8"))
                sq_bounds = tuple(meta.get("bounds", []))
                if len(sq_bounds) != 4:
                    continue
                # Overlap check
                if (sq_bounds[2] < bbox[0] or sq_bounds[0] > bbox[2] or
                        sq_bounds[3] < bbox[1] or sq_bounds[1] > bbox[3]):
                    continue
                gj, geo_polys, _ = detect_damage_for_square(folder, sq_bounds, threshold=0.25, min_area_px=10)
                if gj.get("properties", {}).get("classification") == "damaged_building":
                    geo_polys = []
                else:
                    road_damage_assessed = True
                if gj.get("features"):
                    damage_geojson["features"].extend(gj["features"])
                    geo_polygons.extend(geo_polys)
                found_existing = True
            except Exception:
                pass

    if found_existing:
        return damage_geojson, geo_polygons, road_damage_assessed

    # 2. If no existing square, attempt to download clips for center of bbox
    center_lat = (bbox[1] + bbox[3]) / 2
    center_lon = (bbox[0] + bbox[2]) / 2
    try:
        from scene import save_clips
        from damage import detect_damage_for_square
        saved = save_clips(center_lat, center_lon)
        folder = Path(saved["folder"])
        sq_bounds = tuple(saved["bounds"])
        gj, geo_polys, _ = detect_damage_for_square(folder, sq_bounds, threshold=0.25, min_area_px=10)
        is_building_only = gj.get("properties", {}).get("classification") == "damaged_building"
        if is_building_only:
            geo_polys = []
        if gj.get("features"):
            damage_geojson["features"].extend(gj["features"])
            geo_polygons.extend(geo_polys)
        return damage_geojson, geo_polygons, not is_building_only
    except Exception:
        # Satellite clips unavailable
        return damage_geojson, geo_polygons, False


@login_required
def context_view(request):
    """GET /api/context?bbox=w,s,e,n&mode=walk|drive

    Returns damage polygons, real shelters, dead-end/stranded edges as GeoJSON.
    """
    bbox_str = request.GET.get("bbox", "")
    mode = request.GET.get("mode", "walk")

    try:
        parts = [float(x.strip()) for x in bbox_str.split(",")]
        if len(parts) != 4:
            raise ValueError
        bbox = tuple(parts)  # (west, south, east, north)
    except (ValueError, AttributeError):
        return JsonResponse({"error": "bbox must be west,south,east,north"}, status=400)

    if mode not in ("walk", "drive"):
        mode = "walk"

    from evacuation_engine import (
        get_cached_graph,
        assign_edge_costs,
        prune_dead_ends,
        find_stranded_nodes,
        dead_end_geojson,
        snap_shelters_to_graph,
    )
    from shelters import find_shelters
    from django.conf import settings
    auto_expand_km = getattr(settings, "ROUTE_AUTO_EXPAND_KM", 2.0)

    # Shelters: real shelters only (OSM + DB with auto-expansion if needed)
    try:
        shelters, was_expanded = find_shelters(bbox, auto_expand_km=auto_expand_km)
    except Exception:
        shelters = []

    # Road graph
    try:
        G = get_cached_graph(bbox, mode)
    except Exception as exc:
        return JsonResponse({
            "error": "Area too large or map servers busy. Try a smaller area.",
            "details": str(exc),
        }, status=502)

    # Damage polygons via Sentinel-2 + xBD detection
    damage_geojson, geo_polygons, damage_assessed = _get_damage_for_bbox(bbox)

    # Dead-end / stranded analysis
    snapped = snap_shelters_to_graph(G, shelters)
    shelter_nodes = {s["node"] for s in snapped if "node" in s}
    blocked = assign_edge_costs(G, geo_polygons)

    if not shelter_nodes:
        # If no shelters are available, do not flag the entire map as dead ends/stranded
        de_geojson = {"type": "FeatureCollection", "features": []}
    else:
        import osmnx as ox
        center_lat = (bbox[1] + bbox[3]) / 2
        center_lon = (bbox[0] + bbox[2]) / 2
        try:
            center_node = ox.nearest_nodes(G, X=center_lon, Y=center_lat)
        except Exception:
            center_node = next(iter(G.nodes)) if G.number_of_nodes() > 0 else None

        protected = shelter_nodes.copy()
        if center_node is not None:
            protected.add(center_node)

        dead_edges = prune_dead_ends(G, protected)
        stranded = find_stranded_nodes(G, blocked, shelter_nodes)
        de_geojson = dead_end_geojson(G, dead_edges, stranded)

    return JsonResponse({
        "damage": damage_geojson,
        "shelters": snapped if snapped else shelters,
        "dead_ends": de_geojson,
        "stranded": {"type": "FeatureCollection", "features": []},
        "graph_nodes": G.number_of_nodes(),
        "graph_edges": G.number_of_edges(),
        "damage_assessed": damage_assessed,
    })


@csrf_exempt
@login_required
@require_POST
def routes_view(request):
    """POST /api/routes {start:{lat,lon}, bbox:[w,s,e,n], mode:"walk"|"drive"}

    Returns up to 3 diverse evacuation routes as GeoJSON to real shelters.
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    start = body.get("start", {})
    try:
        start_lat = float(start.get("lat"))
        start_lon = float(start.get("lon"))
    except (TypeError, ValueError):
        return JsonResponse({"error": "start.lat and start.lon are required."}, status=400)

    bbox_raw = body.get("bbox", [])
    try:
        bbox = tuple(float(x) for x in bbox_raw)
        if len(bbox) != 4:
            raise ValueError
    except (TypeError, ValueError):
        return JsonResponse({"error": "bbox must be [west, south, east, north]."}, status=400)

    mode = body.get("mode", "walk")
    if mode not in ("walk", "drive"):
        mode = "walk"

    from evacuation_engine import get_cached_graph, compute_routes
    from shelters import find_shelters
    from django.conf import settings
    auto_expand_km = getattr(settings, "ROUTE_AUTO_EXPAND_KM", 2.0)

    # Real shelters only (OSM + DB with auto-expansion)
    shelters, was_expanded = find_shelters(bbox, auto_expand_km=auto_expand_km)

    if not shelters:
        return JsonResponse({
            "routes": [],
            "warnings": [
                f"No shelters found inside the evacuation zone or within {auto_expand_km:.1f} km. "
                "Please enlarge the evacuation zone or register verified shelters in the admin panel."
            ],
            "disclaimer": "Damage is estimated from satellite imagery and may be wrong. Follow official guidance.",
        })

    # Get graph
    try:
        G = get_cached_graph(bbox, mode)
    except Exception as exc:
        return JsonResponse({
            "error": "Area too large or map servers busy. Try a smaller area.",
            "details": str(exc),
        }, status=502)

    # Damage polygons via Sentinel-2 + xBD detection
    _, geo_polygons, damage_assessed = _get_damage_for_bbox(bbox)

    result = compute_routes(
        G,
        start_lat,
        start_lon,
        shelters,
        mode=mode,
        damage_polys=geo_polygons,
        damage_assessed=damage_assessed,
    )

    if was_expanded:
        result.setdefault("warnings", []).insert(
            0, f"No shelters inside the zone. Search was automatically expanded by {auto_expand_km:.1f} km."
        )

    return JsonResponse(result)
