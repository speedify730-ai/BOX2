#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""PangoCairo Myanmar subtitle rasterizer.

Called by processor.py with the Debian system Python because python3-gi is
provided by apt rather than the /usr/local Python used by the application.
Pango uses HarfBuzz for glyph shaping and FriBidi for bidirectional layout.
"""
import argparse
import html
import os
import sys

import cairo
import gi

# PyGObject treats Cairo as a "foreign" type.  PangoCairo.create_layout()
# receives a pycairo.Context, so the Cairo foreign type must be registered
# before PangoCairo is imported.  Without this, PyGObject raises:
#   KeyError: could not find foreign type Context
# This is especially common in slim Debian/Python images.
try:
    gi.require_foreign("cairo")
except Exception:
    # Older PyGObject releases may not expose require_foreign(); importing
    # pycairo above is still sufficient when python3-gi-cairo is installed.
    pass

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo


def color_value(value, default):
    value = (value or default).strip().lstrip("#")
    if len(value) == 8:
        value = value[-6:]
    if len(value) != 6:
        value = default.lstrip("#")
    try:
        return tuple(int(value[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        value = default.lstrip("#")
        return tuple(int(value[i:i+2], 16) / 255.0 for i in (0, 2, 4))


def make_layout(context, text, font, size_px, max_width):
    layout = PangoCairo.create_layout(context)
    desc = Pango.FontDescription()
    desc.set_family(font)
    # Pango font sizes are points; Cairo/Pango's normal 96-DPI mapping makes
    # 1 px ~= 0.75 pt.
    desc.set_size(int(max(8.0, size_px) * 0.75 * Pango.SCALE))
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    if max_width > 0:
        layout.set_width(int(max_width * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_single_paragraph_mode(False)
    return layout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--font", required=True)
    ap.add_argument("--size", type=float, required=True)
    ap.add_argument("--fill", default="#FFFFFF")
    ap.add_argument("--stroke", default="#000000")
    ap.add_argument("--stroke-width", type=float, default=2)
    ap.add_argument("--max-width", type=int, default=1600)
    ap.add_argument("--padding", type=int, default=14)
    args = ap.parse_args()

    text = args.text.replace("\r\n", "\n").replace("\r", "\n")
    # Never interpret subtitle text as Pango markup. Literal braces, angle
    # brackets and ampersands are part of the subtitle itself.
    text = html.unescape(text)

    # First pass on a tiny surface to obtain the logical extents.
    scratch = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    cr = cairo.Context(scratch)
    layout = make_layout(cr, text, args.font, args.size, args.max_width)
    ink, logical = layout.get_pixel_extents()

    stroke = max(0, int(round(args.stroke_width)))
    pad = max(2, int(args.padding)) + stroke + 2
    width = max(2, logical.width + pad * 2)
    height = max(2, logical.height + pad * 2)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cr.set_operator(cairo.OPERATOR_OVER)

    layout = make_layout(cr, text, args.font, args.size, max(1, width - pad * 2))
    _, logical = layout.get_pixel_extents()
    x = pad + max(0, (width - pad * 2 - logical.width) // 2)
    y = pad + max(0, (height - pad * 2 - logical.height) // 2)
    cr.move_to(x, y)

    fr, fg, fb = color_value(args.fill, "#FFFFFF")
    sr, sg, sb = color_value(args.stroke, "#000000")

    # layout_path asks PangoCairo to shape the Unicode string first. Cairo
    # then strokes/fills the resulting glyph outlines, so combining marks
    # cannot be misplaced by a naive character-by-character renderer.
    PangoCairo.layout_path(cr, layout)
    if stroke:
        cr.set_line_width(stroke * 2.0)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(sr, sg, sb, 1.0)
        cr.stroke_preserve()
    cr.set_source_rgba(fr, fg, fb, 1.0)
    cr.fill()

    surface.write_to_png(args.output)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"pango_render error: {exc}", file=sys.stderr)
        raise
