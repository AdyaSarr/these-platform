"""Uploads stay in memory, capped at 25 MB: nothing is ever written in clear to disk."""

import tempfile
from pathlib import Path
from typing import Any

import pytest
from django.conf import global_settings
from django.conf import settings as django_settings
from django.core.files.uploadedfile import InMemoryUploadedFile, SimpleUploadedFile
from django.test import RequestFactory

from apps.vault.validation import MAX_UPLOAD_BYTES

MB = 1024 * 1024


@pytest.fixture
def temp_dirs(settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Both places Django could spool an upload to, empty and observed."""
    upload_dir, system_tmp = tmp_path / "upload", tmp_path / "tmp"
    upload_dir.mkdir()
    system_tmp.mkdir()
    settings.FILE_UPLOAD_TEMP_DIR = str(upload_dir)
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    assert tempfile.gettempdir() == str(system_tmp)
    return [upload_dir, system_tmp]


def _post(**files: bytes) -> Any:
    data = {name: SimpleUploadedFile(f"{name}.txt", content) for name, content in files.items()}
    return RequestFactory().post("/upload/", data)


def _spooled(dirs: list[Path]) -> list[Path]:
    return [p for d in dirs for p in d.iterdir()]


def test_only_the_limited_memory_handler_is_configured() -> None:
    assert django_settings.FILE_UPLOAD_HANDLERS == [
        "apps.vault.upload_handlers.LimitedMemoryUploadHandler"
    ]


def test_10_mb_upload_stays_in_memory(temp_dirs: list[Path]) -> None:
    request = _post(file=b"a" * (10 * MB))
    upload = request.FILES["file"]
    assert isinstance(upload, InMemoryUploadedFile)
    assert upload.size == 10 * MB
    assert _spooled(temp_dirs) == []  # observed while the upload is still open
    assert not getattr(request, "upload_too_large", False)


def test_django_default_handlers_would_spool_it_to_disk(
    settings: Any, temp_dirs: list[Path]
) -> None:
    """Shows the observation above bites: Django's defaults write the upload to disk."""
    settings.FILE_UPLOAD_HANDLERS = global_settings.FILE_UPLOAD_HANDLERS
    request = _post(file=b"a" * (10 * MB))
    upload = request.FILES["file"]
    assert _spooled(temp_dirs) != []
    upload.close()


def test_upload_of_exactly_25_mb_is_accepted(temp_dirs: list[Path]) -> None:
    request = _post(file=b"a" * MAX_UPLOAD_BYTES)
    assert request.FILES["file"].size == MAX_UPLOAD_BYTES
    assert _spooled(temp_dirs) == []


def test_26_mb_upload_is_interrupted(temp_dirs: list[Path]) -> None:
    request = _post(file=b"a" * (26 * MB))
    assert "file" not in request.FILES
    assert request.upload_too_large is True
    assert _spooled(temp_dirs) == []


def test_the_limit_is_on_the_total_of_the_request(temp_dirs: list[Path]) -> None:
    """Many files under 25 MB each must not add up to more in memory."""
    request = _post(first=b"a" * (13 * MB), second=b"b" * (13 * MB))
    assert "second" not in request.FILES
    assert request.upload_too_large is True


def test_bytes_kept_never_exceed_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    from apps.vault import upload_handlers

    kept: list[int] = []
    real = upload_handlers.LimitedMemoryUploadHandler.receive_data_chunk

    def spy(self: Any, raw_data: bytes, start: int) -> Any:
        result = real(self, raw_data, start)
        kept.append(self.file.tell())
        return result

    monkeypatch.setattr(upload_handlers.LimitedMemoryUploadHandler, "receive_data_chunk", spy)
    _post(file=b"a" * (30 * MB)).FILES  # noqa: B018 - triggers parsing
    assert kept
    assert max(kept) <= MAX_UPLOAD_BYTES
