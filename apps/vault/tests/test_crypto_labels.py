"""HKDF labels: one per usage, pairwise distinct, and no label outside crypto.py."""

import ast
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.vault import crypto
from apps.vault.formats import KeyDomain

APPS = Path(__file__).resolve().parents[2]


def _info_constants() -> dict[str, bytes]:
    return {
        name: value
        for name, value in vars(crypto).items()
        if name.endswith("_INFO") and isinstance(value, bytes)
    }


def test_known_labels() -> None:
    assert _info_constants() == {
        "PSEUDONYM_INFO": b"audit-pseudonym-v1",
        "CHECKPOINT_INFO": b"audit-checkpoint-ed25519-v1",
    }


def test_all_hkdf_labels_are_distinct() -> None:
    labels = list(_info_constants().values())
    labels += [crypto.kek_info(d.value, v) for d in KeyDomain for v in range(1, 101)]
    assert len(labels) == len(set(labels))
    assert all(labels)


def _derive_subkey_arguments() -> list[tuple[str, ast.expr]]:
    found = []
    for path in APPS.rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "derive_subkey":
                found.append((str(path.relative_to(APPS)), node.args[0]))
    return found


def _is_registered_label(arg: ast.expr) -> bool:
    """crypto.X_INFO / X_INFO, or kek_info(...): the label is one of the set above."""
    if isinstance(arg, ast.Name):
        return arg.id.endswith("_INFO") and arg.id in _info_constants()
    if isinstance(arg, ast.Attribute):
        return arg.attr in _info_constants()
    if isinstance(arg, ast.Call):
        func = arg.func
        return (isinstance(func, ast.Name) and func.id == "kek_info") or (
            isinstance(func, ast.Attribute) and func.attr == "kek_info"
        )
    return False


def test_every_derive_subkey_call_uses_a_registered_label() -> None:
    """Otherwise a label written inline elsewhere could collide with one above."""
    calls = _derive_subkey_arguments()
    assert calls  # the scan does find the calls
    offenders = [(path, ast.unparse(arg)) for path, arg in calls if not _is_registered_label(arg)]
    assert offenders == []


def test_unset_master_key_is_refused(settings: Any) -> None:
    del settings.MASTER_KEY
    with pytest.raises(ImproperlyConfigured):
        crypto.derive_subkey(b"audit-pseudonym-v1")


def test_hkdf_is_not_cached() -> None:
    """A cache would keep MASTER_KEY and every sub-key in a global object."""
    assert not hasattr(crypto._hkdf, "cache_info")
    assert not hasattr(crypto.derive_subkey, "cache_info")
