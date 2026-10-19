"""Tray and menu icons: a rounded square in the profile color with its letter or symbol in
white (macOS `StatusIcon`). Not a symbolic icon, because the color identifies the profile.
An orange dot in the top-right corner marks an inconsistent state.

SF Symbols do not exist on Linux, so the eight symbols the profile editor offers
(`IconChooser.symbols`, Components.swift:65) are drawn here with cairo, as outlines in a
24-unit grid. The profile editor draws its badges and icon choices with the same
functions, so the editor's preview is what the panel shows.
"""

from __future__ import annotations

import io
import math
import sys
from functools import lru_cache

import cairo

from ..core.models import Profile, ProfileColor

ALERT_HEX = "#FF9500"  # macOS systemOrange
NO_PROFILE = (ProfileColor.GRAPHITE.hex, "M")
SNI_SIZES = (22, 24, 32, 48)
# Components.swift:65, in the Mac's order.
SYMBOLS = ("briefcase", "house", "building.2", "laptopcomputer", "star", "person", "flask", "globe")


def appearance(profile: Profile | None) -> tuple[str, str]:
    """(color hex, glyph) the icon shows: glyph is one letter or one of SYMBOLS. A symbol
    this port does not draw (another SF Symbol name) falls back to the name's letter."""
    if profile is None:
        return NO_PROFILE
    icon = profile.icon
    if icon is not None and icon.kind == "symbol" and icon.value in SYMBOLS:
        return profile.color.hex, icon.value
    if icon is not None and icon.kind == "letter" and icon.value.strip():
        letter = icon.value
    else:
        letter = profile.name.strip() or "?"
    return profile.color.hex, letter.strip()[:1].upper()


def hex_rgb(hex_color: str) -> tuple[float, float, float]:
    value = int(hex_color.lstrip("#"), 16)
    return ((value >> 16) & 0xFF) / 255, ((value >> 8) & 0xFF) / 255, (value & 0xFF) / 255


def rounded_rect(context, x, y, width, height, radius):
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


# MARK: symbols (24-unit grid, centered on 12,12, drawn as 2-unit outlines)

def _briefcase(c) -> None:
    rounded_rect(c, 3, 7.5, 18, 12.5, 2.2)
    c.stroke()
    c.move_to(9, 7.5)
    c.line_to(9, 5.6)
    c.arc(10.4, 5.6, 1.4, math.pi, 3 * math.pi / 2)
    c.line_to(13.6, 4.2)
    c.arc(13.6, 5.6, 1.4, 3 * math.pi / 2, 0)
    c.line_to(15, 7.5)
    c.stroke()
    c.move_to(3, 13)
    c.line_to(21, 13)
    c.stroke()


def _house(c) -> None:
    c.move_to(2.5, 11.5)
    c.line_to(12, 3.5)
    c.line_to(21.5, 11.5)
    c.stroke()
    c.move_to(5.5, 9.5)
    c.line_to(5.5, 20.5)
    c.line_to(18.5, 20.5)
    c.line_to(18.5, 9.5)
    c.stroke()
    c.move_to(10, 20.5)
    c.line_to(10, 15)
    c.line_to(14, 15)
    c.line_to(14, 20.5)
    c.stroke()


def _building(c) -> None:
    rounded_rect(c, 3.5, 3, 10, 17.5, 1.2)
    c.stroke()
    rounded_rect(c, 13.5, 9, 7, 11.5, 1.2)
    c.stroke()
    for x in (6, 9.4):
        for y in (6, 9.6, 13.2):
            c.rectangle(x, y, 1.9, 1.9)
    for y in (12, 15.4):
        c.rectangle(16.1, y, 1.9, 1.9)
    c.fill()


def _laptop(c) -> None:
    rounded_rect(c, 4.5, 5, 15, 10.5, 1.6)
    c.stroke()
    c.move_to(2, 19)
    c.line_to(22, 19)
    c.stroke()


def _star(c) -> None:
    for point in range(10):
        radius = 9.6 if point % 2 == 0 else 4.1
        angle = -math.pi / 2 + point * math.pi / 5
        x, y = 12 + radius * math.cos(angle), 12.8 + radius * math.sin(angle)
        c.line_to(x, y) if point else c.move_to(x, y)
    c.close_path()
    c.stroke()


def _person(c) -> None:
    c.new_sub_path()
    c.arc(12, 7.5, 4, 0, 2 * math.pi)
    c.stroke()
    c.move_to(4.5, 20.5)
    c.curve_to(4.5, 15.8, 7.8, 13.5, 12, 13.5)
    c.curve_to(16.2, 13.5, 19.5, 15.8, 19.5, 20.5)
    c.close_path()
    c.stroke()


def _flask(c) -> None:
    c.move_to(8, 3.5)
    c.line_to(16, 3.5)
    c.stroke()
    c.move_to(9.6, 3.5)
    c.line_to(9.6, 9.5)
    c.line_to(4.4, 18.2)
    c.curve_to(3.8, 19.3, 4.5, 20.5, 5.8, 20.5)
    c.line_to(18.2, 20.5)
    c.curve_to(19.5, 20.5, 20.2, 19.3, 19.6, 18.2)
    c.line_to(14.4, 9.5)
    c.line_to(14.4, 3.5)
    c.stroke()
    c.move_to(7, 15)
    c.line_to(17, 15)
    c.stroke()


def _globe(c) -> None:
    c.new_sub_path()
    c.arc(12, 12, 9, 0, 2 * math.pi)
    c.stroke()
    c.save()
    c.translate(12, 12)
    c.scale(0.45, 1)
    c.new_sub_path()
    c.arc(0, 0, 9, 0, 2 * math.pi)
    c.restore()
    c.stroke()
    c.move_to(3, 12)
    c.line_to(21, 12)
    c.stroke()


_DRAW = {"briefcase": _briefcase, "house": _house, "building.2": _building, "laptopcomputer": _laptop,
         "star": _star, "person": _person, "flask": _flask, "globe": _globe}


def draw_glyph(context, glyph: str, x: float, y: float, size: float, rgb: tuple[float, float, float],
               letter_scale: float = 0.62, symbol_scale: float = 0.64) -> None:
    """Draws `glyph` (one of SYMBOLS, or a letter) centered in the square (x, y, size)."""
    context.save()
    context.set_source_rgb(*rgb)
    if glyph in _DRAW:
        box = size * symbol_scale
        context.translate(x + (size - box) / 2, y + (size - box) / 2)
        context.scale(box / 24, box / 24)
        # At least ~1.1 device pixels, so the outline survives the 22 px panel size.
        context.set_line_width(max(2.1, 1.15 * 24 / box))
        context.set_line_cap(cairo.LINE_CAP_ROUND)
        context.set_line_join(cairo.LINE_JOIN_ROUND)
        _DRAW[glyph](context)
    else:
        letter = glyph[:1].upper()
        if letter:
            context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            options = cairo.FontOptions()
            options.set_antialias(cairo.ANTIALIAS_GRAY)  # no colored subpixel fringes on the white letter
            context.set_font_options(options)
            context.set_font_size(size * letter_scale)
            extents = context.text_extents(letter)
            context.move_to(x + size / 2 - extents.width / 2 - extents.x_bearing,
                            y + size / 2 - extents.height / 2 - extents.y_bearing)
            context.show_text(letter)
    context.restore()


def draw_badge(context, color_hex: str, glyph: str, size: float, alert: bool = False, inset: float = 1,
               radius: float = 0.22, letter_scale: float = 0.62, symbol_scale: float = 0.64) -> None:
    """The rounded square in `color_hex` with `glyph` in white (StatusIcon.swift:33-51);
    the editor uses the ProfileBadge proportions (radius 0.24, letter 0.55)."""
    inner = size - 2 * inset
    context.set_source_rgb(*hex_rgb(color_hex))
    rounded_rect(context, inset, inset, inner, inner, size * radius)
    context.fill()
    draw_glyph(context, glyph, inset, inset, inner, (1, 1, 1), letter_scale, symbol_scale)
    if alert:
        dot = size * 0.38
        cx, cy = size - dot / 2, dot / 2
        context.set_source_rgb(1, 1, 1)
        context.arc(cx, cy, dot / 2 + 1, 0, 2 * math.pi)
        context.fill()
        context.set_source_rgb(*hex_rgb(ALERT_HEX))
        context.arc(cx, cy, dot / 2, 0, 2 * math.pi)
        context.fill()


def render(color_hex: str, glyph: str, size: int, alert: bool = False) -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    draw_badge(cairo.Context(surface), color_hex, glyph, size, alert)
    surface.flush()
    return surface


def _argb_big_endian(surface: cairo.ImageSurface) -> bytes:
    """SNI IconPixmap wants non-premultiplied ARGB32 in network byte order; cairo
    stores premultiplied ARGB32 as native-endian 32-bit words."""
    width, height, stride = surface.get_width(), surface.get_height(), surface.get_stride()
    data = bytes(surface.get_data())
    out = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            o = y * stride + x * 4
            word = int.from_bytes(data[o:o + 4], sys.byteorder)
            a, r, g, b = word >> 24, (word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF
            if 0 < a < 255:
                r, g, b = (min(255, (c * 255 + a // 2) // a) for c in (r, g, b))
            i = (y * width + x) * 4
            out[i:i + 4] = bytes((a, r, g, b))
    return bytes(out)


@lru_cache(maxsize=64)
def sni_pixmaps(color_hex: str, glyph: str, alert: bool = False,
                sizes: tuple[int, ...] = SNI_SIZES) -> list[tuple[int, int, bytes]]:
    """IconPixmap value: one (width, height, ARGB32 bytes) per size; the panel picks one."""
    return [(size, size, _argb_big_endian(render(color_hex, glyph, size, alert))) for size in sizes]


@lru_cache(maxsize=64)
def png_bytes(color_hex: str, glyph: str, size: int = 16, alert: bool = False) -> bytes:
    """PNG for a dbusmenu `icon-data` property."""
    buffer = io.BytesIO()
    render(color_hex, glyph, size, alert).write_to_png(buffer)
    return buffer.getvalue()


def profile_png(profile: Profile, size: int = 16) -> bytes:
    return png_bytes(*appearance(profile), size=size)
