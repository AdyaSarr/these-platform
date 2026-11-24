"""Who and when (M13b, criteria 1 to 4): the student alone, in every status, with a
recent second factor, at most once an hour; the others get the refusal of right."""

from __future__ import annotations

import datetime as dt
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection

from apps.accounts.models import Thesis
from apps.export import services as export
from apps.export.models import ExportRecord
from apps.export.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
OTHERS = [a for a in h.ROLE_ATTRS.values() if a != "student"]


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    from apps.vault import services as vault

    vault.store(thesis=team.thesis, upload=h.pdf("contenu-fictif"), title="Note fictive",
                level=h.L.PUBLIC, user=team.student)  # fmt: skip
    return team


def _exports() -> int:
    return AuditEvent.objects.filter(action="THESIS_EXPORTED").count()


def _start(team: SimpleNamespace, user: Any = None) -> Any:
    return export.start_export(thesis=team.thesis, user=user or team.student, recent_mfa=True)


@pytest.mark.parametrize("attr", [*OTHERS, "outsider"])
def test_only_the_student_may_export(team: SimpleNamespace, attr: str) -> None:
    with pytest.raises(PermissionDenied):
        _start(team, getattr(team, attr))
    assert ExportRecord.objects.count() == 0 and _exports() == 0


@pytest.mark.parametrize("attr", OTHERS)
def test_the_refusal_is_that_of_a_missing_right(team: SimpleNamespace, attr: str) -> None:
    """Same bytes as on a thesis the person is not a member of, in GET and in
    POST, and also while the student's hourly limit is reached."""
    other = h.make_team()
    user = getattr(team, attr)
    client = h.mfa_browser(user)
    foreign_page = client.get(h.url(other, "page"))
    foreign_post = h.csrf_post(client, team, h.url(other, "download"), {})
    assert foreign_page.status_code == foreign_post.status_code == 403
    for _ in range(2):  # before and after the student's export (limit reached)
        page = client.get(h.url(team, "page"))
        post = h.csrf_post(client, team, h.url(team, "download"), {})
        assert (page.status_code, page.content) == (403, foreign_page.content)
        assert (post.status_code, post.content) == (403, foreign_post.content)
        b"".join(_start(team).chunks())
    assert ExportRecord.objects.filter(user=user).count() == 0


@pytest.mark.parametrize("status", list(Thesis.Status))
def test_every_status_allows_the_student(team: SimpleNamespace, status: str) -> None:
    Thesis.objects.filter(pk=team.thesis.pk).update(status=status)
    b"".join(_start(team).chunks())
    assert ExportRecord.objects.get().outcome == ExportRecord.Outcome.COMPLETE


def test_a_recent_second_factor_is_required(team: SimpleNamespace) -> None:
    from apps.accounts.reauth import ReauthenticationRequiredError

    with pytest.raises(ReauthenticationRequiredError):
        export.start_export(thesis=team.thesis, user=team.student, recent_mfa=False)
    client = h.client_for(team.student)  # signed in, no recent second factor
    response = client.post(h.url(team, "download"), {})
    assert response.status_code == 302
    assert ExportRecord.objects.count() == 0 and _exports() == 0


def test_once_an_hour(team: SimpleNamespace, time_machine: Any) -> None:
    b"".join(_start(team).chunks())
    with pytest.raises(export.ExportRateLimitedError):
        _start(team)
    time_machine.move_to(h.NOW + dt.timedelta(minutes=59), tick=True)
    with pytest.raises(export.ExportRateLimitedError):
        _start(team)
    time_machine.move_to(h.NOW + dt.timedelta(minutes=60, seconds=1), tick=True)
    b"".join(_start(team).chunks())
    assert ExportRecord.objects.count() == 2


def test_an_interrupted_export_counts(team: SimpleNamespace) -> None:
    chunks = _start(team).chunks()
    next(chunks)
    chunks.close()  # the client went away
    assert ExportRecord.objects.get().outcome == ExportRecord.Outcome.INCOMPLETE
    with pytest.raises(export.ExportRateLimitedError):
        _start(team)


def test_the_page_tells_the_student_of_the_limit(team: SimpleNamespace) -> None:
    b"".join(_start(team).chunks())
    client = h.mfa_browser(team.student)
    response = h.csrf_post(client, team, h.url(team, "download"), {})
    assert response.status_code == 429
    assert "Un export par heure" in response.content.decode()


@pytest.mark.serial
def test_two_simultaneous_exports_give_one(team: SimpleNamespace) -> None:
    results: list[str] = []
    barrier = threading.Barrier(2)

    def attempt() -> None:
        try:
            barrier.wait()
            started = _start(team)
            b"".join(started.chunks())
            results.append("exported")
        except export.ExportRateLimitedError:
            results.append("limited")
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == ["exported", "limited"]
    assert ExportRecord.objects.count() == 1 and _exports() == 1
