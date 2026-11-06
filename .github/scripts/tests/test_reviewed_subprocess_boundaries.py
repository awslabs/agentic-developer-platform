"""Reviewed subprocess exceptions retain fixed executable and argument boundaries."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import cryptography
import pytest


ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_design_audit_keeps_metacharacters_in_a_literal_git_argument(monkeypatch):
    audit = load("reviewed_design_audit", "docs/tools/atlassian-mcp/design_audit.py")
    output = Mock(return_value=b"source")
    monkeypatch.setattr(audit.subprocess, "check_output", output)
    reference = audit.BASE + ":path;$(printf not-a-command)"

    assert audit.git("show", reference) == b"source"
    output.assert_called_once_with(["git", "show", reference])


@pytest.mark.parametrize(
    "entrypoint", ["adp-native-credentials.py", "adp-native-credential-host.py"]
)
def test_native_fallback_keeps_the_private_runtime_and_script_fixed(
    tmp_path, monkeypatch, entrypoint
):
    monkeypatch.setattr(sys, "path", list(sys.path))
    module = load("reviewed_native_entrypoint", "modules/gateway/cli/" + entrypoint)
    runtime = tmp_path / "private-runtime"
    executable = runtime / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(cryptography, "__version__", "requires-private-runtime")
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    forwarded = ["--", "other-executable", "-c", "$(printf not-a-command)"]
    execute = Mock(side_effect=RuntimeError("controlled re-exec"))
    monkeypatch.setattr(module.os, "execv", execute)
    script = str(Path(module.__file__).resolve())

    if entrypoint == "adp-native-credentials.py":
        monkeypatch.setattr(module, "runtime_dir", lambda: runtime)
        with pytest.raises(RuntimeError, match="controlled re-exec"):
            module.ensure_runtime(forwarded)
    else:
        monkeypatch.setattr(module.client, "runtime_dir", lambda: runtime)
        monkeypatch.setattr(sys, "argv", [script, *forwarded])
        with pytest.raises(RuntimeError, match="controlled re-exec"):
            module.ensure_runtime()

    execute.assert_called_once_with(
        str(executable), [str(executable), "-I", script, *forwarded]
    )
