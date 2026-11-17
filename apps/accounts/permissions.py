"""Thesis-level access control: the only place where rights are decided (CLAUDE.md rule 4).

Deny by default: an unknown action, an unknown level, an object not attached to a
saved thesis, an anonymous or deactivated user, or a user with no active
membership is refused. ``is_superuser`` / ``is_staff`` grant nothing here.

Rights are read from the database on every call (no session cache), so removing
a member takes effect on their very next request.

The two tables below transcribe docs/ARCHITECTURE.md; tests/test_permissions.py
holds an independent second transcription and compares them cell by cell.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from functools import wraps
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, Protocol

from django.apps import apps as django_apps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponseBase

from apps.core.confidentiality import ConfidentialityLevel

from .models import Membership, Thesis
from .roles import Role

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from apps.vault.models import Document

    AnyUser = AbstractBaseUser | AnonymousUser


class Action(StrEnum):
    # Thesis and team (M01).
    VIEW_THESIS = "VIEW_THESIS"
    VIEW_TEAM = "VIEW_TEAM"
    INVITE_MEMBER = "INVITE_MEMBER"
    CHANGE_ROLE = "CHANGE_ROLE"
    REMOVE_MEMBER = "REMOVE_MEMBER"
    EXTEND_MEMBERSHIP = "EXTEND_MEMBERSHIP"
    # Objectives (M4). OBJECTIVE_READ covers the objectives themselves, NOT their
    # comment thread: comments are internal to the supervision and are shown only
    # to roles holding OBJECTIVE_COMMENT (so a CSI member reads objectives
    # without the comments, M04-objectifs.md).
    OBJECTIVE_READ = "OBJECTIVE_READ"
    OBJECTIVE_WRITE = "OBJECTIVE_WRITE"
    OBJECTIVE_COMMENT = "OBJECTIVE_COMMENT"
    # Meetings (M5).
    MEETING_READ = "MEETING_READ"
    MEETING_WRITE = "MEETING_WRITE"
    MEETING_ACKNOWLEDGE = "MEETING_ACKNOWLEDGE"  # read / approve minutes (attendees only)
    # Conferences and submissions (M6). ST approval decisions: VALIDATE_PUBLICATION.
    CONFERENCE_READ = "CONFERENCE_READ"
    CONFERENCE_WRITE = "CONFERENCE_WRITE"
    SUBMISSION_READ = "SUBMISSION_READ"
    SUBMISSION_WRITE = "SUBMISSION_WRITE"
    # Travel (M7). TRAVEL_READ: what each role sees is filtered by travel's
    # visible_to (the co-supervisor: LIRMM only). The payer's person decides an order
    # and sets its budget (tutor: ST, director: LIRMM), checked by travel.services.
    TRAVEL_READ = "TRAVEL_READ"
    TRAVEL_WRITE = "TRAVEL_WRITE"
    TRAVEL_REMINDERS_SET = "TRAVEL_REMINDERS_SET"
    TRAVEL_RULES_SET = "TRAVEL_RULES_SET"
    TRAVEL_BUDGET_SET = "TRAVEL_BUDGET_SET"
    # Documents (M2); also subject to can_access_level().
    DOCUMENT_READ = "DOCUMENT_READ"
    DOCUMENT_WRITE = "DOCUMENT_WRITE"
    DOCUMENT_COMMENT = "DOCUMENT_COMMENT"
    DOCUMENT_GRANT = "DOCUMENT_GRANT"  # explicit access to one CONFIDENTIAL document
    # Administrative file (M3).
    ADMIN_FILE_READ = "ADMIN_FILE_READ"
    ADMIN_FILE_WRITE = "ADMIN_FILE_WRITE"
    CIFRE_FILE_READ = "CIFRE_FILE_READ"
    # CSI (M8).
    CSI_FILE_READ = "CSI_FILE_READ"
    CSI_REPORT_READ = "CSI_REPORT_READ"
    CSI_OPINION_WRITE = "CSI_OPINION_WRITE"
    # Validations ("Valide" column).
    VALIDATE_OBJECTIVES = "VALIDATE_OBJECTIVES"
    VALIDATE_CSI = "VALIDATE_CSI"
    VALIDATE_ANRT = "VALIDATE_ANRT"
    VALIDATE_PUBLICATION = "VALIDATE_PUBLICATION"
    VALIDATE_MISSION_ORDER = "VALIDATE_MISSION_ORDER"  # M7


_A = Action

# Laid out like the ARCHITECTURE.md table: the comment names the cell.
# fmt: off
ROLE_ACTIONS: Mapping[Role, frozenset[Action]] = MappingProxyType(
    {
        Role.PHD_STUDENT: frozenset(
            {
                _A.VIEW_THESIS, _A.VIEW_TEAM,
                _A.INVITE_MEMBER, _A.CHANGE_ROLE, _A.REMOVE_MEMBER, _A.EXTEND_MEMBERSHIP,
                _A.OBJECTIVE_READ, _A.OBJECTIVE_WRITE, _A.OBJECTIVE_COMMENT,  # écrire
                _A.MEETING_READ, _A.MEETING_WRITE, _A.MEETING_ACKNOWLEDGE,  # écrire
                _A.CONFERENCE_READ, _A.CONFERENCE_WRITE,                # M6
                _A.SUBMISSION_READ, _A.SUBMISSION_WRITE,                # M6
                _A.TRAVEL_READ, _A.TRAVEL_WRITE, _A.TRAVEL_REMINDERS_SET,  # M7
                _A.DOCUMENT_READ, _A.DOCUMENT_WRITE, _A.DOCUMENT_COMMENT,  # écrire
                _A.DOCUMENT_GRANT,                                      # + liste explicite
                _A.ADMIN_FILE_READ, _A.ADMIN_FILE_WRITE, _A.CIFRE_FILE_READ,  # écrire
            }
        ),
        Role.DIRECTOR: frozenset(
            {
                _A.VIEW_THESIS, _A.VIEW_TEAM,
                _A.OBJECTIVE_READ, _A.OBJECTIVE_COMMENT,                # commenter
                _A.MEETING_READ, _A.MEETING_WRITE, _A.MEETING_ACKNOWLEDGE,  # écrire
                _A.CONFERENCE_READ, _A.CONFERENCE_WRITE, _A.SUBMISSION_READ,  # M6
                _A.TRAVEL_READ, _A.TRAVEL_RULES_SET, _A.TRAVEL_BUDGET_SET,  # M7
                _A.VALIDATE_MISSION_ORDER,                              # M7: payer LIRMM
                _A.DOCUMENT_READ, _A.DOCUMENT_COMMENT,                  # lire, commenter
                _A.ADMIN_FILE_READ, _A.CIFRE_FILE_READ,                 # lire
                _A.VALIDATE_OBJECTIVES, _A.VALIDATE_CSI, _A.VALIDATE_ANRT,  # objectifs, CSI, ANRT
            }
        ),
        Role.CO_SUPERVISOR: frozenset(
            {
                _A.VIEW_THESIS, _A.VIEW_TEAM,
                _A.OBJECTIVE_READ, _A.OBJECTIVE_COMMENT,                # commenter
                _A.MEETING_READ, _A.MEETING_WRITE, _A.MEETING_ACKNOWLEDGE,  # écrire
                _A.CONFERENCE_READ, _A.CONFERENCE_WRITE, _A.SUBMISSION_READ,  # M6
                _A.TRAVEL_READ,                                         # M7: LIRMM only
                _A.DOCUMENT_READ, _A.DOCUMENT_COMMENT,                  # lire, commenter
            }                                                       # admin : —
        ),
        Role.INDUSTRIAL_TUTOR: frozenset(
            {
                _A.VIEW_THESIS, _A.VIEW_TEAM,
                _A.OBJECTIVE_READ, _A.OBJECTIVE_COMMENT,                # commenter
                _A.MEETING_READ, _A.MEETING_WRITE, _A.MEETING_ACKNOWLEDGE,  # écrire
                _A.CONFERENCE_READ, _A.CONFERENCE_WRITE, _A.SUBMISSION_READ,  # M6
                _A.TRAVEL_READ, _A.TRAVEL_RULES_SET, _A.TRAVEL_BUDGET_SET,  # M7
                _A.VALIDATE_MISSION_ORDER,                              # M7: payer ST
                _A.DOCUMENT_READ, _A.DOCUMENT_COMMENT,                  # lire, commenter
                _A.CIFRE_FILE_READ,                                     # partie CIFRE
                _A.VALIDATE_ANRT, _A.VALIDATE_PUBLICATION,              # ANRT, publications
            }
        ),
        # Same row as the LIRMM co-supervisor, on the company side: travel filtered
        # to Rousset (ST_ONLY), levels PUBLIC and INTERNAL_ST (LEVEL_ROLES below).
        Role.INDUSTRIAL_CO_SUPERVISOR: frozenset(
            {
                _A.VIEW_THESIS, _A.VIEW_TEAM,
                _A.OBJECTIVE_READ, _A.OBJECTIVE_COMMENT,                # commenter
                _A.MEETING_READ, _A.MEETING_WRITE, _A.MEETING_ACKNOWLEDGE,  # écrire
                _A.CONFERENCE_READ, _A.CONFERENCE_WRITE, _A.SUBMISSION_READ,  # M6
                _A.TRAVEL_READ,                                         # M7: Rousset only
                _A.DOCUMENT_READ, _A.DOCUMENT_COMMENT,                  # lire, commenter
            }                                                       # admin : —
        ),
        Role.CSI_MEMBER: frozenset(
            {
                _A.VIEW_THESIS,                 # no VIEW_TEAM: outside the supervision
                _A.OBJECTIVE_READ,              # read-only, per M04-objectifs.md
                _A.CSI_FILE_READ,                                       # dossier CSI
                _A.CSI_REPORT_READ,                                     # rapport CSI
                _A.CSI_OPINION_WRITE,                                   # avis CSI
            }
        ),
        # "une réunion", "projet partagé": per-object grants (M5, M2), nothing here.
        Role.GUEST: frozenset(),
    }
)
# fmt: on

_L = ConfidentialityLevel

LEVEL_ROLES: Mapping[ConfidentialityLevel, frozenset[Role]] = MappingProxyType(
    {
        _L.PUBLIC: frozenset(Role),  # tous les membres actifs
        _L.INTERNAL_LIRMM: frozenset({Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR}),
        _L.INTERNAL_ST: frozenset(
            {Role.PHD_STUDENT, Role.INDUSTRIAL_TUTOR, Role.INDUSTRIAL_CO_SUPERVISOR}
        ),
        # "+ liste explicite": per-document grants, added in M2.
        _L.CONFIDENTIAL: frozenset({Role.PHD_STUDENT, Role.DIRECTOR, Role.INDUSTRIAL_TUTOR}),
    }
)


class ThesisOwned(Protocol):
    @property
    def thesis(self) -> Thesis: ...


# --------------------------------------------------------------------------- checks


def roles_of(user: AnyUser, thesis: Thesis) -> frozenset[Role]:
    """Roles ``user`` holds on ``thesis`` right now (read from the database)."""
    if not user.is_authenticated or not user.is_active or thesis.pk is None:
        return frozenset()
    rows = (
        Membership.objects.active()
        .filter(user_id=user.pk, thesis_id=thesis.pk)
        .values_list("role", flat=True)
    )
    return frozenset(Role(r) for r in rows)


def _thesis_of(obj: object) -> Thesis | None:
    thesis = obj if isinstance(obj, Thesis) else getattr(obj, "thesis", None)
    if isinstance(thesis, Thesis) and thesis.pk is not None and not thesis._state.adding:
        return thesis
    return None


def actions_for(roles: Iterable[Role]) -> frozenset[Action]:
    """The rights of a set of roles: the union of their rows (no database)."""
    return frozenset().union(*(ROLE_ACTIONS[role] for role in roles))


def can(user: AnyUser, action: Action, obj: Thesis | ThesisOwned) -> bool:
    """Whether ``user`` may perform ``action`` on ``obj`` (a thesis or an object of one)."""
    if not isinstance(action, Action):
        return False
    thesis = _thesis_of(obj)
    if thesis is None:
        return False
    return action in actions_for(roles_of(user, thesis))


def allowed_actions(user: AnyUser, thesis: Thesis) -> frozenset[Action]:
    """Every action ``user`` may perform on ``thesis``: one query for a whole page."""
    if _thesis_of(thesis) is None:
        return frozenset()
    return actions_for(roles_of(user, thesis))


def can_access_level(user: AnyUser, thesis: Thesis, level: ConfidentialityLevel) -> bool:
    """Level half of document access; M2 combines it with ``can(DOCUMENT_READ)``."""
    if not isinstance(level, ConfidentialityLevel):
        return False
    thesis_or_none = _thesis_of(thesis)
    if thesis_or_none is None:
        return False
    return bool(roles_of(user, thesis_or_none) & LEVEL_ROLES[level])


def _m2_document_rule(user: AnyUser, document: Document) -> bool:
    """The M2 rule (docs/ARCHITECTURE.md, "Accès à un document").

    Granted to an active member of the document's thesis who either holds a
    role with DOCUMENT_READ that the level allows (LEVEL_ROLES), or, for a
    CONFIDENTIAL document only, an active explicit grant ("+ liste explicite").
    A grant is worth nothing without an active membership: an expired CSI
    member loses it.
    """
    try:
        level = ConfidentialityLevel(document.confidentiality)
    except ValueError:
        return False
    if document._state.adding:
        return False
    thesis = _thesis_of(document)
    if thesis is None:
        return False
    roles = roles_of(user, thesis)
    if not roles:
        return False
    if any(Action.DOCUMENT_READ in ROLE_ACTIONS[r] and r in LEVEL_ROLES[level] for r in roles):
        return True
    if level is not ConfidentialityLevel.CONFIDENTIAL or user.pk is None:
        return False
    # Through the app registry: accounts does not import vault at module level.
    grants = django_apps.get_model("vault", "DocumentAccessGrant").objects
    return bool(
        grants.filter(document_id=document.pk, user_id=user.pk, revoked_at__isnull=True).exists()
    )


DocumentRestriction = Callable[["AnyUser", "Document"], bool]

# Predicates registered by business modules (AppConfig.ready). Each one may only
# REFUSE: a document is readable when the M2 rule grants it AND every restriction
# accepts. None registered = M2 behaviour.
_DOCUMENT_RESTRICTIONS: list[DocumentRestriction] = []


def register_document_restriction(predicate: DocumentRestriction) -> None:
    """Declare an extra condition on reading documents, ``predicate(user, document)``.

    For a module whose documents need more than role and level: the
    administrative file (M3) closes its attachments to roles without the read
    right of the item's section. A predicate must answer True for documents it
    does not concern. Idempotent: ready() may run more than once.
    """
    if not callable(predicate):
        raise ImproperlyConfigured("register_document_restriction() needs a callable.")
    if predicate not in _DOCUMENT_RESTRICTIONS:
        _DOCUMENT_RESTRICTIONS.append(predicate)


def can_access_document(user: AnyUser, document: Document) -> bool:
    """Read access to one document: the M2 rule AND every registered restriction.

    Restrictions are asked only once the M2 rule has granted access, explicit
    grants included (a grant never bypasses a restriction). A restriction that
    raises is never read as an acceptance: the exception propagates.
    """
    if not _m2_document_rule(user, document):
        return False
    return all(restriction(user, document) for restriction in _DOCUMENT_RESTRICTIONS)


# --------------------------------------------------------------------------- view guards

THESIS_URL_KWARG = "thesis_id"


def _canonical_uuid(value: Any) -> str | None:
    """The id as a canonical UUID string, or None: free text never reaches the log."""
    try:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
    except ValueError:
        return None


def _audit_denial(
    request: HttpRequest, action: Action, thesis: Thesis | None, thesis_id: Any
) -> None:
    """THESIS_ACCESS_DENIED, in its own transaction (kept although 403 follows).

    Called for signed-in users only: the guards send anonymous visitors to the
    login page before, so unauthenticated traffic never fills the append-only log.
    """
    # Imported here: vault.services imports this module (no cycle at load time).
    from apps.vault.audit import AuditAction, audit

    details: dict[str, str] = {"operation": action.value}
    if thesis is None:
        details["reason"] = "unknown_thesis"
        requested = _canonical_uuid(thesis_id) if thesis_id else None
        if requested is not None:
            details["requested_thesis_id"] = requested
    elif roles_of(request.user, thesis):
        details["reason"] = "missing_right"
    else:
        details["reason"] = "not_member"
    with transaction.atomic():
        audit(
            AuditAction.THESIS_ACCESS_DENIED,
            actor=request.user,
            thesis=thesis,
            request=request,
            **details,
        )


def _authorize(request: HttpRequest, action: Action, thesis_id: Any) -> Thesis:
    """Return the thesis or raise PermissionDenied; unknown and forbidden look alike.

    Every refusal is audited (THESIS_ACCESS_DENIED) with the same response, so the
    user cannot tell an unknown thesis from a forbidden one.
    """
    try:
        thesis = Thesis.objects.filter(pk=thesis_id).first() if thesis_id else None
    except (ValueError, ValidationError):  # malformed id: same answer as forbidden
        thesis = None
    if thesis is None or not can(request.user, action, thesis):
        _audit_denial(request, action, thesis, thesis_id)
        raise PermissionDenied
    # For the layout (apps/ui/navigation.py): set only once the guard said yes, so a
    # refused page never shows the sidebar nor the thesis title.
    request.thesis = thesis  # type: ignore[attr-defined]
    return thesis


class ThesisPermissionRequiredMixin:
    """Class-based view guard. Subclasses MUST set ``required_action``.

    Resolves ``thesis_id`` from the URL into ``self.thesis`` before the handler
    runs. Anonymous users are sent to the login page; everyone else without the
    right gets 403.
    """

    required_action: Action | None = None
    thesis: Thesis

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
        if not isinstance(self.required_action, Action):
            raise ImproperlyConfigured(
                f"{type(self).__name__} must set required_action to an Action member."
            )
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        self.thesis = _authorize(request, self.required_action, kwargs.get(THESIS_URL_KWARG))
        response: HttpResponseBase = super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
        return response


_P = ParamSpec("_P")
_View = Callable[Concatenate[HttpRequest, Thesis, _P], HttpResponseBase]


def thesis_permission_required(
    action: Action,
) -> Callable[[_View[_P]], Callable[..., HttpResponseBase]]:
    """Function-view guard: replaces the ``thesis_id`` URL argument by ``thesis``."""
    if not isinstance(action, Action):
        raise ImproperlyConfigured("thesis_permission_required() needs an Action member.")

    def decorator(view: _View[_P]) -> Callable[..., HttpResponseBase]:
        @wraps(view)
        def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponseBase:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            thesis = _authorize(request, action, kwargs.pop(THESIS_URL_KWARG, None))
            return view(request, thesis, *args, **kwargs)

        # Marker checked by the URL audit test (every /t/ route must be guarded).
        wrapped.thesis_permission_action = action  # type: ignore[attr-defined]
        return wrapped

    return decorator
