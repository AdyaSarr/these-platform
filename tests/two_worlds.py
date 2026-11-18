"""Two worlds per module (SR-06, docs/security/revue-2026-11.md).

For a role, worlds that differ only by objects the role does not read (their
EXISTENCE included: « on ne voit l'existence que de ce qu'on peut lire ») must
look the same, byte for byte, on every GET route of a module. Three states are
compared: the shared objects only, then world "a" (hidden objects added), then
world "b" (more of them), all rendered at the same frozen instant by a client
logged in at that instant.

Hidden objects, for a role: documents, meetings, submissions and objectives at a
level it does not read (every level when it lacks the module's read action),
missions it does not see, items of a section it does not read; documents it does
not read linked to the shared meeting and the shared submission; a past meeting
whose action is linked to the shared objective; submissions linked to the shared
objective and aimed at the shared edition. The catalogue of the conferences
(venues, editions, deadlines) is shared: the documented exception.

Not vacuous: every module declares pages that must answer 200 with an expected
content for a reader; the student's pages differ between the worlds; every GET
route of the module is rendered (closed list, as travel/tests/test_minimisation.py);
an id of a hidden object answers like an unknown id.

Fictitious data only (CLAUDE.md, rule 10).
"""

from __future__ import annotations

import datetime as dt
import difflib
import itertools
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

from django.core import mail
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse

from apps.accounts.permissions import LEVEL_ROLES, ROLE_ACTIONS, Action
from apps.accounts.roles import Role
from apps.activity.tests import helpers as h
from apps.core.confidentiality import ConfidentialityLevel as L

R = Role
ROLES = [R.INDUSTRIAL_TUTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_CO_SUPERVISOR, R.DIRECTOR,
         R.CSI_MEMBER]  # fmt: skip
ROLE_IDS = [r.value for r in ROLES]
# Friday 22 January 2027, 10:00 in Paris: two days after h.NOW (the shared objects).
FIXED = dt.datetime(2027, 1, 22, 9, 0, tzinfo=dt.UTC)
UNKNOWN = "00000000-0000-4000-8000-000000000000"
_CSRF = re.compile(r'(name="csrfmiddlewaretoken" value=|"X-CSRFToken": )"[^"]+"')
_D = h.D


# --------------------------------------------------------------------------- routes


def _walk(patterns: list[Any], prefix: str = "", ns: str = "") -> Iterator[tuple[str, str, Any]]:
    for p in patterns:
        if isinstance(p, URLResolver):
            inner = ":".join(n for n in (ns, p.namespace or "") if n)
            yield from _walk(p.url_patterns, prefix + str(p.pattern), inner)
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), ns, p


def get_routes(namespace: str) -> dict[str, str]:
    """{url name: route} of every route of ``namespace`` under a thesis that
    answers GET (a view class without a get() method answers POST only)."""
    out = {}
    for route, ns, pattern in _walk(get_resolver().url_patterns):
        if ns != namespace or not route.startswith("t/<uuid:thesis_id>/") or not pattern.name:
            continue
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class is None or hasattr(view_class, "get"):
            out[pattern.name] = route
    return out


def covered(namespace: str, targets: list[str]) -> set[str]:
    names: set[str] = set()
    for target in targets:
        match = resolve(urlsplit(target).path)
        if match.namespace == namespace and match.url_name:
            names.add(match.url_name)
    return names


def assert_every_route_is_rendered(
    namespace: str, targets: list[str], only: frozenset[str] | None = None
) -> None:
    routes = get_routes(namespace)
    assert routes, namespace  # not vacuous: the walk finds the module
    if only is not None:  # a module whose other routes have their own test
        assert only <= set(routes), sorted(only - set(routes))
        routes = {name: route for name, route in routes.items() if name in only}
    missing = set(routes) - covered(namespace, targets)
    assert missing == set(), f"{namespace}: GET routes not rendered: {sorted(missing)}"


# --------------------------------------------------------------------------- levels


def hidden_levels(role: Role, action: Action) -> list[L]:
    """The levels ``role`` does not read for ``action`` (all of them without it)."""
    if action not in ROLE_ACTIONS[role]:
        return list(L)
    return [level for level in L if role not in LEVEL_ROLES[level]]


def readable_levels(role: Role, action: Action) -> list[L]:
    hidden = hidden_levels(role, action)
    return [level for level in L if level not in hidden]


def hidden_destinations(role: Role) -> list[tuple[str, str]]:
    return {
        R.CO_SUPERVISOR: [("ROUSSET", "ST"), ("CONFERENCE", "LIRMM")],
        R.INDUSTRIAL_CO_SUPERVISOR: [("LIRMM", "LIRMM"), ("CONFERENCE", "LIRMM")],
        R.CSI_MEMBER: [("LIRMM", "LIRMM"), ("ROUSSET", "ST")],
    }.get(role, [])


def hidden_sections(role: Role) -> list[str]:
    return {
        R.INDUSTRIAL_TUTOR: ["ACADEMIC"],
        R.CO_SUPERVISOR: ["CIFRE", "ACADEMIC"],
        R.INDUSTRIAL_CO_SUPERVISOR: ["CIFRE", "ACADEMIC"],
        R.CSI_MEMBER: ["CIFRE", "ACADEMIC"],
    }.get(role, [])


# --------------------------------------------------------------------------- worlds


@dataclass
class Shared:
    documents: dict[L, Any] = field(default_factory=dict)
    meetings: dict[L, Any] = field(default_factory=dict)  # upcoming, one per level
    past_meeting: Any = None  # PUBLIC, begun: a decision, an action, a document
    submissions: dict[L, Any] = field(default_factory=dict)
    objectives: dict[L, Any] = field(default_factory=dict)
    missions: list[Any] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)
    venue: Any = None
    edition: Any = None


@dataclass
class Hidden:
    documents: list[Any] = field(default_factory=list)
    versions: list[Any] = field(default_factory=list)
    meetings: list[Any] = field(default_factory=list)
    submissions: list[Any] = field(default_factory=list)
    objectives: list[Any] = field(default_factory=list)
    missions: list[Any] = field(default_factory=list)
    items: list[Any] = field(default_factory=list)


def _meeting(team: SimpleNamespace, level: L, starts_at: dt.datetime, attendees: list[Any],
             title: str) -> Any:  # fmt: skip
    from apps.meetings import services as meetings
    from apps.meetings.models import Meeting

    return meetings.create_meeting(
        thesis=team.thesis, user=team.student, kind=Meeting.Kind.SUPERVISION,
        starts_at=starts_at, duration_minutes=60, location=Meeting.Location.VISIO,
        level=level, title=title, attendees=attendees,
    )  # fmt: skip


def _readers(team: SimpleNamespace, level: L) -> list[Any]:
    """The internal members (never the CSI member, never the guest) reading ``level``."""
    out = [team.student]
    for role in (R.DIRECTOR, R.CO_SUPERVISOR, R.INDUSTRIAL_TUTOR, R.INDUSTRIAL_CO_SUPERVISOR):
        if role in LEVEL_ROLES[level]:
            out.append(getattr(team, h.ROLE_ATTRS[role]))
    return out


def build_shared(team: SimpleNamespace) -> Shared:
    """Objects of every kind and level, the same in the three states."""
    from apps.conferences import services as conferences
    from apps.conferences.models import ST_LEVELS
    from apps.conferences.tests.helpers import deadline_of, default_edition, submission_of
    from apps.lifecycle import services as lifecycle
    from apps.lifecycle.models import ClosureReason
    from apps.meetings import services as meetings
    from apps.objectives import services as objectives

    shared = Shared()
    for level in L:
        shared.documents[level] = h.document(team, level, title=f"Document partagé {level}")
    for level in (L.PUBLIC, L.INTERNAL_LIRMM, L.CONFIDENTIAL):
        shared.objectives[level] = h.objective(team, level)
    goal = shared.objectives[L.PUBLIC]
    objectives.approve_objective(objective=goal, user=team.director, recent_mfa=True)
    objectives.add_comment(objective=goal, user=team.director, body="Avis partagé fictif")
    for days, level in enumerate(L, start=3):
        shared.meetings[level] = _meeting(
            team, level, h.NOW + dt.timedelta(days=days), _readers(team, level),
            "Réunion partagée fictive",
        )  # fmt: skip
    past = _meeting(team, L.PUBLIC, h.NOW - dt.timedelta(days=5), _readers(team, L.PUBLIC),
                    "Réunion passée fictive")  # fmt: skip
    meetings.add_decision(meeting=past, user=team.student, text="Décision partagée fictive")
    meetings.add_action(
        meeting=past, user=team.student, title="Action partagée fictive",
        assignee=team.student, due_date=_D(2027, 2, 15), objective=goal,
    )  # fmt: skip
    meetings.link_document(meeting=past, user=team.student,
                           document=shared.documents[L.PUBLIC])  # fmt: skip
    shared.past_meeting = past
    shared.edition = default_edition(team)
    shared.venue = shared.edition.venue
    deadline_of(team, shared.edition, day=_D(2027, 2, 12))
    for level in L:
        shared.submissions[level] = submission_of(
            team, level=level, title=f"Article partagé fictif {level}",
            st_work=level in ST_LEVELS, objective=goal,
        )  # fmt: skip
    conferences.link_document(submission=shared.submissions[L.PUBLIC], user=team.student,
                              document=shared.documents[L.PUBLIC])  # fmt: skip
    for day, (destination, payer) in enumerate(
        [("LIRMM", "LIRMM"), ("ROUSSET", "ST"), ("CONFERENCE", "LIRMM")], start=2
    ):
        shared.missions.append(h.mission(team, destination, payer, start=_D(2027, 2, day)))
    shared.items.append(h.item(team, "CIFRE", [R.INDUSTRIAL_TUTOR]))
    shared.items.append(h.item(team, "ACADEMIC", [R.DIRECTOR]))
    # M13a: a closure request (an e-mail to the director), withdrawn at once.
    request = lifecycle.request_closure(
        thesis=team.thesis, user=team.student, reason=ClosureReason.STUDENT_WITHDREW,
        recent_mfa=True,
    )  # fmt: skip
    lifecycle.withdraw_closure(request=request, user=team.student)
    return shared


def build_hidden(team: SimpleNamespace, shared: Shared, role: Role, world: str) -> Hidden:
    """Only what ``role`` does not read; world "b" adds more."""
    from apps.conferences import services as conferences
    from apps.conferences.models import ST_LEVELS
    from apps.conferences.tests.helpers import submission_of
    from apps.meetings import services as meetings
    from apps.objectives.models import OBJECTIVE_LEVELS
    from apps.vault.models import DocumentVersion

    out = Hidden()
    copies = 2 if world == "a" else 1
    goal = shared.objectives[L.PUBLIC]
    shift = 0 if world == "a" else 1
    for n in range(copies):
        for level in hidden_levels(role, Action.DOCUMENT_READ):
            doc = h.document(team, level, title=f"Caché {world}{n} {level}")
            out.documents.append(doc)
            out.versions.append(DocumentVersion.objects.get(document=doc, version=1))
            meetings.link_document(meeting=shared.past_meeting, user=team.student, document=doc)
            conferences.link_document(submission=shared.submissions[L.PUBLIC],
                                      user=team.student, document=doc)  # fmt: skip
        for i, level in enumerate(hidden_levels(role, Action.MEETING_READ)):
            starts = FIXED + dt.timedelta(days=1 + i + shift, hours=n)
            out.meetings.append(
                _meeting(team, level, starts, _readers(team, level), "Réunion cachée fictive")
            )
        for level in hidden_levels(role, Action.SUBMISSION_READ):
            out.submissions.append(submission_of(
                team, level=level, title=f"Article caché fictif {world}{n}",
                st_work=level in ST_LEVELS, objective=goal,
            ))  # fmt: skip
        for level in hidden_levels(role, Action.OBJECTIVE_READ):
            if level in OBJECTIVE_LEVELS:  # never INTERNAL_ST (M4)
                out.objectives.append(h.objective(team, level))
    for level in hidden_levels(role, Action.MEETING_READ):  # begun: linked to the goal
        past = _meeting(team, level, FIXED - dt.timedelta(days=2 + shift),
                        _readers(team, level), "Réunion cachée passée")  # fmt: skip
        meetings.add_decision(meeting=past, user=team.student, text="Décision cachée fictive")
        meetings.add_action(meeting=past, user=team.student, title="Action cachée fictive",
                            assignee=team.student, due_date=_D(2027, 1, 25 + shift),
                            objective=goal)  # fmt: skip
        out.meetings.append(past)
    for day, (destination, payer) in enumerate(hidden_destinations(role), start=8 + 3 * shift):
        out.missions.append(h.mission(team, destination, payer, start=_D(2027, 2, day)))
    for section in hidden_sections(role):
        out.items.append(h.item(team, section, [R.DIRECTOR]))
    return out


# --------------------------------------------------------------------------- rendering


@dataclass(frozen=True)
class Page:
    target: str
    status: int
    content_type: str
    body: str


def render(user: Any, targets: list[str]) -> list[Page]:
    client = h.client_for(user)  # logged in at the rendering instant
    out = []
    for target in targets:
        response = client.get(target)
        body = _CSRF.sub(r'\1"-"', response.content.decode("utf-8", errors="replace"))
        out.append(Page(target, response.status_code, response.get("Content-Type", ""), body))
    return out


def mails_of(user: Any) -> list[tuple[str, str]]:
    return [(str(m.subject), str(m.body)) for m in mail.outbox if user.email in m.to]


def assert_same(left: list[Page], right: list[Page], what: str) -> None:
    for a, b in zip(left, right, strict=True):
        if (a.status, a.content_type, a.body) != (b.status, b.content_type, b.body):
            diff = "\n".join(difflib.unified_diff(a.body.splitlines(), b.body.splitlines(),
                                                  lineterm="", n=1))  # fmt: skip
            raise AssertionError(f"{what}: {a.target} ({a.status} / {b.status})\n{diff}")


def by_target(pages: list[Page]) -> dict[str, Page]:
    return {p.target: p for p in pages}


# --------------------------------------------------------------------------- the test


@dataclass(frozen=True)
class Module:
    """A module's surfaces. ``surfaces`` renders every GET route at least once;
    ``expected`` gives (url, needle) a reader (``read_action``) must see: 200 and
    the needle; ``probes`` gives (hidden url, the same
    url with an unknown id) pairs."""

    namespace: str
    read_action: Action | None
    surfaces: Callable[[SimpleNamespace, Shared], list[str]]
    expected: Callable[[SimpleNamespace, Shared, Role], list[tuple[str, str]]]
    probes: Callable[[SimpleNamespace, Hidden], list[tuple[str, str]]] = lambda team, hidden: []
    only: frozenset[str] | None = None


def reads(role: Role, action: Action | None) -> bool:
    return action is None or action in ROLE_ACTIONS[role]


def run(module: Module, role: Role, time_machine: Any) -> None:
    time_machine.move_to(h.NOW, tick=True)
    team = h.make_team()
    shared = build_shared(team)
    user = getattr(team, h.ROLE_ATTRS[role])
    targets = module.surfaces(team, shared)
    assert_every_route_is_rendered(module.namespace, targets, module.only)
    # The control: the student's pages of the module and the dashboard (a module
    # such as « Fin de thèse » shows no hidden object, the dashboard does).
    plain = [t for t in targets if "?" not in t and resolve(urlsplit(t).path).kwargs.keys()
             == {"thesis_id"}] + [url(team, "accounts:dashboard")]  # fmt: skip

    time_machine.move_to(FIXED, tick=False)
    states = [render(user, targets)]
    mails = [mails_of(user)]
    student = [render(team.student, plain)]

    # Not vacuous: the pages a reader must see, with their content (a role without
    # the module's read action is only compared).
    pages = by_target(states[0])
    if reads(role, module.read_action):
        for target, needle in module.expected(team, shared, role):
            page = pages.get(target) or render(user, [target])[0]
            assert page.status == 200, (target, page.status)
            assert needle in page.body, (target, needle)

    # E-mails: the shared objects wrote to the director (M5a invitation, M13a
    # closure request); the hidden ones write to their own readers (below).
    if role is R.DIRECTOR:
        assert mails[0], "the director received no e-mail of the shared objects"
    sent_before = len(mail.outbox)

    hidden = Hidden()
    for world, hours in (("a", 3), ("b", 2)):
        time_machine.move_to(FIXED - dt.timedelta(hours=hours), tick=True)
        made = build_hidden(team, shared, role, world)
        for name in vars(hidden):
            getattr(hidden, name).extend(getattr(made, name))
        time_machine.move_to(FIXED, tick=False)
        states.append(render(user, targets))
        mails.append(mails_of(user))
        if world == "a":
            student.append(render(team.student, plain))

    for left, right in itertools.pairwise(states):
        assert_same(left, right, f"{role.value} pages")
    assert mails[0] == mails[1] == mails[2], f"{role.value}: e-mails"
    assert len(mail.outbox) > sent_before, "the hidden objects sent no e-mail at all"
    # The control: the student reads the hidden objects, so the worlds differ.
    assert any(a.body != b.body for a, b in zip(student[0], student[1], strict=True)), (
        "the student's pages do not show the hidden objects: the worlds are empty"
    )
    # An id of a hidden object answers like an unknown one.
    for hidden_url, unknown_url in module.probes(team, hidden):
        hidden_id = next(s for s in urlsplit(hidden_url).path.split("/") if s
                         and s not in urlsplit(unknown_url).path.split("/"))  # fmt: skip
        (a,) = render(user, [hidden_url])
        (b,) = render(user, [unknown_url])
        assert (a.status, a.body.replace(hidden_id, UNKNOWN)) == (b.status, b.body), hidden_url


def url(team: SimpleNamespace, name: str, **kw: Any) -> str:
    return reverse(name, kwargs={"thesis_id": team.thesis.pk, **kw})


def unknown(team: SimpleNamespace, name: str, kwarg: str) -> str:
    return url(team, name, **{kwarg: UNKNOWN})
