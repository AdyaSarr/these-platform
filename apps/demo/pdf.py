"""A one-page PDF built in memory: "Document fictif de démonstration" and a title.

Never read from disk (CLAUDE.md, rule 10). Five objects (catalog, pages, page,
font, content stream), Helvetica in WinAnsiEncoding (cp1252), offsets of the
cross-reference table computed from the bytes. No dependency.
"""

from __future__ import annotations

import re

PHRASE = "Document fictif de démonstration"
TITLE_MAX = 120
# Printable cp1252 text without backslash (parentheses are escaped below).
_TITLE = re.compile("[ -\\[\\]-~\u00c0-\u00ff\u2019]+")


def _text(value: str) -> bytes:
    if not 1 <= len(value) <= TITLE_MAX or not _TITLE.fullmatch(value):
        raise ValueError("title: 1 to 120 printable Latin-1 characters, no backslash")
    data = value.encode("cp1252")
    return data.replace(b"(", rb"\(").replace(b")", rb"\)")


def fictional_pdf(title: str) -> bytes:
    """A valid PDF 1.4 whose page reads PHRASE, then ``title``."""
    content = (
        b"BT /F1 20 Tf 72 760 Td (" + _text(PHRASE) + b") Tj ET\n"
        b"BT /F1 14 Tf 72 720 Td (" + _text(title) + b") Tj ET\n"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
