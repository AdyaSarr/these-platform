"""Migration 0004 on a log that already holds rows (M1): renumbered 1..N, chained."""

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.vault.audit import verify_chain
from apps.vault.models import AuditEvent

BEFORE = [("vault", "0003_documents")]


@pytest.mark.django_db(transaction=True)
@pytest.mark.without_vault_keys
def test_existing_rows_are_renumbered_and_chained() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old = executor.loader.project_state(BEFORE).apps.get_model("vault", "AuditEvent")
    for seq in (3, 7, 8):  # M1 left gaps (sequence values lost on rollback)
        old.objects.create(seq=seq, action="LOGIN_FAILED", details_json={"former_seq": seq})

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    rows = list(AuditEvent.objects.order_by("seq"))
    assert [r.seq for r in rows] == [1, 2, 3]
    assert [r.details_json["former_seq"] for r in rows] == [3, 7, 8]  # order kept
    assert rows[0].prev_hash == "0" * 64
    assert [r.prev_hash for r in rows[1:]] == [r.hash for r in rows[:-1]]
    assert verify_chain().findings == []
