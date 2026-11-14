"""demo_seed writes nothing itself: every write goes through a service, so that
the audit chain, encryption and permission checks are those of real use."""

import ast
import re
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1]

# Every module of apps/demo is checked, except the tests and the database
# administration of the demo (demo_db: DROP / CREATE DATABASE, nothing else).
EXEMPT = {
    "__init__.py",
    "apps.py",
    "database.py",
    "management/__init__.py",
    "management/commands/__init__.py",
    "management/commands/demo_db.py",
}
CHECKED = sorted(
    p
    for p in DEMO.rglob("*.py")
    if "tests" not in p.relative_to(DEMO).parts and str(p.relative_to(DEMO)) not in EXEMPT
)

WRITE_METHODS = {
    "save", "delete", "create", "bulk_create", "bulk_update", "update", "get_or_create",
    "update_or_create", "raw", "execute", "executemany", "cursor", "set_password",
    "set_unusable_password", "save_base", "_do_insert", "_do_update",
}  # fmt: skip
FORBIDDEN_MODULES = {
    "apps.vault.crypto", "apps.vault.storage", "apps.vault.keys", "apps.vault.audit",
    "apps.vault.formats", "apps.vault.chain_v1", "django.db.connection", "psycopg",
    "cryptography", "nacl", "pickle", "yaml", "subprocess",
}  # fmt: skip
FORBIDDEN_NAMES = {"audit", "connection", "connections", "open", "eval", "exec", "__import__"}
FILE_READS = {"read_bytes", "read_text", "open"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def test_the_checked_set_is_not_empty_and_covers_the_seed() -> None:
    names = {str(p.relative_to(DEMO)) for p in CHECKED}
    assert {"scenario.py", "pdf.py", "guard.py", "management/commands/demo_seed.py"} <= names


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: str(p.relative_to(DEMO)))
def test_no_direct_write(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in WRITE_METHODS, f"line {node.lineno}: .{node.func.attr}()"
            assert node.func.attr not in FILE_READS, f"line {node.lineno}: reads a file"


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: str(p.relative_to(DEMO)))
def test_no_low_level_import_and_no_direct_audit(path: Path) -> None:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(
                    alias.name == m or alias.name.startswith(m + ".") for m in FORBIDDEN_MODULES
                ), alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(module == m or module.startswith(m + ".") for m in FORBIDDEN_MODULES), (
                module
            )
            for alias in node.names:
                assert alias.name not in FORBIDDEN_NAMES, f"{module}.{alias.name}"
                assert f"{module}.{alias.name}" not in FORBIDDEN_MODULES
        elif isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, f"line {node.lineno}: {node.id}"


def test_vault_services_used_only_through_their_public_api() -> None:
    for path in CHECKED:
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"vault", "vault_services"}
            ):
                assert not node.attr.startswith("_"), f"{path.name}: {node.attr}"


EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def test_every_address_in_the_source_is_at_example_org() -> None:
    for path in DEMO.rglob("*"):
        if path.suffix not in {".py", ".txt", ".html"} or "__pycache__" in path.parts:
            continue
        for address in EMAIL.findall(path.read_text()):
            assert address.endswith("@example.org"), f"{path.name}: {address}"
