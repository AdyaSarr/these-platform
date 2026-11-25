"""Teaching and outreach (M9b): what counts in clear (type, hours, dates, level),
the free texts sealed; read by level by the director, the LIRMM co-supervisor, the
tutor and the company co-supervisor, never by the CSI member nor the guest;
written by the student; the level changed apart, with a second factor."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.accounts.models import Thesis
from apps.activity.models import Event
from apps.portfolio import services
from apps.portfolio.models import Outreach, Teaching
from apps.portfolio.tests import helpers as h
from apps.vault.errors import ReauthenticationRequiredError
from apps.vault.models import AuditEvent, SealedText

pytestmark = pytest.mark.django_db(transaction=True)
L = h.L


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _get(team: SimpleNamespace, attr: str, path: str) -> Any:
    return h.client_for(getattr(team, attr)).get(path)


# --------------------------------------------------------------------------- teaching


def test_create_read_back_and_totals(team: SimpleNamespace) -> None:
    h.teaching(team, course="Cours fictif A", kind="TD", hours="12,5")
    h.teaching(team, course="Cours fictif B", kind="TP", hours="8")
    h.teaching(team, course="À venir fictif", kind="TD", hours="20", start=h.D(2027, 2, 1))
    listing = services.teaching_list(team.student, team.thesis)
    assert {r.texts["course"] for r in listing.done} == {"Cours fictif A", "Cours fictif B"}
    assert [r.texts["course"] for r in listing.upcoming] == ["À venir fictif"]
    totals = {t.kind: t.hours for t in listing.totals}
    assert totals["TD"] == Decimal("12.5") and totals["TP"] == Decimal("8")
    assert listing.total_hours == Decimal("20.5") and listing.partial is False
    event = AuditEvent.objects.filter(action="TEACHING_CREATED").order_by("seq").first()
    assert event is not None and set(event.details_json) >= {"kind", "level"}


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [("hours", "0", "hours"), ("hours", "1,25", "hours"), ("kind", "LECTURE", "kind"),
     ("course", "", "course"), ("course", "a\x00b", "course")],
)  # fmt: skip
def test_strict_validation(team: SimpleNamespace, field: str, value: str, code: str) -> None:
    kwargs: dict[str, Any] = {field: value}
    with pytest.raises(ValidationError) as exc:
        h.teaching(team, **kwargs)
    assert exc.value.code == code
    assert not Teaching.objects.exists()


def test_dates_inside_the_thesis(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        h.teaching(team, start=h.D(2027, 2, 1), end=h.D(2027, 1, 1))
    assert exc.value.code == "date"
    with pytest.raises(ValidationError):
        h.teaching(team, start=h.D(2020, 1, 1))


@pytest.mark.parametrize("attr", h.ACTIVITY_READERS)
def test_the_readers(team: SimpleNamespace, attr: str) -> None:
    shown = h.teaching(team, course="TEMOIN-FUITE-COURS-PUBLIC")
    assert _get(team, attr, h.url(team, "teachings")).status_code == 200
    response = _get(team, attr, h.url(team, "teaching", teaching_id=shown.pk))
    assert response.status_code == 200
    assert "TEMOIN-FUITE-COURS-PUBLIC" in response.content.decode()


@pytest.mark.parametrize("attr", h.ACTIVITY_NOT_READERS)
def test_the_csi_member_and_the_guest_are_refused(team: SimpleNamespace, attr: str) -> None:
    shown = h.teaching(team)
    action = h.outreach(team)
    for path in (
        h.url(team, "teachings"),
        h.url(team, "teaching", teaching_id=shown.pk),
        h.url(team, "outreach_list"),
        h.url(team, "outreach", outreach_id=action.pk),
    ):
        assert _get(team, attr, path).status_code == 403, path
    with pytest.raises(PermissionDenied):
        services.teaching_list(getattr(team, attr), team.thesis)


@pytest.mark.parametrize(
    ("level", "readers"),
    [(L.INTERNAL_LIRMM, {"student", "director", "co_supervisor"}),
     (L.INTERNAL_ST, {"student", "tutor", "st_co_supervisor"}),
     (L.CONFIDENTIAL, {"student", "director", "tutor"})],
)  # fmt: skip
def test_by_level(team: SimpleNamespace, level: L, readers: set[str]) -> None:
    hidden = h.teaching(team, level)
    for attr in h.ACTIVITY_READERS:
        user = getattr(team, attr)
        listed = [r.teaching.pk for r in services.teaching_list(user, team.thesis).done]
        assert (listed == [hidden.pk]) is (attr in readers), attr
        status = _get(team, attr, h.url(team, "teaching", teaching_id=hidden.pk)).status_code
        assert status == (200 if attr in readers else 403), attr


def test_the_mention_follows_the_roles(team: SimpleNamespace) -> None:
    mention = "Calculé sur les enseignements que vous pouvez lire"
    assert mention not in _get(team, "student", h.url(team, "teachings")).content.decode()
    for attr in ("director", "co_supervisor", "tutor", "st_co_supervisor"):
        assert mention in _get(team, attr, h.url(team, "teachings")).content.decode(), attr


def test_the_notes_are_the_students(team: SimpleNamespace) -> None:
    shown = h.teaching(team, notes="TEMOIN-FUITE-NOTE-COURS")
    for attr in ("director", "tutor", "st_co_supervisor"):
        body = _get(team, attr, h.url(team, "teaching", teaching_id=shown.pk)).content.decode()
        assert "TEMOIN-FUITE-NOTE-COURS" not in body, attr
        assert services.teaching_detail(getattr(team, attr), shown).texts["notes"] == ""
    assert (
        "TEMOIN-FUITE-NOTE-COURS"
        in _get(team, "student", h.url(team, "teaching", teaching_id=shown.pk)).content.decode()
    )


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_only_the_student_writes(team: SimpleNamespace, attr: str) -> None:
    shown = h.teaching(team)
    user = getattr(team, attr)
    with pytest.raises(PermissionDenied):
        services.create_teaching(thesis=team.thesis, user=user, course="Cours fictif", kind="TD",
                                 hours="2", start_date=h.START, end_date=h.START)  # fmt: skip
    with pytest.raises(PermissionDenied):
        services.change_teaching_level(teaching=shown, user=user, level=L.INTERNAL_ST,
                                       recent_mfa=True)  # fmt: skip
    for name in ("new_teaching", "new_outreach"):
        assert _get(team, attr, h.url(team, name)).status_code == 403


def test_the_level_change_needs_a_second_factor_and_reseals(team: SimpleNamespace) -> None:
    shown = h.teaching(team, course="Cours fictif", notes="Note fictive")
    with pytest.raises(ReauthenticationRequiredError):
        services.change_teaching_level(teaching=shown, user=team.student, level=L.INTERNAL_ST,
                                       recent_mfa=False)  # fmt: skip
    services.change_teaching_level(teaching=shown, user=team.student, level=L.INTERNAL_ST,
                                   recent_mfa=True)  # fmt: skip
    levels = set(SealedText.objects.filter(object_id=shown.pk).values_list("level", flat=True))
    assert levels == {"INTERNAL_ST"}
    assert services.teaching_list(team.director, team.thesis).done == []
    assert services.teaching_list(team.st_co_supervisor, team.thesis).done[0].texts["course"] == (
        "Cours fictif"
    )


def test_an_update_journals_the_changed_fields_only(team: SimpleNamespace) -> None:
    shown = h.teaching(team, course="Cours fictif")
    values = {
        "course": "Cours fictif",
        "institution": "Autre université fictive",
        "notes": "",
        "kind": "CM",
        "hours": "12",
        "start_date": h.START,
        "end_date": h.START,
    }
    services.update_teaching(teaching=shown, user=team.student, values=values)
    event = AuditEvent.objects.filter(action="TEACHING_UPDATED").get()
    assert event.details_json["fields"] == ["institution", "kind"]


def test_create_posted_like_a_browser(team: SimpleNamespace) -> None:
    client = h.browser_for(team.student)
    data = {"course": "Cours posté fictif", "institution": "", "notes": "", "kind": "TP",
            "hours": "4,5", "start_date": "2027-01-11", "end_date": "2027-01-12",
            "level": "PUBLIC"}  # fmt: skip
    response = h.browser_post(client, h.url(team, "new_teaching"), h.url(team, "new_teaching"),
                              data)  # fmt: skip
    assert response.status_code == 302
    created = Teaching.objects.get()
    assert created.hours == Decimal("4.5") and created.kind == "TP"


def test_the_level_view_asks_for_the_second_factor(team: SimpleNamespace) -> None:
    shown = h.teaching(team)
    client = h.browser_for(team.student)
    page = h.url(team, "teaching", teaching_id=shown.pk)
    response = h.browser_post(client, page, h.url(team, "teaching_level", teaching_id=shown.pk),
                              {"level": "INTERNAL_ST"})  # fmt: skip
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    assert Teaching.objects.get().level == "PUBLIC"
    mfa = h.mfa_browser(team.student)
    response = h.browser_post(mfa, page, h.url(team, "teaching_level", teaching_id=shown.pk),
                              {"level": "INTERNAL_ST"})  # fmt: skip
    assert response.status_code == 302 and Teaching.objects.get().level == "INTERNAL_ST"


# --------------------------------------------------------------------------- outreach


def test_outreach_create_read_and_levels(team: SimpleNamespace) -> None:
    public = h.outreach(team, title="Fête fictive de la science", kind="SCIENCE_FESTIVAL")
    hidden = h.outreach(team, L.INTERNAL_ST, title="TEMOIN-FUITE-DIFFUSION-ST")
    rows = services.outreach_list(team.student, team.thesis).done
    assert {r.outreach.pk for r in rows} == {public.pk, hidden.pk}
    director = services.outreach_list(team.director, team.thesis)
    assert [r.outreach.pk for r in director.done] == [public.pk] and director.partial
    body = _get(team, "co_supervisor", h.url(team, "outreach_list")).content.decode()
    assert "TEMOIN-FUITE-DIFFUSION-ST" not in body and "Fête fictive de la science" in body
    assert _get(team, "st_co_supervisor",
                h.url(team, "outreach", outreach_id=hidden.pk)).status_code == 200  # fmt: skip
    counts = {t.kind: t.count for t in director.totals}
    assert counts["SCIENCE_FESTIVAL"] == 1 and sum(counts.values()) == 1


def test_outreach_validation_and_rights(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        h.outreach(team, kind="BLOG")
    assert exc.value.code == "kind"
    with pytest.raises(ValidationError):
        h.outreach(team, title="")
    shown = h.outreach(team)
    with pytest.raises(PermissionDenied):
        services.change_outreach_level(outreach=shown, user=team.director, level=L.INTERNAL_ST,
                                       recent_mfa=True)  # fmt: skip
    services.change_outreach_level(outreach=shown, user=team.student, level=L.INTERNAL_LIRMM,
                                   recent_mfa=True)  # fmt: skip
    assert Outreach.objects.get().level == "INTERNAL_LIRMM"
    with pytest.raises(PermissionDenied):
        services.outreach_for(team.tutor, team.thesis, shown.pk, operation="detail")


# --------------------------------------------------------------------------- both


def test_no_text_in_clear_in_the_database(team: SimpleNamespace) -> None:
    h.teaching(team, course="TEMOIN-FUITE-COURS", institution="TEMOIN-FUITE-ETAB",
               notes="TEMOIN-FUITE-NOTE-ENS")  # fmt: skip
    h.outreach(team, title="TEMOIN-FUITE-ACTION", audience="TEMOIN-FUITE-PUBLIC",
               notes="TEMOIN-FUITE-NOTE-DIFF")  # fmt: skip
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        for (table,) in cursor.fetchall():
            cursor.execute(f'SELECT CAST(t AS text) FROM "{table}" t')  # noqa: S608  # nosec B608
            dump = "\n".join(row[0] for row in cursor.fetchall())
            assert "TEMOIN-FUITE" not in dump, table


@pytest.mark.parametrize("status", ["DEFENDED", "CLOSED"])
def test_frozen_after_the_defence_but_readable(team: SimpleNamespace, status: str) -> None:
    shown = h.teaching(team)
    action = h.outreach(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status, defended_on=h.D(2027, 1, 15))
    with pytest.raises(PermissionDenied):
        h.teaching(team)
    with pytest.raises(PermissionDenied):
        h.outreach(team)
    assert services.teaching_list(team.director, team.thesis).done[0].teaching.pk == shown.pk
    assert services.outreach_list(team.director, team.thesis).done[0].outreach.pk == action.pk


def test_no_event_of_the_activity_feed(team: SimpleNamespace) -> None:
    before = Event.objects.count()
    shown = h.teaching(team)
    h.outreach(team)
    services.change_teaching_level(teaching=shown, user=team.student, level=L.INTERNAL_ST,
                                   recent_mfa=True)  # fmt: skip
    assert Event.objects.count() == before
