"""Where each e-mail of the platform goes, by the recipient's preference (M11b).

Every queue of e-mails (the five of M3, M5a, M6, M7 and the activity's own) is a
SOURCE: its model, the path to the thesis, the category of each kind
(CATEGORY_OF, the table of docs/modules/M11-activite.md), its sending task, its
send-time checks (skip_reason) and how a digest names and links one of its rows.

- dispatch(row): at queue time. Immediate: queued as before. Daily or weekly:
  DEFERRED, nothing to the broker. « Feed only » (Informations): SKIPPED.
- route_at_send(key): given to deliver_once, after its checks: the preference
  may have changed since the row was queued (DEFERRED, never lost).
- release(user, thesis, category): back to immediate, the DEFERRED rows go.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from django.db import transaction

from apps.core import delivery

from .choices import Category, Frequency

logger = logging.getLogger("apps.core.delivery")

C = Category
CATEGORY_OF: dict[str, dict[str, str]] = {
    "admin_file.reminder": {"J14": C.DEADLINE, "J3": C.DEADLINE, "OVERDUE": C.DEADLINE},
    "admin_file.notification": {
        "REOPENED": C.INFO,
        "DUE_PROPOSED": C.ACTION,
        "DUE_ACCEPTED": C.DEADLINE,
        "DUE_REFUSED": C.DEADLINE,
        "DUE_CHANGED": C.DEADLINE,
    },
    "meetings": {
        "INVITED": C.DEADLINE,
        "CHANGED": C.DEADLINE,
        "CANCELLED": C.DEADLINE,
        "MINUTES": C.ACTION,
        "REMINDER": C.DEADLINE,
        "ACTION_OVERDUE": C.DEADLINE,
    },
    "conferences": {
        "DEADLINE": C.DEADLINE,
        "REQUEST_DUE": C.DEADLINE,
        "APPROVAL_PENDING": C.DEADLINE,
        "APPROVAL_REQUESTED": C.ACTION,
        "APPROVAL_DECIDED": C.INFO,
        "POLICY_CHANGED": C.INFO,
    },
    "travel": {
        "ORDER_TO_REQUEST": C.DEADLINE,
        "RECEIPTS_MISSING": C.DEADLINE,
        "ORDER_PENDING": C.DEADLINE,
        "ORDER_REQUESTED": C.ACTION,
        "ORDER_DECIDED": C.INFO,
        "RULES_CHANGED": C.INFO,
    },
    # The activity's own e-mails carry their category (ActivityNotification.category).
    "activity": {"EVENT": C.INFO},
}


@dataclass(frozen=True)
class Source:
    key: str
    model_path: str
    thesis_path: str
    task_path: str
    checks_path: str  # the module whose skip_reason checks a row at send time

    def model(self) -> Any:
        app, name = self.model_path.split(".")
        return import_module(f"apps.{app}.models").__dict__[name]

    def kinds(self) -> list[str]:
        if self.key == "activity":
            return ["EVENT"]  # the digests are not e-mails of an element
        return [str(value) for value, _label in self.model()._meta.get_field("kind").choices]

    def task(self) -> Any:
        module, name = self.task_path.rsplit(".", 1)
        return getattr(import_module(module), name)

    def skip_reason(self, row: Any) -> str | None:
        reason: str | None = import_module(self.checks_path).skip_reason(row)
        return reason

    def thesis_id(self, row: Any) -> Any:
        obj = row
        for part in self.thesis_path.split("__"):
            obj = getattr(obj, part)
        return obj.pk

    def category(self, row: Any) -> str:
        if self.key == "activity":
            return str(row.category or C.INFO)
        return CATEGORY_OF[self.key][str(row.kind)]


SOURCES: dict[str, Source] = {
    s.key: s
    for s in (
        Source("admin_file.reminder", "admin_file.ReminderLog", "item__thesis",
               "apps.admin_file.tasks.send_reminder_email", "apps.admin_file.notifications"),
        Source("admin_file.notification", "admin_file.ItemNotification", "item__thesis",
               "apps.admin_file.tasks.send_item_notification", "apps.admin_file.notifications"),
        Source("meetings", "meetings.MeetingNotification", "meeting__thesis",
               "apps.meetings.tasks.send_meeting_notification", "apps.meetings.notifications"),
        Source("conferences", "conferences.ConferenceNotification", "thesis",
               "apps.conferences.tasks.send_conference_notification",
               "apps.conferences.notifications"),
        Source("travel", "travel.TravelNotification", "thesis",
               "apps.travel.tasks.send_travel_notification", "apps.travel.notifications"),
        Source("activity", "activity.ActivityNotification", "thesis",
               "apps.activity.tasks.send_activity_notification", "apps.activity.notifications"),
    )
}  # fmt: skip


def _decision(source: Source, row: Any) -> str | None:
    """None: send now; delivery.DEFER: wait for a digest; NOT_WANTED: never."""
    from apps.accounts.models import Thesis

    if source.key == "activity" and row.kind != "EVENT":
        return None  # a digest is never deferred

    from .preferences import frequency_of

    thesis = Thesis.objects.get(pk=source.thesis_id(row))
    frequency = frequency_of(row.recipient, thesis, source.category(row))
    if frequency in (Frequency.DAILY, Frequency.WEEKLY):
        return delivery.DEFER
    if frequency == Frequency.FEED_ONLY:
        return delivery.NOT_WANTED
    return None  # immediate (or no preference at all: as before M11b)


def dispatch(row: Any, key: str, queue: Callable[[], None]) -> None:
    """At queue time: send, defer or drop ``row`` (of SOURCES[key])."""
    source = SOURCES[key]
    decision = _decision(source, row)
    if decision is None:
        queue()
        return
    status = "DEFERRED" if decision == delivery.DEFER else "SKIPPED"
    source.model().objects.filter(pk=row.pk).update(status=status)
    row.status = status
    if status == "SKIPPED":
        logger.info("%s skipped (%s, %s)", key, row.pk, decision)


def route_at_send(key: str) -> Callable[[Any], str | None]:
    """For deliver_once(route=...): the preference read again at send time."""
    source = SOURCES[key]
    return lambda row: _decision(source, row)


def deferred(source: Source, user: Any, thesis_id: Any) -> Any:
    """The DEFERRED rows of ``user`` on a thesis in ``source`` (a queryset)."""
    return source.model().objects.filter(
        recipient_id=user.pk, status="DEFERRED", **{f"{source.thesis_path}_id": thesis_id}
    )


def _later(task: Any, row_id: str) -> None:
    transaction.on_commit(lambda: task.delay(row_id))


def release(user: Any, thesis: Any, category: str) -> int:
    """Back to immediate: the DEFERRED rows of ``category`` are queued (after commit),
    and checked again at send time."""
    released = 0
    for source in SOURCES.values():
        for row in deferred(source, user, thesis.pk).select_for_update():
            if source.category(row) != category:
                continue
            source.model().objects.filter(pk=row.pk).update(status="PENDING")
            _later(source.task(), str(row.pk))
            released += 1
    return released


def describe(source: Source, row: Any) -> tuple[str, str]:
    """(name, page) of a row in the « Résumé » page: clear metadata only, never a
    sealed text; the page its e-mail led to."""
    from django.urls import reverse

    from apps.ui.templatetags.ui import short_date

    from .labels import joined

    kind = str(row.get_kind_display())
    if source.key.startswith("admin_file."):
        from apps.admin_file.services import label_of

        url = reverse(
            "admin_file:item", kwargs={"thesis_id": row.item.thesis_id, "item_id": row.item_id}
        )
        return joined(kind, label_of(row.item)), url
    if source.key == "meetings":
        from apps.meetings.tasks import _action_anchor

        meeting = row.meeting
        url = reverse(
            "meetings:detail", kwargs={"thesis_id": meeting.thesis_id, "meeting_id": meeting.pk}
        )
        name = joined(kind, f"{meeting.get_kind_display()} {short_date(meeting.starts_at)}")
        return name, url + _action_anchor(row)
    if source.key == "conferences":
        from apps.conferences.tasks import _url

        return kind, _url(row)
    if source.key == "travel":
        from apps.travel.tasks import _url as travel_url

        mission = row.mission
        if mission is not None:
            dates = f"{short_date(mission.start_date)} \u2013 {short_date(mission.end_date)}"
            kind = joined(kind, dates, mission.get_destination_display())
        return kind, travel_url(row)
    from .labels import name_of, objects_of

    event = row.event
    obj = objects_of([event]).get(event.object_id)
    url = reverse("activity:open", kwargs={"thesis_id": event.thesis_id, "event_id": event.pk})
    return joined(event.get_kind_display(), name_of(event, obj) if obj else ""), url
