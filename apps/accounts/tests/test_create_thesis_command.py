"""manage.py create_thesis: the only way in for the first user (no public sign-up)."""

import io
import re
from typing import Any

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from apps.accounts.models import Invitation, Thesis
from apps.accounts.roles import Role
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

ARGS = [
    "--title=Thèse fictive",
    "--cifre-start=2026-03-01",
    "--first-enrollment=2026-03-15",
    "--planned-end=2029-02-28",
    "--student-email=Doctorant.Fictif@Example.org",
]


def test_creates_thesis_and_sends_student_invitation() -> None:
    out = io.StringIO()
    call_command("create_thesis", *ARGS, stdout=out)
    thesis = Thesis.objects.get()
    assert thesis.status == Thesis.Status.SETUP
    inv = Invitation.objects.get()
    assert (inv.thesis, inv.role, inv.email) == (
        thesis,
        Role.PHD_STUDENT,
        "doctorant.fictif@example.org",
    )
    assert inv.invited_by is None
    assert inv.email_status == Invitation.EmailStatus.SENT
    assert len(mail.outbox) == 1
    match = re.search(r"/invitation/([A-Za-z0-9_-]{43})/", str(mail.outbox[0].body))
    assert match is not None
    token = match.group(1)
    assert token not in out.getvalue()  # never printed to the terminal
    assert AuditEvent.objects.filter(action="THESIS_CREATED", actor=None).exists()


def test_rejects_end_before_start() -> None:
    args = [a if not a.startswith("--planned-end") else "--planned-end=2025-01-01" for a in ARGS]
    with pytest.raises(CommandError):
        call_command("create_thesis", *args, stdout=io.StringIO())
    assert not Thesis.objects.exists()


@pytest.mark.without_vault_keys
def test_refuses_to_run_before_vault_init() -> None:
    with pytest.raises(CommandError, match="vault_init"):
        call_command("create_thesis", *ARGS, stdout=io.StringIO())
    assert not Thesis.objects.exists()


def test_refuses_to_run_without_the_storage_directory(settings: Any, tmp_path: Any) -> None:
    settings.VAULT_STORAGE_DIR = str(tmp_path / "absent")
    with pytest.raises(CommandError, match="vault_init"):
        call_command("create_thesis", *ARGS, stdout=io.StringIO())
    assert not Thesis.objects.exists()
