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
spectrum of the system audio (endless, or `seconds` if given);
visualizer false stops it. Notifications arriving while it runs are
rendered on top of the bars over an opaque background band.
"""

import array
import asyncio
import json
import math
import os
import subprocess

from timebox_notify import (
    DEFAULT_ADDRESS,
    DEFAULT_SOUND,
    TEXT_Y,
    connect_a2dp,
    connect_le,
    render_notification,
    scroll_text,
    start_agent,
    text_columns,
    wait_for_sink,
)

FIFO = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "timebox.fifo")


def fifo_lines():
    while True:
        with open(FIFO) as f:  # blocks until a writer appears
            for line in f:
                line = line.strip()
                if line:
                    yield line


def ensure_audio(address: str) -> str:
    sink = wait_for_sink(address, 3)
    if sink is None:
        connect_a2dp(address)
        sink = wait_for_sink(address, 8)
    if sink is None:
        raise RuntimeError("audio sink did not appear")
    return sink


# --- live audio visualizer -------------------------------------------------

VIS_RATE = 24000
VIS_FPS = 10
VIS_BANDS = 16
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
        peak = max(peak * 0.995, mag, 1.0)
        heights.append(min(16, int(17 * mag / peak)))
    return heights, peak


def _bar_frame(heights: list[int]) -> list[tuple[int, int, int]]:
    pixels = [(0, 0, 0)] * 256
    for x, h in enumerate(heights):
        for row in range(h):  # row 0 = bottom
            color = (0, 220, 60) if row < 9 else (240, 200, 0) if row < 13 else (255, 40, 40)
            pixels[(15 - row) * 16 + x] = color
    return pixels


# Visualizer state: the running task plus an optional notification overlay
# that gets stamped onto every frame (over an opaque band, for legibility).
_vis: dict = {"task": None, "overlay": None}


def _apply_overlay(frame: list) -> list:
    ov = _vis["overlay"]
    if ov is None:
        return frame
    bg = ov["bg"]
    if ov["kind"] == "scroll":
        for y in range(TEXT_Y - 1, TEXT_Y + 6):
            for x in range(16):
                frame[y * 16 + x] = bg
        pos = ov["pos"]
        for sx in range(16):
            ci = pos - 16 + 1 + sx
            if 0 <= ci < len(ov["cols"]):
                mask = ov["cols"][ci]
                for y in range(5):
                    if mask & (1 << y):
                        frame[(TEXT_Y + y) * 16 + sx] = ov["color"]
        ov["pos"] += 1
        if ov["pos"] >= len(ov["cols"]) + 16:
            _vis["overlay"] = None
    else:  # static notification, shown for a fixed number of frames
        for y in range(ov["row0"], ov["row1"] + 1):
            for x in range(16):
                frame[y * 16 + x] = bg
        for i, p in enumerate(ov["pixels"]):
            if p != bg:
                frame[i] = p
        ov["frames"] -= 1
        if ov["frames"] <= 0:
            _vis["overlay"] = None
    return frame


async def visualize(client, params: dict) -> None:
    """Stream a 16-band spectrum of the system audio (default sink monitor).

    Endless unless `seconds` is given; stopped via {"visualizer": false}.
    """
    seconds = params.get("seconds")
    total = int(float(seconds) * VIS_FPS) if seconds else None
    sink = subprocess.run(
        ["pactl", "get-default-sink"], capture_output=True, text=True, check=True
    ).stdout.strip()
    proc = await asyncio.create_subprocess_exec(
        "parec", f"--device={sink}.monitor", "--format=s16le",
        f"--rate={VIS_RATE}", "--channels=1", "--raw",
        "--latency-msec=100",  # default adaptive latency delivers ~2s bursts
        stdout=asyncio.subprocess.PIPE,
    )
    bytes_per_frame = (VIS_RATE // VIS_FPS) * 2
    peak = 1.0
    sent = 0
    print(f"visualizer started ({seconds}s)" if seconds
          else 'visualizer started (endless — stop with {"visualizer": false})',
          flush=True)
    try:
        while total is None or sent < total:
            data = await proc.stdout.readexactly(bytes_per_frame)
            samples = array.array("h", data)
            heights, peak = await asyncio.to_thread(_band_heights, samples, peak)
            await client.static_image(_apply_overlay(_bar_frame(heights)))
            sent += 1
    except asyncio.IncompleteReadError:
        print("visualizer: audio capture ended", flush=True)
    finally:
        _vis["overlay"] = None
        proc.terminate()
        await proc.wait()
        print("visualizer stopped", flush=True)


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


async def play(params: dict) -> None:
    if _sound_lock.locked():
        print("sound skipped — audio attempt already in flight", flush=True)
        return
    async with _sound_lock:
        try:
            sink = await asyncio.to_thread(ensure_audio, DEFAULT_ADDRESS)
            await asyncio.to_thread(
                subprocess.run,
                ["pw-play", "--target", sink, params.get("sound", DEFAULT_SOUND)],
            )
        except Exception as exc:
            print(f"sound failed: {exc}", flush=True)


async def handle(client, params: dict) -> None:
    if "brightness" in params:
        await client.set_brightness(int(params["brightness"]))

    vis_running = _vis["task"] is not None and not _vis["task"].done()

    if "visualizer" in params:
        if params["visualizer"] and not vis_running:
            _vis["task"] = _spawn(visualize(client, params), "visualizer")
        elif not params["visualizer"] and vis_running:
            _vis["task"].cancel()
        elif params["visualizer"]:
            print("visualizer already running", flush=True)
        return

    icon_color = tuple(params.get("icon_color", (255, 60, 40)))
    background = tuple(params.get("background", (0, 0, 0)))

    if not params.get("silent"):
        _spawn(play(params), "sound")

    if "text" in params:
        if vis_running:
            _vis["overlay"] = {
                "kind": "scroll",
                "cols": text_columns(str(params["text"])),
                "pos": 0,
                "color": icon_color,
                "bg": background,
            }
        else:
            await scroll_text(
                client, str(params["text"]), icon_color, background,
                fps=float(params.get("fps", 10)),
            )
    else:
        pixels = render_notification(
            count=int(params.get("count", 1)),
            icon_color=icon_color,
            number_color=tuple(params.get("number_color", (255, 255, 255))),
            background=background,
        )
        if vis_running:
            rows = [i // 16 for i, p in enumerate(pixels) if p != background]
            _vis["overlay"] = {
                "kind": "static",
                "pixels": pixels,
                "bg": background,
                "row0": max(0, min(rows) - 1) if rows else 0,
                "row1": min(15, max(rows) + 1) if rows else 15,
                "frames": 4 * VIS_FPS,
            }
        else:
            await client.static_image(pixels)


async def main() -> None:
    if not DEFAULT_ADDRESS:
        raise SystemExit("set TIMEBOX_ADDRESS to the box's Bluetooth MAC")
    agent_bus = await start_agent()
    client = await connect_le(DEFAULT_ADDRESS)
    await client.set_brightness(80)
    print("LE control connected", flush=True)

    try:
        sink = await asyncio.to_thread(ensure_audio, DEFAULT_ADDRESS)
        print(f"audio ready: {sink}", flush=True)
    except Exception as exc:
        print(f"audio not ready yet ({exc}) — will retry per notification", flush=True)

    if not os.path.exists(FIFO):
        os.mkfifo(FIFO)
    print(f"listening on {FIFO}", flush=True)

    # ponytail: reconnect-on-demand; add a keepalive task if first-notify
    # latency after long idle turns out to bother.
    lines = fifo_lines()
    while True:
        line = await asyncio.to_thread(next, lines)
        try:
            params = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"bad request {line!r}: {exc}", flush=True)
            continue

        try:
            if not (client._ble and client._ble.is_connected):
                print("LE link lost — reconnecting", flush=True)
                client = await connect_le(DEFAULT_ADDRESS)
            await handle(client, params)
            print(f"notified: {line}", flush=True)
        except Exception as exc:
            print(f"notification failed: {exc}", flush=True)
            try:
                await client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
