"""audit_source(): events written inside it carry ``details.source``.

Used by demo_seed so that its events are recognizable in the log. Only the
content of ``details`` changes: chain_v1 and canonical_json are untouched
(test_chain_v1_frozen.py), and the chain verifies as usual.
"""

import threading

import pytest

from apps.vault.audit import AuditAction, audit, audit_source, verify_chain
from apps.vault.errors import AuditError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db


def test_events_inside_carry_the_source_and_outside_do_not() -> None:
    before = audit(AuditAction.THESIS_CREATED)
    with audit_source("demo_seed"):
        inside = audit(AuditAction.THESIS_CREATED, version=1)
    after = audit(AuditAction.THESIS_CREATED)
    assert "source" not in before.details_json
    assert inside.details_json == {"version": 1, "source": "demo_seed"}
    assert "source" not in after.details_json
    stored = AuditEvent.objects.get(seq=inside.seq)
    assert stored.details_json["source"] == "demo_seed"
    assert not verify_chain().altered


def test_reset_after_an_exception() -> None:
    with pytest.raises(RuntimeError), audit_source("demo_seed"):
        raise RuntimeError
    assert "source" not in audit(AuditAction.THESIS_CREATED).details_json


@pytest.mark.parametrize("name", ["", "Demo", "demo seed", "1demo", "a" * 65, "demo-seed"])
def test_invalid_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError), audit_source(name):
        pass


def test_nesting_is_refused() -> None:
    with audit_source("demo_seed"), pytest.raises(ValueError), audit_source("other"):
        pass


def test_a_caller_cannot_also_pass_source() -> None:
    with audit_source("demo_seed"), pytest.raises(AuditError, match="source"):
        audit(AuditAction.THESIS_CREATED, source="spoofed")


def test_other_threads_are_not_tagged() -> None:
    seen: list[object] = []

    def other() -> None:
        from apps.vault import audit as module

        seen.append(module._SOURCE.get())

    with audit_source("demo_seed"):
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen == [None]
