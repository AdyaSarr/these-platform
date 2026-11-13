"""M3 security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- 1 separation of duties: apps/admin_file/tests/test_validation.py
  ::test_one_person_with_both_roles_cannot_validate_the_collaboration_contract_alone,
  ::test_the_submitter_never_validates_their_cycle (approve AND reject);
  apps/admin_file/tests/test_denial_persistence.py (already_approved journalled).
- 4 content changed after submission: apps/admin_file/tests/test_views.py
  ::test_stale_submission_is_a_409_with_a_clear_message.
- 5 state machine: apps/admin_file/tests/test_transitions.py::test_every_pair (20 pairs);
  apps/admin_file/tests/test_validation.py::test_a_finished_thesis_freezes_its_file.
- 6 activation: apps/admin_file/tests/test_activation.py
  ::test_activation_is_atomic_with_the_last_validation,
  ::test_racing_last_validations_activate_exactly_once.
- 8 reminders: apps/admin_file/tests/test_reminders.py (link only, submitter and
  inactive members, concurrent runs, time changes).
- 10 CSP: apps/admin_file/tests/test_csp_pages.py (pages, fragments, 400 and 409).
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from types import SimpleNamespace
from typing import Any

import pytest
import time_machine
from django.contrib import admin
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory
from apps.accounts.tests.helpers import password_reauthenticate
from apps.admin_file import reminders, services
from apps.admin_file.models import ItemApproval, ItemStatus, Reason, RequiredItem
from apps.core.confidentiality import ConfidentialityLevel as L
from apps.vault import services as vault
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)

PASSWORD = secrets.token_urlsafe(16)


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
    )
    services.seed_items(thesis)
    return team


def _client(user: Any) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _item(thesis: Thesis, code: str) -> RequiredItem:
    return RequiredItem.objects.get(thesis=thesis, code=code)


def _item_url(thesis: Thesis, item: RequiredItem, name: str = "item") -> str:
    return reverse(f"admin_file:{name}", kwargs={"thesis_id": thesis.pk, "item_id": item.pk})


def _pdf() -> SimpleUploadedFile:
    return SimpleUploadedFile("piece.pdf", b"%PDF-1.7\n%Piece fictive\n%%EOF\n")


def _denials(user: Any) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action="ADMIN_ITEM_ACCESS_DENIED", actor=user).order_by("seq")
    )


# --------------------------------------------------------------------------- 2 IDOR


def test_foreign_and_invisible_items_get_the_same_audited_403(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    services.seed_items(other)
    foreign = _item(other, "employment_contract")  # CIFRE: the tutor would see it at home
    invisible = _item(team.thesis, "adum_enrollment")  # ACADEMIC: hidden from the tutor
    client = _client(team.tutor)

    through_my_url = client.get(_item_url(team.thesis, foreign))
    hidden = client.get(_item_url(team.thesis, invisible))
    unknown = client.get(
        reverse(
            "admin_file:item",
            kwargs={"thesis_id": team.thesis.pk, "item_id": "00000000-0000-4000-8000-000000000000"},
        )
    )

    assert through_my_url.status_code == hidden.status_code == unknown.status_code == 403
    assert through_my_url.content == hidden.content == unknown.content
    events = _denials(team.tutor)
    assert len(events) == 3
    assert all(e.thesis_id == team.thesis.pk for e in events)  # never the other thesis
    assert all(e.details_json["reason"] == "not_visible" for e in events)
    assert events[1].object_id == str(invisible.pk)


def test_a_member_of_two_theses_cannot_cross_them_through_the_url(team: SimpleNamespace) -> None:
    other = ThesisFactory.create()
    services.seed_items(other)
    MembershipFactory.create(thesis=other, role=Role.DIRECTOR, user=team.director)
    foreign = _item(other, "adum_enrollment")
    client = _client(team.director)
    assert client.get(_item_url(other, foreign)).status_code == 200  # through its own thesis
    assert client.get(_item_url(team.thesis, foreign)).status_code == 403  # never through mine
    for name in ("submit", "withdraw", "due_date"):
        assert _client(team.student).post(_item_url(team.thesis, foreign, name)).status_code == 403
    assert RequiredItem.objects.get(pk=foreign.pk).status == ItemStatus.TODO


# --------------------------------------------------------------------------- 3 section restriction


def _download(team: SimpleNamespace, user: Any, document: Any) -> int:
    version = document.versions.get(version=1)
    url = reverse("vault:download", kwargs={"thesis_id": team.thesis.pk, "version_id": version.pk})
    return _client(user).get(url).status_code


def _listed(team: SimpleNamespace, user: Any) -> set[Any]:
    url = reverse("vault:documents", kwargs={"thesis_id": team.thesis.pk})
    return {d.pk for d in _client(user).get(url).context["documents"]}


def test_neither_a_grant_nor_a_lower_level_opens_an_attachment(team: SimpleNamespace) -> None:
    contract = _item(team.thesis, "collaboration_contract")  # CIFRE, CONFIDENTIAL
    services.submit(item=contract, user=team.student, upload=_pdf())
    contract.refresh_from_db()
    assert contract.document is not None
    vault.grant_access(document=contract.document, grantee=team.co_supervisor, user=team.student)
    assert contract.document.pk not in _listed(team, team.co_supervisor)
    assert _download(team, team.co_supervisor, contract.document) == 403

    cvec = _item(team.thesis, "cvec_certificate")  # ACADEMIC, INTERNAL_LIRMM
    services.submit(item=cvec, user=team.student, upload=_pdf())
    cvec.refresh_from_db()
    assert cvec.document is not None
    vault.change_level(document=cvec.document, level=L.PUBLIC, user=team.student)
    for outsider in (team.co_supervisor, team.tutor):
        assert cvec.document.pk not in _listed(team, outsider)
        assert _download(team, outsider, cvec.document) == 403
    assert _download(team, team.director, cvec.document) == 200


# --------------------------------------------------------------------------- 7 clear text


def test_custom_labels_and_reject_comments_stay_out_of_logs_mails_and_str(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    label, comment = "Renouvellement fictif XYZ", "Commentaire fictif QWERTY"
    custom = services.create_custom_item(
        thesis=team.thesis,
        user=team.student,
        label=label,
        section="ACADEMIC",  # type: ignore[arg-type]
        validator_roles=[Role.DIRECTOR],
        default_level=L.CONFIDENTIAL,
        mandatory=True,
        attachment_required=False,
        due_date=dt.date(2026, 12, 10),
    )
    services.submit(item=custom, user=team.student)
    services.reject(item=custom, user=team.director, reason=Reason.OTHER, comment=comment)
    caplog.clear()
    caplog.set_level(logging.DEBUG)  # every logger, Celery included
    with time_machine.travel(dt.datetime(2026, 12, 1, 9, 0, tzinfo=dt.UTC), tick=False):
        reminders.run()  # REJECTED: the student is reminded (catalogue items too: overdue)
    assert mail.outbox
    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    audit_text = " ".join(
        str(e.details_json) + e.object_type + e.object_id for e in AuditEvent.objects.all()
    )
    mails = " ".join(str(m.subject) + str(m.body) for m in mail.outbox)
    for secret in (label, comment):
        assert secret not in logged, secret
        assert secret not in audit_text, secret
        assert secret not in mails, secret
        assert secret not in str(custom), secret
        for approval in ItemApproval.objects.all():
            assert secret not in str(approval)


def test_no_model_of_the_file_is_exposed_in_the_django_admin() -> None:
    """Supervisors never use the admin: nothing of the file (labels, comments) is shown."""
    registered = {model._meta.app_label for model in admin.site._registry}
    assert "admin_file" not in registered


# --------------------------------------------------------------------------- 9 re-authentication


@pytest.mark.parametrize("name", ["approve", "reject", "reopen"])
def test_a_password_alone_does_not_allow_a_decision(team: SimpleNamespace, name: str) -> None:
    item = _item(team.thesis, "adum_enrollment")
    services.submit(item=item, user=team.student, upload=_pdf())
    if name == "reopen":
        services.approve(item=item, user=team.director)
    item.refresh_from_db()
    before = item.status
    client = _client(team.director)
    password_reauthenticate(client, PASSWORD)  # recent PASSWORD, no second factor
    data = {} if name == "approve" else {"reason": Reason.ILLEGIBLE.value}
    response = client.post(_item_url(team.thesis, item, name), data)
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("mfa_reauthenticate"))
    assert RequiredItem.objects.get(pk=item.pk).status == before
