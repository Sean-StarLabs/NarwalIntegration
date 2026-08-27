"""Map renderer for Narwal vacuum — converts raw map data to PNG bytes.

Pure Python module with no Home Assistant dependencies.
Uses Pillow for image rendering.

Map data format (confirmed from live robot data):
  - Compressed with standard zlib (header 78 01)
  - Decompressed data is a protobuf message: field 1 = packed repeated varints
  - Skip 4-byte protobuf header, then decode varints
  - Each varint encodes: room_id = value >> 8, pixel_type = value & 0xFF
  - Value 0 = unknown/outside, 0x20 = unassigned floor, 0x28 = unassigned obstacle
  - pixel_type & 0x10 = wall/border edge (darken the room color)
"""

from __future__ import annotations

import io
import logging
import math
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image, ImageDraw

_LOGGER = logging.getLogger(__name__)

# Muted room tint palette (RGB). These are blended lightly into material
# colours so neighbouring rooms remain distinguishable without app-like neon.
ROOM_COLORS: list[tuple[int, int, int]] = [
    (142, 156, 166),  # blue grey
    (150, 161, 145),  # sage
    (166, 151, 144),  # clay
    (166, 158, 140),  # muted oak
    (150, 143, 160),  # mauve grey
    (138, 158, 160),  # slate aqua
    (170, 166, 140),  # straw grey
    (158, 143, 136),  # warm grey
    (142, 162, 150),  # green grey
    (137, 151, 163),  # cool slate
    (166, 142, 142),  # brick grey
    (155, 147, 159),  # thistle grey
    (171, 165, 146),  # limestone
    (150, 160, 164),  # pale slate
    (169, 149, 128),  # warm timber
    (166, 158, 146),  # wheat grey
    (137, 160, 154),  # desaturated teal
    (170, 146, 132),  # salmon grey
    (151, 166, 143),  # moss grey
    (171, 159, 146),  # bisque grey
    (157, 145, 157),  # purple grey
    (146, 158, 164),  # pastel slate
]

FLOOR_MATERIAL_COLORS: dict[str, tuple[int, int, int]] = {
    "timber": (178, 169, 152),
    "tile": (186, 191, 190),
    "carpet": (105, 107, 108),
    "concrete": (138, 145, 146),
    "default": (158, 161, 158),
}
FLOOR_MATERIAL_ALIASES: dict[str, str] = {
    "wood": "timber",
    "hardwood": "timber",
    "laminate": "timber",
    "floorboard": "timber",
    "floorboards": "timber",
    "tiles": "tile",
    "ceramic": "tile",
    "cement": "concrete",
}

ROOM_TYPE_MATERIALS: dict[int, str] = {
    1: "timber",  # Master bedroom
    2: "timber",  # Secondary bedroom
    3: "timber",  # Living room
    4: "timber",  # Kitchen
    5: "tile",  # Bathroom
    6: "tile",  # Toilet
    7: "concrete",  # Balcony
    8: "timber",  # Dining room
    9: "timber",  # Closet
    10: "timber",  # Corridor
    11: "timber",  # Study
    12: "timber",  # Kids' room
    13: "timber",  # Entertainment room
    14: "timber",  # Storage room
    15: "timber",  # Others
}

ROOM_NAME_MATERIAL_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("bath", "shower", "toilet", "wc", "ensuite", "washroom"), "tile"),
    (("kitchen", "dining", "lounge", "living", "hallway"), "timber"),
    (("garage", "balcony", "patio"), "concrete"),
    (("utility", "laundry"), "tile"),
)

# Obstacle/furniture annotation colors by catalog from APK map_furniture.json
OBSTACLE_COLORS: dict[int, tuple[int, int, int]] = {
    # Beds (1-3)
    1: (180, 140, 100),    # single bed - tan
    2: (180, 140, 100),    # double bed - tan
    3: (180, 140, 100),    # baby bed - tan
    # Tables (4-7, 31)
    4: (160, 130, 90),     # dining table - brown
    5: (160, 130, 90),     # round table - brown
    6: (160, 130, 90),     # tea table - brown
    7: (160, 130, 90),     # round tea table - brown
    31: (160, 130, 90),    # desk - brown
    # Cupboards/storage (8-12)
    8: (140, 120, 100),    # TV stand - dark tan
    9: (140, 120, 100),    # bedside table - dark tan
    10: (140, 120, 100),   # locker - dark tan
    11: (140, 120, 100),   # wardrobe - dark tan
    12: (140, 120, 100),   # shoe cabinet - dark tan
    # Sofas/chairs (13-18, 30)
    13: (100, 160, 130),   # armchair - sage
    14: (100, 160, 130),   # sofa - sage
    15: (100, 160, 130),   # L-shaped sofa - sage
    16: (100, 160, 130),   # lazy chair - sage
    17: (100, 160, 130),   # chair - sage
    18: (100, 160, 130),   # bar chair - sage
    30: (100, 160, 130),   # U-shaped sofa - sage
    # Pets (19-21, 75-76)
    19: (200, 160, 120),   # cat toilet - peach
    20: (200, 160, 120),   # pet feeder - peach
    21: (200, 160, 120),   # pet house - peach
    75: (200, 160, 120),   # cat house - peach
    76: (200, 160, 120),   # dog house - peach
    # Appliances (22-25, 34)
    22: (150, 180, 200),   # washing machine - steel blue
    23: (150, 180, 200),   # refrigerator - steel blue
    24: (150, 180, 200),   # air conditioner - steel blue
    25: (150, 180, 200),   # fan - steel blue
    34: (150, 180, 200),   # stove - steel blue
    # Bathroom (28, 33)
    28: (120, 180, 220),   # toilet - light blue
    33: (120, 180, 220),   # washbasin - light blue
    # Misc (26-27, 29, 32, 77-78)
    26: (100, 180, 100),   # potted plant - green
    27: (200, 200, 220),   # floor mirror - silver
    29: (80, 80, 80),      # piano - dark gray
    32: (80, 80, 80),      # grand piano - dark gray
    77: (200, 200, 200),   # round placeholder - gray
    78: (200, 200, 200),   # weighing scale - gray
}
OBSTACLE_COLOR_DEFAULT = (200, 200, 200)

# Special pixel colors
COLOR_UNKNOWN = (0, 0, 0, 0)         # outside map / unmapped, transparent
COLOR_UNASSIGNED_FLOOR = (142, 141, 133)  # floor not assigned to a room
COLOR_UNASSIGNED_OBSTACLE = (112, 111, 104)  # obstacle not in a room
COLOR_FALLBACK = (162, 164, 160)     # unknown room ID
MAP_RENDER_SCALE = 4
ROOM_LABEL_FONT_SCALE = 9
OBSTACLE_LABEL_FONT_SCALE = 6
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-SemiBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "arial.ttf",
)


def _opaque(color: tuple[int, int, int]) -> tuple[int, int, int, int]:
    """Return an RGB color with a fully opaque alpha channel."""
    return (*color, 255)


def _paint_solid_cell(
    px,
    left: int,
    top: int,
    scale: int,
    color: tuple[int, int, int],
) -> None:
    """Paint one scaled map cell with a solid colour."""
    rgba = _opaque(color)
    for sy in range(scale):
        for sx in range(scale):
            px[left + sx, top + sy] = rgba


def _paint_room_cell(
    px,
    left: int,
    top: int,
    scale: int,
    room_id: int,
    ptype: int,
    room_names: dict[int, str] | None,
    room_types: dict[int, int] | None,
    room_materials: dict[object, object] | None = None,
    carpet_mask_px=None,
    carpet_zone_mask_px=None,
) -> None:
    """Paint one scaled map cell with final-resolution material texture."""
    material = _floor_material_for_room(
        room_id, room_names, room_types, room_materials
    )
    for sy in range(scale):
        py = top + sy
        for sx in range(scale):
            px_material = material
            if (
                carpet_mask_px is not None and carpet_mask_px[left + sx, py] > 0
            ) or (
                carpet_zone_mask_px is not None
                and carpet_zone_mask_px[left + sx, py] > 0
            ):
                px_material = "carpet"
            px[left + sx, py] = _opaque(
                _textured_room_color(
                    room_id,
                    ptype,
                    left + sx,
                    py,
                    room_names,
                    room_types,
                    material=px_material,
                )
            )


def _clamp_channel(value: int) -> int:
    """Clamp an integer RGB channel to byte range."""
    return max(0, min(255, value))


def _adjust_color(
    color: tuple[int, int, int],
    amount: int,
) -> tuple[int, int, int]:
    """Lighten or darken an RGB color by a signed amount."""
    return (
        _clamp_channel(color[0] + amount),
        _clamp_channel(color[1] + amount),
        _clamp_channel(color[2] + amount),
    )


def _mix_color(
    color: tuple[int, int, int],
    overlay: tuple[int, int, int],
    alpha: float,
) -> tuple[int, int, int]:
    """Blend two RGB colors."""
    alpha = max(0.0, min(1.0, alpha))
    return (
        _clamp_channel(round((color[0] * (1.0 - alpha)) + (overlay[0] * alpha))),
        _clamp_channel(round((color[1] * (1.0 - alpha)) + (overlay[1] * alpha))),
        _clamp_channel(round((color[2] * (1.0 - alpha)) + (overlay[2] * alpha))),
    )


def _normalize_room_key(value: object) -> str:
    """Return a stable key for matching option values to room names."""
    text = str(value or "").lower().replace("'", "").replace("’", "")
    normalized = "".join(char if char.isalnum() else " " for char in text)
    return " ".join(normalized.split())


def _normalize_floor_material(value: object) -> str | None:
    """Return a supported floor material from an option value."""
    material = _normalize_room_key(value)
    if not material or material in {"auto", "none"}:
        return None
    if "carpet" in material or "rug" in material:
        return "carpet"
    material = material.replace(" ", "_")
    material = FLOOR_MATERIAL_ALIASES.get(material, material)
    if material in FLOOR_MATERIAL_COLORS:
        return material
    return None


def _room_target_matches(
    target: object,
    room_id: int,
    room_names: dict[int, str] | None,
) -> bool:
    """Return whether an option target identifies this room."""
    target_key = _normalize_room_key(target)
    if not target_key:
        return False
    if target_key == str(room_id):
        return True
    room_name = (room_names or {}).get(room_id)
    return bool(room_name and target_key == _normalize_room_key(room_name))


def _material_override_for_room(
    room_id: int,
    room_names: dict[int, str] | None,
    room_materials: dict[object, object] | None,
) -> str | None:
    """Return a configured material override for this room, if present."""
    if not room_materials:
        return None
    fallback_material: str | None = None
    for target, value in room_materials.items():
        material = _normalize_floor_material(value)
        if material is None:
            continue
        if str(target or "").strip().lower() == "*":
            fallback_material = material
            continue
        target_key = _normalize_room_key(target)
        if target_key == "all":
            fallback_material = material
            continue
        if _room_target_matches(target, room_id, room_names):
            return material
    return fallback_material


def _mask_coverage(mask: Image.Image) -> float:
    """Return the fraction of non-zero pixels in a binary mask image."""
    total = mask.width * mask.height
    if total <= 0:
        return 0.0
    histogram = mask.histogram()
    return (total - histogram[0]) / total


def _mask_from_alpha(image: Image.Image) -> Image.Image | None:
    """Return an alpha-derived mask when the carpet image has useful alpha."""
    alpha = image.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min >= 250 or alpha_max <= 32:
        return None
    mask = alpha.point(lambda value: 255 if value > 32 else 0)
    coverage = _mask_coverage(mask)
    if 0.0005 <= coverage <= 0.75:
        return mask
    return None


def _mask_from_colour(image: Image.Image, *, saturation_threshold: int) -> Image.Image:
    """Return a mask for coloured carpet pixels in a Narwal debug PNG."""
    from PIL import Image

    mask = Image.new("L", image.size, 0)
    src = image.load()
    dst = mask.load()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = src[x, y]
            if a <= 32:
                continue
            bright = max(r, g, b)
            dark = min(r, g, b)
            if bright >= 245 and bright - dark <= 8:
                continue
            if bright >= 35 and bright - dark >= saturation_threshold:
                dst[x, y] = 255
    return mask


def _mask_from_brightness(image: Image.Image) -> Image.Image | None:
    """Return a mask for greyscale carpet PNGs with a dark/static background."""
    grayscale = image.convert("L")
    samples: list[int] = []
    if grayscale.width and grayscale.height:
        src = grayscale.load()
        for x in range(grayscale.width):
            samples.append(src[x, 0])
            samples.append(src[x, grayscale.height - 1])
        for y in range(grayscale.height):
            samples.append(src[0, y])
            samples.append(src[grayscale.width - 1, y])
    if not samples:
        return None

    samples.sort()
    background = samples[len(samples) // 2]
    threshold = min(220, max(40, background + 45))
    mask = grayscale.point(lambda value: 255 if value >= threshold else 0)
    coverage = _mask_coverage(mask)
    if 0.0005 <= coverage <= 0.55:
        return mask
    return None


def _carpet_mask_from_image(
    carpet_map_image: bytes | None,
    target_size: tuple[int, int],
) -> Image.Image | None:
    """Build a carpet mask from Narwal's cleartext carpet debug PNG."""
    if not carpet_map_image or target_size[0] <= 0 or target_size[1] <= 0:
        return None

    try:
        from PIL import Image, ImageFilter

        image = Image.open(io.BytesIO(carpet_map_image)).convert("RGBA")
    except Exception:
        _LOGGER.debug("Could not decode Narwal carpet debug image", exc_info=True)
        return None

    if image.size != target_size:
        image = image.resize(target_size, getattr(Image, "Resampling", Image).BILINEAR)

    # The endpoint returns debug PNGs rather than a documented schema, so prefer
    # explicit alpha/colour masks and reject candidates that cover most of the map.
    mask = _mask_from_alpha(image)
    if mask is None:
        for threshold in (35, 55, 75):
            colour_mask = _mask_from_colour(image, saturation_threshold=threshold)
            coverage = _mask_coverage(colour_mask)
            if 0.0005 <= coverage <= 0.65:
                mask = colour_mask
                break
    if mask is None:
        mask = _mask_from_brightness(image)
    if mask is None:
        return None

    return mask.filter(ImageFilter.GaussianBlur(radius=0.6)).point(
        lambda value: 255 if value >= 64 else 0
    )


def _room_bounds_for_pixels(
    pixels: list[int],
    width: int,
) -> dict[int, tuple[int, int, int, int]]:
    """Return floor-cell bounds for each room in unflipped grid coordinates."""
    room_bounds: dict[int, tuple[int, int, int, int]] = {}
    if width <= 0:
        return room_bounds
    for index, value in enumerate(pixels):
        if value == 0 or value in (0x20, 0x28):
            continue
        room_id = value >> 8
        ptype = value & 0xFF
        if ptype & 0x10:
            continue
        x = index % width
        y = index // width
        min_x, min_y, max_x, max_y = room_bounds.get(room_id, (x, y, x, y))
        room_bounds[room_id] = (
            min(min_x, x),
            min(min_y, y),
            max(max_x, x),
            max(max_y, y),
        )
    return room_bounds


def _zone_fraction(
    zone: dict,
    keys: tuple[str, ...],
    default: float,
    *,
    minimum: float = 0.0,
) -> float:
    """Return a clamped room-relative fraction from a carpet zone option."""
    value = default
    for key in keys:
        if key not in zone:
            continue
        try:
            value = float(zone[key])
        except (TypeError, ValueError):
            value = default
        break
    if value > 1.0:
        value /= 100.0
    return max(minimum, min(1.0, value))


def _room_id_for_zone(
    zone: dict,
    room_names: dict[int, str] | None,
    room_bounds: dict[int, tuple[int, int, int, int]],
) -> int | None:
    """Resolve a carpet zone's room target to a room_id."""
    for key in ("room_id", "room", "room_name"):
        if key not in zone:
            continue
        target = zone[key]
        for room_id in room_bounds:
            if _room_target_matches(target, room_id, room_names):
                return room_id
    return None


def _carpet_zone_mask_from_overrides(
    carpet_zones: list[dict[str, object]] | tuple[dict[str, object], ...] | None,
    room_names: dict[int, str] | None,
    room_bounds: dict[int, tuple[int, int, int, int]],
    width: int,
    height: int,
    scale: int,
) -> Image.Image | None:
    """Build a mask for configured carpet/rug zones."""
    if not carpet_zones or width <= 0 or height <= 0 or scale <= 0:
        return None

    from PIL import Image, ImageDraw

    mask = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(mask)
    drew_zone = False

    for zone in carpet_zones:
        if not isinstance(zone, dict):
            continue
        room_id = _room_id_for_zone(zone, room_names, room_bounds)
        if room_id is None:
            continue
        min_x, min_y, max_x, max_y = room_bounds[room_id]
        room_left = min_x * scale
        room_top = (height - 1 - max_y) * scale
        room_right = (max_x + 1) * scale
        room_bottom = (height - min_y) * scale
        room_width = max(1.0, room_right - room_left)
        room_height = max(1.0, room_bottom - room_top)

        centre_x = room_left + (
            room_width * _zone_fraction(zone, ("x_percent", "center_x", "x"), 0.5)
        )
        centre_y = room_top + (
            room_height * _zone_fraction(zone, ("y_percent", "center_y", "y"), 0.5)
        )
        zone_width = room_width * _zone_fraction(
            zone, ("width_percent", "width", "w"), 0.36, minimum=0.02
        )
        zone_height = room_height * _zone_fraction(
            zone, ("height_percent", "height", "h"), 0.28, minimum=0.02
        )
        bounds = [
            int(round(centre_x - (zone_width / 2))),
            int(round(centre_y - (zone_height / 2))),
            int(round(centre_x + (zone_width / 2))),
            int(round(centre_y + (zone_height / 2))),
        ]
        shape = _normalize_room_key(zone.get("shape", "ellipse"))
        if shape in {"rectangle", "rect", "square"}:
            try:
                draw.rounded_rectangle(
                    bounds,
                    radius=max(2, int(min(zone_width, zone_height) * 0.08)),
                    fill=255,
                )
            except AttributeError:
                draw.rectangle(bounds, fill=255)
        else:
            draw.ellipse(bounds, fill=255)
        drew_zone = True

    return mask if drew_zone else None


def _texture_noise(room_id: int, x: int, y: int) -> int:
    """Return a deterministic small noise value for map texture."""
    value = (x * 73_856_093) ^ (y * 19_349_663) ^ (room_id * 83_492_791)
    value ^= value >> 13
    value *= 1_274_126_177
    return ((value >> 16) & 0xFF) - 128


def _floor_material_for_room(
    room_id: int,
    room_names: dict[int, str] | None = None,
    room_types: dict[int, int] | None = None,
    room_materials: dict[object, object] | None = None,
) -> str:
    """Infer a coarse floor material from Narwal room metadata."""
    if material := _material_override_for_room(room_id, room_names, room_materials):
        return material

    room_type = (room_types or {}).get(room_id)
    if room_type in ROOM_TYPE_MATERIALS:
        return ROOM_TYPE_MATERIALS[room_type]

    room_name = (room_names or {}).get(room_id, "").lower()
    for hints, material in ROOM_NAME_MATERIAL_HINTS:
        if any(hint in room_name for hint in hints):
            return material

    return "timber"


def _material_base_color(
    room_id: int,
    material: str,
) -> tuple[int, int, int]:
    """Return a material colour with a subtle deterministic room tint."""
    base = FLOOR_MATERIAL_COLORS.get(material, FLOOR_MATERIAL_COLORS["default"])
    tint = ROOM_COLORS[(room_id - 1) % len(ROOM_COLORS)]
    return _mix_color(base, tint, 0.14)


def _textured_room_color(
    room_id: int,
    ptype: int,
    x: int,
    y: int,
    room_names: dict[int, str] | None = None,
    room_types: dict[int, int] | None = None,
    *,
    material: str | None = None,
) -> tuple[int, int, int]:
    """Return the rendered colour for one room pixel."""
    material = material or _floor_material_for_room(room_id, room_names, room_types)
    color = _material_base_color(room_id, material)
    noise = _texture_noise(room_id, x, y)

    if material == "timber":
        plank_height = 18
        joint_length = 118
        plank_y = (y + (room_id * 7)) % plank_height
        joint_x = (x + ((y // plank_height) % 2) * (joint_length // 2)) % joint_length
        color = _adjust_color(color, noise // 34)
        if plank_y in (0, 1):
            color = _adjust_color(color, -16)
        if joint_x in (0, 1) and plank_y > 2:
            color = _adjust_color(color, -10)
        if ((x * 5) + (y * 2) + room_id) % 29 == 0:
            color = _adjust_color(color, 4)
    elif material == "tile":
        tile_size = 28
        grout_x = (x + room_id) % tile_size
        grout_y = (y + room_id) % tile_size
        color = _adjust_color(color, noise // 44)
        if grout_x in (0, 1) or grout_y in (0, 1):
            color = _mix_color(color, (238, 240, 238), 0.64)
    elif material == "carpet":
        color = _adjust_color(color, noise // 20)
        if (x + y + room_id) % 3 == 0:
            color = _adjust_color(color, 3)
        if (x - y + room_id) % 4 == 0:
            color = _adjust_color(color, -3)
    elif material == "concrete":
        color = _adjust_color(color, noise // 18)
        if (x * 5 + y * 3 + room_id) % 47 == 0:
            color = _adjust_color(color, 12)
        if (x * 7 + y + room_id) % 53 == 0:
            color = _adjust_color(color, -10)
    else:
        color = _adjust_color(color, noise // 24)

    if ptype & 0x10:
        return _darken(color, 44)
    return color


def _load_font(image_font: object, size: int):
    """Load a crisp TrueType font, falling back to Pillow's default font."""
    for path in FONT_PATHS:
        try:
            return image_font.truetype(path, size)
        except OSError:
            continue
    return image_font.load_default()


def _fit_label_font(
    image_font: object,
    text: str,
    max_size: int,
    *,
    max_width: int | None = None,
    max_height: int | None = None,
):
    """Return the largest label font that fits the available room space."""
    min_size = max(8, min(max_size, 14))
    for size in range(max_size, min_size - 1, -1):
        font = _load_font(image_font, size)
        bbox = font.getbbox(text)
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if (max_width is None or width <= max_width) and (
            max_height is None or height <= max_height
        ):
            return font
    return _load_font(image_font, min_size)


def _scaled_coord(value: float, scale: float, size: int) -> int:
    """Return a scaled grid coordinate centred in the rendered map cell."""
    coordinate = int(round(value * scale + (scale / 2)))
    return max(0, min(coordinate, size - 1))


def _draw_label(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: object,
    *,
    fill: tuple[int, int, int] = (255, 255, 255),
    stroke_fill: tuple[int, int, int] = (20, 20, 20),
    padding: int = 4,
) -> None:
    """Draw a readable centred map label."""
    cx, cy = xy
    bbox = font.getbbox(text)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw // 2
    ty = cy - th // 2
    bg = [
        tx - padding,
        ty - padding,
        tx + tw + padding,
        ty + th + padding,
    ]
    draw.rounded_rectangle(
        bg,
        radius=max(5, padding * 2),
        fill=(14, 16, 18, 170),
        outline=(255, 255, 255, 45),
        width=1,
    )
    draw.text(
        (tx, ty),
        text,
        fill=fill,
        font=font,
        stroke_width=max(1, padding // 3),
        stroke_fill=stroke_fill,
    )


def _normalize_rotation(rotation_degrees: int) -> int:
    """Return a rotation supported by both bitmap and point transforms."""
    rotation = int(rotation_degrees or 0) % 360
    return rotation if rotation in (0, 90, 180, 270) else 0


def _transform_point(
    x: float,
    y: float,
    width: int,
    height: int,
    rotation_degrees: int,
    zoom: float,
) -> tuple[float, float] | None:
    """Transform an unrotated image-space point into final view coordinates."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation == 0:
        rx, ry = x, y
        rotated_width, rotated_height = width, height
    elif rotation == 90:
        rx, ry = height - 1 - y, x
        rotated_width, rotated_height = height, width
    elif rotation == 180:
        rx, ry = width - 1 - x, height - 1 - y
        rotated_width, rotated_height = width, height
    elif rotation == 270:
        rx, ry = y, width - 1 - x
        rotated_width, rotated_height = height, width
    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    crop_width = max(1, int(rotated_width / zoom))
    crop_height = max(1, int(rotated_height / zoom))
    left = max(0, (rotated_width - crop_width) // 2)
    top = max(0, (rotated_height - crop_height) // 2)
    return rx - left, ry - top


def _rotated_heading(heading: float | None, rotation_degrees: int) -> float | None:
    """Rotate robot heading to match the final clockwise map rotation."""
    if heading is None:
        return None
    return (heading - _normalize_rotation(rotation_degrees)) % 360


def decompress_map(compressed: bytes) -> bytes:
    """Decompress map grid data using zlib.

    Args:
        compressed: Raw compressed bytes from the robot (zlib format, header 78 01).

    Returns:
        Decompressed bytes containing protobuf-wrapped pixel varints.
    """
    if not compressed:
        return b""

    # Try zlib auto-detect (wbits=47 handles zlib, gzip, and raw)
    try:
        return zlib.decompress(compressed, 47)
    except zlib.error:
        pass

    # Try zlib default
    try:
        return zlib.decompress(compressed)
    except zlib.error:
        pass

    # Try raw deflate
    try:
        return zlib.decompress(compressed, -15)
    except zlib.error:
        pass

    _LOGGER.warning(
        "Could not decompress map data (%d bytes), using raw", len(compressed)
    )
    return compressed


def _decode_packed_varints(data: bytes) -> list[int]:
    """Decode protobuf packed repeated varint field from decompressed map data.

    The decompressed data starts with a protobuf field header:
      byte 0: 0x0a (field 1, wire type 2 = length-delimited)
      bytes 1-3: varint length of the packed data

    After the header, the remaining bytes are packed varint pixel values.

    Args:
        data: Decompressed bytes from decompress_map().

    Returns:
        List of integer pixel values.
    """
    if len(data) < 4:
        return []

    # Skip protobuf header: field tag (1 byte) + length varint (variable)
    pos = 0
    if data[0] == 0x0A:  # field 1, wire type 2
        pos = 1
        # Skip the length varint
        while pos < len(data) and data[pos] & 0x80:
            pos += 1
        pos += 1  # skip the final byte of the length varint
    # else: try decoding from the start (no header)

    pixels: list[int] = []
    while pos < len(data):
        val = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        pixels.append(val)

    return pixels


def lookup_room_at_grid(
    compressed: bytes,
    width: int,
    height: int,
    grid_x: float,
    grid_y: float,
) -> tuple[int, str]:
    """Look up the room_id at a grid pixel coordinate.

    Returns (room_id, description) where description is one of:
      "room_N" for a valid room, "(empty)" for val=0,
      "(unassigned)" for 0x20/0x28, "(out_of_bounds)" if off grid.
    """
    px = int(grid_x)
    py = int(grid_y)
    if px < 0 or px >= width or py < 0 or py >= height:
        return (-1, f"(out_of_bounds: {px},{py} vs {width}x{height})")

    decompressed = decompress_map(compressed)
    if not decompressed:
        return (-1, "(no_data)")
    pixels = _decode_packed_varints(decompressed)

    idx = py * width + px
    if idx >= len(pixels):
        return (-1, f"(idx_overflow: {idx} >= {len(pixels)})")

    val = pixels[idx]
    if val == 0:
        return (0, "(empty)")
    if val in (0x20, 0x28):
        return (0, "(unassigned)")
    room_id = val >> 8
    ptype = val & 0xFF
    wall = " wall" if ptype & 0x10 else ""
    return (room_id, f"room_{room_id}{wall}")


def room_label_points(
    compressed: bytes,
    width: int,
    height: int,
    room_names: dict[int, str] | None,
) -> list[tuple[str, float, float, float, float]]:
    """Return room label centre points and room bounds in unflipped grid coordinates."""
    if not compressed or width <= 0 or height <= 0 or not room_names:
        return []

    decompressed = decompress_map(compressed)
    if not decompressed:
        return []

    pixels = _decode_packed_varints(decompressed)
    expected = width * height
    if len(pixels) < expected:
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}
    room_bounds: dict[int, tuple[int, int, int, int]] = {}
    for i, val in enumerate(pixels):
        if val == 0 or val in (0x20, 0x28):
            continue
        room_id = val >> 8
        ptype = val & 0xFF
        if room_id not in room_names or ptype & 0x10:
            continue
        x = i % width
        y = i // width
        room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
        room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
        room_count[room_id] = room_count.get(room_id, 0) + 1
        min_x, min_y, max_x, max_y = room_bounds.get(room_id, (x, y, x, y))
        room_bounds[room_id] = (
            min(min_x, x),
            min(min_y, y),
            max(max_x, x),
            max(max_y, y),
        )

    points: list[tuple[str, float, float, float, float]] = []
    for room_id, name in room_names.items():
        if not name or room_id not in room_count:
            continue
        min_x, min_y, max_x, max_y = room_bounds[room_id]
        points.append((
            name,
            room_sum_x[room_id] / room_count[room_id],
            room_sum_y[room_id] / room_count[room_id],
            max_x - min_x + 1,
            max_y - min_y + 1,
        ))
    return points


def _render_floor_pixels(
    pixels: list[int],
    width: int,
    height: int,
    room_names: dict[int, str] | None,
    room_types: dict[int, int] | None,
    carpet_map_image: bytes | None = None,
    room_materials: dict[object, object] | None = None,
    carpet_zones: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
) -> tuple[
    Image.Image,
    dict[int, int],
    dict[int, int],
    dict[int, int],
    dict[int, tuple[int, int, int, int]],
]:
    """Render map pixels at final resolution and return room label metadata."""
    from PIL import Image

    scale = MAP_RENDER_SCALE
    img = Image.new("RGBA", (width * scale, height * scale), COLOR_UNKNOWN)
    px = img.load()
    room_bounds = _room_bounds_for_pixels(pixels, width)
    carpet_mask = _carpet_mask_from_image(carpet_map_image, img.size)
    carpet_mask_px = carpet_mask.load() if carpet_mask is not None else None
    carpet_zone_mask = _carpet_zone_mask_from_overrides(
        carpet_zones,
        room_names,
        room_bounds,
        width,
        height,
        scale,
    )
    carpet_zone_mask_px = (
        carpet_zone_mask.load() if carpet_zone_mask is not None else None
    )

    room_sum_x: dict[int, int] = {}
    room_sum_y: dict[int, int] = {}
    room_count: dict[int, int] = {}

    for i, val in enumerate(pixels):
        x = i % width
        y = i // width
        left = x * scale
        top = (height - 1 - y) * scale

        if val == 0:
            continue
        if val == 0x20:
            _paint_solid_cell(px, left, top, scale, COLOR_UNASSIGNED_FLOOR)
            continue
        if val == 0x28:
            _paint_solid_cell(px, left, top, scale, COLOR_UNASSIGNED_OBSTACLE)
            continue

        room_id = val >> 8
        ptype = val & 0xFF
        _paint_room_cell(
            px,
            left,
            top,
            scale,
            room_id,
            ptype,
            room_names,
            room_types,
            room_materials,
            carpet_mask_px,
            carpet_zone_mask_px,
        )

        if room_names and room_id in room_names and not (ptype & 0x10):
            room_sum_x[room_id] = room_sum_x.get(room_id, 0) + x
            room_sum_y[room_id] = room_sum_y.get(room_id, 0) + y
            room_count[room_id] = room_count.get(room_id, 0) + 1

    return img, room_sum_x, room_sum_y, room_count, room_bounds


def _darken(color: tuple[int, int, int], amount: int = 80) -> tuple[int, int, int]:
    """Darken an RGB color by subtracting from each channel."""
    return (
        max(0, color[0] - amount),
        max(0, color[1] - amount),
        max(0, color[2] - amount),
    )


def _draw_dock(
    draw: ImageDraw.ImageDraw,
    dock_x: int,
    dock_y: int,
    size: int = 6,
) -> None:
    """Draw a small Flow-style dock marker at the given image coordinates."""
    half_w = max(5, size // 2)
    half_h = max(4, int(size * 0.42))
    outline_width = max(1, size // 12)
    draw.rounded_rectangle(
        [dock_x - half_w, dock_y - half_h, dock_x + half_w, dock_y + half_h],
        radius=max(3, size // 5),
        fill=(248, 249, 250, 245),
        outline=(90, 96, 104, 220),
        width=outline_width,
    )
    slot_h = max(2, size // 7)
    draw.rounded_rectangle(
        [
            dock_x - int(half_w * 0.55),
            dock_y - slot_h,
            dock_x + int(half_w * 0.55),
            dock_y + slot_h,
        ],
        radius=max(1, slot_h),
        fill=(38, 42, 48, 235),
    )
    led = max(1, size // 10)
    draw.ellipse(
        [
            dock_x + int(half_w * 0.58) - led,
            dock_y + int(half_h * 0.35) - led,
            dock_x + int(half_w * 0.58) + led,
            dock_y + int(half_h * 0.35) + led,
        ],
        fill=(80, 190, 120, 255),
    )


def _draw_robot(
    draw: ImageDraw.ImageDraw,
    rx: int,
    ry: int,
    heading: float | None,
    radius: int,
) -> None:
    """Draw a small Narwal Flow-style robot marker.

    Args:
        draw: PIL ImageDraw instance.
        rx: Robot X in image coordinates (already Y-flipped).
        ry: Robot Y in image coordinates (already Y-flipped).
        heading: Heading in degrees (0=right, 90=up in world coords).
            None to draw circle only without heading arrow.
        radius: Circle radius in pixels.
    """
    import math

    outline_width = max(1, radius // 8)

    if heading is None:
        heading = 90
    rad = math.radians(heading)
    front_dx = math.cos(rad)
    front_dy = -math.sin(rad)
    side_dx = -front_dy
    side_dy = front_dx

    def point(forward: float, side: float) -> tuple[float, float]:
        return (
            rx + (front_dx * forward) + (side_dx * side),
            ry + (front_dy * forward) + (side_dy * side),
        )

    # Soft shadow/halo for contrast on bright room colours.
    draw.ellipse(
        [
            rx - radius - 2,
            ry - radius - 2,
            rx + radius + 2,
            ry + radius + 2,
        ],
        fill=(0, 0, 0, 55),
    )

    # Main circular body.
    draw.ellipse(
        [rx - radius, ry - radius, rx + radius, ry + radius],
        fill=(246, 247, 248, 245),
        outline=(96, 102, 110, 210),
        width=outline_width,
    )

    # Flow-style front sensor bar, rotated with heading.
    bar_half_width = radius * 0.38
    bar_half_height = max(2, radius * 0.13)
    bar = [
        point(radius * 0.58 - bar_half_height, -bar_half_width),
        point(radius * 0.58 + bar_half_height, -bar_half_width),
        point(radius * 0.58 + bar_half_height, bar_half_width),
        point(radius * 0.58 - bar_half_height, bar_half_width),
    ]
    draw.polygon(bar, fill=(30, 33, 38, 245))
    lens_radius = max(1, radius // 8)
    for side in (-bar_half_width * 0.55, bar_half_width * 0.55):
        lx, ly = point(radius * 0.58, side)
        draw.ellipse(
            [lx - lens_radius, ly - lens_radius, lx + lens_radius, ly + lens_radius],
            fill=(85, 160, 225, 255),
        )

    # Top dial and subtle heading notch.
    dial_radius = max(2, radius // 3)
    draw.ellipse(
        [rx - dial_radius, ry - dial_radius, rx + dial_radius, ry + dial_radius],
        fill=(232, 234, 236, 255),
        outline=(150, 155, 160, 230),
        width=max(1, outline_width // 2),
    )
    notch = [
        point(radius * 0.95, 0),
        point(radius * 0.55, -radius * 0.16),
        point(radius * 0.55, radius * 0.16),
    ]
    draw.polygon(notch, fill=(255, 255, 255, 235))


def render_map_png(
    decompressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
    room_types: dict[int, int] | None = None,
    carpet_map_image: bytes | None = None,
    room_materials: dict[object, object] | None = None,
    carpet_zones: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
) -> bytes:
    """Render decompressed map data as a PNG image.

    Decodes the protobuf-packed varint pixel data and renders each pixel:
      - Value 0: unknown/outside (dark gray)
      - Value 0x20: unassigned floor (light gray)
      - Value 0x28: unassigned obstacle (dark gray)
      - Otherwise: room_id = value >> 8, pixel_type = value & 0xFF
        - pixel_type & 0x10: wall/border (darker shade of room color)
        - else: floor (room color)

    Args:
        decompressed: Decompressed map bytes (from decompress_map).
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position in grid coordinates (optional).
        robot_y: Robot Y position in grid coordinates (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position in grid coordinates (optional).
        dock_y: Dock Y position in grid coordinates (optional).
        room_names: Mapping of room_id to display name (optional).
        room_types: Mapping of room_id to Narwal RoomType enum (optional).
        carpet_map_image: Narwal carpet debug PNG bytes (optional).
        room_materials: Optional room_id/name -> material overrides.
        carpet_zones: Optional room-relative carpet/rug zones.

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    if not decompressed or width <= 0 or height <= 0:
        return b""

    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return b""

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        _LOGGER.warning(
            "Map has %d pixels, expected %d (%dx%d) — padding",
            len(pixels), expected, width, height,
        )
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    scale = MAP_RENDER_SCALE
    img, room_sum_x, room_sum_y, room_count, room_bounds = _render_floor_pixels(
        pixels,
        width,
        height,
        room_names,
        room_types,
        carpet_map_image,
        room_materials,
        carpet_zones,
    )

    draw = ImageDraw.Draw(img)
    scaled_height = height * scale

    # Draw room labels at flipped centroids
    if room_names:
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            min_x, min_y, max_x, max_y = room_bounds[rid]
            max_label_width = int((max_x - min_x + 1) * scale * 0.82)
            max_label_height = int((max_y - min_y + 1) * scale * 0.46)
            font = _fit_label_font(
                ImageFont,
                name,
                ROOM_LABEL_FONT_SCALE * scale,
                max_width=max_label_width,
                max_height=max_label_height,
            )
            cx = _scaled_coord(
                room_sum_x[rid] // room_count[rid], scale, img.width
            )
            cy = _scaled_coord(
                height - 1 - (room_sum_y[rid] // room_count[rid]),
                scale,
                img.height,
            )
            _draw_label(
                draw,
                (cx, cy),
                name,
                font=font,
                padding=max(3, scale),
            )

    # Draw dock position (before robot so robot draws on top)
    # Flip dock Y to match the flipped image
    marker_radius = max(3 * scale, min(width, height) * scale // 80)

    if (
        dock_x is not None
        and dock_y is not None
        and math.isfinite(dock_x)
        and math.isfinite(dock_y)
        and 0 <= dock_x < width
        and 0 <= dock_y < height
    ):
        dock_size = marker_radius * 2
        _draw_dock(
            draw,
            _scaled_coord(dock_x, scale, img.width),
            _scaled_coord(height - 1 - dock_y, scale, img.height),
            dock_size,
        )

    # Draw robot position (flip Y) — skip if out of bounds
    if (
        robot_x is not None
        and robot_y is not None
        and math.isfinite(robot_x)
        and math.isfinite(robot_y)
        and 0 <= robot_x < width
        and 0 <= robot_y < height
    ):
        rx = _scaled_coord(robot_x, scale, img.width)
        ry = _scaled_coord(height - 1 - robot_y, scale, scaled_height)
        _draw_robot(draw, rx, ry, robot_heading, marker_radius)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_base_map(
    compressed: bytes,
    width: int,
    height: int,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
    obstacles: list | None = None,
    origin_x: int = 0,
    origin_y: int = 0,
    show_obstacle_labels: bool = True,
    show_room_labels: bool = True,
    show_dock: bool = True,
    room_types: dict[int, int] | None = None,
    carpet_map_image: bytes | None = None,
    room_materials: dict[object, object] | None = None,
    carpet_zones: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
) -> Image.Image | None:
    """Render the static floor plan as a PIL Image (no robot overlay).

    Returns a PIL Image that can be cached and reused across frames.
    Only needs to be re-rendered when the static map data changes.

    Args:
        obstacles: List of ObstacleInfo objects to render (optional).
        origin_x: Map origin X offset for obstacle coordinate transform.
        origin_y: Map origin Y offset for obstacle coordinate transform.
        show_obstacle_labels: Whether to draw furniture/obstacle labels.
        show_room_labels: Whether to draw room labels into the base image.
        show_dock: Whether to draw the dock into the base image.
        room_types: Mapping of room_id to Narwal RoomType enum (optional).
        carpet_map_image: Narwal carpet debug PNG bytes (optional).
        room_materials: Optional room_id/name -> material overrides.
        carpet_zones: Optional room-relative carpet/rug zones.
    """
    try:
        from PIL import ImageDraw, ImageFont
    except ImportError:
        _LOGGER.error("Pillow is required for map rendering")
        return None

    decompressed = decompress_map(compressed)
    if not decompressed or width <= 0 or height <= 0:
        return None

    pixels = _decode_packed_varints(decompressed)
    expected = width * height

    if len(pixels) < expected:
        pixels.extend([0] * (expected - len(pixels)))
    elif len(pixels) > expected:
        pixels = pixels[:expected]

    scale = MAP_RENDER_SCALE
    img, room_sum_x, room_sum_y, room_count, room_bounds = _render_floor_pixels(
        pixels,
        width,
        height,
        room_names,
        room_types,
        carpet_map_image,
        room_materials,
        carpet_zones,
    )
    draw = ImageDraw.Draw(img)

    if room_names and show_room_labels:
        for rid, name in room_names.items():
            if not name or rid not in room_count:
                continue
            min_x, min_y, max_x, max_y = room_bounds[rid]
            max_label_width = int((max_x - min_x + 1) * scale * 0.82)
            max_label_height = int((max_y - min_y + 1) * scale * 0.46)
            font = _fit_label_font(
                ImageFont,
                name,
                ROOM_LABEL_FONT_SCALE * scale,
                max_width=max_label_width,
                max_height=max_label_height,
            )
            cx = _scaled_coord(
                room_sum_x[rid] // room_count[rid], scale, img.width
            )
            cy = _scaled_coord(
                height - 1 - (room_sum_y[rid] // room_count[rid]),
                scale,
                img.height,
            )
            _draw_label(
                draw,
                (cx, cy),
                name,
                font=font,
                padding=max(3, scale),
            )

    # Draw obstacle/furniture annotations (static data from get_map field 2.32)
    if obstacles:
        obs_font = _load_font(ImageFont, OBSTACLE_LABEL_FONT_SCALE * scale)
        for obs in obstacles:
            gx, gy = obs.to_grid_coords(origin_x, origin_y)
            # Skip out-of-bounds obstacles
            if gx < 0 or gx >= width or gy < 0 or gy >= height:
                continue
            img_x = _scaled_coord(gx, scale, img.width)
            img_y = _scaled_coord(height - 1 - gy, scale, img.height)
            half_w = max(scale, int(obs.width * scale / 2))
            half_h = max(scale, int(obs.height * scale / 2))
            color = OBSTACLE_COLORS.get(obs.type_id, OBSTACLE_COLOR_DEFAULT)
            draw.rectangle(
                [img_x - half_w, img_y - half_h, img_x + half_w, img_y + half_h],
                outline=color, width=max(1, scale // 2),
            )
            if show_obstacle_labels:
                label = obs.display_name
                bbox = obs_font.getbbox(label)
                th = bbox[3] - bbox[1]
                _draw_label(
                    draw,
                    (img_x, img_y - half_h - th - (3 * scale)),
                    label,
                    fill=color,
                    font=obs_font,
                    padding=max(2, scale),
                )

    if (
        show_dock
        and dock_x is not None
        and dock_y is not None
        and math.isfinite(dock_x)
        and math.isfinite(dock_y)
        and 0 <= dock_x < width
        and 0 <= dock_y < height
    ):
        marker_radius = max(3 * scale, min(width, height) * scale // 80)
        dock_size = marker_radius * 2
        _draw_dock(
            draw,
            _scaled_coord(dock_x, scale, img.width),
            _scaled_coord(height - 1 - dock_y, scale, img.height),
            dock_size,
        )

    return img


def render_overlay(
    base_img: Image.Image,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    trail: list[tuple[float, float]] | None = None,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
    room_labels: list[tuple[str, float, float] | tuple[str, float, float, float, float]]
    | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
) -> bytes:
    """Draw robot position and trail on a copy of the cached base map.

    Args:
        base_img: Cached PIL Image from render_base_map (not modified).
        height: Map height in pixels (for Y-flip).
        robot_x: Robot X in grid coordinates.
        robot_y: Robot Y in grid coordinates.
        robot_heading: Heading in degrees.
        trail: Narwal-native display_map trajectory points in grid coordinates.
        rotation_degrees: Clockwise map rotation in degrees.
        zoom: Centre zoom factor.
        room_labels: Room label centre points and optional bounds in grid coordinates.
        dock_x: Dock X position in grid coordinates.
        dock_y: Dock Y position in grid coordinates.

    Returns:
        PNG bytes of the composited image.
    """
    from PIL import ImageDraw, ImageFont

    img = base_img.copy()
    original_width = img.width
    original_height = img.height
    scale = img.height / height if height > 0 else 1.0
    map_width = original_width / scale if scale > 0 else original_width
    max_grid_segment = max(24.0, min(map_width, height) * 0.18)

    img = _apply_view_transform(img, rotation_degrees, zoom)
    draw = ImageDraw.Draw(img, "RGBA")

    def is_valid_grid_point(grid_x: float, grid_y: float) -> bool:
        return (
            math.isfinite(grid_x)
            and math.isfinite(grid_y)
            and 0 <= grid_x < map_width
            and 0 <= grid_y < height
        )

    def final_point(grid_x: float, grid_y: float) -> tuple[int, int] | None:
        if not is_valid_grid_point(grid_x, grid_y):
            return None
        x = _scaled_coord(grid_x, scale, original_width)
        y = _scaled_coord(height - 1 - grid_y, scale, original_height)
        transformed = _transform_point(
            x, y, original_width, original_height, rotation_degrees, zoom
        )
        if transformed is None:
            return None
        tx, ty = transformed
        return int(round(tx)), int(round(ty))

    # Draw the Narwal-native trajectory, skipping invalid or discontinuous points.
    if trail and len(trail) >= 2:
        trail_width = max(1, int(round(scale)))
        previous_grid: tuple[float, float] | None = None
        previous_point: tuple[int, int] | None = None
        for i in range(len(trail) - 1):
            grid_x, grid_y = trail[i]
            if not is_valid_grid_point(grid_x, grid_y):
                previous_grid = None
                previous_point = None
                continue

            point = final_point(grid_x, grid_y)
            if point is None:
                previous_grid = None
                previous_point = None
                continue

            if previous_grid is not None and previous_point is not None:
                distance = math.hypot(
                    grid_x - previous_grid[0],
                    grid_y - previous_grid[1],
                )
                if distance <= max_grid_segment:
                    draw.line(
                        [previous_point, point],
                        fill=(255, 255, 255, 220),
                        width=trail_width,
                    )

            previous_grid = (grid_x, grid_y)
            previous_point = point

        last_grid_x, last_grid_y = trail[-1]
        if previous_grid is not None and is_valid_grid_point(last_grid_x, last_grid_y):
            last_point = final_point(last_grid_x, last_grid_y)
            if last_point is not None:
                distance = math.hypot(
                    last_grid_x - previous_grid[0],
                    last_grid_y - previous_grid[1],
                )
                if distance <= max_grid_segment:
                    draw.line(
                        [previous_point, last_point],
                        fill=(255, 255, 255, 220),
                        width=trail_width,
                    )

    if room_labels:
        max_font_size = ROOM_LABEL_FONT_SCALE * int(round(scale))
        for label_info in room_labels:
            label, grid_x, grid_y = label_info[:3]
            point = final_point(grid_x, grid_y)
            if point is None:
                continue
            x, y = point
            if -80 <= x <= img.width + 80 and -80 <= y <= img.height + 80:
                max_label_width = None
                max_label_height = None
                if len(label_info) >= 5:
                    max_label_width = int(label_info[3] * scale * 0.82)
                    max_label_height = int(label_info[4] * scale * 0.46)
                font = _fit_label_font(
                    ImageFont,
                    label,
                    max_font_size,
                    max_width=max_label_width,
                    max_height=max_label_height,
                )
                _draw_label(draw, (x, y), label, font, padding=max(4, int(scale)))

    if dock_x is not None and dock_y is not None:
        point = final_point(dock_x, dock_y)
        if point is not None:
            dx, dy = point
            if 0 <= dx < img.width and 0 <= dy < img.height:
                robot_radius = max(5 * int(round(scale)), min(img.width, img.height) // 64)
                dock_size = robot_radius * 2
                _draw_dock(draw, dx, dy, dock_size)

    # Draw robot
    if robot_x is not None and robot_y is not None:
        point = final_point(robot_x, robot_y)
        if point is not None:
            rx, ry = point
            if 0 <= rx < img.width and 0 <= ry < img.height:
                radius = max(5 * int(round(scale)), min(img.width, img.height) // 64)
                _draw_robot(
                    draw,
                    rx,
                    ry,
                    _rotated_heading(robot_heading, rotation_degrees),
                    radius,
                )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _apply_view_transform(
    img: Image.Image,
    rotation_degrees: int = 0,
    zoom: float = 1.0,
) -> Image.Image:
    """Apply final map rotation and centre zoom."""
    rotation = _normalize_rotation(rotation_degrees)
    if rotation:
        img = img.rotate(-rotation, expand=True)

    zoom = max(1.0, min(2.0, float(zoom or 1.0)))
    if zoom <= 1.0:
        return img

    crop_width = max(1, int(img.width / zoom))
    crop_height = max(1, int(img.height / zoom))
    left = max(0, (img.width - crop_width) // 2)
    top = max(0, (img.height - crop_height) // 2)
    return img.crop((left, top, left + crop_width, top + crop_height))


def render_map_from_compressed(
    compressed: bytes,
    width: int,
    height: int,
    robot_x: float | None = None,
    robot_y: float | None = None,
    robot_heading: float | None = None,
    dock_x: float | None = None,
    dock_y: float | None = None,
    room_names: dict[int, str] | None = None,
    room_types: dict[int, int] | None = None,
    carpet_map_image: bytes | None = None,
    room_materials: dict[object, object] | None = None,
    carpet_zones: list[dict[str, object]] | tuple[dict[str, object], ...] | None = None,
) -> bytes:
    """Decompress and render map data in one step (legacy interface).

    Args:
        compressed: Compressed map bytes from the robot.
        width: Map width in pixels.
        height: Map height in pixels.
        robot_x: Robot X position (optional).
        robot_y: Robot Y position (optional).
        robot_heading: Robot heading in degrees (optional).
        dock_x: Dock X position (optional).
        dock_y: Dock Y position (optional).
        room_names: Mapping of room_id to display name (optional).
        room_types: Mapping of room_id to Narwal RoomType enum (optional).
        carpet_map_image: Narwal carpet debug PNG bytes (optional).
        room_materials: Optional room_id/name -> material overrides.
        carpet_zones: Optional room-relative carpet/rug zones.

    Returns:
        PNG image as bytes, or empty bytes on failure.
    """
    decompressed = decompress_map(compressed)
    return render_map_png(
        decompressed, width, height, robot_x, robot_y, robot_heading,
        dock_x, dock_y, room_names, room_types, carpet_map_image,
        room_materials, carpet_zones,
    )
