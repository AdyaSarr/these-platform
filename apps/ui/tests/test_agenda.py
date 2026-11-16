"""The « À venir » card of the thesis dashboard (frontend step 8a).

- Sources: meetings, conference deadlines and ST request dates, missions, objective
  due dates (category only), due dates of the administrative-file items; each one
  read only with its module's action, through the module's own filter per viewer
  (Mission.objects.visible_to, admin_file.services.visible_items...).
- Order of the operations: filter per viewer, THEN the 30-day window, THEN sort,
  THEN cut at 8 lines. A cut before the filter would let the number of lines
  reveal hidden elements (tested here with hidden missions nearer in time than the
  visible one; the same case is in the two-worlds test of M7).
- No sealed text: dates, kinds, acronyms, destinations, categories, item labels.
"""

from __future__ import annotations

import datetime as dt
import html as html_module
import re
from types import SimpleNamespace
from typing import Any

import pytest
from django.urls import reverse

from apps.travel.tests.helpers import NOW, D, client_for, make_team, mission_of

pytestmark = pytest.mark.django_db(transaction=True)

TODAY = NOW.date()  # 2027-01-20, a Wednesday


@pytest.fixture(autouse=True)
def _now(time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=False)


@pytest.fixture
def team() -> SimpleNamespace:
    return make_team()


def _dashboard(team: SimpleNamespace, role: str) -> str:
    response = client_for(team, role).get(
        reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk})
    )
    assert response.status_code == 200
    return response.content.decode()


def _agenda(page: str) -> list[tuple[str, str]]:
    """(kind, text) of each line of the card, in order; spaces collapsed, but never
    the no-break ones (\\s would match them)."""
    card = re.search(r'<section class="card agenda".*?</section>', page, re.S)
    assert card is not None
    items = re.findall(
        r'<li class="agenda-item" data-agenda="(\w+)">(.*?)</li>', card.group(0), re.S
    )
    return [
        (
            kind,
            re.sub(r"[ \t\r\n]+", " ", html_module.unescape(re.sub(r"<[^>]+>", " ", text))).strip(),
        )
        for kind, text in items
    ]


WORKING_DAYS = [D(2027, 1, d) for d in (21, 22, 25, 26, 27, 28, 29)] + [
    D(2027, 2, d) for d in (1, 2, 3)
]


def test_the_cut_comes_after_the_filter_per_viewer(team: SimpleNamespace) -> None:
    """Ten missions hidden from the co-supervisor (to Rousset, paid by ST), all
    nearer than the one they see (to the LIRMM, paid by the LIRMM): the
    co-supervisor's card shows theirs, as if the others did not exist."""
    for day in WORKING_DAYS:
        mission_of(team, destination="ROUSSET", payer="ST", start_date=day, end_date=day)
    mission_of(team, payer="LIRMM", start_date=D(2027, 2, 12), end_date=D(2027, 2, 12))

    student = _agenda(_dashboard(team, "student"))
    assert len(student) == 8  # the limit, reached by the nearest missions
    assert all(kind == "mission" for kind, _ in student)
    assert "12\u00a0févr." not in " ".join(text for _, text in student)

    co_sup = _agenda(_dashboard(team, "co_supervisor"))
    assert co_sup == [("mission", next(t for k, t in co_sup if k == "mission"))]
    assert co_sup[0][1].startswith("12\u00a0févr.")


def test_the_window_is_thirty_days(team: SimpleNamespace) -> None:
    last = TODAY + dt.timedelta(days=30)  # 2027-02-19, a Friday
    mission_of(team, payer="LIRMM", start_date=last, end_date=last)
    mission_of(team, payer="LIRMM", start_date=D(2027, 2, 22), end_date=D(2027, 2, 22))
    lines = _agenda(_dashboard(team, "student"))
    assert [text[:8] for _, text in lines] == ["19\u00a0févr."]


def test_meetings_show_date_and_kind_never_the_title(team: SimpleNamespace) -> None:
    from apps.meetings.tests.helpers import meeting_of

    meeting_of(
        team,
        starts_at=dt.datetime(2027, 1, 28, 13, 0, tzinfo=dt.UTC),
        title="Titre fictif à ne jamais afficher",
        attendees=[team.student, team.director],
    )
    page = _dashboard(team, "co_supervisor")
    lines = _agenda(page)
    assert lines and lines[0][0] == "meeting"
    assert lines[0][1].startswith("28\u00a0janv. · 14:00")  # Paris time
    assert "Encadrement" in lines[0][1] or "encadrement" in lines[0][1]
    assert "vous n'y assistez pas" in lines[0][1]
    assert "Titre fictif" not in page


def test_admin_file_items_follow_the_part_each_role_sees(team: SimpleNamespace) -> None:
    from apps.admin_file import services as admin_file
    from apps.admin_file.models import RequiredItem

    seen_by_tutor = set(
        admin_file.visible_items(team.tutor, team.thesis).values_list("pk", flat=True)
    )
    hidden = (
        admin_file.visible_items(team.student, team.thesis).exclude(pk__in=seen_by_tutor).first()
    )
    shown = admin_file.visible_items(team.tutor, team.thesis).first()
    assert hidden is not None and shown is not None
    # Fixture only: due dates set directly (the agenda reads them, it never writes).
    RequiredItem.objects.filter(pk=hidden.pk).update(due_date=D(2027, 1, 22))
    RequiredItem.objects.filter(pk=shown.pk).update(due_date=D(2027, 1, 29))

    def labels(role: str) -> list[str]:
        return [text for kind, text in _agenda(_dashboard(team, role)) if kind == "item"]

    hidden_label, shown_label = admin_file.label_of(hidden), admin_file.label_of(shown)
    assert [hidden_label in t for t in labels("student")] == [True, False]
    tutor = labels("tutor")
    assert len(tutor) == 1 and shown_label in tutor[0] and hidden_label not in tutor[0]


def test_nothing_planned(team: SimpleNamespace) -> None:
    page = _dashboard(team, "student")
    assert _agenda(page) == []
    assert "Rien de prévu dans les 30 prochains jours." in page


def test_select_is_window_then_sort_then_cut() -> None:
    from apps.ui.agenda import Entry, select

    def entry(day: dt.date, label: str) -> Entry:
        return Entry(
            day=day, time=None, kind="mission", icon="plane", label=label, detail="", url="/"
        )

    entries = [entry(TODAY + dt.timedelta(days=n), str(n)) for n in (31, 12, 0, 30, -1, 3)]
    entries += [entry(TODAY + dt.timedelta(days=n), f"x{n}") for n in range(4, 11)]
    picked = select(entries, TODAY)
    assert [e.label for e in picked] == ["0", "3", "x4", "x5", "x6", "x7", "x8", "x9"]
    assert select([entry(TODAY + dt.timedelta(days=30), "30")], TODAY)[0].label == "30"
