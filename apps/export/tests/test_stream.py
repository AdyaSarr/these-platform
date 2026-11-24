"""How the ZIP is made (M13b, criteria 10 to 12): in a stream, nothing written to
disk, journaled before the first byte, stopped cleanly, capped."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings

from apps.export import services as export
from apps.export.models import ExportRecord
from apps.export.tests import helpers as h
from apps.lifecycle.tests.helpers import audited
from apps.vault.models import AuditEvent, DocumentVersion

pytestmark = pytest.mark.django_db(transaction=True)

# --------------------------------------------------------------------------- audit hook
# Python's audit hooks cannot be removed: one hook, switched on by WATCH only.
WATCH: list[list[str]] = []
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if not WATCH:
        return
    if event.startswith("tempfile."):
        WATCH[-1].append(f"{event} {args!r}")
    elif event == "open":
        path, mode, flags = ([*list(args), None, None, None])[:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & WRITE_FLAGS
        )
        if writing:
            WATCH[-1].append(f"open {path!r} {mode!r}")


sys.addaudithook(_hook)


@pytest.fixture
def team(time_machine: Any) -> SimpleNamespace:
    time_machine.move_to(h.NOW, tick=True)
    return h.make_team()


def _listing(root: Path) -> set[str]:
    return {str(p) for p in root.rglob("*")} if root.exists() else set()


def test_nothing_is_written_to_disk(team: SimpleNamespace) -> None:
    import tempfile

    h.world(team)
    roots = [Path(tempfile.gettempdir()), Path(settings.VAULT_STORAGE_DIR)]
    before = [_listing(r) for r in roots]
    WATCH.append([])
    try:
        h.run(team, all_versions=True)
    finally:
        seen = WATCH.pop()
    assert seen == []
    assert [_listing(r) for r in roots] == before


def test_the_event_is_written_before_the_first_byte(team: SimpleNamespace) -> None:
    w = h.world(team)
    started = export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True)
    (event,) = audited("THESIS_EXPORTED")  # nothing read from the stream yet
    assert event["all_versions"] is False and event["st_consent"] is None
    assert event["documents"] == {"PUBLIC": 1, "CONFIDENTIAL": 1}
    assert event["confidential_ids"] == [str(w.conf.pk)]
    assert event["excluded"]["INTERNAL_ST"] >= 3
    text = str(event)
    for title in ("Rapport annuel", "Contrat fictif", "Objectif fictif", team.thesis.title):
        assert title not in text
    b"".join(started.chunks())


def test_no_read_is_journaled_one_by_one(team: SimpleNamespace) -> None:
    h.world(team)
    last = AuditEvent.objects.order_by("-seq").values_list("seq", flat=True).first()
    h.run(team, all_versions=True)
    after = list(AuditEvent.objects.filter(seq__gt=last).values_list("action", flat=True))
    assert after == ["THESIS_EXPORTED"]


def test_the_stream_reads_one_document_at_a_time(
    team: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.vault import services as vault

    h.world(team)
    calls: list[str] = []
    real = vault.export_read

    def counting(planned: Any) -> bytes:
        calls.append(str(planned.version_id))
        return real(planned)

    monkeypatch.setattr(vault, "export_read", counting)
    chunks = export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True,
                                 all_versions=True).chunks()  # fmt: skip
    assert calls == []  # nothing decrypted before the stream is read
    next(chunks)
    assert len(calls) <= 1
    b"".join(chunks)
    assert len(calls) == 3


def test_a_missing_blob_stops_the_export(team: SimpleNamespace) -> None:
    w = h.world(team)
    started = export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True)
    version = DocumentVersion.objects.get(document=w.conf)
    (Path(settings.VAULT_STORAGE_DIR) / version.ciphertext_path).unlink()
    with pytest.raises(export.ExportInterruptedError):
        b"".join(started.chunks())
    record = ExportRecord.objects.get()
    assert (record.outcome, record.reason) == (ExportRecord.Outcome.INCOMPLETE, "blob_missing")
    assert audited("THESIS_EXPORT_INCOMPLETE") == [
        {"export": str(record.pk), "reason": "blob_missing"}
    ]


def test_a_client_that_goes_away(team: SimpleNamespace) -> None:
    h.world(team)
    chunks = export.start_export(thesis=team.thesis, user=team.student,
                                 recent_mfa=True).chunks()  # fmt: skip
    next(chunks)
    chunks.close()
    record = ExportRecord.objects.get()
    assert (record.outcome, record.reason) == (ExportRecord.Outcome.INCOMPLETE, "client_gone")
    assert [e["reason"] for e in audited("THESIS_EXPORT_INCOMPLETE")] == ["client_gone"]


def test_a_complete_export_is_recorded(team: SimpleNamespace) -> None:
    h.world(team)
    h.run(team)
    record = ExportRecord.objects.get()
    assert record.outcome == ExportRecord.Outcome.COMPLETE and record.finished_at is not None
    assert audited("THESIS_EXPORT_INCOMPLETE") == []


def test_the_size_is_capped_before_anything_starts(team: SimpleNamespace,
                                                   settings: Any) -> None:  # fmt: skip
    h.world(team)
    settings.EXPORT_MAX_BYTES = 100
    with pytest.raises(export.ExportTooLargeError):
        export.start_export(thesis=team.thesis, user=team.student, recent_mfa=True)
    assert ExportRecord.objects.count() == 0 and audited("THESIS_EXPORTED") == []


def test_the_default_cap_is_two_gibibytes() -> None:
    assert settings.EXPORT_MAX_BYTES == 2 * 1024**3
