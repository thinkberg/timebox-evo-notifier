#!/usr/bin/env python3
"""Hold BLE control + A2DP audio links to the TimeBox and serve notifications.

The box tolerates its links being held indefinitely (that's how the phone app
works), but link setup/teardown is a minefield of races with its power
management. So: connect once, hold forever, and take notification requests
over a FIFO.

Run:      TIMEBOX_ADDRESS=<box-mac> python timebox_daemon.py
Notify:   echo '{"count": 3}' > $XDG_RUNTIME_DIR/timebox.fifo

Accepted JSON keys (all optional): text "..." (scrolls instead of the
envelope icon; fps sets scroll speed), count, icon_color [r,g,b],
number_color [r,g,b], background [r,g,b], brightness 0-100,
sound <path>, silent true/false. visualizer true streams a 16-band
spectrum of the system audio (endless — capturing only while audio
plays — or `seconds` if given); visualizer false stops it and hands
the panel back to the clock. mode picks
the look: "bars" (default) or "tunnel" — concentric spectrum rings
flowing inward, colors fading with age. Tunnel knobs: spin (rotation
in border px/frame; negative reverses, 0 stops), fade (per-ring decay,
0-1), bands (analyzer width, 2-60). stereo true analyzes L/R apart:
bars mirror bottom (L) / top (R) at half height, the tunnel splits
into left/right semicircles. All switchable while running.
clock ["time","weather","temp","date"] pins which sub-views the box's
clock cycles through (clock_color sets its color); shown immediately
when idle, and replayed whenever the daemon restores the clock.
Each view is a full-screen page the box cycles through (~15 s each);
the weather page is an animated scene. Default from TIMEBOX_CLOCK
(comma-separated), falling back to time,weather.
clock_flash/clock_every (seconds) let the clock interrupt a running
visualizer: flash seconds of clock every every seconds (defaults 30
and 600; flash 0 turns it off).
Notifications arriving while it runs are
rendered on top of the frame over an opaque background band.

Bad values are clamped or replaced with defaults — a malformed request
must never cost us the BLE link.
"""

import array
import asyncio
import colorsys
import json
import math
import os
import signal
import stat
import subprocess
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from functools import lru_cache

from bleak.exc import BleakError

from timebox_notify import (
    DEFAULT_ADDRESS,
    DEFAULT_SOUND,
    TEXT_Y,
    bring_up_audio,
    connect_le,
    play_sound,
    render_notification,
    scroll_text,
    start_agent,
    text_columns,
    valid_address,
)

RGB = tuple[int, int, int]

# One writer at a time on the panel: an image transfer is a SEQUENCE of
# 138-byte GATT writes; a foreign packet interleaved mid-sequence breaks
# the firmware's reassembly (e.g. brightness during a visualizer frame).
_panel_lock = asyncio.Lock()


def _le_alive(client) -> bool:
    # ponytail: divoom_protocol exposes no public "connected" property;
    # keep the private-attribute poke in this one place.
    return client._ble is not None and client._ble.is_connected


# The LE client is shared between the FIFO loop and the visualizer; both
# get it via ensure_le() so only ever one of them dials the link.
_le: dict = {"client": None}
_le_lock = asyncio.Lock()


_le_dialed = False  # first connect logs elsewhere; redials log here


async def ensure_le():
    """The current LE client, reconnecting first if the link is down."""
    global _le_dialed
    async with _le_lock:
        client = _le["client"]
        if client is None or not _le_alive(client):
            if _le_dialed:
                print("LE link lost — reconnecting", flush=True)
            _le["client"] = await connect_le(DEFAULT_ADDRESS)
            if _le_dialed:
                print("LE reconnected", flush=True)
            _le_dialed = True
        return _le["client"]


async def drop_le() -> None:
    """Let go of the LE link so the next ensure_le() redials.

    Needed because a box-side reconnect can leave the client "connected"
    but with services unresolved — every write fails until we let go.
    The teardown runs detached: disconnecting from a powered-off box can
    hang inside BlueZ for minutes (even cancellation gets swallowed), and
    the redial must never wait on the corpse.
    """
    client = _le["client"]
    if client is None:
        return
    _le["client"] = None

    async def _reap() -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=10)
        except Exception:
            pass  # best effort — the link is gone either way

    _spawn(_reap(), "LE teardown")


# --- FIFO request parsing ------------------------------------------------------


def _color(raw: dict, key: str, default: RGB) -> RGB:
    try:
        r, g, b = (min(255, max(0, int(c))) for c in raw[key])
        return (r, g, b)
    except Exception:
        return default


def parse_params(raw: dict) -> dict:
    """Validated/clamped copy of a FIFO request. Junk values become defaults."""
    p = {
        "icon_color": _color(raw, "icon_color", (255, 60, 40)),
        "number_color": _color(raw, "number_color", (255, 255, 255)),
        "background": _color(raw, "background", (0, 0, 0)),
        "silent": bool(raw.get("silent")),
        "sound": str(raw.get("sound", DEFAULT_SOUND)),
    }
    try:
        p["count"] = min(99, max(0, int(raw.get("count", 1))))
    except Exception:
        p["count"] = 1
    try:
        p["fps"] = min(20.0, max(1.0, float(raw.get("fps", 10))))
    except Exception:
        p["fps"] = 10.0
    if "brightness" in raw:
        try:
            p["brightness"] = min(100, max(0, int(raw["brightness"])))
        except Exception:
            pass
    if "text" in raw:
        p["text"] = str(raw["text"])
    if "visualizer" in raw:
        p["visualizer"] = bool(raw["visualizer"])
    if "seconds" in raw:
        try:
            p["seconds"] = min(3600.0, max(1.0, float(raw["seconds"])))
        except Exception:
            pass
    if raw.get("mode") in ("bars", "tunnel"):
        p["mode"] = raw["mode"]
    if "spin" in raw:
        try:  # border px per frame; negative reverses, 0 stops the rotation
            p["spin"] = min(5.0, max(-5.0, float(raw["spin"])))
        except Exception:
            pass
    if "fade" in raw:
        try:  # per-ring brightness decay toward the center
            p["fade"] = min(1.0, max(0.0, float(raw["fade"])))
        except Exception:
            pass
    if "bands" in raw:
        try:  # tunnel analyzer width; the outer ring has 60 px
            p["bands"] = min(60, max(2, int(raw["bands"])))
        except Exception:
            pass
    if "stereo" in raw:
        p["stereo"] = bool(raw["stereo"])
    if "clock" in raw:
        try:
            views = [v for v in raw["clock"] if v in _CLOCK_VIEWS]
        except TypeError:
            views = []
        p["clock"] = views or ["time"]  # a clock with nothing on it helps nobody
        p["clock_color"] = _color(raw, "clock_color", (255, 255, 255))
    if "clock_flash" in raw:
        try:  # seconds the clock interrupts a running visualizer; 0 = never
            p["clock_flash"] = min(300, max(0, int(raw["clock_flash"])))
        except Exception:
            pass
    if "clock_every" in raw:
        try:  # seconds between those interruptions (and weather re-pushes)
            p["clock_every"] = min(3600, max(60, int(raw["clock_every"])))
        except Exception:
            pass
    return p


# --- live audio visualizer -----------------------------------------------------

VIS_RATE = 24000
VIS_FPS = 10
VIS_BANDS = 16
SILENCE_SECS = 10  # digital silence before capture (and KDE's mic icon) pauses
TUNNEL_FADE = 0.92  # per-ring brightness decay toward the center
TUNNEL_SPIN = 0.5  # signal rotation, border px per frame (full turn = 12 s at 10 fps)
# ponytail: pure-python Goertzel — 7 ms/frame even at 60 bands (measured);
# numpy FFT if CPU ever bothers.
@lru_cache(maxsize=8)
def _vis_freqs(n: int) -> list[float]:
    """n log-spaced analysis frequencies, 50 Hz – 8 kHz. Below ~150 Hz at
    n=60 adjacent bands sit closer than the 10 Hz resolution of the
    2400-sample window — the low end smears a bit."""
    return [50.0 * (8000.0 / 50.0) ** (i / (n - 1)) for i in range(n)]


_VIS_FREQS = _vis_freqs(VIS_BANDS)


def _band_heights(
    samples, peak: float, freqs: list[float], rows: int = 16
) -> tuple[list[int], float]:
    n = len(samples)
    heights = []
    for f in freqs:
        c = 2.0 * math.cos(2.0 * math.pi * f / VIS_RATE)
        s1 = s2 = 0.0
        for x in samples:
            s0 = x + c * s1 - s2
            s2 = s1
            s1 = s0
        mag = math.sqrt(max(s1 * s1 + s2 * s2 - c * s1 * s2, 0.0)) / n
        # Auto-gain: slow-decay running peak (0.995/frame ≈ 2 s half-life
        # at 10 fps) so quiet and loud material both fill the panel.
        peak = max(peak * 0.995, mag, 1.0)
        # rows+1: a band sitting exactly at peak must reach full height.
        heights.append(min(rows, int((rows + 1) * mag / peak)))
    return heights, peak


def _frame_heights(samples, peak, freqs, rows, stereo):
    """Heights for one interleaved-stereo capture frame.

    Mono mixes the channels down; stereo returns a (left, right) pair
    sharing one auto-gain peak so the two sides stay comparable.
    """
    left, right = samples[0::2], samples[1::2]
    if not stereo:
        mono = array.array("h", ((l + r) // 2 for l, r in zip(left, right)))
        return _band_heights(mono, peak, freqs, rows)
    hl, peak = _band_heights(left, peak, freqs, rows)
    hr, peak = _band_heights(right, peak, freqs, rows)
    return (hl, hr), peak


def _bar_color(row: int, rows: int) -> RGB:
    # green low, yellow mid, red top — same proportions at any height
    if row * 16 < rows * 9:
        return (0, 220, 60)
    if row * 16 < rows * 13:
        return (240, 200, 0)
    return (255, 40, 40)


def _bar_frame(heights: list[int]) -> list[RGB]:
    pixels: list[RGB] = [(0, 0, 0)] * 256
    for x, h in enumerate(heights):
        for row in range(h):  # row 0 = bottom
            pixels[(15 - row) * 16 + x] = _bar_color(row, 16)
    return pixels


def _bar_frame_stereo(left: list[int], right: list[int]) -> list[RGB]:
    """Left channel grows up from the bottom edge, right channel down from
    the top, 8 rows each, meeting in the middle."""
    pixels: list[RGB] = [(0, 0, 0)] * 256
    for x in range(16):
        for row in range(left[x]):
            pixels[(15 - row) * 16 + x] = _bar_color(row, 8)
        for row in range(right[x]):
            pixels[row * 16 + x] = _bar_color(row, 8)
    return pixels


def _rings() -> list[list[int]]:
    """The panel as 8 concentric rings, each ordered clockwise from its
    top-left corner. Perimeters 60, 52, … 4 — together exactly 256 px."""
    rings = []
    for r in range(8):
        lo, hi = r, 15 - r
        path = ([(lo, x) for x in range(lo, hi)]
                + [(y, hi) for y in range(lo, hi)]
                + [(hi, x) for x in range(hi, lo, -1)]
                + [(y, lo) for y in range(hi, lo, -1)])
        rings.append([y * 16 + x for y, x in path])
    return rings


_RINGS = _rings()
# Tunnel state: outer-ring patterns, newest first (entry r = r frames ago).
_tunnel: dict = {"hist": deque(maxlen=len(_RINGS)), "offset": 0}


def _tunnel_color(band: int, nb: int, height: int) -> RGB:
    # sqrt lifts quiet bands into visibility; 0 stays black
    r, g, b = colorsys.hsv_to_rgb(band / nb, 1.0, math.sqrt(height / 16))
    return (int(255 * r), int(255 * g), int(255 * b))


def _tunnel_frame(heights: list[int]) -> list[RGB]:
    """Psychedelic tunnel: the current spectrum wrapped around the border,
    history shrinking ring by ring toward the center, colors bleeding out
    with age. Hue = band, brightness = band height."""
    outer = len(_RINGS[0])
    nb = len(heights)  # 1:1 pixel:band at 60; any count resamples cleanly
    return _tunnel_render(
        [_tunnel_color(i * nb // outer, nb, heights[i * nb // outer])
         for i in range(outer)])


def _tunnel_frame_stereo(left: list[int], right: list[int]) -> list[RGB]:
    """Stereo tunnel: one semicircle per channel, point-symmetric — the
    same frequency sits diametrically opposite its other channel. R runs
    low→high down the right side from top-center; L continues around,
    low→high up the left side from bottom-center."""
    outer = len(_RINGS[0])
    half = outer // 2
    nb = len(left)
    pattern: list[RGB] = [(0, 0, 0)] * outer
    for j in range(half):
        band = j * nb // half
        # ring index 8 = just right of top-center; +half = 180° opposite
        pattern[(8 + j) % outer] = _tunnel_color(band, nb, right[band])
        pattern[(8 + j + half) % outer] = _tunnel_color(band, nb, left[band])
    return _tunnel_render(pattern)


def _tunnel_render(pattern: list[RGB]) -> list[RGB]:
    """Rotate the border pattern by the accumulated spin, push it onto the
    history, and paint the rings — newest outside, fading inward."""
    outer = len(pattern)
    _tunnel["offset"] = (_tunnel["offset"] + _vis["spin"]) % outer
    off = int(_tunnel["offset"])
    if off:
        pattern = pattern[-off:] + pattern[:-off]
    _tunnel["hist"].appendleft(pattern)
    pixels: list[RGB] = [(0, 0, 0)] * 256
    for age, ring in enumerate(_RINGS):
        if age >= len(_tunnel["hist"]):
            break
        src = _tunnel["hist"][age]
        fade = _vis["fade"] ** age
        for j, idx in enumerate(ring):
            r, g, b = src[j * outer // len(ring)]
            # Quantized to 32-steps: fewer palette colors → ~5 instead of 8
            # BLE chunks per frame; the steps are invisible on the panel.
            pixels[idx] = (_q32(r * fade), _q32(g * fade), _q32(b * fade))
    return pixels


def _q32(v: float) -> int:
    return min(255, (int(v) + 16) & ~31)


# --- notification overlays (stamped onto visualizer frames) --------------------


@dataclass
class ScrollOverlay:
    """Marquee riding the visualizer frames: one column per frame over an
    opaque band (glyph rows ± 1 px) so text stays legible against the bars."""

    cols: list[int]
    color: RGB
    bg: RGB
    pos: int = 0

    def stamp(self, frame: list[RGB]) -> bool:
        for y in range(TEXT_Y - 1, TEXT_Y + 6):
            for x in range(16):
                frame[y * 16 + x] = self.bg
        for sx in range(16):
            ci = self.pos - 16 + 1 + sx
            if 0 <= ci < len(self.cols):
                mask = self.cols[ci]
                for y in range(5):
                    if mask & (1 << y):
                        frame[(TEXT_Y + y) * 16 + sx] = self.color
        self.pos += 1
        return self.pos < len(self.cols) + 16


@dataclass
class StaticOverlay:
    """Icon notification held on top of the bars for a fixed time, over an
    opaque band covering its content rows."""

    pixels: list[RGB]
    bg: RGB
    row0: int
    row1: int
    frames: int = field(default=4 * VIS_FPS)  # hold for 4 s

    @classmethod
    def from_pixels(cls, pixels: list[RGB], bg: RGB) -> "StaticOverlay":
        rows = [i // 16 for i, p in enumerate(pixels) if p != bg]
        return cls(
            pixels=pixels,
            bg=bg,
            row0=max(0, min(rows) - 1) if rows else 0,
            row1=min(15, max(rows) + 1) if rows else 15,
        )

    def stamp(self, frame: list[RGB]) -> bool:
        for y in range(self.row0, self.row1 + 1):
            for x in range(16):
                frame[y * 16 + x] = self.bg
        for i, p in enumerate(self.pixels):
            if p != self.bg:
                frame[i] = p
        self.frames -= 1
        return self.frames > 0


# Visualizer state: the running task, the overlay stamped on each frame,
# the render knobs — all switchable while running — and flash_until, the
# loop-clock deadline until which the clock owns the panel (weather_loop).
_vis: dict = {"task": None, "overlay": None, "mode": "bars", "stereo": False,
              "spin": TUNNEL_SPIN, "fade": TUNNEL_FADE,
              "bands": len(_RINGS[0]), "flash_until": 0.0}


def _sink_running() -> bool:
    """True when the default sink has an uncorked stream playing into it.

    Checked while our own parec is dead (or about to be), so the monitor
    capture never holds the sink RUNNING against us.
    """
    try:
        sink = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        short = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return False
    for line in short.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1] == sink:
            return line.rstrip().endswith("RUNNING")
    return False


def _clock_payload(views: list[str], color: RGB) -> bytes:
    """Clock-channel switch pinning its sub-views (45 00 01 layout, per
    node-divoom-timebox-evo, state-dump-verified against this firmware);
    the face stays fullscreen."""
    flags = [v in views for v in ("time", "weather", "temp", "date")]
    return bytes([0x45, 0x00, 0x01, 0x00, *flags, *color])


# The clock command replayed on every restore ({"clock": [...]} swaps it).
# Always explicit flags — the bare 45 00 RESETS the box's stored sub-views
# (probed via 0x46 state dumps). Each enabled view is a full-screen page
# the box cycles through (~15 s); the weather page is an animated scene.
# TIMEBOX_CLOCK ("time,weather") sets the boot default so it survives
# daemon restarts; the FIFO key changes it live.
_CLOCK_VIEWS = ("time", "weather", "temp", "date")
_clock: dict = {"payload": _clock_payload(
    [v for v in os.environ.get("TIMEBOX_CLOCK", "time,weather").split(",")
     if v in _CLOCK_VIEWS] or ["time"],
    (255, 255, 255))}


async def _restore_clock(client) -> None:
    """Hand the panel back to the clock (paused, stopped, capture lost).

    Bounded (TBX-26-006): a wedged write here would park the task for good.
    """
    if client is None:
        return
    try:
        async with _panel_lock:
            await asyncio.wait_for(
                client.send_raw_payload(_clock["payload"]), 5)
    except Exception:
        pass  # a dead panel must not stop the audio wait


def _apply_overlay(frame: list[RGB]) -> list[RGB]:
    overlay = _vis["overlay"]
    if overlay is not None and not overlay.stamp(frame):
        _vis["overlay"] = None
    return frame


def _is_frame_ack(n: bytes) -> bool:
    """The box acks every 0x44 image write: 01 <len16> 04 44 55 ..."""
    return n[3:6] == b"\x04\x44\x55"


async def _push_frame(client, frame: list[RGB]) -> None:
    """Panel write paced by the box's ack. Write-without-response returns
    when bluezd queues the chunks, not when the radio sends them — a recipe
    fatter than the link (~60 chunks/s measured) would otherwise pile
    unbounded lag into bluez's queue. Waiting for the ack caps that queue
    at one frame; the newest-audio drain upstream turns the wait into
    skipped frames, so overload degrades to lower fps, not growing lag.
    """
    loop = asyncio.get_running_loop()
    async with _panel_lock:
        client.clear_notifications()
        await client.static_image(frame)
        deadline = loop.time() + 2  # a lost ack must not wedge the stream
        while loop.time() < deadline:
            if any(_is_frame_ack(n) for n in client.drain_notifications()):
                return
            await asyncio.sleep(0.01)
    print("visualizer: no frame ack in 2s — link congested?", flush=True)


async def visualize(params: dict) -> None:
    """Stream a 16-band spectrum of the system audio (default sink monitor).

    Endless unless `seconds` is given; stopped via {"visualizer": false}.
    Endless mode captures only while the default sink is actually playing:
    after SILENCE_SECS of silence, or a lost capture (sink switched,
    PipeWire restarted, suspend), parec is stopped — which also clears
    KDE's microphone-in-use indicator — and the bars resume when audio
    comes back. Endless mode also survives the LE link dropping (box
    power management): it redials via ensure_le() and carries on. A
    timed run still dies on BLE errors — it would overrun its window.
    """
    seconds = params.get("seconds")
    total = int(seconds * VIS_FPS) if seconds else None
    bytes_per_frame = (VIS_RATE // VIS_FPS) * 2 * 2  # 2 ch × 2 B, interleaved
    peak = 1.0
    sent = 0
    quiet = 0
    waiting = False  # capture paused/lost; log the resume once, not per retry
    _tunnel["hist"].clear()
    print(f"visualizer started ({seconds}s)" if seconds
          else 'visualizer started (endless — stop with {"visualizer": false})',
          flush=True)
    try:
        while total is None or sent < total:
            # Endless mode: no capture stream while nothing plays.
            if total is None and not _sink_running():
                client = _le["client"]  # may be dead; all writes are guarded
                if not waiting:
                    waiting = True
                    print("visualizer: audio idle — capture paused", flush=True)
                    _tunnel["hist"].clear()  # stale history; restart fresh
                    await _restore_clock(client)
                # Overlays are normally stamped onto capture frames; keep
                # notifications visible while paused.
                if _vis["overlay"] is not None:
                    try:
                        await _push_frame(
                            client, _apply_overlay(_bar_frame([0] * VIS_BANDS)))
                    except Exception:
                        pass  # panel trouble must not stop the audio wait
                    await asyncio.sleep(1 / VIS_FPS)
                    if _vis["overlay"] is None:  # that was its last frame
                        await _restore_clock(client)
                else:
                    await asyncio.sleep(2)
                continue
            try:
                client = await ensure_le()
            except Exception as exc:
                if total is not None:
                    raise
                print(f"visualizer: LE reconnect failed ({exc}) — retrying",
                      flush=True)
                await asyncio.sleep(15)
                continue
            proc = None
            try:
                # Re-queried per attempt: the default sink may have changed.
                sink = subprocess.run(
                    ["pactl", "get-default-sink"],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                proc = await asyncio.create_subprocess_exec(
                    "parec", "--client-name=TimeBox visualizer",
                    f"--device={sink}.monitor", "--format=s16le",
                    f"--rate={VIS_RATE}", "--channels=2", "--raw",
                    "--latency-msec=100",  # default adaptive latency delivers ~2s bursts
                    stdout=asyncio.subprocess.PIPE,
                )
                while total is None or sent < total:
                    data = await proc.stdout.readexactly(bytes_per_frame)
                    # Render the NEWEST audio: BLE writes can be slower than
                    # capture, and stale frames would lag the music.
                    # ponytail: StreamReader has no "available" API; keep the
                    # private-buffer poke in this one place (like _le_alive).
                    while len(proc.stdout._buffer) >= bytes_per_frame:
                        data = await proc.stdout.readexactly(bytes_per_frame)
                    if waiting:
                        waiting = False
                        print("visualizer: audio capture resumed", flush=True)
                    samples = array.array("h", data)
                    if total is None:
                        quiet = 0 if any(samples) else quiet + 1
                        if quiet >= SILENCE_SECS * VIS_FPS:
                            # ponytail: an uncorked stream of pure digital
                            # zeros keeps the sink RUNNING, so this respawns
                            # parec every SILENCE_SECS; panel is black either
                            # way. Track corked state via pactl if it bites.
                            quiet = 0
                            break
                    # Clock flash: the clock owns the panel; keep draining
                    # audio, skip the writes. A notification overrides it.
                    if (_vis["overlay"] is None and
                            asyncio.get_running_loop().time()
                            < _vis["flash_until"]):
                        continue
                    # One read per frame: a live mode/stereo switch during
                    # the to_thread await must not mismatch heights/renderer.
                    mode, stereo = _vis["mode"], _vis["stereo"]
                    freqs = (_vis_freqs(_vis["bands"]) if mode == "tunnel"
                             else _VIS_FREQS)
                    rows = 8 if stereo and mode == "bars" else 16
                    heights, peak = await asyncio.to_thread(
                        _frame_heights, samples, peak, freqs, rows, stereo)
                    if mode == "tunnel":
                        frame = (_tunnel_frame_stereo(*heights) if stereo
                                 else _tunnel_frame(heights))
                    else:
                        frame = (_bar_frame_stereo(*heights) if stereo
                                 else _bar_frame(heights))
                    await _push_frame(client, _apply_overlay(frame))
                    sent += 1
            except BleakError as exc:
                if total is not None:
                    raise
                print(f"visualizer: panel write failed ({exc}) — reconnecting",
                      flush=True)
                await drop_le()
                await asyncio.sleep(2)
            except (asyncio.IncompleteReadError,
                    subprocess.CalledProcessError, OSError) as exc:
                if total is not None:
                    if isinstance(exc, asyncio.IncompleteReadError):
                        print("visualizer: audio capture ended", flush=True)
                        break
                    raise
                if not waiting:
                    waiting = True
                    print("visualizer: audio capture ended — waiting for audio",
                          flush=True)
                    _tunnel["hist"].clear()  # stale history; restart fresh
                    await _restore_clock(client)
                await asyncio.sleep(2)
            finally:
                if proc is not None:
                    try:
                        # kill, not terminate: parec answers SIGTERM by
                        # flushing onto a full pipe nobody reads any more —
                        # it blocks forever and proc.wait() wedges recovery.
                        proc.kill()
                    except ProcessLookupError:
                        pass  # parec already gone — that's why we're here
                    # Drain to EOF: with unread audio backlogged, asyncio
                    # pauses the pipe and proc.wait() deadlocks even after
                    # the child is dead — wait() needs the pipe closed too.
                    while await proc.stdout.read(65536):
                        pass
                    await proc.wait()
    finally:
        _vis["overlay"] = None
        # The box keeps displaying the last pushed frame forever.
        await _restore_clock(_le["client"])
        print("visualizer stopped", flush=True)


# --- weather --------------------------------------------------------------------

# The box's clock/weather display expects a phone app to feed it data; here
# the daemon does that job with DWD data via Bright Sky (free, keyless JSON
# front for DWD open data). Needs TIMEBOX_LATLON="lat,lon" in the env file.
WEATHER_EVERY = 30 * 60  # seconds between forecast fetches
# Clock air time while the visualizer holds the panel: every `every`
# seconds the clock shows for `flash` seconds. FIFO keys clock_every /
# clock_flash (0 = never); changes apply from the next cycle.
_weather = {"every": 10 * 60, "flash": 30}

# Divoom icon bytes for the 5F push: 1 clear, 3 cloudy, 5 thunderstorm,
# 6 rain, 8 snow, 9 fog.
_BS_CONDITION = {"thunderstorm": 5, "snow": 8, "sleet": 8, "hail": 8,
                 "rain": 6, "fog": 9}


def _bs_to_box(condition: str | None, icon: str | None) -> int:
    """Bright Sky condition/icon -> Divoom weather icon byte. The condition
    names precipitation; a dry sky falls back to the cloud-cover icon."""
    if condition in _BS_CONDITION:
        return _BS_CONDITION[condition]
    return 1 if icon in ("clear-day", "clear-night") else 3


def _fetch_weather(latlon: str) -> tuple[int, int]:
    """(temperature °C, Divoom icon) for "lat,lon", from DWD via Bright Sky."""
    lat, lon = (float(c) for c in latlon.split(","))
    with urllib.request.urlopen(
        f"https://api.brightsky.dev/current_weather?lat={lat}&lon={lon}",
        timeout=15,
    ) as resp:
        weather = json.load(resp)["weather"]
    return round(weather["temperature"]), _bs_to_box(
        weather.get("condition"), weather.get("icon"))


async def weather_loop() -> None:
    """Keep the box's weather current and give the clock its air time.

    Every _weather["every"] seconds: re-push temperature/condition
    (re-pushing covers a power-cycled box that lost them) and, if the
    visualizer holds the panel, show the clock for _weather["flash"]
    seconds — the visualizer skips its writes for that window and simply
    repaints afterwards. The idle panel shows the clock anyway. Redialing
    via ensure_le() doubles as the idle daemon's link self-healing.
    """
    latlon = os.environ.get("TIMEBOX_LATLON")
    if not latlon:
        print("weather: TIMEBOX_LATLON not set — box weather stays stale",
              flush=True)
    weather = None
    age = WEATHER_EVERY  # stale from the start: fetch on the first pass
    while True:
        if latlon and age >= WEATHER_EVERY:
            try:
                weather = await asyncio.to_thread(_fetch_weather, latlon)
                age = 0
                print(f"weather: {weather[0]}°C icon {weather[1]}",
                      flush=True)
            except Exception as exc:
                print(f"weather: fetch failed ({exc}) — retrying later",
                      flush=True)
        try:
            client = await ensure_le()
            if weather is not None:
                temp, icon = weather
                async with _panel_lock:
                    await asyncio.wait_for(client.send_raw_payload(
                        bytes([0x5F, temp & 0xFF, icon])), 5)
            vis_running = _vis["task"] is not None and not _vis["task"].done()
            if vis_running and _weather["flash"] > 0:
                _vis["flash_until"] = (asyncio.get_running_loop().time()
                                       + _weather["flash"])
                await _restore_clock(client)
                print(f"clock flash ({_weather['flash']}s)", flush=True)
            elif not vis_running:
                # Idle: re-assert the clock. The box's hardware button and
                # its demo carousel after an app-less power-up both wander
                # off-channel; this herds it back within one cycle.
                await _restore_clock(client)
        except (BleakError, asyncio.TimeoutError) as exc:
            print(f"weather: push failed ({exc})", flush=True)
            await drop_le()
        except Exception as exc:
            print(f"weather: LE unavailable ({exc})", flush=True)
        await asyncio.sleep(_weather["every"])
        age += _weather["every"]


# --- sound ----------------------------------------------------------------------

# Sound runs as a background task so a slow/failing audio dial never blocks
# the FIFO loop or the display. The lock keeps it to ONE audio attempt at a
# time — stacked dials are what wedge the box into br-connection-busy.
_sound_lock = asyncio.Lock()
_background: set = set()


def _spawn(coro, label: str) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)

    def _done(t: asyncio.Task) -> None:
        _background.discard(t)
        if not t.cancelled() and t.exception():
            print(f"{label} failed: {t.exception()}", flush=True)

    task.add_done_callback(_done)
    return task


async def initial_audio() -> None:
    """Latch the box's audio link at startup, in the background.

    Holds the sound lock while dialing, so a notification arriving meanwhile
    skips its chime instead of stacking a second dial (stacked dials are what
    wedge the box into br-connection-busy).
    """
    async with _sound_lock:
        try:
            sink = await asyncio.to_thread(bring_up_audio, DEFAULT_ADDRESS, 3)
            print(f"audio ready: {sink}", flush=True)
        except Exception as exc:
            print(f"audio not ready ({exc}) — will retry per notification", flush=True)


async def play(params: dict) -> None:
    if _sound_lock.locked():
        print("sound skipped — audio attempt already in flight", flush=True)
        return
    async with _sound_lock:
        try:
            await asyncio.to_thread(
                play_sound, DEFAULT_ADDRESS, params["sound"], 3
            )
        except Exception as exc:
            print(f"sound failed: {exc}", flush=True)


# --- request handling -------------------------------------------------------------


async def handle(client, params: dict) -> None:
    if "brightness" in params:
        async with _panel_lock:
            await client.set_brightness(params["brightness"])

    vis_running = _vis["task"] is not None and not _vis["task"].done()

    if any(k in params for k in ("clock", "clock_flash", "clock_every")):
        changed = []
        if "clock" in params:
            _clock["payload"] = _clock_payload(params["clock"],
                                               params["clock_color"])
            if not vis_running:  # otherwise the next frame repaints anyway;
                async with _panel_lock:  # the payload shows on restore
                    await asyncio.wait_for(
                        client.send_raw_payload(_clock["payload"]), 5)
            changed.append(f"views: {', '.join(params['clock'])}")
        if "clock_flash" in params:
            _weather["flash"] = params["clock_flash"]
            changed.append(f"flash: {params['clock_flash']}s")
        if "clock_every" in params:
            _weather["every"] = params["clock_every"]
            changed.append(f"every: {params['clock_every']}s")
        print(f"clock {'; '.join(changed)}", flush=True)
        return

    if "visualizer" in params:
        if params["visualizer"] and not vis_running:
            _vis["mode"] = params.get("mode", "bars")
            _vis["stereo"] = params.get("stereo", False)
            _vis["spin"] = params.get("spin", TUNNEL_SPIN)
            _vis["fade"] = params.get("fade", TUNNEL_FADE)
            _vis["bands"] = params.get("bands", len(_RINGS[0]))
            _vis["task"] = _spawn(visualize(params), "visualizer")
        elif not params["visualizer"] and vis_running:
            _vis["task"].cancel()
        elif params["visualizer"]:
            changed = []
            for key in ("mode", "spin", "fade", "bands", "stereo"):
                if params.get(key, _vis[key]) != _vis[key]:
                    _vis[key] = params[key]
                    changed.append(f"{key}: {params[key]}")
            print(f"visualizer {', '.join(changed)}" if changed
                  else "visualizer already running", flush=True)
        return

    if not params["silent"]:
        _spawn(play(params), "sound")

    if "text" in params:
        if vis_running:
            _vis["overlay"] = ScrollOverlay(
                cols=text_columns(params["text"]),
                color=params["icon_color"],
                bg=params["background"],
            )
        else:
            async with _panel_lock:
                await scroll_text(
                    client, params["text"], params["icon_color"],
                    params["background"], fps=params["fps"],
                )
    else:
        pixels = render_notification(
            count=params["count"],
            icon_color=params["icon_color"],
            number_color=params["number_color"],
            background=params["background"],
        )
        if vis_running:
            _vis["overlay"] = StaticOverlay.from_pixels(pixels, params["background"])
        else:
            async with _panel_lock:
                await client.static_image(pixels)


# --- daemon main loop --------------------------------------------------------------


def fifo_path() -> str:
    """Private FIFO in XDG_RUNTIME_DIR only — never a world-writable /tmp."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise SystemExit("XDG_RUNTIME_DIR is not set — refusing a /tmp FIFO")
    path = os.path.join(runtime_dir, "timebox.fifo")
    if os.path.lexists(path):
        st = os.lstat(path)
        if not stat.S_ISFIFO(st.st_mode) or st.st_uid != os.getuid():
            raise SystemExit(f"{path} exists and is not our FIFO — remove it first")
        os.chmod(path, 0o600)  # a FIFO left over from an older run may be laxer
    else:
        os.mkfifo(path, 0o600)
    return path


def fifo_lines(path: str):
    while True:
        with open(path) as f:  # blocks until a writer appears
            for line in f:
                line = line.strip()
                if line:
                    yield line


def _dump_tasks() -> None:
    """SIGUSR2: print where every asyncio task is suspended — the BT stack
    has wedged awaits more than once and thread dumps can't see them."""
    for task in asyncio.all_tasks():
        frames = task.get_stack(limit=8)
        where = " <- ".join(
            f"{f.f_code.co_name}:{f.f_lineno}" for f in reversed(frames))
        print(f"task {task.get_coro().__qualname__}: {where or 'done'}",
              flush=True)


async def main() -> None:
    if not DEFAULT_ADDRESS:
        raise SystemExit("set TIMEBOX_ADDRESS to the box's Bluetooth MAC")
    if not valid_address(DEFAULT_ADDRESS):
        raise SystemExit(f"TIMEBOX_ADDRESS {DEFAULT_ADDRESS!r} is not a MAC address")

    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR2, _dump_tasks)
    agent_bus = await start_agent(DEFAULT_ADDRESS)
    client = await ensure_le()
    await client.set_brightness(80)
    # Persistent on the box: boot into the clock, not the demo carousel.
    await client.set_startup_channel(0)
    print("LE control connected", flush=True)

    # Serve requests as soon as the display works. Audio bring-up can take a
    # minute against a box that isn't answering pages, and notifications
    # written in that window would otherwise be dropped.
    path = fifo_path()
    print(f"listening on {path}", flush=True)
    _spawn(initial_audio(), "audio bring-up")
    _spawn(weather_loop(), "weather")

    # ponytail: reconnect-on-demand; add a keepalive task if first-notify
    # latency after long idle turns out to bother.
    lines = fifo_lines(path)
    while True:
        line = await asyncio.to_thread(next, lines)
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"bad request (not JSON): {exc}", flush=True)
            continue
        if not isinstance(raw, dict):
            print("bad request (not a JSON object)", flush=True)
            continue
        params = parse_params(raw)

        try:
            client = await ensure_le()
            await handle(client, params)
            # Log keys only: notification text is private and journald
            # would keep it forever.
            print(f"notified ({', '.join(sorted(raw))})", flush=True)
        except Exception as exc:
            print(f"notification failed: {exc}", flush=True)
            # Only tear the link down when it is actually the problem —
            # anything else (bad sound file, pactl hiccup) must not cost
            # us the connection.
            client = _le["client"]
            if client is not None and (isinstance(exc, BleakError) or not _le_alive(client)):
                await drop_le()


if __name__ == "__main__":
    asyncio.run(main())
