"""Static checks on who may call the vault's low-level storage.

storage.write / storage.read handle sealed blobs: only vault.services (which
seals, checks rights and audits) and tests may call them.
"""

import ast
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_IO = {"write", "read"}
ALLOWED = {BASE_DIR / "apps" / "vault" / "services.py", BASE_DIR / "apps" / "vault" / "storage.py"}


def storage_io_uses(source: str) -> set[str]:
    """``storage.write`` / ``storage.read`` attribute uses, and imports of them."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in STORAGE_IO
            and isinstance(node.value, ast.Name | ast.Attribute)
            and (node.value.id if isinstance(node.value, ast.Name) else node.value.attr)
            == "storage"
        ):
            found.add(node.attr)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.split(".")[-1] == "storage"
        ):
            found |= {alias.name for alias in node.names} & STORAGE_IO
    return found


def test_detector_sees_every_form() -> None:
    """Guards against a scan that silently finds nothing."""
    assert storage_io_uses("from apps.vault import storage\nstorage.write(b'')") == {"write"}
    assert storage_io_uses("import apps.vault.storage\napps.vault.storage.read('x')") == {"read"}
    assert storage_io_uses("from apps.vault.storage import read, write") == {"read", "write"}
    assert storage_io_uses("from .storage import write as w") == {"write"}
    assert storage_io_uses("f.write(b'')\nstorage.root()") == set()


def test_storage_io_only_from_vault_services() -> None:
    offenders = {
        str(path.relative_to(BASE_DIR)): sorted(uses)
        for root in ("apps", "config")
        for path in (BASE_DIR / root).rglob("*.py")
        if "tests" not in path.parts
        and path not in ALLOWED
        and (uses := storage_io_uses(path.read_text()))
    }
    assert offenders == {}
