"""Tray and menu icons: a rounded square in the profile color with its letter in white
(macOS `StatusIcon`). Not a symbolic icon, because the color identifies the profile.
An orange dot in the top-right corner marks an inconsistent state.
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


def appearance(profile: Profile | None) -> tuple[str, str]:
    """(color hex, letter) the icon shows. SF Symbols do not exist on Linux, so a
    symbol icon falls back to the first letter of the profile name."""
    if profile is None:
        return NO_PROFILE
    if profile.icon is not None and profile.icon.kind == "letter" and profile.icon.value.strip():
        letter = profile.icon.value
    else:
        letter = profile.name.strip() or "?"
    return profile.color.hex, letter.strip()[:1].upper()


def _rgb(hex_color: str) -> tuple[float, float, float]:
    value = int(hex_color.lstrip("#"), 16)
    return ((value >> 16) & 0xFF) / 255, ((value >> 8) & 0xFF) / 255, (value & 0xFF) / 255


def _rounded_rect(context, x, y, width, height, radius):
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


def render(color_hex: str, letter: str, size: int, alert: bool = False) -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    context = cairo.Context(surface)
    context.set_source_rgb(*_rgb(color_hex))
    _rounded_rect(context, 1, 1, size - 2, size - 2, size * 0.22)
    context.fill()

    letter = letter[:1].upper()
    if letter:
        context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        context.set_font_size(size * 0.62)
        extents = context.text_extents(letter)
        context.move_to(size / 2 - extents.width / 2 - extents.x_bearing,
                        size / 2 - extents.height / 2 - extents.y_bearing)
        context.set_source_rgb(1, 1, 1)
        context.show_text(letter)

    if alert:
        dot = size * 0.38
        cx, cy = size - dot / 2, dot / 2
        context.set_source_rgb(1, 1, 1)
        context.arc(cx, cy, dot / 2 + 1, 0, 2 * math.pi)
        context.fill()
        context.set_source_rgb(*_rgb(ALERT_HEX))
        context.arc(cx, cy, dot / 2, 0, 2 * math.pi)
        context.fill()
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
def sni_pixmaps(color_hex: str, letter: str, alert: bool = False,
                sizes: tuple[int, ...] = SNI_SIZES) -> list[tuple[int, int, bytes]]:
    """IconPixmap value: one (width, height, ARGB32 bytes) per size; the panel picks one."""
    return [(size, size, _argb_big_endian(render(color_hex, letter, size, alert))) for size in sizes]


@lru_cache(maxsize=64)
def png_bytes(color_hex: str, letter: str, size: int = 16, alert: bool = False) -> bytes:
    """PNG for a dbusmenu `icon-data` property."""
    buffer = io.BytesIO()
    render(color_hex, letter, size, alert).write_to_png(buffer)
    return buffer.getvalue()


def profile_png(profile: Profile, size: int = 16) -> bytes:
    return png_bytes(*appearance(profile), size=size)
