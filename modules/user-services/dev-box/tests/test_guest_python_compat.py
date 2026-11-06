"""Guest-side Crew helpers must run on AL2023's system Python 3.9 (#8055).

The first live user-data bootstrap failed at `install_crew.py` with
``TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'``: PEP 604 unions in
a ``def`` signature are evaluated at definition time unless annotations are postponed.
The systemd units (``/usr/bin/python3``) and the controller's SSM commands (``python3``)
run the installed helpers on the system interpreter, so every helper the image or the
bootstrap installs is compiled here against the 3.9 grammar, checked for the known
3.10+ constructs that the grammar does not catch, and, when a 3.9 interpreter is
available on the box, actually imported under it.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
IMAGE_DIR = MODULE_DIR / "image"
CHECKPOINT = MODULE_DIR.parents[1] / "agent-factory" / "agent" / "scripts" / "crew_checkpoint.py"
HELPERS = sorted(IMAGE_DIR.glob("*.py")) + [CHECKPOINT]
# Shipped onto the guest by crew.pkr.hcl / crew-bootstrap.sh.tftpl (render_template.py is build-side only).
INSTALLED = ("install_crew.py", "write_enrollment.py", "write_host_certificate.py", "verify_crew_ready.py",
             "restore_crew_host.py", "checkpoint_crew_host.py", "crew_checkpoint.py")

# A `def` line (or its continuation) carrying `X | None` / `None | X` / `-> X | Y` in annotations.
PEP604_IN_ANNOTATION = re.compile(r"(?m)^\s*(?:def\s.*|[\w*]+.*,\s*)?\w+\s*:\s*[\w\[\], .]+\s*\|\s*(?:None|[\w\[\]]+)\s*[=,)]|->\s*[\w\[\], .]+\s*\|")
RUNTIME_310_PLUS = (
    (re.compile(r"(?m)^\s*match\s+[^=]+:\s*$"), "match statement"),
    (re.compile(r"\bzip\([^)]*\bstrict\s*="), "zip(strict=)"),
    (re.compile(r"@dataclass\([^)]*\b(slots|kw_only)\s*="), "dataclass(slots=/kw_only=)"),
    (re.compile(r"\bTypeAlias\b|\bParamSpec\b|\bTypeGuard\b|\bSelf\b"), "typing 3.10+ names"),
    (re.compile(r"\bisinstance\([^,]+,\s*[\w.]+(\[[^\]]*\])?\s*\|"), "isinstance with a union"),
    (re.compile(r"\bint\.bit_count\b|\bitertools\.pairwise\b|\baiter\(|\banext\(|\bimport zoneinfo\b|\bimport graphlib\b"), "3.10+ stdlib"),
)


def _python39():
    """A CPython 3.9 on this box: PATH, the uv store, or the distro path; None when absent."""
    candidates = [shutil.which("python3.9"), "/usr/bin/python3.9"]
    candidates += [str(path) for path in sorted(Path.home().joinpath(".local/share/uv/python").glob("cpython-3.9*/bin/python3.9"))]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            probe = subprocess.run([candidate, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"], capture_output=True, text=True, check=False)
            if probe.stdout.strip() == "True":
                return candidate
    return None


PYTHON39 = _python39()


def test_every_shipped_helper_is_covered():
    assert {path.name for path in HELPERS} >= set(INSTALLED)
    assert CHECKPOINT.is_file()


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: path.name)
def test_helper_compiles_under_the_3_9_grammar(helper):
    ast.parse(helper.read_text(), str(helper), feature_version=(3, 9))


@pytest.mark.parametrize("helper", HELPERS, ids=lambda path: path.name)
def test_helper_does_not_evaluate_3_10_constructs_at_import_time(helper):
    source = helper.read_text()
    for pattern, label in RUNTIME_310_PLUS:
        assert pattern.search(source) is None, f"{helper.name}: {label}"
    if PEP604_IN_ANNOTATION.search(source):
        tree = ast.parse(source)
        first = next((node for node in tree.body if not isinstance(node, ast.Expr)), None)
        assert (isinstance(first, ast.ImportFrom) and first.module == "__future__"
                and any(alias.name == "annotations" for alias in first.names)), (
            f"{helper.name} uses `X | None` in a signature; postpone evaluation with `from __future__ import annotations`")


@pytest.mark.parametrize("name", INSTALLED)
def test_installed_helper_imports_under_python_3_9(name):
    if PYTHON39 is None:
        pytest.skip("no CPython 3.9 on this box (python3.9, /usr/bin/python3.9 or the uv store)")
    helper = CHECKPOINT if name == "crew_checkpoint.py" else IMAGE_DIR / name
    # /opt/adp/crew holds every helper side by side; restore_crew_host imports crew_checkpoint from there.
    code = ("import importlib.util, sys; sys.path[:0] = sys.argv[2:]; "
            "spec = importlib.util.spec_from_file_location(sys.argv[1].rsplit('/', 1)[-1][:-3], sys.argv[1]); "
            "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print('import-ok')")
    result = subprocess.run([PYTHON39, "-I", "-c", code, str(helper), str(IMAGE_DIR), str(CHECKPOINT.parent)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and result.stdout.strip() == "import-ok", result.stderr


def test_install_crew_tar_filter_is_available_on_3_9():
    """install_crew.py extracts with filter="data" (3.12, backported to 3.9.17+)."""
    if PYTHON39 is None:
        pytest.skip("no CPython 3.9 on this box")
    result = subprocess.run([PYTHON39, "-I", "-c", "import tarfile; print(hasattr(tarfile, 'data_filter'))"], capture_output=True, text=True, check=False)
    assert result.stdout.strip() == "True"


def test_the_running_interpreter_is_not_what_the_guest_has():
    """Guard against assuming the dev box's interpreter: this suite runs on 3.12, the guest on 3.9."""
    assert sys.version_info >= (3, 12)
