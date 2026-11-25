"""The snapshot of an export (M13b): everything the student reads, at one instant.

``take()`` runs inside the export's REPEATABLE READ READ ONLY transaction
(services.start_export): every query sees the database at the same instant. It
reads through the modules' own read rules (readable_by, visible_to,
visible_items, can_access_document, can_access_level) and narrows them by
``levels`` (INTERNAL_ST out without an ST agreement). It decrypts the sealed
texts (small, capped) and only PLANS the documents: they are decrypted one at a
time, later, by the stream (archive.py). Nothing is written, nothing journaled.

People are ``{"nom", "role"}`` everywhere; never an address. Ids are those of the
platform; a link to an object that is not exported is ``None``.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import Membership, Thesis
from apps.accounts.permissions import Action, allowed_actions, readable_levels
from apps.core.confidentiality import ConfidentialityLevel as L

TEXTS_MAX_BYTES = 32 * 1024 * 1024
ENTRY_NAME = re.compile(r"[a-z0-9][a-z0-9/-]*\.[a-z0-9]+")
DATA_FILES = (
    "these.json",
    "equipe.json",
    "documents.json",
    "objectifs.json",
    "reunions.json",
    "dossier-administratif.json",
    "conferences.json",
    "deplacements.json",
    "portfolio.json",
    "journal/mes-evenements.json",
)
# Keys of the student's own journal lines that are exported; any other is dropped
# (pseudonyms, fingerprints, key identifiers, chain fields, other people's ids).
SAFE_DETAIL_KEYS = frozenset(
    {
        "action",
        "action_id",
        "added",
        "approval_id",
        "category",
        "changes",
        "checkpoint_seq",
        "cifre_year",
        "complete",
        "conflicts",
        "count",
        "created",
        "cycle",
        "day",
        "days",
        "deadlines",
        "decision_id",
        "defended_on",
        "destination",
        "document",
        "document_id",
        "due_at",
        "editions",
        "end_date",
        "error",
        "evidence",
        "expense_id",
        "field",
        "fields",
        "flat_rate",
        "grants_revoked",
        "item",
        "items",
        "kind",
        "level",
        "location",
        "mandatory",
        "mime",
        "new",
        "new_level",
        "new_role",
        "object_id",
        "object_ids",
        "object_type",
        "objective_linked",
        "old",
        "old_level",
        "old_role",
        "operation",
        "order_id",
        "payer",
        "prefilled",
        "previous_status",
        "progress",
        "proposal",
        "purpose",
        "reason",
        "removed",
        "request_due",
        "result",
        "result_declared",
        "review_days",
        "revisions",
        "role",
        "section",
        "size",
        "slot",
        "slots",
        "start_date",
        "status",
        "st_work",
        "type",
        "tz_name",
        "until",
        "venues",
        "version",
        "version_id",
        "versions",
        "year",
        "all_versions",
        "documents",
        "texts",
        "excluded",
        "st_included",
        "st_consent",
        "confidential_ids",
        "export",
        "consent",
    }
)


class TextsTooLargeError(Exception):
    """The decrypted texts exceed TEXTS_MAX_BYTES."""


@dataclass
class Snapshot:
    files: dict[str, Any]
    documents: list[tuple[str, Any]]  # (entry name, vault.PlannedVersion)
    total_bytes: int
    consent: Any
    st_open: bool
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    st_included: int = 0
    confidential_ids: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- small helpers


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: Any) -> str | None:
    return str(value) if value is not None else None


def entry_name(n: int, title: str, version: int, extension: str) -> str:
    """``documents/0001-<slug>-v3.<ext>``: built by us, never the title as is."""
    stem = slugify(title).replace("_", "-")[:60].strip("-") or "document"
    stem = re.sub(r"-{2,}", "-", stem)
    name = f"documents/{n:04d}-{stem}-v{version}.{extension.lower()}"
    if not ENTRY_NAME.fullmatch(name) or ".." in name:
        raise ValueError("unsafe entry name")  # a bug: the parts above cannot make one
    return name


def st_state(thesis: Thesis, now: dt.datetime) -> tuple[bool, Any]:
    """(INTERNAL_ST may go in, the ST agreement used): the ST confidentiality is
    over (its end date is past), or a valid agreement of the tutor exists."""
    from .models import StExportConsent

    until = thesis.st_confidential_until
    if until is not None and until < timezone.localdate(now):
        return True, None
    consent = (
        StExportConsent.objects.filter(thesis=thesis, revoked_at__isnull=True, valid_until__gt=now)
        .order_by("-created_at")
        .first()
    )
    return consent is not None, consent


class _Context:
    def __init__(self, user: Any, thesis: Thesis, levels: frozenset[L]) -> None:
        from apps.vault import services as vault

        self.user, self.thesis, self.levels = user, thesis, levels
        self.level_values = [lv.value for lv in levels]
        self.vault = vault
        self.text_counts: Counter[str] = Counter()
        self.text_bytes = 0
        self.exported_documents: set[uuid.UUID] = set()
        self.confidential: set[str] = set()
        rows = (
            Membership.objects.filter(thesis=thesis)
            .select_related("user", "organization")
            .order_by("active_from", "pk")
        )
        self.memberships = list(rows)
        self.role_of = {m.user_id: m.role for m in self.memberships}  # the latest one
        self.name_of = {m.user_id: m.user.get_full_name() for m in self.memberships}

    def person(self, user_id: Any) -> dict[str, Any] | None:
        if user_id is None:
            return None
        return {"nom": self.name_of.get(user_id, ""), "role": self.role_of.get(user_id)}

    def texts(self, objs: list[Any], fields: tuple[str, ...]) -> dict[tuple[Any, str], str]:
        items = [(o, name) for o in objs for name in fields]
        found = self.vault.export_texts(self.user, self.thesis, items, levels=self.levels)
        levels = {o.pk: self._level_of(o) for o in objs}
        for (pk, _name), text in found.items():
            self.text_counts[levels[pk]] += 1
            self.text_bytes += len(text.encode())
        if self.text_bytes > TEXTS_MAX_BYTES:
            raise TextsTooLargeError
        return found

    def _level_of(self, obj: Any) -> str:
        for path in (
            "level",
            "meeting.level",
            "objective.level",
            "submission.level",
            "mission.level",
            "expense.mission.level",
        ):
            value: Any = obj
            try:
                for part in path.split("."):
                    value = getattr(value, part)
            except AttributeError:
                continue
            return str(value)
        return L.PUBLIC.value

    def document(self, document_id: Any) -> str | None:
        """The id of a linked document if it is exported, else None."""
        return str(document_id) if document_id in self.exported_documents else None

    def note_confidential(self, obj: Any, level: str) -> None:
        if level == L.CONFIDENTIAL.value:
            self.confidential.add(str(obj.pk))


# --------------------------------------------------------------------------- the files


def _these(c: _Context) -> dict[str, Any]:
    t = c.thesis
    return {
        "id": str(t.pk),
        "titre": t.title,
        "statut": t.status,
        "debut_cifre": _iso(t.cifre_start_date),
        "premiere_inscription": _iso(t.first_enrollment_date),
        "fin_prevue": _iso(t.planned_end_date),
        "soutenue_le": _iso(t.defended_on),
        "close_le": _iso(t.closed_at),
        "confidentialite_st_jusqu_au": _iso(t.st_confidential_until),
    }


def _equipe(c: _Context) -> dict[str, Any]:
    def organisation(m: Membership) -> str | None:
        return m.organization.name if m.organization is not None else None

    mine = [m for m in c.memberships if m.user_id == c.user.pk]
    others: list[dict[str, Any]] = []
    for m in c.memberships:
        if m.user_id == c.user.pk:
            continue
        row = {"nom": m.user.get_full_name(), "role": m.role, "organisme": organisation(m)}
        if row not in others:
            others.append(row)
    return {
        "doctorant": {
            "nom": c.user.get_full_name(),
            "adhesions": [
                {
                    "role": m.role,
                    "organisme": organisation(m),
                    "debut": _iso(m.active_from),
                    "fin": _iso(m.active_until),
                }
                for m in mine
            ],
        },
        "membres": sorted(others, key=lambda p: (p["role"], p["nom"])),
    }


def _documents(c: _Context, all_versions: bool, snap: Snapshot) -> list[dict[str, Any]]:
    index = []
    planned_docs = c.vault.export_documents(
        c.user, c.thesis, all_versions=all_versions, levels=c.levels
    )
    for n, (document, planned, every) in enumerate(planned_docs, start=1):
        c.exported_documents.add(document.pk)
        level = document.confidentiality
        c.note_confidential(document, level)
        names = {}
        for p in planned:
            name = entry_name(n, document.title, p.version, p.extension)
            names[p.version] = name
            snap.documents.append((name, p))
            snap.total_bytes += p.size
            snap.counts.setdefault("versions", Counter())[level] += 1
        snap.counts.setdefault("documents", Counter())[level] += 1
        index.append(
            {
                "id": str(document.pk),
                "titre": document.title,
                "niveau": level,
                "versions": [
                    {
                        "version": v.version,
                        "type": v.mime_type,
                        "taille": v.size,
                        "deposee_le": _iso(v.created_at),
                        "fichier": names.get(v.version),
                    }
                    for v in every
                ],
            }
        )
    return index


def _objectifs(c: _Context) -> dict[str, Any]:
    from apps.objectives.models import (
        Objective,
        ObjectiveComment,
        ObjectiveRevision,
        ProgressUpdate,
        ThesisYear,
    )

    years = ThesisYear.objects.filter(thesis=c.thesis).order_by("number")
    rows = list(
        Objective.objects.readable_by(c.user, c.thesis)
        .filter(level__in=c.level_values)
        .select_related("year")
        .order_by("created_at", "pk")
    )
    texts = c.texts(rows, ("title", "description", "success_criterion"))
    revisions = list(
        ObjectiveRevision.objects.filter(objective__in=rows)
        .select_related("objective")
        .order_by("created_at", "pk")
    )
    rev_texts = c.texts(revisions, ("reason", "old_value", "new_value"))
    progress = list(
        ProgressUpdate.objects.filter(objective__in=rows)
        .select_related("objective")
        .order_by("date", "created_at", "pk")
    )
    progress_texts = c.texts(progress, ("note",))
    comments: list[Any] = []
    if Action.OBJECTIVE_COMMENT_READ in allowed_actions(c.user, c.thesis):
        comments = list(
            ObjectiveComment.objects.filter(objective__in=rows)
            .select_related("objective")
            .order_by("created_at", "pk")
        )
    comment_texts = c.texts(comments, ("body",))
    out = []
    for o in rows:
        c.note_confidential(o, o.level)
        out.append(
            {
                "id": str(o.pk),
                "annee": o.year.number if o.year else None,
                "categorie": o.category,
                "echeance": _iso(o.due_date),
                "poids": o.weight,
                "statut": o.status,
                "avancement": o.progress,
                "niveau": o.level,
                "titre": texts.get((o.pk, "title"), ""),
                "description": texts.get((o.pk, "description"), ""),
                "critere_de_reussite": texts.get((o.pk, "success_criterion"), ""),
                "approuve_par": c.person(o.approved_by_id),
                "approuve_le": _iso(o.approved_at),
                "modifie_depuis_l_approbation": o.changed_since_approval,
                "resultat_declare": o.result_declared,
                "pris_en_compte_par": c.person(o.acknowledged_by_id),
                "pris_en_compte_le": _iso(o.acknowledged_at),
                "revisions": [
                    {
                        "champ": r.field,
                        "ancienne_valeur": rev_texts.get((r.pk, "old_value"), r.old_clear),
                        "nouvelle_valeur": rev_texts.get((r.pk, "new_value"), r.new_clear),
                        "motif": rev_texts.get((r.pk, "reason"), ""),
                        "par": c.person(r.changed_by_id),
                        "le": _iso(r.created_at),
                        "pris_en_compte_le": _iso(r.acknowledged_at),
                    }
                    for r in revisions
                    if r.objective_id == o.pk
                ],
                "avancements": [
                    {
                        "date": _iso(p.date),
                        "avancement": p.progress,
                        "statut": p.status,
                        "note": progress_texts.get((p.pk, "note"), ""),
                        "preuve": c.document(p.evidence_id),
                    }
                    for p in progress
                    if p.objective_id == o.pk
                ],
                "commentaires": [
                    {
                        "auteur": c.person(m.author_id),
                        "le": _iso(m.created_at),
                        "texte": comment_texts.get((m.pk, "body"), ""),
                    }
                    for m in comments
                    if m.objective_id == o.pk
                ],
            }
        )
    return {
        "annees": [
            {
                "numero": y.number,
                "debut_cifre": _iso(y.cifre_start),
                "fin_cifre": _iso(y.cifre_end),
                "debut_universitaire": _iso(y.academic_start),
                "fin_universitaire": _iso(y.academic_end),
            }
            for y in years
        ],
        "objectifs": out,
    }


def _reunions(c: _Context, objective_ids: set[str]) -> dict[str, Any]:
    from apps.meetings.models import (
        ActionItem,
        Attendee,
        Decision,
        Meeting,
        MeetingDocument,
        MinutesAcknowledgement,
    )

    rows = list(
        Meeting.objects.readable_by(c.user, c.thesis)
        .filter(level__in=c.level_values)
        .order_by("starts_at", "pk")
    )
    texts = c.texts(rows, ("title", "agenda", "minutes", "location_detail", "visio_link"))
    decisions = list(
        Decision.objects.filter(meeting__in=rows)
        .select_related("meeting")
        .order_by("created_at", "pk")
    )
    decision_texts = c.texts(decisions, ("text",))
    actions = list(
        ActionItem.objects.filter(meeting__in=rows)
        .select_related("meeting")
        .order_by("created_at", "pk")
    )
    action_texts = c.texts(actions, ("title",))
    attendees = list(Attendee.objects.filter(meeting__in=rows).order_by("created_at", "pk"))
    marks = list(
        MinutesAcknowledgement.objects.filter(meeting__in=rows).order_by("created_at", "pk")
    )
    links = list(MeetingDocument.objects.filter(meeting__in=rows).order_by("created_at", "pk"))
    out = []
    for m in rows:
        c.note_confidential(m, m.level)
        out.append(
            {
                "id": str(m.pk),
                "type": m.kind,
                "lieu_de_presentation": m.presentation_venue,
                "debut": _iso(m.starts_at),
                "duree_minutes": m.duration_minutes,
                "lieu": m.location,
                "niveau": m.level,
                "statut": m.status,
                "annulee_le": _iso(m.cancelled_at),
                "titre": texts.get((m.pk, "title"), ""),
                "ordre_du_jour": texts.get((m.pk, "agenda"), ""),
                "compte_rendu": texts.get((m.pk, "minutes"), ""),
                "lieu_precis": texts.get((m.pk, "location_detail"), ""),
                "lien_visio": texts.get((m.pk, "visio_link"), ""),
                "participants": [c.person(a.user_id) for a in attendees if a.meeting_id == m.pk],
                "lectures_du_compte_rendu": [
                    {
                        "personne": c.person(k.user_id),
                        "version": k.minutes_version,
                        "type": k.kind,
                        "le": _iso(k.created_at),
                    }
                    for k in marks
                    if k.meeting_id == m.pk
                ],
                "decisions": [
                    {
                        "texte": decision_texts.get((d.pk, "text"), ""),
                        "le": _iso(d.created_at),
                        "retiree_le": _iso(d.withdrawn_at),
                    }
                    for d in decisions
                    if d.meeting_id == m.pk
                ],
                "actions": [
                    {
                        "titre": action_texts.get((a.pk, "title"), ""),
                        "responsable": c.person(a.assignee_id),
                        "echeance": _iso(a.due_date),
                        "statut": a.status,
                        "faite_le": _iso(a.done_at),
                        "objectif": str(a.objective_id)
                        if str(a.objective_id) in objective_ids
                        else None,
                    }
                    for a in actions
                    if a.meeting_id == m.pk
                ],
                "documents": [
                    d
                    for d in (c.document(x.document_id) for x in links if x.meeting_id == m.pk)
                    if d
                ],
            }
        )
    return {"reunions": out}


def _dossier(c: _Context) -> dict[str, Any]:
    from apps.admin_file import services as admin_file
    from apps.admin_file.models import DueDateProposal, ItemApproval, ItemReopening

    items = list(admin_file.visible_items(c.user, c.thesis).order_by("section", "code", "pk"))
    approvals = list(ItemApproval.objects.filter(item__in=items).order_by("created_at", "pk"))
    reopenings = list(ItemReopening.objects.filter(item__in=items).order_by("created_at", "pk"))
    proposals = list(DueDateProposal.objects.filter(item__in=items).order_by("created_at", "pk"))
    return {
        "pieces": [
            {
                "id": str(i.pk),
                "code": i.code,
                "libelle": admin_file.label_of(i),
                "section": i.section,
                "statut": i.status,
                "obligatoire": i.mandatory,
                "piece_jointe_requise": i.attachment_required,
                "echeance": _iso(i.due_date),
                "cycle": i.cycle,
                "document": c.document(i.document_id),
                "version_deposee": i.submitted_version if c.document(i.document_id) else None,
                "deposee_par": c.person(i.submitted_by_id),
                "validee_le": _iso(i.validated_at),
                "validations": [
                    {
                        "cycle": a.cycle,
                        "role": a.role,
                        "par": c.person(a.user_id),
                        "version": a.version,
                        "le": _iso(a.created_at),
                    }
                    for a in approvals
                    if a.item_id == i.pk
                ],
                "reouvertures": [
                    {
                        "cycle": r.cycle,
                        "par": c.person(r.reopened_by_id),
                        "motif": r.reason_code,
                        "commentaire": r.reason_comment,
                        "le": _iso(r.created_at),
                        "validee_le": _iso(r.validated_at),
                    }
                    for r in reopenings
                    if r.item_id == i.pk
                ],
                "echeances_proposees": [
                    {
                        "proposee_par": c.person(p.proposed_by_id),
                        "de": _iso(p.from_date),
                        "a": _iso(p.to_date),
                        "motif": p.reason_code,
                        "commentaire": p.comment,
                        "statut": p.status,
                        "decidee_par": c.person(p.decided_by_id),
                        "decidee_le": _iso(p.decided_at),
                    }
                    for p in proposals
                    if p.item_id == i.pk
                ],
            }
            for i in items
        ]
    }


def _version_ref(c: _Context, version: Any) -> dict[str, Any] | None:
    if version is None or c.document(version.document_id) is None:
        return None
    return {"document": str(version.document_id), "version": version.version}


def _conferences(c: _Context, objective_ids: set[str]) -> dict[str, Any]:
    from apps.conferences.models import (
        Deadline,
        Edition,
        Preprint,
        PublicationPolicy,
        StApproval,
        Submission,
        SubmissionAuthor,
        SubmissionDocument,
        Venue,
    )

    if Action.CONFERENCE_READ not in allowed_actions(c.user, c.thesis):
        return {"lieux": [], "editions": [], "echeances": [], "politique": None, "soumissions": []}
    venues = Venue.objects.filter(thesis=c.thesis).order_by("acronym", "pk")
    editions = Edition.objects.filter(thesis=c.thesis).order_by("year", "pk")
    deadlines = Deadline.objects.filter(thesis=c.thesis).order_by("due_at", "pk")
    policy = PublicationPolicy.objects.filter(thesis=c.thesis).first()
    rows = list(
        Submission.objects.readable_by(c.user, c.thesis)
        .filter(level__in=c.level_values)
        .select_related("submitted_version", "published_version")
        .order_by("created_at", "pk")
    )
    ids = {s.pk for s in rows}
    texts = c.texts(rows, ("title", "abstract", "external_authors", "notes"))
    approvals = list(
        StApproval.objects.filter(submission__in=rows)
        .select_related("submission", "version")
        .order_by("created_at", "pk")
    )
    approval_texts = c.texts(approvals, ("comment",))
    preprints = list(
        Preprint.objects.filter(submission__in=rows)
        .select_related("submission", "version")
        .order_by("posted_on", "pk")
    )
    preprint_texts = c.texts(preprints, ("url",))
    authors = list(
        SubmissionAuthor.objects.filter(submission__in=rows).order_by("created_at", "pk")
    )
    links = list(
        SubmissionDocument.objects.filter(submission__in=rows).order_by("created_at", "pk")
    )
    out = []
    for s in rows:
        c.note_confidential(s, s.level)
        out.append(
            {
                "id": str(s.pk),
                "lieu": _id(s.venue_id),
                "edition": _id(s.edition_id),
                "objectif": str(s.objective_id) if str(s.objective_id) in objective_ids else None,
                "niveau": s.level,
                "statut": s.status,
                "travail_st": s.st_work,
                "version_soumise": _version_ref(c, s.submitted_version),
                "soumise_le": _iso(s.submitted_at),
                "version_publiee": _version_ref(c, s.published_version),
                "publiee_le": _iso(s.published_at),
                "reprise_de": str(s.resubmission_of_id) if s.resubmission_of_id in ids else None,
                "titre": texts.get((s.pk, "title"), ""),
                "resume": texts.get((s.pk, "abstract"), ""),
                "auteurs_externes": texts.get((s.pk, "external_authors"), ""),
                "notes": texts.get((s.pk, "notes"), ""),
                "auteurs": [c.person(a.user_id) for a in authors if a.submission_id == s.pk],
                "documents": [
                    d
                    for d in (c.document(x.document_id) for x in links if x.submission_id == s.pk)
                    if d
                ],
                "accords_st": [
                    {
                        "objet": a.purpose,
                        "version": _version_ref(c, a.version),
                        "delai_jours": a.review_days,
                        "demande_avant": _iso(a.request_due),
                        "demande_par": c.person(a.requested_by_id),
                        "decision": a.decision,
                        "motif": a.reason,
                        "commentaire": approval_texts.get((a.pk, "comment"), ""),
                        "decide_par": c.person(a.decided_by_id),
                        "decide_le": _iso(a.decided_at),
                    }
                    for a in approvals
                    if a.submission_id == s.pk
                ],
                "prepublications": [
                    {
                        "plateforme": p.platform,
                        "le": _iso(p.posted_on),
                        "url": preprint_texts.get((p.pk, "url"), ""),
                        "version": _version_ref(c, p.version),
                    }
                    for p in preprints
                    if p.submission_id == s.pk
                ],
            }
        )
    return {
        "lieux": [
            {"id": str(v.pk), "type": v.kind, "sigle": v.acronym, "nom": v.name, "url": v.url}
            for v in venues
        ],
        "editions": [
            {
                "id": str(e.pk),
                "lieu": str(e.venue_id),
                "annee": e.year,
                "lieu_de_l_evenement": e.location,
                "debut": _iso(e.event_start),
                "fin": _iso(e.event_end),
            }
            for e in editions
        ],
        "echeances": [
            {
                "id": str(d.pk),
                "edition": str(d.edition_id),
                "type": d.kind,
                "date": _iso(d.due_at),
                "fuseau": d.tz_name,
                "annulee_le": _iso(d.cancelled_at),
            }
            for d in deadlines
        ],
        "politique": {"delai_relecture_st_jours": policy.st_review_days} if policy else None,
        "soumissions": out,
    }


def _deplacements(c: _Context, meeting_ids: set[str], submission_ids: set[str]) -> dict[str, Any]:
    from apps.accounts.permissions import can_access_level
    from apps.travel.models import (
        Expense,
        ExpenseReceipt,
        Mission,
        MissionOrder,
        PresenceDay,
        TimeSplitTarget,
        TravelBudget,
        TravelPolicy,
    )

    if Action.TRAVEL_READ not in allowed_actions(c.user, c.thesis):
        return {"jours": [], "repartition": [], "regles": None, "missions": [], "budgets": []}
    days = PresenceDay.objects.visible_to(c.user, c.thesis).order_by("day")
    targets = TimeSplitTarget.objects.filter(thesis=c.thesis).order_by("cifre_year")
    policy = TravelPolicy.objects.filter(thesis=c.thesis).first()
    rows = list(
        Mission.objects.visible_to(c.user, c.thesis)
        .filter(level__in=c.level_values)
        .order_by("start_date", "pk")
    )
    texts = c.texts(rows, ("destination_detail", "purpose", "lodging_note"))
    orders = list(
        MissionOrder.objects.filter(mission__in=rows)
        .select_related("mission")
        .order_by("created_at", "pk")
    )
    order_texts = c.texts(orders, ("comment", "estimated_total"))
    expenses = list(
        Expense.objects.filter(mission__in=rows)
        .select_related("mission")
        .order_by("created_at", "pk")
    )
    expense_texts = c.texts(expenses, ("estimated_amount", "actual_amount"))
    receipts = list(
        ExpenseReceipt.objects.filter(expense__in=expenses).order_by("created_at", "pk")
    )
    budgets = [
        b
        for b in TravelBudget.objects.filter(thesis=c.thesis, level__in=c.level_values).order_by(
            "year", "payer"
        )
        if can_access_level(c.user, c.thesis, L(b.level))
    ]
    budget_texts = c.texts(budgets, ("amount",))
    out = []
    for m in rows:
        c.note_confidential(m, m.level)
        out.append(
            {
                "id": str(m.pk),
                "debut": _iso(m.start_date),
                "fin": _iso(m.end_date),
                "destination": m.destination,
                "edition": _id(m.edition_id),
                "reunion": str(m.meeting_id) if str(m.meeting_id) in meeting_ids else None,
                "soumission": str(m.submission_id)
                if str(m.submission_id) in submission_ids
                else None,
                "transports": list(m.transports),
                "hebergement": m.lodging,
                "payeur": m.payer,
                "niveau": m.level,
                "statut": m.status,
                "billets_reserves": m.tickets_booked,
                "hebergement_reserve": m.lodging_booked,
                "ordre_officiel_obtenu": m.official_order_obtained,
                "ordre_officiel": c.document(m.official_order_document_id),
                "destination_precise": texts.get((m.pk, "destination_detail"), ""),
                "motif": texts.get((m.pk, "purpose"), ""),
                "note_hebergement": texts.get((m.pk, "lodging_note"), ""),
                "ordres": [
                    {
                        "payeur": o.payer,
                        "demande_par": c.person(o.requested_by_id),
                        "debut": _iso(o.snapshot_start),
                        "fin": _iso(o.snapshot_end),
                        "destination": o.snapshot_destination,
                        "decision": o.decision,
                        "motif": o.reason,
                        "decide_par": c.person(o.decided_by_id),
                        "decide_le": _iso(o.decided_at),
                        "commentaire": order_texts.get((o.pk, "comment"), ""),
                        "total_estime": order_texts.get((o.pk, "estimated_total"), ""),
                    }
                    for o in orders
                    if o.mission_id == m.pk
                ],
                "frais": [
                    {
                        "categorie": e.category,
                        "forfait": e.flat_rate,
                        "montant_estime": expense_texts.get((e.pk, "estimated_amount"), ""),
                        "montant_reel": expense_texts.get((e.pk, "actual_amount"), ""),
                        "justificatifs": [
                            d
                            for d in (
                                c.document(r.document_id) for r in receipts if r.expense_id == e.pk
                            )
                            if d
                        ],
                    }
                    for e in expenses
                    if e.mission_id == m.pk
                ],
            }
        )
    for b in budgets:
        c.note_confidential(b, b.level)
    return {
        "jours": [{"jour": _iso(d.day), "nature": d.kind} for d in days],
        "repartition": [
            {
                "annee_cifre": t.cifre_year,
                "st_pourcent": t.st_percent,
                "labo_pourcent": t.lab_percent,
            }
            for t in targets
        ],
        "regles": None
        if policy is None
        else {
            "teletravail_compte_comme": policy.remote_counts_as,
            "conference_compte_comme": policy.conference_counts_as,
            "lundi_de_pentecote_ferie": policy.whit_monday_holiday,
            "seuil_d_alerte_points": policy.gap_alert_points,
            "jours_labo_minimum_par_mois": policy.min_lab_days_per_month,
            "delai_ordre_de_mission_jours": policy.order_lead_days,
            "delai_justificatifs_jours": policy.receipt_grace_days,
        },
        "missions": out,
        "budgets": [
            {
                "payeur": b.payer,
                "annee": b.year,
                "niveau": b.level,
                "montant": budget_texts.get((b.pk, "amount"), ""),
            }
            for b in budgets
        ],
    }


def _portfolio(c: _Context) -> dict[str, Any]:
    """The trainings (M9a), texts decrypted, at the exported levels; the date of the
    next re-registration."""
    from apps.portfolio.models import PortfolioPolicy, Training

    policy = PortfolioPolicy.objects.filter(thesis=c.thesis).first()
    out: dict[str, Any] = {
        "reinscription": _iso(policy.reregistration_date) if policy else None,
        "formations": [],
    }
    if Action.TRAINING_READ not in allowed_actions(c.user, c.thesis):
        return out
    rows = list(
        Training.objects.readable_by(c.user, c.thesis)
        .filter(level__in=c.level_values)
        .order_by("start_date", "created_at", "pk")
    )
    texts = c.texts(rows, ("title", "provider", "notes"))
    for t in rows:
        c.note_confidential(t, t.level)
        out["formations"].append(
            {
                "id": str(t.pk),
                "intitule": texts.get((t.pk, "title"), ""),
                "organisme": texts.get((t.pk, "provider"), ""),
                "notes": texts.get((t.pk, "notes"), ""),
                "categorie": t.category,
                "heures": str(t.hours),
                "debut": _iso(t.start_date),
                "fin": _iso(t.end_date),
                "niveau": t.level,
                "etat_adum": t.adum_status,
                "attestation": c.document(t.certificate_id),
            }
        )
    return out


def _journal(c: _Context) -> list[dict[str, Any]]:
    from apps.vault.models import AuditEvent

    rows = AuditEvent.objects.filter(
        Q(thesis=c.thesis) | Q(thesis__isnull=True), actor_id=c.user.pk
    ).order_by("seq")
    return [
        {
            "seq": e.seq,
            "timestamp": _iso(e.timestamp),
            "action": e.action,
            "object_type": e.object_type,
            "object_id": e.object_id,
            "hash": e.hash,
            "details": {k: v for k, v in sorted(e.details_json.items()) if k in SAFE_DETAIL_KEYS},
        }
        for e in rows
    ]


def _st_counts(c: _Context, all_versions: bool) -> int:
    """How many top-level objects of the student are INTERNAL_ST (excluded, or
    included under an ST agreement or after the ST confidentiality)."""
    from apps.conferences.models import Submission
    from apps.meetings.models import Meeting
    from apps.objectives.models import Objective
    from apps.portfolio.models import Training
    from apps.travel.models import Mission, TravelBudget

    st = L.INTERNAL_ST
    if st not in readable_levels(c.user, c.thesis):
        return 0
    only = frozenset({st})
    count = len(c.vault.export_documents(c.user, c.thesis, all_versions=all_versions, levels=only))
    for model in (Meeting, Submission, Objective, Training):
        count += model.objects.readable_by(c.user, c.thesis).filter(level=st.value).count()
    count += Mission.objects.visible_to(c.user, c.thesis).filter(level=st.value).count()
    count += TravelBudget.objects.filter(thesis=c.thesis, level=st.value).count()
    return count


# --------------------------------------------------------------------------- take


def take(user: Any, thesis: Thesis, *, all_versions: bool, now: dt.datetime) -> Snapshot:
    """Everything of the export, read inside the caller's snapshot transaction."""
    st_open, consent = st_state(thesis, now)
    levels = readable_levels(user, thesis)
    if not st_open:
        levels = levels - {L.INTERNAL_ST}
    c = _Context(user, thesis, frozenset(levels))
    snap = Snapshot(files={}, documents=[], total_bytes=0, consent=consent, st_open=st_open)
    snap.files["documents.json"] = _documents(c, all_versions, snap)
    objectifs = _objectifs(c)
    objective_ids = {o["id"] for o in objectifs["objectifs"]}
    reunions = _reunions(c, objective_ids)
    conferences = _conferences(c, objective_ids)
    snap.files.update(
        {
            "these.json": _these(c),
            "equipe.json": _equipe(c),
            "objectifs.json": objectifs,
            "reunions.json": reunions,
            "dossier-administratif.json": _dossier(c),
            "conferences.json": conferences,
            "deplacements.json": _deplacements(
                c,
                {m["id"] for m in reunions["reunions"]},
                {s["id"] for s in conferences["soumissions"]},
            ),
            "portfolio.json": _portfolio(c),
            "journal/mes-evenements.json": _journal(c),
        }
    )
    st_count = _st_counts(c, all_versions)
    if st_open:
        snap.st_included = st_count
        snap.excluded = {}
    else:
        snap.excluded = {L.INTERNAL_ST.value: st_count} if st_count else {}
    snap.counts["texts"] = c.text_counts
    snap.confidential_ids = sorted(c.confidential)
    snap.counts = {k: dict(v) for k, v in snap.counts.items()}
    return snap
