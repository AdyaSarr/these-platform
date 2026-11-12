"""CLAUDE.md rule 1: only apps/vault/crypto.py imports cryptographic libraries."""

import ast
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CRYPTO_MODULES = {"hashlib", "hmac", "secrets", "cryptography", "nacl", "argon2", "oqs", "random"}
ALLOWED = {BASE_DIR / "apps" / "vault" / "crypto.py"}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_vault_crypto_imports_crypto_libraries() -> None:
    offenders = {
        str(p.relative_to(BASE_DIR)): sorted(_imported_roots(p) & CRYPTO_MODULES)
        for p in (BASE_DIR / "apps").rglob("*.py")
        if "tests" not in p.parts and "migrations" not in p.parts and p not in ALLOWED
    }
    assert {k: v for k, v in offenders.items() if v} == {}
