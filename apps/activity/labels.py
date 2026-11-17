"""What each element of the feed is called, and where it leads (M11a).

Clear metadata only: a document's title, a piece's label, a submission's venue and
year, a mission's dates and destination, a meeting's type and date, an objective's
category and due date, a member's name. NEVER a sealed text (no SEALED_TEXT_READ,
neither in the bell nor in the Activity page): a meeting, an action or an
objective is named by its type and date, not by its title.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from django.urls import reverse
from django.utils.translation import gettext as _

from apps.ui.templatetags.ui import short_date

from .choices import ObjectType
from .models import Event
from .readers import THESIS_PATH, _models

T = ObjectType

# Relations fetched with each type, so that naming costs one query per type.
_RELATED: dict[str, tuple[str, ...]] = {
    T.DOCUMENT_GRANT: ("document",),
    T.MEETING_DOCUMENT: ("document", "meeting"),
    T.SUBMISSION_DOCUMENT: ("document", "submission__venue", "submission__edition"),
    T.OBJECTIVE_COMMENT: ("objective",),
    T.ACTION: ("meeting",),
    T.SUBMISSION: ("venue", "edition"),
    T.APPROVAL: ("submission__venue", "submission__edition"),
    T.ORDER: ("mission",),
    T.MEMBERSHIP: ("user",),
}


SEPARATOR = " · "


def joined(*parts: Any) -> str:
    """The parts that say something, separated: never a separator followed (or
    preceded) by nothing."""
    return SEPARATOR.join(str(p) for p in parts if p is not None and str(p).strip())


def objects_of(events: Iterable[Event]) -> dict[Any, Any]:
    """{object id: object} of ``events``, one query per type of object."""
    by_type: dict[str, set[Any]] = defaultdict(set)
    thesis_of: dict[str, Any] = {}
    for event in events:
        by_type[event.object_type].add(event.object_id)
        thesis_of[event.object_type] = event.thesis_id
    found: dict[Any, Any] = {}
    for object_type, ids in by_type.items():
        model = _models()[object_type]
        path = THESIS_PATH[object_type]
        query = model.objects.filter(pk__in=ids, **{f"{path}_id": thesis_of[object_type]})
        if object_type in _RELATED:
            query = query.select_related(*_RELATED[object_type])
        found.update({o.pk: o for o in query})
    return found


def _submission(submission: Any) -> str:
    edition = submission.edition
    return f"{submission.venue.acronym} {edition.year}" if edition else submission.venue.acronym


def _meeting(meeting: Any) -> str:
    return f"{meeting.get_kind_display()} · {short_date(meeting.starts_at)}"


def _mission(mission: Any) -> str:
    return (
        f"{short_date(mission.start_date)} \u2013 {short_date(mission.end_date)} · "
        f"{mission.get_destination_display()}"
    )


def _objective(objective: Any) -> str:
    return _("%(category)s objective · due %(date)s") % {
        "category": objective.get_category_display(),
        "date": short_date(objective.due_date),
    }


def name_of(event: Event, obj: Any) -> str:
    """The clear name of the element (never a sealed text)."""
    from apps.admin_file.services import label_of

    t = event.object_type
    if t == T.DOCUMENT:
        return str(obj.title)
    if t == T.DOCUMENT_GRANT:
        return str(obj.document.title)
    if t == T.MEETING_DOCUMENT:
        return joined(obj.document.title, _meeting(obj.meeting))
    if t == T.SUBMISSION_DOCUMENT:
        return joined(obj.document.title, _submission(obj.submission))
    if t == T.ITEM:
        return label_of(obj)
    if t == T.OBJECTIVE:
        return _objective(obj)
    if t == T.OBJECTIVE_COMMENT:
        return _objective(obj.objective)
    if t == T.MEETING:
        return _meeting(obj)
    if t == T.ACTION:
        return _("Action due %(date)s · %(meeting)s") % {
            "date": short_date(obj.due_date),
            "meeting": _meeting(obj.meeting),
        }
    if t in (T.SUBMISSION, T.APPROVAL):
        return _submission(obj if t == T.SUBMISSION else obj.submission)
    if t == T.MISSION:
        return _mission(obj)
    if t == T.ORDER:
        return _mission(obj.mission)
    if t == T.MEMBERSHIP:
        return joined(obj.user.get_full_name() or _("A member"), obj.get_role_display())
    raise ValueError(t)


def target_of(event: Event, obj: Any) -> str:
    """The page of the element (where opening it leads)."""
    kw = {"thesis_id": event.thesis_id}
    t = event.object_type
    if t == T.DOCUMENT:
        return reverse("vault:document", kwargs={**kw, "document_id": obj.pk})
    if t in (T.DOCUMENT_GRANT, T.MEETING_DOCUMENT, T.SUBMISSION_DOCUMENT):
        return reverse("vault:document", kwargs={**kw, "document_id": obj.document_id})
    if t == T.ITEM:
        return reverse("admin_file:item", kwargs={**kw, "item_id": obj.pk})
    if t == T.OBJECTIVE:
        return reverse("objectives:detail", kwargs={**kw, "objective_id": obj.pk})
    if t == T.OBJECTIVE_COMMENT:
        return reverse("objectives:detail", kwargs={**kw, "objective_id": obj.objective_id})
    if t == T.MEETING:
        return reverse("meetings:detail", kwargs={**kw, "meeting_id": obj.pk})
    if t == T.ACTION:
        detail = reverse("meetings:detail", kwargs={**kw, "meeting_id": obj.meeting_id})
        return f"{detail}#action-{obj.pk}"
    if t == T.SUBMISSION:
        return reverse("conferences:submission", kwargs={**kw, "submission_id": obj.pk})
    if t == T.APPROVAL:
        return reverse("conferences:submission", kwargs={**kw, "submission_id": obj.submission_id})
    if t == T.MISSION:
        return reverse("travel:mission", kwargs={**kw, "mission_id": obj.pk})
    if t == T.ORDER:
        return reverse("travel:mission", kwargs={**kw, "mission_id": obj.mission_id})
    if t == T.MEMBERSHIP:
        return reverse("accounts:team", kwargs=kw)
    raise ValueError(t)
