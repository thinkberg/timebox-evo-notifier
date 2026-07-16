#!/usr/bin/env python3
"""Runnable checks for the pure logic that would otherwise break silently:
encoder patch, font packing, request validation, overlay stamping, and the
KDE bridge's unread bookkeeping.

No framework — run it: .venv/bin/python test_render.py
"""

from array import array

from timebox_bridge import CLOSED_BY_CALL, CLOSED_DISMISSED, CLOSED_EXPIRED, UnreadTracker
from timebox_daemon import (
    _RINGS,
    _bar_frame_stereo,
    _bs_to_box,
    _clock_payload,
    _frame_heights,
    _is_frame_ack,
    _tunnel,
    _tunnel_frame,
    _tunnel_frame_stereo,
    _vis,
    _vis_freqs,
    _wave,
    _wave_frame,
    _wave_frame_stereo,
    ScrollOverlay,
    StaticOverlay,
    parse_params,
)
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
assert parse_params({"icon": "github"})["icon"] == "github"
assert parse_params({"icon": "junk"})["icon"] == "envelope"
assert parse_params({})["icon"] == "envelope"

# Icon templates: the octocat differs from the envelope, its ear tips
# survive a 2-digit badge, and the count digits land on top.
cat = render_notification(42, (255, 60, 40), (255, 255, 255), (0, 0, 0),
                          icon="github")
env = render_notification(42, (255, 60, 40), (255, 255, 255), (0, 0, 0))
assert cat != env
assert cat[2 * 16 + 2] == cat[2 * 16 + 13] == (255, 60, 40)  # ear tips
assert (255, 255, 255) in cat  # digits stamped inside the face
assert render_notification(1, (9, 9, 9), (2, 2, 2), (0, 0, 0),
                           icon="junk") == render_notification(
                               1, (9, 9, 9), (2, 2, 2), (0, 0, 0))

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

# Tunnel: the 8 rings partition the panel exactly; the first frame lights
# only the outer ring; history reaches the center after 8 frames with
# brightness bleeding out monotonically; silence renders black; only known
# modes pass validation.
assert [len(r) for r in _RINGS] == [60, 52, 44, 36, 28, 20, 12, 4]
assert sorted(i for ring in _RINGS for i in ring) == list(range(256))
_tunnel["hist"].clear()
frame = _tunnel_frame([16] * 16)
assert all(frame[i] != (0, 0, 0) for i in _RINGS[0])
assert all(frame[i] == (0, 0, 0) for i in _RINGS[1])
for _ in range(7):
    frame = _tunnel_frame([16] * 16)
assert frame[_RINGS[7][0]] != (0, 0, 0)  # oldest frame reached the center
bright = [max(frame[ring[0]]) for ring in _RINGS]
assert all(a >= b for a, b in zip(bright, bright[1:]))  # bleed-out
_tunnel["hist"].clear()
assert all(c == (0, 0, 0) for c in _tunnel_frame([0] * 16))
_tunnel["hist"].clear()
_tunnel["offset"] = 0  # a lone lit band revolves in single-pixel steps
lone = [0] * 16
lone[0] = 16


def _lit():
    frame = _tunnel_frame(lone)
    return [j for j, i in enumerate(_RINGS[0]) if frame[i] != (0, 0, 0)]


first = cur = _lit()
for _ in range(9):
    prev, cur = cur, _lit()
    assert cur in (prev, [(j + 1) % 60 for j in prev])
assert cur != first
_tunnel["hist"].clear()
_tunnel["offset"] = 0  # 60 bands: 1:1 pixel:band on the outer ring
h60 = [0] * 60
h60[7] = 16
frame = _tunnel_frame(h60)
assert sum(frame[i] != (0, 0, 0) for i in _RINGS[0]) == 1
assert frame[_RINGS[0][7]] != (0, 0, 0)
_tunnel["hist"].clear()
frame = _tunnel_frame([16] * 60)
assert all(frame[i] != (0, 0, 0) for i in _RINGS[0])
_tunnel["hist"].clear()
assert parse_params({"mode": "tunnel"})["mode"] == "tunnel"
assert "mode" not in parse_params({"mode": "spiral"})
assert parse_params({"spin": 999})["spin"] == 5.0
assert parse_params({"spin": -2})["spin"] == -2.0
assert "spin" not in parse_params({"spin": "fast"})
assert parse_params({"fade": 2})["fade"] == 1.0
assert parse_params({"fade": 0.7})["fade"] == 0.7
assert parse_params({"bands": 999})["bands"] == 60
assert parse_params({"bands": 1})["bands"] == 2
assert "fade" not in parse_params({"fade": "x"})
assert "bands" not in parse_params({"bands": "x"})
assert parse_params({"stereo": 1})["stereo"] is True
assert parse_params({"stereo": False})["stereo"] is False
assert "stereo" not in parse_params({})

# Stereo: the mixdown of identical channels matches the per-channel
# analysis; heights respect the row budget; each bar half stays on its
# side; identical channels give a mirror-symmetric tunnel.
sig = array("h", [1000, 1000, -800, -800] * 600)  # L == R, interleaved
freqs = _vis_freqs(16)
mono_h, _ = _frame_heights(sig, 1.0, freqs, 16, False)
(hl, hr), _ = _frame_heights(sig, 1.0, freqs, 16, True)
assert mono_h == hl
(hl8, _), _ = _frame_heights(sig, 1.0, freqs, 8, True)
assert 0 < max(hl8) <= 8
frame = _bar_frame_stereo([8] + [0] * 15, [0] * 16)
assert frame[15 * 16] != (0, 0, 0) and frame[8 * 16] != (0, 0, 0)  # bottom half
assert all(frame[row * 16] == (0, 0, 0) for row in range(8))  # top half dark
frame = _bar_frame_stereo([0] * 16, [8] + [0] * 15)
assert frame[0] != (0, 0, 0) and frame[7 * 16] != (0, 0, 0)  # top half
assert all(frame[row * 16] == (0, 0, 0) for row in range(8, 16))
_tunnel["hist"].clear()
_tunnel["offset"] = 0
frame = _tunnel_frame_stereo([16] * 30, [16] * 30)
for j in range(30):  # same frequency sits diametrically opposite
    assert frame[_RINGS[0][(8 + j) % 60]] == frame[_RINGS[0][(38 + j) % 60]]
_tunnel["hist"].clear()

# Wave: scrolling spectrogram. The newest spectrum enters at the right edge
# (dir h) / top row (dir v), band 0 at the bottom/left; history reaches the
# far edge after 16 frames, fading monotonically; silence renders black.
assert parse_params({"mode": "wave"})["mode"] == "wave"
assert parse_params({"dir": "v"})["dir"] == "v"
assert "dir" not in parse_params({"dir": "x"})
assert parse_params({"palette": "heat"})["palette"] == "heat"
assert "palette" not in parse_params({"palette": "neon"})
_vis["dir"], _vis["palette"] = "h", "rainbow"
_wave["hist"].clear()
frame = _wave_frame([16] * 16)
assert all(frame[y * 16 + 15] != (0, 0, 0) for y in range(16))
assert all(frame[y * 16 + x] == (0, 0, 0) for y in range(16) for x in range(15))
lone = [0] * 16
lone[0] = 16  # lowest band sits on the bottom row, like the bars
_wave["hist"].clear()
frame = _wave_frame(lone)
assert frame[15 * 16 + 15] != (0, 0, 0)
assert sum(c != (0, 0, 0) for c in frame) == 1
_wave["hist"].clear()
for _ in range(16):
    frame = _wave_frame([16] * 16)
assert frame[0] != (0, 0, 0)  # oldest column reached the left edge
bright = [max(frame[x]) for x in range(15, -1, -1)]
assert all(a >= b for a, b in zip(bright, bright[1:]))  # fade with age
_vis["dir"] = "v"
_wave["hist"].clear()
frame = _wave_frame([16] * 16)
assert all(frame[x] != (0, 0, 0) for x in range(16))  # newest = top row
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(1, 16) for x in range(16))
_vis["dir"] = "h"
_wave["hist"].clear()
assert all(c == (0, 0, 0) for c in _wave_frame([0] * 16))

# Stereo wave: newest columns at the middle (L at 7, R at 8), each side ages
# outward and stays on its half; identical channels mirror around the middle;
# dir v puts R on top / L below, like the stereo bars.
_wave["hist"].clear()
frame = _wave_frame_stereo([16] * 16, [0] * 16)
assert all(frame[y * 16 + 7] != (0, 0, 0) for y in range(16))
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(16) for x in range(16) if x != 7)
for _ in range(8):
    frame = _wave_frame_stereo([16] * 16, [0] * 16)
assert all(frame[y * 16] != (0, 0, 0) for y in range(16))  # left edge reached
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(16) for x in range(8, 16))  # right half stays dark
_wave["hist"].clear()
for _ in range(8):
    frame = _wave_frame_stereo([16] * 16, [16] * 16)
for y in range(16):
    for x in range(8):
        assert frame[y * 16 + x] == frame[y * 16 + (15 - x)]
_vis["dir"] = "v"
_wave["hist"].clear()
frame = _wave_frame_stereo([0] * 16, [16] * 16)
assert all(frame[7 * 16 + x] != (0, 0, 0) for x in range(16))  # R = row 7
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(16) for x in range(16) if y != 7)
_vis["dir"] = "h"

# Heat palette: loudness only — green quiet, red loud, black silent.
_vis["palette"] = "heat"
_wave["hist"].clear()
frame = _wave_frame([2] * 8 + [16] * 8)
green, red = frame[15 * 16 + 15], frame[15]
assert green[1] > green[0] and red[0] > red[1]
_wave["hist"].clear()
assert all(c == (0, 0, 0) for c in _wave_frame([0] * 16))
_vis["palette"] = "rainbow"

# A live stereo toggle leaves the other shape in the history: rendering must
# stop at the first mismatched entry, not crash.
_wave["hist"].clear()
_wave_frame([16] * 16)
frame = _wave_frame_stereo([16] * 16, [16] * 16)
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(16) for x in range(16) if x not in (7, 8))
frame = _wave_frame([16] * 16)
assert all(frame[y * 16 + x] == (0, 0, 0)
           for y in range(16) for x in range(15))
_wave["hist"].clear()

# Latency guard: even a worst-case dense tunnel frame must encode small —
# unquantized frames hit 8 BLE image chunks, which made mode changes
# feel sluggish (typical music frames: 5).
for k in range(8):
    frame = _tunnel_frame([(i * 7 + k * 5) % 17 for i in range(60)])
assert (len(_safe_encode_image(frame)) + 137) // 138 <= 6
_tunnel["hist"].clear()
for k in range(16):  # same guard for a dense worst-case wave frame
    frame = _wave_frame([(i * 7 + k * 5) % 17 for i in range(16)])
assert (len(_safe_encode_image(frame)) + 137) // 138 <= 6
_wave["hist"].clear()

# Weather: condition names precipitation and beats the cloud-cover icon;
# a dry (or missing) condition falls back to it; unknowns read as cloudy.
assert _bs_to_box("thunderstorm", "clear-day") == 5
assert _bs_to_box("snow", None) == 8 == _bs_to_box("sleet", None) == _bs_to_box("hail", None)
assert _bs_to_box("rain", "cloudy") == 6
assert _bs_to_box("fog", None) == 9
assert _bs_to_box("dry", "clear-night") == 1
assert _bs_to_box(None, "partly-cloudy-day") == 3
assert _bs_to_box("dry", None) == 3
# The 5F payload carries °C as a signed byte.
assert bytes([0x5F, -3 & 0xFF, 6]) == b"\x5f\xfd\x06"

# Clock sub-views: 45 00 01 layout (node-divoom-timebox-evo), fullscreen
# face, flags in time/weather/temp/date order, then the color.
assert _clock_payload(["time", "weather"], (255, 0, 0)) == bytes(
    [0x45, 0x00, 0x01, 0x00, 1, 1, 0, 0, 255, 0, 0])
p = parse_params({"clock": ["date", "bogus", "time"], "clock_color": [0, 300, -5]})
assert p["clock"] == ["date", "time"] and p["clock_color"] == (0, 255, 0)
assert parse_params({"clock": "junk"})["clock"] == ["time"]  # chars, none valid
assert parse_params({"clock": 5})["clock"] == ["time"]  # not iterable
p = parse_params({"clock_flash": 999, "clock_every": 5})
assert p["clock_flash"] == 300 and p["clock_every"] == 60  # clamped
assert parse_params({"clock_flash": 0})["clock_flash"] == 0  # 0 = off, valid
assert "clock_flash" not in parse_params({"clock_flash": "junk"})

# Frame-ack pacing: recognize the box's 0x44 image ack among the other
# notification frames (bytes as captured live from this firmware).
assert _is_frame_ack(bytes.fromhex("010600044455 4ef10002".replace(" ", "")))
assert not _is_frame_ack(bytes.fromhex("01090004 3355 220000 00b70002".replace(" ", "")))  # chunk echo
assert not _is_frame_ack(bytes.fromhex("010d0004 f755 4e6f6202 070400008902 02".replace(" ", "")))  # heartbeat
assert not _is_frame_ack(b"")  # short frame must not match

# KDE bridge: only allow-listed apps count (case-insensitive); an id is only
# unread once its Notify call has returned; replaces_id means "update", not a
# new notification; dismissing clears it, merely expiring does not.
t = UnreadTracker({"thunderbird"})

t.on_notify(serial=10, app_name="Signal", replaces_id=0)  # not allow-listed
assert t.on_reply(10, 900) is False and t.count == 0

t.on_notify(serial=11, app_name="ThunderBird", replaces_id=0)  # case-insensitive
assert t.on_reply(11, 1) is True and t.count == 1
t.on_notify(serial=12, app_name="Thunderbird", replaces_id=0)
assert t.on_reply(12, 2) is True and t.count == 2

t.on_notify(serial=13, app_name="Thunderbird", replaces_id=2)  # update of id 2
assert t.on_reply(13, 2) is False and t.count == 2

assert t.on_closed(1, CLOSED_EXPIRED) is False and t.count == 2  # still unread
assert t.on_closed(1, CLOSED_DISMISSED) is True and t.count == 1
assert t.on_closed(2, CLOSED_BY_CALL) is True and t.count == 0
assert t.on_closed(2, CLOSED_DISMISSED) is False  # already gone, no re-fire

# Icon policy: octocat only while gitify is the sole unread source; any
# unread mail brings the envelope back (envelope wins); empty = envelope.
t = UnreadTracker({"thunderbird", "gitify"})
assert t.icon == "envelope"
t.on_notify(serial=20, app_name="gitify", replaces_id=0)
assert t.on_reply(20, 5) is True and (t.count, t.icon) == (1, "github")
t.on_notify(serial=21, app_name="Thunderbird", replaces_id=0)
assert t.on_reply(21, 6) is True and (t.count, t.icon) == (2, "envelope")
assert t.on_closed(6, CLOSED_DISMISSED) is True  # mail read: octocat again
assert (t.count, t.icon) == (1, "github")

print("all checks pass")
