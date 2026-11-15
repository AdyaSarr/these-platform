"""scripts/po_fill.py: translations written into the catalogue without corrupting it
(the 2026-11-15 defect: a multi-line msgstr's continuation lines stayed glued to the
msgid, so the source string lost its translation)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

BASE_DIR = Path(__file__).resolve().parent.parent


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("po_fill", BASE_DIR / "scripts" / "po_fill.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEAD = 'msgid ""\nmsgstr ""\n"Language: fr\\n"'

MULTILINE = """#: apps/travel/forms.py:80
#, fuzzy
#| msgid "Something else"
msgid ""
"Never an address: neither yours nor that of someone hosting you. Sealed at "
"the mission's level."
msgstr ""
"Une ancienne traduction approchée, "
"sur deux lignes."
"""

CONTEXTS = """#: apps/objectives/models.py:10
msgid "Planned"
msgstr "Prévu"

#: apps/travel/choices.py:40
#, fuzzy
msgctxt "mission status"
msgid "Planned"
msgstr "Prévu"
"""

PLURAL = """#: templates/travel/month.html:17
#, python-format
msgid "%(n)s working day at the LIRMM this month."
msgid_plural "%(n)s working days at the LIRMM this month."
msgstr[0] ""
msgstr[1] ""
"""


def _catalogue(*blocks: str) -> str:
    return "\n\n".join([HEAD, *(b.rstrip("\n") for b in blocks)]) + "\n"


def test_a_multiline_msgstr_is_replaced_whole() -> None:
    po = _module()
    msgid = (
        "Never an address: neither yours nor that of someone hosting you. "
        "Sealed at the mission's level."
    )
    text, missing = po.fill(_catalogue(MULTILINE), {(None, msgid): "Jamais d'adresse."})
    assert missing == []
    block = text.split("\n\n")[1]
    assert "ancienne" not in block and "deux lignes" not in block
    assert "fuzzy" not in block and "#|" not in block
    assert block.endswith('msgstr "Jamais d\'adresse."')
    assert po._key(block.split("\n")) == (None, msgid)  # the msgid is intact


def test_entries_are_matched_on_context_and_msgid() -> None:
    po = _module()
    text, missing = po.fill(_catalogue(CONTEXTS), {("mission status", "Planned"): "Prévue"})
    assert missing == []
    objectives, missions = text.split("\n\n")[1:3]
    assert objectives.endswith('msgstr "Prévu"')
    assert missions.endswith('msgstr "Prévue"') and "fuzzy" not in missions


def test_plural_forms_and_other_flags_are_kept() -> None:
    po = _module()
    key = (None, "%(n)s working day at the LIRMM this month.")
    text, _ = po.fill(_catalogue(PLURAL), {key: ["%(n)s jour", "%(n)s jours"]})
    block = text.split("\n\n")[1]
    assert "#, python-format" in block
    assert 'msgid_plural "%(n)s working days at the LIRMM this month."' in block
    assert block.endswith('msgstr[0] "%(n)s jour"\nmsgstr[1] "%(n)s jours"')


def test_an_unknown_msgid_writes_nothing(tmp_path: Path) -> None:
    po = _module()
    catalogue = tmp_path / "django.po"
    catalogue.write_text(_catalogue(CONTEXTS), encoding="utf-8")
    source = tmp_path / "fr.json"
    source.write_text(json.dumps([{"msgid": "Nowhere", "msgstr": "Nulle part"}]), encoding="utf-8")
    assert po.main(["po_fill.py", str(catalogue), str(source)]) == 1
    assert catalogue.read_text(encoding="utf-8") == _catalogue(CONTEXTS)


def test_quotes_and_backslashes_round_trip() -> None:
    po = _module()
    block = 'msgid "Say \\"yes\\""\nmsgstr ""'
    text, missing = po.fill(_catalogue(block), {(None, 'Say "yes"'): 'Dites « oui » \\ "x"'})
    assert missing == []
    assert po._unquote(text.split("\n\n")[1].split("\n")[1:]) == 'Dites « oui » \\ "x"'
