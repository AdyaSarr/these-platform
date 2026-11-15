"""M7 security audit: one test (or a pointer to one) per lead of the end-of-module review.

Leads already closed elsewhere:
- working days, French holidays, Whit Monday, a CIFRE year over two calendar years:
  apps/travel/tests/test_holidays.py, test_split.py.
- data minimisation (the tutor sees every kind of day, the director "unavailable", a
  co-supervisor LIRMM days and missions only; no counter, page, .ics, card or M5/M6
  section tells two worlds apart): apps/travel/tests/test_minimisation.py, checked by
  mutants (docs/decisions.md); its DEFERRED list is the merge gate below.
- a past day changed is journaled, a future one is not: test_presence.py.
- the payer's person alone decides an approval, second factor asked after a guard
  that refuses anyone else; snapshot coverage and invalidation; history; two
  simultaneous decisions decide once: test_orders.py, test_order_views.py.
- links to a meeting or a submission refused alike (malformed, unknown, other thesis,
  unreadable), never echoed: test_mission_links.py.
- amounts: ASCII digits only, integer cents, canonical sealed form: test_amounts.py;
  flat-rate lines, receipts, uploaded names never kept: test_expenses.py; budgets of
  each payer's person, never shown to a co-supervisor: test_budgets.py.
- reminders and notifications: Paris dates, once, a link only, the recipient checked
  again at sending, never a co-supervisor: test_reminders.py.
- .ics labels only: test_ics.py. Card and M5/M6 sections: test_dashboard.py.
- the official order is followed, not replaced (labels, checklist, document, warning):
  test_official_order.py.
"""

from __future__ import annotations

import ast
import datetime as dt
import logging
import secrets
import threading
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import get_resolver, reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.helpers import mfa_login
from apps.travel import reminders, services
from apps.travel.models import Expense, Mission, MissionOrder
from apps.travel.tests.helpers import NOW, PASSWORD, D, database_dump, make_team, mission_of
from apps.travel.tests.test_minimisation import DEFERRED
from apps.vault import storage
from apps.vault.models import AuditEvent, SealedText
from apps.vault.tests.concurrency import run_threads

pytestmark = pytest.mark.django_db(transaction=True)

APP = Path(__file__).resolve().parents[1] / "apps" / "travel"


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(NOW, tick=True)
    return make_team()


def _marker() -> str:
    return "TEMOIN-" + secrets.token_hex(8)


def _url(thesis: Any, name: str, **kw: Any) -> str:
    return reverse(f"travel:{name}", kwargs={"thesis_id": thesis.pk, **kw})


def _pdf(name: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"%PDF-1.7\n%fictif\n%%EOF\n", "application/pdf")


# ---------------------------------------------------------------- 1. no clear text anywhere


def test_no_marker_nor_amount_anywhere(
    team: SimpleNamespace, caplog: pytest.LogCaptureFixture, time_machine: Any
) -> None:
    caplog.set_level(logging.DEBUG)
    marks = {k: _marker() for k in ("place", "purpose", "lodging", "comment", "file", "official")}
    # Amounts no other row could contain: a comma or a point between digits.
    amounts = {
        "estimate": ("987654,32", "987654.32"),
        "line": ("876543,21", "876543.21"),
        "actual": ("765432,10", "765432.10"),
        "budget": ("654321,09", "654321.09"),
    }
    student, director = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(director, team.director, PASSWORD)
    response = student.post(
        _url(team.thesis, "new_mission"),
        {
            "start_date": "2027-02-08",
            "end_date": "2027-02-10",
            "destination": "OTHER",
            "destination_detail": marks["place"],
            "payer": "LIRMM",
            "level": "INTERNAL_LIRMM",
            "transports": ["TRAIN"],
            "lodging": "HOTEL",
            "lodging_note": marks["lodging"],
            "purpose": marks["purpose"],
        },
    )
    assert response.status_code == 302, response.content[:400]
    mission = Mission.objects.get()
    m = {"mission_id": mission.pk}
    student.post(
        _url(team.thesis, "add_expense", **m),
        {"category": "TRANSPORT", "estimated": amounts["line"][0], "actual": ""},
    )
    expense = Expense.objects.get()
    student.post(
        _url(team.thesis, "request_order", **m), {"estimated_total": amounts["estimate"][0]}
    )
    order = MissionOrder.objects.get()
    director.post(
        _url(team.thesis, "decide_order", order_id=order.pk),
        {"decision": "GRANTED", "reason": "", "comment": marks["comment"]},
    )
    student.post(
        _url(team.thesis, "official_order", **m), {"file": _pdf(f"{marks['official']}.pdf")}
    )
    director.post(
        _url(team.thesis, "set_budget"),
        {"payer": "LIRMM", "year": "2027", "amount": amounts["budget"][0]},
    )
    time_machine.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=dt.UTC), tick=True)
    student, director = Client(), Client()  # the sessions expired meanwhile
    mfa_login(student, team.student, PASSWORD)
    mfa_login(director, team.director, PASSWORD)
    student.post(_url(team.thesis, "mission_done", **m))
    student.post(
        _url(team.thesis, "update_expense", expense_id=expense.pk),
        {"estimated": amounts["line"][0], "actual": amounts["actual"][0]},
    )
    student.post(
        _url(team.thesis, "attach_receipt", expense_id=expense.pk),
        {"file": _pdf(f"{marks['file']}.pdf")},
    )
    reminders.run(dt.datetime(2027, 2, 25, 6, 0, tzinfo=dt.UTC))
    pages: list[str] = []
    for client in (student, director):
        for target in (
            _url(team.thesis, "mission", **m),
            _url(team.thesis, "missions"),
            _url(team.thesis, "budget") + "?year=2027",
            _url(team.thesis, "mission_ics", **m),
            _url(team.thesis, "missions_ics"),
            reverse("accounts:dashboard", kwargs={"thesis_id": team.thesis.pk}),
        ):
            response = client.get(target)
            assert response.status_code == 200, target
            if "ics" in target:
                pages.append(response.content.decode())
    assert Mission.objects.get().status == "DONE"
    assert MissionOrder.objects.get().decision == "GRANTED"

    dumps = database_dump()
    files = [p.read_bytes() for p in storage.root().rglob("*") if p.is_file()]
    logs = [f"{r.getMessage()} {r.args}" for r in caplog.records]
    mails = [f"{m.subject}{m.body}" for m in mail.outbox]
    assert len(dumps) > 50 and files and logs and mails and pages
    secrets_in_clear = [*marks.values(), *(a for pair in amounts.values() for a in pair)]
    for secret in secrets_in_clear:
        for dump in dumps:
            assert secret not in dump, secret
        for blob in files:
            assert secret.encode() not in blob, secret
        for line in [*logs, *mails, *pages]:
            assert secret not in line, secret
    sealed = SealedText.objects.filter(thesis=team.thesis, slot__gte=0x0401, slot__lte=0x04FF)
    assert sealed.count() == 8  # place, purpose, lodging, comment, estimate, line x2, budget
    assert set(sealed.values_list("level", flat=True)) == {"INTERNAL_LIRMM"}


# --------------------------------------------------------------------------- 2. IDOR


def test_every_travel_route_refuses_an_object_of_another_thesis(team: SimpleNamespace) -> None:
    other = make_team()
    theirs = mission_of(other, payer="ST")
    order = services.request_order(mission=theirs, user=other.student, estimated_total="10")
    expense = services.add_expense(mission=theirs, user=other.student, category="TRANSPORT")
    student, tutor = Client(), Client()
    mfa_login(student, team.student, PASSWORD)
    mfa_login(tutor, team.tutor, PASSWORD)
    m, o, e = {"mission_id": theirs.pk}, {"order_id": order.pk}, {"expense_id": expense.pk}
    calls: list[tuple[Client, str, str, dict[str, Any], dict[str, Any]]] = [
        (student, "get", "mission", m, {}),
        (student, "get", "edit_mission", m, {}),
        (student, "post", "edit_mission", m, {"purpose": "x"}),
        (student, "post", "mission_level", m, {"level": "PUBLIC"}),
        (student, "post", "cancel_mission", m, {}),
        (student, "post", "mission_done", m, {}),
        (student, "post", "mission_reimbursed", m, {}),
        (student, "post", "request_order", m, {"estimated_total": "10"}),
        (student, "post", "official_order", m, {"file": _pdf("x.pdf")}),
        (student, "post", "add_expense", m, {"category": "MEALS"}),
        (student, "get", "mission_ics", m, {}),
        (student, "post", "update_expense", e, {"estimated": "1"}),
        (student, "post", "attach_receipt", e, {"file": _pdf("y.pdf")}),
        (student, "post", "withdraw_order", o, {}),
        (tutor, "post", "decide_order", o, {"decision": "GRANTED"}),
    ]
    tested = {name for _, _, name, _, _ in calls}
    untested = {"calendar", "month", "presence", "split", "settings", "rules", "target",
                "reminders", "missions", "new_mission", "missions_ics", "budget", "set_budget",
                "orders"}  # fmt: skip
    assert tested | untested == {
        getattr(p, "name", None) for p in get_resolver("apps.travel.urls").url_patterns
    }
    for client, method, name, kw, data in calls:
        response = getattr(client, method)(_url(team.thesis, name, **kw), data)
        assert response.status_code == 403, (name, method)
    theirs.refresh_from_db()
    order.refresh_from_db()
    assert (theirs.status, theirs.level, order.decision) == (
        "ORDER_REQUESTED",
        "INTERNAL_ST",
        "PENDING",
    )
    assert not theirs.official_order_document_id
    assert Expense.objects.filter(mission=theirs).count() == 1


# --------------------------------------------------------------------------- 3. concurrency


def test_two_simultaneous_requests_leave_one_pending(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def request() -> None:
        barrier.wait()
        try:
            services.request_order(mission=mission, user=team.student, estimated_total="10")
            outcomes.append("requested")
        except ValidationError as exc:
            outcomes.append(str(exc.code))

    run_threads(request, request, control=lambda: None)
    assert sorted(outcomes) == ["already_pending", "requested"]
    assert MissionOrder.objects.filter(decision="PENDING").count() == 1


# --------------------------------------------------------------------------- 4. rotation


def test_rotation_and_retirement_keep_travel_texts_and_amounts_readable(
    team: SimpleNamespace, time_machine: Any
) -> None:
    mission = mission_of(team, payer="LIRMM", purpose="Motif fictif")
    services.add_expense(mission=mission, user=team.student, category="MEALS", estimated="12")
    order = services.request_order(mission=mission, user=team.student, estimated_total="34")
    services.decide_order(
        order=order,
        user=team.director,
        decision="GRANTED",
        comment="Accord fictif",
        recent_mfa=True,
    )
    services.set_budget(
        thesis=team.thesis,
        user=team.director,
        payer="LIRMM",
        year=2027,
        amount="56",
        recent_mfa=True,
    )
    call_command("rotate_kek", "INTERNAL_LIRMM", stdout=StringIO())
    time_machine.move_to(NOW + dt.timedelta(hours=1), tick=True)
    call_command("rotate_kek", "INTERNAL_LIRMM", "--retire", stdout=StringIO())
    kek_ids = set(SealedText.objects.values_list("kek_id", flat=True))
    assert kek_ids == {"env:INTERNAL_LIRMM:2"}
    assert services.mission_texts(team.director, mission)["purpose"] == "Motif fictif"
    (view,) = services.orders_of(team.director, mission)
    assert (view.estimate, view.comment) == (3400, "Accord fictif")
    (line,) = services.expenses_of(team.director, mission)
    assert line.estimated == 1200
    assert services.budget_summary(team.director, team.thesis, 2027)["LIRMM"].budget == 5600


# --------------------------------------------------------------------------- 5. static


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# Whole packages refused, and urllib.request (urllib.parse is allowed: it parses our
# own SITE_URL). dateutil is a test-only dependency (tests/test_repo_hygiene.py).
FORBIDDEN_PACKAGES = {"socket", "http", "requests", "httpx", "aiohttp", "ftplib", "yaml",
                      "pickle", "marshal", "shelve", "icalendar", "dateutil"}  # fmt: skip
FORBIDDEN_MODULES = {"urllib.request"}


@pytest.mark.parametrize(
    "path",
    sorted(p for p in APP.rglob("*.py") if "tests" not in p.parts),
    ids=lambda p: str(p.relative_to(APP)),
)
def test_no_network_yaml_or_pickle_in_the_module(path: Path) -> None:
    """No API (SNCF or other): everything is typed by hand."""
    imported = _imports(path)
    bad = {
        name
        for name in imported
        if name in FORBIDDEN_MODULES or name.split(".")[0] in FORBIDDEN_PACKAGES
    }
    assert bad == set()


def test_no_travel_model_in_the_django_admin() -> None:
    from django.contrib import admin

    assert {m for m in admin.site._registry if m._meta.app_label == "travel"} == set()


ADDRESS_WORDS = ("address", "adresse", "domicile", "home", "street", "rue", "postal", "zip")


def test_no_address_field_in_the_module() -> None:
    """No personal address is ever stored (docs/modules/M07-deplacements.md)."""
    from django.apps import apps

    names = [
        f"{model.__name__}.{field.name}"
        for model in apps.get_app_config("travel").get_models()
        for field in model._meta.get_fields()
    ]
    assert names
    assert [n for n in names if any(w in n.lower() for w in ADDRESS_WORDS)] == []


def test_anonymous_and_strangers_get_nothing(team: SimpleNamespace) -> None:
    mission = mission_of(team)
    before = AuditEvent.objects.count()
    anonymous = Client()
    for name, kw in (
        ("calendar", {}),
        ("mission", {"mission_id": mission.pk}),
        ("missions_ics", {}),
    ):
        response = anonymous.get(_url(team.thesis, name, **kw))
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]
    assert AuditEvent.objects.count() == before
    stranger = Client()
    stranger.force_login(UserFactory.create(password=PASSWORD))
    for name, kw in (("calendar", {}), ("mission", {"mission_id": mission.pk}), ("budget", {})):
        assert stranger.get(_url(team.thesis, name, **kw)).status_code == 403


def test_a_finished_thesis_freezes_travel(team: SimpleNamespace) -> None:
    from apps.accounts.models import Thesis

    mission = mission_of(team)
    Thesis.objects.filter(pk=team.thesis.pk).update(status=Thesis.Status.DEFENDED)
    for action in (
        lambda: mission_of(team, start_date=D(2027, 3, 1), end_date=D(2027, 3, 1)),
        lambda: services.request_order(mission=mission, user=team.student, estimated_total="1"),
        lambda: services.add_expense(mission=mission, user=team.student, category="MEALS"),
        lambda: services.set_presence(
            thesis=team.thesis,
            user=team.student,
            first=D(2027, 2, 1),
            last=D(2027, 2, 1),
            kind="LIRMM",
        ),
    ):
        with pytest.raises(ValidationError) as info:
            action()
        assert info.value.code == "frozen"
    assert timezone.now()  # the clock still runs: nothing else to check


# --------------------------------------------------------------------------- merge gate


@pytest.mark.merge_gate
def test_every_m7_page_is_covered_by_the_two_worlds_test() -> None:
    assert DEFERRED == {}, (
        "pages of M7 still outside the two-worlds test: render them in "
        f"test_minimisation._surfaces() before the merge: {sorted(DEFERRED)}"
    )
