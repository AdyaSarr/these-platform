"""Closed lists of the activity app (M11a, docs/modules/M11-activite.md)."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


class EventKind(models.TextChoices):
    DOCUMENT_ADDED = "DOCUMENT_ADDED", _("New document")
    DOCUMENT_VERSION_ADDED = "DOCUMENT_VERSION_ADDED", _("New version of a document")
    DOCUMENT_GRANTED = "DOCUMENT_GRANTED", _("Document shared with you")
    DOCUMENT_LINKED = "DOCUMENT_LINKED", _("Document linked")
    ITEM_SUBMITTED = "ITEM_SUBMITTED", _("Piece submitted")
    ITEM_VALIDATED = "ITEM_VALIDATED", _("Piece validated")
    ITEM_REJECTED = "ITEM_REJECTED", _("Piece refused")
    ITEM_REOPENED = "ITEM_REOPENED", _("Piece reopened")
    OBJECTIVE_CREATED = "OBJECTIVE_CREATED", _("New objective")
    OBJECTIVE_CHANGED = "OBJECTIVE_CHANGED", _("Objective changed")
    PROGRESS_REPORTED = "PROGRESS_REPORTED", _("Progress reported")
    OBJECTIVE_COMMENTED = "OBJECTIVE_COMMENTED", _("Comment on an objective")
    MEETING_PLANNED = "MEETING_PLANNED", _("Meeting planned")
    MEETING_CHANGED = "MEETING_CHANGED", _("Meeting changed")
    MEETING_CANCELLED = "MEETING_CANCELLED", _("Meeting cancelled")
    MINUTES_PUBLISHED = "MINUTES_PUBLISHED", _("Minutes published")
    ACTION_ASSIGNED = "ACTION_ASSIGNED", _("Action assigned")
    SUBMISSION_CREATED = "SUBMISSION_CREATED", _("New submission")
    SUBMISSION_STATUS_CHANGED = "SUBMISSION_STATUS_CHANGED", _("Submission status changed")
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED", _("ST approval requested")
    APPROVAL_DECIDED = "APPROVAL_DECIDED", _("ST approval decided")
    MISSION_PLANNED = "MISSION_PLANNED", _("Mission planned")
    MISSION_CHANGED = "MISSION_CHANGED", _("Mission changed")
    MISSION_CANCELLED = "MISSION_CANCELLED", _("Mission cancelled")
    ORDER_REQUESTED = "ORDER_REQUESTED", _("Mission approval requested")
    ORDER_DECIDED = "ORDER_DECIDED", _("Mission approval decided")
    MEMBER_JOINED = "MEMBER_JOINED", _("New team member")
    MEMBER_ROLE_CHANGED = "MEMBER_ROLE_CHANGED", _("Role changed")
    MEMBER_LEFT = "MEMBER_LEFT", _("Member left")
    OBJECTIVE_OVERDUE = "OBJECTIVE_OVERDUE", _("Objective overdue")
    CALENDAR_FEED_EXPIRING = "CALENDAR_FEED_EXPIRING", _("Calendar subscription expiring")


class ObjectType(models.TextChoices):
    DOCUMENT = "DOCUMENT", "DOCUMENT"
    DOCUMENT_GRANT = "DOCUMENT_GRANT", "DOCUMENT_GRANT"
    MEETING_DOCUMENT = "MEETING_DOCUMENT", "MEETING_DOCUMENT"
    SUBMISSION_DOCUMENT = "SUBMISSION_DOCUMENT", "SUBMISSION_DOCUMENT"
    ITEM = "ITEM", "ITEM"
    OBJECTIVE = "OBJECTIVE", "OBJECTIVE"
    OBJECTIVE_COMMENT = "OBJECTIVE_COMMENT", "OBJECTIVE_COMMENT"
    MEETING = "MEETING", "MEETING"
    ACTION = "ACTION", "ACTION"
    SUBMISSION = "SUBMISSION", "SUBMISSION"
    APPROVAL = "APPROVAL", "APPROVAL"
    MISSION = "MISSION", "MISSION"
    ORDER = "ORDER", "ORDER"
    MEMBERSHIP = "MEMBERSHIP", "MEMBERSHIP"
    FEED = "FEED", "FEED"  # M12: one's own iCal feed


# The module each type belongs to: the filter of the Activity page.
MODULE_OF: dict[str, str] = {
    ObjectType.DOCUMENT: "documents",
    ObjectType.DOCUMENT_GRANT: "documents",
    ObjectType.MEETING_DOCUMENT: "documents",
    ObjectType.SUBMISSION_DOCUMENT: "documents",
    ObjectType.ITEM: "admin_file",
    ObjectType.OBJECTIVE: "objectives",
    ObjectType.OBJECTIVE_COMMENT: "objectives",
    ObjectType.MEETING: "meetings",
    ObjectType.ACTION: "meetings",
    ObjectType.SUBMISSION: "conferences",
    ObjectType.APPROVAL: "conferences",
    ObjectType.MISSION: "travel",
    ObjectType.ORDER: "travel",
    ObjectType.MEMBERSHIP: "team",
    ObjectType.FEED: "team",
}
MODULES = ("documents", "admin_file", "objectives", "meetings", "conferences", "travel", "team")


class Category(models.TextChoices):
    INFO = "INFO", _("Information")
    ACTION = "ACTION", _("Asks you to act")
    DEADLINE = "DEADLINE", _("Deadline reminders")


class Frequency(models.TextChoices):
    IMMEDIATE = "IMMEDIATE", _("At once, one e-mail per element")
    DAILY = "DAILY", _("Daily digest")
    WEEKLY = "WEEKLY", _("Weekly digest (Monday)")
    FEED_ONLY = "FEED_ONLY", _("No e-mail: the Activity page only")


class NotificationKind(models.TextChoices):
    EVENT = "EVENT", _("One element")
    DIGEST_DAILY = "DIGEST_DAILY", _("Daily digest")
    DIGEST_WEEKLY = "DIGEST_WEEKLY", _("Weekly digest")


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    SENT = "SENT", _("Sent")
    FAILED = "FAILED", _("Failed")
    SKIPPED = "SKIPPED", _("Not sent: no longer concerned or due")
    DEFERRED = "DEFERRED", _("Waiting for a digest")
    DIGESTED = "DIGESTED", _("Sent in a digest")


K = EventKind
# What each kind e-mails in M11a (docs/modules/M11-activite.md, « Courriel M11a »):
# INFO follows the person's preference; ACTION is immediate, to the recipients the
# notifications module names; NONE: an existing e-mail already covers it.
EMAIL_OF: dict[str, str] = {
    K.DOCUMENT_ADDED: "INFO",
    K.DOCUMENT_VERSION_ADDED: "INFO",
    K.DOCUMENT_GRANTED: "INFO",
    K.DOCUMENT_LINKED: "INFO",
    K.ITEM_SUBMITTED: "ACTION",
    K.ITEM_VALIDATED: "INFO",
    K.ITEM_REJECTED: "ACTION",
    K.ITEM_REOPENED: "NONE",
    K.OBJECTIVE_CREATED: "INFO",
    K.OBJECTIVE_CHANGED: "INFO",
    K.PROGRESS_REPORTED: "INFO",
    K.OBJECTIVE_COMMENTED: "INFO",
    K.MEETING_PLANNED: "NONE",
    K.MEETING_CHANGED: "NONE",
    K.MEETING_CANCELLED: "NONE",
    K.MINUTES_PUBLISHED: "NONE",
    K.ACTION_ASSIGNED: "ACTION",
    K.SUBMISSION_CREATED: "INFO",
    K.SUBMISSION_STATUS_CHANGED: "INFO",
    K.APPROVAL_REQUESTED: "NONE",
    K.APPROVAL_DECIDED: "NONE",
    K.MISSION_PLANNED: "INFO",
    K.MISSION_CHANGED: "INFO",
    K.MISSION_CANCELLED: "INFO",
    K.ORDER_REQUESTED: "NONE",
    K.ORDER_DECIDED: "NONE",
    K.MEMBER_JOINED: "INFO",
    K.MEMBER_ROLE_CHANGED: "INFO",
    K.MEMBER_LEFT: "INFO",
    K.OBJECTIVE_OVERDUE: "INFO",  # Échéances for the student (notifications.category_for)
    K.CALENDAR_FEED_EXPIRING: "INFO",  # Échéances for its owner (notifications.category_for)
}


def allowed_frequencies(category: str) -> frozenset[str]:
    """M11b: Informations, every frequency; À faire, never « feed only »;
    Échéances, immediate or daily only. Also a database constraint."""
    if category == Category.INFO:
        return frozenset(Frequency.values)
    if category == Category.ACTION:
        return frozenset({Frequency.IMMEDIATE, Frequency.DAILY, Frequency.WEEKLY})
    return frozenset({Frequency.IMMEDIATE, Frequency.DAILY})
