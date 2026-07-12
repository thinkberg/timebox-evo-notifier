TECHNICAL MEMORANDUM                                         TBX-26-001

SUBJECT:      Divoom TimeBox Evo as a Linux notification display
              with sound - findings and resulting architecture
DATE:         2026-07-12
STATUS:       FINAL - pipeline operational, validated end to end


SUMMARY. The TimeBox Evo now serves as a notification display with
sound from Linux: scrolling text or icon on the 16x16 panel plus a
chime from the box speaker, delivered by one write to a FIFO, with
sub-second latency. Control is BLE-only on this hardware revision;
the classic serial protocol is dead. The working hypothesis at the
start - that the classic protocol would be simpler than BLE - was
disproven on day one. Every real obstacle was in link management:
BlueZ bearer selection on the host, and the box's classic
connection policy. The resolution is a resident daemon that
establishes both links once and never releases them.

1. BACKGROUND. The box was to show notifications, ideally with
sound. Prior observation: BLE control and Bluetooth audio appeared
mutually exclusive. Reverse-engineered BLE protocol support existed
(divoom_protocol 0.2.0, from iOS HCI captures). One day of
systematic probing (2026-07-12) produced the findings below.

2. FINDINGS. Established by direct test against the hardware.

   2.1 The box is one MAC address with two identities:
   "TimeBox-Evo-light" (BLE: control, FE EF AA 55 envelope, iOS
   init ritual) and "TimeBox-Evo-audio" (classic: A2DP sink only).
   The classic SPP channel accepts connections but answers nothing
   in either documented framing; the older RomRider/hass-divoom
   classic protocol does not apply to this revision.

   2.2 BlueZ merges both identities into one device object. On
   Connect(), BlueZ prefers BR/EDR because the LE advertisement
   carries the dual-mode flag; the box answers a classic connect
   by killing its LE side. Fix: pin the bearer with `bluetoothctl
   bearer <mac> le` between scan and connect. Requires
   Experimental = true in /etc/bluetooth/main.conf (set, in
   effect). The pin does not survive device-object churn; it is
   re-applied on every connect.

   2.3 PreferredBearer applies to ALL outgoing connects. With the
   pin on "le", an A2DP dial collides with the live BLE link
   (br-connection-busy). The audio dial must flip to "bredr"
   first; the next LE connect re-pins "le".

   2.4 Link order is fixed: BLE first, classic second. Both links
   then coexist indefinitely (verified: image - sound - image over
   simultaneously held links). The reverse order blocks: the box
   does not accept new LE connections reliably while paging, and
   host-initiated dials against a settled box fail (2.5).

   2.5 Box classic policy, observed black-box: it pages the host
   aggressively at power-on and after link LOSS; it does not
   re-page after a clean host-initiated disconnect. When settled,
   its page scan naps: outgoing dials mostly time out
   (br-connection-canceled); dials stacked inside one attempt
   window return br-connection-busy. A stuck pending attempt is
   cleared by `bluetoothctl block` + `unblock`. The only reliable
   audio trigger is box-initiated: keep the device TRUSTED so its
   boot-page latches silently.

   2.6 The box sometimes demands a legacy PIN on classic connects.
   Desktop agent popups are too short-lived to answer; an own
   BlueZ agent (registered by the daemon) answers the PIN (from
   the TIMEBOX_PIN environment variable) and authorizes the box's
   incoming audio profile.

   2.7 After a bluetoothd restart, WirePlumber loses its BlueZ
   registration: transports exist at BlueZ level but no card/sink
   appears. `systemctl --user restart wireplumber` repairs it.

   2.8 Display rates over a held BLE link: 3.7 fps with
   acknowledged writes (hard floor), 10 fps streamed verified
   smooth on-panel. Native 0x8b animation upload caps at 255
   chunks x 256 B = ~64 KB = ~1450 two-color frames = ~355
   characters of 3x5 marquee at 1 px/frame. Streamed marquee
   length is unbounded (~2.5 chars/s at 10 fps).

   2.9 Defect found in divoom_protocol 0.2.0: encode_image()
   crashes on frames with 256 unique colors (palette count
   overflows the byte; wire format wants 0x00 for 256). Open.

3. ASSUMPTIONS. Believed, not proven. The box firmware serves one
classic host and drops LE for classic when the host mixes bearers
(inferred from behavior, no firmware source). Page-scan napping is
inferred from timing patterns, not documented. Phone apps are
assumed to use the same LE-first order; not captured.

4. CONCLUSIONS. Link churn is the enemy. Every failure mode of the
day - bearer races, busy storms, lost audio - occurred during link
setup or teardown; none occurred while links were simply held. The
correct architecture is therefore a resident process holding both
links for its lifetime (the phone-app model), not a per-
notification script. Where the box has a policy (it wants to own
the audio link), the host must yield to it, not fight it: trust
the device, let its boot-page latch, never cleanly disconnect what
you want back.

5. ACTION. In effect:

   5.1 timebox_daemon.py holds BLE + A2DP; notifications via
   $XDG_RUNTIME_DIR/timebox.fifo, JSON per line: text (scrolls,
   umlauts transliterated), count, icon_color, number_color,
   background, brightness, sound, silent, fps.

   5.2 timebox_notify.py remains as standalone fallback (slower:
   reconnects per call) and hosts the shared protocol/render code
   including the 3x5 marquee font.

   5.3 The encode_image() 256-color defect (2.9) is worked around
   in timebox_notify.py without touching the library: frames with
   256 unique colors have their two closest colors merged to 255
   before encoding (imperceptible on a 16x16 panel; verified
   byte-identical output for normal frames). A proper fix in
   divoom_protocol (palette byte 0x00 = 256) remains open
   upstream. Obsolete test.py deleted.

   5.4 The daemon runs as a systemd user service
   (timebox-daemon.service, enabled, linked from the repo).
   Address and PIN live in ~/.config/timebox/env (mode 600),
   outside the repo. Restart=on-failure covers box-unreachable
   periods. No ACTION items remain open except the upstream
   library fix (5.3). After a box power cycle the first
   notification pays ~15 s reconnect; all subsequent ones are
   instant.
