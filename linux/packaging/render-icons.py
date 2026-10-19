"""Renders the launcher icon as PNGs for the hicolor theme (build time only).

usage: python3 packaging/render-icons.py ICON.svg OUTPUT_DIR [SIZE ...]
writes OUTPUT_DIR/<n>x<n>/apps/<icon name>.png for each size (default 16 24 32 48 64 128 256).

Docks and app grids that do not load SVG (and the 16/24 px menu sizes, which look
better pre-rendered) use these. librsvg (gi.repository.Rsvg) renders the SVG when it
is installed; otherwise the same design is drawn with cairo, so the build only
needs pycairo (python3-gi-cairo, already a runtime dependency). The tray draws its
own icon (multicred/tray/icon.py); this is only for launchers.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import cairo

SIZES = (16, 24, 32, 48, 64, 128, 256)
DESIGN = 128.0  # the SVG's viewBox


def _rsvg_renderer(svg: Path):
    try:
        import gi
        gi.require_version("Rsvg", "2.0")
        gi.require_foreign("cairo")  # pycairo contexts into librsvg (python3-gi-cairo)
        from gi.repository import Rsvg
    except (ImportError, ValueError):
        return None
    handle = Rsvg.Handle.new_from_file(str(svg))

    def draw(context: cairo.Context, size: int) -> None:
        viewport = Rsvg.Rectangle()
        viewport.x, viewport.y, viewport.width, viewport.height = 0, 0, size, size
        handle.render_document(context, viewport)
    return draw


def _rounded(context: cairo.Context, x: float, y: float, width: float, height: float, radius: float) -> None:
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


def _hex(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))


def draw_with_cairo(context: cairo.Context, size: int) -> None:
    """The SVG's design: indigo rounded square, orange and teal tiles, a white "M"."""
    context.save()
    context.scale(size / DESIGN, size / DESIGN)
    _rounded(context, 8, 8, 112, 112, 26)
    context.set_source_rgb(*_hex("#5856D6"))
    context.fill()
    for x, color in ((30, "#D9770B"), (54, "#0E8A9E")):
        _rounded(context, x, x, 44, 44, 11)
        context.set_source_rgba(*_hex(color), 0.95)
        context.fill()
    context.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    context.set_font_size(46)
    extents = context.text_extents("M")
    context.move_to(64 - extents.x_advance / 2, 80)
    context.set_source_rgb(1, 1, 1)
    context.show_text("M")
    context.restore()


def render(svg: Path, output: Path, sizes=SIZES) -> list[Path]:
    draw = _rsvg_renderer(svg) or draw_with_cairo
    written = []
    for size in sizes:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        draw(cairo.Context(surface), size)
        path = output / f"{size}x{size}" / "apps" / f"{svg.stem}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        surface.write_to_png(str(path))
        written.append(path)
    return written


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    sizes = [int(n) for n in argv[2:]] or SIZES
    for path in render(Path(argv[0]), Path(argv[1]), sizes):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
