"""Encrypted blob files: atomic, never overwritten, private, read without following links.

Layout under VAULT_STORAGE_DIR: ``<2 hex>/<32 hex>.blob``, the shard being the
first two digits of the name; the name comes from the OS CSPRNG, nothing in a
path comes from a user. The bytes written here are already sealed (AEAD) by
vault.services: this module never sees a plaintext.

Every access goes through directory file descriptors (``dir_fd``) opened with
O_NOFOLLOW at each level, so no symbolic link is ever followed and there is no
window between checking a path and opening it.

Write: temporary file (O_EXCL, 0600) in the shard, fsync, then published with
``os.link`` (atomic, fails if the name exists: never overwrites; there is no
fallback to os.replace), temporary name removed, shard directory fsynced.
On macOS, fsync does not flush the drive cache: F_FULLFSYNC is used there.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from . import crypto, formats
from .errors import BlobNotFoundError, StorageError
from .validation import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

# Also checked by the database (DocumentVersion.ciphertext_path).
BLOB_PATH_REGEX = r"^([0-9a-f]{2})/\1[0-9a-f]{30}\.blob$"
_BLOB_PATH = re.compile(BLOB_PATH_REGEX)
TMP_PREFIX = ".tmp-"
_HEADER_BYTES = len(formats.pack_blob(b""))  # the format byte
# Smallest and largest sealed blob: header + nonce + (plaintext) + tag.
MIN_BLOB_BYTES = _HEADER_BYTES + crypto.NONCE_BYTES + crypto.TAG_BYTES
MAX_BLOB_BYTES = MIN_BLOB_BYTES + MAX_UPLOAD_BYTES

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_MODE, _DIR_MODE = 0o600, 0o700

for _func in (os.open, os.link, os.unlink, os.mkdir):
    if _func not in os.supports_dir_fd:  # pragma: no cover - platform check
        raise ImportError(f"vault.storage needs dir_fd support for os.{_func.__name__}")


# --------------------------------------------------------------------------- settings


def root() -> Path:
    """VAULT_STORAGE_DIR as an absolute path (a relative one is taken from BASE_DIR)."""
    path = Path(settings.VAULT_STORAGE_DIR)
    return path if path.is_absolute() else Path(str(settings.BASE_DIR)) / path


def check_settings() -> None:
    """Start-up checks (VaultConfig.ready). Path-based: does not create anything."""
    raw = Path(settings.VAULT_STORAGE_DIR)
    if settings.VAULT_STORAGE_STRICT and not raw.is_absolute():
        raise ImproperlyConfigured("VAULT_STORAGE_DIR must be an absolute path in production.")
    path = root()
    if path.is_symlink():
        raise ImproperlyConfigured("VAULT_STORAGE_DIR must not be a symbolic link.")
    resolved = path.resolve()
    # STATICFILES_DIRS entries are paths or (prefix, path) pairs.
    static_dirs = [d[1] if isinstance(d, tuple | list) else d for d in settings.STATICFILES_DIRS]
    served = [settings.STATIC_ROOT, *static_dirs, settings.MEDIA_ROOT]
    for directory in filter(None, served):
        other = Path(str(directory)).resolve()
        if resolved.is_relative_to(other) or other.is_relative_to(resolved):
            raise ImproperlyConfigured(
                "VAULT_STORAGE_DIR must not be inside, nor contain, a directory served as "
                "static or media files."
            )


# --------------------------------------------------------------------------- low level


def _new_name() -> str:
    return crypto.random_hex(16)


def _fsync(fd: int) -> None:
    """fsync, or F_FULLFSYNC where it exists (macOS) and is accepted."""
    full = getattr(fcntl, "F_FULLFSYNC", None)
    if full is not None:
        try:
            fcntl.fcntl(fd, full)
            return
        except OSError:
            pass  # e.g. a filesystem that does not support it
    os.fsync(fd)


def _root_problems(fd: int) -> list[str]:
    info = os.fstat(fd)
    problems = []
    if info.st_uid != os.geteuid():
        problems.append("not owned by the server's user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        problems.append(f"mode {stat.S_IMODE(info.st_mode):o} instead of 700")
    return problems


def _check_root_permissions(fd: int) -> None:
    problems = _root_problems(fd)
    if not problems:
        return
    message = f"Unsafe permissions on VAULT_STORAGE_DIR: {', '.join(problems)}."
    if settings.VAULT_STORAGE_STRICT:
        raise ImproperlyConfigured(message)
    logger.warning(message)


def init_root() -> bool:
    """Create the storage root in 0700 (manage.py vault_init); True if it was created.

    An existing root is accepted only if it is a real directory owned by the
    server's user with mode 700: anything else is refused, in every
    environment. The parent directory is fsynced so the new entry survives a crash.
    """
    path = root()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(path, _DIR_MODE)
        created = True
    except FileExistsError:
        created = False
    try:
        fd = os.open(path, _DIR_FLAGS)
    except OSError as exc:  # ELOOP / ENOTDIR: a symlink or not a directory
        raise StorageError("VAULT_STORAGE_DIR exists but is not a real directory") from exc
    try:
        if created:
            os.fchmod(fd, _DIR_MODE)  # exactly 700, whatever the umask
        problems = _root_problems(fd)
        if problems:
            raise StorageError(f"VAULT_STORAGE_DIR exists with unsafe permissions: {problems}")
    finally:
        os.close(fd)
    if created:
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            _fsync(parent_fd)
        finally:
            os.close(parent_fd)
    return created


@contextmanager
def _root_fd() -> Iterator[int]:
    try:
        fd = os.open(root(), _DIR_FLAGS)  # never created here: see init_root()
    except FileNotFoundError:
        raise StorageError("vault storage directory missing: run `manage.py vault_init`") from None
    except OSError as exc:  # includes ELOOP / ENOTDIR for a symlinked root
        raise StorageError("the vault storage directory cannot be opened safely") from exc
    try:
        _check_root_permissions(fd)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _shard_fd(root_fd: int, shard: str) -> Iterator[int]:
    fd = os.open(shard, _DIR_FLAGS, dir_fd=root_fd)  # refuses a symlinked shard
    try:
        yield fd
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


# --------------------------------------------------------------------------- write


def write(data: bytes) -> str:
    """Store ``data`` (already sealed) in a new blob; return its relative path.

    Refuses anything that does not look like a sealed v1 blob (header byte,
    room for nonce and tag): a cheap guard against storing a plaintext by
    mistake, not a proof that the data is encrypted.
    """
    if not isinstance(data, bytes) or len(data) < MIN_BLOB_BYTES or data[0] != formats.FORMAT_V1:
        raise StorageError("refusing to store data that is not a sealed v1 blob")
    name = _new_name()
    shard, filename = name[:2], f"{name}.blob"
    try:
        with _root_fd() as root_fd:
            try:
                os.mkdir(shard, _DIR_MODE, dir_fd=root_fd)
                _fsync(root_fd)  # the root gained an entry
            except FileExistsError:
                pass
            with _shard_fd(root_fd, shard) as shard_fd:
                _publish(shard_fd, filename, data)
    except OSError as exc:
        raise StorageError("the blob could not be written") from exc
    return f"{shard}/{filename}"


def _publish(shard_fd: int, filename: str, data: bytes) -> None:
    tmp = f"{TMP_PREFIX}{crypto.random_hex(16)}"
    fd = os.open(tmp, _NEW_FILE_FLAGS, _FILE_MODE, dir_fd=shard_fd)
    try:
        try:
            _write_all(fd, data)
            _fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, filename, src_dir_fd=shard_fd, dst_dir_fd=shard_fd, follow_symlinks=False)
        except FileExistsError:
            raise StorageError(
                "blob name collision: an existing blob is never overwritten"
            ) from None
        except OSError as exc:
            # No fallback to os.replace: it would overwrite an existing blob.
            raise StorageError("the vault storage must support hard links") from exc
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=shard_fd)
    _fsync(shard_fd)


# --------------------------------------------------------------------------- read


def read(rel: str) -> bytes:
    """Bytes of the blob at ``rel``; the path is checked before any file access."""
    if not isinstance(rel, str) or not _BLOB_PATH.fullmatch(rel):
        raise StorageError("invalid blob path")
    shard, filename = rel.split("/")
    try:
        with _root_fd() as root_fd, _shard_fd(root_fd, shard) as shard_fd:
            fd = os.open(filename, _READ_FLAGS, dir_fd=shard_fd)
            try:
                return _read_regular_file(fd)
            finally:
                os.close(fd)
    except FileNotFoundError:
        raise BlobNotFoundError("blob not found") from None
    except OSError as exc:  # ELOOP (symbolic link), ENOTDIR, EACCES...
        raise StorageError("the blob cannot be opened safely") from exc


def _read_regular_file(fd: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise StorageError("not a regular file")
    if info.st_size > MAX_BLOB_BYTES:
        raise StorageError("blob too large")
    chunks, remaining = [], info.st_size + 1  # +1: notice a file that grew
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != info.st_size:  # a published blob never changes
        raise StorageError("blob changed while being read")
    return data
