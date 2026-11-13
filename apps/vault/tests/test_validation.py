"""Uploaded files: size limit, type detected from the content, extension consistency."""

from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.vault import validation as v

PDF = b"%PDF-1.7\n%fictitious\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 17
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x14\x00" + b"\x00" * 24
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18
CSV = b"nom;valeur\nessai fictif;42\n"
TXT = "Compte rendu fictif.\nAccentué : é à ç.\n".encode()
TEX = b"\\documentclass{article}\n\\begin{document}Fictif\\end{document}\n"


def _upload(name: str, data: bytes, content_type: str = "application/octet-stream") -> Any:
    return SimpleUploadedFile(name, data, content_type=content_type)


def _code(excinfo: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(excinfo.value.code)


@pytest.mark.parametrize(
    ("name", "data", "mime", "extension"),
    [
        ("rapport.pdf", PDF, "application/pdf", "pdf"),
        ("figure.png", PNG, "image/png", "png"),
        ("photo.jpg", JPEG, "image/jpeg", "jpg"),
        ("photo.jpeg", JPEG, "image/jpeg", "jpg"),
        ("archive.zip", ZIP, "application/zip", "zip"),
        ("vide.zip", EMPTY_ZIP, "application/zip", "zip"),
        ("mesures.csv", CSV, "text/csv", "csv"),
        ("notes.txt", TXT, "text/plain", "txt"),
        ("article.tex", TEX, "application/x-tex", "tex"),
        ("RAPPORT.PDF", PDF, "application/pdf", "pdf"),
        ("bom.txt", b"\xef\xbb\xbfavec BOM\n", "text/plain", "txt"),
    ],
)
def test_allowed_types(name: str, data: bytes, mime: str, extension: str) -> None:
    checked = v.validate_upload(_upload(name, data))
    assert (checked.mime_type, checked.extension) == (mime, extension)
    assert checked.data == data
    assert checked.size == len(data)


def test_client_content_type_is_ignored() -> None:
    checked = v.validate_upload(_upload("rapport.pdf", PDF, content_type="text/html"))
    assert checked.mime_type == "application/pdf"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("figure.png", PDF),  # content and extension disagree
        ("rapport.pdf", PNG),
        ("rapport.pdf", TXT),
        ("notes.txt", PDF),
        ("archive.zip", JPEG),
        ("notes.txt", b"MZ\x90\x00\x03\x00\x00\x00"),  # Windows executable
        ("page.txt", b"\x00\x00\x00\x00"),
        ("script.txt", b"#!/bin/sh\x00\n"),
    ],
)
def test_content_must_match_the_extension(name: str, data: bytes) -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload(name, data))
    assert _code(excinfo) in {"type_mismatch", "unknown_type"}


@pytest.mark.parametrize(
    "name",
    [
        "page.html",
        "image.svg",
        "programme.exe",
        "rapport.pdf.exe",
        "sans_extension",
        ".pdf",
        "doc.docx",
        "a.PDF ",
    ],
)
def test_extension_not_in_the_whitelist(name: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload(name, PDF))
    assert _code(excinfo) == "extension"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("mesures.csv", b"\xff\xfeU\x00T\x00F\x00-\x001\x006\x00"),  # UTF-16
        ("notes.txt", "é".encode("latin-1") + b" latin-1\n"),
        ("notes.txt", b"texte\x00avec NUL\n"),
        ("notes.txt", b"bip\x07\n"),  # control character
        ("article.tex", b"\x1b[31mcouleur\n"),  # terminal escape
    ],
)
def test_text_must_be_clean_utf8(name: str, data: bytes) -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload(name, data))
    assert _code(excinfo) in {"type_mismatch", "unknown_type"}


def test_text_allows_tab_cr_lf_and_form_feed() -> None:
    v.validate_upload(_upload("notes.txt", b"a\tb\r\nc\x0cd\n"))


def test_empty_file_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload("notes.txt", b""))
    assert _code(excinfo) == "empty"


# --------------------------------------------------------------------------- size


def test_file_of_exactly_the_limit_is_accepted() -> None:
    data = b"a" * (v.MAX_UPLOAD_BYTES - 1) + b"\n"
    assert v.validate_upload(_upload("gros.txt", data)).size == v.MAX_UPLOAD_BYTES


def test_file_over_the_limit_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload("gros.txt", b"a" * (v.MAX_UPLOAD_BYTES + 1)))
    assert _code(excinfo) == "too_large"


class LyingUpload(SimpleUploadedFile):
    """Declares a small size but holds more: the limit is enforced on bytes read.

    Yields chunk_size pieces like Django's TemporaryUploadedFile (uploads over
    FILE_UPLOAD_MAX_MEMORY_SIZE); the in-memory class yields everything at once.
    """

    reads = 0

    def chunks(self, chunk_size: int | None = None) -> Any:
        assert self.file is not None
        self.file.seek(0)
        while piece := self.file.read(chunk_size or 64 * 1024):
            LyingUpload.reads += len(piece)
            yield piece


def test_declared_size_is_not_trusted() -> None:
    upload = LyingUpload("gros.txt", b"a" * (v.MAX_UPLOAD_BYTES + 10), "text/plain")
    upload.size = 10
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(upload)
    assert _code(excinfo) == "too_large"


def test_reading_stops_soon_after_the_limit() -> None:
    LyingUpload.reads = 0
    upload = LyingUpload("gros.txt", b"a" * (3 * v.MAX_UPLOAD_BYTES), "text/plain")
    upload.size = 10
    with pytest.raises(ValidationError):
        v.validate_upload(upload)
    assert LyingUpload.reads <= v.MAX_UPLOAD_BYTES + v.READ_CHUNK_BYTES


def test_declared_size_over_the_limit_is_refused_before_reading() -> None:
    LyingUpload.reads = 0
    upload = LyingUpload("rapport.pdf", PDF, "application/pdf")
    upload.size = v.MAX_UPLOAD_BYTES + 1
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(upload)
    assert _code(excinfo) == "too_large"
    assert LyingUpload.reads == 0


def test_messages_are_in_french() -> None:
    with pytest.raises(ValidationError) as excinfo:
        v.validate_upload(_upload("page.html", PDF))
    assert "type de fichier" in excinfo.value.messages[0].lower()


def test_mime_whitelist_is_the_one_the_database_checks() -> None:
    from apps.vault.models import ALLOWED_MIME_TYPES

    assert set(ALLOWED_MIME_TYPES) == {t.mime_type for t in v.FILE_TYPES.values()}
