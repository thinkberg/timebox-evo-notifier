#!/usr/bin/env python3
"""Send a notification image (and sound) to a Divoom TimeBox Evo.

Control goes over BLE ("TimeBox-Evo-light"); sound over classic A2DP
("TimeBox-Evo-audio"). Both share one MAC. The box supports both links
simultaneously, but ONLY in this order: BLE first, then A2DP — it stops
LE advertising while a classic link is up.

System prerequisites (one-time):
  * /etc/bluetooth/main.conf: Experimental = true  (for PreferredBearer)
  * TIMEBOX_ADDRESS set to the box's MAC (or pass --address).
  * Box paired (classic bond; if a PIN is asked, set TIMEBOX_PIN)
    AND trusted:
        bluetoothctl trust <box-mac>
    Trusted means the box's constant audio-reconnect attempts succeed
    silently instead of hanging in desktop authorization popups (which
    blocks all other classic connects). The script wins the LE window by
    briefly kicking the classic link and racing the reconnect.
  * If sinks stop appearing after a bluetoothd restart:
        systemctl --user restart wireplumber

timebox_daemon.py imports this module as its library (font, rendering,
agent, link management); the CLI entry point lives at the bottom.
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import warnings

# bleak/BlueZ cosmetic nag; irrelevant — we chunk writes at 138 B ourselves.
warnings.filterwarnings("ignore", message="Using default MTU value.*")

from bleak import BleakScanner
from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method
from divoom_protocol import DivoomClient

# --- divoom_protocol encoder patch ------------------------------------------

# divoom_protocol 0.2.0 bug: encode_image() overflows the palette-count byte
# on frames with 256 unique colors (wire format wants 0x00 for 256). Patch at
# our boundary instead of the lib: merge the two closest colors to 255. On a
# 16x16 panel 256 uniques means every pixel is distinct, so one pixel shifting
# to its nearest neighbor color is imperceptible.
import divoom_protocol.commands as _dp_commands  # noqa: E402

_orig_encode_image = _dp_commands.encode_image


def _safe_encode_image(pixels):
    pixels = list(pixels)
    if len(set(pixels)) == 256:
        best = None
        for i in range(len(pixels)):
            for j in range(i + 1, len(pixels)):
                a, b = pixels[i], pixels[j]
                d = (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
                if best is None or d < best[0]:
                    best = (d, i, j)
        pixels[best[2]] = pixels[best[1]]
    return _orig_encode_image(pixels)


_dp_commands.encode_image = _safe_encode_image

WIDTH = 16
HEIGHT = 16
DEFAULT_ADDRESS = os.environ.get("TIMEBOX_ADDRESS", "")
DEFAULT_SOUND = "/usr/share/sounds/ocean/stereo/message-new-instant.oga"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def valid_address(address: str) -> bool:
    return bool(_MAC_RE.match(address))


RGB = tuple[int, int, int]
Pixels = list[RGB]

# --- 3x5 font & frame rendering (shared with the daemon) ---------------------

# Compact 3x5 digits, stored row by row.
DIGITS: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
}


# 3x5 letters and punctuation in the same row format as DIGITS.
# ponytail: uppercase-only marquee font; add a 5x7 set if readability bothers.
LETTERS: dict[str, tuple[str, ...]] = {
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("011", "100", "100", "100", "011"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("011", "100", "101", "101", "011"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "010"),
    "K": ("101", "101", "110", "101", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("110", "101", "101", "101", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "010", "001"),
    "R": ("110", "101", "110", "101", "101"),
    "S": ("011", "100", "010", "001", "110"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    " ": ("000", "000", "000", "000", "000"),
    ".": ("000", "000", "000", "000", "010"),
    ",": ("000", "000", "000", "010", "100"),
    "!": ("010", "010", "010", "000", "010"),
    "?": ("110", "001", "010", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    ";": ("000", "010", "000", "010", "100"),
    "-": ("000", "000", "111", "000", "000"),
    "+": ("000", "010", "111", "010", "000"),
    "=": ("000", "111", "000", "111", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "'": ("010", "010", "000", "000", "000"),
    '"': ("101", "101", "000", "000", "000"),
    "(": ("001", "010", "010", "010", "001"),
    ")": ("100", "010", "010", "010", "100"),
    "_": ("000", "000", "000", "000", "111"),
    "%": ("101", "001", "010", "100", "101"),
    "#": ("101", "111", "101", "111", "101"),
    "<": ("001", "010", "100", "010", "001"),
    ">": ("100", "010", "001", "010", "100"),
}
FONT: dict[str, tuple[str, ...]] = {**DIGITS, **LETTERS}

TEXT_Y = 5  # vertical offset centering the 5-row glyphs on the 16px panel


def normalize_text(text: str) -> str:
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"),
                     ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        text = text.replace(src, dst)
    return text.upper()


def text_columns(text: str) -> list[int]:
    """Text as column bitmasks (bit y set = pixel on), 3 cols + 1 gap per char."""
    cols: list[int] = []
    for ch in normalize_text(text):
        glyph = FONT.get(ch, FONT[" "])
        for x in range(3):
            mask = 0
            for y, row in enumerate(glyph):
                if row[x] == "1":
                    mask |= 1 << y
            cols.append(mask)
        cols.append(0)
    return cols


async def scroll_text(
    client: DivoomClient,
    text: str,
    color: RGB,
    background: RGB,
    fps: float = 10,
) -> None:
    """Stream a right-to-left marquee of `text` across the panel."""
    cols = text_columns(text)
    total_frames = len(cols) + WIDTH  # scroll fully across and out
    t0 = time.monotonic()
    for f in range(total_frames):
        pixels: Pixels = [background] * (WIDTH * HEIGHT)
        for sx in range(WIDTH):
            ci = f - WIDTH + 1 + sx  # text column under screen column sx
            if 0 <= ci < len(cols):
                mask = cols[ci]
                for y in range(5):
                    if mask & (1 << y):
                        pixels[(TEXT_Y + y) * WIDTH + sx] = color
        await client.static_image(pixels)
        await asyncio.sleep(max(0.0, (f + 1) / fps - (time.monotonic() - t0)))


def parse_color(value: str) -> RGB:
    try:
        parts = tuple(int(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Color must be R,G,B") from exc

    if len(parts) != 3 or any(component < 0 or component > 255 for component in parts):
        raise argparse.ArgumentTypeError(
            "Color must contain three values between 0 and 255"
        )
    return parts  # type: ignore[return-value]


def set_pixel(pixels: Pixels, x: int, y: int, color: RGB) -> None:
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        pixels[y * WIDTH + x] = color


# Icon templates, one string per pixel row, X lit in icon_color. Keep the
# identifying shape (ears, folds, tentacle) outside the center of rows
# 5-11, where draw_number carves its count window.
_ICONS = {
    "envelope": (
        "................",
        "................",
        "................",
        ".XXXXXXXXXXXXXX.",
        ".XX..........XX.",
        ".X.X........X.X.",
        ".X..X......X..X.",
        ".X...X....X...X.",
        ".X....X..X....X.",
        ".X.....XX.....X.",
        ".X............X.",
        ".X............X.",
        ".XXXXXXXXXXXXXX.",
        "................",
        "................",
        "................",
    ),
    # Octocat, GitHub-mark style: filled silhouette — ear nubs with a dip
    # between them, wide cheeks, and the tentacle curling out bottom-left.
    # The count window is carved out of the face by draw_number.
    "github": (
        "................",
        "..XX........XX..",
        ".XXXX......XXXX.",
        ".XXXXXXXXXXXXXX.",
        ".XXXXXXXXXXXXXX.",
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXX",
        ".XXXXXXXXXXXXXX.",
        ".XXXXXXXXXXXXXX.",
        "..XXXXXXXXXXXX..",
        "...XXXXXXXXXX...",
        "....XX..........",
        "..XXX...........",
    ),
}


def draw_number(
    pixels: Pixels,
    number: int,
    color: RGB,
    background: RGB,
) -> None:
    text = str(max(0, min(number, 99)))
    width = len(text) * 3 + max(0, len(text) - 1)
    start_x = (WIDTH - width) // 2
    start_y = 6

    # Clear a small badge region inside the envelope.
    for y in range(5, 12):
        for x in range(max(0, start_x - 1), min(WIDTH, start_x + width + 1)):
            set_pixel(pixels, x, y, background)

    for digit_index, digit in enumerate(text):
        glyph = DIGITS[digit]
        origin_x = start_x + digit_index * 4
        for y, row in enumerate(glyph):
            for x, bit in enumerate(row):
                if bit == "1":
                    set_pixel(pixels, origin_x + x, start_y + y, color)


def render_notification(
    count: int,
    icon_color: RGB,
    number_color: RGB,
    background: RGB,
    icon: str = "envelope",
) -> Pixels:
    pixels: Pixels = [icon_color if c == "X" else background
                      for row in _ICONS.get(icon, _ICONS["envelope"])
                      for c in row]
    if count > 0:
        draw_number(pixels, count, number_color, background)
    return pixels


# --- BlueZ pairing agent ------------------------------------------------------

PIN = os.environ.get("TIMEBOX_PIN", "0000")
AGENT_PATH = "/timebox/agent"


class PinAgent(ServiceInterface):
    """Answers the box's legacy PIN requests and authorizes its incoming
    audio connections. Without this, classic connects stall in a desktop
    PIN popup or get refused.

    Scoped strictly to the configured box: while registered as the default
    agent it fields pairing requests for the whole desktop, so anything
    that is not the box is rejected — otherwise a nearby hostile device
    could pair with the host silently."""

    def __init__(self, address: str) -> None:
        super().__init__("org.bluez.Agent1")
        self._dev_suffix = "dev_" + address.replace(":", "_").lower()

    def _check(self, device: str) -> None:
        if not device.lower().endswith(self._dev_suffix):
            raise DBusError("org.bluez.Error.Rejected", "not the TimeBox")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        self._check(device)
        return PIN

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        self._check(device)
        if not PIN.isdigit():
            raise DBusError("org.bluez.Error.Rejected", "TIMEBOX_PIN not numeric")
        return int(PIN)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u") -> None:
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o") -> None:
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s") -> None:
        self._check(device)

    @method()
    def Cancel(self) -> None:
        pass

    @method()
    def Release(self) -> None:
        pass


async def start_agent(address: str) -> MessageBus:
    """Register as default BlueZ agent (scoped to `address`) for the
    lifetime of this process."""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    bus.export(AGENT_PATH, PinAgent(address))
    intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface(
        "org.bluez.AgentManager1"
    )
    await mgr.call_register_agent(AGENT_PATH, "KeyboardOnly")
    await mgr.call_request_default_agent(AGENT_PATH)
    return bus


# --- Bluetooth link management: LE control window + A2DP audio ----------------


def btctl(*args: str, timeout: int = 25) -> tuple[int, str]:
    """Run bluetoothctl. A hung helper is a failed call, never an exception —
    bluetoothctl can block indefinitely on a wedged adapter, and that must not
    take the daemon down."""
    try:
        proc = subprocess.run(
            ["bluetoothctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 1, f"bluetoothctl {' '.join(args)} timed out after {timeout}s"
    return proc.returncode, proc.stdout + proc.stderr


async def connect_le(address: str) -> DivoomClient:
    """Win the LE window against the box's classic auto-reconnect.

    The box only advertises LE while its classic link is down, and (being a
    trusted speaker) it re-pages us within seconds of any disconnect. So:
    drop classic, catch the advertisement, connect — and if the box wins
    the race, kick it off and try again.
    """
    last: Exception | None = None
    for attempt in range(5):
        device = await BleakScanner.find_device_by_address(address, timeout=8)
        if device is None:
            # Not advertising — usually because the classic link is up. Kick
            # it (the box only re-pages after real link loss, not after this
            # clean disconnect, so audio needs re-dialing afterwards). Not on
            # the final attempt: no scan follows, the kick would only cost us
            # the audio link for nothing.
            if attempt < 4:
                _, info = btctl("info", address)
                if "Connected: yes" in info:
                    btctl("disconnect", address)
                    await asyncio.sleep(1.0)
            continue
        # The box advertises as dual-mode, so a fresh BlueZ device object
        # defaults to BR/EDR on connect — which the box answers by killing
        # its LE side. Pinning the bearer is what makes BLE stick; it needs
        # Experimental = true in /etc/bluetooth/main.conf.
        btctl("bearer", address, "le")

        client = DivoomClient()
        try:
            await client.connect(device)
            await client.init_session()
            return client
        except Exception as exc:
            last = exc
            try:
                await client.disconnect()
            except Exception:
                pass

    raise RuntimeError(f"could not establish BLE control after 5 attempts: {last}")


def connect_a2dp(address: str) -> None:
    """Dial out for audio if the box hasn't already reconnected itself."""
    # PreferredBearer applies to ALL outgoing connects — with it still on
    # "le", the A2DP dial-out collides with the live BLE link (br-connection-
    # busy). Flip to bredr for the dial; connect_le re-pins le on each run.
    btctl("bearer", address, "bredr")
    last = ""
    # The box's page scan naps; landing a page can take several patient
    # tries with growing gaps. ponytail: ~60s worst case before giving up.
    for attempt in range(5):
        rc, out = btctl("connect", address, A2DP_SINK_UUID, timeout=30)
        if rc == 0:
            return
        last = out.strip().splitlines()[-1] if out.strip() else "unknown"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"A2DP connect failed after retries: {last}")


def timebox_sink(address: str) -> str | None:
    sinks = subprocess.run(
        ["pactl", "list", "short", "sinks"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    address_marker = address.replace(":", "_")
    return next(
        (line.split("\t")[1] for line in sinks.splitlines() if address_marker in line),
        None,
    )


def wait_for_sink(address: str, timeout_s: float) -> str | None:
    card = "bluez_card." + address.replace(":", "_")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sink = timebox_sink(address)
        if sink:
            return sink
        # Card may come up in headset profile (mono 16kHz) — force A2DP.
        subprocess.run(
            ["pactl", "set-card-profile", card, "a2dp-sink"],
            capture_output=True,
        )
        time.sleep(0.5)
    return None


def bring_up_audio(address: str, initial_wait: float = 15) -> str:
    """Return the box's PipeWire sink, waiting/dialing as needed.

    The box (trusted) re-pages us moments after losing its classic link, so
    often the sink just appears on its own alongside the live BLE link —
    hence wait first, dial only if it doesn't come. `initial_wait` is long
    for one-shot CLI use (box may be mid-repage) and short in the daemon
    (which retries per notification anyway).
    """
    sink = wait_for_sink(address, initial_wait)
    if sink is None:
        connect_a2dp(address)
        sink = wait_for_sink(address, 8)
    if sink is None:
        raise RuntimeError("TimeBox audio sink did not appear")
    return sink


def play_sound(address: str, sound: str, initial_wait: float = 15) -> None:
    sink = bring_up_audio(address, initial_wait)
    subprocess.run(["pw-play", "--target", sink, sound], check=True)


# --- standalone CLI -----------------------------------------------------------


async def send_notification(args: argparse.Namespace) -> None:
    pixels = render_notification(
        count=args.count,
        icon_color=args.icon_color,
        number_color=args.number_color,
        background=args.background,
    )

    agent_bus = await start_agent(args.address)
    try:
        client = await connect_le(args.address)
        try:
            if args.brightness is not None:
                await client.set_brightness(args.brightness)

            tasks = []
            if args.text:
                tasks.append(scroll_text(client, args.text, args.icon_color, args.background))
            else:
                tasks.append(client.static_image(pixels))
            if not args.silent:
                # Classic A2DP coexists with BLE, but only when BLE came first.
                print("display sent; bringing up audio (can take a minute)...")
                tasks.append(asyncio.to_thread(play_sound, args.address, args.sound))
            await asyncio.gather(*tasks)
        finally:
            await client.disconnect()
    finally:
        agent_bus.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Display an envelope notification on a TimeBox Evo."
    )
    parser.add_argument(
        "--address",
        default=DEFAULT_ADDRESS,
        help="Bluetooth address (default: $TIMEBOX_ADDRESS)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="Notification count from 0 to 99 (default: 1)",
    )
    parser.add_argument(
        "--text",
        help="Scroll this text instead of showing the envelope icon",
    )
    parser.add_argument(
        "--brightness",
        type=int,
        choices=range(0, 101),
        metavar="0-100",
        default=None,
        help="Display brightness (default: leave unchanged)",
    )
    parser.add_argument(
        "--icon-color",
        type=parse_color,
        default=(255, 60, 40),
        metavar="R,G,B",
        help="Envelope color (default: 255,60,40)",
    )
    parser.add_argument(
        "--number-color",
        type=parse_color,
        default=(255, 255, 255),
        metavar="R,G,B",
        help="Count color (default: 255,255,255)",
    )
    parser.add_argument(
        "--background",
        type=parse_color,
        default=(0, 0, 0),
        metavar="R,G,B",
        help="Background color (default: 0,0,0)",
    )
    parser.add_argument(
        "--sound",
        default=DEFAULT_SOUND,
        help=f"Sound file to play (default: {DEFAULT_SOUND})",
    )
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Do not play a notification sound",
    )
    return parser


def send_to_daemon(params: dict) -> bool:
    """Write one notification request to a running daemon's FIFO.

    Returns False (never raising) when no daemon is listening, so callers
    can fall back to doing the work themselves. Shared by the CLI and the
    KDE bridge.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return False  # never fall back to a world-writable /tmp FIFO
    try:
        fd = os.open(os.path.join(runtime_dir, "timebox.fifo"),
                     os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False  # no FIFO or no reader
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(params) + "\n")
    return True


def try_daemon(args: argparse.Namespace) -> bool:
    """If timebox_daemon.py is running, hand the CLI notification to it."""
    if args.address and args.address != DEFAULT_ADDRESS:
        return False  # daemon serves $TIMEBOX_ADDRESS; a different box is ours
    params = {
        "count": args.count,
        "icon_color": list(args.icon_color),
        "number_color": list(args.number_color),
        "background": list(args.background),
        "sound": args.sound,
        "silent": args.silent,
    }
    if args.brightness is not None:
        params["brightness"] = args.brightness
    if args.text:
        params["text"] = args.text
    return send_to_daemon(params)


def main() -> int:
    args = build_parser().parse_args()

    if try_daemon(args):
        print("Notification handed to running daemon")
        return 0

    if not args.address:
        print("error: no address — set TIMEBOX_ADDRESS or pass --address",
              file=sys.stderr)
        return 2

    if not valid_address(args.address):
        print(f"error: {args.address!r} is not a Bluetooth MAC (AA:BB:CC:DD:EE:FF)",
              file=sys.stderr)
        return 2

    if not 0 <= args.count <= 99:
        print("error: --count must be between 0 and 99", file=sys.stderr)
        return 2

    try:
        asyncio.run(send_notification(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"TimeBox communication failed: {exc}", file=sys.stderr)
        return 1

    print(f"Notification sent to {args.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
