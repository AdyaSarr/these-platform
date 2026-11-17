"""The demonstration thesis, written through the services exactly as people would.

Every write goes through a service (tests/test_structure.py): the audit chain,
encryption and permission checks are those of real use. Two things differ:

- The CLOCK. Each step runs at its own simulated instant (time-machine, Python
  clock only: no audited path reads PostgreSQL's clock), in chronological order,
  so that objectives and memberships exist for months and the progress chart has
  a real curve. Every step must end within STEP_WINDOW of its start.
- WHO ACTS. The command acts in each person's name. Where a view would require a
  second factor less than 10 minutes old (approving objectives, acknowledging,
  deciding a mission approval, setting a travel budget, a target or the rules),
  the command states it (``recent_mfa=True``): those events are attributed to
  fictional people of a demonstration database, and every event carries
  ``details.source = "demo_seed"`` (vault.audit_source).

No password and no TOTP secret is generated: accounts are created by the real
invitation flow with an unusable password (accounts.services.accept_invitation_
new_user(password=None)); at the end, at the real time, the real password-reset
task gives one link per person. Invitation tokens never leave the process
(in-memory mailbox, consumed at once).

Dates are anchored on today: the CIFRE start is the first day of the month five
months ago, so the demonstration is always in the middle of year 1.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import time_machine
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test.utils import override_settings
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts import tasks as account_tasks
from apps.accounts.forms import InviteForm
from apps.accounts.models import Invitation, Organization, Thesis, User
from apps.accounts.roles import Role
from apps.admin_file import services as admin_file
from apps.admin_file.models import DueDateReason, Reason, RequiredItem
from apps.conferences import services as conferences
from apps.conferences.models import Edition, StApproval, Submission, Venue
from apps.core.confidentiality import ConfidentialityLevel as Level
from apps.meetings import services as meetings
from apps.meetings.models import Meeting, MinutesAcknowledgement
from apps.objectives import services as objectives
from apps.objectives.models import Category, Objective, Status
from apps.objectives.years import add_years
from apps.travel import holidays as travel_holidays
from apps.travel import services as travel
from apps.travel.models import Expense, Mission, PresenceDay
from apps.vault import services as vault
from apps.vault.models import AuditEvent, Document, DocumentVersion

from . import guard
from .pdf import fictional_pdf

DEMO_TITLE = "Thèse de démonstration : contre-mesures pour un circuit fictif"
AUDIT_SOURCE = "demo_seed"
STEP_WINDOW = dt.timedelta(minutes=30)
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
_INVITATION_LINK = re.compile(r"/invitation/([A-Za-z0-9_-]{43})/")
_ANY_LINK = re.compile(r"https?://\S+")


class ScenarioError(Exception):
    """demo_seed refused or could not complete. The message is safe to print."""


class AlreadySeededError(ScenarioError):
    pass


# --------------------------------------------------------------------------- people


@dataclass(frozen=True)
class Person:
    role: Role
    first_name: str
    last_name: str
    email: str
    organization: str


COMPANY = "Entreprise fictive (démo)"
LAB = "Laboratoire fictif (démo)"
UNIVERSITY = "Université fictive (démo)"
ORGANIZATION_KINDS = {
    COMPANY: Organization.Kind.ST,
    LAB: Organization.Kind.LIRMM,
    UNIVERSITY: Organization.Kind.UNIVERSITY,
}

STUDENT = Person(Role.PHD_STUDENT, "Alix", "Exemple", "alix.exemple@example.org", COMPANY)
DIRECTOR = Person(Role.DIRECTOR, "Camille", "Fictif", "camille.fictif@example.org", LAB)
CO_SUPERVISOR = Person(Role.CO_SUPERVISOR, "Noé", "Démo", "noe.demo@example.org", LAB)
TUTOR = Person(Role.INDUSTRIAL_TUTOR, "Sacha", "Témoin", "sacha.temoin@example.org", COMPANY)
COMPANY_CO_SUPERVISOR = Person(
    Role.INDUSTRIAL_CO_SUPERVISOR, "Maé", "Essai", "mae.essai@example.org", COMPANY
)
CSI = Person(Role.CSI_MEMBER, "Lou", "Modèle", "lou.modele@example.org", UNIVERSITY)
PEOPLE = (STUDENT, DIRECTOR, CO_SUPERVISOR, TUTOR, COMPANY_CO_SUPERVISOR, CSI)


# --------------------------------------------------------------------------- objectives


@dataclass(frozen=True)
class ObjectiveSpec:
    key: str
    title: str
    category: Category
    weight: int
    level: Level
    due: tuple[int, int]  # (month after the CIFRE start, day; 31 = last day)
    description: str = "Objectif fictif de démonstration."
    criterion: str = "Validé par l'encadrement (démonstration)."


YEAR_1 = (
    ObjectiveSpec("sota", "État de l'art (démonstration)", Category.RESEARCH, 2, Level.PUBLIC,
                  (3, 30)),
    ObjectiveSpec("bench", "Banc de simulation (démonstration)", Category.RESEARCH, 3,
                  Level.INTERNAL_LIRMM, (4, 31)),
    ObjectiveSpec("counter", "Contre-mesure sur un cas d'école fictif", Category.RESEARCH, 3,
                  Level.CONFIDENTIAL, (9, 31)),
    ObjectiveSpec("training", "Formations doctorales : 30 heures", Category.TRAINING, 1,
                  Level.PUBLIC, (11, 15)),
    ObjectiveSpec("paper", "Article de workshop (démonstration)", Category.PUBLICATION, 2,
                  Level.PUBLIC, (10, 30)),
)  # fmt: skip
SIDE_TOOL = ObjectiveSpec(
    "tool", "Outil annexe (démonstration)", Category.OTHER, 1, Level.INTERNAL_LIRMM, (7, 28)
)

# Progress at the end of each month after the start (month index -> key -> (%, status)).
_IP, _OK, _PART = Status.IN_PROGRESS.value, Status.ACHIEVED.value, Status.PARTIAL.value
MONTHLY_PROGRESS: dict[int, dict[str, tuple[int, str]]] = {
    0: {"sota": (30, _IP), "bench": (10, _IP)},
    1: {"sota": (60, _IP), "bench": (25, _IP), "counter": (10, _IP)},
    2: {"sota": (85, _IP), "bench": (40, _IP), "counter": (20, _IP)},
    3: {"sota": (100, _OK), "bench": (55, _IP), "counter": (30, _IP), "training": (20, _IP)},
    4: {"bench": (70, _PART), "counter": (45, _IP), "training": (40, _IP)},
}
RECENT_PROGRESS = {"counter": (50, _IP), "training": (50, _IP)}
DIRECTOR_COMMENT = "Commentaire fictif : bonne piste, à confirmer."
CO_SUPERVISOR_COMMENT = "Commentaire fictif : prévoir un second scénario."
COMPANY_CO_SUPERVISOR_COMMENT = "Commentaire fictif : cadrer le périmètre avec l'équipe."
CHARTER_REJECTION = "Signature du directeur manquante en page 2 (document fictif)."
CONTRACT_REOPENING = "Avenant fictif au contrat (démonstration)."
DUE_DATE_PROPOSAL = "Convention en attente de l'école doctorale (fictif)."


# --------------------------------------------------------------------------- results


@dataclass(frozen=True)
class StepWindow:
    name: str
    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class AccountLink:
    role: Role
    name: str
    email: str
    url: str


@dataclass(frozen=True)
class SeedResult:
    thesis_id: str
    steps: tuple[StepWindow, ...]
    links: tuple[AccountLink, ...]


# --------------------------------------------------------------------------- meetings

TUTOR_MEETING: dict[str, object] = {
    "kind": "ST_TUTOR",
    "duration_minutes": 60,
    "location": "ROUSSET",
    "level": Level.INTERNAL_ST,
    "title": "Point mensuel avec le tuteur (fictif)",
    "location_detail": "Salle fictive B",
    "attendees": [Role.PHD_STUDENT, Role.INDUSTRIAL_TUTOR, Role.INDUSTRIAL_CO_SUPERVISOR],
}
TUTOR_AGENDA = "1. Accès au banc fictif\n2. Calendrier des mesures fictives"
TUTOR_MINUTES = (
    "Compte rendu fictif : le banc est disponible deux jours par semaine ; "
    "les mesures se font sur le circuit fictif uniquement."
)
TRIPARTITE_MEETING: dict[str, object] = {
    "kind": "TRIPARTITE",
    "duration_minutes": 90,
    "location": "VISIO",
    "level": Level.CONFIDENTIAL,
    "title": "Réunion tripartite du trimestre (fictive)",
    "visio_link": "https://visio.example.org/tripartite-fictive",
    "attendees": [Role.PHD_STUDENT, Role.DIRECTOR, Role.INDUSTRIAL_TUTOR],
}
TRIPARTITE_AGENDA = "1. Bilan du trimestre (fictif)\n2. Stratégie de publication"
TRIPARTITE_MINUTES = (
    "Compte rendu fictif : bilan conforme au plan ; un premier article est envisagé "
    "après la relecture de l'entreprise fictive."
)
SEMINAR: dict[str, object] = {
    "kind": "PRESENTATION",
    "presentation_venue": "TEAM_SEMINAR",
    "duration_minutes": 45,
    "location": "MONTPELLIER",
    "level": Level.PUBLIC,
    "title": "Séminaire d'équipe : présentation du sujet (fictive)",
    "attendees": [Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR],
}
SEMINAR_MINUTES = "Présentation fictive du sujet ; questions sur le modèle de menace fictif."


# --------------------------------------------------------------------------- dates


def _month(anchor: dt.date, k: int) -> dt.date:
    index = anchor.year * 12 + anchor.month - 1 + k
    return dt.date(index // 12, index % 12 + 1, 1)


def _day(anchor: dt.date, k: int, day: int) -> dt.date:
    first = _month(anchor, k)
    last = _month(anchor, k + 1) - dt.timedelta(days=1)
    return first.replace(day=min(day, last.day))


def _at(anchor: dt.date, k: int, day: int, hour: int) -> dt.datetime:
    moment = dt.datetime.combine(_day(anchor, k, day), dt.time(hour))
    return timezone.make_aware(moment)  # Europe/Paris (TIME_ZONE)


def working_day(day: dt.date, *, back: bool) -> dt.date:
    """``day``, or the nearest working day (Monday to Friday): back to the Friday
    for a past date (it stays before what follows it in the scenario), on to the
    Monday for a future one (it stays to come)."""
    step = dt.timedelta(days=-1 if back else 1)
    while day.weekday() >= 5:
        day += step
    return day


def working_week_start(start: dt.date, length: int, *, back: bool) -> dt.date:
    """A start such that the ``length`` days from it are all working days: moved
    back (past mission) or on (future mission), one day at a time."""
    step = dt.timedelta(days=-1 if back else 1)
    while any((start + dt.timedelta(days=n)).weekday() >= 5 for n in range(length)):
        start += step
    return start


def anchor_for(today: dt.date) -> dt.date:
    """The CIFRE start of the demonstration: first day of the month 5 months ago."""
    return _month(today.replace(day=1), -5)


# --------------------------------------------------------------------------- run


def _upload(title: str) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        "document-fictif.pdf", fictional_pdf(title), content_type="application/pdf"
    )


@contextmanager
def _in_memory_mailbox() -> Iterator[list[mail.EmailMessage]]:
    with override_settings(EMAIL_BACKEND=LOCMEM):
        if getattr(mail, "outbox", None) is None:
            mail.outbox = []
        if mail.outbox:
            raise ScenarioError("the in-memory mailbox is not empty")
        try:
            yield mail.outbox
        finally:
            mail.outbox.clear()  # no link stays behind in memory


def _take_one_link(outbox: list[mail.EmailMessage], email: str, pattern: re.Pattern[str]) -> str:
    messages = [m for m in outbox if m.to == [email]]
    if len(messages) != 1 or len(outbox) != 1:
        raise ScenarioError("expected exactly one e-mail in the in-memory mailbox")
    match = pattern.search(str(messages[0].body))
    outbox.clear()
    if match is None:
        raise ScenarioError("no link in the e-mail")
    return match.group(1) if match.groups() else match.group(0)


@dataclass
class _Run:
    anchor: dt.date
    real_now: dt.datetime
    outbox: list[mail.EmailMessage]
    steps: list[StepWindow] = field(default_factory=list)
    users: dict[Role, User] = field(default_factory=dict)
    thesis: Thesis | None = None
    objectives: dict[str, Objective] = field(default_factory=dict)
    note: Document | None = None
    meetings: dict[str, Meeting] = field(default_factory=dict)
    venues: dict[str, Venue] = field(default_factory=dict)
    editions: dict[str, Edition] = field(default_factory=dict)
    submissions: dict[str, Submission] = field(default_factory=dict)
    articles: dict[str, Document] = field(default_factory=dict)
    summary: Document | None = None
    missions: dict[str, Mission] = field(default_factory=dict)

    # ---------------------------------------------------------------- machinery

    def step(self, name: str, when: dt.datetime, *actions: Callable[[], object]) -> None:
        if self.steps and when <= self.steps[-1].end:
            raise ScenarioError(f"step {name}: not after the previous step")
        if when + STEP_WINDOW >= self.real_now:
            raise ScenarioError(f"step {name}: in the future")
        with time_machine.travel(when, tick=True):
            for action in actions:
                action()
            # Notifications of the step (reopening, due dates) went to the in-memory
            # mailbox too: fictional people, nothing to read; the next invitation
            # link must be the only e-mail there.
            self.outbox.clear()
            if timezone.now() >= when + STEP_WINDOW:
                raise ScenarioError(f"step {name}: took longer than {STEP_WINDOW}")
        self.steps.append(StepWindow(name, when, when + STEP_WINDOW))

    def at(self, k: int, day: int, hour: int) -> dt.datetime:
        return _at(self.anchor, k, day, hour)

    def meeting_at(self, k: int, day: int, hour: int) -> dt.datetime:
        """A past meeting's start, on a working day (never a weekend, point 13)."""
        moment = dt.datetime.combine(
            working_day(_day(self.anchor, k, day), back=True), dt.time(hour)
        )
        return timezone.make_aware(moment)

    def past_range(self, k: int, day: int, length: int) -> dict[str, dt.date]:
        """A past mission's dates, all within one working week."""
        start = working_week_start(_day(self.anchor, k, day), length, back=True)
        return {"start_date": start, "end_date": start + dt.timedelta(days=length - 1)}

    def month_end(self, k: int) -> dt.datetime:
        return self.at(k, 31, 18)

    @property
    def t(self) -> Thesis:
        if self.thesis is None:
            raise ScenarioError("no thesis yet")
        return self.thesis

    def item(self, code: str) -> RequiredItem:
        student = self.users[Role.PHD_STUDENT]
        return admin_file.visible_items(student, self.t).get(code=code)

    # ---------------------------------------------------------------- team

    def _join(self, person: Person, invitation: Invitation) -> None:
        """What the invitation link page then the account form do."""
        token = _take_one_link(self.outbox, invitation.email, _INVITATION_LINK)
        checked = accounts.check_token(token)
        self.users[person.role] = accounts.accept_invitation_new_user(
            checked.pk,
            first_name=person.first_name,
            last_name=person.last_name,
            password=None,  # unusable: set later through the reset link
        )

    def create_thesis(self) -> None:
        call_command("vault_init", stdout=_Discard())
        invitation = accounts.create_thesis(
            title=DEMO_TITLE,
            cifre_start=self.anchor,
            first_enrollment=_month(self.anchor, 4),
            planned_end=add_years(self.anchor, 3) - dt.timedelta(days=1),
            student_email=STUDENT.email,
            student_organization=STUDENT.organization,
            student_organization_kind=ORGANIZATION_KINDS[STUDENT.organization],
        )
        self.thesis = Thesis.objects.get(pk=invitation.thesis_id)
        self._join(STUDENT, invitation)

    def invite(self, *people: Person) -> None:
        """The team page: InviteForm (which resolves the organization) then the service."""
        for person in people:
            form = InviteForm(
                data={
                    "email": person.email,
                    "role": person.role.value,
                    "new_organization": person.organization,
                    "new_organization_kind": ORGANIZATION_KINDS[person.organization],
                }
            )
            if not form.is_valid():
                raise ScenarioError(f"invitation form refused: {form.errors.as_text()}")
            invitation = accounts.create_invitation(
                thesis=self.t,
                email=form.cleaned_data["email"],
                role=Role(form.cleaned_data["role"]),
                organization=form.resolved_organization(),
                by=self.users[Role.PHD_STUDENT],
            )
            self._join(person, invitation)

    # ---------------------------------------------------------------- file

    def submit(self, code: str, attachment: str | None) -> None:
        upload = _upload(attachment) if attachment else None
        admin_file.submit(item=self.item(code), user=self.users[Role.PHD_STUDENT], upload=upload)

    def approve(self, role: Role, *codes: str) -> None:
        for code in codes:
            admin_file.approve(item=self.item(code), user=self.users[role])

    def reopen_contract(self) -> None:
        """The student reopens a piece the tutor had validated: the tutor is notified."""
        admin_file.reopen(
            item=self.item("employment_contract"),
            user=self.users[Role.PHD_STUDENT],
            reason=Reason.AMENDMENT,
            comment=CONTRACT_REOPENING,
        )

    def propose_due_date(self) -> None:
        """A pending proposal for the director (training agreement, academic part)."""
        item = self.item("training_agreement")
        if item.due_date is None:
            raise ScenarioError("the training agreement has a due date (catalogue)")
        admin_file.propose_due_date(
            item=item,
            user=self.users[Role.PHD_STUDENT],
            due_date=item.due_date + dt.timedelta(days=30),
            reason=DueDateReason.WAITING_THIRD_PARTY,
            comment=DUE_DATE_PROPOSAL,
        )

    def reject_charter(self) -> None:
        admin_file.reject(
            item=self.item("doctoral_charter"),
            user=self.users[Role.DIRECTOR],
            reason=Reason.MISSING_SIGNATURE,
            comment=CHARTER_REJECTION,
        )

    # ---------------------------------------------------------------- objectives

    def create_objectives(self, *specs: ObjectiveSpec) -> None:
        year = objectives.current_year(self.t)
        if year is None or year.number != 1:
            raise ScenarioError("the demonstration runs in CIFRE year 1")
        for spec in specs:
            self.objectives[spec.key] = objectives.create_objective(
                thesis=self.t,
                user=self.users[Role.PHD_STUDENT],
                year=year,
                title=spec.title,
                description=spec.description,
                success_criterion=spec.criterion,
                category=spec.category,
                due_date=_day(self.anchor, *spec.due),
                weight=spec.weight,
                level=spec.level,
            )

    def approve_year(self) -> None:
        year = objectives.current_year(self.t)
        if year is None:
            raise ScenarioError("no current year")
        # recent_mfa=True: stated by the command for the fictional director (docstring).
        objectives.approve_year(year=year, user=self.users[Role.DIRECTOR], recent_mfa=True)

    def approve_objective(self, key: str) -> None:
        objectives.approve_objective(
            objective=self.objectives[key], user=self.users[Role.DIRECTOR], recent_mfa=True
        )

    def progress(self, values: dict[str, tuple[int, str]]) -> None:
        for key, (percent, status) in values.items():
            objectives.add_progress(
                objective=self.objectives[key],
                user=self.users[Role.PHD_STUDENT],
                date=timezone.localdate(),
                progress=percent,
                status=status,
                note="Point d'avancement fictif (démonstration).",
            )

    def comment(self, role: Role, key: str, body: str) -> None:
        objectives.add_comment(objective=self.objectives[key], user=self.users[role], body=body)

    def acknowledge(self, key: str) -> None:
        objectives.acknowledge_changes(
            objective=self.objectives[key], user=self.users[Role.DIRECTOR], recent_mfa=True
        )

    def drop(self, key: str) -> None:
        objectives.drop_objective(
            objective=self.objectives[key],
            user=self.users[Role.PHD_STUDENT],
            reason="Abandonné au profit du banc de simulation (démonstration).",
        )

    # ---------------------------------------------------------------- documents

    def store(self, title: str, level: Level) -> Document:
        return vault.store(
            thesis=self.t,
            upload=_upload(title),
            title=title,
            level=level,
            user=self.users[Role.PHD_STUDENT],
        )

    def store_note(self) -> None:
        self.note = self.store("Note de travail (fictive)", Level.CONFIDENTIAL)

    def new_note_version(self) -> None:
        if self.note is None:
            raise ScenarioError("no note yet")
        vault.add_version(
            document=self.note,
            upload=_upload("Note de travail, version 2 (fictive)"),
            user=self.users[Role.PHD_STUDENT],
        )

    # ---------------------------------------------------------------- meetings

    def meeting(self, key: str, role: Role, starts_at: dt.datetime, **fields: object) -> None:
        roles = fields.pop("attendees")
        self.meetings[key] = meetings.create_meeting(
            thesis=self.t,
            user=self.users[role],
            starts_at=starts_at,
            attendees=[self.users[r] for r in roles],  # type: ignore[attr-defined]
            **fields,  # type: ignore[arg-type]
        )

    def agenda(self, key: str, role: Role, text: str) -> None:
        meetings.save_agenda(meeting=self.meetings[key], user=self.users[role], agenda=text)

    def minutes(self, key: str, role: Role, text: str) -> None:
        meeting = Meeting.objects.get(pk=self.meetings[key].pk)
        meetings.save_minutes(
            meeting=meeting, user=self.users[role], minutes=text, version=meeting.minutes_version
        )

    def decide(self, key: str, role: Role, text: str) -> None:
        meetings.add_decision(meeting=self.meetings[key], user=self.users[role], text=text)

    def assign(
        self, key: str, role: Role, title: str, assignee: Role, due: dt.date, objective: str = ""
    ) -> None:
        meetings.add_action(
            meeting=self.meetings[key],
            user=self.users[role],
            title=title,
            assignee=self.users[assignee],
            due_date=due,
            objective=self.objectives[objective] if objective else None,
        )

    def mark(self, key: str, role: Role, kind: str) -> None:
        meeting = Meeting.objects.get(pk=self.meetings[key].pk)
        # recent_mfa=True for an approval: stated by the command (module docstring).
        meetings.acknowledge(
            meeting=meeting,
            user=self.users[role],
            kind=kind,
            version=meeting.minutes_version,
            recent_mfa=kind == MinutesAcknowledgement.Kind.APPROVED,
        )

    def finish_tutor_action(self) -> None:
        action = self.meetings["tutor"].actions.get(assignee=self.users[Role.INDUSTRIAL_TUTOR])
        meetings.set_action_status(
            action=action, user=self.users[Role.INDUSTRIAL_TUTOR], status="DONE"
        )

    def link_summary(self) -> None:
        if self.summary is None:
            raise ScenarioError("no public summary yet")
        meetings.link_document(
            meeting=self.meetings["seminar"],
            user=self.users[Role.PHD_STUDENT],
            document=self.summary,
        )

    def store_summary(self) -> None:
        self.summary = self.store("Résumé public du sujet (fictif)", Level.PUBLIC)

    def upcoming(self) -> None:
        """Meetings to come, a few days after the real time (created at month 4)."""
        student, director, co = Role.PHD_STUDENT, Role.DIRECTOR, Role.CO_SUPERVISOR
        today = timezone.localdate(self.real_now)

        def on(days: int, hour: int) -> dt.datetime:
            # Never a weekend: on to the Monday (it stays to come).
            day = working_day(today + dt.timedelta(days=days), back=False)
            return timezone.make_aware(dt.datetime.combine(day, dt.time(hour)))

        self.meeting(
            "supervision",
            student,
            on(2, 10),
            kind=Meeting.Kind.SUPERVISION,
            duration_minutes=60,
            location=Meeting.Location.VISIO,
            level=Level.INTERNAL_LIRMM,
            title="Point d'encadrement : banc de mesure (fictif)",
            visio_link="https://visio.example.org/point-fictif",
            attendees=[student, director, co],
        )
        self.agenda(
            "supervision",
            student,
            "1. Résultats fictifs du banc\n2. Plan du chapitre d'état de l'art",
        )
        self.meetings["tutor-next"] = meetings.duplicate_meeting(
            meeting=self.meetings["tutor"],
            user=self.users[student],
            starts_at=on(9, 14),
        )
        self.meeting(
            "csi",
            student,
            on(20, 9),
            kind=Meeting.Kind.CSI,
            duration_minutes=120,
            location=Meeting.Location.MONTPELLIER,
            level=Level.PUBLIC,
            title="Comité de suivi individuel, année 1 (fictif)",
            attendees=[student, director],
        )
        self.agenda("csi", student, "Présentation de l'avancement (fictive), puis échanges.")

    # ---------------------------------------------------------------- conferences

    def _venue(self, key: str, kind: str, acronym: str, name: str) -> None:
        self.venues[key] = conferences.create_venue(
            thesis=self.t,
            user=self.users[Role.PHD_STUDENT],
            kind=kind,
            acronym=acronym,
            name=name,
            url=f"https://{acronym.lower()}.example.org/",
        )

    def _edition(self, key: str, venue: str, year: int, location: str) -> None:
        self.editions[key] = conferences.create_edition(
            venue=self.venues[venue],
            user=self.users[Role.PHD_STUDENT],
            year=year,
            location=location,
        )

    def _deadline(self, edition: str, kind: str, day: dt.date, tz_name: str = "AoE") -> None:
        conferences.add_deadline(
            edition=self.editions[edition],
            user=self.users[Role.PHD_STUDENT],
            kind=kind,
            day=day,
            wall=dt.time(23, 59),
            tz_name=tz_name,
        )

    def catalogue(self) -> None:
        a = self.anchor
        self._venue("sfca", "CONFERENCE", "SFCA", "Symposium fictif sur les canaux auxiliaires")
        self._venue("afpq", "WORKSHOP", "AFPQ", "Atelier fictif de cryptographie post-quantique")
        self._venue("rfsm", "JOURNAL", "RFSM", "Revue fictive de sécurité matérielle")
        self._edition("sfca", "sfca", _day(a, 3, 1).year, "Ville fictive A")
        self._deadline("sfca", "ABSTRACT", _day(a, 2, 20))
        self._deadline("sfca", "PAPER", _day(a, 3, 1))
        self._deadline("sfca", "NOTIFICATION", _day(a, 4, 1))
        self._deadline("sfca", "CAMERA_READY", _day(a, 4, 25), "America/Los_Angeles")
        self._edition("afpq", "afpq", _day(a, 2, 15).year, "Ville fictive B")
        self._deadline("afpq", "PAPER", _day(a, 2, 15))
        self._deadline("afpq", "NOTIFICATION", _day(a, 3, 15))

    def submission(self, key: str, edition: str, title: str, objective: str = "") -> None:
        student = self.users[Role.PHD_STUDENT]
        target = self.editions[edition]
        self.submissions[key] = conferences.create_submission(
            thesis=self.t,
            user=student,
            venue=target.venue,
            edition=target,
            title=title,
            abstract="Résumé fictif (démonstration).",
            external_authors="Co-autrice fictive",
            level=Level.CONFIDENTIAL,
            objective=self.objectives[objective] if objective else None,
            authors=[self.users[Role.INDUSTRIAL_TUTOR]],
        )
        self.articles[key] = self.store(f"Article fictif {key.upper()}", Level.CONFIDENTIAL)
        conferences.link_document(
            submission=self.submissions[key], user=student, document=self.articles[key]
        )

    def _current_version(self, key: str) -> DocumentVersion:
        document = Document.objects.get(pk=self.articles[key].pk)
        return DocumentVersion.objects.get(document=document, version=document.current_version)

    def request(self, key: str) -> None:
        conferences.request_approval(
            submission=self.submissions[key],
            user=self.users[Role.PHD_STUDENT],
            version=self._current_version(key),
        )

    def st_decide(self, key: str, decision: str, reason: str = "", comment: str = "") -> None:
        approval = StApproval.objects.get(submission=self.submissions[key], decision="PENDING")
        # recent_mfa=True: stated by the command for the fictional tutor (docstring).
        conferences.decide_approval(
            approval=approval,
            user=self.users[Role.INDUSTRIAL_TUTOR],
            decision=decision,
            reason=reason,
            comment=comment,
            recent_mfa=True,
        )

    def new_article_version(self, key: str) -> None:
        vault.add_version(
            document=self.articles[key],
            upload=_upload(f"Article fictif {key.upper()}, version 2"),
            user=self.users[Role.PHD_STUDENT],
        )

    def set_status(self, key: str, status: str, *, with_version: bool = False) -> None:
        conferences.set_status(
            submission=self.submissions[key],
            user=self.users[Role.PHD_STUDENT],
            status=status,
            version=self._current_version(key) if with_version else None,
        )

    def pending_submission(self) -> None:
        """A submission awaiting ST's approval, for a deadline 40 days after the real time."""
        due = timezone.localdate(self.real_now) + dt.timedelta(days=40)
        self._venue("cfse", "CONFERENCE", "CFSE", "Colloque fictif sur la sécurité embarquée")
        self._edition("cfse", "cfse", due.year, "Ville fictive C")
        self._deadline("cfse", "PAPER", due)
        self.submission("c", "cfse", "Contre-mesure fictive pour un cas d'école")
        self.request("c")

    # ---------------------------------------------------------------- travel (M7)

    def travel_settings(self) -> None:
        """Target and monthly rule (the supervisors), budgets (each payer's person) for
        every calendar year of the demonstration. Fictitious amounts. recent_mfa=True:
        stated by the command for the fictional supervisors (docstring)."""
        tutor, director = self.users[Role.INDUSTRIAL_TUTOR], self.users[Role.DIRECTOR]
        travel.set_target(thesis=self.t, user=tutor, recent_mfa=True, cifre_year=1, st_percent=70)
        travel.set_rules(thesis=self.t, user=director, recent_mfa=True, min_lab_days_per_month=2)
        for year in range(self.anchor.year, timezone.localdate(self.real_now).year + 1):
            travel.set_budget(
                thesis=self.t, user=tutor, payer="ST", year=year, amount="3500", recent_mfa=True
            )
            travel.set_budget(
                thesis=self.t, user=director, payer="LIRMM", year=year, amount="2800",
                recent_mfa=True,
            )  # fmt: skip

    def presence(self, k: int, *, leave: tuple[int, int] | None = None) -> None:
        """The working days of month ``k`` not filled in yet (by a mission): Rousset,
        remote on Thursdays, a LIRMM day a fortnight, leave on ``leave`` (days)."""
        student = self.users[Role.PHD_STUDENT]
        first = _month(self.anchor, k)
        last = _day(self.anchor, k, 31)
        filled = set(
            PresenceDay.objects.filter(thesis=self.t, day__range=(first, last)).values_list(
                "day", flat=True
            )
        )
        for day in travel_holidays.working_days(first, last):
            if day in filled:
                continue
            if leave and leave[0] <= day.day <= leave[1]:
                kind = "LEAVE"
            elif day.weekday() == 3:
                kind = "REMOTE"
            elif day.weekday() == 1 and day.isocalendar().week % 2 == 0:
                kind = "LIRMM"
            else:
                kind = "ROUSSET"
            travel.set_presence(thesis=self.t, user=student, first=day, last=day, kind=kind)

    def _mission(self, key: str, lines: list[tuple[str, str, bool]], **fields: Any) -> None:
        student = self.users[Role.PHD_STUDENT]
        self.missions[key] = travel.create_mission(thesis=self.t, user=student, **fields)
        for category, estimated, flat_rate in lines:
            travel.add_expense(
                mission=self.missions[key],
                user=student,
                category=category,
                estimated=estimated,
                flat_rate=flat_rate,
            )

    def _request(self, key: str, total: str) -> None:
        travel.request_order(
            mission=self.missions[key], user=self.users[Role.PHD_STUDENT], estimated_total=total
        )

    def _decide_order(self, key: str, role: Role, decision: str, **kw: Any) -> None:
        order = self.missions[key].orders.get(decision="PENDING")
        # recent_mfa=True: stated by the command for the fictional supervisors (docstring).
        travel.decide_order(
            order=order, user=self.users[role], decision=decision, recent_mfa=True, **kw
        )

    def lab_mission(self) -> None:
        self._mission(
            "lab",
            [("TRANSPORT", "95", False), ("LODGING", "180", False), ("MEALS", "45", True)],
            **self.past_range(1, 14, 3),
            destination="LIRMM",
            payer="LIRMM",
            transports=["TRAIN", "URBAN_TRANSIT"],
            lodging="GUESTHOUSE",
            purpose="Séjour fictif au laboratoire : séances de travail (démonstration).",
        )
        self._request("lab", "320")

    def official_order(self) -> None:
        travel.attach_official_order(
            mission=self.missions["lab"],
            user=self.users[Role.PHD_STUDENT],
            upload=_upload("Ordre de mission officiel (fictif)"),
        )

    def _actuals(self, key: str, actuals: dict[str, str], receipts: set[str]) -> None:
        student = self.users[Role.PHD_STUDENT]
        for expense in Expense.objects.filter(mission=self.missions[key]).order_by("created_at"):
            travel.update_expense(expense=expense, user=student, actual=actuals[expense.category])
            if expense.category in receipts:
                travel.attach_receipt(
                    expense=expense, user=student, upload=_upload(f"Justificatif fictif {key}")
                )

    def lab_back(self) -> None:
        travel.mark_done(mission=self.missions["lab"], user=self.users[Role.PHD_STUDENT])
        self._actuals(
            "lab",
            {"TRANSPORT": "92,40", "LODGING": "180", "MEALS": "45"},
            receipts={"TRANSPORT", "LODGING"},
        )

    def refused_mission(self) -> None:
        self._mission(
            "refused",
            [("TRANSPORT", "95", False)],
            **self.past_range(2, 6, 2),
            destination="LIRMM",
            payer="ST",
            purpose="Séjour fictif demandé sur le budget ST (démonstration).",
            fill_presence=False,
        )
        self._request("refused", "95")

    def conference_mission(self) -> None:
        self._mission(
            "sfca",
            [("REGISTRATION", "350", False), ("LODGING", "240", False), ("MEALS", "60", True)],
            **self.past_range(4, 9, 3),
            destination="CONFERENCE",
            edition=self.editions["sfca"],
            submission=self.submissions["a"],
            payer="LIRMM",
            transports=["TRAIN"],
            lodging="HOTEL",
            purpose="Présentation fictive de l'article accepté (démonstration).",
        )
        self._request("sfca", "650")

    def conference_back(self) -> None:
        travel.mark_done(mission=self.missions["sfca"], user=self.users[Role.PHD_STUDENT])
        # The lodging receipt is still missing: the card and the reminder show it.
        self._actuals(
            "sfca",
            {"REGISTRATION": "350", "LODGING": "236,50", "MEALS": "60"},
            receipts={"REGISTRATION"},
        )

    def pending_mission(self) -> None:
        """A mission to the LIRMM paid by ST in ten days: its approval awaits the tutor."""
        start = working_week_start(
            timezone.localdate(self.real_now) + dt.timedelta(days=10), 2, back=False
        )
        self._mission(
            "pending",
            [("TRANSPORT", "95", False)],
            start_date=start,
            end_date=start + dt.timedelta(days=1),
            destination="LIRMM",
            payer="ST",
            purpose="Séjour fictif à venir (démonstration).",
            fill_presence=True,  # its LIRMM days appear in the calendar
        )
        self._request("pending", "95")

    # ---------------------------------------------------------------- the story

    def play(self) -> None:
        s, at, end = self.step, self.at, self.month_end
        director, tutor = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
        s("thesis", at(0, 1, 9), self.create_thesis)
        s(
            "team",
            at(0, 2, 10),
            lambda: self.invite(DIRECTOR, CO_SUPERVISOR, TUTOR, COMPANY_CO_SUPERVISOR),
        )
        s(
            "file-1",
            at(0, 3, 9),
            lambda: self.submit("employment_contract", "Attestation d'emploi (fictive)"),
            lambda: self.submit("fsd_zrr_clearance", None),
        )
        s(
            "file-1-validated",
            at(0, 5, 14),
            lambda: self.approve(tutor, "employment_contract"),
            lambda: self.approve(director, "fsd_zrr_clearance"),
        )
        s("travel-settings", at(0, 6, 10), self.travel_settings)
        s("objectives", at(0, 15, 10), lambda: self.create_objectives(*YEAR_1))
        s("objectives-approved", at(0, 16, 15), self.approve_year)
        s(
            "cifre-agreement",
            at(0, 20, 10),
            lambda: self.submit("cifre_agreement", "Convention CIFRE (fictive)"),
        )
        s(
            "cifre-agreement-validated",
            at(0, 24, 11),
            lambda: self.approve(tutor, "cifre_agreement"),
        )
        s("presence-0", at(0, 27, 17), lambda: self.presence(0))
        s("progress-0", end(0), lambda: self.progress(MONTHLY_PROGRESS[0]))
        s("mission-lab", at(1, 2, 10), self.lab_mission)
        s(
            "meeting-tutor",
            at(1, 5, 10),
            lambda: self.meeting(
                "tutor", Role.PHD_STUDENT, self.meeting_at(1, 12, 14), **TUTOR_MEETING
            ),
            lambda: self.agenda("tutor", Role.PHD_STUDENT, TUTOR_AGENDA),
        )
        s(
            "mission-lab-granted",
            at(1, 6, 11),
            lambda: self._decide_order("lab", director, "GRANTED", comment="Accord fictif."),
        )
        s("mission-lab-official", at(1, 7, 10), self.official_order)
        s("conferences", at(1, 8, 10), self.catalogue)
        s("mission-st-requested", at(1, 9, 10), self.refused_mission)
        s(
            "submission-a",
            at(1, 10, 10),
            lambda: self.submission("a", "sfca", "Attaque fictive par canal auxiliaire", "paper"),
            lambda: self.request("a"),
        )
        s(
            "submission-b",
            at(1, 11, 10),
            lambda: self.submission(
                "b", "afpq", "Implémentation fictive d'un schéma post-quantique"
            ),
            lambda: self.request("b"),
        )
        s(
            "meeting-tutor-minutes",
            at(1, 12, 16),
            lambda: self.minutes("tutor", Role.PHD_STUDENT, TUTOR_MINUTES),
            lambda: self.decide("tutor", tutor, "Les mesures restent sur le banc fictif."),
            lambda: self.assign(
                "tutor",
                Role.PHD_STUDENT,
                "Envoyer le protocole de mesure fictif",
                tutor,
                _day(self.anchor, 1, 26),
            ),
            lambda: self.assign(
                "tutor",
                tutor,
                "Préparer le banc de simulation",
                Role.PHD_STUDENT,
                _day(self.anchor, 2, 28),
                "bench",
            ),
        )
        s("meeting-tutor-approved", at(1, 13, 9), lambda: self.mark("tutor", tutor, "APPROVED"))
        s("mission-lab-back", at(1, 17, 10), self.lab_back)
        s("st-grants-a", at(1, 20, 10), lambda: self.st_decide("a", "GRANTED"))
        s(
            "mission-st-refused",
            at(1, 21, 10),
            lambda: self._decide_order("refused", tutor, "REFUSED", reason="BUDGET_EXHAUSTED"),
        )
        s("meeting-tutor-action-done", at(1, 25, 10), self.finish_tutor_action)
        s(
            "st-refuses-b",
            at(1, 26, 10),
            lambda: self.st_decide(
                "b", "REFUSED", "CHANGES_REQUESTED", "Retirer la figure fictive 3."
            ),
        )
        s("presence-1", at(1, 27, 17), lambda: self.presence(1))
        s(
            "b-version-2",
            at(1, 28, 10),
            lambda: self.new_article_version("b"),
            lambda: self.request("b"),
        )
        s("progress-1", end(1), lambda: self.progress(MONTHLY_PROGRESS[1]))
        s(
            "meeting-tripartite",
            at(2, 1, 10),
            lambda: self.meeting(
                "tripartite", Role.PHD_STUDENT, self.meeting_at(2, 20, 10), **TRIPARTITE_MEETING
            ),
            lambda: self.agenda("tripartite", Role.PHD_STUDENT, TRIPARTITE_AGENDA),
        )
        s(
            "mission-lab-reimbursed",
            at(2, 3, 10),
            lambda: travel.mark_reimbursed(
                mission=self.missions["lab"], user=self.users[Role.PHD_STUDENT]
            ),
        )
        s("st-grants-b", at(2, 5, 10), lambda: self.st_decide("b", "GRANTED"))
        s(
            "b-submitted",
            at(2, 10, 10),
            lambda: self.set_status("b", "SUBMITTED", with_version=True),
        )
        s(
            "meeting-tripartite-minutes",
            at(2, 20, 15),
            lambda: self.minutes("tripartite", Role.PHD_STUDENT, TRIPARTITE_MINUTES),
            lambda: self.decide(
                "tripartite", director, "Le premier article vise une conférence fictive."
            ),
            lambda: self.decide(
                "tripartite", tutor, "Relecture ST de quatre semaines avant soumission."
            ),
            lambda: self.assign(
                "tripartite",
                director,
                "Rédiger le plan de l'article fictif",
                Role.PHD_STUDENT,
                _day(self.anchor, 3, 10),
                "sota",
            ),
        )
        s(
            "meeting-tripartite-marks",
            at(2, 21, 9),
            lambda: self.mark("tripartite", director, "APPROVED"),
            lambda: self.mark("tripartite", tutor, "READ"),
        )
        s(
            "collaboration",
            at(2, 25, 10),
            lambda: self.submit("collaboration_contract", "Contrat de collaboration (fictif)"),
        )
        # One of the two validations: the industrial tutor's is still awaited.
        s(
            "a-submitted",
            at(2, 26, 10),
            lambda: self.set_status("a", "SUBMITTED", with_version=True),
        )
        s("presence-2", at(2, 27, 17), lambda: self.presence(2, leave=(3, 14)))
        s(
            "collaboration-director",
            at(2, 28, 16),
            lambda: self.approve(director, "collaboration_contract"),
        )
        s("progress-2", end(2), lambda: self.progress(MONTHLY_PROGRESS[2]))
        s(
            "seminar",
            at(3, 1, 10),
            lambda: self.meeting(
                "seminar", Role.PHD_STUDENT, self.meeting_at(3, 14, 14), **SEMINAR
            ),
        )
        s("side-objective", at(3, 10, 10), lambda: self.create_objectives(SIDE_TOOL))
        s("side-objective-approved", at(3, 11, 9), lambda: self.approve_objective("tool"))
        s(
            "seminar-minutes",
            at(3, 14, 17),
            lambda: self.minutes("seminar", Role.PHD_STUDENT, SEMINAR_MINUTES),
        )
        s("seminar-read", at(3, 15, 9), lambda: self.mark("seminar", Role.CO_SUPERVISOR, "READ"))
        s(
            "director-comment",
            at(3, 15, 17),
            lambda: self.comment(director, "counter", DIRECTOR_COMMENT),
        )
        s("b-rejected", at(3, 16, 10), lambda: self.set_status("b", "REJECTED"))
        s("mission-sfca", at(3, 20, 10), self.conference_mission)
        s(
            "mission-sfca-granted",
            at(3, 22, 10),
            lambda: self._decide_order("sfca", director, "GRANTED"),
        )
        s("presence-3", at(3, 27, 17), lambda: self.presence(3))
        s("progress-3", end(3), lambda: self.progress(MONTHLY_PROGRESS[3]))
        s("acknowledged", at(4, 1, 9), lambda: self.acknowledge("sota"))
        s(
            "academic-file",
            at(4, 2, 10),
            lambda: self.submit("adum_enrollment", "Inscription ADUM (fictive)"),
            lambda: self.submit("cvec_certificate", "Attestation CVEC (fictive)"),
        )
        s("a-accepted", at(4, 3, 10), lambda: self.set_status("a", "ACCEPTED"))
        s(
            "academic-file-validated",
            at(4, 6, 14),
            lambda: self.approve(director, "adum_enrollment", "cvec_certificate"),
        )
        s(
            "funding",
            at(4, 8, 10),
            lambda: self.submit("funding_proof", "Attestation de financement (fictive)"),
        )
        s(
            "co-supervisor-comment",
            at(4, 10, 11),
            lambda: self.comment(Role.CO_SUPERVISOR, "bench", CO_SUPERVISOR_COMMENT),
        )
        s(
            "company-co-supervisor-comment",
            at(4, 11, 11),
            lambda: self.comment(
                Role.INDUSTRIAL_CO_SUPERVISOR, "paper", COMPANY_CO_SUPERVISOR_COMMENT
            ),
        )
        s("contract-reopened", at(4, 12, 10), self.reopen_contract)
        s("mission-sfca-back", at(4, 13, 10), self.conference_back)
        s("side-objective-dropped", at(4, 15, 10), lambda: self.drop("tool"))
        s(
            "charter",
            at(4, 20, 10),
            lambda: self.submit("doctoral_charter", "Charte du doctorat (fictive)"),
        )
        s("public-summary", at(4, 22, 10), self.store_summary)
        s("seminar-document", at(4, 22, 12), self.link_summary)
        s("note", at(4, 23, 10), self.store_note)
        s("due-date-proposed", at(4, 25, 10), self.propose_due_date)
        s("submission-c", at(4, 26, 10), self.pending_submission)
        s("charter-rejected", at(4, 27, 15), self.reject_charter)
        s("presence-4", at(4, 27, 17), lambda: self.presence(4))
        s("upcoming-meetings", at(4, 28, 10), self.upcoming)
        s("progress-4", end(4), lambda: self.progress(MONTHLY_PROGRESS[4]))
        # The last steps happen today, a few hours before the real time.
        hour = dt.timedelta(hours=1)
        s("mission-pending", self.real_now - 4 * hour, self.pending_mission)
        s("csi", self.real_now - 3 * hour, lambda: self.invite(CSI))
        s("note-v2", self.real_now - 2 * hour, self.new_note_version)
        s("progress-today", self.real_now - hour, lambda: self.progress(RECENT_PROGRESS))

    # ---------------------------------------------------------------- links

    def links(self) -> tuple[AccountLink, ...]:
        """At the real time: the real password-reset task, one link per person."""
        found = []
        for person in PEOPLE:
            user = self.users[person.role]
            account_tasks.send_password_reset_email(str(user.pk))
            url = _take_one_link(self.outbox, user.email, _ANY_LINK)
            name = f"{person.first_name} {person.last_name}"
            found.append(AccountLink(person.role, name, user.email, url))
        return tuple(found)


class _Discard:
    def write(self, *args: object, **kwargs: object) -> None:
        pass

    def flush(self) -> None:
        pass


def _require_empty() -> None:
    if Thesis.objects.filter(title=DEMO_TITLE).exists():
        raise AlreadySeededError(
            "The demonstration thesis already exists: recreate the database with "
            "scripts/demo_reset.sh instead of running demo_seed again."
        )
    if (
        Thesis.objects.exists()
        or User.objects.exists()
        or Invitation.objects.exists()
        or AuditEvent.objects.exists()
    ):
        raise ScenarioError(
            "The database is not empty: demo_seed needs a fresh demonstration database "
            "(scripts/demo_reset.sh)."
        )


def seed() -> SeedResult:
    """Write the demonstration thesis. Refused outside a demonstration database."""
    refused = guard.current_refusal()
    if refused is not None:
        raise ScenarioError(f"demo_seed refused: {refused}")
    _require_empty()
    real_now = timezone.now()
    anchor = anchor_for(timezone.localdate(real_now))
    with _in_memory_mailbox() as outbox, vault.audit_source(AUDIT_SOURCE):
        run = _Run(anchor=anchor, real_now=real_now, outbox=outbox)
        run.play()
        links = run.links()
    return SeedResult(thesis_id=str(run.t.pk), steps=tuple(run.steps), links=links)
