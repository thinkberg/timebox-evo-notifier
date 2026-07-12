#!/usr/bin/env python3
"""Runnable checks for the pure logic that would otherwise break silently:
encoder patch, font packing, request validation, overlay stamping.

No framework — run it: .venv/bin/python test_render.py
"""

from timebox_daemon import ScrollOverlay, StaticOverlay, parse_params
from timebox_notify import (
    _orig_encode_image,
    _safe_encode_image,
    normalize_text,
    render_notification,
    text_columns,
    valid_address,
)

# Encoder patch: 256 unique colors must encode (lib alone crashes) with a
# 255-entry palette; frames below the limit must be byte-identical to the
# unpatched encoder.
rainbow = [(i % 256, (i * 7) % 256, (i * 13) % 256) for i in range(256)]
assert len(set(rainbow)) == 256
assert _safe_encode_image(rainbow)[11] == 255
normal = render_notification(3, (255, 60, 40), (255, 255, 255), (0, 0, 0))
assert _safe_encode_image(normal) == _orig_encode_image(normal)

# Font: umlaut transliteration, column packing ('I' = 3 cols + 1 gap,
# first column lights rows 0 and 4), unknown chars render as space.
assert normalize_text("Größe 42?") == "GROESSE 42?"
cols = text_columns("I")
assert len(cols) == 4 and cols[0] == 0b10001 and cols[3] == 0
assert text_columns("~") == [0, 0, 0, 0]

assert valid_address("11:22:33:AA:BB:CC")
assert not valid_address("garbage") and not valid_address("")

# Request validation: junk clamps to defaults and never raises.
p = parse_params({"count": "abc", "fps": 999, "brightness": "x",
                  "icon_color": [999, -1, "z"]})
assert p["count"] == 1
assert p["fps"] == 20.0
assert "brightness" not in p
assert p["icon_color"] == (255, 60, 40)
p = parse_params({"brightness": 150, "seconds": 0})
assert p["brightness"] == 100 and p["seconds"] == 1.0

# Overlays: the scroll clears its band to background and reports it is
# still running; the static overlay derives sane row bounds and stamps.
frame = [(9, 9, 9)] * 256
scroll = ScrollOverlay(cols=[0b11111], color=(1, 2, 3), bg=(0, 0, 0))
assert scroll.stamp(frame) is True
assert frame[4 * 16] == (0, 0, 0)  # band row above the glyphs
static = StaticOverlay.from_pixels(normal, (0, 0, 0))
assert 0 <= static.row0 < static.row1 <= 15
frame2 = [(9, 9, 9)] * 256
assert static.stamp(frame2) is True
assert frame2[static.row0 * 16] == (0, 0, 0)

print("all checks pass")
