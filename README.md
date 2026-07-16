# timebox

> **⚠️ This repository is an experiment in AI-assisted development.**
> Nearly all code and documentation here was written by an AI agent in
> a single day, driven by live trial against the hardware. It works,
> and it was security-reviewed at the end ([TS-26-002](docs/TS-26-002-code-production-quality-trade-study.txt)
> records the five security issues that process created and how they
> were caught) — but use it with the appropriate skepticism: residual
> risks may remain. Read the code before running it, especially the
> parts that touch pairing, the system D-Bus, and your audio stack.

Notifications on a Divoom TimeBox Evo from Linux: icons or scrolling
text on the 16x16 panel, with a sound from the box speaker. Control
goes over BLE, sound over classic A2DP, both held simultaneously.

Why the architecture looks the way it does: [TBX-26-001](docs/TBX-26-001-notification-pipeline.txt) —
all memos in the [register](docs/REGISTER.md).

![The TimeBox Evo running the live audio visualizer during development](coding.jpg)

## Setup (one-time)

```bash
# 1. BlueZ experimental interfaces (for per-device bearer pinning)
sudo sed -i 's/^#Experimental = false/Experimental = true/' /etc/bluetooth/main.conf
sudo systemctl restart bluetooth
systemctl --user restart wireplumber   # wireplumber loses BlueZ on restart

# 2. Find your box's MAC (shows up as "TimeBox-Evo-light" /
#    "TimeBox-Evo-audio" while scanning) and export it:
bluetoothctl scan on
export TIMEBOX_ADDRESS=<box-mac>

# 3. Pair the box (classic bond) and trust it — trusted means its
#    audio link latches silently at every power-on
bluetoothctl pair $TIMEBOX_ADDRESS
bluetoothctl trust $TIMEBOX_ADDRESS

# If your box asks for a PIN, export it so the daemon/CLI can answer
# pairing prompts automatically (default: 0000):
export TIMEBOX_PIN=<your-pin>

# 4. Python environment
python -m venv .venv
.venv/bin/pip install bleak dbus_fast divoom_protocol
```

## Daemon (recommended)

Connects once, holds both links, serves notifications from a FIFO
with sub-second latency:

```bash
.venv/bin/python timebox_daemon.py &
```

### Install as a systemd user service

```bash
# Config (address required; PIN only if your box asks for one;
# LATLON only if you want weather on the box's clock — see below)
mkdir -p ~/.config/timebox
printf 'TIMEBOX_ADDRESS=<box-mac>\nTIMEBOX_PIN=<pin>\nTIMEBOX_LATLON=<lat>,<lon>\n' > ~/.config/timebox/env
chmod 600 ~/.config/timebox/env

# App + venv into ~/.local/share/timebox
mkdir -p ~/.local/share/timebox
cp timebox_daemon.py timebox_bridge.py timebox_notify.py ~/.local/share/timebox/
python -m venv ~/.local/share/timebox/.venv
~/.local/share/timebox/.venv/bin/pip install bleak dbus_fast divoom_protocol

# Unit into ~/.config/systemd/user, then enable and start
cp timebox-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now timebox-daemon

# Logs / status
journalctl --user -u timebox-daemon -f
systemctl --user status timebox-daemon
```

The units run the copy in `~/.local/share/timebox` — adjust
`WorkingDirectory`/`ExecStart` in the `.service` files if you install
elsewhere. The daemon restarts automatically (15 s backoff) if the
box is unreachable.

Then notifications are one shell line — JSON per line into the FIFO:

```bash
FIFO=$XDG_RUNTIME_DIR/timebox.fifo

# Envelope icon with unread count, default chime
echo '{"count": 3}' > $FIFO

# Scrolling text (A-Z, digits, punctuation; umlauts transliterated)
echo '{"text": "Build failed!"}' > $FIFO

# Green, faster scroll, no sound
echo '{"text": "Deploy OK", "icon_color": [0,255,80], "fps": 15, "silent": true}' > $FIFO

# Custom sound and colors
echo '{"count": 7, "icon_color": [40,200,255], "sound": "/usr/share/sounds/ocean/stereo/bell.oga"}' > $FIFO

# Live 16-band spectrum of whatever the system is playing
echo '{"visualizer": true}' > $FIFO            # endless
echo '{"visualizer": true, "seconds": 30}' > $FIFO  # fixed duration
echo '{"visualizer": true, "mode": "tunnel"}' > $FIFO  # psychedelic tunnel
echo '{"visualizer": true, "mode": "tunnel", "spin": 2}' > $FIFO  # faster rotation
echo '{"visualizer": true, "fade": 0.7, "bands": 16}' > $FIFO  # tune the look live
echo '{"visualizer": true, "stereo": true}' > $FIFO  # split L/R channels
echo '{"visualizer": false}' > $FIFO           # stop

# Some favorite recipes — every knob switches live, no restart needed:

# Calm stereo tunnel: no rotation, soft glow, chunky bands
echo '{"visualizer": true, "mode": "tunnel", "stereo": true, "fade": 0.8, "bands": 30, "spin": 0}' > $FIFO

# Slow-motion deep tunnel: long history glow, gentle drift
echo '{"visualizer": true, "mode": "tunnel", "fade": 0.97, "spin": 0.2}' > $FIFO

# Strobe-y and chunky: coarse bands, hard fade, fast reverse spin
echo '{"visualizer": true, "mode": "tunnel", "bands": 8, "fade": 0.5, "spin": -3}' > $FIFO

# Stereo bars: left channel rises from the bottom, right falls from the top
echo '{"visualizer": true, "mode": "bars", "stereo": true}' > $FIFO

# Nudge a single knob while it runs — everything else stays as it is
echo '{"visualizer": true, "spin": 1}' > $FIFO

# Frame pacing follows the BLE link: colorful recipes (many bands + stereo)
# make bigger frames, and when they exceed what the radio carries (~60
# chunks/s) the visualizer drops frames instead of falling behind the music.

# Notifications sent while the visualizer runs are drawn on top of the
# bars, over an opaque band, so they stay legible.
```

The visualizer records the default sink's *monitor* (what the speakers
play — never the microphone), which KDE reports with the mic-in-use tray
icon, labeled "TimeBox visualizer" — but only while a real microphone
device is present; with none attached KDE shows nothing at all. In
endless mode 10 s of silence hands the panel back to the clock; the
capture — and with it the icon — pauses once the sink actually goes idle
(a silent-but-open stream, like a paused AirPlay sender, keeps both
alive). Everything resumes when audio plays again.

All keys (each optional):

| Key            | Meaning                                    | Default            |
|----------------|--------------------------------------------|--------------------|
| `text`         | scroll this text instead of the icon       | —                  |
| `fps`          | scroll speed in frames/s (1 px per frame)  | `10`               |
| `count`        | badge number 0–99 on the envelope icon     | `1`                |
| `icon_color`   | icon / text color `[r,g,b]`                | `[255,60,40]`      |
| `number_color` | badge color `[r,g,b]`                      | `[255,255,255]`    |
| `background`   | background color `[r,g,b]`                 | `[0,0,0]`          |
| `brightness`   | panel brightness 0–100                     | unchanged          |
| `sound`        | audio file to play through the box         | message chime      |
| `silent`       | `true` = no sound                          | `false`            |
| `visualizer`   | `true` starts the live spectrum, `false` stops it | —           |
| `seconds`      | visualizer duration; omit for endless      | endless            |
| `mode`         | visualizer look: `"bars"` (16 bands), or `"tunnel"` — spectrum rings flowing inward, fading with age, one band per border pixel (60); switchable while running | `"bars"` |
| `spin`         | tunnel rotation in border px per frame, −5…5; negative reverses, 0 stops; switchable while running | `0.5` |
| `fade`         | tunnel per-ring brightness decay toward the center, 0…1; switchable while running | `0.92` |
| `bands`        | tunnel analyzer width, 2…60; switchable while running | `60` |
| `stereo`       | analyze L/R separately: bars mirror bottom (L) / top (R) at half height, tunnel splits into semicircles with the same frequency diametrically opposite; switchable while running | `false` |
| `clock`        | pin the sub-views the box's clock cycles through: list from `"time"`, `"weather"`, `"temp"`, `"date"`, e.g. `{"clock": ["time","weather"]}`; each is a full-screen page (weather: an animated scene) shown ~15 s in turn; replayed whenever the daemon restores the clock | `TIMEBOX_CLOCK` env, or `time,weather` |
| `clock_color`  | clock color `[r,g,b]` (only with `clock`)  | white              |
| `clock_flash`  | seconds the clock interrupts a running visualizer per flash; `0` = never | `30` |
| `clock_every`  | seconds between clock flashes (also the weather re-push cadence); applies from the next cycle | `600` |

## Weather on the clock

The box's built-in clock/weather display expects the Divoom phone app to
feed it — without one it shows stale data forever. With
`TIMEBOX_LATLON=<lat>,<lon>` set, the daemon takes over: it fetches the
current temperature and conditions every 30 minutes from
[Bright Sky](https://brightsky.dev) (a free, keyless JSON API for DWD
open data) and re-pushes them to the box every `clock_every` seconds.
Leave the variable unset to skip the weather fetch.

The idle panel shows the box's clock channel. Its enabled sub-views are
full-screen pages the box cycles through (~15 s each): plain time, an
*animated* weather scene, temperature, date — the firmware cannot merge
them onto one page. Pick pages with the `clock` key, e.g.
`echo '{"clock": ["time","weather"]}' > $XDG_RUNTIME_DIR/timebox.fifo`,
or set the boot default with `TIMEBOX_CLOCK=time,weather` in the env
file (a plain `TIMEBOX_CLOCK=time` means a steady, never-cycling
clock).
While the visualizer runs, the clock still gets air time: `clock_flash`
seconds every `clock_every` seconds (default 30 s every 10 min, `0`
disables), after which the spectrum takes the panel back.

The daemon also keeps the box from wandering: it sets the box's startup
channel to the clock (persistent, so a power-cycled box boots into the
clock instead of its demo carousel) and re-asserts the clock on an idle
panel every `clock_every` seconds — a channel switched via the box's
hardware button finds its way back within one cycle.

## KDE notifications on the box

`timebox_bridge.py` mirrors KDE's notification bell: allow-listed apps bump an
unread **count badge** on the panel (envelope + number). It listens on the
session bus, so KDE's own notifications keep working untouched — and only the
count is ever sent to the box, never the message text.

```bash
# Which apps reach the box (nothing is forwarded if unset).
# The name is the app's D-Bus app_name — see it with:
#   dbus-monitor --session "interface='org.freedesktop.Notifications'"
echo 'TIMEBOX_ONLY_APPS=Thunderbird,Nextcloud' >> ~/.config/timebox/env

cp timebox-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now timebox-bridge
journalctl --user -u timebox-bridge -f
```

The badge counts notifications that are still unread: dismissing one (or
clicking it) decrements it, while one that merely times out on screen stays
counted — same as the bell icon. While the visualizer runs, an arriving badge
shows on top of the bars for a few seconds instead of persisting.

How it works and why it eavesdrops rather than replaces KDE's notification
daemon: [TBX-26-003](docs/TBX-26-003-kde-notification-bridge.txt).

## One-shot CLI

Same options as flags. If the daemon is running, the CLI hands the
notification to it and returns instantly; otherwise it connects
itself (~10–20 s, audio may retry for up to a few minutes):

```bash
.venv/bin/python timebox_notify.py --count 5
.venv/bin/python timebox_notify.py --text "Kaffee ist fertig!" --icon-color 255,180,0
.venv/bin/python timebox_notify.py --count 12 --silent --brightness 60
```

## Practical examples

```bash
# Long-running command, notify on completion
make -j8; echo "{\"text\": \"make: exit $?\"}" > $XDG_RUNTIME_DIR/timebox.fifo

# Cron: top of every hour, quietly
0 * * * * echo '{"text": "'"$(date +\%H:00)"'", "silent": true}' > /run/user/1000/timebox.fifo
```

## Security notes

- **The panel is a public display.** The box's BLE side is unencrypted
  and unauthenticated by design (its firmware never pairs the LE link):
  everything you display — including notification text — is readable by
  a BLE sniffer in radio range, and anyone in range can connect and draw
  on the panel. Don't send confidential content. Audio (A2DP) is
  link-encrypted via the classic bond.
- **The FIFO is your trust boundary.** It is created 0600 inside
  `$XDG_RUNTIME_DIR` (the daemon refuses to start without it); whoever
  can write it can display content, start the visualizer, and play any
  file readable by your user (`sound` key).
- The daemon registers a Bluetooth agent that auto-answers pairing and
  authorization **only for the configured box address**; requests from
  any other device are rejected.
- The daemon's journal log records notification keys, not content.
- The KDE bridge eavesdrops the session bus, so it *sees* every notification
  in-process — but it reads only the app name, forwards only a count, and logs
  neither. No notification text ever reaches the box (or the air).

## Troubleshooting

- **First notification after box power-cycle is slow (~15 s)** —
  expected; the daemon re-establishes BLE, everything after is instant.
- **No sound, `br-connection-busy`** — the box only reliably brings
  audio up itself, at power-on. Power-cycle the box; trusted, it
  latches within seconds. A stuck connect attempt clears with
  `bluetoothctl block <mac>` + `unblock`.
- **Sink missing although connected** — `systemctl --user restart
  wireplumber` (happens after bluetoothd restarts).
- **A PIN dialog appears** — the daemon answers it itself while
  running, using `$TIMEBOX_PIN` (default `0000`). Set the variable
  in the daemon's environment if your box uses a different PIN.
