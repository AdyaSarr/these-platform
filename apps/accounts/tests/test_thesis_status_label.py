"""The ACTIVE thesis status reads « En cours », through its own translation context
("thesis status"): no other "Active" of the interface changes."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import Client
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import Thesis
from apps.accounts.roles import Role
from apps.accounts.tests.factories import MembershipFactory, ThesisFactory

PO = Path(__file__).resolve().parents[3] / "locale" / "fr" / "LC_MESSAGES" / "django.po"


def test_the_labels_in_french() -> None:
    with translation.override("fr"):
        assert str(Thesis.Status.SETUP.label) == "En préparation"
        assert str(Thesis.Status.ACTIVE.label) == "En cours"
        assert str(Thesis.Status.DEFENDED.label) == "Soutenue"
        assert str(Thesis.Status.CLOSED.label) == "Clôturée"


def test_no_other_active_changes() -> None:
    """Only the "thesis status" context reads « En cours »: a plain "Active" (or the
    lower-case "active" of Django's user model) keeps its own translation."""
    with translation.override("fr"):
        assert translation.pgettext("thesis status", "Active") == "En cours"
        assert translation.gettext("Active") == "Active"
        assert translation.gettext("active") == "actif"


def test_the_catalogue_holds_en_cours_for_the_thesis_status_only() -> None:
    """« En cours » is the thesis status (context) and, already before, an objective's
    "In progress" (M4): nothing else, and no plain "Active" reads « En cours »."""
    blocks = [b for b in PO.read_text().split("\n\n") if not b.lstrip().startswith("#~")]

    def key(block: str) -> tuple[str | None, str]:
        context = re.search(r'^msgctxt "(.*)"$', block, re.M)
        msgid = re.search(r'^msgid "(.*)"$', block, re.M)
        assert msgid is not None
        return (context.group(1) if context else None, msgid.group(1))

    en_cours = {key(b) for b in blocks if re.search(r'^msgstr "En cours"$', b, re.M)}
    assert en_cours == {("thesis status", "Active"), (None, "In progress")}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "label"),
    [(Thesis.Status.SETUP, "En préparation"), (Thesis.Status.ACTIVE, "En cours")],
)
def test_the_dashboard_shows_the_label(status: str, label: str) -> None:
    thesis = ThesisFactory.create(status=status)
    student = MembershipFactory.create(thesis=thesis, role=Role.PHD_STUDENT).user
    client = Client()
    client.force_login(student)
    page = html.unescape(
        client.get(reverse("accounts:dashboard", kwargs={"thesis_id": thesis.pk})).content.decode()
    )
    status_line = re.search(r'<p class="muted">\s*(.*?) ·', page, re.S)
    assert status_line is not None and status_line.group(1).strip() == label
    if status == Thesis.Status.SETUP:
        assert "La thèse passera « En cours » quand" in re.sub(r"\s+", " ", page)


@pytest.mark.django_db
def test_no_migration_is_needed() -> None:
    """Migrations store a choice's SOURCE label ("Active"), which the translation
    context does not change: no migration, no data or schema touched."""
    from io import StringIO

    from django.core.management import call_command

    call_command("makemigrations", "accounts", "--check", "--dry-run", stdout=StringIO())
    state = MigrationLoader(connection).project_state()
    choices = dict(state.models["accounts", "thesis"].fields["status"].choices)
    assert str(choices["ACTIVE"]) == "Active"
