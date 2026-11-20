"""Replays the mutants of scripts/mutants.toml (SR-12). Started by scripts/mutants.sh.

For each mutant: the file is saved to var/mutants-backup/, `old` (found exactly
once) is replaced by `new` in the working tree, only the mutant's tests are run
(serially, fixed order), and the file is restored. Killed: pytest exits 1 and
every listed test has a FAILED case. Survived: pytest exits 0. Anything else
(stale `old`, setup ERROR only, collection error, interruption) is an error.

The tree must be clean (git status --porcelain) before the run, and is again
after it. The file is restored in a `finally`, on SIGINT and SIGTERM too; a
backup left by a killed run is restored by the next one (or --restore-only,
which mutants.sh calls on exit). Nothing is ever committed.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "scripts" / "mutants.toml"
BACKUP_DIR = REPO / "var" / "mutants-backup"
BACKUP = BACKUP_DIR / "current.json"
LOG_DIR = REPO / "var" / "mutants"
FAILED_LINE = re.compile(r"^FAILED (\S+)", re.MULTILINE)


def _git_dirty() -> str:
    # Fixed arguments, git from PATH: the developer's own tool.
    return subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip


def restore() -> bool:
    """Put back the file of a backup left by a run; True if there was one."""
    if not BACKUP.exists():
        return False
    saved = json.loads(BACKUP.read_text())
    (REPO / saved["file"]).write_text(saved["content"])
    BACKUP.unlink()
    print(f"mutants: {saved['file']} restored from its backup")
    return True


def _raise_on_term(signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def _pytest(tests: list[str], log: Path) -> tuple[int, str]:
    # Node ids from the versioned manifest; uv from PATH, as for every command of CLAUDE.md.
    command = ["uv", "run", "pytest", "-p", "no:randomly", "-q", "-rfE", *tests]
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)  # noqa: S603
    output = result.stdout + result.stderr
    log.write_text(output)
    return result.returncode, output


def _covered(node: str, failed: list[str]) -> bool:
    """Whether a FAILED node id belongs to ``node`` (a file or a test function)."""
    return any(f == node or f.startswith((node + "::", node + "[")) for f in failed)


def run_one(mutant: dict[str, Any]) -> tuple[str, str]:
    path = REPO / mutant["file"]
    original = path.read_text()
    count = original.count(mutant["old"])
    if count != 1:
        return "erreur", f"`old` trouvé {count} fois (mutant périmé)"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps({"file": mutant["file"], "content": original}))
    try:
        path.write_text(original.replace(mutant["old"], mutant["new"], 1))
        code, output = _pytest(mutant["tests"], LOG_DIR / f"{mutant['id']}.log")
    finally:
        path.write_text(original)
        BACKUP.unlink(missing_ok=True)
    failed = FAILED_LINE.findall(output)
    if code == 0:
        return "survivant", "tous les tests passent"
    missing = [t for t in mutant["tests"] if not _covered(t, failed)]
    if code == 1 and not missing:
        return "tué", f"{len(failed)} échec(s)"
    detail = f"pytest exit={code}"
    if missing:
        detail += f", sans échec : {', '.join(missing)}"
    return "erreur", detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", nargs="*", help="mutant ids (or their Mnn prefix); default: all")
    parser.add_argument("--list", action="store_true", help="list the mutants and stop")
    parser.add_argument("--restore-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.restore_only:
        restore()
        return 0

    mutants: list[dict[str, Any]] = tomllib.loads(MANIFEST.read_text())["mutant"]
    if args.list:
        for m in mutants:
            print(f"{m['id']:32} {m['file']}\n{'':32} {m['claim']}")
        return 0
    if args.ids:
        wanted = set(args.ids)
        mutants = [m for m in mutants if m["id"] in wanted or m["id"].split("-")[0] in wanted]
        if not mutants:
            print(f"mutants: aucun mutant ne correspond à {sorted(wanted)}", file=sys.stderr)
            return 2

    restore()  # a backup left by a killed run
    dirty = _git_dirty()
    if dirty:
        print("mutants: l'arbre de travail n'est pas propre ; rien n'est lancé.\n" + dirty,
              file=sys.stderr)  # fmt: skip
        return 2

    signal.signal(signal.SIGTERM, _raise_on_term)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    every_test = list(dict.fromkeys(t for m in mutants for t in m["tests"]))
    print(f"mutants: ligne de base, {len(every_test)} cible(s) sur le code intact…", flush=True)
    try:
        code, _ = _pytest(every_test, LOG_DIR / "baseline.log")
    except KeyboardInterrupt:
        print("\nmutants: interrompu pendant la ligne de base ; aucun fichier modifié.",
              file=sys.stderr)  # fmt: skip
        return 130
    if code != 0:
        print(f"mutants: la ligne de base échoue (pytest exit={code}, var/mutants/baseline.log) ;"
              " un mutant « tué » ne voudrait rien dire.", file=sys.stderr)  # fmt: skip
        return 2

    results = []
    try:
        for mutant in mutants:
            started = time.monotonic()
            print(f"mutants: {mutant['id']}…", end=" ", flush=True)
            verdict, detail = run_one(mutant)
            seconds = time.monotonic() - started
            print(f"{verdict} ({detail}, {seconds:.0f} s)", flush=True)
            results.append((mutant["id"], verdict, detail))
    except KeyboardInterrupt:
        print("\nmutants: interrompu ; fichier restauré.", file=sys.stderr)
        return 130
    finally:
        restore()

    if _git_dirty():
        print("mutants: l'arbre n'est plus propre après le passage !\n" + _git_dirty(),
              file=sys.stderr)  # fmt: skip
        return 3

    counts = {v: sum(1 for _, r, _ in results if r == v) for v in ("tué", "survivant", "erreur")}
    print("\nRésumé des mutants")
    for mutant_id, verdict, detail in results:
        print(f"  {verdict:10} {mutant_id:32} {detail}")
    print(
        f"mutants: {len(results)} lancés, {counts['tué']} tués, {counts['survivant']} survivants,"
        f" {counts['erreur']} en erreur (journaux : var/mutants/)"
    )
    return 0 if counts["tué"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
