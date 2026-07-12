#!/usr/bin/env python3
"""Mirror KDE's notification bell onto the TimeBox as an unread count badge.

Eavesdrops the session bus (BecomeMonitor) instead of replacing KDE's
notification daemon: desktop notifications keep working exactly as before,
and allow-listed ones additionally bump a count badge on the panel.

Only the COUNT is sent to the box — never notification text. The box's BLE
link is unencrypted, so anything displayed is readable by a sniffer in radio
range; a bare number leaks nothing. Notification content is never logged
either.

Config (~/.config/timebox/env):
    TIMEBOX_ONLY_APPS=Thunderbird,Nextcloud   # nothing is forwarded if empty

Run:  TIMEBOX_ONLY_APPS=... python timebox_bridge.py
"""

import asyncio
import os
import sys

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

from timebox_notify import send_to_daemon

# NotificationClosed reasons (org.freedesktop.Notifications spec).
CLOSED_EXPIRED = 1  # timed out on screen — stays unread, like KDE's bell
CLOSED_DISMISSED = 2  # user dismissed it
CLOSED_BY_CALL = 3  # closed by the app (usually because the user clicked it)

MATCH_RULES = [
    "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    # The service's own traffic: the method return carrying the new id, and
    # the NotificationClosed signal. Well-known sender names are resolved to
    # the current owner by the bus, so a plasmashell restart is transparent.
    "sender='org.freedesktop.Notifications'",
]


def allowed_apps() -> set[str]:
    raw = os.environ.get("TIMEBOX_ONLY_APPS", "")
    return {a.strip().lower() for a in raw.split(",") if a.strip()}


class UnreadTracker:
    """Which allow-listed notifications are still unread.

    A Notify call only yields its notification id in the method return, so
    pending serials are matched to their reply. Notifications that merely
    expire on screen stay unread (KDE keeps them in the bell's history);
    dismissing or clicking one clears it.
    """

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.unread: set[int] = set()
        self._pending: dict[int, int] = {}  # call serial -> replaces_id

    def on_notify(self, serial: int, app_name: str, replaces_id: int) -> None:
        if app_name.lower() not in self.allowed:
            return
        self._pending[serial] = replaces_id

    def on_reply(self, reply_serial: int, notification_id: int) -> bool:
        """Returns True if the unread count changed."""
        replaces_id = self._pending.pop(reply_serial, None)
        if replaces_id is None:
            return False  # not one of ours
        if replaces_id and replaces_id in self.unread:
            return False  # an update of an existing notification, not a new one
        before = len(self.unread)
        self.unread.add(notification_id)
        return len(self.unread) != before

    def on_closed(self, notification_id: int, reason: int) -> bool:
        """Returns True if the unread count changed."""
        if reason == CLOSED_EXPIRED or notification_id not in self.unread:
            return False
        self.unread.remove(notification_id)
        return True

    @property
    def count(self) -> int:
        return len(self.unread)


# What the panel is currently showing, as far as we know. None = unknown
# (nothing pushed yet, or the last push was lost).
_shown: int | None = None


def push(count: int) -> bool:
    """Show `count` on the panel (silent — box audio is retired).

    Fails quietly when no daemon is listening: it may simply be reconnecting
    to the box (~15 s). The reconcile loop retries.
    """
    global _shown
    if send_to_daemon({"count": count, "silent": True}):
        _shown = count
        return True
    _shown = None
    return False


async def reconcile(tracker: "UnreadTracker", period: float = 5.0) -> None:
    """Converge the panel to the true count after a lost push.

    The daemon restarts (and reconnects to the box) independently of us, so a
    badge sent in that window is dropped. Rather than queue retries, just push
    again whenever the panel is known to be out of date.
    """
    while True:
        await asyncio.sleep(period)
        if _shown != tracker.count:
            push(tracker.count)


async def main() -> None:
    allowed = allowed_apps()
    if not allowed:
        print("TIMEBOX_ONLY_APPS is empty — nothing will be forwarded", flush=True)
    else:
        print(f"forwarding notifications from: {', '.join(sorted(allowed))}", flush=True)

    tracker = UnreadTracker(allowed)

    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    # A monitor connection may only receive, never send — so this bus does
    # nothing else afterwards (the FIFO carries our output).
    await bus.call(
        Message(
            destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus.Monitoring",
            member="BecomeMonitor",
            signature="asu",
            body=[MATCH_RULES, 0],
        )
    )

    def on_message(msg: Message) -> bool:
        changed = False
        if (msg.message_type == MessageType.METHOD_CALL
                and msg.interface == "org.freedesktop.Notifications"
                and msg.member == "Notify"):
            # Notify(app_name, replaces_id, icon, summary, body, actions, hints, timeout)
            # Only app_name and replaces_id are ever read — content stays untouched.
            app_name, replaces_id = msg.body[0], msg.body[1]
            tracker.on_notify(msg.serial, app_name, replaces_id)
        elif msg.message_type == MessageType.METHOD_RETURN and msg.body:
            changed = tracker.on_reply(msg.reply_serial, msg.body[0])
        elif (msg.message_type == MessageType.SIGNAL
              and msg.member == "NotificationClosed" and len(msg.body) >= 2):
            changed = tracker.on_closed(msg.body[0], msg.body[1])

        if changed:
            print(f"unread: {tracker.count}", flush=True)
            if not push(tracker.count):
                print("daemon not listening — will retry", flush=True)

        # Claim every message: a monitor sees method calls addressed to other
        # peers, and dbus_fast would otherwise auto-reply "unknown method" to
        # them — sending anything from a monitor gets the connection killed.
        return True

    bus.add_message_handler(on_message)
    print("monitoring KDE notifications", flush=True)
    asyncio.create_task(reconcile(tracker))
    await bus.wait_for_disconnect()
    print("session bus disconnected", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
