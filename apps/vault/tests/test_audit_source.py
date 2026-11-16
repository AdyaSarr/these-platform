"""audit_source(): events written inside it carry ``details.source``.

Used by demo_seed so that its events are recognizable in the log. Only the
content of ``details`` changes: chain_v1 and canonical_json are untouched
(test_chain_v1_frozen.py), and the chain verifies as usual.

Mechanism: a context manager that sets a ContextVar (one value per thread and
per asyncio task) and resets it in ``finally``; audit() reads it. The key
``source`` is reserved: audit() refuses it from any caller, so only
audit_source() can set it, and only to a name of the closed list AUDIT_SOURCES.
"""

import ast
import secrets
import threading
from pathlib import Path

import pytest
from django.test import Client
from django.urls import reverse

from apps.vault import audit as audit_module
from apps.vault.audit import AUDIT_SOURCES, AuditAction, audit, audit_source, verify_chain
from apps.vault.errors import AuditError
from apps.vault.models import AuditEvent

pytestmark = pytest.mark.django_db

APPS = Path(__file__).resolve().parents[2]
WRONG = secrets.token_urlsafe(16)  # generated: no password literal in the repo


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


def test_the_list_of_sources_is_closed() -> None:
    assert frozenset({"demo_seed"}) == AUDIT_SOURCES
    assert isinstance(AUDIT_SOURCES, frozenset)


@pytest.mark.parametrize(
    "name",
    ["", "Demo", "demo seed", "1demo", "demo-seed", "other_source", "demo_seed ", "DEMO_SEED",
     "demo_seed\x00", None, 1, b"demo_seed"],
)  # fmt: skip
def test_any_name_outside_the_list_is_refused(name: object) -> None:
    with pytest.raises(ValueError), audit_source(name):  # type: ignore[arg-type]
        pass
    assert audit_module._SOURCE.get() is None


def test_reset_after_an_exception_in_the_same_thread() -> None:
    with pytest.raises(RuntimeError), audit_source("demo_seed"):
        audit(AuditAction.THESIS_CREATED)
        raise RuntimeError
    assert audit_module._SOURCE.get() is None
    assert "source" not in audit(AuditAction.THESIS_CREATED).details_json


@pytest.mark.serial
def test_an_exception_in_one_thread_tags_nothing_in_another() -> None:
    """Thread A fails inside the context; thread B, then the main thread, look
    afterwards: neither is tagged."""
    failed = threading.Event()
    seen: list[object] = []

    def tagged_then_fails() -> None:
        try:
            with audit_source("demo_seed"):
                raise RuntimeError
        except RuntimeError:
            seen.append(("a after", audit_module._SOURCE.get()))
        finally:
            failed.set()

    def other() -> None:
        failed.wait(5)
        seen.append(("b", audit_module._SOURCE.get()))

    a, b = threading.Thread(target=tagged_then_fails), threading.Thread(target=other)
    b.start()
    a.start()
    a.join()
    b.join()
    assert sorted(seen, key=str) == [("a after", None), ("b", None)]
    assert "source" not in audit(AuditAction.THESIS_CREATED).details_json


@pytest.mark.serial
def test_other_threads_are_not_tagged_while_the_context_is_open() -> None:
    seen: list[object] = []

    def other() -> None:
        seen.append(audit_module._SOURCE.get())

    with audit_source("demo_seed"):
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen == [None]


def test_nesting_is_refused_and_keeps_the_outer_source() -> None:
    with audit_source("demo_seed"):
        with pytest.raises(ValueError), audit_source("demo_seed"):
            pass
        assert audit(AuditAction.THESIS_CREATED).details_json["source"] == "demo_seed"
    assert audit_module._SOURCE.get() is None


@pytest.mark.parametrize("value", ["demo_seed", "spoofed", ""])
def test_source_is_a_reserved_key_for_every_caller(value: str) -> None:
    """Outside the context too: no caller can write details.source itself."""
    with pytest.raises(AuditError, match="source"):
        audit(AuditAction.THESIS_CREATED, source=value)
    with audit_source("demo_seed"), pytest.raises(AuditError, match="source"):
        audit(AuditAction.THESIS_CREATED, source=value)
    assert not AuditEvent.objects.exists()


def test_http_request_data_never_sets_the_source() -> None:
    """A real audited request (failed sign-in) carrying "source" everywhere a
    client controls: query string, form field, headers, cookie."""
    browser = Client()
    browser.cookies["source"] = "demo_seed"
    browser.post(
        reverse("account_login") + "?source=demo_seed&details.source=demo_seed",
        {"login": "nobody@example.org", "password": WRONG, "source": "demo_seed"},
        HTTP_X_AUDIT_SOURCE="demo_seed",
        HTTP_SOURCE="demo_seed",
    )
    events = list(AuditEvent.objects.filter(action=AuditAction.LOGIN_FAILED))
    assert events, "the request was not audited: the test would prove nothing"
    for event in events:
        assert "source" not in event.details_json


def test_audit_source_is_called_by_demo_seed_only() -> None:
    callers = []
    for path in APPS.rglob("*.py"):
        if "tests" in path.parts or path.name == "audit.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "audit_source":
                    callers.append(str(path.relative_to(APPS)))
    assert callers == ["demo/scenario.py"]
