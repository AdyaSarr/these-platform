"""The overdue-action e-mail leads to the action itself (M11a, a gap closed): the
meeting's page carries one anchor per action (id="action-<uuid>"), and the link of
ACTION_OVERDUE ends with it. Still a link only (CLAUDE.md rule 7)."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core import mail
from django.test import Client
from django.urls import reverse

from apps.meetings import reminders, services
from apps.meetings.tests.helpers import meeting_of
from apps.objectives.tests.helpers import D, make_team, marker

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def test_the_overdue_e_mail_links_the_action(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    action = services.add_action(
        meeting=meeting, user=team.student, title=marker(), assignee=team.tutor,
        due_date=D(2027, 2, 1),
    )  # fmt: skip
    mail.outbox.clear()
    reminders.run(D(2027, 2, 2))
    (sent,) = mail.outbox
    detail = reverse(
        "meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": meeting.pk}
    )
    links = re.findall(r"https?://\S+", sent.body)
    assert links == [f"{settings.SITE_URL}{detail}#action-{action.pk}"]


def test_the_meeting_page_has_one_anchor_per_action(team: SimpleNamespace) -> None:
    meeting = meeting_of(team)
    actions = [
        services.add_action(
            meeting=meeting, user=team.student, title=marker(), assignee=who, due_date=D(2027, 2, 1)
        )
        for who in (team.tutor, team.director)
    ]
    client = Client()
    client.force_login(team.director)
    html = client.get(
        reverse("meetings:detail", kwargs={"thesis_id": team.thesis.pk, "meeting_id": meeting.pk})
    ).content.decode()
    for action in actions:
        assert html.count(f'id="action-{action.pk}"') == 1
