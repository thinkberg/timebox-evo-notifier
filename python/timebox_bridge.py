#!/usr/bin/env python3
"""Mirror KDE's notification bell onto the TimeBox as an unread count badge.

Eavesdrops the session bus (BecomeMonitor) instead of replacing KDE's
notification daemon: desktop notifications keep working exactly as before,
and allow-listed ones additionally bump a count badge on the panel.

Only the COUNT (and which badge icon to wear: an octocat head while gitify
is the only unread source, the envelope otherwise) is sent to the box —
never notification text. The box's BLE link is unencrypted, so anything
displayed is readable by a sniffer in radio range; a bare number leaks
nothing. Notification content is never logged either. One narrow exception
to the never-read-content rule: a gitify batch notification (title
"Gitify", body "You have N notifications") has its N extracted so the
badge shows gitify's real count — only that integer is kept.

Config (~/.config/timebox/env):
    TIMEBOX_ONLY_APPS=Thunderbird,Nextcloud   # nothing is forwarded if empty

Run:  TIMEBOX_ONLY_APPS=... python timebox_bridge.py
"""

import asyncio
import os
import re
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


def _batch_count(summary: str, body: str) -> int:
    """How many notifications this one desktop notification stands for.

    Gitify batches new notifications into a single popup titled "Gitify"
    (its brand constant, locale-invariant) whose body carries the count.
    Any other title means a single notification whose body is an arbitrary
    subject title — its digits ("Fix #123") must not be read as a count.
    """
    if summary != "Gitify":
        return 1
    m = re.search(r"\d+", body)
    return min(99, max(1, int(m.group()))) if m else 1


class UnreadTracker:
    """Which allow-listed notifications are still unread.

    A Notify call only yields its notification id in the method return, so
    pending serials are matched to their reply. Notifications that merely
    expire on screen stay unread (KDE keeps them in the bell's history);
    dismissing or clicking one clears it.
    """

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        # notification id -> (app lowercased, how many it stands for)
        self.unread: dict[int, tuple[str, int]] = {}
        # serial -> (app, replaces_id, weight)
        self._pending: dict[int, tuple[str, int, int]] = {}

    def on_notify(self, serial: int, app_name: str, replaces_id: int,
                  summary: str, body: str) -> None:
        app = app_name.lower()
        if app not in self.allowed:
            return
        self._pending[serial] = (app, replaces_id, _batch_count(summary, body))

    def on_reply(self, reply_serial: int, notification_id: int) -> bool:
        """Returns True if the unread count changed."""
        pending = self._pending.pop(reply_serial, None)
        if pending is None:
            return False  # not one of ours
        app, replaces_id, weight = pending
        if replaces_id and replaces_id in self.unread:
            # An update of an existing notification: refresh its weight.
            before = self.unread[replaces_id]
            self.unread[replaces_id] = (app, weight)
            return self.unread[replaces_id] != before
        before_count = self.count
        self.unread[notification_id] = (app, weight)
        return self.count != before_count

    def on_closed(self, notification_id: int, reason: int) -> bool:
        """Returns True if the unread count changed."""
        if reason == CLOSED_EXPIRED or notification_id not in self.unread:
            return False
        del self.unread[notification_id]
        return True

    @property
    def count(self) -> int:
        return sum(weight for _, weight in self.unread.values())

    @property
    def icon(self) -> str:
        """The badge icon: octocat when gitify is the only unread source,
        the envelope whenever any other app has unread (envelope wins)."""
        # ponytail: hardcoded app→icon; grow a map when a third app wants one
        return ("github" if self.unread
                and {app for app, _ in self.unread.values()} == {"gitify"}
                else "envelope")


# What the panel is currently showing, as far as we know, as (count, icon).
# None = unknown (nothing pushed yet, or the last push was lost).
_shown: tuple[int, str] | None = None


def push(count: int, icon: str) -> bool:
    """Show `count` on the panel (silent — box audio is retired).

    Fails quietly when no daemon is listening: it may simply be reconnecting
    to the box (~15 s). The reconcile loop retries.
    """
    global _shown
    if send_to_daemon({"count": count, "icon": icon, "silent": True}):
        _shown = (count, icon)
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
        if _shown != (tracker.count, tracker.icon):
            push(tracker.count, tracker.icon)


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
            # Content is only read to extract gitify's batch count (see
            # _batch_count); nothing of it is stored, forwarded, or logged.
            tracker.on_notify(msg.serial, msg.body[0], msg.body[1],
                              msg.body[3], msg.body[4])
        elif msg.message_type == MessageType.METHOD_RETURN and msg.body:
            changed = tracker.on_reply(msg.reply_serial, msg.body[0])
        elif (msg.message_type == MessageType.SIGNAL
              and msg.member == "NotificationClosed" and len(msg.body) >= 2):
            changed = tracker.on_closed(msg.body[0], msg.body[1])

        if changed:
            print(f"unread: {tracker.count} ({tracker.icon})", flush=True)
            if not push(tracker.count, tracker.icon):
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
