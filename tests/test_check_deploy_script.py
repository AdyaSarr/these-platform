"""scripts/check_deploy.sh: check --deploy with the prod settings, the variables of
.env and fictitious production values, all inside a subshell.

Never run on the real .env: each test copies the script into a temporary
repository with a fake .env (witness values only) and a fake `uv` first on PATH,
which records its directory, its arguments and its environment, then exits with a
given code. No Django, no manage.py, no network.
"""

from __future__ import annotations

import shutil
import stat
import subprocess  # nosec B404
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_deploy.sh"
BASH = "/bin/bash"
ZSH = shutil.which("zsh")

WITNESS = "TEMOIN-FUITE-CLE-MAITRESSE"
FAKE_ENV = (
    "DJANGO_SETTINGS_MODULE=config.settings.dev\n"
    "DJANGO_SITE_URL=http://localhost:8000\n"
    "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1\n"
    "EMAIL_URL=\n"
    "DEFAULT_FROM_EMAIL=noreply@localhost\n"
    f"MASTER_KEY={WITNESS}\n"
    "VAULT_STORAGE_DIR=./var/vault\n"
)
PRODUCTION = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "DJANGO_SITE_URL": "https://these.example.org",
    "DJANGO_ALLOWED_HOSTS": "these.example.org",
    "EMAIL_URL": "smtp+tls://demo:demo@smtp.example.org:587",  # pragma: allowlist secret
    "DEFAULT_FROM_EMAIL": "noreply@these.example.org",
}
# Every variable the script sets or loads: none may outlive it in the caller.
SET_BY_THE_SCRIPT = {*PRODUCTION, "MASTER_KEY", "VAULT_STORAGE_DIR"}


def _repo(tmp_path: Path, *, code: int = 0, with_env: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "scripts" / "check_deploy.sh")
    if with_env:
        (repo / ".env").write_text(FAKE_ENV)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    out = tmp_path / "uv"
    fake.write_text(
        "#!/bin/bash\n"
        f'pwd -P > "{out}.pwd"\n'
        f'printf "%s\\n" "$@" > "{out}.args"\n'
        f'env > "{out}.env"\n'
        f'if [ -d "$VAULT_STORAGE_DIR" ]; then echo yes; else echo no; fi > "{out}.dir"\n'
        f"exit {code}\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return repo


def _environment(tmp_path: Path) -> dict[str, str]:
    return {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path)}


def _run(tmp_path: Path, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603
        [BASH, str(repo / "scripts" / "check_deploy.sh")],
        cwd=tmp_path,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _recorded_env(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "uv.env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_the_script_is_valid_bash() -> None:
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_check_deploy_runs_with_prod_settings_from_the_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _run(tmp_path, repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "uv.pwd").read_text().strip() == str(repo.resolve())
    args = (tmp_path / "uv.args").read_text().splitlines()
    assert args == [
        "run", "python", "manage.py", "check", "--deploy",
        "--settings=config.settings.prod", "--fail-level", "WARNING",
    ]  # fmt: skip


def test_dot_env_is_loaded_and_the_production_values_win(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(tmp_path, repo)
    env = _recorded_env(tmp_path)
    assert env["MASTER_KEY"] == WITNESS  # from .env
    for name, value in PRODUCTION.items():
        assert env[name] == value, name


def test_the_vault_directory_is_temporary_and_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _run(tmp_path, repo)
    vault = Path(_recorded_env(tmp_path)["VAULT_STORAGE_DIR"])
    assert (tmp_path / "uv.dir").read_text().strip() == "yes"  # there during the check
    assert repo not in vault.parents
    assert not vault.exists()


def test_the_exit_code_of_the_check_is_kept_and_the_directory_removed(tmp_path: Path) -> None:
    repo = _repo(tmp_path, code=7)
    result = _run(tmp_path, repo)
    assert result.returncode == 7
    assert not Path(_recorded_env(tmp_path)["VAULT_STORAGE_DIR"]).exists()


def test_without_dot_env_nothing_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path, with_env=False)
    result = _run(tmp_path, repo)
    assert result.returncode != 0
    assert ".env" in result.stdout + result.stderr
    assert not (tmp_path / "uv.args").exists()


@pytest.mark.parametrize("shell", [BASH, ZSH], ids=["bash", "zsh"])
def test_sourcing_the_script_leaves_nothing_in_the_caller(
    tmp_path: Path, shell: str | None
) -> None:
    """Even sourced (`source scripts/check_deploy.sh`), the variables stay in the
    subshell: the caller keeps its environment, its options, and is not exited."""
    if shell is None:
        pytest.skip("zsh is not installed")
    repo = _repo(tmp_path)
    script = repo / "scripts" / "check_deploy.sh"
    result = subprocess.run(  # nosec B603
        [shell, "-c", f'source "{script}"; echo "status=$?"; set -o; echo ---; env'],
        cwd=tmp_path,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout
    assert "status=0" in output, output + result.stderr
    assert (tmp_path / "uv.args").exists()  # the check did run
    options, _, env = output.partition("---")
    names = {line.split("=", 1)[0] for line in env.splitlines() if "=" in line}
    assert names.isdisjoint(SET_BY_THE_SCRIPT), names & SET_BY_THE_SCRIPT
    assert WITNESS not in env
    assert not any(line.split()[:2] == ["allexport", "on"] for line in options.splitlines())
