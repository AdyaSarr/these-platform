"""List what the vault storage holds that the database does not expect, and vice versa.

READ-ONLY: never deletes, never opens a blob (directory listing and lstat only).

Sections (one line each: "<SECTION>\t<relative path>[\t<details>]"):
- MISSING          a document version whose blob does not exist (serious);
- ORPHAN           a blob no document version references, older than the grace
                   period (e.g. left by a rolled-back transaction: its DEK was
                   never stored, so it can never be decrypted);
- RECENT           an unreferenced blob younger than the grace period (a
                   transaction may still be about to reference it);
- STALE TEMPORARY  a temporary file older than the grace period;
- ANOMALY          anything else: unexpected name, symbolic link, not a regular
                   file, permissions wider than 700 / 600, or a file with more
                   than one hard link (e.g. a temporary name left linked to a
                   blob after a failed unlink, or a copy made by hand).
Exit status 2 when there is a MISSING blob or an ANOMALY.
"""

from __future__ import annotations

import os
import re
import stat
import time
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from apps.vault import storage
from apps.vault.models import DocumentVersion

_SHARD = re.compile(r"[0-9a-f]{2}")
_TMP = re.compile(re.escape(storage.TMP_PREFIX) + r"[0-9a-f]{32}")
_BLOB = re.compile(storage.BLOB_PATH_REGEX)


class Command(BaseCommand):
    help = "List orphan blobs, missing blobs and anomalies of the vault storage (no deletion)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--grace-hours",
            type=float,
            default=24.0,
            help="Unreferenced files younger than this are not reported as orphans (24).",
        )

    def handle(self, *args: Any, grace_hours: float, **options: Any) -> None:
        referenced = set(DocumentVersion.objects.values_list("ciphertext_path", flat=True))
        report = _Scan(grace_seconds=grace_hours * 3600)
        base = storage.root()
        if base.is_symlink() or not base.is_dir():
            report.add("ANOMALY", ".", "storage directory missing or not a directory")
        else:
            report.check_mode(".", base.lstat(), 0o700)
            report.scan(base, referenced)
        for path in sorted(referenced - report.found):
            report.add("MISSING", path)

        for section in ("MISSING", "ANOMALY", "ORPHAN", "RECENT", "STALE TEMPORARY"):
            for path, details in sorted(report.lines[section]):
                self.stdout.write("\t".join(filter(None, (section, path, details))))
        counts = ", ".join(f"{len(v)} {k.lower()}" for k, v in report.lines.items())
        self.stdout.write(f"SUMMARY\t{len(referenced)} referenced, {counts}")
        if report.lines["MISSING"] or report.lines["ANOMALY"]:
            raise CommandError("vault storage needs attention (MISSING or ANOMALY)", returncode=2)


class _Scan:
    def __init__(self, *, grace_seconds: float) -> None:
        self.grace = grace_seconds
        self.now = time.time()
        self.found: set[str] = set()
        self.lines: dict[str, list[tuple[str, str]]] = {
            k: [] for k in ("MISSING", "ANOMALY", "ORPHAN", "RECENT", "STALE TEMPORARY")
        }

    def add(self, section: str, path: str, details: str = "") -> None:
        self.lines[section].append((path, details))

    def check_mode(self, path: str, info: os.stat_result, allowed: int) -> None:
        mode = stat.S_IMODE(info.st_mode)
        if mode & ~allowed:
            self.add("ANOMALY", path, f"mode {mode:o}, expected at most {allowed:o}")

    def _old(self, info: os.stat_result) -> bool:
        return self.now - info.st_mtime > self.grace

    def scan(self, base: os.PathLike[str], referenced: set[str]) -> None:
        with os.scandir(base) as shards:
            for shard in shards:
                if shard.is_symlink() or not shard.is_dir(follow_symlinks=False):
                    self.add("ANOMALY", shard.name, "unexpected entry at the root")
                elif not _SHARD.fullmatch(shard.name):
                    self.add("ANOMALY", shard.name, "unexpected directory name")
                else:
                    self.check_mode(shard.name, shard.stat(follow_symlinks=False), 0o700)
                    self._scan_shard(shard.path, shard.name, referenced)

    def _scan_shard(self, path: str, shard: str, referenced: set[str]) -> None:
        with os.scandir(path) as entries:
            for entry in entries:
                rel = f"{shard}/{entry.name}"
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    self.add("ANOMALY", rel, "symbolic link")
                elif not stat.S_ISREG(info.st_mode):
                    self.add("ANOMALY", rel, "not a regular file")
                elif info.st_nlink != 1:
                    self.add("ANOMALY", rel, f"{info.st_nlink} hard links")
                    if _BLOB.fullmatch(rel):
                        self.found.add(rel)  # present, even if oddly linked
                elif _TMP.fullmatch(entry.name):
                    self.check_mode(rel, info, 0o600)
                    if self._old(info):
                        self.add("STALE TEMPORARY", rel, f"{info.st_size} bytes")
                elif _BLOB.fullmatch(rel):
                    self.check_mode(rel, info, 0o600)
                    self.found.add(rel)
                    if rel not in referenced:
                        section = "ORPHAN" if self._old(info) else "RECENT"
                        self.add(section, rel, f"{info.st_size} bytes")
                else:
                    self.add("ANOMALY", rel, "unexpected file name")
