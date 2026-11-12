"""Acceptance: no secret in the repository, .env.example documents every variable,
no CDN, vendored htmx matches its recorded fingerprint."""

import base64
import hashlib
import re
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_CALL = re.compile(r"""env(?:\.\w+)?\(\s*["']([A-Z][A-Z0-9_]*)["']""")
ENV_EXAMPLE_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)


def _documented_variables() -> set[str]:
    return set(ENV_EXAMPLE_KEY.findall((BASE_DIR / ".env.example").read_text()))


def test_every_settings_variable_is_documented_in_env_example() -> None:
    used: set[str] = set()
    for path in (BASE_DIR / "config").rglob("*.py"):
        used |= set(ENV_CALL.findall(path.read_text()))
    assert used, "the regex found no env() call: settings layout changed?"
    missing = used - _documented_variables()
    assert not missing, f"undocumented in .env.example: {sorted(missing)}"


def test_compose_variables_are_documented_in_env_example() -> None:
    compose = (BASE_DIR / "docker-compose.yml").read_text()
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose))
    assert not used - _documented_variables()


def test_env_file_is_ignored_and_never_tracked() -> None:
    gitignore = (BASE_DIR / ".gitignore").read_text().splitlines()
    assert ".env" in gitignore
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=BASE_DIR, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    env_files = [f for f in tracked if re.search(r"(^|/)\.env(\.|$)", f)]
    assert env_files in ([], [".env.example"])


def test_settings_never_read_env_file_outside_dev_and_test() -> None:
    for name in ("base.py", "prod.py"):
        source = (BASE_DIR / "config" / "settings" / name).read_text()
        assert "read_env" not in source, f"{name} must not read a .env file"


def test_templates_load_no_external_resource() -> None:
    external = re.compile(r"""<(script|link)\b[^>]*(src|href)\s*=\s*["'](https?:)?//""", re.I)
    for path in (BASE_DIR / "templates").rglob("*.html"):
        assert not external.search(path.read_text()), f"external resource in {path}"


def test_vendored_htmx_matches_recorded_sha384() -> None:
    decisions = (BASE_DIR / "docs" / "decisions.md").read_text()
    htmx_line = next(
        line for line in decisions.splitlines() if "htmx" in line and "sha384-" in line
    )
    recorded = re.search(r"sha384-([A-Za-z0-9+/=]+)", htmx_line)
    assert recorded is not None
    content = (BASE_DIR / "static" / "vendor" / "htmx" / "htmx.min.js").read_bytes()
    actual = base64.b64encode(hashlib.sha384(content).digest()).decode()
    assert actual == recorded.group(1)
