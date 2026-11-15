"""Write French translations into a gettext catalogue after `manage.py makemessages`.

    uv run python scripts/po_fill.py locale/fr/LC_MESSAGES/django.po translations.json
    uv run python manage.py compilemessages -l fr

``translations.json``: a list of objects
    {"msgid": "...", "msgstr": "..."}
    {"msgid": "...", "msgid_plural": "...", "msgstr": ["one", "many"]}
    {"msgctxt": "mission status", "msgid": "Planned", "msgstr": "Prévue"}

Each entry named is rewritten: its comments and references, msgctxt, msgid (with
its continuation lines) are kept; its flags lose "fuzzy"; its old "#|" lines and
its whole old msgstr (WITH the continuation lines of a multi-line msgstr) go; the
new msgstr is written. An entry is matched on (msgctxt, msgid), never on msgid
alone: "Planned" of the missions is not "Planned" of the objectives.

Refuses to write (exit 1) when a translation names no entry of the catalogue. No
network, no third-party code: the catalogue is a text file of our own.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

Key = tuple[str | None, str]  # (msgctxt, msgid)

_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n", "\\t": "\t"}


def _unquote(lines: list[str]) -> str:
    raw = "".join(m.group(1) for line in lines for m in _STRING.finditer(line))
    return re.sub(r"\\[\\\"nt]", lambda m: _ESCAPES[m.group(0)], raw)


def _quote(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _field(lines: list[str], keyword: str) -> list[str] | None:
    """The lines of ``keyword`` (its first line and its continuation lines)."""
    for i, line in enumerate(lines):
        if line.startswith(keyword + " "):
            out = [line]
            for follow in lines[i + 1 :]:
                if not follow.startswith('"'):
                    break
                out.append(follow)
            return out
    return None


def _key(lines: list[str]) -> Key | None:
    msgid = _field(lines, "msgid")
    if msgid is None:
        return None
    context = _field(lines, "msgctxt")
    return (None if context is None else _unquote(context), _unquote(msgid))


def _rewrite(lines: list[str], value: str | list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        if line.startswith("msgstr"):
            break  # msgstr (and msgstr[n]) and their continuation lines are last
        if line.startswith("#|"):
            continue
        if line.startswith("#,"):
            flags = [f.strip() for f in line[2:].split(",") if f.strip() not in ("", "fuzzy")]
            if flags:
                kept.append("#, " + ", ".join(flags))
            continue
        kept.append(line)
    if isinstance(value, list):
        kept += [f"msgstr[{n}] {_quote(text)}" for n, text in enumerate(value)]
    else:
        kept.append(f"msgstr {_quote(value)}")
    return kept


def fill(catalogue: str, translations: dict[Key, str | list[str]]) -> tuple[str, list[Key]]:
    """The catalogue with ``translations`` written in, and the keys it has no entry for."""
    head, *blocks = catalogue.split("\n\n")
    done: set[Key] = set()
    out = [head]
    for block in blocks:
        lines = block.split("\n")
        key = _key(lines)
        if key is not None and key in translations:
            out.append("\n".join(_rewrite(lines, translations[key])))
            done.add(key)
        else:
            out.append(block)
    missing = sorted(set(translations) - done, key=lambda k: (k[0] or "", k[1]))
    return "\n\n".join(out), missing


def load(path: Path) -> dict[Key, str | list[str]]:
    items: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("a list of translations is expected")
    out: dict[Key, str | list[str]] = {}
    for item in items:
        msgstr = item["msgstr"]
        if not isinstance(item["msgid"], str) or not isinstance(msgstr, str | list):
            raise ValueError(f"invalid translation: {item!r}")
        out[(item.get("msgctxt"), item["msgid"])] = msgstr
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    po, source = Path(argv[1]), Path(argv[2])
    text, missing = fill(po.read_text(encoding="utf-8"), load(source))
    if missing:
        print("not in the catalogue (run makemessages first):", file=sys.stderr)
        for context, msgid in missing:
            print(f"  [{context}] {msgid!r}" if context else f"  {msgid!r}", file=sys.stderr)
        return 1
    po.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
