# timebox

Notifications on a Divoom TimeBox Evo from Linux: icons or scrolling
text on the 16x16 panel, with a sound from the box speaker. Control
goes over BLE, sound over classic A2DP, both held simultaneously.

Why the architecture looks the way it does: [TBX-26-001](docs/TBX-26-001-notification-pipeline.md).

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
# Config (address required; PIN only if your box asks for one)
mkdir -p ~/.config/timebox
printf 'TIMEBOX_ADDRESS=<box-mac>\nTIMEBOX_PIN=<pin>\n' > ~/.config/timebox/env
chmod 600 ~/.config/timebox/env

# Link, enable and start the unit straight from this repo
systemctl --user enable --now $(pwd)/timebox-daemon.service

# Logs / status
journalctl --user -u timebox-daemon -f
systemctl --user status timebox-daemon
```

The unit assumes the repo lives at `~/Documents/Source/timebox` —
adjust `WorkingDirectory`/`ExecStart` in `timebox-daemon.service`
if yours is elsewhere. It restarts automatically (15 s backoff) if
the box is unreachable.

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
echo '{"visualizer": false}' > $FIFO           # stop

# Notifications sent while the visualizer runs are drawn on top of the
# bars, over an opaque band, so they stay legible.
```

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
