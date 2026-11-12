"""permissions.can(), can_access_level() and the view guards.

The expected matrices below are a SECOND, independent transcription of
docs/ARCHITECTURE.md, laid out like the document. permissions.py is the first.
If either drifts from the document, the parametrized tests fail.
"""

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from django.utils import timezone
from django.views import View

from apps.accounts.models import Membership, Thesis, User
from apps.accounts.permissions import (
    LEVEL_ROLES,
    ROLE_ACTIONS,
    Action,
    ThesisPermissionRequiredMixin,
    can,
    can_access_level,
    roles_of,
    thesis_permission_required,
)
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.core.confidentiality import ConfidentialityLevel as Level

A = Action
R = Role

# --------------------------------------------------------------------------- expected matrix
# One block per column of the ARCHITECTURE.md table, plus thesis/team actions (M01).

_ALL_BUT_GUEST = {R.PHD_STUDENT, R.DIRECTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.CSI_MEMBER}
_SUPERVISION = {R.PHD_STUDENT, R.DIRECTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR}

EXPECTED: dict[Action, set[Role]] = {
    # Thesis and team (M01-comptes.md): the PhD student is the thesis super-role.
    A.VIEW_THESIS: _ALL_BUT_GUEST,
    A.VIEW_TEAM: _SUPERVISION,  # not the CSI: outside the supervision
    A.INVITE_MEMBER: {R.PHD_STUDENT},
    A.CHANGE_ROLE: {R.PHD_STUDENT},
    A.REMOVE_MEMBER: {R.PHD_STUDENT},
    A.EXTEND_MEMBERSHIP: {R.PHD_STUDENT},
    # Objectifs: écrire / commenter x3 / dossier CSI (-> CSI_FILE_READ) / —
    # + M04-objectifs.md: the CSI reads objectives (read-only, without comments).
    A.OBJECTIVE_READ: _SUPERVISION | {R.CSI_MEMBER},
    A.OBJECTIVE_WRITE: {R.PHD_STUDENT},
    A.OBJECTIVE_COMMENT: _SUPERVISION,
    # Réunions: écrire x4 / — / une réunion (per-object grant, M5)
    A.MEETING_READ: _SUPERVISION,
    A.MEETING_WRITE: _SUPERVISION,
    # Documents: écrire / lire, commenter x3 / rapport CSI / projet partagé (per-object, M2)
    A.DOCUMENT_READ: _SUPERVISION,
    A.DOCUMENT_WRITE: {R.PHD_STUDENT},
    A.DOCUMENT_COMMENT: _SUPERVISION,
    # Admin./RH: écrire / lire / — / partie CIFRE / — / —
    A.ADMIN_FILE_READ: {R.PHD_STUDENT, R.DIRECTOR},
    A.ADMIN_FILE_WRITE: {R.PHD_STUDENT},
    A.CIFRE_FILE_READ: {R.PHD_STUDENT, R.DIRECTOR, R.INDUSTRIAL_TUTOR},
    # CSI_MEMBER column entries
    A.CSI_FILE_READ: {R.CSI_MEMBER},
    A.CSI_REPORT_READ: {R.CSI_MEMBER},
    A.CSI_OPINION_WRITE: {R.CSI_MEMBER},
    # Valide: director = objectifs, CSI, ANRT ; tutor = ANRT, publications
    A.VALIDATE_OBJECTIVES: {R.DIRECTOR},
    A.VALIDATE_CSI: {R.DIRECTOR},
    A.VALIDATE_ANRT: {R.DIRECTOR, R.INDUSTRIAL_TUTOR},
    A.VALIDATE_PUBLICATION: {R.INDUSTRIAL_TUTOR},
}

EXPECTED_LEVELS: dict[Level, set[Role]] = {
    Level.PUBLIC: set(Role),
    Level.INTERNAL_LIRMM: {R.PHD_STUDENT, R.DIRECTOR, R.CO_SUPERVISOR},
    Level.INTERNAL_ST: {R.PHD_STUDENT, R.INDUSTRIAL_TUTOR},
    Level.CONFIDENTIAL: {R.PHD_STUDENT, R.DIRECTOR, R.INDUSTRIAL_TUTOR},
}


def test_expected_matrix_covers_every_action() -> None:
    assert set(EXPECTED) == set(Action)


def test_role_actions_table_matches_expected() -> None:
    """Compares the two tables as data: the failure message names the cell."""
    assert set(ROLE_ACTIONS) == set(Role)
    for role in Role:
        assert ROLE_ACTIONS[role] == {a for a, roles in EXPECTED.items() if role in roles}, role


def test_level_roles_table_matches_expected() -> None:
    assert {k: set(v) for k, v in LEVEL_ROLES.items()} == EXPECTED_LEVELS


def test_tables_are_read_only() -> None:
    with pytest.raises(TypeError):
        ROLE_ACTIONS[Role.GUEST] = frozenset(Action)  # type: ignore[index]
    assert isinstance(ROLE_ACTIONS[Role.GUEST], frozenset)


def test_csi_reads_objectives_but_not_their_comments() -> None:
    """Comments are shown only to OBJECTIVE_COMMENT holders (rule for M4)."""
    csi = ROLE_ACTIONS[Role.CSI_MEMBER]
    assert A.OBJECTIVE_READ in csi
    assert not csi & {A.OBJECTIVE_COMMENT, A.OBJECTIVE_WRITE, A.VIEW_TEAM}


def test_guest_has_no_thesis_level_right() -> None:
    assert ROLE_ACTIONS[Role.GUEST] == frozenset()


# --------------------------------------------------------------------------- can(): role x action


@pytest.mark.django_db
@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("action", list(Action))
def test_can_role_action(role: Role, action: Action) -> None:
    """One 'allowed' or 'refused' test per (role, action), through the database."""
    m = MembershipFactory.create(role=role)
    assert can(m.user, action, m.thesis) is (role in EXPECTED[action])


# --------------------------------------------------------------------------- can(): who


@pytest.fixture
def director() -> Membership:
    return MembershipFactory.create(role=Role.DIRECTOR)


@pytest.mark.django_db
def test_anonymous_is_refused(director: Membership) -> None:
    assert not can(AnonymousUser(), A.VIEW_THESIS, director.thesis)


@pytest.mark.django_db
def test_user_without_membership_is_refused(director: Membership) -> None:
    assert not can(UserFactory.create(), A.VIEW_THESIS, director.thesis)


@pytest.mark.django_db
def test_member_of_another_thesis_is_refused(director: Membership) -> None:
    assert not can(director.user, A.VIEW_THESIS, ThesisFactory.create())


@pytest.mark.django_db
def test_superuser_has_no_implicit_right(director: Membership) -> None:
    admin = UserFactory.create(is_superuser=True, is_staff=True)
    assert not any(can(admin, a, director.thesis) for a in Action)


@pytest.mark.django_db
def test_deactivated_user_is_refused(director: Membership) -> None:
    User.objects.filter(pk=director.user.pk).update(is_active=False)
    director.user.refresh_from_db()
    assert not can(director.user, A.VIEW_THESIS, director.thesis)


@pytest.mark.django_db
def test_removed_member_is_refused(director: Membership) -> None:
    director.active_until = timezone.now()
    director.save()
    assert not can(director.user, A.VIEW_THESIS, director.thesis)


@pytest.mark.django_db
def test_membership_not_started_yet_is_refused() -> None:
    m = MembershipFactory.create(active_from=timezone.now() + dt.timedelta(hours=1))
    assert not can(m.user, A.VIEW_THESIS, m.thesis)


@pytest.mark.django_db
def test_expired_membership_is_refused() -> None:
    m = MembershipFactory.create(
        role=Role.CSI_MEMBER,
        active_from=timezone.now() - dt.timedelta(days=61),
        active_until=timezone.now() - dt.timedelta(days=1),
    )
    assert not can(m.user, A.CSI_FILE_READ, m.thesis)


@pytest.mark.django_db
def test_rights_of_cumulated_roles_add_up(director: Membership) -> None:
    MembershipFactory.create(user=director.user, thesis=director.thesis, role=R.INDUSTRIAL_TUTOR)
    assert roles_of(director.user, director.thesis) == {R.DIRECTOR, R.INDUSTRIAL_TUTOR}
    assert can(director.user, A.VALIDATE_CSI, director.thesis)  # from DIRECTOR
    assert can(director.user, A.VALIDATE_PUBLICATION, director.thesis)  # from TUTOR


@pytest.mark.django_db
def test_rights_are_read_from_database_on_every_call(director: Membership) -> None:
    assert can(director.user, A.VIEW_THESIS, director.thesis)
    Membership.objects.filter(pk=director.pk).update(active_until=timezone.now())
    assert not can(director.user, A.VIEW_THESIS, director.thesis)


# --------------------------------------------------------------------------- can(): what


@pytest.mark.django_db
def test_plain_string_action_is_refused(director: Membership) -> None:
    """Only Action members are accepted: a typo cannot become a silent permission."""
    assert not can(director.user, "VIEW_THESIS", director.thesis)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_object_owned_by_thesis_is_resolved(director: Membership) -> None:
    owned = SimpleNamespace(thesis=director.thesis)
    assert can(director.user, A.VIEW_THESIS, owned)


@pytest.mark.django_db
@pytest.mark.parametrize("obj", [None, object(), SimpleNamespace(thesis="not a thesis")])
def test_object_without_thesis_is_refused(director: Membership, obj: Any) -> None:
    assert not can(director.user, A.VIEW_THESIS, obj)


@pytest.mark.django_db
def test_unsaved_thesis_is_refused(director: Membership) -> None:
    assert not can(director.user, A.VIEW_THESIS, Thesis(title="x"))


# --------------------------------------------------------------------------- can_access_level()


@pytest.mark.django_db
@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("level", list(Level))
def test_can_access_level(role: Role, level: Level) -> None:
    m = MembershipFactory.create(role=role)
    assert can_access_level(m.user, m.thesis, level) is (role in EXPECTED_LEVELS[level])


@pytest.mark.django_db
def test_level_refused_without_membership(director: Membership) -> None:
    assert not can_access_level(UserFactory.create(), director.thesis, Level.PUBLIC)


@pytest.mark.django_db
def test_plain_string_level_is_refused(director: Membership) -> None:
    assert not can_access_level(director.user, director.thesis, "PUBLIC")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- view guards


class _TeamView(ThesisPermissionRequiredMixin, View):
    required_action = Action.VIEW_TEAM

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponse(str(self.thesis.pk))


class _NoActionView(ThesisPermissionRequiredMixin, View):
    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return HttpResponse("should never run")


@thesis_permission_required(Action.VIEW_TEAM)
def _team_function(request: HttpRequest, thesis: Thesis) -> HttpResponse:
    return HttpResponse(str(thesis.pk))


def _get(user: Any, view: Any, thesis_id: Any) -> HttpResponse:
    request = RequestFactory().get("/t/x/")
    request.user = user
    response: HttpResponse = view(request, thesis_id=thesis_id)
    return response


_VIEWS = [_TeamView.as_view(), _team_function]


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
def test_guard_allows_member_with_action(view: Any, director: Membership) -> None:
    response = _get(director.user, view, director.thesis.pk)
    assert response.status_code == 200
    assert response.content.decode() == str(director.thesis.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
def test_guard_refuses_member_without_action(view: Any) -> None:
    guest = MembershipFactory.create(role=Role.GUEST)
    with pytest.raises(PermissionDenied):
        _get(guest.user, view, guest.thesis.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
def test_guard_refuses_removed_member(view: Any, director: Membership) -> None:
    Membership.objects.filter(pk=director.pk).update(active_until=timezone.now())
    with pytest.raises(PermissionDenied):
        _get(director.user, view, director.thesis.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
def test_guard_gives_403_for_unknown_thesis(view: Any, director: Membership) -> None:
    """Unknown and forbidden theses are indistinguishable."""
    with pytest.raises(PermissionDenied):
        _get(director.user, view, uuid.uuid4())


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
def test_guard_redirects_anonymous_to_login(view: Any, director: Membership) -> None:
    response = _get(AnonymousUser(), view, director.thesis.pk)
    assert response.status_code == 302
    assert response["Location"].startswith("/accounts/login/")


@pytest.mark.django_db
def test_view_without_required_action_fails_closed(director: Membership) -> None:
    with pytest.raises(ImproperlyConfigured):
        _get(director.user, _NoActionView.as_view(), director.thesis.pk)


def test_decorator_rejects_non_action() -> None:
    with pytest.raises(ImproperlyConfigured):
        thesis_permission_required("VIEW_TEAM")  # type: ignore[arg-type]


def test_guards_are_marked_for_the_url_audit() -> None:
    """tests/ will walk every /t/ route and require this marker (step 2)."""
    assert getattr(_team_function, "thesis_permission_action", None) is Action.VIEW_TEAM


@pytest.mark.django_db
@pytest.mark.parametrize("view", _VIEWS)
@pytest.mark.parametrize("bad_id", ["not-a-uuid", None, ""])
def test_guard_gives_403_for_malformed_thesis_id(
    view: Any, director: Membership, bad_id: Any
) -> None:
    with pytest.raises(PermissionDenied):
        _get(director.user, view, bad_id)
