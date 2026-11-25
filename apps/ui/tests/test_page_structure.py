"""Common structure of the module pages (step 2, points 5 to 7):

- a page header (templates/ui/page_header.html, tag {% page_header %}): title,
  optional subtitle, the main action on the right as a button, secondary actions
  as discreet buttons with an icon; no "back to the thesis" link (the sidebar does
  it) and no bar of underlined links separated by dots;
- the sub-sections of a module as tabs: a <nav> of links, the current one with
  aria-current="page" (each tab is another page: not an ARIA tablist, which is
  for panels of one page);
- a choice of 2 or 3 values as a segmented control, the current one with
  aria-current="page".

No permission change: a button is shown under the very condition the old link
was (the role's write action), and the views re-check anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.template import Context, Template
from django.urls import reverse

from apps.accounts.permissions import ROLE_ACTIONS, Action
from apps.accounts.roles import Role

from .helpers import signed_in

pytestmark = pytest.mark.django_db

BASE_DIR = Path(__file__).resolve().parents[3]
TEMPLATES = BASE_DIR / "templates"
NBSP = "\u00a0"

# Pages outside a thesis keep their own heading (sign-in, invitation, list of theses).
OWN_H1 = {
    "ui/page_header.html",
    "ui/styleguide.html",
    "core/home.html",
    "account/login.html",
}


def _all_templates() -> list[Path]:
    paths = list(TEMPLATES.rglob("*.html")) + list(
        (BASE_DIR / "apps").glob("*/templates/**/*.html")
    )
    return sorted(paths)


def _name(path: Path) -> str:
    return str(path.relative_to(TEMPLATES)) if TEMPLATES in path.parents else str(path)


# --------------------------------------------------------------------------- static


def test_no_back_to_the_thesis_link_anywhere() -> None:
    found = [_name(p) for p in _all_templates() if "Back to the thesis" in p.read_text("utf-8")]
    assert found == []


def test_the_old_travel_link_bar_is_gone() -> None:
    assert not (TEMPLATES / "travel" / "_nav.html").exists()


def test_no_old_year_list() -> None:
    found = [_name(p) for p in _all_templates() if 'class="years"' in p.read_text("utf-8")]
    assert found == []


def test_thesis_pages_take_their_title_from_the_page_header() -> None:
    found = [
        _name(p)
        for p in _all_templates()
        if "<h1" in p.read_text("utf-8")
        and _name(p) not in OWN_H1
        and not _name(p).startswith("accounts/invitation")
    ]
    assert found == []


# --------------------------------------------------------------------------- the component


def _render(source: str, **context: object) -> str:
    return Template("{% load ui %}" + source).render(Context(context))


def test_page_header_with_a_title_only() -> None:
    html = _render("{% page_header %}Réunions{% endpage_header %}")
    assert re.search(r'<header class="page-header">', html)
    assert re.search(r"<h1>\s*Réunions\s*</h1>", html)
    assert "page-subtitle" not in html and "page-actions" not in html and "page-parent" not in html


def test_page_header_with_every_part() -> None:
    html = _render(
        "{% page_header parent_url=up parent_label='Objectifs' %}<em>Titre</em>"
        "{% subtitle %}Année 1{% actions %}"
        '{% include "ui/action.html" with url="/s/" label="Réglages" icon="settings" %}'
        '{% include "ui/action.html" with url="/n/" label="Nouveau" icon="plus" primary=True %}'
        "{% endpage_header %}",
        up="/t/x/objectives/",
    )
    assert re.search(r'<a class="page-parent" href="/t/x/objectives/">.*?Objectifs', html, re.S)
    assert re.search(r"<h1>\s*<em>Titre</em>\s*</h1>", html)
    assert re.search(r'<p class="page-subtitle">\s*Année 1\s*</p>', html)
    actions = re.search(r'<div class="page-actions">(.*?)</div>', html, re.S)
    assert actions is not None
    assert re.search(
        r'<a class="btn btn-primary" href="/n/">.*?<span>Nouveau</span></a>', actions.group(1), re.S
    )
    assert re.search(r'<a class="btn btn-ghost" href="/s/">\s*<svg class="icon"', actions.group(1))
    # The main action comes last in the source: on the right of the line.
    assert actions.group(1).index("/s/") < actions.group(1).index("/n/")


def test_title_and_subtitle_are_escaped() -> None:
    html = _render(
        "{% page_header %}{{ t }}{% subtitle %}{{ t }}{% endpage_header %}", t="<b>x</b>"
    )
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


# --------------------------------------------------------------------------- pages


def _get(role: Role, url_name: str, **query: str):  # type: ignore[no-untyped-def]
    client, thesis = signed_in(role)
    url = reverse(url_name, kwargs={"thesis_id": thesis.pk})
    response = client.get(url, query)
    assert response.status_code == 200, (role, url_name, response.status_code)
    return response.content.decode(), thesis


def _main(html: str) -> str:
    match = re.search(r"<main\b.*?</main>", html, re.S)
    assert match is not None
    return match.group(0)


def _header(html: str) -> str:
    match = re.search(r'<header class="page-header">.*?</header>', html, re.S)
    assert match is not None
    return match.group(0)


def _primary(html: str) -> list[tuple[str, str]]:
    return re.findall(
        r'<a class="btn btn-primary" href="([^"]+)">.*?<span>([^<]+)</span></a>',
        _header(html),
        re.S,
    )


def _tabs(html: str) -> list[tuple[str, bool]]:
    nav = re.search(r'<nav class="tabs" aria-label="[^"]+">(.*?)</nav>', html, re.S)
    assert nav is not None
    return [
        (label.strip(), bool(current))
        for current, label in re.findall(
            r'<a class="tab" href="[^"]+"( aria-current="page")?>([^<]+)</a>', nav.group(1)
        )
    ]


SEGMENT = (
    r'<(?:a|span) class="segment[^"]*"(?: href="[^"]*")?( aria-current="page")?[^>]*>'
    r"(.*?)</(?:a|span)>"
)


def _segments(html: str) -> list[tuple[str, bool]]:
    nav = re.search(r'<nav class="segmented" aria-label="[^"]+">(.*?)</nav>', html, re.S)
    assert nav is not None
    return [
        (re.sub(r"<[^>]+>", "", label).strip(), bool(current))
        for current, label in re.findall(
            SEGMENT,
            nav.group(1),
            re.S,
        )
    ]


PRIMARY = [
    ("meetings:list", Action.MEETING_READ, Action.MEETING_WRITE, "meetings:new",
     "Nouvelle réunion"),
    ("vault:documents", Action.DOCUMENT_READ, Action.DOCUMENT_WRITE, "vault:upload",
     "Déposer un document"),
    (
        "conferences:deadlines", Action.CONFERENCE_READ, Action.CONFERENCE_WRITE,
        "conferences:new_venue", "Nouvelle conférence ou revue",
    ),
    (
        "conferences:submissions", Action.CONFERENCE_READ, Action.SUBMISSION_WRITE,
        "conferences:new_submission", "Nouvelle soumission",
    ),
    (
        "admin_file:file", Action.CIFRE_FILE_READ, Action.ADMIN_FILE_WRITE,
        "admin_file:new_item", "Ajouter une pièce personnalisée",
    ),
    ("travel:missions", Action.TRAVEL_READ, Action.TRAVEL_WRITE, "travel:new_mission",
     "Prévoir une mission"),
    ("objectives:dashboard", Action.OBJECTIVE_READ, Action.OBJECTIVE_WRITE, "objectives:new",
     "Nouvel objectif"),
]  # fmt: skip

CASES = [
    pytest.param(role, *case, id=f"{case[0]}-{role.value}")
    for case in PRIMARY
    for role in Role
    if case[1] in ROLE_ACTIONS[role]
]


@pytest.mark.parametrize(("role", "page", "read", "write", "target", "label"), CASES)
def test_the_main_action_is_a_button_for_whoever_may_write_only(
    role: Role, page: str, read: Action, write: Action, target: str, label: str
) -> None:
    html, thesis = _get(role, page)
    expected = [(reverse(target, kwargs={"thesis_id": thesis.pk}), label)]
    assert _primary(html) == (expected if write in ROLE_ACTIONS[role] else [])
    # Never twice: the old link inside the page is gone.
    assert _main(html).count(f'href="{expected[0][0]}"') == (
        1 if write in ROLE_ACTIONS[role] else 0
    )


def test_review_period_is_a_discreet_settings_button() -> None:
    html, thesis = _get(Role.PHD_STUDENT, "conferences:deadlines")
    policy = reverse("conferences:policy", kwargs={"thesis_id": thesis.pk})
    ghost = re.search(
        rf'<a class="btn btn-ghost" href="{policy}">\s*<svg class="icon"[^>]*>.*?</svg>'
        r"<span>([^<]+)</span>",
        _header(html),
        re.S,
    )
    assert ghost is not None
    assert re.fullmatch(rf"Délai de relecture ST{NBSP}: \d+ jours", ghost.group(1))
    assert "lucide-settings" in ghost.group(0) or "M9.671 4.136" in ghost.group(0)


def test_missions_calendar_export_is_a_secondary_button() -> None:
    html, thesis = _get(Role.PHD_STUDENT, "travel:missions")
    ics = reverse("travel:missions_ics", kwargs={"thesis_id": thesis.pk})
    assert re.search(rf'<a class="btn btn-ghost" href="{ics}">', _header(html))


@pytest.mark.parametrize(
    ("page", "tabs"),
    [
        ("meetings:list", [("Réunions", True), ("Registre des décisions", False)]),
        ("meetings:register", [("Réunions", False), ("Registre des décisions", True)]),
        ("conferences:deadlines", [("Échéances", True), ("Soumissions", False)]),
        ("conferences:submissions", [("Échéances", False), ("Soumissions", True)]),
        ("conferences:policy", [("Échéances", True), ("Soumissions", False)]),
    ],
)
def test_module_tabs(page: str, tabs: list[tuple[str, bool]]) -> None:
    html, _ = _get(Role.PHD_STUDENT, page)
    assert _tabs(html) == tabs


TRAVEL_TABS = ["Calendrier", "Missions", "Temps ST / LIRMM", "Budget", "Réglages"]
ORDERS_TAB = "À décider"


@pytest.mark.parametrize(
    ("page", "current"),
    [
        ("travel:calendar", "Calendrier"),
        ("travel:missions", "Missions"),
        ("travel:new_mission", "Missions"),
        ("travel:split", "Temps ST / LIRMM"),
        ("travel:budget", "Budget"),
        ("travel:settings", "Réglages"),
    ],
)
def test_travel_tabs(page: str, current: str) -> None:
    html, _ = _get(Role.PHD_STUDENT, page)
    assert _tabs(html) == [(label, label == current) for label in TRAVEL_TABS]


def test_the_budget_tab_follows_the_budget_rule() -> None:
    """A co-supervisor never sees the budget (travel.services.budget_payers):
    neither its tab."""
    html, _ = _get(Role.CO_SUPERVISOR, "travel:calendar")
    assert [label for label, _ in _tabs(html)] == [t for t in TRAVEL_TABS if t != "Budget"]


@pytest.mark.parametrize(
    ("role", "shown"),
    [
        (Role.DIRECTOR, True),  # decides the LIRMM approvals
        (Role.INDUSTRIAL_TUTOR, True),  # decides the ST approvals
        (Role.PHD_STUDENT, False),
        (Role.CO_SUPERVISOR, False),
    ],
)
def test_the_approvals_tab_is_for_the_payers_person_only(role: Role, shown: bool) -> None:
    """Same rule as the queue (travel.services.pending_orders_for): the right to
    decide and the payer's role."""
    html, thesis = _get(role, "travel:calendar")
    labels = [label for label, _ in _tabs(html)]
    assert (ORDERS_TAB in labels) is shown
    orders = reverse("travel:orders", kwargs={"thesis_id": thesis.pk})
    assert (f'href="{orders}"' in html) is shown


@pytest.mark.parametrize("role", [Role.DIRECTOR, Role.INDUSTRIAL_TUTOR])
def test_the_approvals_page_says_approval_never_order(role: Role) -> None:
    html, _ = _get(role, "travel:orders")
    assert dict(_tabs(html))[ORDERS_TAB] is True
    header = _header(html)
    assert re.search(r"<h1>\s*Accords de mission à décider\s*</h1>", header)
    tabs = re.search(r'<nav class="tabs".*?</nav>', html, re.S)
    assert tabs is not None
    for part in (header, tabs.group(0)):
        assert "rdre de mission" not in part and "rdres de mission" not in part


def _module_pages(module: str, thesis_id: object) -> dict[str, str]:
    from apps.ui.tabs import MODULES

    return {
        str(tab.label): reverse(f"{module}:{tab.url_name}", kwargs={"thesis_id": thesis_id})
        for tab in MODULES[module].tabs
    }


@pytest.mark.parametrize("role", [r for r in Role if r is not Role.GUEST])
@pytest.mark.parametrize("module", ["meetings", "conferences", "travel", "portfolio"])
def test_tabs_follow_the_rights(role: Role, module: str) -> None:
    """Like the sidebar: every tab shown opens (200), every tab left out is a page
    refused to this role (403 or 404)."""
    from django.utils import translation

    client, thesis = signed_in(role)
    with translation.override("fr"):
        pages = _module_pages(module, thesis.pk)
    first = client.get(next(iter(pages.values())))
    if first.status_code != 200:  # the module itself is refused: no tab anywhere
        assert first.status_code in (403, 404), (role, module, first.status_code)
        return
    shown = {label for label, _ in _tabs(first.content.decode())}
    assert shown <= set(pages)
    for label, url in pages.items():
        status = client.get(url).status_code
        if label in shown:
            assert status == 200, (role, label, status)
        else:
            assert status in (403, 404), (role, label, status)


def test_tabs_come_before_the_page_header() -> None:
    html, _ = _get(Role.PHD_STUDENT, "meetings:list")
    assert html.index('<nav class="tabs"') < html.index('<header class="page-header">')


def test_meeting_period_is_a_segmented_control() -> None:
    html, _ = _get(Role.PHD_STUDENT, "meetings:list")
    assert _segments(html) == [("À venir", True), ("Passées", False)]
    html, _ = _get(Role.PHD_STUDENT, "meetings:list", when="past")
    assert _segments(html) == [("À venir", False), ("Passées", True)]


def test_calendar_months_are_a_segmented_control_with_the_month_in_the_middle() -> None:
    html, _ = _get(Role.PHD_STUDENT, "travel:calendar")
    segments = _segments(html)
    assert len(segments) == 3
    assert segments[0][0] == "Mois précédent" and segments[2][0] == "Mois suivant"
    assert re.fullmatch(r"[a-zéû]+ \d{4}", segments[1][0]), segments[1]
    assert segments[1][1] is True  # the month shown is the current segment


def test_objective_years_are_a_segmented_control() -> None:
    html, _ = _get(Role.PHD_STUDENT, "objectives:dashboard")
    segments = _segments(html)
    assert segments and segments[0][0] == "Année 1"
    assert [current for _, current in segments].count(True) == 1
