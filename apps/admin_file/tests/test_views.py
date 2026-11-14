"""Pages of the administrative file: allowed and refused per role, re-authentication
before every decision, clear 409/400 messages, htmx fragments, dashboard block.

transaction=True (CLAUDE.md): signing in is audited, then the vault stores.
"""

from __future__ import annotations

import datetime as dt
import html as html_lib
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
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
from apps.admin_file.models import ItemApproval, ItemStatus, Reason, RequiredItem
from apps.admin_file.tests.helpers import put
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services as vault
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)
S = ItemStatus
ROLES = ("student", "director", "co_supervisor", "tutor", "csi", "guest")
READERS = {"student", "director", "tutor"}  # CIFRE_FILE_READ
HX = {"HX-Request": "true"}


def _pdf(marker: str = "fictive") -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", f"%PDF-1.7\n%Piece {marker}\n%%EOF\n".encode())


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
    """Signed in WITHOUT a recent second factor (no authentication record)."""
    client = Client()
    client.force_login(user)
    return client


def _mfa(user: Any) -> Client:
    """Signed in through allauth's real flow: second factor less than 10 min old."""
    client = Client()
    mfa_login(client, user, PASSWORD)
    return client


def _url(name: str, team: SimpleNamespace, item: RequiredItem | None = None) -> str:
    kwargs: dict[str, Any] = {"thesis_id": team.thesis.pk}
    if item is not None:
        kwargs["item_id"] = item.pk
    return reverse(f"admin_file:{name}", kwargs=kwargs)


def _submit(team: SimpleNamespace, item: RequiredItem, **data: Any) -> Any:
    data.setdefault("file", _pdf())
    return _signed_in(team.student).post(_url("submit", team, item), data)


# --------------------------------------------------------------------------- reading


@pytest.mark.parametrize("role", ROLES)
def test_file_page_by_role(team: SimpleNamespace, role: str) -> None:
    response = _signed_in(getattr(team, role)).get(_url("file", team))
    assert response.status_code == (200 if role in READERS else 403)


def test_the_tutor_sees_the_cifre_part_only(team: SimpleNamespace) -> None:
    html = _signed_in(team.tutor).get(_url("file", team)).content.decode()
    assert "Contrat de travail ST" in html
    assert "Inscription ADUM" not in html
    html = _signed_in(team.director).get(_url("file", team)).content.decode()
    assert "Contrat de travail ST" in html and "Inscription ADUM" in html


def test_progress_counts_the_items_the_user_sees(team: SimpleNamespace) -> None:
    put(_item(team, "funding_proof"), S.VALIDATED, team.student)  # CIFRE
    put(_item(team, "adum_enrollment"), S.VALIDATED, team.student)  # ACADEMIC
    tutor = _signed_in(team.tutor).get(_url("file", team)).context["progress"]
    director = _signed_in(team.director).get(_url("file", team)).context["progress"]
    assert (tutor.validated, tutor.total) == (1, 4)
    assert (director.validated, director.total) == (2, 10)


@pytest.mark.parametrize("role", ROLES)
def test_item_page_by_role(team: SimpleNamespace, role: str) -> None:
    cifre = _item(team, "collaboration_contract")
    response = _signed_in(getattr(team, role)).get(_url("item", team, cifre))
    assert response.status_code == (200 if role in READERS else 403)


def test_an_academic_item_is_refused_to_the_tutor_and_audited(team: SimpleNamespace) -> None:
    response = _signed_in(team.tutor).get(_url("item", team, _item(team, "adum_enrollment")))
    assert response.status_code == 403
    assert AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED", actor=team.tutor).exists()


def test_an_item_of_another_thesis_is_refused(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    services.seed_items(other)
    foreign = RequiredItem.objects.get(thesis=other, code="adum_enrollment")
    response = _signed_in(team.director).get(_url("item", team, foreign))
    assert response.status_code == 403


def test_deposit_warnings_are_shown(team: SimpleNamespace) -> None:
    page = _signed_in(team.student).get(_url("item", team, _item(team, "funding_proof")))
    text = html_lib.unescape(page.content.decode())
    assert "salaire ≥ seuil de l'école doctorale, sans le montant" in text
    page = _signed_in(team.student).get(_url("item", team, _item(team, "fsd_zrr_clearance")))
    assert "jamais le dossier de demande" in html_lib.unescape(page.content.decode())


def test_attaching_an_existing_document_warns_about_the_documents_page(
    team: SimpleNamespace,
) -> None:
    vault.store(
        thesis=team.thesis,
        upload=_pdf(),
        title="Attestation fictive",
        level=L.INTERNAL_LIRMM,
        user=team.student,
    )
    html = _signed_in(team.student).get(_url("item", team, _item(team, "cvec_certificate")))
    content = html.content.decode()
    assert "Attestation fictive" in content  # offered as an existing document
    assert "ne pourront plus le lire depuis la page Documents" in content


# --------------------------------------------------------------------------- student actions


def test_student_submits_a_file(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _submit(team, item)
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == S.SUBMITTED
    assert item.document is not None
    assert item.document.confidentiality == L.INTERNAL_LIRMM


def test_submit_with_htmx_returns_the_row_fragment(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _signed_in(team.student).post(
        _url("submit", team, item), {"file": _pdf()}, headers=HX
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert f'id="item-{item.pk}"' in html
    assert "<html" not in html  # a fragment, not a page


@pytest.mark.parametrize("role", ["director", "tutor", "co_supervisor", "csi", "guest"])
def test_only_the_student_submits(team: SimpleNamespace, role: str) -> None:
    item = _item(team, "collaboration_contract")
    response = _signed_in(getattr(team, role)).post(_url("submit", team, item), {"file": _pdf()})
    assert response.status_code == 403
    assert RequiredItem.objects.get(pk=item.pk).status == S.TODO


def test_attach_an_existing_document(team: SimpleNamespace) -> None:
    document = vault.store(
        thesis=team.thesis,
        upload=_pdf(),
        title="Attestation fictive",
        level=L.INTERNAL_LIRMM,
        user=team.student,
    )
    item = _item(team, "cvec_certificate")
    response = _signed_in(team.student).post(
        _url("submit", team, item), {"document": str(document.pk)}
    )
    assert response.status_code == 302
    assert RequiredItem.objects.get(pk=item.pk).document_id == document.pk


def test_missing_file_is_a_400_with_a_message(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    response = _signed_in(team.student).post(_url("submit", team, item), {}, headers=HX)
    assert response.status_code == 400
    assert "pièce jointe" in response.content.decode()


def test_withdraw(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")
    _submit(team, item)
    assert _signed_in(team.director).post(_url("withdraw", team, item)).status_code == 403
    assert _signed_in(team.student).post(_url("withdraw", team, item)).status_code == 302
    assert RequiredItem.objects.get(pk=item.pk).status == S.TODO


def test_due_date(team: SimpleNamespace) -> None:
    """Revised 2026-11-14: a validator of the item changes it (second factor), the
    student no longer does (they propose); audited with from and to."""
    item = _item(team, "adum_enrollment")
    before = item.due_date
    data = {"due_date": "2027-02-01"}
    assert _signed_in(team.student).post(_url("due_date", team, item), data).status_code == 403
    assert _mfa(team.director).post(_url("due_date", team, item), data).status_code == 302
    assert RequiredItem.objects.get(pk=item.pk).due_date == dt.date(2027, 2, 1)
    event = AuditEvent.objects.get(action="ADMIN_ITEM_UPDATED")
    assert before is not None
    assert event.details_json == {
        "field": "due_date",
        "from": before.isoformat(),
        "to": "2027-02-01",
    }


# --------------------------------------------------------------------------- decisions


DECISIONS = ("approve", "reject", "reopen")


@pytest.mark.parametrize("name", DECISIONS)
def test_decision_views_put_reauth_after_the_guard_and_before_view(name: str) -> None:
    """The M2 defect: RecentAuthRequiredMixin AFTER the thesis guard, BEFORE View."""
    from django.views import View

    view_class = {
        "approve": views.ApproveView,
        "reject": views.RejectView,
        "reopen": views.ReopenView,
    }[name]
    mro = view_class.__mro__
    assert mro.index(ThesisPermissionRequiredMixin) < mro.index(RecentAuthRequiredMixin)
    assert mro.index(RecentAuthRequiredMixin) < mro.index(View)


def _ready_for(team: SimpleNamespace, name: str) -> RequiredItem:
    item = _item(team, "adum_enrollment")
    _submit(team, item)
    item.refresh_from_db()
    if name == "reopen":
        services.approve(item=item, user=team.director)
        item.refresh_from_db()
    return item


def _data(name: str) -> dict[str, str]:
    return {} if name == "approve" else {"reason": Reason.ILLEGIBLE.value, "comment": ""}


@pytest.mark.parametrize("name", DECISIONS)
def test_without_a_recent_second_factor_nothing_happens(team: SimpleNamespace, name: str) -> None:
    item = _ready_for(team, name)
    client = _signed_in(team.director)  # signing in is itself audited: count after it
    before = (item.status, ItemApproval.objects.count(), AuditEvent.objects.count())

    response = client.post(_url(name, team, item), _data(name))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))

    response = client.post(_url(name, team, item), _data(name), headers=HX)
    assert response.status_code == 200
    assert response["HX-Redirect"].startswith(reverse("mfa_reauthenticate"))

    item.refresh_from_db()
    assert (item.status, ItemApproval.objects.count(), AuditEvent.objects.count()) == before


@pytest.mark.parametrize("name", DECISIONS)
def test_a_refused_role_gets_403_before_any_reauth(team: SimpleNamespace, name: str) -> None:
    item = _ready_for(team, name)
    response = _signed_in(team.co_supervisor).post(_url(name, team, item), _data(name))
    assert response.status_code == 403  # not a redirect to the re-authentication page


@pytest.mark.parametrize(
    ("name", "expected"),
    [("approve", S.VALIDATED), ("reject", S.REJECTED), ("reopen", S.TODO)],
)
def test_decisions_with_a_recent_second_factor(
    team: SimpleNamespace, name: str, expected: str
) -> None:
    item = _ready_for(team, name)
    response = _mfa(team.director).post(_url(name, team, item), _data(name), headers=HX)
    assert response.status_code == 200
    assert RequiredItem.objects.get(pk=item.pk).status == expected


def test_the_validator_rule_holds_through_the_view(team: SimpleNamespace) -> None:
    """The director reads the CIFRE part but does not validate the tutor's item."""
    item = _item(team, "employment_contract")
    _submit(team, item, file=_pdf())
    response = _mfa(team.director).post(_url("approve", team, item))
    assert response.status_code == 403
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_reject_without_a_reason_is_a_400(team: SimpleNamespace) -> None:
    item = _ready_for(team, "reject")
    response = _mfa(team.director).post(_url("reject", team, item), {"reason": ""}, headers=HX)
    assert response.status_code == 400
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_stale_submission_is_a_409_with_a_clear_message(team: SimpleNamespace) -> None:
    item = _ready_for(team, "approve")
    assert item.document is not None
    vault.add_version(document=item.document, upload=_pdf("v2"), user=team.student)
    response = _mfa(team.director).post(_url("approve", team, item), headers=HX)
    assert response.status_code == 409
    assert "Le document a changé depuis le dépôt" in response.content.decode()
    assert RequiredItem.objects.get(pk=item.pk).status == S.SUBMITTED


def test_invalid_transition_is_a_409_asking_to_reload(team: SimpleNamespace) -> None:
    item = _item(team, "adum_enrollment")  # TODO: nothing to approve
    response = _mfa(team.director).post(_url("approve", team, item), headers=HX)
    assert response.status_code == 409
    assert "La pièce a changé, rechargez la page." in response.content.decode()


def test_htmx_swaps_400_and_409_responses() -> None:
    """Without responseHandling, htmx 2 would drop the error fragments silently."""
    html = Client().get(reverse("account_login")).content.decode()
    assert '"code":"40[09]","swap":true' in html


# --------------------------------------------------------------------------- custom items


def test_custom_item_form_warns_about_the_clear_label(team: SimpleNamespace) -> None:
    response = _signed_in(team.student).get(_url("new_item", team))
    assert response.status_code == 200
    assert "libellé n'est pas chiffré" in response.content.decode()


def test_custom_item_creation(team: SimpleNamespace) -> None:
    data = {
        "label": "Renouvellement fictif",
        "section": "ACADEMIC",
        "validators": "DIRECTOR",
        "default_level": "INTERNAL_LIRMM",
        "mandatory": "on",
        "due_date": "2027-06-01",
    }
    assert _signed_in(team.director).post(_url("new_item", team), data).status_code == 403
    response = _signed_in(team.student).post(_url("new_item", team), data)
    assert response.status_code == 302
    item = RequiredItem.objects.get(thesis=team.thesis, label="Renouvellement fictif")
    assert item.code.startswith("custom-")
    assert item.validator_roles == ["DIRECTOR"]
    assert item.mandatory and not item.attachment_required
    event = AuditEvent.objects.get(action="ADMIN_ITEM_CREATED")
    assert "Renouvellement" not in str(event.details_json)
    assert event.details_json["item"] == item.code


@pytest.mark.parametrize(
    ("validators", "section", "level"),
    [
        ("INDUSTRIAL_TUTOR", "ACADEMIC", "CONFIDENTIAL"),  # the tutor cannot see ACADEMIC
        ("DIRECTOR", "CIFRE", "INTERNAL_ST"),  # the director cannot read INTERNAL_ST
        ("CO_SUPERVISOR", "ACADEMIC", "INTERNAL_LIRMM"),  # never a validator
    ],
)
def test_custom_item_refuses_blind_validators(
    team: SimpleNamespace, validators: str, section: str, level: str
) -> None:
    data = {
        "label": "Pièce fictive",
        "section": section,
        "validators": validators,
        "default_level": level,
    }
    response = _signed_in(team.student).post(_url("new_item", team), data)
    assert response.status_code == 400
    assert not RequiredItem.objects.filter(label="Pièce fictive").exists()


# --------------------------------------------------------------------------- dashboard


@pytest.mark.parametrize("role", ["student", "director", "tutor", "co_supervisor"])
def test_dashboard_block_under_cifre_file_read(team: SimpleNamespace, role: str) -> None:
    put(_item(team, "funding_proof"), S.VALIDATED, team.student)
    response = _signed_in(getattr(team, role)).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    html = response.content.decode()
    assert ('data-block="admin_file"' in html) == (role in READERS)
    if role == "tutor":
        assert "1 / 4" in html
    if role == "director":
        assert "1 / 10" in html


def test_incomplete_banner_on_an_active_thesis(team: SimpleNamespace) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.ACTIVE)
    html = _signed_in(team.director).get(_url("file", team)).content.decode()
    assert "Dossier incomplet" in html


def test_a_custom_item_defaults_to_confidential(team: SimpleNamespace) -> None:
    """Acceptance: a custom item's attachments default to CONFIDENTIAL."""
    response = _signed_in(team.student).get(_url("new_item", team))
    assert response.context["form"]["default_level"].value() == L.CONFIDENTIAL
