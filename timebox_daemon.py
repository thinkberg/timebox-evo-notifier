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
plays — or `seconds` if given); visualizer false stops it.
Notifications arriving while it runs are
rendered on top of the bars over an opaque background band.

Bad values are clamped or replaced with defaults — a malformed request
must never cost us the BLE link.
"""

import array
import asyncio
import json
import math
import os
import stat
import subprocess
from dataclasses import dataclass, field

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
    return p


# --- live audio visualizer -----------------------------------------------------

VIS_RATE = 24000
VIS_FPS = 10
VIS_BANDS = 16
SILENCE_SECS = 10  # digital silence before capture (and KDE's mic icon) pauses
# ponytail: pure-python Goertzel, ~40k mults/frame; numpy FFT if CPU bothers.
_VIS_FREQS = [50.0 * (8000.0 / 50.0) ** (i / (VIS_BANDS - 1)) for i in range(VIS_BANDS)]


def _band_heights(samples: array.array, peak: float) -> tuple[list[int], float]:
    n = len(samples)
    heights = []
    for f in _VIS_FREQS:
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
        # 17, not 16: a band sitting exactly at peak must reach full height.
        heights.append(min(16, int(17 * mag / peak)))
    return heights, peak


def _bar_frame(heights: list[int]) -> list[RGB]:
    pixels: list[RGB] = [(0, 0, 0)] * 256
    for x, h in enumerate(heights):
        for row in range(h):  # row 0 = bottom; green low, yellow mid, red top
            color = (0, 220, 60) if row < 9 else (240, 200, 0) if row < 13 else (255, 40, 40)
            pixels[(15 - row) * 16 + x] = color
    return pixels


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


# Visualizer state: the running task plus the overlay stamped on each frame.
_vis: dict = {"task": None, "overlay": None}


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


async def _blank_panel(client) -> None:
    try:
        async with _panel_lock:
            await client.static_image(_bar_frame([0] * VIS_BANDS))
    except Exception:
        pass  # a dead panel must not stop the audio wait


def _apply_overlay(frame: list[RGB]) -> list[RGB]:
    overlay = _vis["overlay"]
    if overlay is not None and not overlay.stamp(frame):
        _vis["overlay"] = None
    return frame


async def visualize(client, params: dict) -> None:
    """Stream a 16-band spectrum of the system audio (default sink monitor).

    Endless unless `seconds` is given; stopped via {"visualizer": false}.
    Endless mode captures only while the default sink is actually playing:
    after SILENCE_SECS of silence, or a lost capture (sink switched,
    PipeWire restarted, suspend), parec is stopped — which also clears
    KDE's microphone-in-use indicator — and the bars resume when audio
    comes back. Holds the client it started with: if the LE link is
    re-established the task ends with a logged error — restart the
    visualizer afterwards.
    """
    seconds = params.get("seconds")
    total = int(seconds * VIS_FPS) if seconds else None
    bytes_per_frame = (VIS_RATE // VIS_FPS) * 2
    peak = 1.0
    sent = 0
    quiet = 0
    waiting = False  # capture paused/lost; log the resume once, not per retry
    print(f"visualizer started ({seconds}s)" if seconds
          else 'visualizer started (endless — stop with {"visualizer": false})',
          flush=True)
    try:
        while total is None or sent < total:
            # Endless mode: no capture stream while nothing plays.
            if total is None and not _sink_running():
                if not waiting:
                    waiting = True
                    print("visualizer: audio idle — capture paused", flush=True)
                    await _blank_panel(client)
                # Overlays are normally stamped onto capture frames; keep
                # notifications visible while paused.
                if _vis["overlay"] is not None:
                    async with _panel_lock:
                        await client.static_image(
                            _apply_overlay(_bar_frame([0] * VIS_BANDS)))
                    await asyncio.sleep(1 / VIS_FPS)
                    if _vis["overlay"] is None:  # that was its last frame
                        await _blank_panel(client)
                else:
                    await asyncio.sleep(2)
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
                    f"--rate={VIS_RATE}", "--channels=1", "--raw",
                    "--latency-msec=100",  # default adaptive latency delivers ~2s bursts
                    stdout=asyncio.subprocess.PIPE,
                )
                while total is None or sent < total:
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
                    heights, peak = await asyncio.to_thread(_band_heights, samples, peak)
                    async with _panel_lock:
                        await client.static_image(_apply_overlay(_bar_frame(heights)))
                    sent += 1
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
                    await _blank_panel(client)
                await asyncio.sleep(2)
            finally:
                if proc is not None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass  # parec already gone — that's why we're here
                    await proc.wait()
    finally:
        _vis["overlay"] = None
        print("visualizer stopped", flush=True)


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

    if "visualizer" in params:
        if params["visualizer"] and not vis_running:
            _vis["task"] = _spawn(visualize(client, params), "visualizer")
        elif not params["visualizer"] and vis_running:
            _vis["task"].cancel()
        elif params["visualizer"]:
            print("visualizer already running", flush=True)
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


async def main() -> None:
    if not DEFAULT_ADDRESS:
        raise SystemExit("set TIMEBOX_ADDRESS to the box's Bluetooth MAC")
    if not valid_address(DEFAULT_ADDRESS):
        raise SystemExit(f"TIMEBOX_ADDRESS {DEFAULT_ADDRESS!r} is not a MAC address")

    agent_bus = await start_agent(DEFAULT_ADDRESS)
    client = await connect_le(DEFAULT_ADDRESS)
    await client.set_brightness(80)
    print("LE control connected", flush=True)

    # Serve requests as soon as the display works. Audio bring-up can take a
    # minute against a box that isn't answering pages, and notifications
    # written in that window would otherwise be dropped.
    path = fifo_path()
    print(f"listening on {path}", flush=True)
    _spawn(initial_audio(), "audio bring-up")

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
            if not _le_alive(client):
                print("LE link lost — reconnecting", flush=True)
                client = await connect_le(DEFAULT_ADDRESS)
            await handle(client, params)
            # Log keys only: notification text is private and journald
            # would keep it forever.
            print(f"notified ({', '.join(sorted(raw))})", flush=True)
        except Exception as exc:
            print(f"notification failed: {exc}", flush=True)
            # Only tear the link down when it is actually the problem —
            # anything else (bad sound file, pactl hiccup) must not cost
            # us the connection.
            if isinstance(exc, BleakError) or not _le_alive(client):
                try:
                    await client.disconnect()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())
