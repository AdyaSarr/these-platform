"""scripts/mutants.toml stays replayable (SR-12): every mutant still applies to
the code as it is, and names tests that exist. The mutants themselves are run by
hand (scripts/mutants.sh), never by the suite."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
MANIFEST = BASE_DIR / "scripts" / "mutants.toml"
FIELDS = {"id", "claim", "file", "old", "new", "tests"}


def _mutants() -> list[dict[str, Any]]:
    mutants: list[dict[str, Any]] = tomllib.loads(MANIFEST.read_text())["mutant"]
    return mutants


def test_the_manifest_lists_the_claimed_mutants() -> None:
    mutants = _mutants()
    assert len(mutants) >= 8
    ids = [m["id"] for m in mutants]
    assert len(set(ids)) == len(ids)
    for mutant in mutants:
        assert set(mutant) == FIELDS, mutant.get("id")
        assert re.fullmatch(r"M\d{2}-[a-z0-9-]+", mutant["id"]), mutant["id"]
        assert mutant["claim"].strip() and mutant["tests"], mutant["id"]


def test_every_mutant_applies_once_to_the_current_code() -> None:
    for mutant in _mutants():
        path = BASE_DIR / mutant["file"]
        assert path.is_file(), mutant["id"]
        assert mutant["old"] != mutant["new"], mutant["id"]
        count = path.read_text().count(mutant["old"])
        assert count == 1, f"{mutant['id']}: `old` found {count} times in {mutant['file']}"


def test_every_named_test_exists() -> None:
    for mutant in _mutants():
        for node in mutant["tests"]:
            path, _, function = node.partition("::")
            source = BASE_DIR / path
            assert source.is_file() and source.name.startswith("test_"), node
            if function:
                assert re.search(rf"^def {re.escape(function)}\(", source.read_text(), re.M), node


def test_the_mutants_are_not_part_of_the_suite() -> None:
    assert "mutants" not in (BASE_DIR / "scripts" / "test_all.sh").read_text()
