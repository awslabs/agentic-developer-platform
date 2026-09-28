"""Installed Task CLI dispatch and package/download seams (#6064)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
PAYLOAD = ("adp-task.py", "adp_task_client.py")


def files(path):
    match = re.search(r'^CLI_FILES="([^"]+)"', path.read_text(), re.MULTILINE)
    assert match, "installer/update payload must be explicit"
    return match.group(1).split()


def test_task_install_update_and_download_payload_agree():
    from src.cli_download.routes import ALLOWED_SCRIPTS, PYTHON_SCRIPT_MEDIA_TYPE, SCRIPT_MEDIA_TYPES

    install, update = files(CLI / "install.sh"), files(CLI / "adp")
    assert install == update
    for name in PAYLOAD:
        assert name in install
        assert ALLOWED_SCRIPTS[name] == (CLI / name).resolve()
        assert SCRIPT_MEDIA_TYPES[name] == PYTHON_SCRIPT_MEDIA_TYPE
        assert ALLOWED_SCRIPTS[name].is_file()


@pytest.fixture
def installed(tmp_path):
    prefix = tmp_path / "bin"
    shutil.copytree(CLI, prefix)
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"], "ADP_HOME": str(home / ".adp")}

    def run(*args):
        return subprocess.run(["bash", str(prefix / "adp"), *args], env=env, text=True, capture_output=True, timeout=20)

    return prefix, run


def test_real_dispatch_preserves_selected_deployment_and_arguments(installed):
    prefix, run = installed
    for name in ("development", "isolated"):
        result = run("deployment", "add", name, "--url", f"https://{name}.example.test")
        assert result.returncode == 0, result.stderr
    (prefix / "adp-task.py").write_text(
        "import os,sys,json,adp_common\n"
        "print(json.dumps({'argv':sys.argv[1:], 'deployment':os.environ.get('ADP_DEPLOYMENT_ID'), "
        "'config':os.environ.get('BG_CONFIG_DIR'), 'url':adp_common.gateway_url()}))\n"
    )
    result = run("--deployment", "isolated", "task", "monitor", "tsk_fixture", "--timeout", "3")
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["argv"] == ["monitor", "tsk_fixture", "--timeout", "3"]
    assert observed["deployment"]
    assert observed["url"] == "https://isolated.example.test/api"
    wrong = run("task", "status", "tsk_fixture", "--deployment", "development")
    assert wrong.returncode != 0
    assert "deployment" in wrong.stderr.lower()


def test_task_help_and_all_command_help_work_without_credentials(installed):
    _, run = installed
    top = run("help")
    assert top.returncode == 0
    assert "task" in top.stdout
    for verb in ("submit", "status", "monitor", "abort"):
        result = run("task", verb, "--help")
        assert result.returncode == 0, result.stderr
        assert verb in result.stdout.lower()


def test_human_task_resolves_selected_tenant_service_task_does_not(installed):
    prefix, run = installed
    assert run("deployment", "add", "isolated", "--url", "https://isolated.example.test").returncode == 0
    (prefix / "adp-tenant.py").write_text(
        "import sys\nassert sys.argv[1:] == ['--resolve-env', 'tenant-b']\nprint('export ADP_TENANT_ID=tenant-b')\n"
    )
    (prefix / "adp-task.py").write_text("import os,json\nprint(json.dumps({'tenant':os.environ.get('ADP_TENANT_ID')}))\n")
    human = run("--deployment", "isolated", "--tenant", "tenant-b", "task", "status", "tsk_fixture", "--human-login")
    assert human.returncode == 0, human.stderr
    assert json.loads(human.stdout)["tenant"] == "tenant-b"
    service = run("--deployment", "isolated", "task", "status", "tsk_fixture")
    assert service.returncode == 0, service.stderr
    assert json.loads(service.stdout)["tenant"] is None
    refused = run("--deployment", "isolated", "--tenant", "tenant-b", "task", "status", "tsk_fixture")
    assert refused.returncode != 0 and "--human-login" in refused.stderr
