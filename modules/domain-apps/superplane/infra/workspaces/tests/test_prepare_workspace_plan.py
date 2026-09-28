import json
from pathlib import Path
import subprocess
import sys

import pytest

from backend_fixtures import backend_config, initialize, with_account_cli
from test_plan_safety import (
    ACCOUNT,
    ENVIRONMENT,
    REGION,
    WORKSPACE,
    GUARD,
    _cluster,
    _plan,
)


def prepare_fixture(tmp_path):
    module = tmp_path / "module"
    module.mkdir()
    variables = tmp_path / "input.json"
    target = {
        "account_id": ACCOUNT,
        "aws_region": REGION,
        "environment": ENVIRONMENT,
        "workspace_name": WORKSPACE,
        "org_id": "test-org",
        "workspace_id": WORKSPACE,
    }
    variables.write_text(json.dumps(target))
    record = tmp_path / "calls.json"
    executable = tmp_path / "terraform"
    plan = _plan(_cluster(["create"]))
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,zipfile\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(Path(__file__).parent)!r})\n"
        "from backend_fixtures import initialize,saved_plan\n"
        f"record=Path({str(record)!r});plan={plan!r}\n"
        "calls=json.loads(record.read_text()) if record.exists() else [];calls.append(sys.argv[1:]);record.write_text(json.dumps(calls))\n"
        "if sys.argv[1]=='version': print(json.dumps({'terraform_version':'1.9.8'}))\n"
        "elif sys.argv[1]=='init':\n"
        "    path=next(x.split('=',1)[1] for x in sys.argv if x.startswith('-backend-config='))\n"
        "    initialize(Path.cwd(),json.loads(Path(path).read_text()))\n"
        "elif sys.argv[1]=='plan':\n"
        "    output=next(x.split('=',1)[1] for x in sys.argv if x.startswith('-out='))\n"
        "    variables=next(x.split('=',1)[1] for x in sys.argv if x.startswith('-var-file='))\n"
        "    assert Path(variables).exists()\n"
        "    config=json.loads(Path('.terraform/terraform.tfstate').read_text())['backend']['config']\n"
        "    saved_plan(Path(output),plan,config)\n"
        "elif sys.argv[1]=='show':\n"
        "    with zipfile.ZipFile(sys.argv[-1]) as archive: print(archive.read('rendering.json').decode())\n"
        "else: sys.exit(99)\n"
    )
    executable.chmod(0o700)
    command = [
        sys.executable,
        str(GUARD.with_name("prepare_workspace_plan.py")),
        "--module-dir",
        str(module),
        "--variables",
        str(variables),
        "--output-dir",
        str(tmp_path / "review"),
        "--backend-bucket",
        "test-state",
        "--backend-region",
        REGION,
        "--lock-table",
        "test-locks",
        "--terraform-binary",
        str(executable),
    ]
    return with_account_cli(command, tmp_path, ACCOUNT), module, target, record


def test_preparation_derives_and_binds_actual_backend_without_applying(tmp_path):
    command, _, target, record = prepare_fixture(tmp_path)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    authorization = json.loads(
        (tmp_path / "review/workspace-authorization.proposed.json").read_text()
    )
    assert authorization["backend"]["key"] == backend_config(target)["key"]
    assert authorization["plan_file_sha256"]
    assert all(call[0] != "apply" for call in json.loads(record.read_text()))


def test_fresh_account_missing_role_is_refused_before_initialization(tmp_path):
    command, _, _, record = prepare_fixture(tmp_path)
    (tmp_path / "missing-account-role").touch()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "prerequisite" in result.stdout
    assert all(call[0] == "version" for call in json.loads(record.read_text()))


@pytest.mark.parametrize(
    "problem",
    [
        "different-workspace",
        "different-environment",
        "different-bucket",
        "local-state",
        "named-workspace",
    ],
)
def test_preparation_does_not_reconfigure_or_migrate_reused_directory(
    tmp_path, problem
):
    command, module, target, record = prepare_fixture(tmp_path)
    config = backend_config(target)
    if problem == "different-workspace":
        config["key"] = config["key"].replace(WORKSPACE, "other")
    elif problem == "different-environment":
        config["key"] = config["key"].replace(ENVIRONMENT + "/", "prod/")
    elif problem == "different-bucket":
        config["bucket"] = "other-state"
    elif problem == "local-state":
        (module / "terraform.tfstate").write_text("{}")
    initialize(module, config)
    if problem == "named-workspace":
        (module / ".terraform/environment").write_text("other")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert all(call[0] == "version" for call in json.loads(record.read_text()))
