"""The ZIP of an export, produced as a stream (M13b, « Fabrication »).

``zipfile`` writes into ``_Sink``, a buffer emptied after every piece: the output is
not seekable, so zipfile writes a data descriptor after each entry instead of
going back to its header (no temporary file, no copy of the whole archive). One
document is decrypted at a time, when its turn comes (vault.export_read), and its
SHA-256 is computed while it is written. ``manifest.json`` comes last, then the
central directory.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from typing import IO, Any, cast

from django.template.loader import render_to_string
from django.utils import timezone

from apps.vault import crypto

FORMAT = "these-export/1"
PIECE = 1024 * 1024  # bytes written to zipfile at a time


class _Sink:
    """The file zipfile writes to: no tell() (zipfile then streams), emptied by take()."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _info(name: str, *, compress: bool, at: Any) -> zipfile.ZipInfo:
    local = timezone.localtime(at)
    info = zipfile.ZipInfo(name, date_time=local.timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16  # rw------- once extracted
    return info


def stream(snapshot: Any, *, record: Any, read: Any) -> Iterator[bytes]:
    """The bytes of the ZIP. ``read(planned)``: the decrypted bytes of one version."""
    sink = _Sink()
    # Closed below, after the manifest (SIM115).
    archive = zipfile.ZipFile(cast("IO[bytes]", sink), mode="w", allowZip64=True)
    entries: list[dict[str, Any]] = []
    at = record.started_at

    def write(name: str, data: bytes, *, kind: str, compress: bool, **meta: Any) -> Iterator[bytes]:
        digest = crypto.Sha256()
        with archive.open(_info(name, compress=compress, at=at), mode="w", force_zip64=True) as f:
            for start in range(0, len(data), PIECE):
                piece = data[start : start + PIECE]
                f.write(piece)
                digest.update(piece)
                if out := sink.take():
                    yield out
        if out := sink.take():
            yield out
        entries.append({"path": name, "sha256": digest.hexdigest(), "size": len(data),
                        "kind": kind, **meta})  # fmt: skip

    readme = render_to_string("export/readme.txt", {"snapshot": snapshot, "at": at}).encode()
    yield from write("LISEZMOI.txt", readme, kind="readme", compress=True,
                     object_id=None, level=None)  # fmt: skip
    for name, value in snapshot.files.items():
        yield from write(name, _json(value), kind="data", compress=True, object_id=None,
                         level=None)  # fmt: skip
    for name, planned in snapshot.documents:
        data = read(planned)
        yield from write(name, data, kind="document", compress=False,
                         object_id=str(planned.document_id), level=planned.level.value)  # fmt: skip
        del data
    manifest = {
        "format": FORMAT,
        "created_at": at.isoformat(),
        "thesis_id": str(record.thesis_id),
        "options": {
            "all_versions": record.all_versions,
            "st_consent_id": str(record.consent_id) if record.consent_id else None,
        },
        "entries": entries,
        "excluded": dict(snapshot.excluded),
    }
    with archive.open(_info("manifest.json", compress=True, at=at), mode="w") as f:
        f.write(_json(manifest))
    archive.close()
    if out := sink.take():
        yield out
