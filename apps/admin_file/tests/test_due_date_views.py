"""Pages for due dates and reopening (M3, revised 2026-11-14): who sees which
control, a 403 BEFORE any re-authentication for a refused role, a recent second
factor for a validator's decision, browser-like POSTs (CSRF + Origin), and what a
reopened or rejected item shows of its cancelled approvals.

transaction=True (CLAUDE.md): signing in is audited, then the vault stores.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Thesis
from apps.accounts.permissions import ThesisPermissionRequiredMixin
from apps.accounts.reauth import RecentAuthRequiredMixin
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.helpers import mfa_login
from apps.admin_file import services, views
from apps.admin_file.models import DueDateProposal, DueDateReason, Reason, RequiredItem
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest")
SITE_ORIGIN = "http://testserver"
HX = {"HX-Request": "true"}
LATER = dt.date(2027, 6, 1)


@pytest.fixture
def team() -> SimpleNamespace:
    thesis = ThesisFactory.create(status=Thesis.Status.SETUP)

    def member(role: Role, **kwargs: Any) -> Any:
        return MembershipFactory.create(
            thesis=thesis, role=role, user__password=PASSWORD, **kwargs
        ).user

    team = SimpleNamespace(
        thesis=thesis,
        student=member(Role.PHD_STUDENT),
        director=member(Role.DIRECTOR),
        co_supervisor=member(Role.CO_SUPERVISOR),
        tutor=member(Role.INDUSTRIAL_TUTOR),
        csi=member(Role.CSI_MEMBER, active_until=timezone.now() + dt.timedelta(days=60)),
        guest=member(Role.GUEST),
    )
    services.seed_items(thesis)
    return team


def _item(team: SimpleNamespace, code: str) -> RequiredItem:
    return RequiredItem.objects.get(thesis=team.thesis, code=code)


def _signed_in(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _mfa(user: Any) -> Client:
    client = Client()
    mfa_login(client, user, PASSWORD)
    return client


def _url(name: str, team: SimpleNamespace, item: RequiredItem, **extra: Any) -> str:
    kwargs = {"thesis_id": team.thesis.pk, "item_id": item.pk, **extra}
    return reverse(f"admin_file:{name}", kwargs=kwargs)


def _csrf_token(response: Any) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode())
    assert match, "no CSRF token in the page"
    return match.group(1)


def _browser_post(client: Client, page_url: str, url: str, data: dict[str, Any]) -> Any:
    """What a browser does: the page's CSRF token and the site's Origin header."""
    page = client.get(page_url)
    return client.post(
        url, {**data, "csrfmiddlewaretoken": _csrf_token(page)}, HTTP_ORIGIN=SITE_ORIGIN
    )


def _browser(user: Any, *, mfa: bool) -> Client:
    """A CSRF-enforcing client carrying a real session (signed in by a plain client:
    the sign-in forms are not what is tested here)."""
    signed = _mfa(user) if mfa else _signed_in(user)
    client = Client(enforce_csrf_checks=True)
    client.cookies = signed.cookies
    return client


def _propose(team: SimpleNamespace, item: RequiredItem) -> DueDateProposal:
    return services.propose_due_date(
        item=item, user=team.student, due_date=LATER, reason=DueDateReason.WAITING_SIGNATURE
    )


def _custom(team: SimpleNamespace) -> RequiredItem:
    return services.create_custom_item(
        thesis=team.thesis,
        user=team.student,
        label="Pièce personnalisée fictive",
        section="ACADEMIC",  # type: ignore[arg-type]
        validator_roles=[Role.DIRECTOR],
        default_level=L.CONFIDENTIAL,
        mandatory=True,
        attachment_required=False,
        due_date=dt.date(2027, 1, 15),
    )


# --------------------------------------------------------------------------- order of checks


REAUTH_VIEWS = (views.DueDateView, views.AcceptDueDateView, views.RefuseDueDateView)


@pytest.mark.parametrize("view_class", REAUTH_VIEWS)
def test_guard_then_item_gate_then_reauth_then_view(view_class: type) -> None:
    from django.views import View

    mro = view_class.__mro__
    assert mro.index(ThesisPermissionRequiredMixin) < mro.index(views.ValidatorGateMixin)
    assert mro.index(views.ValidatorGateMixin) < mro.index(RecentAuthRequiredMixin)
    assert mro.index(RecentAuthRequiredMixin) < mro.index(View)


@pytest.mark.parametrize("view_class", [views.CustomDueDateView, views.ProposeDueDateView])
def test_student_views_need_no_second_factor(view_class: type) -> None:
    assert not issubclass(view_class, RecentAuthRequiredMixin)


# --------------------------------------------------------------------------- direct change


@pytest.mark.parametrize("role", ROLES)
def test_catalogue_due_date_by_role(team: SimpleNamespace, role: str) -> None:
    """employment_contract: CIFRE, validated by the tutor only."""
    item = _item(team, "employment_contract")
    before = item.due_date
    user = getattr(team, role)
    response = _signed_in(user).post(_url("due_date", team, item), {"due_date": "2027-06-01"})
    if role == "tutor":  # allowed, but asked for a second factor first
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_reauthenticate"))
        response = _mfa(user).post(_url("due_date", team, item), {"due_date": "2027-06-01"})
        assert response.status_code == 302
        assert _item(team, "employment_contract").due_date == LATER
    else:  # 403 before any re-authentication, nothing changed
        assert response.status_code == 403
        assert _item(team, "employment_contract").due_date == before


def test_a_refused_change_of_a_visible_item_is_audited(team: SimpleNamespace) -> None:
    item = _item(team, "employment_contract")
    _signed_in(team.director).post(_url("due_date", team, item), {"due_date": "2027-06-01"})
    event = AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED").order_by("-seq")[0]
    assert (event.actor, event.details_json["reason"]) == (team.director, "not_validator")


def test_clearing_a_mandatory_catalogue_due_date_is_a_400(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _mfa(team.director).post(_url("due_date", team, item), {"due_date": ""}, headers=HX)
    assert response.status_code == 400
    assert _item(team, "adum_enrollment").due_date == item.due_date


def test_a_date_after_the_thesis_is_a_400(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _mfa(team.director).post(
        _url("due_date", team, item), {"due_date": "2030-01-01"}, headers=HX
    )
    assert response.status_code == 400


def test_direct_change_posted_like_a_browser(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    client = _browser(team.director, mfa=True)
    response = _browser_post(
        client, _url("item", team, item), _url("due_date", team, item), {"due_date": "2027-06-01"}
    )
    assert response.status_code == 302, response.content[:300]
    assert _item(team, "adum_enrollment").due_date == LATER


@pytest.mark.parametrize("role", ROLES)
def test_custom_due_date_by_role(team: SimpleNamespace, role: str) -> None:
    item = _custom(team)
    response = _signed_in(getattr(team, role)).post(
        _url("custom_due_date", team, item), {"due_date": "2027-06-01"}
    )
    assert response.status_code == (302 if role == "student" else 403)
    expected = LATER if role == "student" else dt.date(2027, 1, 15)
    assert RequiredItem.objects.get(pk=item.pk).due_date == expected


def test_the_custom_route_refuses_a_catalogue_item(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _signed_in(team.student).post(
        _url("custom_due_date", team, item), {"due_date": "2027-06-01"}
    )
    assert response.status_code == 403
    assert _item(team, "adum_enrollment").due_date == item.due_date


# --------------------------------------------------------------------------- proposals


@pytest.mark.parametrize("role", ROLES)
def test_propose_by_role(team: SimpleNamespace, role: str) -> None:
    item = _item(team, "training_agreement")
    data = {"due_date": "2027-06-01", "reason": "WAITING_THIRD_PARTY", "comment": ""}
    response = _signed_in(getattr(team, role)).post(_url("propose_due_date", team, item), data)
    assert response.status_code == (302 if role == "student" else 403)
    assert DueDateProposal.objects.filter(item=item).exists() == (role == "student")


def test_propose_posted_like_a_browser(team: SimpleNamespace) -> None:
    item = _item(team, "training_agreement")
    client = _browser(team.student, mfa=False)
    data = {"due_date": "2027-06-01", "reason": "ADMINISTRATIVE_DELAY", "comment": "Fictif"}
    response = _browser_post(
        client, _url("item", team, item), _url("propose_due_date", team, item), data
    )
    assert response.status_code == 302, response.content[:300]
    assert DueDateProposal.objects.get(item=item).reason_code == "ADMINISTRATIVE_DELAY"


def test_a_second_proposal_is_a_400(team: SimpleNamespace) -> None:
    item = _item(team, "training_agreement")
    _propose(team, item)
    data = {"due_date": "2027-07-01", "reason": "OTHER"}
    response = _signed_in(team.student).post(_url("propose_due_date", team, item), data, headers=HX)
    assert response.status_code == 400


@pytest.mark.parametrize("decision", ["accept_due_date", "refuse_due_date"])
@pytest.mark.parametrize("role", ROLES)
def test_decide_by_role(team: SimpleNamespace, decision: str, role: str) -> None:
    item = _item(team, "training_agreement")  # academic: the director decides
    proposal = _propose(team, item)
    url = _url(decision, team, item, proposal_id=proposal.pk)
    response = _signed_in(getattr(team, role)).post(url)
    if role == "director":
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("mfa_reauthenticate"))
        assert _mfa(team.director).post(url).status_code == 302
        proposal.refresh_from_db()
        assert proposal.status == ("ACCEPTED" if decision == "accept_due_date" else "REFUSED")
    else:
        assert response.status_code == 403
        proposal.refresh_from_db()
        assert proposal.status == "PENDING"


def test_accept_posted_like_a_browser(team: SimpleNamespace) -> None:
    item = _item(team, "training_agreement")
    proposal = _propose(team, item)
    client = _browser(team.director, mfa=True)
    url = _url("accept_due_date", team, item, proposal_id=proposal.pk)
    response = _browser_post(client, _url("item", team, item), url, {})
    assert response.status_code == 302, response.content[:300]
    assert _item(team, "training_agreement").due_date == LATER


@pytest.mark.parametrize("role", ROLES)
def test_withdraw_by_role(team: SimpleNamespace, role: str) -> None:
    item = _item(team, "training_agreement")
    proposal = _propose(team, item)
    url = _url("withdraw_due_date", team, item, proposal_id=proposal.pk)
    response = _signed_in(getattr(team, role)).post(url)
    assert response.status_code == (302 if role == "student" else 403)


def test_a_proposal_through_another_item_url_is_refused(team: SimpleNamespace) -> None:
    proposal = _propose(team, _item(team, "training_agreement"))
    other = _item(team, "training_plan")
    url = _url("accept_due_date", team, other, proposal_id=proposal.pk)
    assert _mfa(team.director).post(url).status_code == 403
    proposal.refresh_from_db()
    assert proposal.status == "PENDING"


# --------------------------------------------------------------------------- what is shown


def _page(user: Any, team: SimpleNamespace, item: RequiredItem) -> str:
    return _signed_in(user).get(_url("item", team, item)).content.decode()


def test_controls_shown_for_a_catalogue_item(team: SimpleNamespace) -> None:
    item = _item(team, "employment_contract")
    student, tutor, director = (
        _page(u, team, item) for u in (team.student, team.tutor, team.director)
    )
    assert "Proposer cette échéance" in student and "Enregistrer l'échéance" not in student
    assert "Enregistrer l'échéance" in tutor and "Proposer cette échéance" not in tutor
    assert "Enregistrer l'échéance" not in director


def test_controls_shown_for_a_custom_item(team: SimpleNamespace) -> None:
    item = _custom(team)
    assert "Enregistrer l'échéance" in _page(team.student, team, item)
    assert "Enregistrer l'échéance" not in _page(team.director, team, item)


def test_a_pending_proposal_is_shown_with_its_decisions(team: SimpleNamespace) -> None:
    item = _item(team, "training_agreement")
    _propose(team, item)
    director, student = _page(team.director, team, item), _page(team.student, team, item)
    assert "Échéance proposée" in director and "Échéance proposée" in student
    assert "Accepter" in director and "Refuser la proposition" in director
    assert "Retirer la proposition" in student and "Accepter" not in student
    assert "Proposer cette échéance" not in student  # one pending at a time


def _reopen_options(html: str) -> set[str]:
    form = html[html.index("/reopen/") :]
    form = form[: form.index("</form>")]
    return set(re.findall(r'<option value="([A-Z_]+)"', form))


def test_the_student_reopen_form_offers_three_reasons(team: SimpleNamespace) -> None:
    item = _item(team, "employment_contract")
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.tutor)
    assert _reopen_options(_page(team.student, team, item)) == {"AMENDMENT", "EXPIRED", "OTHER"}
    assert _reopen_options(_page(team.tutor, team, item)) == {r.value for r in Reason}


def test_a_reopened_item_shows_no_valid_approval(team: SimpleNamespace) -> None:
    item = _item(team, "employment_contract")
    services.submit(item=item, user=team.student)
    services.approve(item=item, user=team.tutor)
    services.reopen(item=item, user=team.student, reason=Reason.AMENDMENT, comment="Avenant")
    for user in (team.tutor, team.student):
        html = _page(user, team, item)
        assert "puis rouverte le" in html
        assert "Avenant" in html
        assert 'class="approvals"' not in html  # the list of VALID approvals
        assert "Vous avez approuvé ce dépôt" not in html


def test_a_rejected_two_party_item_shows_the_cancelled_approval(team: SimpleNamespace) -> None:
    item = _item(team, "collaboration_contract")
    from django.core.files.uploadedfile import SimpleUploadedFile

    upload = SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%fictive\n%%EOF\n")
    services.submit(item=item, user=team.student, upload=upload)
    services.approve(item=item, user=team.director)
    services.reject(item=item, user=team.tutor, reason=Reason.MISSING_SIGNATURE)
    html = _page(team.director, team, item)
    assert "Approbations annulées" in html
    assert 'class="approvals"' not in html
    assert "Vous avez approuvé ce dépôt" not in html
