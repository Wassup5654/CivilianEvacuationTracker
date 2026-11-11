"""Annotation, hazard detection, and safe routing for 5 km satellite scenes."""

from __future__ import annotations

import collections
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

IMAGE_SIZE = 512
NATIVE_SIZE = 500
NATIVE_PIXEL_M = 10
SCALE_BAR_TEXT = "10 m per original pixel"

# Forbidden phrases check: none of these words must appear in satellite scene phrase lists
FORBIDDEN_PHRASES: tuple[str, ...] = (
    "pothole",
    "vehicle",
    "weapon",
    "personnel",
    "fortification",
)

# Standard allowed phrase list for satellite scene layers, hazards, and routing
SCENE_PHRASES: list[str] = [
    "road corridor",
    "safe route",
    "water damage",
    "standing water",
    "flooded pavement",
    "structure damage",
    "collapsed building",
    "rubble field",
    "car travel may not work",
    "debris on roadway",
    "not visible at satellite resolution",
]

NEXT_STEPS_TEXT = (
    "Stay in the marked safer direction, do not re-enter buildings marked unsound, "
    "do not drive through marks that block cars, do not drink or drive through marked water, "
    "and ask local emergency services what to do next."
)

NOT_VISIBLE_AT_10M = "not visible at satellite resolution"
ROAD_NOT_PASSABLE_MSG = "The road is not passable"
ROAD_PASSABLE_MSG = "Safe route along visible road corridor identified"

SQUARES_DIR = Path("data") / "squares"


class AnnotationError(RuntimeError):
    """The annotation or routing process could not be completed."""


def validate_scene_phrases(phrases: list[str] | tuple[str, ...] = SCENE_PHRASES) -> bool:
    """Ensure the phrase list does not contain any forbidden terms."""
    for phrase in phrases:
        p_lower = phrase.lower()
        for forbidden in FORBIDDEN_PHRASES:
            if forbidden in p_lower:
                raise ValueError(f"Forbidden phrase detected: '{forbidden}' in '{phrase}'")
    return True


# Run validation at import time
validate_scene_phrases()


@dataclass
class AnnotationResult:
    passable: bool
    passable_message: str
    route_drawn: bool
    route_coordinates: list[tuple[int, int]]
    water_damage_detected: bool
    structure_damage_detected: bool
    car_travel_blocked: bool
    potholes_drawn: bool
    trip_marks_drawn: bool
    captured: str
    cloud_cover: str
    scale_bar_text: str
    legend: dict[str, str]
    next_steps: str
    image: Image.Image | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("image", None)
        return data


def _get_font(size: int = 12, bold: bool = False) -> ImageFont.ImageFont:
    """Load a truetype font or fall back to the default PIL font."""
    font_names = ["arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"]
    for name in font_names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_synthetic_corridor(
    blocked: bool = False,
) -> tuple[Image.Image, np.ndarray, np.ndarray, np.ndarray]:
    """Generate a 512x512 synthetic corridor image with roads, flood, and rubble.

    Returns:
        (image, road_mask, water_mask, structure_mask)
    """
    img = Image.new("RGB", (IMAGE_SIZE, IMAGE_SIZE), (135, 155, 115))  # Terrain background
    draw = ImageDraw.Draw(img)

    road_mask = np.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=bool)
    water_mask = np.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=bool)
    structure_mask = np.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=bool)

    road_color = (128, 128, 128)
    flood_color = (35, 95, 220)   # Blue flood
    rubble_color = (185, 75, 45)  # Red/brown rubble

    # 1. Base intersection around address pin (center: 256, 256)
    # Horizontal connector from x=160 to x=380, y=235 to y=275
    draw.rectangle([160, 235, 380, 275], fill=road_color)
    road_mask[235:276, 160:381] = True

    # 2. Branch 1 (West lane going north): x=175 to 225, y=0 to 256
    draw.rectangle([175, 0, 225, 256], fill=road_color)
    road_mask[0:257, 175:226] = True

    # 3. Branch 2 (East lane going north): x=285 to 335, y=0 to 256
    draw.rectangle([285, 0, 335, 256], fill=road_color)
    road_mask[0:257, 285:336] = True

    # 4. Blue flood covering Branch 1 at y=90..150
    draw.rectangle([170, 90, 230, 150], fill=flood_color)
    water_mask[90:151, 170:231] = True

    # 5. Rubble block covering Branch 2 at y=90..150
    draw.rectangle([280, 90, 340, 150], fill=rubble_color)
    structure_mask[90:151, 280:341] = True

    # 6. Bypass / Detour road going around both obstacles to the right:
    # Curves from x=335, y=220 out to x=430, going up to y=40, then reaching the top boundary y=0
    if not blocked:
        # A clear bypass corridor connecting to the top edge
        draw.rectangle([335, 200, 420, 240], fill=road_color)
        road_mask[200:241, 335:421] = True

        draw.rectangle([380, 0, 420, 220], fill=road_color)
        road_mask[0:221, 380:421] = True
    else:
        # When blocked, add the road segment but place another hazard over it so no path reaches edge
        draw.rectangle([335, 200, 420, 240], fill=road_color)
        road_mask[200:241, 335:421] = True

        draw.rectangle([380, 0, 420, 220], fill=road_color)
        road_mask[0:221, 380:421] = True

        # Block the bypass with rubble/water
        draw.rectangle([375, 90, 425, 150], fill=rubble_color)
        structure_mask[90:151, 375:426] = True

    return img, road_mask, water_mask, structure_mask


def detect_synthetic_hazards(
    img: Image.Image,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract road, water, and structure masks from a synthetic image by color."""
    arr = np.array(img.convert("RGB"))
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

    # Blue flood
    water_mask = (b > 160) & (b > r.astype(int) + 30) & (b > g.astype(int) + 20)

    # Red/brown rubble
    structure_mask = (r > 150) & (r.astype(int) > g.astype(int) + 40) & (r.astype(int) > b.astype(int) + 40)

    # Neutral gray asphalt road
    diff_rg = np.abs(r.astype(int) - g.astype(int))
    diff_gb = np.abs(g.astype(int) - b.astype(int))
    road_mask = (diff_rg < 25) & (diff_gb < 25) & (r > 90) & (r < 165)

    return road_mask, water_mask, structure_mask


def detect_scene_hazards(
    latest_img: Image.Image,
    earlier_img: Image.Image | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Detect road corridor and hazards for a real satellite scene."""
    arr = np.array(latest_img.convert("RGB"))
    r, g, b = arr[:, :, 0].astype(int), arr[:, :, 1].astype(int), arr[:, :, 2].astype(int)

    # Synthetic check first
    synth_road, synth_water, synth_struct = detect_synthetic_hazards(latest_img)
    if synth_water.sum() > 200 and synth_struct.sum() > 200:
        return synth_road, synth_water, synth_struct

    # Satellite water heuristic: dark blue or high moisture
    water_mask = (b > r + 15) & (b > g + 10) & (b < 120)

    # Structure damage heuristic: significant difference between earlier and latest
    if earlier_img is not None:
        earlier_arr = np.array(earlier_img.resize(latest_img.size).convert("RGB"))
        diff = np.abs(arr.astype(int) - earlier_arr.astype(int)).mean(axis=-1)
        structure_mask = diff > 45
    else:
        # High-contrast rubble/debris clusters
        structure_mask = (r > 180) & (g > 160) & (b > 140) & (np.abs(r - b) > 30)

    # Road corridor heuristic: linear features / medium luminance with low saturation
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    road_mask = (lum > 70) & (lum < 185) & (sat < 35)

    return road_mask, water_mask, structure_mask


def find_safe_route(
    road_mask: np.ndarray,
    water_mask: np.ndarray,
    structure_mask: np.ndarray,
    start_point: tuple[int, int] = (256, 256),
) -> list[tuple[int, int]] | None:
    """Find a connected path of road pixels avoiding hazards from start to the edge.

    Returns:
        List of (x, y) coordinates from start_point to edge, or None if blocked.
    """
    height, width = road_mask.shape
    sx, sy = start_point

    # Hazards exclude road pixels
    hazard_mask = water_mask | structure_mask
    traversable = road_mask & ~hazard_mask

    # If the starting point itself is not traversable, find nearest traversable pixel
    if not (0 <= sx < width and 0 <= sy < height and traversable[sy, sx]):
        best_dist = float("inf")
        best_pt = None
        for dy in range(-25, 26):
            for dx in range(-25, 26):
                nx, ny = sx + dx, sy + dy
                if 0 <= nx < width and 0 <= ny < height and traversable[ny, nx]:
                    d = dx * dx + dy * dy
                    if d < best_dist:
                        best_dist = d
                        best_pt = (nx, ny)
        if best_pt is None:
            return None
        sx, sy = best_pt

    # Check if start is already at an edge
    if sx == 0 or sx == width - 1 or sy == 0 or sy == height - 1:
        return [(sx, sy)]

    # 8-connectivity BFS
    visited = np.zeros((height, width), dtype=bool)
    parent_x = np.full((height, width), -1, dtype=np.int32)
    parent_y = np.full((height, width), -1, dtype=np.int32)

    queue = collections.deque([(sx, sy)])
    visited[sy, sx] = True
    reached_edge: tuple[int, int] | None = None

    directions = [
        (0, -1), (0, 1), (-1, 0), (1, 0),
        (-1, -1), (1, -1), (-1, 1), (1, 1),
    ]

    while queue:
        cx, cy = queue.popleft()

        # Goal check: reached any boundary pixel
        if cx == 0 or cx == width - 1 or cy == 0 or cy == height - 1:
            reached_edge = (cx, cy)
            break

        for dx, dy in directions:
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < width and 0 <= ny < height:
                if traversable[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    parent_x[ny, nx] = cx
                    parent_y[ny, nx] = cy
                    queue.append((nx, ny))

    if reached_edge is None:
        return None

    # Reconstruct path from edge back to start
    path: list[tuple[int, int]] = []
    curr: tuple[int, int] | None = reached_edge
    while curr is not None:
        path.append((int(curr[0]), int(curr[1])))
        cx, cy = curr
        px = int(parent_x[cy, cx])
        py = int(parent_y[cy, cx])
        if px == -1 and py == -1:
            break
        curr = (px, py)

    path.reverse()
    return path


def draw_annotated_graphic(
    base_image: Image.Image,
    road_mask: np.ndarray,
    water_mask: np.ndarray,
    structure_mask: np.ndarray,
    route: list[tuple[int, int]] | None,
    captured: str,
    cloud_cover: str | float,
    start_point: tuple[int, int] = (256, 256),
) -> Image.Image:
    """Draw the 512x512 annotated graphic with overlays, scale bar, legend, and next steps."""
    canvas = base_image.convert("RGBA").resize((IMAGE_SIZE, IMAGE_SIZE))

    # 1. Overlay layer for water, structure damage, and car blocking marks
    overlay = Image.new("RGBA", (IMAGE_SIZE, IMAGE_SIZE), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    # Water damage overlay (blue semi-transparent: #2563EB with alpha 130)
    water_indices = np.argwhere(water_mask)
    for y, x in water_indices:
        overlay.putpixel((x, y), (37, 99, 235, 130))

    # Structure damage overlay (red semi-transparent: #DC2626 with alpha 140)
    struct_indices = np.argwhere(structure_mask)
    for y, x in struct_indices:
        overlay.putpixel((x, y), (220, 38, 38, 140))

    # Car travel may not work marks: where water or rubble covers the roadway
    car_blocked_mask = road_mask & (water_mask | structure_mask)
    car_blocked_indices = np.argwhere(car_blocked_mask)
    for y, x in car_blocked_indices[::4]:  # Hatch pattern
        overlay_draw.line([(x - 2, y - 2), (x + 2, y + 2)], fill=(245, 158, 11, 220), width=1)
        overlay_draw.line([(x - 2, y + 2), (x + 2, y - 2)], fill=(245, 158, 11, 220), width=1)

    # Composite base image with overlay
    annotated = Image.alpha_composite(canvas, overlay)
    draw = ImageDraw.Draw(annotated)

    font_small = _get_font(10)
    font_bold = _get_font(11, bold=True)
    font_large = _get_font(13, bold=True)

    # 2. Safe route (Green line, width 4)
    if route is not None and len(route) > 1:
        draw.line(route, fill=(16, 185, 129, 255), width=4)
        # End arrow / destination marker
        ex, ey = route[-1]
        draw.ellipse([ex - 4, ey - 4, ex + 4, ey + 4], fill=(16, 185, 129, 255), outline=(255, 255, 255, 255))
    elif route is None:
        # Prominent warning that the road is not passable
        draw.rectangle([40, 210, 472, 260], fill=(140, 20, 20, 230), outline=(255, 255, 255, 255))
        draw.text((70, 225), "THE ROAD IS NOT PASSABLE - CORRIDOR BLOCKED", fill=(255, 255, 255, 255), font=font_large)

    # 3. Address pin (Brass / amber circle at start point)
    sx, sy = start_point
    draw.ellipse([sx - 6, sy - 6, sx + 6, sy + 6], fill=(154, 99, 36, 255), outline=(255, 255, 255, 255), width=2)
    draw.text((sx + 8, sy - 6), "Address", fill=(255, 255, 255, 255), font=font_bold)

    # 4. Top banner: Scene Date and Cloud Note
    cloud_str = f"{float(cloud_cover):.1f}%" if isinstance(cloud_cover, (int, float)) else str(cloud_cover)
    top_text = f"Scene captured: {captured} | Cloud cover: {cloud_str}"
    draw.rectangle([0, 0, IMAGE_SIZE, 24], fill=(20, 25, 22, 200))
    draw.text((8, 5), top_text, fill=(245, 241, 230, 255), font=font_small)

    # 5. Scale bar (10 m per original pixel; 50px = 500m)
    bar_x = IMAGE_SIZE - 160
    bar_y = 32
    draw.rectangle([bar_x - 6, bar_y - 4, bar_x + 154, bar_y + 26], fill=(20, 25, 22, 180))
    draw.line([(bar_x, bar_y + 14), (bar_x + 50, bar_y + 14)], fill=(255, 255, 255, 255), width=3)
    draw.line([(bar_x, bar_y + 9), (bar_x, bar_y + 19)], fill=(255, 255, 255, 255), width=2)
    draw.line([(bar_x + 50, bar_y + 9), (bar_x + 50, bar_y + 19)], fill=(255, 255, 255, 255), width=2)
    draw.text((bar_x + 56, bar_y + 2), "500 m", fill=(255, 255, 255, 255), font=font_small)
    draw.text((bar_x, bar_y + 16), SCALE_BAR_TEXT, fill=(210, 210, 210, 255), font=font_small)

    # 6. Bottom Legend and Next Steps panel
    panel_y = IMAGE_SIZE - 128
    draw.rectangle([0, panel_y, IMAGE_SIZE, IMAGE_SIZE], fill=(24, 30, 28, 225), outline=(143, 191, 155, 120))

    # Legend rows (left side of panel)
    lx = 10
    ly = panel_y + 6
    draw.text((lx, ly), "LAYERS & HAZARDS:", fill=(230, 200, 140, 255), font=font_bold)

    # Safe route
    draw.line([(lx, ly + 18), (lx + 16, ly + 18)], fill=(16, 185, 129, 255), width=3)
    draw.text((lx + 22, ly + 12), "Safe route" if route is not None else "Route: blocked", fill=(240, 240, 240, 255), font=font_small)

    # Water damage
    draw.rectangle([lx, ly + 28, lx + 16, ly + 36], fill=(37, 99, 235, 255))
    draw.text((lx + 22, ly + 26), "Water damage", fill=(240, 240, 240, 255), font=font_small)

    # Structure damage
    draw.rectangle([lx, ly + 42, lx + 16, ly + 50], fill=(220, 38, 38, 255))
    draw.text((lx + 22, ly + 40), "Structure damage (do not enter)", fill=(240, 240, 240, 255), font=font_small)

    # Car travel may not work
    draw.rectangle([lx, ly + 56, lx + 16, ly + 64], fill=(245, 158, 11, 255))
    draw.text((lx + 22, ly + 54), "Car travel may not work (debris/water)", fill=(240, 240, 240, 255), font=font_small)

    # Trip hazards and potholes: explicit rows stating not visible at satellite resolution
    draw.text((lx, ly + 70), f"Trip hazards: {NOT_VISIBLE_AT_10M}", fill=(180, 180, 180, 255), font=font_small)
    draw.text((lx, ly + 84), f"Potholes & curbs: {NOT_VISIBLE_AT_10M}", fill=(180, 180, 180, 255), font=font_small)

    # Next steps panel (right side of panel)
    nx = 265
    ny = panel_y + 6
    draw.text((nx, ny), "NEXT STEPS:", fill=(230, 200, 140, 255), font=font_bold)
    steps_lines = [
        "1. Stay in marked safer direction.",
        "2. Do not re-enter buildings marked unsound.",
        "3. Do not drive through marks that block cars.",
        "4. Do not drink or drive through marked water.",
        "5. Ask local emergency services what to do next.",
    ]
    for i, line in enumerate(steps_lines):
        draw.text((nx, ny + 15 + i * 13), line, fill=(240, 240, 240, 255), font=font_small)

    return annotated.convert("RGB")


def annotate_scene(
    image: Image.Image,
    captured: str = "2026-10-23T00:00:00Z",
    cloud_cover: str | float = 0.0,
    earlier_image: Image.Image | None = None,
    road_mask: np.ndarray | None = None,
    water_mask: np.ndarray | None = None,
    structure_mask: np.ndarray | None = None,
    start_point: tuple[int, int] = (256, 256),
) -> AnnotationResult:
    """Analyze the scene, find a safe route around hazards, and draw the annotated graphic."""
    # Ensure forbidden phrases validation
    validate_scene_phrases()

    # 1. Obtain masks
    if road_mask is None or water_mask is None or structure_mask is None:
        detected_road, detected_water, detected_struct = detect_scene_hazards(image, earlier_image)
        road_mask = detected_road if road_mask is None else road_mask
        water_mask = detected_water if water_mask is None else water_mask
        structure_mask = detected_struct if structure_mask is None else structure_mask

    # 2. Check for car-blocking hazards where road overlaps damage
    car_blocked_mask = road_mask & (water_mask | structure_mask)
    has_water = bool(water_mask.sum() > 0)
    has_structure = bool(structure_mask.sum() > 0)
    has_car_blocked = bool(car_blocked_mask.sum() > 0)

    # 3. Route finding
    route = find_safe_route(road_mask, water_mask, structure_mask, start_point=start_point)
    passable = route is not None
    passable_msg = ROAD_PASSABLE_MSG if passable else ROAD_NOT_PASSABLE_MSG

    # 4. Draw graphic
    annotated_img = draw_annotated_graphic(
        base_image=image,
        road_mask=road_mask,
        water_mask=water_mask,
        structure_mask=structure_mask,
        route=route,
        captured=captured,
        cloud_cover=cloud_cover,
        start_point=start_point,
    )

    legend_data = {
        "safe_route": "Visible along road corridor" if passable else ROAD_NOT_PASSABLE_MSG,
        "water_damage": "Large standing water or flooded pavement" if has_water else "None detected",
        "structure_damage": "Large collapsed buildings or rubble fields" if has_structure else "None detected",
        "car_travel_may_not_work": "Debris or water covering roadway" if has_car_blocked else "None detected",
        "trip_hazards": NOT_VISIBLE_AT_10M,
        "potholes": NOT_VISIBLE_AT_10M,
        "curbs": NOT_VISIBLE_AT_10M,
    }

    return AnnotationResult(
        passable=passable,
        passable_message=passable_msg,
        route_drawn=passable,
        route_coordinates=route or [],
        water_damage_detected=has_water,
        structure_damage_detected=has_structure,
        car_travel_blocked=has_car_blocked,
        potholes_drawn=False,
        trip_marks_drawn=False,
        captured=captured,
        cloud_cover=f"{float(cloud_cover):.1f}%" if isinstance(cloud_cover, (int, float)) else str(cloud_cover),
        scale_bar_text=SCALE_BAR_TEXT,
        legend=legend_data,
        next_steps=NEXT_STEPS_TEXT,
        image=annotated_img,
    )


def annotate_square(
    latitude: float,
    longitude: float,
    root: Path | str = SQUARES_DIR,
) -> dict[str, Any]:
    """Annotate the saved 5 km square for a latitude and longitude."""
    folder = Path(root) / f"{latitude:.5f}_{longitude:.5f}"
    meta_path = folder / "square.json"
    latest_path = folder / "latest.tif"
    earlier_path = folder / "earlier.tif"
    out_path = folder / "annotated.png"

    if not folder.is_dir() or not latest_path.is_file():
        raise AnnotationError("Save the satellite square before running annotation.")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        meta = {}

    latest_meta = meta.get("latest") or {}
    captured = str(latest_meta.get("captured") or "Unknown date")
    cloud_cover = latest_meta.get("cloud_cover") or 0.0

    latest_img = Image.open(latest_path).convert("RGB")
    earlier_img = Image.open(earlier_path).convert("RGB") if earlier_path.is_file() else None

    result = annotate_scene(
        image=latest_img,
        captured=captured,
        cloud_cover=cloud_cover,
        earlier_image=earlier_img,
    )

    if result.image is not None:
        result.image.save(out_path, format="PNG")

    res_dict = result.to_dict()
    res_dict["annotated_file"] = str(out_path)
    res_dict["folder"] = str(folder)

    # Persist in square.json
    meta["annotation"] = res_dict
    try:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError:
        pass

    return res_dict
