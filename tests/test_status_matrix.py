"""M13a: the service x status matrix (docs/modules/M13-fin-de-these.md, criterion 3).

Every public function of every ``apps/*/services.py`` is CLASSIFIED below; the list
is closed (a new function fails the test until it is classified). A function
classified as a read writes nothing (static check of its body). Every write is
CALLED with a recipe: in ACTIVE it succeeds (the recipe is a real call), in
DEFENDED and CLOSED it gives the matrix's answer; a refusal writes nothing (the
database fingerprint is unchanged, the audit journal aside).
"""

from __future__ import annotations

import ast
import datetime as dt
import functools
import importlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection

from apps.accounts.models import Thesis
from apps.accounts.services import InvalidInvitationError, TeamError
from apps.admin_file.transitions import InvalidTransitionError

ROOT = Path(__file__).resolve().parent.parent
S = Thesis.Status
NOW = dt.datetime(2027, 1, 20, 9, 0, tzinfo=dt.UTC)
DEFENSE = dt.date(2027, 3, 1)  # the defence date of a DEFENDED or CLOSED thesis here
REFUSALS = (PermissionDenied, ValidationError, TeamError, InvalidInvitationError,
            InvalidTransitionError)  # fmt: skip

READ, READ_AUDITED, PERSONAL, SYSTEM, REDUCTION, WRITE, LIFECYCLE = (
    "read", "read_audited", "personal", "system", "reduction", "write", "lifecycle",
)  # fmt: skip

# --------------------------------------------------------------------------- classification
# read: no write at all. read_audited: a read that journals itself (rule 8).
# personal: the person's own state (read marks). system: called by commands, the
# thesis creation or the vault upkeep, never by a person on a thesis. reduction:
# reduces an access, allowed whatever the status. write: in the matrix below.
# lifecycle: the passages themselves (apps/lifecycle/tests).

CLASSIFICATION: dict[str, str] = {
    # accounts (M1)
    "accounts.accept_invitation": WRITE, "accounts.accept_invitation_new_user": WRITE,
    "accounts.add_membership": WRITE, "accounts.change_role": WRITE,
    "accounts.check_token": READ, "accounts.create_invitation": WRITE,
    "accounts.create_thesis": SYSTEM, "accounts.end_membership": WRITE,
    "accounts.end_membership_for_closure": SYSTEM, "accounts.ensure_usable": READ,
    "accounts.extend_membership": WRITE, "accounts.invite_phd_student": SYSTEM,
    "accounts.revoke_invitation": WRITE, "accounts.revoke_open_invitations": SYSTEM,
    "accounts.team_block": READ, "accounts.team_summary": READ,
    # activity (M11)
    "activity.bell": READ, "activity.grouped": READ, "activity.digest_page": READ,
    "activity.feed": READ,
    "activity.mark_all_read": PERSONAL, "activity.open_event": PERSONAL,
    "activity.purge": SYSTEM,
    # admin_file (M3)
    "admin_file.accept_due_date": WRITE, "admin_file.allowed_levels": READ,
    "admin_file.approve": WRITE, "admin_file.attachable_documents": READ,
    "admin_file.check_validators": READ, "admin_file.create_custom_item": WRITE,
    "admin_file.dashboard_context": READ, "admin_file.is_stale": READ,
    "admin_file.item_for": READ, "admin_file.item_rights": READ,
    "admin_file.label_of": READ, "admin_file.next_items": READ,
    "admin_file.pending_proposal": READ, "admin_file.progress": READ,
    "admin_file.proposal_for": READ, "admin_file.propose_due_date": WRITE,
    "admin_file.refuse_due_date": WRITE, "admin_file.reject": WRITE,
    "admin_file.reopen": WRITE, "admin_file.reopen_reasons_for": READ,
    "admin_file.require_due_date_validator": READ, "admin_file.seed_items": SYSTEM,
    "admin_file.setup_explanation": READ, "admin_file.submit": WRITE,
    "admin_file.update_due_date": WRITE, "admin_file.visible_items": READ,
    "admin_file.withdraw": WRITE, "admin_file.withdraw_due_date_proposal": WRITE,
    # conferences (M6)
    "conferences.add_deadline": WRITE, "conferences.add_preprint": WRITE,
    "conferences.cancel_deadline": WRITE, "conferences.change_deadline": WRITE,
    "conferences.change_level": WRITE, "conferences.create_edition": WRITE,
    "conferences.create_submission": WRITE, "conferences.create_venue": WRITE,
    "conferences.dashboard_block": READ, "conferences.deadline_display": READ,
    "conferences.decide_approval": WRITE, "conferences.deny_unknown": READ_AUDITED,
    "conferences.link_document": WRITE, "conferences.pending_approvals": READ,
    "conferences.reference_deadline": READ, "conferences.request_approval": WRITE,
    "conferences.request_due_dates": READ, "conferences.resubmit_elsewhere": WRITE,
    "conferences.review_days": READ, "conferences.set_authors": WRITE,
    "conferences.set_review_days": WRITE, "conferences.set_st_work": WRITE,
    "conferences.set_status": WRITE, "conferences.submission_counts": READ,
    "conferences.submission_detail": READ_AUDITED, "conferences.submission_for": READ_AUDITED,
    "conferences.submission_list": READ, "conferences.submissions_for_objective": READ,
    "conferences.unlink_document": WRITE, "conferences.upcoming_deadlines": READ,
    "conferences.update_submission": WRITE, "conferences.update_venue": WRITE,
    "conferences.withdraw_approval": WRITE,
    # feeds (M12): creating needs an open thesis already (feeds.allowed)
    "feeds.allowed": READ, "feeds.block": READ, "feeds.create": WRITE,
    "feeds.daily": SYSTEM, "feeds.find": READ, "feeds.live": READ,
    "feeds.regenerate": WRITE, "feeds.revoke": REDUCTION, "feeds.revoke_for": REDUCTION,
    "feeds.touch": SYSTEM,
    # lifecycle (M13a): its own tests (apps/lifecycle/tests)
    "lifecycle.cancel_defense": LIFECYCLE, "lifecycle.confirm_closure": LIFECYCLE,
    "lifecycle.daily": SYSTEM, "lifecycle.declare_defense": LIFECYCLE,
    "lifecycle.page": READ, "lifecycle.request_closure": LIFECYCLE,
    "lifecycle.withdraw_closure": LIFECYCLE,
    # meetings (M5a)
    "meetings.acknowledge": WRITE, "meetings.actions_for_objective": READ,
    "meetings.add_action": WRITE, "meetings.add_decision": WRITE,
    "meetings.cancel_meeting": WRITE, "meetings.change_level": WRITE,
    "meetings.create_meeting": WRITE, "meetings.dashboard_block": READ,
    "meetings.decision_register": READ, "meetings.duplicate_meeting": WRITE,
    "meetings.is_attendee": READ, "meetings.is_writer": READ,
    "meetings.link_document": WRITE, "meetings.meeting_detail": READ,
    "meetings.meeting_for": READ_AUDITED, "meetings.meeting_list": READ,
    "meetings.overdue_actions": READ, "meetings.prefill_agenda": WRITE,
    "meetings.record_ics_export": READ_AUDITED, "meetings.refuse_unknown": READ_AUDITED,
    "meetings.require_attendee": READ_AUDITED, "meetings.require_writer": READ_AUDITED,
    "meetings.save_agenda": WRITE, "meetings.save_minutes": WRITE,
    "meetings.set_action_status": WRITE, "meetings.set_attendees": WRITE,
    "meetings.unlink_document": WRITE, "meetings.update_meeting": WRITE,
    "meetings.withdraw_decision": WRITE,
    # objectives (M4)
    "objectives.acknowledge_changes": WRITE, "objectives.add_comment": WRITE,
    "objectives.add_progress": WRITE, "objectives.approve_objective": WRITE,
    "objectives.approve_year": WRITE, "objectives.comments_for": READ_AUDITED,
    "objectives.create_objective": WRITE, "objectives.current_academic_year": READ,
    "objectives.current_year": READ, "objectives.dashboard": READ,
    "objectives.dashboard_block": READ, "objectives.drop_objective": WRITE,
    "objectives.objective_detail": READ, "objectives.objective_for": READ_AUDITED,
    "objectives.objective_texts": READ, "objectives.pending_acknowledgements": READ,
    "objectives.progress_for": READ, "objectives.revisions_for": READ,
    "objectives.stats_for": READ, "objectives.sync_years": SYSTEM,
    "objectives.update_objective": WRITE,
    # travel (M7)
    "travel.actual_amount_set": READ, "travel.add_expense": WRITE,
    "travel.attach_official_order": WRITE, "travel.attach_receipt": WRITE,
    "travel.bounds": READ, "travel.budget_payers": READ, "travel.budget_summary": READ,
    "travel.budget_years": READ, "travel.cancel_mission": WRITE,
    "travel.change_mission_level": WRITE, "travel.check_budget_setter": READ_AUDITED,
    "travel.check_decider": READ_AUDITED, "travel.create_mission": WRITE,
    "travel.dashboard_block": READ, "travel.decide_order": WRITE,
    "travel.expense_for": READ_AUDITED, "travel.expenses_incomplete": READ,
    "travel.expenses_of": READ, "travel.link_for": READ, "travel.mark_done": WRITE,
    "travel.mark_reimbursed": WRITE, "travel.mission_for": READ_AUDITED,
    "travel.mission_list": READ, "travel.mission_texts": READ,
    "travel.month_rule": READ, "travel.month_view": READ,
    "travel.official_order_missing": READ, "travel.order_covers": READ,
    "travel.order_for": READ_AUDITED, "travel.orders_of": READ,
    "travel.pending_orders_for": READ, "travel.policy_of": SYSTEM,
    "travel.receipts_open": READ,
    "travel.record_ics_export": READ_AUDITED, "travel.request_order": WRITE,
    "travel.rules_of": READ, "travel.set_budget": WRITE, "travel.set_presence": WRITE,
    "travel.set_reminders": WRITE, "travel.set_rules": WRITE, "travel.set_target": WRITE,
    "travel.split_summary": READ, "travel.targets_of": READ,
    "travel.update_expense": WRITE, "travel.update_mission": WRITE,
    "travel.visible_days": READ, "travel.withdraw_order": WRITE,
    # vault (M2)
    "vault.add_version": WRITE, "vault.change_level": WRITE,
    "vault.dashboard_block": READ, "vault.decrypt_secret": SYSTEM,
    "vault.document_for": READ_AUDITED, "vault.download_name": READ,
    "vault.encrypt_secret": SYSTEM, "vault.grant_access": WRITE,
    "vault.latest_documents": READ, "vault.mfa_secrets_using": READ,
    "vault.open_text": READ_AUDITED, "vault.open_texts": READ_AUDITED,
    "vault.open_version": READ_AUDITED, "vault.readable_documents": READ,
    "vault.revoke_access": WRITE, "vault.rewrap_batch": SYSTEM,
    "vault.sealed_texts_using": READ, "vault.sealing": WRITE, "vault.store": WRITE,
    "vault.vault_problems": READ,
}  # fmt: skip


def _public_functions() -> set[str]:
    names = set()
    for path in sorted((ROOT / "apps").glob("*/services.py")):
        module = importlib.import_module(f"apps.{path.parent.name}.services")
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("_") and fn.__module__ == module.__name__:
                names.add(f"{path.parent.name}.{name}")
    return names


def test_every_public_service_is_classified() -> None:
    assert _public_functions() == set(CLASSIFICATION)


WRITE_CALLS = {"save", "create", "update", "delete", "bulk_create", "get_or_create",
               "update_or_create", "audit", "sealing", "record"}  # fmt: skip


def test_a_read_service_writes_nothing() -> None:
    """Static: the body of a read names no write (save, create, update, delete,
    audit, sealing, an activity record)."""
    offenders = {}
    for key, kind in CLASSIFICATION.items():
        if kind != READ:
            continue
        module_name, name = key.split(".")
        fn = getattr(importlib.import_module(f"apps.{module_name}.services"), name)
        tree = ast.parse(inspect.getsource(fn).lstrip())
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }
        if called & WRITE_CALLS:
            offenders[key] = sorted(called & WRITE_CALLS)
    assert offenders == {}


# --------------------------------------------------------------------------- the world


class World:
    """The team of the activity tests (seven roles) and, on first use, each object
    a recipe needs, all made through the services while the thesis is ACTIVE."""

    def __init__(self, time_machine: Any) -> None:
        from apps.activity.tests.helpers import make_team

        self.tm = time_machine
        self.t = make_team()
        self.thesis = self.t.thesis
        self._cache: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in BUILDERS:
            raise AttributeError(name)
        if name not in self._cache:
            self._cache[name] = BUILDERS[name](self)
        return self._cache[name]


def _pdf(text: str = "piece fictive") -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("piece.pdf", f"%PDF-1.7\n%{text}\n%%EOF\n".encode())


def _org(w: World) -> Any:
    from apps.accounts.models import Membership

    return Membership.objects.filter(thesis=w.thesis, user=w.t.co_supervisor).get().organization


def _membership(w: World, user: Any) -> Any:
    from apps.accounts.models import Membership

    return Membership.objects.active().get(thesis=w.thesis, user=user)


def _invitation(w: World) -> Any:
    from apps.accounts import services, tasks

    inv = services.create_invitation(
        thesis=w.thesis, email="invitee.fictive@example.org", role="CO_SUPERVISOR",
        organization=_org(w), by=w.t.student,
    )  # fmt: skip
    tasks.send_invitation_email(str(inv.pk))
    inv.refresh_from_db()
    return inv


def _new_user(w: World) -> Any:
    from apps.accounts.tests.factories import UserFactory

    return UserFactory.create(email="invitee.fictive@example.org")


def _document(w: World, level: str = "PUBLIC") -> Any:
    from apps.core.confidentiality import ConfidentialityLevel
    from apps.vault import services as vault

    return vault.store(thesis=w.thesis, upload=_pdf(), title="Note fictive",
                       level=ConfidentialityLevel(level), user=w.t.student)  # fmt: skip


def _grant(w: World) -> Any:
    from apps.vault import services as vault

    return vault.grant_access(document=w.confidential, grantee=w.t.director, user=w.t.student)


def _item(w: World, status: str = "TODO") -> Any:
    from apps.accounts.roles import Role
    from apps.admin_file import services
    from apps.admin_file.models import Section
    from apps.admin_file.tests.helpers import item_of

    item = item_of(w.t, section=Section.ACADEMIC, validator_roles=[Role.DIRECTOR],
                   due_date=dt.date(2027, 6, 1))  # fmt: skip
    if status in ("SUBMITTED", "VALIDATED"):
        item = services.submit(item=item, user=w.t.student)
    if status == "VALIDATED":
        item = services.approve(item=item, user=w.t.director)
    return item


def _proposal(w: World) -> Any:
    from apps.admin_file import services
    from apps.admin_file.models import DueDateReason

    return services.propose_due_date(
        item=w.item, user=w.t.student, due_date=dt.date(2027, 7, 1), reason=DueDateReason.OTHER
    )


def _venue(w: World) -> Any:
    from apps.conferences.tests.helpers import venue_of

    return venue_of(w.t)


def _edition(w: World) -> Any:
    from apps.conferences.tests.helpers import edition_of

    return edition_of(w.t, venue=w.venue)


def _deadline(w: World) -> Any:
    from apps.conferences.tests.helpers import deadline_of

    return deadline_of(w.t, w.edition)


def _submission(w: World) -> Any:
    from apps.conferences.tests.helpers import submission_of

    return submission_of(w.t, edition=w.edition)


def _withdrawn_submission(w: World) -> Any:
    from apps.conferences import services
    from apps.conferences.tests.helpers import submission_of

    submission = submission_of(w.t, edition=w.edition, title="Article fictif retiré")
    return services.set_status(submission=submission, user=w.t.student, status="WITHDRAWN")


def _other_edition(w: World) -> Any:
    from apps.conferences.tests.helpers import edition_of, venue_of

    return edition_of(w.t, venue=venue_of(w.t, acronym="AFPQ", name="Atelier fictif"))


def _granted_version(w: World) -> Any:
    from apps.conferences.tests.helpers import granted

    granted(w.t, w.submission, w.version)
    return w.version


def _version(w: World) -> Any:
    from apps.conferences.tests.helpers import article_version

    return article_version(w.t, w.submission)


def _approval(w: World) -> Any:
    from apps.conferences import services

    return services.request_approval(submission=w.submission, user=w.t.student, version=w.version)


def _submission_link(w: World) -> Any:
    w.version  # noqa: B018 (links the article's document)
    return w.submission.document_links.get()


def _meeting(w: World) -> Any:
    from apps.meetings.tests.helpers import meeting_of

    return meeting_of(w.t, starts_at=NOW + dt.timedelta(days=7))


def _started_meeting(w: World) -> Any:
    """Minutes, decisions and actions open at the start of the meeting."""
    from apps.meetings.tests.helpers import meeting_of

    meeting = meeting_of(w.t, starts_at=NOW + dt.timedelta(hours=1))
    w.tm.move_to(NOW + dt.timedelta(hours=2), tick=True)
    return meeting


def _meeting_with_minutes(w: World) -> Any:
    from apps.meetings import services

    meeting = w.started_meeting
    services.save_minutes(meeting=meeting, user=w.t.student, minutes="CR fictif", version=0)
    return meeting


def _decision(w: World) -> Any:
    from apps.meetings import services

    return services.add_decision(
        meeting=w.started_meeting, user=w.t.student, text="Décision fictive"
    )


def _action_item(w: World) -> Any:
    from apps.meetings import services

    return services.add_action(meeting=w.started_meeting, user=w.t.student, title="Action fictive",
                               assignee=w.t.student, due_date=dt.date(2027, 2, 15))  # fmt: skip


def _meeting_link(w: World) -> Any:
    from apps.meetings import services

    return services.link_document(meeting=w.meeting, user=w.t.student, document=w.document)


def _objective(w: World) -> Any:
    from apps.objectives.tests.helpers import objective_of

    return objective_of(w.t)


def _approved_objective(w: World) -> Any:
    from apps.objectives.tests.helpers import approved

    return approved(w.t, title="Objectif fictif approuvé")


def _changed_objective(w: World) -> Any:
    from apps.objectives import services

    objective = w.approved_objective
    services.update_objective(
        objective=objective,
        user=w.t.student,
        changes={"due_date": dt.date(2027, 7, 15)},
        reason="Report fictif",
    )
    objective.refresh_from_db()
    return objective


def _mission(w: World) -> Any:
    from apps.travel.tests.helpers import mission_of

    return mission_of(w.t)  # starts 2027-02-08: before DEFENSE


def _mission_after(w: World) -> Any:
    from apps.travel.tests.helpers import mission_of

    return mission_of(w.t, start_date=dt.date(2027, 4, 12), end_date=dt.date(2027, 4, 13))


def _expense(w: World) -> Any:
    from apps.travel import services

    return services.add_expense(mission=w.mission, user=w.t.student, category="TRANSPORT")


def _expense_after(w: World) -> Any:
    from apps.travel import services

    return services.add_expense(mission=w.mission_after, user=w.t.student, category="TRANSPORT")


def _order(w: World) -> Any:
    from apps.travel import services

    return services.request_order(mission=w.mission, user=w.t.student, estimated_total="100")


def _done_mission(w: World) -> Any:
    from apps.travel import services
    from apps.travel.tests.helpers import mission_of

    mission = mission_of(w.t, payer="OTHER")
    w.tm.move_to(dt.datetime(2027, 2, 11, 9, 0, tzinfo=dt.UTC), tick=True)
    return services.mark_done(mission=mission, user=w.t.student)


BUILDERS: dict[str, Callable[[World], Any]] = {
    "invitation": _invitation, "new_user": _new_user,
    "document": _document, "confidential": functools.partial(_document, level="CONFIDENTIAL"),
    "grant": _grant,
    "item": _item, "submitted_item": functools.partial(_item, status="SUBMITTED"),
    "validated_item": functools.partial(_item, status="VALIDATED"), "proposal": _proposal,
    "venue": _venue, "edition": _edition, "deadline": _deadline, "submission": _submission,
    "version": _version, "approval": _approval, "submission_link": _submission_link,
    "meeting": _meeting, "started_meeting": _started_meeting,
    "meeting_with_minutes": _meeting_with_minutes,
    "withdrawn_submission": _withdrawn_submission, "other_edition": _other_edition,
    "granted_version": _granted_version,
    "decision": _decision, "action_item": _action_item, "meeting_link": _meeting_link,
    "objective": _objective, "approved_objective": _approved_objective,
    "changed_objective": _changed_objective,
    "mission": _mission, "mission_after": _mission_after, "expense": _expense,
    "expense_after": _expense_after, "order": _order, "done_mission": _done_mission,
}  # fmt: skip


# --------------------------------------------------------------------------- recipes
# Each recipe builds what it needs (thesis ACTIVE) and returns the call itself.


def _m(module: str) -> Any:
    return importlib.import_module(f"apps.{module}.services")


P = functools.partial


def _sealing(w: World, action: str, level: str = "PUBLIC") -> Callable[[], Any]:
    from apps.accounts.permissions import Action
    from apps.core.confidentiality import ConfidentialityLevel

    def call() -> None:
        with _m("vault").sealing(
            user=w.t.student,
            thesis=w.thesis,
            level=ConfidentialityLevel(level),
            action=Action(action),
        ):
            pass

    return call


def _lvl(name: str) -> Any:
    from apps.core.confidentiality import ConfidentialityLevel

    return ConfidentialityLevel(name)


Recipe = Callable[[World], Callable[[], Any]]
ALLOW, REFUSE = "allow", "refuse"

RECIPES: dict[str, tuple[Recipe, str]] = {
    # ---- accounts (M1): the end of a membership only, in DEFENDED
    "accounts.accept_invitation": (lambda w: P(
        _m("accounts").accept_invitation, w.invitation.pk, user=w.new_user), REFUSE),
    "accounts.accept_invitation_new_user": (lambda w: P(
        _m("accounts").accept_invitation_new_user, w.invitation.pk, first_name="Prénom",
        last_name="Fictif", password="Mot-de-passe-fictif-29!"),  # noqa: S106  # pragma: allowlist secret
        REFUSE),
    "accounts.add_membership": (lambda w: P(
        _m("accounts").add_membership, user=w.new_user, thesis=w.thesis,
        role="CO_SUPERVISOR", organization=_org(w)), REFUSE),
    "accounts.change_role": (lambda w: P(
        _m("accounts").change_role, _membership(w, w.t.co_supervisor), "DIRECTOR",
        by=w.t.student), REFUSE),
    "accounts.create_invitation": (lambda w: P(
        _m("accounts").create_invitation, thesis=w.thesis, email="autre.fictif@example.org",
        role="CO_SUPERVISOR", organization=_org(w), by=w.t.student), REFUSE),
    "accounts.end_membership": (lambda w: P(
        _m("accounts").end_membership, _membership(w, w.t.co_supervisor), by=w.t.student), ALLOW),
    "accounts.extend_membership": (lambda w: P(
        _m("accounts").extend_membership, _membership(w, w.t.csi), by=w.t.student), REFUSE),
    "accounts.revoke_invitation": (lambda w: P(
        _m("accounts").revoke_invitation, w.invitation, by=w.t.student), REFUSE),
    # ---- admin_file (M3): frozen
    "admin_file.accept_due_date": (lambda w: P(
        _m("admin_file").accept_due_date, proposal=w.proposal, user=w.t.director), REFUSE),
    "admin_file.approve": (lambda w: P(
        _m("admin_file").approve, item=w.submitted_item, user=w.t.director), REFUSE),
    "admin_file.create_custom_item": (lambda w: P(
        _m("admin_file").create_custom_item, thesis=w.thesis, user=w.t.student,
        label="Pièce personnalisée fictive", section="ACADEMIC", validator_roles=["DIRECTOR"],
        default_level=_lvl("CONFIDENTIAL"), mandatory=False, attachment_required=False,
        due_date=None), REFUSE),
    "admin_file.propose_due_date": (lambda w: P(
        _m("admin_file").propose_due_date, item=w.item, user=w.t.student,
        due_date=dt.date(2027, 7, 1), reason="OTHER"), REFUSE),
    "admin_file.refuse_due_date": (lambda w: P(
        _m("admin_file").refuse_due_date, proposal=w.proposal, user=w.t.director), REFUSE),
    "admin_file.reject": (lambda w: P(
        _m("admin_file").reject, item=w.submitted_item, user=w.t.director,
        reason="ILLEGIBLE"), REFUSE),
    "admin_file.reopen": (lambda w: P(
        _m("admin_file").reopen, item=w.validated_item, user=w.t.student,
        reason="AMENDMENT"), REFUSE),
    "admin_file.submit": (lambda w: P(
        _m("admin_file").submit, item=w.item, user=w.t.student), REFUSE),
    "admin_file.update_due_date": (lambda w: P(
        _m("admin_file").update_due_date, item=w.item, user=w.t.director,
        due_date=dt.date(2027, 7, 1)), REFUSE),
    "admin_file.withdraw": (lambda w: P(
        _m("admin_file").withdraw, item=w.submitted_item, user=w.t.student), REFUSE),
    "admin_file.withdraw_due_date_proposal": (lambda w: P(
        _m("admin_file").withdraw_due_date_proposal, proposal=w.proposal,
        user=w.t.student), REFUSE),
    # ---- conferences (M6): conferences and ST approvals stay open in DEFENDED
    "conferences.add_deadline": (lambda w: P(
        _m("conferences").add_deadline, edition=w.edition, user=w.t.student, kind="CAMERA_READY",
        day=dt.date(2027, 5, 3), wall=dt.time(23, 59), tz_name="AoE"), ALLOW),
    "conferences.add_preprint": (lambda w: P(
        _m("conferences").add_preprint, submission=w.submission, user=w.t.student,
        platform="EPRINT", posted_on=dt.date(2027, 1, 9),
        url="https://eprint.example.org/2027/001", version=w.granted_version), ALLOW),
    "conferences.cancel_deadline": (lambda w: P(
        _m("conferences").cancel_deadline, deadline=w.deadline, user=w.t.director), ALLOW),
    "conferences.change_deadline": (lambda w: P(
        _m("conferences").change_deadline, deadline=w.deadline, user=w.t.director,
        day=dt.date(2027, 3, 8), wall=dt.time(23, 59), tz_name="AoE"), ALLOW),
    "conferences.change_level": (lambda w: P(
        _m("conferences").change_level, submission=w.submission, user=w.t.student,
        level=_lvl("INTERNAL_ST"), recent_mfa=True), ALLOW),
    "conferences.create_edition": (lambda w: P(
        _m("conferences").create_edition, venue=w.venue, user=w.t.student, year=2028), ALLOW),
    "conferences.create_submission": (lambda w: P(
        _m("conferences").create_submission, thesis=w.thesis, user=w.t.student, venue=w.venue,
        edition=w.edition, title="Article fictif", level=_lvl("CONFIDENTIAL")), ALLOW),
    "conferences.create_venue": (lambda w: P(
        _m("conferences").create_venue, thesis=w.thesis, user=w.t.student, kind="CONFERENCE",
        acronym="AUTRE", name="Autre conférence fictive"), ALLOW),
    "conferences.decide_approval": (lambda w: P(
        _m("conferences").decide_approval, approval=w.approval, user=w.t.tutor,
        decision="GRANTED", recent_mfa=True), ALLOW),
    "conferences.link_document": (lambda w: P(
        _m("conferences").link_document, submission=w.submission, user=w.t.student,
        document=w.confidential), ALLOW),
    "conferences.request_approval": (lambda w: P(
        _m("conferences").request_approval, submission=w.submission, user=w.t.student,
        version=w.version), ALLOW),
    "conferences.resubmit_elsewhere": (lambda w: P(
        _m("conferences").resubmit_elsewhere, submission=w.withdrawn_submission, user=w.t.student,
        venue=w.other_edition.venue, edition=w.other_edition), ALLOW),
    "conferences.set_authors": (lambda w: P(
        _m("conferences").set_authors, submission=w.submission, user=w.t.student,
        authors=[w.t.student, w.t.director]), ALLOW),
    "conferences.set_review_days": (lambda w: P(
        _m("conferences").set_review_days, thesis=w.thesis, user=w.t.tutor, days=45,
        recent_mfa=True), ALLOW),
    "conferences.set_st_work": (lambda w: P(
        _m("conferences").set_st_work, submission=w.submission, user=w.t.tutor, value=False,
        recent_mfa=True), ALLOW),
    "conferences.set_status": (lambda w: P(
        _m("conferences").set_status, submission=w.submission, user=w.t.student,
        status="WITHDRAWN"), ALLOW),
    "conferences.unlink_document": (lambda w: P(
        _m("conferences").unlink_document, link=w.submission_link, user=w.t.student), ALLOW),
    "conferences.update_submission": (lambda w: P(
        _m("conferences").update_submission, submission=w.submission, user=w.t.student,
        changes={"title": "Titre fictif modifié"}), ALLOW),
    "conferences.update_venue": (lambda w: P(
        _m("conferences").update_venue, venue=w.venue, user=w.t.student,
        changes={"name": "Nom fictif modifié"}), ALLOW),
    "conferences.withdraw_approval": (lambda w: P(
        _m("conferences").withdraw_approval, approval=w.approval, user=w.t.student), ALLOW),
    # ---- feeds (M12): no new link on a non-open thesis
    "feeds.create": (lambda w: P(
        _m("feeds").create, user=w.t.student, thesis=w.thesis, recent_mfa=True), REFUSE),
    "feeds.regenerate": (lambda w: P(
        _m("feeds").regenerate, user=w.t.student, thesis=w.thesis, recent_mfa=True), REFUSE),
    # ---- meetings (M5a): frozen
    "meetings.acknowledge": (lambda w: P(
        _m("meetings").acknowledge, meeting=w.meeting_with_minutes, user=w.t.co_supervisor,
        kind="READ", version=1), REFUSE),
    "meetings.add_action": (lambda w: P(
        _m("meetings").add_action, meeting=w.started_meeting, user=w.t.student,
        title="Action fictive",
        assignee=w.t.student, due_date=dt.date(2027, 2, 15)), REFUSE),
    "meetings.add_decision": (lambda w: P(
        _m("meetings").add_decision, meeting=w.started_meeting, user=w.t.student,
        text="Décision fictive"), REFUSE),
    "meetings.cancel_meeting": (lambda w: P(
        _m("meetings").cancel_meeting, meeting=w.meeting, user=w.t.student), REFUSE),
    "meetings.change_level": (lambda w: P(
        _m("meetings").change_level, meeting=w.meeting, user=w.t.student,
        level=_lvl("INTERNAL_LIRMM"), recent_mfa=True), REFUSE),
    "meetings.create_meeting": (lambda w: P(
        _m("meetings").create_meeting, thesis=w.thesis, user=w.t.student, kind="SUPERVISION",
        starts_at=NOW + dt.timedelta(days=9), duration_minutes=60, location="VISIO",
        level=_lvl("PUBLIC"), title="Point fictif", attendees=[w.t.student, w.t.director]), REFUSE),
    "meetings.duplicate_meeting": (lambda w: P(
        _m("meetings").duplicate_meeting, meeting=w.meeting, user=w.t.student,
        starts_at=NOW + dt.timedelta(days=14)), REFUSE),
    "meetings.link_document": (lambda w: P(
        _m("meetings").link_document, meeting=w.meeting, user=w.t.student,
        document=w.document), REFUSE),
    "meetings.prefill_agenda": (lambda w: (w.action_item, P(
        _m("meetings").prefill_agenda, meeting=w.meeting, user=w.t.student))[1], REFUSE),
    "meetings.save_agenda": (lambda w: P(
        _m("meetings").save_agenda, meeting=w.meeting, user=w.t.student,
        agenda="Ordre du jour fictif"), REFUSE),
    "meetings.save_minutes": (lambda w: P(
        _m("meetings").save_minutes, meeting=w.started_meeting, user=w.t.student,
        minutes="CR fictif",
        version=0), REFUSE),
    "meetings.set_action_status": (lambda w: P(
        _m("meetings").set_action_status, action=w.action_item, user=w.t.student,
        status="DONE"), REFUSE),
    "meetings.set_attendees": (lambda w: P(
        _m("meetings").set_attendees, meeting=w.meeting, user=w.t.student,
        attendees=[w.t.student, w.t.director, w.t.tutor]), REFUSE),
    "meetings.unlink_document": (lambda w: P(
        _m("meetings").unlink_document, link=w.meeting_link, user=w.t.student), REFUSE),
    "meetings.update_meeting": (lambda w: P(
        _m("meetings").update_meeting, meeting=w.meeting, user=w.t.student,
        changes={"duration_minutes": 90}), REFUSE),
    "meetings.withdraw_decision": (lambda w: P(
        _m("meetings").withdraw_decision, decision=w.decision, user=w.t.student), REFUSE),
    # ---- objectives (M4): frozen
    "objectives.acknowledge_changes": (lambda w: P(
        _m("objectives").acknowledge_changes, objective=w.changed_objective, user=w.t.director,
        recent_mfa=True), REFUSE),
    "objectives.add_comment": (lambda w: P(
        _m("objectives").add_comment, objective=w.objective, user=w.t.director,
        body="Commentaire fictif"), REFUSE),
    "objectives.add_progress": (lambda w: P(
        _m("objectives").add_progress, objective=w.approved_objective, user=w.t.student,
        date=dt.date(2027, 1, 18), progress=30, status="IN_PROGRESS"), REFUSE),
    "objectives.approve_objective": (lambda w: P(
        _m("objectives").approve_objective, objective=w.objective, user=w.t.director,
        recent_mfa=True), REFUSE),
    "objectives.approve_year": (lambda w: P(
        _m("objectives").approve_year, year=w.objective.year, user=w.t.director,
        recent_mfa=True), REFUSE),
    "objectives.create_objective": (lambda w: P(
        _m("objectives").create_objective, thesis=w.thesis, user=w.t.student,
        year=_year(w), title="Objectif fictif", category="RESEARCH",
        due_date=dt.date(2027, 6, 30)), REFUSE),
    "objectives.drop_objective": (lambda w: P(
        _m("objectives").drop_objective, objective=w.objective, user=w.t.student,
        reason="Abandon fictif motivé"), REFUSE),
    "objectives.update_objective": (lambda w: P(
        _m("objectives").update_objective, objective=w.objective, user=w.t.student,
        changes={"weight": 3}), REFUSE),
    # ---- travel (M7): frozen, but receipts and actual amounts of a mission begun
    # before the defence
    "travel.add_expense[before]": (lambda w: P(
        _m("travel").add_expense, mission=w.mission, user=w.t.student, category="MEALS"), ALLOW),
    "travel.add_expense[after]": (lambda w: P(
        _m("travel").add_expense, mission=w.mission_after, user=w.t.student,
        category="MEALS"), REFUSE),
    "travel.attach_official_order": (lambda w: P(
        _m("travel").attach_official_order, mission=w.mission, user=w.t.student,
        upload=_pdf()), REFUSE),
    "travel.attach_receipt[before]": (lambda w: P(
        _m("travel").attach_receipt, expense=w.expense, user=w.t.student, upload=_pdf()), ALLOW),
    "travel.attach_receipt[after]": (lambda w: P(
        _m("travel").attach_receipt, expense=w.expense_after, user=w.t.student,
        upload=_pdf()), REFUSE),
    "travel.cancel_mission": (lambda w: P(
        _m("travel").cancel_mission, mission=w.mission, user=w.t.student), REFUSE),
    "travel.change_mission_level": (lambda w: P(
        _m("travel").change_mission_level, mission=w.mission, user=w.t.student,
        level=_lvl("CONFIDENTIAL"), recent_mfa=True), REFUSE),
    "travel.create_mission": (lambda w: P(
        _m("travel").create_mission, thesis=w.thesis, user=w.t.student,
        start_date=dt.date(2027, 2, 15), end_date=dt.date(2027, 2, 16), destination="LIRMM",
        payer="ST", fill_presence=False), REFUSE),
    "travel.decide_order": (lambda w: P(
        _m("travel").decide_order, order=w.order, user=w.t.tutor, decision="GRANTED",
        recent_mfa=True), REFUSE),
    "travel.mark_done": (lambda w: _mark_done(w), REFUSE),
    "travel.mark_reimbursed": (lambda w: P(
        _m("travel").mark_reimbursed, mission=w.done_mission, user=w.t.student), REFUSE),
    "travel.request_order": (lambda w: P(
        _m("travel").request_order, mission=w.mission, user=w.t.student,
        estimated_total="100"), REFUSE),
    "travel.set_budget": (lambda w: P(
        _m("travel").set_budget, thesis=w.thesis, user=w.t.tutor, payer="ST", year=2027,
        amount="4000", recent_mfa=True), REFUSE),
    "travel.set_presence": (lambda w: P(
        _m("travel").set_presence, thesis=w.thesis, user=w.t.student, first=dt.date(2027, 1, 25),
        last=dt.date(2027, 1, 25), kind="LIRMM"), REFUSE),
    "travel.set_reminders": (lambda w: P(
        _m("travel").set_reminders, thesis=w.thesis, user=w.t.student, recent_mfa=True,
        order_lead_days=30, receipt_grace_days=3), REFUSE),
    "travel.set_rules": (lambda w: P(
        _m("travel").set_rules, thesis=w.thesis, user=w.t.director, recent_mfa=True,
        whit_monday_holiday=False), REFUSE),
    "travel.set_target": (lambda w: P(
        _m("travel").set_target, thesis=w.thesis, user=w.t.tutor, recent_mfa=True, cifre_year=1,
        st_percent=50), REFUSE),
    "travel.update_expense[before]": (lambda w: P(
        _m("travel").update_expense, expense=w.expense, user=w.t.student, actual="118,37"), ALLOW),
    "travel.update_expense[after]": (lambda w: P(
        _m("travel").update_expense, expense=w.expense_after, user=w.t.student,
        actual="118,37"), REFUSE),
    "travel.update_mission": (lambda w: P(
        _m("travel").update_mission, mission=w.mission, user=w.t.student,
        changes={"official_order_obtained": True}), REFUSE),
    "travel.withdraw_order": (lambda w: P(
        _m("travel").withdraw_order, order=w.order, user=w.t.student), REFUSE),
    # ---- vault (M2): documents stay open in DEFENDED
    "vault.add_version": (lambda w: P(
        _m("vault").add_version, document=w.document, upload=_pdf("v2"), user=w.t.student), ALLOW),
    "vault.change_level": (lambda w: P(
        _m("vault").change_level, document=w.document, level=_lvl("INTERNAL_LIRMM"),
        user=w.t.student), ALLOW),
    "vault.grant_access": (lambda w: P(
        _m("vault").grant_access, document=w.confidential, grantee=w.t.director,
        user=w.t.student), ALLOW),
    "vault.revoke_access": (lambda w: P(
        _m("vault").revoke_access, grant=w.grant, user=w.t.student), ALLOW),
    "vault.sealing[document]": (lambda w: _sealing(w, "DOCUMENT_WRITE"), ALLOW),
    "vault.sealing[objective]": (lambda w: _sealing(w, "OBJECTIVE_WRITE"), REFUSE),
    "vault.store": (lambda w: P(
        _m("vault").store, thesis=w.thesis, upload=_pdf(), title="Version finale fictive",
        level=_lvl("CONFIDENTIAL"), user=w.t.student), ALLOW),
}  # fmt: skip


def _year(w: World) -> Any:
    from apps.objectives.models import ThesisYear

    return ThesisYear.objects.get(thesis=w.thesis, number=1)


def _mark_done(w: World) -> Callable[[], Any]:
    from apps.travel.tests.helpers import mission_of

    mission = mission_of(w.t, payer="OTHER", start_date=dt.date(2027, 1, 25),
                         end_date=dt.date(2027, 1, 26))  # fmt: skip

    def call() -> Any:
        w.tm.move_to(dt.datetime(2027, 1, 27, 9, 0, tzinfo=dt.UTC), tick=True)
        return _m("travel").mark_done(mission=mission, user=w.t.student)

    return call


def test_every_write_has_a_recipe() -> None:
    writes = {key for key, kind in CLASSIFICATION.items() if kind == WRITE}
    assert {key.split("[")[0] for key in RECIPES} == writes


# --------------------------------------------------------------------------- the calls

EXCLUDED_TABLES = {"vault_auditevent", "django_session"}


def fingerprint() -> dict[str, str]:
    """Every table but the audit journal, as one digest per table (row texts)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        tables = sorted(row[0] for row in cursor.fetchall() if row[0] not in EXCLUDED_TABLES)
        out = {}
        for table in tables:
            cursor.execute(  # nosec B608 (names from pg_tables)
                f"SELECT md5(coalesce(string_agg(CAST(t AS text), '|' "  # noqa: S608
                f"ORDER BY CAST(t AS text)), '')) FROM \"{table}\" t"
            )
            out[table] = cursor.fetchone()[0]
    return out


def _set_status(thesis: Thesis, status: str) -> None:
    fields: dict[str, Any] = {"status": status}
    if status in (S.DEFENDED, S.CLOSED):
        fields["defended_on"] = DEFENSE
    Thesis.objects.filter(pk=thesis.pk).update(**fields)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("status", [S.ACTIVE, S.DEFENDED, S.CLOSED])
@pytest.mark.parametrize("key", sorted(RECIPES))
def test_the_matrix(key: str, status: str, time_machine: Any) -> None:
    time_machine.move_to(NOW, tick=True)
    recipe, in_defended = RECIPES[key]
    world = World(time_machine)
    call = recipe(world)
    _set_status(world.thesis, status)
    expected = ALLOW if status == S.ACTIVE else in_defended if status == S.DEFENDED else REFUSE
    before = fingerprint()
    if expected == ALLOW:
        call()
        return
    with pytest.raises(REFUSALS):
        call()
    after = fingerprint()
    assert {t: v for t, v in after.items() if before.get(t) != v} == {}
