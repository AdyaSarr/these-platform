"""Static checks of the visual layer (frontend, docs/decisions.md).

- No inline style or script anywhere in the templates (the strict CSP would block it).
- The CSP itself is unchanged by the redesign.
- static/ loads nothing from outside (no CDN, no web font service, no data: URI).
- The CSS stays small, hand-written, in cascade layers, with every token defined.
- The mobile menu (popover) has a CSS-only fallback: without popover support the
  sidebar is shown expanded, never hidden.
- Every vendored file matches the recorded fingerprints.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC = BASE_DIR / "static"
CSS_DIR = STATIC / "css"
VENDOR = STATIC / "vendor"
# Link order = layer order. legacy (the styles of the modules before the redesign)
# comes before components: a later layer wins whatever the specificity.
CSS_FILES = ["base.css", "layout.css", "legacy.css", "components.css"]
LAYERS = "@layer reset, tokens, base, layout, legacy, components, utilities;"

# --------------------------------------------------------------------------- templates


def _templates() -> list[Path]:
    paths = list((BASE_DIR / "templates").rglob("*.html"))
    paths += list((BASE_DIR / "apps").glob("*/templates/**/*.html"))
    assert len(paths) > 50
    return sorted(paths)


def _without_comments(source: str) -> str:
    source = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", source, flags=re.S)
    return re.sub(r"\{#.*?#\}", "", source, flags=re.S)


INLINE = {
    "style attribute": re.compile(r"\sstyle\s*=", re.I),
    "style element": re.compile(r"<style\b", re.I),
    "inline script": re.compile(r"<script\b(?![^>]*\bsrc\s*=)", re.I),
    "event handler": re.compile(r"\son[a-z]+\s*=", re.I),
    "htmx inline handler": re.compile(r"\bhx-on", re.I),
}


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(BASE_DIR)))
def test_no_inline_style_or_script_in_templates(path: Path) -> None:
    source = _without_comments(path.read_text(encoding="utf-8"))
    found = {kind for kind, regex in INLINE.items() if regex.search(source)}
    assert not found, (path, found)


def test_the_inline_scan_sees_what_it_must() -> None:
    samples = {
        "style attribute": '<p style="color:red">',
        "style element": "<style>p{}</style>",
        "inline script": "<script>alert(1)</script>",
        "event handler": '<a onclick="x()">',
        "htmx inline handler": '<a hx-on:click="x()">',
    }
    for kind, sample in samples.items():
        assert INLINE[kind].search(sample), kind
    assert not INLINE["inline script"].search('<script src="/static/x.js" defer></script>')
    assert not _without_comments("{# <style> #}").strip()


# --------------------------------------------------------------------------- CSP

EXPECTED_CSP = {
    "default-src": ["'none'"],
    "script-src": ["'self'"],
    "style-src": ["'self'"],
    "img-src": ["'self'"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
    "base-uri": ["'none'"],
}


def test_csp_setting_is_unchanged() -> None:
    assert settings.CONTENT_SECURITY_POLICY == {"DIRECTIVES": EXPECTED_CSP}


@pytest.mark.django_db
def test_csp_header_is_unchanged(client: Client) -> None:
    header = client.get(reverse("account_login"))["Content-Security-Policy"]
    directives = {
        name: values
        for name, *values in (part.split() for part in header.split(";") if part.strip())
    }
    assert directives == EXPECTED_CSP


# --------------------------------------------------------------------------- static/

TEXT_SUFFIXES = {".css", ".js", ".svg", ".html", ".json", ".txt"}
LICENCE_NAMES = {"LICENSE", "OFL.txt", "README.md", "SHA384SUMS"}
# XML namespace names: identifiers, never fetched.
ALLOWED_URLS = {"http://www.w3.org/2000/svg"}
URL = re.compile(r"https?://[^\s\"'()<>]+", re.I)


def _static_text_files() -> list[Path]:
    return sorted(
        p
        for p in STATIC.rglob("*")
        if p.is_file() and p.suffix in TEXT_SUFFIXES and p.name not in LICENCE_NAMES
    )


def test_static_text_files_name_no_external_url() -> None:
    found = {
        (str(p.relative_to(BASE_DIR)), url)
        for p in _static_text_files()
        for url in URL.findall(p.read_text(encoding="utf-8", errors="replace"))
        if url not in ALLOWED_URLS
    }
    assert not found


def test_css_loads_only_relative_files() -> None:
    for path in STATIC.rglob("*.css"):
        css = path.read_text(encoding="utf-8")
        assert "@import" not in css, path  # one <link> per file, in base.html
        for target in re.findall(r"url\(\s*['\"]?([^'\")\s]+)", css):
            assert not re.match(r"([a-z]+:|//)", target, re.I), (path, target)


# --------------------------------------------------------------------------- CSS

CSS_BUDGET = 40 * 1024
FONT_BUDGET = 160 * 1024


def _css(name: str) -> str:
    return (CSS_DIR / name).read_text(encoding="utf-8")


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_css_files_are_the_expected_ones() -> None:
    assert sorted(p.name for p in CSS_DIR.glob("*.css")) == sorted(CSS_FILES)


def test_css_budget() -> None:
    total = sum((CSS_DIR / name).stat().st_size for name in CSS_FILES)
    assert total <= CSS_BUDGET, total


def test_font_budget() -> None:
    fonts = list(STATIC.rglob("*.woff2"))
    assert fonts
    assert sum(p.stat().st_size for p in fonts) <= FONT_BUDGET
    assert not [p for p in STATIC.rglob("*") if p.suffix in {".ttf", ".otf", ".woff", ".eot"}]


def test_base_template_links_the_css_files_in_layer_order() -> None:
    base = (BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    linked = re.findall(r"""<link rel="stylesheet" href="\{% static 'css/([\w.-]+)' %\}">""", base)
    assert linked == CSS_FILES


def _top_level_preludes(css: str) -> list[str]:
    """What precedes each top-level block (or statement) of a stylesheet."""
    preludes, depth, current = [], 0, ""
    for char in _strip_css_comments(css):
        if depth == 0 and char in "{;":
            preludes.append(current.strip())
            current = ""
            if char == "{":
                depth = 1
            continue
        if depth:
            depth += {"{": 1, "}": -1}.get(char, 0)
        else:
            current += char
    return preludes


def test_layer_order_is_declared_first() -> None:
    assert _top_level_preludes(_css("base.css"))[0] + ";" == LAYERS


@pytest.mark.parametrize("name", CSS_FILES)
def test_every_rule_is_in_a_layer(name: str) -> None:
    preludes = _top_level_preludes(_css(name))
    if name == "base.css":
        preludes = preludes[1:]
    layers = LAYERS.removeprefix("@layer ").rstrip(";").split(", ")
    assert preludes
    for prelude in preludes:
        assert re.fullmatch(r"@layer [\w-]+", prelude), (name, prelude)
        assert prelude.removeprefix("@layer ") in layers, (name, prelude)


def test_every_custom_property_used_is_defined() -> None:
    css = "".join(_strip_css_comments(_css(name)) for name in CSS_FILES)
    defined = set(re.findall(r"(--[\w-]+)\s*:", css))
    used = set(re.findall(r"var\(\s*(--[\w-]+)", css))
    assert used - defined == set()


def test_reduced_motion_is_respected() -> None:
    base = _strip_css_comments(_css("base.css"))
    assert "@media (prefers-reduced-motion: reduce)" in base


# --------------------------------------------------------------------------- popover fallback


def _remove_blocks(css: str, prelude: str) -> tuple[str, list[str]]:
    """``css`` without the blocks opened by ``prelude``, and those blocks' bodies."""
    removed, out, i = [], "", 0
    while (start := css.find(prelude, i)) != -1:
        out += css[i:start]
        j = css.index("{", start) + 1
        depth = 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        removed.append(css[css.index("{", start) + 1 : j - 1])
        i = j
    return out + css[i:], removed


def _rules(css: str) -> list[tuple[str, str]]:
    """Innermost (selector, declarations) pairs; at-rule preludes are dropped."""
    return [
        (sel.strip().split("{")[-1].strip(), body)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


POPOVER_SUPPORTS = "@supports selector(:popover-open)"


def test_layout_has_the_popover_fallback() -> None:
    css = _strip_css_comments(_css("layout.css"))
    assert POPOVER_SUPPORTS in css
    outside, inside = _remove_blocks(css, POPOVER_SUPPORTS)
    assert inside
    # Outside the @supports block: the menu button is hidden (it would do nothing
    # without popover support) and nothing hides the sidebar.
    toggles = [body for sel, body in _rules(outside) if re.fullmatch(r"\.nav-toggle", sel)]
    assert any(re.search(r"display\s*:\s*none", body) for body in toggles), toggles
    for sel, body in _rules(outside):
        if "sidebar" in sel:
            assert not re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", body), sel
        assert ":popover-open" not in sel, sel
    # Inside: the button is shown and the closed sidebar is hidden (mobile only).
    body = "".join(inside)
    assert re.search(r"\.nav-toggle[^{]*\{[^}]*display\s*:\s*inline-flex", body)
    assert re.search(r"\.sidebar:not\(:popover-open\)\s*\{[^}]*display\s*:\s*none", body)


NARROW = "@media (max-width: 63.99rem)"
WIDE = "@media (min-width: 64rem)"


def _media_rules(css: str, prelude: str) -> list[tuple[str, str]]:
    return _rules("".join(_remove_blocks(css, prelude)[1]))


def _hides(rules: list[tuple[str, str]], selector: str) -> bool:
    return any(sel == selector and re.search(r"display\s*:\s*none", body) for sel, body in rules)


def test_header_height_is_fixed_and_known_to_scroll_padding() -> None:
    """One line of a fixed height, the --header-height that scroll-padding-top
    adds: a sticky header never hides the title the page scrolls to (WCAG 2.4.11)."""
    css = _strip_css_comments(_css("layout.css"))
    top = _remove_blocks(css, "@media")[0]
    header = [body for sel, body in _rules(top) if sel == ".site-header"]
    assert len(header) == 1
    assert re.search(r"flex-wrap\s*:\s*nowrap", header[0])
    assert re.search(r"(?<![-\w])height\s*:\s*var\(--header-height\)", header[0])
    assert not re.search(r"min-height|padding(-block)?\s*:\s*var\(--space-[4-7]\)", header[0])
    base = _strip_css_comments(_css("base.css"))
    assert re.search(r"scroll-padding-top\s*:\s*calc\(var\(--header-height\)", base)
    # No other rule of the header changes its height or lets it wrap.
    for sel, body in _rules(css):
        if sel.endswith(".site-header") and body != header[0]:
            assert not re.search(r"(?<![-\w])(min-)?height\s*:|flex-wrap\s*:\s*wrap", body), sel


def test_narrow_screen_moves_the_tools_into_the_menu() -> None:
    css = _strip_css_comments(_css("layout.css"))
    outside, _ = _remove_blocks(css, POPOVER_SUPPORTS)
    narrow = _media_rules(outside, NARROW)
    wide = _media_rules(outside, WIDE)
    # Outside @supports: also true without popover support, where the sidebar
    # is shown expanded and holds the tools.
    assert _hides(narrow, ".app-with-nav .header-tools")
    assert _hides(narrow, ".app-with-nav .header-thesis")
    assert _hides(wide, ".nav-tools")
    assert _hides(wide, ".header-brand")
    # Nothing hides the menu's copy on a narrow screen, popover or not.
    for sel, body in _rules(css):
        if ".nav-tools" in sel and re.search(r"display\s*:\s*none", body):
            assert (sel, body) in wide, sel


def test_cards_keep_their_natural_height() -> None:
    """A short card is never stretched to the height of the tallest of its row."""
    css = _strip_css_comments(_css("components.css"))
    blocks = [body for sel, body in _rules(css) if sel == ".blocks"]
    assert blocks and re.search(r"align-items\s*:\s*start", blocks[0]), blocks


def test_the_popover_fallback_scan_sees_what_it_must() -> None:
    css = ".nav-toggle { display: none; } @supports x { .a { b: c; } } .d { e: f; }"
    outside, inside = _remove_blocks(css, "@supports x")
    assert inside == [" .a { b: c; } "]
    assert (".d", " e: f; ") in _rules(outside)


# --------------------------------------------------------------------------- vendored files

MANIFEST = VENDOR / "SHA384SUMS"


def _sha384(path: Path) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()


def test_vendored_files_match_the_manifest() -> None:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    listed = {}
    for line in lines:
        digest, name = line.split("  ", 1)
        listed[name] = digest
    present = {
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*")
        if p.is_file() and p.name not in {"README.md", "SHA384SUMS", ".DS_Store"}
    }
    assert set(listed) == present
    for name, digest in listed.items():
        assert _sha384(VENDOR / name) == digest, name


def test_manifest_fingerprint_is_recorded_in_decisions() -> None:
    decisions = (BASE_DIR / "docs" / "decisions.md").read_text(encoding="utf-8")
    # The latest line wins: decisions.md only grows, a new manifest gets a new line.
    line = [line for line in decisions.splitlines() if "SHA384SUMS" in line and "sha384-" in line][
        -1
    ]
    recorded = re.findall(r"sha384-[A-Za-z0-9+/=]+", line)
    assert _sha384(MANIFEST) in recorded


def test_vendored_licences_are_present() -> None:
    assert (VENDOR / "inter" / "OFL.txt").is_file()
    assert (VENDOR / "lucide" / "LICENSE").is_file()
