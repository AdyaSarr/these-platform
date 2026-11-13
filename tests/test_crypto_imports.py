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


# Only the public AEAD API draws the nonce: _seal (which takes one) and _open stay
# private to crypto.py and its own tests.
PRIVATE_AEAD = {"_seal", "_open"}


def _private_aead_uses(path: Path) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Attribute) and node.attr in PRIVATE_AEAD:
            used.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in PRIVATE_AEAD:
            used.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            used |= {alias.name for alias in node.names} & PRIVATE_AEAD
    return used


def test_private_aead_functions_are_used_only_by_crypto_and_its_tests() -> None:
    allowed = {BASE_DIR / "apps" / "vault" / "crypto.py"}
    allowed_dir = BASE_DIR / "apps" / "vault" / "tests"
    offenders = {
        str(p.relative_to(BASE_DIR)): sorted(_private_aead_uses(p))
        for root in ("apps", "config", "tests")
        for p in (BASE_DIR / root).rglob("*.py")
        if p not in allowed and allowed_dir not in p.parents and _private_aead_uses(p)
    }
    assert offenders == {}
    # The scan sees the real uses (guards against a scan that finds nothing).
    assert _private_aead_uses(BASE_DIR / "apps" / "vault" / "crypto.py") == PRIVATE_AEAD
