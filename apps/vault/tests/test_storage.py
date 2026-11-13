"""vault.storage: atomic, no-clobber, private blob files; strict reads."""

import errno
import fcntl
import logging
import os
import re
import stat
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured

from apps.vault import storage
from apps.vault.errors import BlobNotFoundError, StorageError


def _sealed(payload: bytes) -> bytes:
    """Shaped like a sealed v1 blob (header, nonce, ..., tag); not real ciphertext."""
    return b"\x01" + b"\x00" * 12 + payload + b"\x00" * 16


DATA = _sealed(b"fictitious sealed bytes")
NAME = "ab" + "0" * 30


@pytest.fixture
def root(settings: Any) -> Path:
    """Initialized by the conftest fixture (as by `manage.py vault_init`)."""
    path = Path(settings.VAULT_STORAGE_DIR)
    assert path.is_dir()
    return path


@pytest.fixture
def wide_umask() -> Generator[None]:
    previous = os.umask(0)
    yield
    os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# --------------------------------------------------------------------------- write


def test_write_returns_a_path_in_the_layout_and_stores_the_bytes(root: Path) -> None:
    rel = storage.write(DATA)
    assert re.fullmatch(storage.BLOB_PATH_REGEX, rel)
    assert (root / rel).read_bytes() == DATA
    assert storage.read(rel) == DATA


def test_names_are_random(root: Path) -> None:
    assert len({storage.write(DATA) for _ in range(50)}) == 50


def test_files_and_directories_are_private_whatever_the_umask(root: Path, wide_umask: None) -> None:
    rel = storage.write(DATA)
    assert _mode(root / rel) == 0o600
    assert _mode((root / rel).parent) == 0o700
    assert _mode(root) == 0o700


def test_no_temporary_file_is_left(root: Path) -> None:
    rel = storage.write(DATA)
    assert _files(root) == [rel.split("/")[0], rel]


def test_existing_blob_is_never_overwritten(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation target: publishing with os.replace would silently overwrite."""
    monkeypatch.setattr(storage, "_new_name", lambda: NAME)
    first = storage.write(_sealed(b"existing content"))
    with pytest.raises(StorageError, match="collision"):
        storage.write(_sealed(b"new content"))
    assert (root / first).read_bytes() == _sealed(b"existing content")
    assert _files(root) == ["ab", first]


@pytest.mark.parametrize("code", [errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EMLINK])
def test_without_hard_links_writing_is_refused_never_replaced(
    root: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def no_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(code, os.strerror(code))

    replaced: list[Any] = []
    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "replace", lambda *a, **k: replaced.append(a))
    monkeypatch.setattr(os, "rename", lambda *a, **k: replaced.append(a))
    with pytest.raises(StorageError, match="hard links"):
        storage.write(DATA)
    assert replaced == []
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_failed_write_leaves_no_temporary_file(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_write(fd: int, data: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", broken_write)
    with pytest.raises(StorageError):
        storage.write(DATA)
    assert [p for p in root.rglob("*") if p.is_file()] == []


def _record_fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    kinds: list[str] = []
    real = storage._fsync

    def spy(fd: int) -> None:
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real(fd)

    monkeypatch.setattr(storage, "_fsync", spy)
    return kinds


def test_fsync_file_then_directory(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage, "_new_name", iter(["ab" + "1" * 30, "ab" + "2" * 30]).__next__)
    kinds = _record_fsyncs(monkeypatch)
    storage.write(DATA)  # new shard: the root, which gained an entry, is synced too
    assert kinds == ["dir", "file", "dir"]
    kinds.clear()
    storage.write(DATA)  # existing shard
    assert kinds == ["file", "dir"]


def test_full_fsync_is_used_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd: calls.append(cmd))
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("fsync"))
    storage._fsync(0)
    assert calls == [51]


def test_fsync_falls_back_when_full_fsync_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def refuse(fd: int, cmd: int) -> None:
        raise OSError(errno.ENOTSUP, "not supported")

    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", refuse)
    monkeypatch.setattr(os, "fsync", lambda fd: calls.append("fsync"))
    storage._fsync(0)
    assert calls == ["fsync"]


# --------------------------------------------------------------------------- read


@pytest.mark.parametrize(
    "rel",
    [
        "../" + NAME + ".blob",
        "/etc/passwd",
        f"ab/{NAME}.txt",
        f"cd/{NAME}.blob",  # shard does not match the name
        f"ab/../ab/{NAME}.blob",
        f"AB/{NAME.upper()}.blob",
        f"ab/{NAME}.blob/",
        "",
        None,
    ],
)
def test_malformed_paths_are_refused_before_any_file_access(
    root: Path, monkeypatch: pytest.MonkeyPatch, rel: Any
) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a))
    with pytest.raises(StorageError):
        storage.read(rel)
    assert opened == []


def test_missing_blob(root: Path) -> None:
    storage.write(DATA)
    with pytest.raises(BlobNotFoundError):
        storage.read(f"ab/{NAME}.blob")


def test_symlinked_blob_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not a blob")
    (root / "ab").mkdir(parents=True)
    (root / "ab" / f"{NAME}.blob").symlink_to(outside)
    with pytest.raises(StorageError) as excinfo:
        storage.read(f"ab/{NAME}.blob")
    assert not isinstance(excinfo.value, BlobNotFoundError)


def test_symlinked_shard_is_refused(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / f"{NAME}.blob").write_bytes(DATA)
    (root / "ab").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(StorageError):
        storage.read(f"ab/{NAME}.blob")


def test_directory_in_place_of_a_blob_is_refused(root: Path) -> None:
    (root / "ab" / f"{NAME}.blob").mkdir(parents=True)
    with pytest.raises(StorageError):
        storage.read(f"ab/{NAME}.blob")


def test_oversized_blob_is_refused_without_being_read(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "ab").mkdir(parents=True)
    with open(root / "ab" / f"{NAME}.blob", "wb") as f:
        f.truncate(storage.MAX_BLOB_BYTES + 1)  # sparse: no real disk use
    reads: list[int] = []

    def spy_read(fd: int, n: int) -> bytes:
        reads.append(n)
        return b""

    monkeypatch.setattr(os, "read", spy_read)
    with pytest.raises(StorageError, match="too large"):
        storage.read(f"ab/{NAME}.blob")
    assert reads == []


@pytest.mark.parametrize("delta", [1, -1])
def test_blob_changing_while_read_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch, delta: int
) -> None:
    rel = storage.write(DATA)
    real_read = os.read
    served = False

    def changing_read(fd: int, n: int) -> bytes:
        nonlocal served
        if served:
            return b""
        served = True
        chunk = real_read(fd, n)
        return chunk + b"x" if delta > 0 else chunk[:-1]

    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(StorageError, match="changed"):
        storage.read(rel)


def test_blob_size_bounds() -> None:
    from apps.vault.validation import MAX_UPLOAD_BYTES

    # Written out here: header 1, nonce 12, tag 16 (independent of the constants).
    assert storage.MIN_BLOB_BYTES == 1 + 12 + 16
    assert storage.MAX_BLOB_BYTES == 1 + 12 + MAX_UPLOAD_BYTES + 16


# --------------------------------------------------------------------------- write guard

PDF = b"%PDF-1.7\n%fictitious plaintext document\n%%EOF\n"


@pytest.mark.parametrize(
    "data",
    [
        PDF,
        b"",
        b"\x01" + b"\x00" * 27,  # one byte short of header + nonce + tag
        b"\x02" + b"\x00" * 40,  # unknown format byte
        "\x01" + "\x00" * 40,  # str, not bytes
    ],
    ids=["clear-pdf", "empty", "too-short", "bad-format", "str"],
)
def test_write_refuses_what_is_not_a_sealed_blob(root: Path, data: Any) -> None:
    with pytest.raises(StorageError, match="not a sealed"):
        storage.write(data)
    assert list(root.iterdir()) == []


def test_smallest_sealed_blob_is_accepted(root: Path) -> None:
    rel = storage.write(b"\x01" + b"\x00" * 28)
    assert storage.read(rel) == b"\x01" + b"\x00" * 28


# --------------------------------------------------------------------------- vault_init


def test_missing_root_is_never_created_by_read_or_write(settings: Any, tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    settings.VAULT_STORAGE_DIR = str(absent)
    with pytest.raises(StorageError, match="vault_init"):
        storage.write(DATA)
    with pytest.raises(StorageError, match="vault_init"):
        storage.read(f"ab/{NAME}.blob")
    assert not absent.exists()


def test_init_root_creates_a_private_directory_and_syncs_its_parent(
    settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wide_umask: None
) -> None:
    target = tmp_path / "parent" / "vault"
    settings.VAULT_STORAGE_DIR = str(target)
    synced: list[int] = []
    real = storage._fsync

    def spy(fd: int) -> None:
        synced.append(os.fstat(fd).st_ino)
        real(fd)

    monkeypatch.setattr(storage, "_fsync", spy)
    assert storage.init_root() is True
    assert _mode(target) == 0o700
    assert synced == [target.parent.stat().st_ino]
    synced.clear()
    assert storage.init_root() is False  # idempotent
    assert synced == []


@pytest.mark.parametrize("strict", [True, False])
def test_init_root_refuses_an_unsafe_existing_root(settings: Any, root: Path, strict: bool) -> None:
    settings.VAULT_STORAGE_STRICT = strict  # refused in every environment
    root.chmod(0o755)
    with pytest.raises(StorageError, match="unsafe permissions"):
        storage.init_root()


def test_init_root_refuses_a_symlink_or_a_file(settings: Any, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    (tmp_path / "file").write_bytes(b"")
    for name in ("link", "file"):
        settings.VAULT_STORAGE_DIR = str(tmp_path / name)
        with pytest.raises(StorageError, match="not a real directory"):
            storage.init_root()


@pytest.mark.django_db
def test_vault_init_command(settings: Any, tmp_path: Path) -> None:
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.VAULT_STORAGE_DIR = str(tmp_path / "new")
    out = StringIO()
    call_command("vault_init", stdout=out)
    assert "created" in out.getvalue()
    (tmp_path / "new").chmod(0o750)
    with pytest.raises(CommandError, match="unsafe permissions"):
        call_command("vault_init", stdout=StringIO())


# --------------------------------------------------------------------------- root


def test_symlinked_root_is_refused(settings: Any, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    settings.VAULT_STORAGE_DIR = str(tmp_path / "link")
    with pytest.raises((StorageError, ImproperlyConfigured)):
        storage.write(DATA)
    with pytest.raises(ImproperlyConfigured):
        storage.check_settings()


def test_wide_root_permissions_are_refused_in_strict_mode(settings: Any, root: Path) -> None:
    settings.VAULT_STORAGE_STRICT = True
    root.chmod(0o750)
    with pytest.raises(ImproperlyConfigured, match="permissions"):
        storage.write(DATA)


def test_wide_root_permissions_only_warn_otherwise(
    settings: Any, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings.VAULT_STORAGE_STRICT = False
    root.chmod(0o755)
    with caplog.at_level(logging.WARNING, logger="apps.vault.storage"):
        rel = storage.write(DATA)
    assert storage.read(rel) == DATA
    assert any("permissions" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "where",
    ["STATIC_ROOT", "STATICFILES_DIRS", "MEDIA_ROOT", "CONTAINS_STATIC_ROOT"],
)
def test_root_inside_a_served_directory_stops_start_up(
    settings: Any, tmp_path: Path, where: str
) -> None:
    served = tmp_path / "served"
    settings.STATIC_ROOT = str(tmp_path / "static-root")
    settings.STATICFILES_DIRS = [str(tmp_path / "static-src")]
    settings.MEDIA_ROOT = ""
    if where == "STATIC_ROOT":
        settings.STATIC_ROOT = str(served)
    elif where == "STATICFILES_DIRS":
        settings.STATICFILES_DIRS = [str(served)]
    elif where == "MEDIA_ROOT":
        settings.MEDIA_ROOT = str(served)
    settings.VAULT_STORAGE_DIR = str(served / "vault")
    if where == "CONTAINS_STATIC_ROOT":
        settings.VAULT_STORAGE_DIR = str(tmp_path)
    with pytest.raises(ImproperlyConfigured):
        storage.check_settings()
    with pytest.raises(ImproperlyConfigured):
        apps.get_app_config("vault").ready()


def test_relative_root_is_refused_in_strict_mode(settings: Any) -> None:
    settings.VAULT_STORAGE_STRICT = True
    settings.VAULT_STORAGE_DIR = "var/vault"
    with pytest.raises(ImproperlyConfigured, match="absolute"):
        storage.check_settings()


def test_relative_root_is_resolved_from_base_dir_otherwise(settings: Any) -> None:
    settings.VAULT_STORAGE_STRICT = False
    settings.VAULT_STORAGE_DIR = "var/vault"
    storage.check_settings()
    assert storage.root() == Path(settings.BASE_DIR) / "var" / "vault"


def test_default_settings_pass_the_checks() -> None:
    storage.check_settings()
