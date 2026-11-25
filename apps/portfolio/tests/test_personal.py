"""Skills and professional project (M9b, Q1): the student alone, nobody else, in no
form (page, list, count, activity feed, ADUM summary); sealed at the fixed level
CONFIDENTIAL; readable after the defence, written in SETUP and ACTIVE only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.accounts.models import Thesis
from apps.activity.models import Event
from apps.portfolio import services
from apps.portfolio.models import PERSONAL_LEVEL, CareerNote, SkillNote
from apps.portfolio.tests import helpers as h
from apps.vault.models import AuditEvent, SealedText

pytestmark = pytest.mark.django_db(transaction=True)
PAGES = ("skills", "new_skill", "career")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _get(team: SimpleNamespace, attr: str, path: str) -> Any:
    return h.client_for(getattr(team, attr)).get(path)


def test_the_fixed_level() -> None:
    assert PERSONAL_LEVEL == "CONFIDENTIAL"
    for model in (SkillNote, CareerNote):
        assert "level" not in {f.name for f in model._meta.concrete_fields}


def test_the_student_writes_and_reads_back(team: SimpleNamespace) -> None:
    skill = h.skill(team, title="Analyse fictive", evidence="Preuve fictive", category="SCIENTIFIC")
    services.set_career_note(thesis=team.thesis, user=team.student, text="Projet fictif")
    rows = services.skill_list(team.student, team.thesis)
    assert [(r.skill.pk, r.texts["title"], r.texts["evidence"]) for r in rows] == [
        (skill.pk, "Analyse fictive", "Preuve fictive")
    ]
    assert services.career_note(team.student, team.thesis) == "Projet fictif"
    note = CareerNote.objects.get(thesis=team.thesis)
    assert set(SealedText.objects.filter(object_id__in=[skill.pk, note.pk])
               .values_list("level", flat=True)) == {"CONFIDENTIAL"}  # fmt: skip
    services.update_skill(
        skill=skill,
        user=team.student,
        values={"title": "Analyse fictive", "evidence": "", "category": "OTHER"},
    )
    event = AuditEvent.objects.filter(action="SKILL_NOTE_UPDATED").get()
    assert event.details_json["fields"] == ["category", "evidence"]
    services.set_career_note(thesis=team.thesis, user=team.student, text="")
    assert services.career_note(team.student, team.thesis) == ""
    assert AuditEvent.objects.filter(action="CAREER_NOTE_CHANGED").count() == 2


def test_validation(team: SimpleNamespace) -> None:
    with pytest.raises(ValidationError) as exc:
        h.skill(team, category="SOFT")
    assert exc.value.code == "category"
    with pytest.raises(ValidationError) as exc:
        h.skill(team, title="")
    assert exc.value.code == "title"
    with pytest.raises(ValidationError):
        services.set_career_note(thesis=team.thesis, user=team.student, text="x" * 8001)


@pytest.mark.parametrize("attr", h.NOT_STUDENT)
def test_nobody_else_reads_or_writes(team: SimpleNamespace, attr: str) -> None:
    skill = h.skill(team, title="TEMOIN-FUITE-COMPETENCE")
    services.set_career_note(thesis=team.thesis, user=team.student, text="TEMOIN-FUITE-PROJET")
    user = getattr(team, attr)
    for name in PAGES:
        assert _get(team, attr, h.url(team, name)).status_code == 403, name
    assert _get(team, attr, h.url(team, "edit_skill", skill_id=skill.pk)).status_code == 403
    with pytest.raises(PermissionDenied):
        services.skill_list(user, team.thesis)
    with pytest.raises(PermissionDenied):
        services.career_note(user, team.thesis)
    with pytest.raises(PermissionDenied):
        h.skill(SimpleNamespace(thesis=team.thesis, student=user))
    with pytest.raises(PermissionDenied):
        services.set_career_note(thesis=team.thesis, user=user, text="Autre projet")
    assert not SkillNote.objects.readable_by(user, team.thesis).exists()


@pytest.mark.parametrize("attr", ["director", "tutor", "co_supervisor", "st_co_supervisor"])
def test_no_trace_on_the_supervisors_portfolio_pages(team: SimpleNamespace, attr: str) -> None:
    pages = [h.url(team, n) for n in ("trainings", "teachings", "outreach_list")]
    pages = [p for p in pages if _get(team, attr, p).status_code == 200]
    before = [_get(team, attr, p).content for p in pages]
    h.skill(team, title="TEMOIN-FUITE-COMPETENCE")
    services.set_career_note(thesis=team.thesis, user=team.student, text="TEMOIN-FUITE-PROJET")
    after = [_get(team, attr, p).content for p in pages]
    import re

    def plain(body: bytes) -> str:
        return re.sub(r'value="[^"]+"', "", body.decode())

    assert [plain(b) for b in before] == [plain(a) for a in after]


def test_the_skills_page_is_the_students(team: SimpleNamespace) -> None:
    h.skill(team, title="Compétence fictive visible")
    services.set_career_note(thesis=team.thesis, user=team.student, text="Projet fictif visible")
    body = _get(team, "student", h.url(team, "skills")).content.decode()
    assert "Compétence fictive visible" in body and "Projet fictif visible" in body


def test_posted_like_a_browser(team: SimpleNamespace) -> None:
    client = h.browser_for(team.student)
    page = h.url(team, "new_skill")
    data = {"title": "Compétence postée fictive", "evidence": "", "category": "LANGUAGE"}
    assert h.browser_post(client, page, page, data).status_code == 302
    career = h.url(team, "career")
    assert (
        h.browser_post(client, career, career, {"text": "Projet posté fictif"}).status_code == 302
    )
    assert services.career_note(team.student, team.thesis) == "Projet posté fictif"


def test_readable_after_the_defence_frozen_for_writing(team: SimpleNamespace) -> None:
    h.skill(team, title="Compétence fictive")
    Thesis.objects.filter(pk=team.thesis.pk).update(status="DEFENDED", defended_on=h.D(2027, 1, 15))
    assert services.skill_list(team.student, team.thesis)[0].texts["title"] == "Compétence fictive"
    with pytest.raises(PermissionDenied):
        h.skill(team)
    with pytest.raises(PermissionDenied):
        services.set_career_note(thesis=team.thesis, user=team.student, text="Projet fictif")


def test_no_text_in_clear_and_no_activity_event(team: SimpleNamespace) -> None:
    before = Event.objects.count()
    h.skill(team, title="TEMOIN-FUITE-TITRE-COMP", evidence="TEMOIN-FUITE-PREUVE")
    services.set_career_note(thesis=team.thesis, user=team.student, text="TEMOIN-FUITE-CARRIERE")
    assert Event.objects.count() == before
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        for (table,) in cursor.fetchall():
            cursor.execute(f'SELECT CAST(t AS text) FROM "{table}" t')  # noqa: S608  # nosec B608
            dump = "\n".join(row[0] for row in cursor.fetchall())
            assert "TEMOIN-FUITE" not in dump, table


def test_never_in_the_adum_summary(team: SimpleNamespace) -> None:
    h.skill(team, title="TEMOIN-FUITE-COMPETENCE-ADUM")
    services.set_career_note(thesis=team.thesis, user=team.student, text="TEMOIN-FUITE-PROJET")
    h.training(team)
    client = h.client_for(team.student)
    body = client.get(h.url(team, "adum") + "?internal=1&accepted=1").content.decode()
    assert "TEMOIN-FUITE" not in body
    for section in services.ADUM_SECTIONS:
        _name, content = services.adum_csv(thesis=team.thesis, user=team.student, year=1,
                                           include_internal=True, recent_mfa=True,
                                           section=section)  # fmt: skip
        assert b"TEMOIN-FUITE" not in content, section
