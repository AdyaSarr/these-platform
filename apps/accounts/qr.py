"""The TOTP QR code as inline SVG, checked against a strict allow-list.

allauth gives the QR code as <img src="data:image/svg+xml;base64,...">, which our
CSP (img-src 'self') blocks: the browser then showed the alt text, which was the
TOTP secret. Inline SVG needs no CSP change, provided it holds nothing the CSP
would block or that could run: this module accepts only <svg> and <path> with a
fixed set of presentation attributes (fill, stroke... are attributes, not inline
styles: style-src does not apply to them), and refuses anything else.

The SVG comes from allauth's adapter (qrcode, server side) and encodes the
otpauth:// URL in its modules only: no user-provided text is in it.
"""

from __future__ import annotations

import re

ALLOWED = {
    "svg": frozenset({"width", "height", "version", "viewBox", "xmlns"}),
    "path": frozenset({"d", "fill", "fill-opacity", "fill-rule", "stroke", "id"}),
}
_XML_DECLARATION = re.compile(r"\A\s*<\?xml[^?>]*\?>\s*")
_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]*)((?:\s+[^<>]*?)?)\s*(/?)>")
_ATTRIBUTE = re.compile(r'\s+([A-Za-z][A-Za-z0-9:-]*)="([^"<>]*)"')
# Attribute values: path data, numbers, colours, units, a namespace URL for xmlns.
_VALUE = re.compile(r"[A-Za-z0-9 .,#%+\-]*")
XMLNS = "http://www.w3.org/2000/svg"


def inline_svg(svg: str) -> str:
    """``svg`` without its XML declaration, if it passes the allow-list; ValueError otherwise."""
    if not isinstance(svg, str):
        raise ValueError("not a string")
    body = _XML_DECLARATION.sub("", svg, count=1).strip()
    if not body.startswith("<svg") or not body.endswith("</svg>"):
        raise ValueError("not a single <svg> element")
    position, depth = 0, 0
    for match in _TAG.finditer(body):
        if body[position : match.start()].strip():  # text between tags
            raise ValueError("text content is not allowed")
        closing, name, attributes, self_closing = match.groups()
        if name not in ALLOWED:
            raise ValueError(f"element <{name}> is not allowed")
        if closing:
            if attributes.strip():
                raise ValueError("attributes on a closing tag")
            depth -= 1
        else:
            _check_attributes(name, attributes)
            if not self_closing:
                depth += 1
        position = match.end()
    if body[position:].strip() or depth != 0:
        raise ValueError("malformed SVG")
    if not re.match(rf'<svg\b[^>]*\sxmlns="{re.escape(XMLNS)}"', body):
        raise ValueError("missing SVG namespace")
    return body


def _check_attributes(element: str, attributes: str) -> None:
    rest = _ATTRIBUTE.sub("", attributes)
    if rest.strip():  # unquoted, single-quoted or otherwise unparsed attributes
        raise ValueError("unexpected attribute syntax")
    for name, value in _ATTRIBUTE.findall(attributes):
        if name not in ALLOWED[element]:
            raise ValueError(f"attribute {name!r} is not allowed on <{element}>")
        if name == "xmlns":
            if value != XMLNS:
                raise ValueError("unexpected namespace")
        elif not _VALUE.fullmatch(value):
            raise ValueError(f"unexpected value in {name!r}")
