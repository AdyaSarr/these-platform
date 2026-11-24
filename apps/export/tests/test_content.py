"""What the ZIP holds (M13b, criteria 5, 6 and 9): file by file, the latest version
by default and every version on request, the sealed texts decrypted, a coherent
manifest, names built by us."""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from apps.accounts.models import Membership
from apps.export.tests import helpers as h
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db(transaction=True)
DATA_FILES = [
    "LISEZMOI.txt", "conferences.json", "deplacements.json", "documents.json",
    "dossier-administratif.json", "equipe.json", "journal/mes-evenements.json",
    "manifest.json", "objectifs.json", "reunions.json", "these.json",
]  # fmt: skip
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9/-]*\.[a-z0-9]+")


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _documents(archive: Any) -> list[str]:
    return sorted(n for n in archive.namelist() if n.startswith("documents/"))


def test_the_entries_latest_versions(team: SimpleNamespace) -> None:
    h.world(team)
    archive = h.run(team)
    assert sorted(n for n in archive.namelist() if not n.startswith("documents/")) == DATA_FILES
    assert _documents(archive) == [
        "documents/0001-rapport-annuel-v2.pdf",
        "documents/0002-contrat-fictif-v1.pdf",
    ]
    assert archive.read("documents/0001-rapport-annuel-v2.pdf") == h.pdf_bytes("contenu-public-v2")
    assert archive.read("documents/0002-contrat-fictif-v1.pdf") == h.pdf_bytes(
        "contenu-confidentiel"
    )


def test_every_version_on_request(team: SimpleNamespace) -> None:
    h.world(team)
    archive = h.run(team, all_versions=True)
    assert _documents(archive) == [
        "documents/0001-rapport-annuel-v1.pdf",
        "documents/0001-rapport-annuel-v2.pdf",
        "documents/0002-contrat-fictif-v1.pdf",
    ]
    assert archive.read("documents/0001-rapport-annuel-v1.pdf") == h.pdf_bytes("contenu-public-v1")


def test_the_thesis(team: SimpleNamespace) -> None:
    t = team.thesis
    assert h.read(h.run(team), "these.json") == {
        "id": str(t.pk),
        "titre": t.title,
        "statut": t.status,
        "debut_cifre": t.cifre_start_date.isoformat(),
        "premiere_inscription": t.first_enrollment_date.isoformat(),
        "fin_prevue": t.planned_end_date.isoformat(),
        "soutenue_le": None,
        "close_le": None,
        "confidentialite_st_jusqu_au": None,
    }


def test_the_team_names_roles_and_organisations_only(team: SimpleNamespace) -> None:
    rows = Membership.objects.filter(thesis=team.thesis).select_related("user", "organization")
    mine = [m for m in rows if m.user_id == team.student.pk]
    others = sorted(
        ({"nom": m.user.get_full_name(), "role": m.role, "organisme": m.organization.name}
         for m in rows if m.user_id != team.student.pk),
        key=lambda p: (p["role"], p["nom"]),
    )  # fmt: skip
    assert h.read(h.run(team), "equipe.json") == {
        "doctorant": {
            "nom": team.student.get_full_name(),
            "adhesions": [
                {
                    "role": m.role,
                    "organisme": m.organization.name,
                    "debut": m.active_from.isoformat(),
                    "fin": m.active_until.isoformat() if m.active_until else None,
                }
                for m in mine
            ],
        },
        "membres": others,
    }


def test_documents_index_titles_levels_and_files(team: SimpleNamespace) -> None:
    w = h.world(team)
    index = h.read(h.run(team), "documents.json")
    by_id = {d["id"]: d for d in index}
    assert set(by_id) == {str(w.doc.pk), str(w.conf.pk)}
    assert by_id[str(w.doc.pk)]["titre"] == "Rapport annuel"
    assert by_id[str(w.conf.pk)]["niveau"] == "CONFIDENTIAL"
    assert [v["fichier"] for v in by_id[str(w.doc.pk)]["versions"]] == [
        None, "documents/0001-rapport-annuel-v2.pdf",
    ]  # fmt: skip


def test_sealed_texts_are_decrypted(team: SimpleNamespace) -> None:
    w = h.world(team)
    archive = h.run(team)
    (objective,) = h.read(archive, "objectifs.json")["objectifs"]
    assert (objective["titre"], objective["description"]) == (
        "Objectif fictif", "Description fictive",
    )  # fmt: skip
    meetings = {m["id"]: m for m in h.read(archive, "reunions.json")["reunions"]}
    assert meetings[str(w.meeting.pk)]["titre"] == "Point d'avancement fictif"
    missions = {m["id"]: m for m in h.read(archive, "deplacements.json")["missions"]}
    assert missions[str(w.mission.pk)]["motif"] == "Motif fictif"


def test_a_supervisor_comment_with_name_and_role(team: SimpleNamespace) -> None:
    h.world(team)
    (objective,) = h.read(h.run(team), "objectifs.json")["objectifs"]
    (comment,) = objective["commentaires"]
    assert comment["texte"] == "Commentaire du directeur fictif"
    assert comment["auteur"] == {"nom": team.director.get_full_name(), "role": "DIRECTOR"}


def test_people_are_a_name_and_a_role(team: SimpleNamespace) -> None:
    w = h.world(team)
    meetings = {m["id"]: m for m in h.read(h.run(team), "reunions.json")["reunions"]}
    assert sorted(meetings[str(w.meeting.pk)]["participants"], key=lambda p: p["role"]) == [
        {"nom": team.director.get_full_name(), "role": "DIRECTOR"},
        {"nom": team.student.get_full_name(), "role": "PHD_STUDENT"},
    ]


def test_the_journal_holds_the_student_s_own_lines(team: SimpleNamespace) -> None:
    w = h.world(team)
    from django.db.models import Q

    own = AuditEvent.objects.filter(
        Q(thesis=team.thesis) | Q(thesis__isnull=True), actor=team.student
    )
    expected = set(own.values_list("seq", flat=True))
    lines = h.read(h.run(team), "journal/mes-evenements.json")
    assert {line["seq"] for line in lines} == expected
    for line in lines:
        assert set(line) == {"seq", "timestamp", "action", "object_type", "object_id", "hash",
                             "details"}  # fmt: skip
    failed = next(line for line in lines if line["seq"] == w.failed.seq)
    assert "email_hmac" not in failed["details"]


def test_the_manifest_lists_every_entry_with_its_digest(team: SimpleNamespace) -> None:
    w = h.world(team)
    archive = h.run(team)
    manifest = h.read(archive, "manifest.json")
    assert manifest["format"] == "these-export/1"
    assert manifest["thesis_id"] == str(team.thesis.pk)
    assert manifest["options"] == {"all_versions": False, "st_consent_id": None}
    entries = {e["path"]: e for e in manifest["entries"]}
    assert set(entries) == set(archive.namelist()) - {"manifest.json"}
    for path, entry in entries.items():
        data = archive.read(path)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest(), path
        assert entry["size"] == len(data), path
    assert entries["documents/0002-contrat-fictif-v1.pdf"]["level"] == "CONFIDENTIAL"
    assert entries["documents/0002-contrat-fictif-v1.pdf"]["object_id"] == str(w.conf.pk)
    assert manifest["excluded"]["INTERNAL_ST"] >= 3
    text = archive.read("manifest.json").decode()
    for title in ("Rapport annuel", "Contrat fictif", team.thesis.title):
        assert title not in text


HOSTILE = ["../../etc/passwd", "/abs/chemin", "C:\\fenetre\\x", "a\u202eb", "x" * 200,
           "\U0001f642\U0001f642", "Même titre", "Même titre", "..", "con.pdf.exe"]  # fmt: skip


def test_entry_names_are_built_by_us(team: SimpleNamespace) -> None:
    from apps.vault import services as vault

    for n, title in enumerate(HOSTILE):
        vault.store(thesis=team.thesis, upload=h.pdf(f"hostile-{n}"), title=title,
                    level=h.L.PUBLIC, user=team.student)  # fmt: skip
    names = h.run(team).namelist()
    assert len(names) == len(set(names))
    documents = [n for n in names if n.startswith("documents/")]
    assert len(documents) == len(HOSTILE)
    for name in names:
        assert SAFE_NAME.fullmatch(name), name
        assert ".." not in name and not name.startswith("/") and "\\" not in name
        assert len(name) <= 100
