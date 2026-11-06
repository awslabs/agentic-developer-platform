import json
import runpy
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = runpy.run_path(str(ROOT / "modules/gateway/scripts/platform-operator-roles.py"))
ACCOUNT = "123456789012"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/qualification/owned"


@pytest.mark.parametrize("value", ["", ROLE, ROLE + "," + ROLE + "-2"])
def test_exact_list(value):
    assert MODULE["validate"](value, ACCOUNT) == value


@pytest.mark.parametrize("value", [
    ROLE + "," + ROLE, ROLE + ",", ROLE + "*", ROLE + "\n",
    ROLE.replace(ACCOUNT, "999999999999"), ROLE + "|", ROLE + "&",
    "arn:aws:sts::123456789012:assumed-role/test/session", None,
])
def test_invalid_list(value):
    with pytest.raises(ValueError):
        MODULE["validate"](value, ACCOUNT)


def test_missing_parameter_preserves_existing_default(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 254, "", "(ParameterNotFound)"))
    assert MODULE["load"]("dev", ACCOUNT, "us-east-1") == f"arn:aws:iam::{ACCOUNT}:role/ADP-Agent-dev-platform"


def test_read_failure_refuses_deployment(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 254, "", "(AccessDeniedException)"))
    with pytest.raises(ValueError, match="Cannot read"):
        MODULE["load"]("dev", ACCOUNT, "us-east-1")


def test_explicit_empty_disables_exception(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps({"Parameter": {"Value": ""}}), ""))
    assert MODULE["load"]("dev", ACCOUNT, "us-east-1") == ""
