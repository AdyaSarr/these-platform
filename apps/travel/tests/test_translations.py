"""Every message of the travel module (code and templates) has a French translation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PO = ROOT / "locale" / "fr" / "LC_MESSAGES" / "django.po"


def _entries() -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for block in PO.read_text().split("\n\n"):
        refs = re.findall(r"^#: (.+)$", block, re.M)
        msgid = re.search(r'^msgid ((?:".*"\n?)+)', block, re.M)
        msgstr = re.search(r'^msgstr ((?:".*"\n?)+)', block, re.M)
        if not msgid or not msgstr:
            continue
        joined = lambda m: "".join(re.findall(r'"(.*)"', m.group(1)))  # noqa: E731
        entries.append(
            {
                "refs": " ".join(refs),
                "msgid": joined(msgid),
                "msgstr": joined(msgstr),
                "fuzzy": "#, fuzzy" in block
                or "fuzzy" in "".join(re.findall(r"^#,.*$", block, re.M)),
            }
        )
    return entries


def test_every_travel_message_is_translated() -> None:
    mine = [
        e
        for e in _entries()
        if e["msgid"]
        and ("apps/travel/" in str(e["refs"]) or "templates/travel/" in str(e["refs"]))
    ]
    assert len(mine) > 30  # the module's messages are in the catalogue
    missing = [e["msgid"] for e in mine if not e["msgstr"] or e["fuzzy"]]
    assert missing == []


def test_the_compiled_catalogue_gives_the_translations() -> None:
    """The .mo is compiled from this .po: gettext in French returns each msgstr."""
    from django.utils import translation

    mine = [
        e
        for e in _entries()
        if e["msgid"]
        and "%" not in str(e["msgid"])
        and ("apps/travel/" in str(e["refs"]) or "templates/travel/" in str(e["refs"]))
    ]
    with translation.override("fr"):
        wrong = [e["msgid"] for e in mine if translation.gettext(str(e["msgid"])) != e["msgstr"]]
    assert wrong == []
