"""manage.py vault_gc: lists orphans, stale temporary files, anomalies and missing
blobs; never deletes, never reads a blob."""

import builtins
import os
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.vault import storage
from apps.vault.tests.factories import DocumentVersionFactory

pytestmark = pytest.mark.django_db

DAY = 24 * 3600


@pytest.fixture
def root(settings: Any) -> Path:
    """Initialized by the conftest fixture (as by `manage.py vault_init`)."""
    return Path(settings.VAULT_STORAGE_DIR)


def _sealed(payload: bytes) -> bytes:
    return b"\x01" + b"\x00" * 12 + payload + b"\x00" * 16


def _age(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then), follow_symlinks=False)


def _gc(*args: str) -> tuple[str, int]:
    out = StringIO()
    try:
        call_command("vault_gc", *args, stdout=out)
    except CommandError as exc:
        return out.getvalue(), exc.returncode
    return out.getvalue(), 0


def _snapshot(root: Path) -> dict[str, tuple[int, float]]:
    return {
        str(p.relative_to(root)): (p.lstat().st_size, p.lstat().st_mtime) for p in root.rglob("*")
    }


def test_clean_storage(root: Path) -> None:
    for _ in range(3):
        DocumentVersionFactory.create(ciphertext_path=storage.write(_sealed(b"sealed")))
    out, code = _gc()
    assert code == 0
    assert all(paths == [] for paths in _sections(out).values())
    assert "3 referenced" in out


def test_report(root: Path, tmp_path: Path) -> None:
    referenced = storage.write(_sealed(b"referenced"))
    DocumentVersionFactory.create(ciphertext_path=referenced)
    old_orphan = storage.write(_sealed(b"old orphan"))
    _age(root / old_orphan, 2 * DAY)
    recent_orphan = storage.write(_sealed(b"recent orphan"))
    missing = DocumentVersionFactory.create().ciphertext_path  # row without blob

    shard = root / referenced.split("/")[0]
    stale_tmp = shard / (".tmp-" + "a" * 32)
    stale_tmp.write_bytes(b"partial")
    stale_tmp.chmod(0o600)
    _age(stale_tmp, 2 * DAY)
    fresh_tmp = shard / (".tmp-" + "b" * 32)
    fresh_tmp.write_bytes(b"partial")
    fresh_tmp.chmod(0o600)
    stray = shard / "notes.txt"
    stray.write_bytes(b"?")
    stray.chmod(0o600)
    link = shard / ("c" * 32 + ".blob")
    link.symlink_to(tmp_path)
    wide = root / storage.write(_sealed(b"wide"))
    wide.chmod(0o644)
    DocumentVersionFactory.create(ciphertext_path=str(wide.relative_to(root)))
    (root / "not-a-shard").mkdir()

    before = _snapshot(root)
    out, code = _gc()
    assert _snapshot(root) == before  # nothing deleted, nothing touched

    assert code != 0
    sections = _sections(out)
    assert sections["MISSING"] == [missing]
    assert sections["ORPHAN"] == [old_orphan]
    assert sections["RECENT"] == [recent_orphan]
    assert sections["STALE TEMPORARY"] == [str(stale_tmp.relative_to(root))]
    assert set(sections["ANOMALY"]) == {
        str(stray.relative_to(root)),
        str(link.relative_to(root)),
        str(wide.relative_to(root)),
        "not-a-shard",
    }
    assert str(fresh_tmp.relative_to(root)) not in out


def _sections(out: str) -> dict[str, list[str]]:
    """Lines look like "<SECTION>\t<path>[\t<details>]"."""
    sections: dict[str, list[str]] = {
        k: [] for k in ("MISSING", "ORPHAN", "RECENT", "STALE TEMPORARY", "ANOMALY")
    }
    for line in out.splitlines():
        kind, _, rest = line.partition("\t")
        if kind in sections:
            sections[kind].append(rest.split("\t")[0])
    return {k: sorted(v) for k, v in sections.items()}


def test_orphans_alone_do_not_fail(root: Path) -> None:
    orphan = storage.write(_sealed(b"orphan"))
    _age(root / orphan, 2 * DAY)
    out, code = _gc()
    assert code == 0
    assert _sections(out)["ORPHAN"] == [orphan]


def test_grace_period_is_configurable(root: Path) -> None:
    orphan = storage.write(_sealed(b"orphan"))
    _age(root / orphan, 2 * 3600)
    assert _sections(_gc()[0])["RECENT"] == [orphan]
    assert _sections(_gc("--grace-hours", "1")[0])["ORPHAN"] == [orphan]


def test_missing_root_reports_every_row_as_missing(root: Path) -> None:
    root.rmdir()
    row = DocumentVersionFactory.create()
    out, code = _gc()
    assert code != 0
    assert _sections(out)["MISSING"] == [row.ciphertext_path]


def test_blobs_are_never_opened(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage.write(_sealed(b"sealed"))
    real_open, real_os_open = builtins.open, os.open

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        assert ".blob" not in str(file), "vault_gc opened a blob"
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        assert ".blob" not in str(path), "vault_gc opened a blob"
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    _gc()


def test_temporary_name_left_linked_after_a_failed_unlink_is_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.link succeeded, removing the temporary name failed: the .tmp- name is a
    second hard link to the blob; vault_gc reports both names, whatever their age."""
    from apps.vault.errors import StorageError

    real_unlink = os.unlink

    def failing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).startswith(storage.TMP_PREFIX):
            raise PermissionError(1, "Operation not permitted")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    with pytest.raises(StorageError):
        storage.write(_sealed(b"blob"))
    monkeypatch.setattr(os, "unlink", real_unlink)

    tmp = next(root.rglob(storage.TMP_PREFIX + "*"))
    blob = next(root.rglob("*.blob"))
    assert tmp.stat().st_ino == blob.stat().st_ino  # same file, two names
    out, code = _gc()
    assert code != 0
    assert set(_sections(out)["ANOMALY"]) == {
        str(tmp.relative_to(root)),
        str(blob.relative_to(root)),
    }
    assert "2 hard links" in out
