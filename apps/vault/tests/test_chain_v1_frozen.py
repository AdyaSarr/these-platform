"""chain_v1.py and formats.canonical_json are FROZEN (CLAUDE.md): stored rows were
hashed with exactly this code. This test fails on ANY change to it; a new format
goes into chain_v2.py instead (never update the fingerprints below to make it pass)."""

import ast
from pathlib import Path

from apps.vault import crypto

VAULT = Path(__file__).resolve().parents[1]

FROZEN_MODULE = (
    "c1b072c7ac0e7a1634a776019d2f4ce945ce723a8f9fbd0d6ae953cad1e2992f"  # pragma: allowlist secret
)
FROZEN_FUNCTIONS = {  # in formats.py
    "_utf16_order": (
        "ae37d81466b9fccabf171dd975f88126"  # pragma: allowlist secret
        "8ff373bb9522c6db518c8d957d1ddfed"  # pragma: allowlist secret
    ),
    "_encode_json": (
        "0f23a2420a6fc705a5449df8da27022a"  # pragma: allowlist secret
        "1b3840c6bf920477e0488ff8d7666a1b"  # pragma: allowlist secret
    ),
    "canonical_json": (
        "d5b21c18f61c66f36bf07f75295d692a"  # pragma: allowlist secret
        "5de33449f1ea3d37f905bdd70550d821"  # pragma: allowlist secret
    ),
}


def test_chain_v1_is_unchanged() -> None:
    assert crypto.sha256((VAULT / "chain_v1.py").read_bytes()).hex() == FROZEN_MODULE


def test_canonical_json_is_unchanged() -> None:
    text = (VAULT / "formats.py").read_text()
    found = {
        node.name: crypto.sha256((ast.get_source_segment(text, node) or "").encode()).hex()
        for node in ast.parse(text).body
        if isinstance(node, ast.FunctionDef) and node.name in FROZEN_FUNCTIONS
    }
    assert found == FROZEN_FUNCTIONS
