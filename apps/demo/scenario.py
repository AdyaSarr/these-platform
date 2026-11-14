"""The demonstration thesis, written through the services exactly as people would.

Every write goes through a service (tests/test_structure.py): the audit chain,
encryption and permission checks are those of real use. Two things differ:

- The CLOCK. Each step runs at its own simulated instant (time-machine, Python
  clock only: no audited path reads PostgreSQL's clock), in chronological order,
  so that objectives and memberships exist for months and the progress chart has
  a real curve. Every step must end within STEP_WINDOW of its start.
- WHO ACTS. The command acts in each person's name. Where a view would require a
  second factor less than 10 minutes old (approving objectives, acknowledging),
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
from apps.admin_file.models import Reason, RequiredItem
from apps.core.confidentiality import ConfidentialityLevel as Level
from apps.objectives import services as objectives
from apps.objectives.models import Category, Objective, Status
from apps.objectives.years import add_years
from apps.vault import services as vault
from apps.vault.models import AuditEvent, Document

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
CSI = Person(Role.CSI_MEMBER, "Lou", "Modèle", "lou.modele@example.org", UNIVERSITY)
PEOPLE = (STUDENT, DIRECTOR, CO_SUPERVISOR, TUTOR, CSI)


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
CHARTER_REJECTION = "Signature du directeur manquante en page 2 (document fictif)."


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

    # ---------------------------------------------------------------- machinery

    def step(self, name: str, when: dt.datetime, *actions: Callable[[], object]) -> None:
        if self.steps and when <= self.steps[-1].end:
            raise ScenarioError(f"step {name}: not after the previous step")
        if when + STEP_WINDOW >= self.real_now:
            raise ScenarioError(f"step {name}: in the future")
        with time_machine.travel(when, tick=True):
            for action in actions:
                action()
            if timezone.now() >= when + STEP_WINDOW:
                raise ScenarioError(f"step {name}: took longer than {STEP_WINDOW}")
        self.steps.append(StepWindow(name, when, when + STEP_WINDOW))

    def at(self, k: int, day: int, hour: int) -> dt.datetime:
        return _at(self.anchor, k, day, hour)

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

    # ---------------------------------------------------------------- the story

    def play(self) -> None:
        s, at, end = self.step, self.at, self.month_end
        director, tutor = Role.DIRECTOR, Role.INDUSTRIAL_TUTOR
        s("thesis", at(0, 1, 9), self.create_thesis)
        s("team", at(0, 2, 10), lambda: self.invite(DIRECTOR, CO_SUPERVISOR, TUTOR))
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
        s("progress-0", end(0), lambda: self.progress(MONTHLY_PROGRESS[0]))
        s("progress-1", end(1), lambda: self.progress(MONTHLY_PROGRESS[1]))
        s(
            "collaboration",
            at(2, 25, 10),
            lambda: self.submit("collaboration_contract", "Contrat de collaboration (fictif)"),
        )
        # One of the two validations: the industrial tutor's is still awaited.
        s(
            "collaboration-director",
            at(2, 28, 16),
            lambda: self.approve(director, "collaboration_contract"),
        )
        s("progress-2", end(2), lambda: self.progress(MONTHLY_PROGRESS[2]))
        s("side-objective", at(3, 10, 10), lambda: self.create_objectives(SIDE_TOOL))
        s("side-objective-approved", at(3, 11, 9), lambda: self.approve_objective("tool"))
        s(
            "director-comment",
            at(3, 15, 17),
            lambda: self.comment(director, "counter", DIRECTOR_COMMENT),
        )
        s("progress-3", end(3), lambda: self.progress(MONTHLY_PROGRESS[3]))
        s("acknowledged", at(4, 1, 9), lambda: self.acknowledge("sota"))
        s(
            "academic-file",
            at(4, 2, 10),
            lambda: self.submit("adum_enrollment", "Inscription ADUM (fictive)"),
            lambda: self.submit("cvec_certificate", "Attestation CVEC (fictive)"),
        )
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
        s("side-objective-dropped", at(4, 15, 10), lambda: self.drop("tool"))
        s(
            "charter",
            at(4, 20, 10),
            lambda: self.submit("doctoral_charter", "Charte du doctorat (fictive)"),
        )
        s(
            "public-summary",
            at(4, 22, 10),
            lambda: self.store("Résumé public du sujet (fictif)", Level.PUBLIC),
        )
        s("note", at(4, 23, 10), self.store_note)
        s("charter-rejected", at(4, 27, 15), self.reject_charter)
        s("progress-4", end(4), lambda: self.progress(MONTHLY_PROGRESS[4]))
        # The last steps happen today, a few hours before the real time.
        hour = dt.timedelta(hours=1)
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
