# Examples

Everything goes as one JSON line into the daemon's FIFO. All snippets
below assume:

```bash
FIFO=$XDG_RUNTIME_DIR/timebox.fifo
```

All keys and their defaults: see the table in the [README](README.md).
Every visualizer knob switches live — send a line with just the knobs
you want changed, everything else stays as it is.

## Notifications

```bash
# Envelope icon with unread count, default chime
echo '{"count": 3}' > $FIFO

# Scrolling text (A-Z, digits, punctuation; umlauts transliterated)
echo '{"text": "Build failed!"}' > $FIFO

# Green, faster scroll, no sound
echo '{"text": "Deploy OK", "icon_color": [0,255,80], "fps": 15, "silent": true}' > $FIFO

# Custom sound and colors
echo '{"count": 7, "icon_color": [40,200,255], "sound": "/usr/share/sounds/ocean/stereo/bell.oga"}' > $FIFO

# GitHub octocat icon instead of the envelope (the KDE bridge sends this
# automatically while gitify is the only app with unread notifications)
echo '{"count": 2, "icon": "github", "silent": true}' > $FIFO
```

## Visualizer

```bash
# Live 16-band spectrum of whatever the system is playing
echo '{"visualizer": true}' > $FIFO            # endless
echo '{"visualizer": true, "seconds": 30}' > $FIFO  # fixed duration
echo '{"visualizer": false}' > $FIFO           # stop

# Nudge a single knob while it runs — everything else stays as it is
echo '{"visualizer": true, "spin": 1}' > $FIFO
```

Frame pacing follows the BLE link: colorful recipes (many bands +
stereo) make bigger frames, and when they exceed what the radio
carries (~60 chunks/s) the visualizer drops frames instead of falling
behind the music. Notifications sent while the visualizer runs are
drawn on top of it, over an opaque band, so they stay legible.

### Bars

```bash
# The default: 16 bands rising from the bottom
echo '{"visualizer": true, "mode": "bars"}' > $FIFO

# Stereo bars: left channel rises from the bottom, right falls from the top
echo '{"visualizer": true, "mode": "bars", "stereo": true}' > $FIFO
```

### Tunnel

```bash
# Psychedelic tunnel: spectrum wraps the border, history sinks toward the center
echo '{"visualizer": true, "mode": "tunnel"}' > $FIFO
echo '{"visualizer": true, "mode": "tunnel", "spin": 2}' > $FIFO  # faster rotation
echo '{"visualizer": true, "fade": 0.7, "bands": 16}' > $FIFO  # tune the look live

# Calm stereo tunnel: no rotation, soft glow, chunky bands
echo '{"visualizer": true, "mode": "tunnel", "stereo": true, "fade": 0.8, "bands": 30, "spin": 0}' > $FIFO

# Slow-motion deep tunnel: long history glow, gentle drift
echo '{"visualizer": true, "mode": "tunnel", "fade": 0.97, "spin": 0.2}' > $FIFO

# Strobe-y and chunky: coarse bands, hard fade, fast reverse spin
echo '{"visualizer": true, "mode": "tunnel", "bands": 8, "fade": 0.5, "spin": -3}' > $FIFO
```

### Wave

A scrolling 16-band spectrogram: each frame's spectrum becomes one
line of pixels and drifts across the panel. `fade` sets the trail
length; `spin`/`bands` don't apply. At 10 fps the panel holds 1.6 s of
history (0.8 s per side in stereo). `rainbow` tells you *which* band
is loud by color; `heat` tells you *how* loud and looks the most like
a classic spectrogram. Anything panned shows up in stereo mode as
asymmetry between the two halves.

```bash
# The default: rainbow river — spectrum flows right→left, hue = frequency
echo '{"visualizer": true, "mode": "wave"}' > $FIFO

# SDR waterfall: history falls from the top, heat colors like a real spectrogram
echo '{"visualizer": true, "mode": "wave", "dir": "v", "palette": "heat"}' > $FIFO

# Long glowing trail: old columns barely dim — good for slow, ambient material
echo '{"visualizer": true, "mode": "wave", "fade": 0.97}' > $FIFO

# Comet: hard fade, only the newest few columns visible — punchy for beats
echo '{"visualizer": true, "mode": "wave", "fade": 0.5}' > $FIFO

# Full-persistence scroll: no dimming, the whole history at equal brightness
echo '{"visualizer": true, "mode": "wave", "fade": 1.0}' > $FIFO

# Stereo butterfly: L and R enter at the middle and drift apart horizontally —
# mono material renders mirror-symmetric, wide mixes visibly desync the wings
echo '{"visualizer": true, "mode": "wave", "stereo": true}' > $FIFO

# Stereo curtain: vertical split — right channel rises, left falls from the middle
echo '{"visualizer": true, "mode": "wave", "stereo": true, "dir": "v"}' > $FIFO

# Heat + stereo + long trail: a slow two-sided ember field
echo '{"visualizer": true, "mode": "wave", "stereo": true, "palette": "heat", "fade": 0.97}' > $FIFO
```

## Clock

```bash
# Pin which pages the box's clock cycles through (~15 s each)
echo '{"clock": ["time", "weather"]}' > $FIFO
echo '{"clock": ["time"], "clock_color": [255,180,0]}' > $FIFO  # steady amber clock

# While the visualizer runs, give the clock 30 s of air time every 10 min
echo '{"clock_flash": 30, "clock_every": 600}' > $FIFO
echo '{"clock_flash": 0}' > $FIFO  # never interrupt the visualizer
```

## Shell integration

```bash
# Long-running command, notify on completion
make -j8; echo "{\"text\": \"make: exit $?\"}" > $FIFO

# Cron: top of every hour, quietly (cron has no $XDG_RUNTIME_DIR — spell it out)
0 * * * * echo '{"text": "'"$(date +\%H:00)"'", "silent": true}' > /run/user/1000/timebox.fifo
```
