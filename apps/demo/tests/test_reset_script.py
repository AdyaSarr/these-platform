"""scripts/demo_reset.sh: refuses before deleting anything unless every check passes.

The script is run with a fake `uv` first on PATH, which records its arguments
and answers `demo_db describe` as told: no Django, no database is touched.
"""

import os
import re
import stat
import subprocess  # nosec B404 - runs our own script with fixed arguments
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "demo_reset.sh"
BASH = "/bin/bash"


def _fake_uv(tmp_path: Path, describe: str) -> tuple[dict[str, str], Path]:
    calls = tmp_path / "calls.txt"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        'if [[ "$*" == *"demo_db describe"* ]]; then\n'
        f"  printf '%s\\n' {describe}\n"
        "fi\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return env, calls


def _run(env: dict[str, str], *args: str, answer: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed command line
        [BASH, str(SCRIPT), *args],
        input=answer,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _destructive(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [c for c in calls.read_text().splitlines() if "demo_db drop" in c or "migrate" in c]


VAULT = f"vault={REPO}/var/vault-demo"


def test_strict_mode_and_hard_coded_paths() -> None:
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert 'VAULT_REL="var/vault-demo"' in text
    assert ".env" not in text.replace(".env.example", "")  # never reads it
    for line in text.splitlines():
        if re.search(r"\brm\b", line) and not line.lstrip().startswith("#"):
            assert re.search(r'rm -rf -- "\$\{[A-Z_]+:\?[^}]*\}"', line), line
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, check=False)  # nosec
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["theses", "theses_demo_x", "", "x;rm"])
def test_refuses_a_database_not_ending_in_demo(tmp_path: Path, name: str) -> None:
    env, calls = _fake_uv(tmp_path, f"'database={name}' '{VAULT}'")
    result = _run(env, "--yes")
    assert result.returncode != 0
    assert "_demo" in result.stderr
    assert _destructive(calls) == []


def test_refuses_when_django_disagrees_on_the_vault_directory(tmp_path: Path) -> None:
    env, calls = _fake_uv(tmp_path, "'database=theses_demo' 'vault=/tmp/elsewhere'")
    result = _run(env, "--yes")
    assert result.returncode != 0
    assert "vault" in result.stderr.lower()
    assert _destructive(calls) == []


@pytest.mark.parametrize("answer", ["", "non\n", "oui \n", "OUI\n", "yes\n"])
def test_needs_a_typed_oui(tmp_path: Path, answer: str) -> None:
    env, calls = _fake_uv(tmp_path, f"'database=theses_demo' '{VAULT}'")
    result = _run(env, answer=answer)
    assert result.returncode != 0
    assert "theses_demo" in result.stdout  # what would be deleted is shown first
    assert "var/vault-demo" in result.stdout
    assert _destructive(calls) == []


def test_unknown_option_is_refused(tmp_path: Path) -> None:
    env, calls = _fake_uv(tmp_path, f"'database=theses_demo' '{VAULT}'")
    result = _run(env, "--force")
    assert result.returncode != 0
    assert not calls.exists()


def test_forces_demo_settings_whatever_the_environment(tmp_path: Path) -> None:
    env, calls = _fake_uv(tmp_path, "'database=theses' 'vault=x'")
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.prod"
    _run(env, "--yes")
    assert "demo_db describe" in calls.read_text()
    text = SCRIPT.read_text()
    assert "export DJANGO_SETTINGS_MODULE=config.settings.demo" in text
    assert os.environ.get("DJANGO_SETTINGS_MODULE") != "config.settings.demo"
