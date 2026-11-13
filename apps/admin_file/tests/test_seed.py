"""Creation of the required items: by create_thesis for a new thesis, by the
idempotent `manage.py admin_file_seed` for an existing one (no data migration:
no thesis exists when migrations run)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io

import pytest
from django.core.management import CommandError, call_command

from apps.accounts.models import Thesis
from apps.accounts.tests.factories import ThesisFactory
from apps.admin_file import catalog, services
from apps.admin_file.catalog import CATALOG
from apps.admin_file.models import RequiredItem
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

CODES = [i.code for i in CATALOG]


def _seeded() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action="ADMIN_FILE_SEEDED").order_by("seq"))


def test_seed_creates_every_catalogue_item() -> None:
    thesis = ThesisFactory.create(
        cifre_start_date=dt.date(2026, 3, 1), first_enrollment_date=dt.date(2026, 3, 15)
    )
    assert services.seed_items(thesis) == CODES
    items = {i.code: i for i in RequiredItem.objects.filter(thesis=thesis)}
    assert set(items) == set(CODES)
    for entry in CATALOG:
        item = items[entry.code]
        assert item.status == RequiredItem.Status.TODO
        assert item.label == ""  # shown from the catalogue, not stored
        assert item.section == entry.section
        assert item.validator_roles == list(entry.validator_roles)
        assert item.default_level == entry.default_level
        assert (item.mandatory, item.attachment_required) == (
            entry.mandatory,
            entry.attachment_required,
        )
    assert items["employment_contract"].due_date == dt.date(2026, 3, 1)
    assert items["collaboration_contract"].due_date == dt.date(2026, 5, 30)  # start + 90 d
    assert items["training_plan"].due_date == dt.date(2026, 9, 11)  # enrolment + 180 d


def test_seed_is_audited_with_codes_only() -> None:
    thesis = ThesisFactory.create()
    services.seed_items(thesis)
    [event] = _seeded()
    assert event.thesis_id == thesis.pk
    assert event.actor is None
    assert event.details_json == {"items": CODES}
    for entry in CATALOG:  # never a label
        assert str(entry.label) not in str(event.details_json)


def test_seed_is_idempotent() -> None:
    thesis = ThesisFactory.create()
    services.seed_items(thesis)
    assert services.seed_items(thesis) == []
    assert RequiredItem.objects.filter(thesis=thesis).count() == len(CODES)
    assert len(_seeded()) == 1  # nothing created, nothing audited


def test_seed_never_overwrites_an_existing_item() -> None:
    thesis = ThesisFactory.create()
    services.seed_items(thesis)
    RequiredItem.objects.filter(thesis=thesis, code="adum_enrollment").update(
        due_date=dt.date(2030, 1, 1), mandatory=False
    )
    services.seed_items(thesis)
    item = RequiredItem.objects.get(thesis=thesis, code="adum_enrollment")
    assert (item.due_date, item.mandatory) == (dt.date(2030, 1, 1), False)


def test_seed_adds_a_code_added_to_the_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    thesis = ThesisFactory.create()
    services.seed_items(thesis)
    extra = dataclasses.replace(CATALOG[-1], code="fictitious_new_item")
    monkeypatch.setattr(catalog, "CATALOG", (*CATALOG, extra))
    assert services.seed_items(thesis) == ["fictitious_new_item"]


def test_seed_only_touches_its_thesis() -> None:
    first, second = ThesisFactory.create(), ThesisFactory.create()
    services.seed_items(first)
    assert not RequiredItem.objects.filter(thesis=second).exists()


# --------------------------------------------------------------------------- create_thesis

ARGS = [
    "--title=Thèse fictive",
    "--cifre-start=2026-03-01",
    "--first-enrollment=2026-03-15",
    "--planned-end=2029-02-28",
    "--student-email=doctorant.fictif@example.org",
]


def test_create_thesis_seeds_the_administrative_file() -> None:
    call_command("create_thesis", *ARGS, stdout=io.StringIO())
    thesis = Thesis.objects.get()
    assert sorted(RequiredItem.objects.filter(thesis=thesis).values_list("code", flat=True)) == (
        sorted(CODES)
    )
    actions = list(AuditEvent.objects.order_by("seq").values_list("action", flat=True))
    assert actions.index("THESIS_CREATED") < actions.index("ADMIN_FILE_SEEDED")


def test_failed_create_thesis_leaves_no_item() -> None:
    args = [*ARGS[:-1], "--student-email=not-an-address"]
    with pytest.raises(CommandError):
        call_command("create_thesis", *args, stdout=io.StringIO())
    assert not RequiredItem.objects.exists()


# --------------------------------------------------------------------------- admin_file_seed


def test_command_seeds_one_thesis_and_is_idempotent() -> None:
    thesis = ThesisFactory.create()
    out = io.StringIO()
    call_command("admin_file_seed", f"--thesis={thesis.pk}", stdout=out)
    assert RequiredItem.objects.filter(thesis=thesis).count() == len(CODES)
    assert "10" in out.getvalue()
    out = io.StringIO()
    call_command("admin_file_seed", f"--thesis={thesis.pk}", stdout=out)
    assert RequiredItem.objects.filter(thesis=thesis).count() == len(CODES)
    assert "0" in out.getvalue()


def test_command_all_skips_finished_theses() -> None:
    active = ThesisFactory.create(status=Thesis.Status.ACTIVE)
    setup = ThesisFactory.create(status=Thesis.Status.SETUP)
    closed = ThesisFactory.create(status=Thesis.Status.CLOSED)
    defended = ThesisFactory.create(status=Thesis.Status.DEFENDED)
    call_command("admin_file_seed", "--all", stdout=io.StringIO())
    seeded = set(RequiredItem.objects.values_list("thesis_id", flat=True))
    assert seeded == {active.pk, setup.pk}
    assert closed.pk not in seeded and defended.pk not in seeded


def test_command_refuses_a_finished_or_unknown_thesis() -> None:
    closed = ThesisFactory.create(status=Thesis.Status.CLOSED)
    with pytest.raises(CommandError):
        call_command("admin_file_seed", f"--thesis={closed.pk}", stdout=io.StringIO())
    with pytest.raises(CommandError):
        call_command(
            "admin_file_seed", "--thesis=00000000-0000-4000-8000-000000000000", stdout=io.StringIO()
        )
    assert not RequiredItem.objects.exists()


def test_command_needs_exactly_one_target() -> None:
    with pytest.raises(CommandError):
        call_command("admin_file_seed", stdout=io.StringIO())
