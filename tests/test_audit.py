"""Auditor's checks on the M0 base: one test per attack path.

1. DEBUG=True in production       -> test_debug_*, test_non_prod_settings_*
2. reaching /admin/ in production -> test_admin_*
3. loading an external script     -> test_csp_*, test_templates_*
4. a secret reaching Git          -> test_sensitive_paths_*, test_detect_secrets_*
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.prod_helpers import BASE_DIR, PROD_HOST, prod_env, run_python

_REQUESTS = """
import json, django
django.setup()
from django.conf import settings
from django.test import Client
client = Client(raise_request_exception=False)
out = {{"DEBUG": settings.DEBUG}}
for path in {paths!r}:
    r = client.get(path, secure=True, headers={{"host": {host!r}}})
    out[path] = {{"status": r.status_code, "csp": r.headers.get("Content-Security-Policy", "")}}
print(json.dumps(out))
"""


def _requests(paths: list[str], host: str = PROD_HOST, **env: str | None) -> dict[str, Any]:
    result = run_python(_REQUESTS.format(paths=paths, host=host), prod_env(**env))
    assert result.returncode == 0, result.stderr
    data: dict[str, Any] = json.loads(result.stdout.strip().splitlines()[-1])
    return data


# --------------------------------------------------------------------------- 1. DEBUG


def test_debug_cannot_be_enabled_by_environment_in_prod() -> None:
    out = _requests([], DEBUG="True", DJANGO_DEBUG="True")
    assert out["DEBUG"] is False


def test_debug_is_never_read_from_environment() -> None:
    env_call = re.compile(r"""env(?:\.\w+)?\(\s*["']([A-Z0-9_]+)["']""")
    for path in (BASE_DIR / "config").rglob("*.py"):
        assert not [v for v in env_call.findall(path.read_text()) if "DEBUG" in v], path


def test_debug_true_only_in_dev_settings() -> None:
    files = {
        p.name
        for p in (BASE_DIR / "config" / "settings").glob("*.py")
        if re.search(r"^DEBUG\s*=\s*True", p.read_text(), re.MULTILINE)
    }
    assert files == {"dev.py"}


@pytest.mark.parametrize("module", ["config.settings.dev", "config.settings.test"])
def test_non_prod_settings_refuse_a_production_environment(module: str) -> None:
    """Wrong DJANGO_SETTINGS_MODULE on a prod host: refuse to start rather than
    serve DEBUG pages (which would expose CELERY_BROKER_URL) or the admin."""
    result = run_python("import django; django.setup()", prod_env(DJANGO_SETTINGS_MODULE=module))
    assert result.returncode != 0
    assert "DJANGO_ALLOWED_HOSTS" in result.stderr


def test_dev_settings_still_start_with_local_hosts() -> None:
    env = prod_env(
        DJANGO_SETTINGS_MODULE="config.settings.dev",
        DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1,[::1]",
    )
    result = run_python(
        "import django; django.setup(); from django.conf import settings; print(settings.DEBUG)",
        env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


# --------------------------------------------------------------------------- 2. /admin/


@pytest.mark.parametrize(
    "env",
    [{}, {"ADMIN_ENABLED": "True", "DJANGO_ADMIN_ENABLED": "True"}],
    ids=["default", "env-tries-to-enable-admin"],
)
def test_admin_returns_404_over_http_in_prod(env: dict[str, str]) -> None:
    out = _requests(["/admin/", "/admin/login/"], **env)
    assert out["/admin/"]["status"] == 404
    assert out["/admin/login/"]["status"] == 404


# --------------------------------------------------------------------------- 3. external scripts


def test_csp_header_sent_in_prod_even_on_errors() -> None:
    # /health/ answers 503 here (no database), /nope/ is a 404.
    out = _requests(["/health/", "/nope/"])
    for path in ("/health/", "/nope/"):
        csp = out[path]["csp"]
        assert "script-src 'self'" in csp, path
        assert "default-src 'none'" in csp, path
        assert "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp


def test_csp_never_relaxed_in_code() -> None:
    relax = re.compile(r"csp_exempt|csp_update|csp_replace|unsafe-inline|unsafe-eval")
    for root in ("apps", "config"):
        for path in (BASE_DIR / root).rglob("*.py"):
            if "tests" in path.parts:
                continue
            assert not relax.search(path.read_text()), path


def _templates() -> list[Path]:
    return list((BASE_DIR / "templates").rglob("*.html")) + list(
        (BASE_DIR / "apps").rglob("templates/**/*.html")
    )


def test_templates_have_no_inline_script_or_event_handler() -> None:
    inline_script = re.compile(r"<script\b(?![^>]*\bsrc=)", re.I)
    handler = re.compile(r"""\s(on[a-z]+|hx-on[:\w-]*)\s*=""", re.I)
    for path in _templates():
        text = path.read_text()
        assert not inline_script.search(text), f"inline <script> in {path}"
        assert not handler.search(text), f"inline event handler in {path}"


# --------------------------------------------------------------------------- 4. secrets in Git


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".env.prod", "secrets/kek.txt", "tls.pem", "signing.key", "var/vault/x"],
)
def test_sensitive_paths_are_git_ignored(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path], cwd=BASE_DIR, check=False
    )
    assert result.returncode == 0, f"{path} is not ignored by .gitignore"


def test_env_example_is_not_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=BASE_DIR, check=False
    )
    assert result.returncode == 1


def test_detect_secrets_finds_nothing_new_in_tracked_files() -> None:
    """Same scan as the pre-commit hook, but in pytest: catches `git commit --no-verify`."""
    baseline = json.loads((BASE_DIR / ".secrets.baseline").read_text())
    # Exclusions read from the baseline (single source of truth with the hook),
    # plus the baseline file itself.
    excluded = [
        pattern
        for f in baseline["filters_used"]
        if f["path"] == "detect_secrets.filters.regex.should_exclude_file"
        for pattern in f["pattern"]
    ]
    assert excluded, "the baseline must list its excluded files"
    scan = subprocess.run(
        [
            "detect-secrets",
            "scan",
            *(arg for pattern in excluded for arg in ("--exclude-files", pattern)),
            "--exclude-files",
            r"^\.secrets\.baseline$",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    found = {
        (f, s["hashed_secret"])
        for f, items in json.loads(scan.stdout)["results"].items()
        for s in items
    }
    known = {(f, s["hashed_secret"]) for f, items in baseline["results"].items() for s in items}
    assert found <= known, f"new potential secrets: {sorted(found - known)}"
