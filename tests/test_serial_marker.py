"""Every test that needs to run alone carries @pytest.mark.serial.

scripts/test_all.sh runs the suite in two phases: in parallel (pytest-xdist) the
tests not marked serial, then alone the tests marked serial. A test needs to run
alone when it reaches, in its own body or through a function or fixture of its
module, one of:
- threads (run_threads, threading.Thread, ThreadPoolExecutor): real concurrent
  connections with waits and timeouts, that a loaded CPU could fail for nothing;
- a listening socket (socket.socket, socket.create_server): a measured timeout;
- a child pytest run (run_pytest, or a subprocess call naming "pytest").
"""

from __future__ import annotations

import ast
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
THREAD_NAMES = {"run_threads", "Thread", "ThreadPoolExecutor", "run_pytest"}
SUBPROCESS_CALLS = {"run", "Popen", "check_output", "check_call", "call"}

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _is_serial_mark(node: ast.AST) -> bool:
    """``pytest.mark.serial`` (or ``mark.serial``), called or not, in a list or not."""
    return any(
        isinstance(n, ast.Attribute)
        and n.attr == "serial"
        and isinstance(n.value, ast.Attribute | ast.Name)
        and (n.value.attr if isinstance(n.value, ast.Attribute) else n.value.id) == "mark"
        for n in ast.walk(node)
    )


def _module_is_serial(tree: ast.Module) -> bool:
    return any(
        isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets)
        and _is_serial_mark(stmt.value)
        for stmt in tree.body
    )


def _reaches_directly(function: Function) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in THREAD_NAMES:
            return True
        if isinstance(node, ast.Attribute):
            if node.attr in THREAD_NAMES - {"run_pytest"} and _base(node) == "threading":
                return True
            if node.attr in {"socket", "create_server"} and _base(node) == "socket":
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SUBPROCESS_CALLS
            and _base(node.func) == "subprocess"
            and any(
                isinstance(c, ast.Constant) and c.value == "pytest"
                for arg in node.args
                for c in ast.walk(arg)
            )
        ):
            return True
    return False


def _base(node: ast.Attribute) -> str | None:
    return node.value.id if isinstance(node.value, ast.Name) else None


def _functions(tree: ast.Module) -> list[tuple[Function, bool]]:
    """Module-level functions and methods of module-level classes, each with whether
    its class is marked serial."""
    found: list[tuple[Function, bool]] = []
    for stmt in tree.body:
        if isinstance(stmt, Function):
            found.append((stmt, False))
        elif isinstance(stmt, ast.ClassDef):
            serial = any(_is_serial_mark(d) for d in stmt.decorator_list)
            found += [(s, serial) for s in stmt.body if isinstance(s, Function)]
    return found


def unmarked_serial_tests(source: str) -> set[str]:
    """Names of the tests of ``source`` that need to run alone and are not marked serial."""
    tree = ast.parse(source)
    if _module_is_serial(tree):
        return set()
    functions = _functions(tree)
    by_name = {f.name: f for f, _ in functions}
    uses: dict[str, set[str]] = {}
    for f, _ in functions:
        names = {n.id for n in ast.walk(f) if isinstance(n, ast.Name)}
        params = {a.arg for a in [*f.args.posonlyargs, *f.args.args, *f.args.kwonlyargs]}
        uses[f.name] = (names | params) & by_name.keys() - {f.name}
    reaching = {name for name, f in by_name.items() if _reaches_directly(f)}
    while True:  # helpers and fixtures of the module, transitively
        more = {name for name, used in uses.items() if used & reaching} - reaching
        if not more:
            break
        reaching |= more
    return {
        f.name
        for f, class_serial in functions
        if f.name.startswith("test")
        and f.name in reaching
        and not class_serial
        and not any(_is_serial_mark(d) for d in f.decorator_list)
    }


def test_detector_sees_every_form() -> None:
    """Guards against a scan that silently finds nothing."""
    cases = {
        "def test_a():\n    run_threads(f, control=g)": {"test_a"},
        "import threading\ndef test_a():\n    threading.Thread(target=f).start()": {"test_a"},
        "def test_a():\n    with ThreadPoolExecutor() as e: pass": {"test_a"},
        "import socket\ndef test_a():\n    socket.socket()": {"test_a"},
        "import socket\ndef test_a():\n    socket.create_server(('', 0))": {"test_a"},
        "def test_a():\n    subprocess.run([sys.executable, '-m', 'pytest'])": {"test_a"},
        "def test_a():\n    run_pytest('-q')": {"test_a"},
        # Through a helper, a nested helper, a fixture, a method.
        "def _h():\n    run_threads()\ndef _g():\n    _h()\ndef test_a():\n    _g()": {"test_a"},
        "@pytest.fixture\ndef server():\n    socket.socket()\ndef test_a(server): pass": {"test_a"},
        "class TestX:\n    def test_a(self):\n        run_threads()": {"test_a"},
    }
    for source, expected in cases.items():
        assert unmarked_serial_tests(source) == expected, source


def test_detector_accepts_every_serial_mark_and_ignores_the_rest() -> None:
    marked = [
        "@pytest.mark.serial\ndef test_a():\n    run_threads()",
        "@pytest.mark.serial()\ndef test_a():\n    run_threads()",
        "pytestmark = pytest.mark.serial\ndef test_a():\n    run_threads()",
        "pytestmark = [pytest.mark.django_db, pytest.mark.serial]\ndef test_a():\n"
        "    run_threads()",
        "@pytest.mark.serial\nclass TestX:\n    def test_a(self):\n        run_threads()",
    ]
    for source in marked:
        assert unmarked_serial_tests(source) == set(), source
    harmless = [
        "def test_a():\n    subprocess.run(['git', 'ls-files'])",
        "def test_a():\n    threading.Event().set()",  # no thread started here
        "def test_a():\n    socket.gethostname()",
        "def _h():\n    run_threads()\ndef test_a(): pass",  # helper never used
    ]
    for source in harmless:
        assert unmarked_serial_tests(source) == set(), source


def test_every_test_that_needs_to_run_alone_is_marked_serial() -> None:
    files = sorted(
        p
        for root in ("apps", "tests")
        for p in (BASE_DIR / root).rglob("test_*.py")
        if "__pycache__" not in p.parts
    )
    assert len(files) > 100  # the scan did find the suite
    missing = {
        f"{path.relative_to(BASE_DIR)}::{name}"
        for path in files
        for name in unmarked_serial_tests(path.read_text())
    }
    assert not missing, "mark these tests @pytest.mark.serial:\n" + "\n".join(sorted(missing))
