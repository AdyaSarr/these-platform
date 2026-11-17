"""Who reads an event: reads() per event, readable_events() in SQL (M11a).

reads() reuses the EXISTING rules only, on the CURRENT state of the object and of
the membership (docs/modules/M11-activite.md, « Lecteurs »):

- documents: permissions.can_access_document (role, level, explicit grant, the
  restrictions of M3 and M7);
- M3 items: the read right of the item's section (admin_file.visible_items);
- M7 missions: Mission.objects.visible_to (LIRMM_ONLY, ST_ONLY);
- meetings, submissions, objectives: the list's right AND the object's level
  (can_access_level): one is notified only of what one can read (decision of
  2026-11-17, option B; the lists themselves do not change);
- linked objects (action, approval, comment, order, link of a document) follow
  their parent, on top of their own rule; the team: VIEW_TEAM.

The actor never reads their own event; a non-member reads nothing.

readable_events() is the same rule written as one SQL query (the bell is on every
page: requirement c). It must give EXACTLY the set of reads(); a test compares the
two on every role, type of object and level, and another checks that the
document restrictions it knows are all those registered.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.db.models import Q, QuerySet

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import (
    LEVEL_ROLES,
    ROLE_ACTIONS,
    Action,
    actions_for,
    can,
    can_access_document,
    can_access_level,
    roles_of,
)
from apps.core.confidentiality import ConfidentialityLevel

from .choices import ObjectType
from .models import Event

T = ObjectType


def _models() -> dict[str, Any]:
    """{object type: model}, imported here: the business modules import this app."""
    from apps.admin_file.models import RequiredItem
    from apps.conferences.models import StApproval, Submission, SubmissionDocument
    from apps.meetings.models import ActionItem, Meeting, MeetingDocument
    from apps.objectives.models import Objective, ObjectiveComment
    from apps.travel.models import Mission, MissionOrder
    from apps.vault.models import Document, DocumentAccessGrant

    return {
        T.DOCUMENT: Document,
        T.DOCUMENT_GRANT: DocumentAccessGrant,
        T.MEETING_DOCUMENT: MeetingDocument,
        T.SUBMISSION_DOCUMENT: SubmissionDocument,
        T.ITEM: RequiredItem,
        T.OBJECTIVE: Objective,
        T.OBJECTIVE_COMMENT: ObjectiveComment,
        T.MEETING: Meeting,
        T.ACTION: ActionItem,
        T.SUBMISSION: Submission,
        T.APPROVAL: StApproval,
        T.MISSION: Mission,
        T.ORDER: MissionOrder,
        T.MEMBERSHIP: Membership,
    }


# The path from each type of object to its thesis (an object of another thesis is
# never read through this one).
THESIS_PATH: dict[str, str] = {
    T.DOCUMENT: "thesis",
    T.DOCUMENT_GRANT: "document__thesis",
    T.MEETING_DOCUMENT: "meeting__thesis",
    T.SUBMISSION_DOCUMENT: "submission__thesis",
    T.ITEM: "thesis",
    T.OBJECTIVE: "thesis",
    T.OBJECTIVE_COMMENT: "objective__thesis",
    T.MEETING: "thesis",
    T.ACTION: "meeting__thesis",
    T.SUBMISSION: "thesis",
    T.APPROVAL: "submission__thesis",
    T.MISSION: "thesis",
    T.ORDER: "mission__thesis",
    T.MEMBERSHIP: "thesis",
}


def load(event: Event) -> Any:
    """The object of ``event`` on the event's thesis, or None."""
    model = _models()[event.object_type]
    path = THESIS_PATH[event.object_type]
    return model.objects.filter(pk=event.object_id, **{f"{path}_id": event.thesis_id}).first()


# --------------------------------------------------------------------------- reads()


def _level(user: Any, thesis: Thesis, level: str) -> bool:
    return can_access_level(user, thesis, ConfidentialityLevel(level))


def _meeting(user: Any, meeting: Any) -> bool:
    return can(user, Action.MEETING_READ, meeting) and _level(user, meeting.thesis, meeting.level)


def _submission(user: Any, submission: Any) -> bool:
    return can(user, Action.SUBMISSION_READ, submission) and _level(
        user, submission.thesis, submission.level
    )


def _objective(user: Any, objective: Any, action: Action = Action.OBJECTIVE_READ) -> bool:
    return can(user, action, objective) and _level(user, objective.thesis, objective.level)


def _mission(user: Any, mission: Any) -> bool:
    from apps.travel.models import Mission

    return Mission.objects.visible_to(user, mission.thesis).filter(pk=mission.pk).exists()


def _item(user: Any, item: Any) -> bool:
    from apps.admin_file.models import SECTION_READ_ACTION, Section

    return can(user, SECTION_READ_ACTION[Section(item.section)], item)


def _grant(user: Any, grant: Any) -> bool:
    return (
        grant.user_id == user.pk
        and grant.revoked_at is None
        and can_access_document(user, grant.document)
    )


RULES: dict[str, Callable[[Any, Any], bool]] = {
    T.DOCUMENT: can_access_document,
    T.DOCUMENT_GRANT: _grant,
    T.MEETING_DOCUMENT: lambda u, o: can_access_document(u, o.document) and _meeting(u, o.meeting),
    T.SUBMISSION_DOCUMENT: lambda u, o: (
        can_access_document(u, o.document) and _submission(u, o.submission)
    ),
    T.ITEM: _item,
    T.OBJECTIVE: _objective,
    T.OBJECTIVE_COMMENT: lambda u, o: _objective(u, o.objective, Action.OBJECTIVE_COMMENT),
    T.MEETING: _meeting,
    T.ACTION: lambda u, o: _meeting(u, o.meeting),
    T.SUBMISSION: _submission,
    T.APPROVAL: lambda u, o: _submission(u, o.submission),
    T.MISSION: _mission,
    T.ORDER: lambda u, o: _mission(u, o.mission),
    T.MEMBERSHIP: lambda u, o: can(u, Action.VIEW_TEAM, o.thesis),
}


def reads(user: Any, event: Event) -> bool:
    """Whether ``user`` reads ``event`` now (never their own, never a non-member)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if event.actor_id == user.pk:
        return False
    if not roles_of(user, event.thesis):
        return False
    obj = load(event)
    return obj is not None and RULES[event.object_type](user, obj)


# --------------------------------------------------------------------------- SQL


def _readable_documents(user: Any, thesis: Thesis, roles: frozenset[Any]) -> QuerySet[Any]:
    """permissions.can_access_document in SQL: the M2 rule, then the restrictions
    of M3 (attachments) and M7 (orders and receipts of a mission)."""
    from apps.admin_file.models import SECTION_READ_ACTION, RequiredItem, Section
    from apps.travel.models import ExpenseReceipt, Mission
    from apps.vault.models import Document

    by_role = {
        level
        for level in ConfidentialityLevel
        if any(Action.DOCUMENT_READ in ROLE_ACTIONS[r] and r in LEVEL_ROLES[level] for r in roles)
    }
    granted = Q(
        confidentiality=ConfidentialityLevel.CONFIDENTIAL,
        grants__user_id=user.pk,
        grants__revoked_at__isnull=True,
    )
    docs = Document.objects.filter(thesis=thesis).filter(Q(confidentiality__in=by_role) | granted)
    actions = actions_for(roles)
    sections = [s for s in Section if SECTION_READ_ACTION[s] in actions]
    closed_items = RequiredItem.objects.filter(document__isnull=False).exclude(section__in=sections)
    docs = docs.exclude(pk__in=closed_items.values("document_id"))
    levels = [level for level in ConfidentialityLevel if roles & LEVEL_ROLES[level]]
    open_missions = Mission.objects.visible_to(user, thesis).filter(level__in=levels)
    closed_orders = Mission.objects.filter(official_order_document__isnull=False).exclude(
        pk__in=open_missions.values("pk")
    )
    closed_receipts = ExpenseReceipt.objects.exclude(
        expense__mission__in=open_missions.values("pk")
    )
    docs = docs.exclude(pk__in=closed_orders.values("official_order_document_id"))
    docs = docs.exclude(pk__in=closed_receipts.values("document_id"))
    return docs.values("pk").distinct()


def readable_events(user: Any, thesis: Thesis) -> QuerySet[Event]:
    """The events of ``thesis`` that ``user`` reads: exactly {e | reads(user, e)}."""
    from apps.admin_file.models import SECTION_READ_ACTION, RequiredItem, Section
    from apps.conferences.models import StApproval, Submission, SubmissionDocument
    from apps.meetings.models import ActionItem, Meeting, MeetingDocument
    from apps.objectives.models import Objective, ObjectiveComment
    from apps.travel.models import Mission, MissionOrder
    from apps.vault.models import DocumentAccessGrant

    none = Event.objects.none()
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return none
    roles = roles_of(user, thesis)
    if not roles:
        return none
    actions = actions_for(roles)
    levels = [level for level in ConfidentialityLevel if roles & LEVEL_ROLES[level]]
    docs = _readable_documents(user, thesis, roles)

    def ids(queryset: QuerySet[Any]) -> Any:
        return queryset.values("pk")

    meetings = Meeting.objects.filter(thesis=thesis, level__in=levels)
    if Action.MEETING_READ not in actions:
        meetings = meetings.none()
    submissions = Submission.objects.filter(thesis=thesis, level__in=levels)
    if Action.SUBMISSION_READ not in actions:
        submissions = submissions.none()
    objectives = Objective.objects.filter(thesis=thesis, level__in=levels)
    commented = objectives if Action.OBJECTIVE_COMMENT in actions else objectives.none()
    if Action.OBJECTIVE_READ not in actions:
        objectives = objectives.none()
    sections = [s for s in Section if SECTION_READ_ACTION[s] in actions]
    missions = Mission.objects.visible_to(user, thesis)
    team = Membership.objects.filter(thesis=thesis)
    if Action.VIEW_TEAM not in actions:
        team = team.none()
    grants = DocumentAccessGrant.objects.filter(
        user_id=user.pk, revoked_at__isnull=True, document__in=docs
    )
    rules = {
        T.DOCUMENT: docs,
        T.DOCUMENT_GRANT: ids(grants),
        T.MEETING_DOCUMENT: ids(
            MeetingDocument.objects.filter(document__in=docs, meeting__in=meetings)
        ),
        T.SUBMISSION_DOCUMENT: ids(
            SubmissionDocument.objects.filter(document__in=docs, submission__in=submissions)
        ),
        T.ITEM: ids(RequiredItem.objects.filter(thesis=thesis, section__in=sections)),
        T.OBJECTIVE: ids(objectives),
        T.OBJECTIVE_COMMENT: ids(ObjectiveComment.objects.filter(objective__in=commented)),
        T.MEETING: ids(meetings),
        T.ACTION: ids(ActionItem.objects.filter(meeting__in=meetings)),
        T.SUBMISSION: ids(submissions),
        T.APPROVAL: ids(StApproval.objects.filter(submission__in=submissions)),
        T.MISSION: ids(missions),
        T.ORDER: ids(MissionOrder.objects.filter(mission__in=missions)),
        T.MEMBERSHIP: ids(team),
    }
    condition = Q()
    for object_type, readable in rules.items():
        condition |= Q(object_type=object_type, object_id__in=readable)
    return Event.objects.filter(thesis=thesis).exclude(actor_id=user.pk).filter(condition)
