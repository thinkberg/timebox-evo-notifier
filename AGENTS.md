# Timebox contributor guide

## Project map

- `timebox_notify.py` is the shared BLE, audio, and 16×16 rendering library; it also provides the one-shot CLI.
- `timebox_daemon.py` owns the persistent device links and accepts JSON requests through a private FIFO.
- `timebox_bridge.py` mirrors allow-listed KDE notification counts to that FIFO. Never forward notification content.
- `test_render.py` is the framework-free check for pure logic.
- `docs/REGISTER.md` indexes the engineering memos.

## Working rules

- Keep the dependency-free, single-module structure unless a real need changes it.
- Preserve security boundaries: validate FIFO input, keep its permissions private, restrict Bluetooth pairing/authorization to the configured TimeBox, and do not log notification text or secrets.
- Treat Bluetooth and PipeWire behavior as hardware integration work: run the pure check first, then verify changed device behavior against a real box when available. Do not claim hardware verification otherwise.
- For behavior or architectural changes, update the relevant user docs and add a memo using the next register number in the same change.

## Check

```bash
.venv/bin/python test_render.py
```
