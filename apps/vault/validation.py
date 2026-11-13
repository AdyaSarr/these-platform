"""Validation of uploaded files (docs/SECURITY.md, "Fichiers déposés").

Untrusted input (CLAUDE.md rule 11):
- size bounded on the bytes actually read (the declared size is only used to
  refuse early), at most MAX_UPLOAD_BYTES + one chunk held in memory;
- type recognised from the CONTENT, and required to match the extension;
- the client's Content-Type is ignored, and of the file name only the
  extension is used (the download name is rebuilt from the title).

Formats are not parsed (no ZIP decompression, no PDF rendering): files are
stored encrypted and only ever served as attachments with nosniff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class FileType:
    mime_type: str
    extension: str  # canonical, used for the download name
    extensions: tuple[str, ...]  # accepted on upload
    magic: tuple[bytes, ...] = ()  # empty: a text type


FILE_TYPES: Mapping[str, FileType] = MappingProxyType(
    {
        "pdf": FileType("application/pdf", "pdf", ("pdf",), (b"%PDF-",)),
        "png": FileType("image/png", "png", ("png",), (b"\x89PNG\r\n\x1a\n",)),
        "jpeg": FileType("image/jpeg", "jpg", ("jpg", "jpeg"), (b"\xff\xd8\xff",)),
        # Local file header, or end of central directory alone (empty archive).
        "zip": FileType("application/zip", "zip", ("zip",), (b"PK\x03\x04", b"PK\x05\x06")),
        "csv": FileType("text/csv", "csv", ("csv",)),
        "txt": FileType("text/plain", "txt", ("txt",)),
        "tex": FileType("application/x-tex", "tex", ("tex",)),
    }
)
_BY_EXTENSION = {ext: key for key, t in FILE_TYPES.items() for ext in t.extensions}
_TEXT = "text"
_UTF8_BOM = b"\xef\xbb\xbf"
# C0 controls except tab, LF, FF, CR; DEL; C1 controls. Searched in C (25 MB in ms).
_FORBIDDEN_CHARS = re.compile("[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class CheckedFile:
    data: bytes = field(repr=False)
    mime_type: str
    extension: str

    @property
    def size(self) -> int:
        return len(self.data)


class Upload(Protocol):
    """What validate_upload reads from Django's UploadedFile (nothing else)."""

    @property
    def name(self) -> str | None: ...

    @property
    def size(self) -> int | None: ...

    def chunks(self, chunk_size: int | None = None) -> Iterator[bytes]: ...


def validate_upload(upload: Upload) -> CheckedFile:
    """Return the file's bytes with OUR mime type and extension, or raise ValidationError."""
    expected = FILE_TYPES[_type_of_extension(upload.name or "")]
    if upload.size is not None and upload.size > MAX_UPLOAD_BYTES:
        raise _too_large()
    data = _read_bounded(upload)
    if not data:
        raise ValidationError(_("The file is empty."), code="empty")
    detected = _detect(data)
    if detected is None:
        raise ValidationError(
            _("The content of the file is not of an accepted type."), code="unknown_type"
        )
    matches = (detected == _TEXT) if not expected.magic else (FILE_TYPES.get(detected) == expected)
    if not matches:
        raise ValidationError(
            _("The content of the file does not match its extension (.%(extension)s)."),
            code="type_mismatch",
            params={"extension": expected.extension},
        )
    return CheckedFile(data=data, mime_type=expected.mime_type, extension=expected.extension)


def _type_of_extension(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, extension = base.rpartition(".")
    key = _BY_EXTENSION.get(extension.lower()) if dot and stem else None
    if key is None:
        allowed = ", ".join(sorted({t.extension for t in FILE_TYPES.values()}))
        raise ValidationError(
            _("This type of file is not accepted. Accepted types: %(types)s."),
            code="extension",
            params={"types": allowed},
        )
    return key


def _too_large() -> ValidationError:
    return ValidationError(
        _("The file is larger than %(limit)d MB."),
        code="too_large",
        params={"limit": MAX_UPLOAD_BYTES // (1024 * 1024)},
    )


def _read_bounded(upload: Upload) -> bytes:
    buffer = bytearray()
    for chunk in upload.chunks(READ_CHUNK_BYTES):
        buffer += chunk
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise _too_large()
    return bytes(buffer)


def _detect(data: bytes) -> str | None:
    for key, file_type in FILE_TYPES.items():
        if file_type.magic and data.startswith(file_type.magic):
            return key
    return _TEXT if _is_clean_text(data) else None


def _is_clean_text(data: bytes) -> bool:
    """Strict UTF-8 (optional BOM), no NUL nor control character but tab, LF, FF, CR."""
    try:
        text = data.removeprefix(_UTF8_BOM).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return _FORBIDDEN_CHARS.search(text) is None
