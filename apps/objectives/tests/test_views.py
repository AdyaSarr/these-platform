"""Views of the objectives (/t/<thesis_id>/objectives/): rights per role, recent second
factor, reasons, CSI without comments, restricted content, dashboard, one audited
read per request, no title outside the pages that show it."""

from __future__ import annotations

import datetime as dt
import html as html_module
import logging
import re
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory, UserFactory
from apps.accounts.tests.helpers import mfa_login, password_reauthenticate
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.objectives import services
from apps.objectives.models import Objective, ObjectiveRevision, Status
from apps.objectives.tests.helpers import D, marker, year_of
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
HX = {"HX-Request": "true"}
NOW = dt.datetime(2027, 3, 15, 10, 0, tzinfo=dt.UTC)
RESTRICTED_TEXT = "Contenu restreint"
PUBLIC_WARNING = "visible par les membres du CSI, extérieurs à l'encadrement"
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest", "outsider")


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    # Ticking: allauth's authentication records and the TOTP need a moving clock.
    time_machine.move_to(NOW, tick=True)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create(
        cifre_start_date=D(2026, 12, 1),
        first_enrollment_date=D(2026, 12, 15),
        planned_end_date=D(2029, 11, 30),
    )

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    return SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
        outsider=UserFactory.create(password=PASSWORD),
    )


def _client(team: SimpleNamespace, role: str, *, mfa: bool = False) -> Client:
    client = Client()
    user = getattr(team, role)
    if mfa:
        mfa_login(client, user, PASSWORD)
    else:
        client.force_login(user)  # signed in, but no recent second factor recorded
    return client


def _url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(f"objectives:{name}", kwargs={"thesis_id": team.thesis.pk, **kw})


def _objective(team: SimpleNamespace, **overrides: Any) -> Objective:
    fields: dict[str, Any] = {
        "thesis": team.thesis,
        "user": team.student,
        "year": year_of(team),
        "title": "Titre fictif",
        "description": "Description fictive",
        "success_criterion": "Critère fictif",
        "category": "RESEARCH",
        "due_date": D(2027, 6, 30),
        "weight": 2,
        "level": L.PUBLIC,
    }
    fields.update(overrides)
    return services.create_objective(**fields)


def _approved(team: SimpleNamespace, **overrides: Any) -> Objective:
    objective = _objective(team, **overrides)
    return services.approve_objective(objective=objective, user=team.director, recent_mfa=True)


def _events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("seq"))


def _form_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "year": "1",
        "title": "Nouvel objectif fictif",
        "description": "",
        "success_criterion": "",
        "category": "RESEARCH",
        "due_date": "2027-06-30",
        "weight": "2",
        "level": "PUBLIC",
    }
    data.update(overrides)
    return data


# =========================================================================== rights per view


READERS = {"student", "director", "co_supervisor", "tutor", "csi"}


@pytest.mark.parametrize("role", ROLES)
def test_dashboard(team: SimpleNamespace, role: str) -> None:
    response = _client(team, role).get(_url(team, "dashboard"))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_detail(team: SimpleNamespace, role: str) -> None:
    objective = _objective(team)
    response = _client(team, role).get(_url(team, "detail", objective_id=objective.pk))
    assert response.status_code == (200 if role in READERS else 403)


@pytest.mark.parametrize("role", ROLES)
def test_new_objective_page(team: SimpleNamespace, role: str) -> None:
    response = _client(team, role).get(_url(team, "new"))
    assert response.status_code == (200 if role == "student" else 403)


@pytest.mark.parametrize("role", ROLES)
def test_create(team: SimpleNamespace, role: str) -> None:
    response = _client(team, role).post(_url(team, "new"), _form_data())
    if role == "student":
        objective = Objective.objects.get()
        assert response.status_code == 302
        assert response["Location"] == _url(team, "detail", objective_id=objective.pk)
    else:
        assert response.status_code == 403
        assert not Objective.objects.exists()


@pytest.mark.parametrize("role", ROLES)
def test_edit(team: SimpleNamespace, role: str) -> None:
    objective = _objective(team)
    client = _client(team, role)
    assert client.get(_url(team, "edit", objective_id=objective.pk)).status_code == (
        200 if role == "student" else 403
    )
    response = client.post(
        _url(team, "edit", objective_id=objective.pk), _form_data(title="Titre modifié fictif")
    )
    assert response.status_code == (302 if role == "student" else 403)


@pytest.mark.parametrize("role", ROLES)
def test_progress(team: SimpleNamespace, role: str) -> None:
    objective = _objective(team)
    response = _client(team, role).post(
        _url(team, "progress", objective_id=objective.pk),
        {"date": "2027-03-10", "progress": "30", "status": "IN_PROGRESS", "note": ""},
        headers=HX,
    )
    assert response.status_code == (200 if role == "student" else 403)
    assert Objective.objects.get(pk=objective.pk).progress == (30 if role == "student" else 0)


@pytest.mark.parametrize("role", ROLES)
def test_comments_fragment(team: SimpleNamespace, role: str) -> None:
    objective = _objective(team)
    commenters = {"student", "director", "co_supervisor", "tutor"}
    client = _client(team, role)
    url = _url(team, "comments", objective_id=objective.pk)
    assert client.get(url, headers=HX).status_code == (200 if role in commenters else 403)
    response = client.post(url, {"body": "Avis fictif"}, headers=HX)
    assert response.status_code == (200 if role in commenters else 403)


def test_an_objective_of_another_thesis_through_my_url_is_refused(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    theirs_student = MembershipFactory.create(thesis=other, role=Role.PHD_STUDENT).user
    theirs = services.create_objective(
        thesis=other,
        user=theirs_student,
        year=other.years.get(number=1),
        title="Autre fictif",
        category="RESEARCH",
        due_date=D(2027, 6, 30),
    )
    client = _client(team, "director")
    for name in ("detail", "comments", "edit"):
        assert client.get(_url(team, name, objective_id=theirs.pk)).status_code == 403, name


# =========================================================================== recent second factor


DECISIONS = {
    "approve": ("director", lambda team, o: {}),
    "acknowledge": ("director", lambda team, o: {}),
    "level": (
        "student",
        lambda team, o: {"level": "CONFIDENTIAL", "reason": "Motif fictif suffisant"},
    ),
}


def _ready(team: SimpleNamespace, name: str) -> Objective:
    if name == "approve":
        return _objective(team)
    objective = _approved(team)
    if name == "acknowledge":
        services.update_objective(
            objective=objective,
            user=team.student,
            changes={"weight": 3},
            reason="Motif fictif suffisant",
        )
    return objective


def _decided(team: SimpleNamespace, name: str, objective: Objective) -> bool:
    row = Objective.objects.get(pk=objective.pk)
    return {
        "approve": row.approved_at is not None,
        "acknowledge": not row.changed_since_approval,
        "level": row.level == L.CONFIDENTIAL,
    }[name]


@pytest.mark.parametrize("name", list(DECISIONS))
def test_without_a_recent_second_factor_nothing_happens(team: SimpleNamespace, name: str) -> None:
    role, data = DECISIONS[name]
    objective = _ready(team, name)
    client = _client(team, role)  # force_login: no authentication record at all
    url = _url(team, name, objective_id=objective.pk)
    response = client.post(url, data(team, objective))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    htmx = client.post(url, data(team, objective), headers=HX)
    assert htmx.status_code == 200 and htmx["HX-Redirect"].startswith(reverse("mfa_reauthenticate"))
    assert not _decided(team, name, objective)


@pytest.mark.parametrize("name", list(DECISIONS))
def test_the_password_alone_is_not_enough(team: SimpleNamespace, name: str) -> None:
    role, data = DECISIONS[name]
    objective = _ready(team, name)
    client = _client(team, role)
    password_reauthenticate(client, PASSWORD)  # a recent PASSWORD re-authentication
    response = client.post(_url(team, name, objective_id=objective.pk), data(team, objective))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    assert not _decided(team, name, objective)


@pytest.mark.parametrize("name", list(DECISIONS))
def test_with_a_recent_second_factor_the_decision_is_made(team: SimpleNamespace, name: str) -> None:
    role, data = DECISIONS[name]
    objective = _ready(team, name)
    client = _client(team, role, mfa=True)
    response = client.post(_url(team, name, objective_id=objective.pk), data(team, objective))
    assert response.status_code == 302
    assert response["Location"] == _url(team, "detail", objective_id=objective.pk)
    assert _decided(team, name, objective)


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("approve", "student"),
        ("approve", "co_supervisor"),
        ("approve", "tutor"),
        ("approve", "csi"),
        ("acknowledge", "student"),
        ("acknowledge", "tutor"),
        ("level", "director"),
        ("level", "csi"),
    ],
)
def test_403_comes_before_any_reauthentication(team: SimpleNamespace, name: str, role: str) -> None:
    """No recent second factor here: a wrong order of classes would answer 302."""
    _role, data = DECISIONS[name]
    objective = _ready(team, name)
    response = _client(team, role).post(
        _url(team, name, objective_id=objective.pk), data(team, objective)
    )
    assert response.status_code == 403
    assert not _decided(team, name, objective)


def test_approve_a_whole_year(team: SimpleNamespace) -> None:
    first, second = _objective(team), _objective(team)
    url = _url(team, "approve_year")
    assert _client(team, "co_supervisor").post(url, {"year": "1"}).status_code == 403
    no_mfa = _client(team, "director").post(url, {"year": "1"})
    assert no_mfa.status_code == 302 and no_mfa["Location"].startswith(
        reverse("mfa_reauthenticate")
    )
    assert Objective.objects.filter(approved_at__isnull=False).count() == 0
    response = _client(team, "director", mfa=True).post(url, {"year": "1"})
    assert response.status_code == 302
    assert {o.pk for o in Objective.objects.filter(approved_at__isnull=False)} == {
        first.pk,
        second.pk,
    }


def test_approve_year_refuses_an_unknown_year(team: SimpleNamespace) -> None:
    client = _client(team, "director", mfa=True)
    for value in ("9", "abc", ""):
        assert client.post(_url(team, "approve_year"), {"year": value}).status_code == 400


def test_the_class_order_is_guard_then_reauth_then_view() -> None:
    from django.views import View

    from apps.accounts.permissions import ThesisPermissionRequiredMixin
    from apps.accounts.reauth import RecentAuthRequiredMixin
    from apps.objectives import views

    for cls in (views.ApproveView, views.ApproveYearView, views.AcknowledgeView, views.LevelView):
        mro = cls.__mro__
        assert (
            mro.index(ThesisPermissionRequiredMixin)
            < mro.index(RecentAuthRequiredMixin)
            < mro.index(View)
        ), cls
        assert cls.reauth_mfa is True


# =========================================================================== reasons


def test_editing_an_approved_objective_without_reason_is_refused(team: SimpleNamespace) -> None:
    """Acceptance criterion 1, view side."""
    objective = _approved(team)
    client = _client(team, "student")
    page = client.get(_url(team, "edit", objective_id=objective.pk))
    assert 'name="reason"' in page.content.decode()
    url = _url(team, "edit", objective_id=objective.pk)
    unchanged = {"description": "Description fictive", "success_criterion": "Critère fictif"}
    response = client.post(url, _form_data(title="Titre modifié fictif", reason="", **unchanged))
    assert response.status_code == 400
    assert "motif" in response.content.decode().lower()
    assert not ObjectiveRevision.objects.exists()
    response = client.post(
        url,
        _form_data(
            title="Titre modifié fictif", reason="Le périmètre fictif a changé", **unchanged
        ),
    )
    assert response.status_code == 302
    assert list(ObjectiveRevision.objects.values_list("field", flat=True)) == ["title"]


def test_a_draft_form_asks_no_reason(team: SimpleNamespace) -> None:
    objective = _objective(team)
    page = _client(team, "student").get(_url(team, "edit", objective_id=objective.pk))
    assert 'name="reason"' not in page.content.decode()


def test_dropping_without_reason_is_refused(team: SimpleNamespace) -> None:
    objective = _objective(team)
    client = _client(team, "student")
    url = _url(team, "drop", objective_id=objective.pk)
    response = client.post(url, {"reason": ""})
    assert response.status_code == 400
    assert Objective.objects.get(pk=objective.pk).status == Status.PLANNED
    response = client.post(url, {"reason": "Piste abandonnée, motif fictif"})
    assert response.status_code == 302
    assert Objective.objects.get(pk=objective.pk).status == Status.DROPPED


def test_a_state_conflict_is_a_409(team: SimpleNamespace) -> None:
    objective = _approved(team)
    response = _client(team, "director", mfa=True).post(
        _url(team, "approve", objective_id=objective.pk)
    )
    assert response.status_code == 409


# =========================================================================== CSI


def test_csi_sees_objectives_but_never_the_comments(team: SimpleNamespace) -> None:
    """Acceptance criterion 4, view side."""
    objective = _approved(team)
    comment = marker()
    services.add_comment(objective=objective, user=team.director, body=comment)
    client = _client(team, "csi")
    page = client.get(_url(team, "detail", objective_id=objective.pk))
    html = page.content.decode()
    assert page.status_code == 200
    assert "Titre fictif" in html  # read-only, but readable
    comments_url = _url(team, "comments", objective_id=objective.pk)
    assert comments_url not in html  # the fragment is not even requested
    assert comment not in html
    objectives_root = _url(team, "dashboard")
    assert not re.search(rf'(action|hx-post|hx-get)="{re.escape(objectives_root)}', html)
    assert "<textarea" not in html and "hx-post" not in html
    fragment = client.get(comments_url, headers=HX)
    assert fragment.status_code == 403
    assert comment not in fragment.content.decode()


def test_supervisors_load_the_comments_fragment(team: SimpleNamespace) -> None:
    objective = _approved(team)
    comment = marker()
    services.add_comment(objective=objective, user=team.tutor, body=comment)
    client = _client(team, "director")
    html = client.get(_url(team, "detail", objective_id=objective.pk)).content.decode()
    comments_url = _url(team, "comments", objective_id=objective.pk)
    assert f'hx-get="{comments_url}"' in html
    assert comment in client.get(comments_url, headers=HX).content.decode()


def test_the_csi_dashboard_links_to_the_objectives(team: SimpleNamespace) -> None:
    html = (
        _client(team, "csi")
        .get(reverse("accounts:csi_file", kwargs={"thesis_id": team.thesis.pk}))
        .content.decode()
    )
    assert _url(team, "dashboard") in html


# =========================================================================== restricted content


@pytest.mark.parametrize(
    ("level", "role"),
    [(L.INTERNAL_LIRMM, "tutor"), (L.INTERNAL_LIRMM, "csi"), (L.CONFIDENTIAL, "co_supervisor")],
)
def test_restricted_content_for_roles_without_the_level(
    team: SimpleNamespace, level: L, role: str
) -> None:
    title = marker()
    objective = _approved(team, level=level, title=title)
    client = _client(team, role)
    for url in (_url(team, "dashboard"), _url(team, "detail", objective_id=objective.pk)):
        html = client.get(url).content.decode()
        assert title not in html, url
        assert RESTRICTED_TEXT in html, url


def test_the_public_warning_is_in_the_forms(team: SimpleNamespace) -> None:
    client = _client(team, "student")

    def text(url: str) -> str:  # the apostrophe is escaped (&#x27;) in the HTML
        return html_module.unescape(client.get(url).content.decode())

    assert PUBLIC_WARNING in text(_url(team, "new"))
    objective = _approved(team)
    assert PUBLIC_WARNING in text(_url(team, "detail", objective_id=objective.pk))  # level form


def test_internal_st_is_not_offered(team: SimpleNamespace) -> None:
    html = _client(team, "student").get(_url(team, "new")).content.decode()
    assert 'value="INTERNAL_ST"' not in html
    response = _client(team, "student").post(_url(team, "new"), _form_data(level="INTERNAL_ST"))
    assert response.status_code == 400


# =========================================================================== dashboard


def test_dashboard_counts_drafts_overdue_and_pending_acknowledgements(
    team: SimpleNamespace,
) -> None:
    _objective(team, title="Brouillon fictif A")
    _objective(team, title="Brouillon fictif B")
    late = _approved(team, title="En retard fictif", due_date=D(2027, 3, 1))
    changed = _approved(team, title="Modifié fictif")
    services.update_objective(
        objective=changed, user=team.student, changes={"weight": 3}, reason="Motif fictif suffisant"
    )
    declared = _approved(team, title="Atteint fictif")
    services.add_progress(
        objective=declared,
        user=team.student,
        date=D(2027, 3, 10),
        progress=100,
        status=Status.ACHIEVED,
    )
    director = _client(team, "director").get(_url(team, "dashboard")).content.decode()
    assert 'data-drafts="2"' in director
    assert 'data-overdue="1"' in director
    pending = re.search(r'<section class="pending"[^>]*>(.*?)</section>', director, re.S)
    assert pending is not None
    assert "Modifié fictif" in pending.group(1) and "Atteint fictif" in pending.group(1)
    assert "En retard fictif" not in pending.group(1)
    overdue = re.search(r'<section class="overdue-list"[^>]*>(.*?)</section>', director, re.S)
    assert overdue is not None and "En retard fictif" in overdue.group(1)
    assert "<svg" in director  # the chart
    co = _client(team, "co_supervisor").get(_url(team, "dashboard")).content.decode()
    assert '<section class="pending"' not in co  # the director's list only
    assert late.pk  # (used)


def test_dashboard_of_another_year(team: SimpleNamespace) -> None:
    _objective(team, title="Année deux fictive", year=year_of(team, 2))
    client = _client(team, "student")
    assert "Année deux fictive" not in client.get(_url(team, "dashboard")).content.decode()
    html = client.get(_url(team, "dashboard") + "?year=2").content.decode()
    assert "Année deux fictive" in html
    assert client.get(_url(team, "dashboard") + "?year=abc").status_code == 200  # current year


def test_the_thesis_dashboard_has_an_objectives_block(team: SimpleNamespace) -> None:
    html = (
        _client(team, "director")
        .get(reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}))
        .content.decode()
    )
    assert _url(team, "dashboard") in html


# ====================================================== one audited read per request


def test_one_confidential_read_event_per_dashboard_request(team: SimpleNamespace) -> None:
    for i in range(3):
        _approved(team, level=L.CONFIDENTIAL, title=f"Confidentiel fictif {i}")
    _approved(team, level=L.PUBLIC)
    before = len(_events("SEALED_TEXT_READ"))
    _client(team, "director").get(_url(team, "dashboard"))
    assert len(_events("SEALED_TEXT_READ")) == before + 1


def test_one_confidential_read_event_per_detail_request(team: SimpleNamespace) -> None:
    objective = _approved(team, level=L.CONFIDENTIAL)
    services.update_objective(
        objective=objective,
        user=team.student,
        changes={"title": "Titre bis fictif"},
        reason="Motif fictif suffisant",
    )
    for day in (1, 2):
        services.add_progress(
            objective=objective,
            user=team.student,
            date=D(2027, 3, day),
            progress=10 * day,
            status=Status.IN_PROGRESS,
            note=f"Note fictive {day}",
        )
    before = len(_events("SEALED_TEXT_READ"))
    html = (
        _client(team, "director")
        .get(_url(team, "detail", objective_id=objective.pk))
        .content.decode()
    )
    assert "Note fictive 2" in html and "Titre bis fictif" in html
    assert len(_events("SEALED_TEXT_READ")) == before + 1


# ====================================================== no title outside the page


def test_no_title_in_urls_logs_error_pages_or_page_titles(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    title = marker()
    client = _client(team, "student")
    created = client.post(_url(team, "new"), _form_data(title=title))
    objective = Objective.objects.get()
    responses = [
        created,
        client.get(_url(team, "detail", objective_id=objective.pk)),
        client.post(_url(team, "drop", objective_id=objective.pk), {"reason": ""}),  # 400
        _client(team, "guest").get(_url(team, "detail", objective_id=objective.pk)),  # 403
        _client(team, "director").get(_url(team, "dashboard")),
    ]
    for response in responses:
        assert title not in response.get("Location", "")
        html = response.content.decode()
        page_title = re.search(r"<title>(.*?)</title>", html, re.S)
        assert page_title is None or title not in page_title.group(1)
    assert title not in responses[3].content.decode()  # the 403 page
    assert title not in created["Location"]
    assert all(title not in record.getMessage() for record in caplog.records)
    assert all(title not in str(record.args) for record in caplog.records)
