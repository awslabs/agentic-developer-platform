"""The apply command must consume only the verified saved binary, even across path changes."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend_fixtures import account_prerequisites, with_account_cli

from test_plan_safety import (
    ACCOUNT,
    ENVIRONMENT,
    GUARD,
    REGION,
    WORKSPACE,
    _assert_denied,
    _authorization_for,
    _change,
    _cluster,
    _owned_tags,
    _plan,
    _run,
    _stub_artifact,
    _stub_terraform,
    _write,
)


@pytest.mark.parametrize("actions", [["create"], ["update"]])
@pytest.mark.parametrize("missing", ["--plan-file", "--inventory", "--estimate"])
def test_ordinary_plan_requires_complete_evidence(tmp_path, actions, missing):
    plan = _write(tmp_path, _plan(_cluster(actions)))
    flags = {
        "--plan-file": _stub_artifact(tmp_path, plan),
        "--terraform-binary": _stub_terraform(tmp_path),
        "--inventory": tmp_path / "inventory.json",
        "--estimate": tmp_path / "estimate.json",
    }
    del flags[missing]
    extra = [part for flag, path in flags.items() for part in (flag, str(path))]
    _assert_denied(_run(plan, *extra, complete=False), because=missing)


@pytest.mark.parametrize("actions", [["create"], ["update"]])
def test_ordinary_plan_cannot_claim_a_different_target(tmp_path, actions):
    plan = _write(tmp_path, _plan(_cluster(actions)))
    _assert_denied(
        _run(plan, region="us-west-2"), because="but the PLAN says aws_region"
    )


@pytest.mark.parametrize("actions", [["create"], ["update"]])
def test_ordinary_plan_must_render_the_saved_binary(tmp_path, actions):
    plan = _write(tmp_path, _plan(_cluster(actions)))
    artifact = _stub_artifact(tmp_path, plan)
    changed = json.loads(plan.read_text())
    changed["planned_values"] = {"root_module": {"resources": []}}
    plan.write_text(json.dumps(changed))
    _assert_denied(
        _run(plan, "--plan-file", str(artifact)), because="is NOT a rendering"
    )


@pytest.mark.parametrize(
    "actions",
    [
        ["create"],
        ["update"],
        ["no-op"],
        ["delete", "create"],
        ["create", "delete"],
        ["delete"],
    ],
)
def test_owned_eip_fixed_cost_in_every_action_class(actions):
    from check_workspace_plan import _estimate

    plan = _plan(_change("aws_eip.nat[0]", actions, {"tags_all": _owned_tags()}))
    estimate = _estimate(plan, aws_region=REGION)
    assert estimate["bounded_monthly_usd"] == pytest.approx(
        0 if actions == ["delete"] else 3.65
    )


def _apply_fixture(tmp_path, *, mode="normal", apply_exit=0, principal=None):
    principal = principal or f"arn:aws:iam::{ACCOUNT}:user/operator"
    rendered = _plan(_cluster(["create"]))
    rendered["planned_values"]["outputs"]["provisioning_principal_arn"]["value"] = (
        principal
    )
    plan = _write(tmp_path, rendered)
    from backend_fixtures import backend_config, initialize, saved_plan

    target = {
        "environment": ENVIRONMENT,
        "workspace_name": WORKSPACE,
        "org_id": "test-org",
        "workspace_id": WORKSPACE,
        "account_id": ACCOUNT,
    }
    config = backend_config(target)
    initialize(tmp_path, config)
    artifact = tmp_path / "saved.tfplan"
    saved_plan(artifact, json.loads(plan.read_text()), config)
    auth = tmp_path / "authorization.json"
    document = _authorization_for(plan, artifact)
    document["backend"] = {"type": "s3", "workspace": "default", **config}
    document["account_prerequisites"] = account_prerequisites(ACCOUNT)
    document["provisioning_principal"] = {
        "account_id": ACCOUNT,
        "principal_arn": principal,
    }
    auth.write_text(json.dumps(document))
    record = tmp_path / "applied.json"
    executable = tmp_path / "terraform"
    # On show, deliberately replace either the original source or the private copy AFTER
    # reading the rendering. Apply records the digest it actually consumed.
    executable.write_text(
        f"#!{sys.executable}\n"
        "import hashlib,json,sys,zipfile\n"
        "from pathlib import Path\n"
        f"mode={mode!r}\nsource=Path({str(artifact)!r})\nrecord=Path({str(record)!r})\n"
        "if sys.argv[1:3] == ['show','-json']:\n"
        "    saved=Path(sys.argv[3])\n"
        "    with zipfile.ZipFile(saved) as archive: rendered=archive.read('rendering.json').decode()\n"
        "    if mode == 'replace-source': source.write_bytes(b'other plan')\n"
        "    if mode == 'replace-private':\n"
        "        saved.chmod(0o600)\n"
        "        saved.write_bytes(b'other plan')\n"
        "    print(rendered)\n"
        "elif sys.argv[1:3] == ['apply','-input=false']:\n"
        "    saved=Path(sys.argv[3])\n"
        "    record.write_text(json.dumps({'argv':sys.argv[1:],'digest':hashlib.sha256(saved.read_bytes()).hexdigest()}))\n"
        f"    sys.exit({apply_exit})\n"
        "else: sys.exit(99)\n"
    )
    executable.chmod(0o700)
    command = [sys.executable, str(GUARD.with_name("apply_workspace_plan.py"))]
    flags = {
        "plan-json": plan,
        "plan-file": artifact,
        "authorization": auth,
        "inventory": tmp_path / "inventory.json",
        "estimate": tmp_path / "estimate.json",
        "module-dir": tmp_path,
        "terraform-binary": executable,
        "account-id": ACCOUNT,
        "aws-region": REGION,
        "environment": ENVIRONMENT,
        "workspace-name": WORKSPACE,
        "org-id": "test-org",
        "workspace-id": WORKSPACE,
    }
    command += [
        part for flag, value in flags.items() for part in (f"--{flag}", str(value))
    ]
    return with_account_cli(command, tmp_path, ACCOUNT), artifact, auth, record


@pytest.mark.parametrize("problem", ["missing-role", "missing-evidence"])
def test_apply_rechecks_account_prerequisites_before_mutation(tmp_path, problem):
    command, _, auth, record = _apply_fixture(tmp_path)
    if problem == "missing-role":
        (tmp_path / "missing-account-role").touch()
    else:
        document = json.loads(auth.read_text())
        del document["account_prerequisites"]
        auth.write_text(json.dumps(document))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "prerequisite" in result.stderr
    assert not record.exists()


@pytest.mark.parametrize("mode", ["normal", "replace-source"])
def test_applies_reviewed_copy_after_complete_verification(tmp_path, mode):
    command, artifact, _, record = _apply_fixture(tmp_path, mode=mode)
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(record.read_text())
    assert receipt["digest"] == expected
    assert Path(receipt["argv"][-1]) != artifact
    assert not Path(receipt["argv"][-1]).exists()  # private copy is cleaned up
    assert (
        json.loads((tmp_path / "estimate.json").read_text())["bounded_monthly_usd"] > 0
    )


@pytest.mark.parametrize(
    "problem",
    [
        "artifact",
        "authorization-target",
        "authorization-json",
        "authorization-artifact",
        "replace-private",
    ],
)
def test_changed_or_unauthorized_plan_never_reaches_apply(tmp_path, problem):
    command, artifact, auth, record = _apply_fixture(tmp_path, mode=problem)
    if problem == "artifact":
        artifact.write_bytes(b"other plan")
    elif problem.startswith("authorization-"):
        document = json.loads(auth.read_text())
        field, value = {
            "authorization-target": ("workspace_name", "other-workspace"),
            "authorization-json": ("plan_sha256", "0" * 64),
            "authorization-artifact": ("plan_file_sha256", "0" * 64),
        }[problem]
        document[field] = value
        auth.write_text(json.dumps(document))
    result = subprocess.run(command, capture_output=True, text=True)
    _assert_denied(result, because="DENIED")
    assert not record.exists()


def test_apply_failure_is_returned(tmp_path):
    command, _, _, record = _apply_fixture(tmp_path, apply_exit=7)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 7
    assert record.exists()


@pytest.mark.parametrize(
    "problem", ["missing", "extra-address", "wrong-shape", "empty-identity"]
)
def test_authorization_requires_complete_identity_mapping(tmp_path, problem):
    from test_plan_safety import _authorize, _genuine_plan

    plan = _genuine_plan(creating=False)
    plan_path = _write(tmp_path, plan)
    flags = _authorize(tmp_path, plan)
    auth = Path(flags[flags.index("--authorize-destroy") + 1])
    document = json.loads(auth.read_text())
    if problem == "missing":
        del document["destroy_identities"]
    elif problem == "extra-address":
        document["destroy_identities"]["aws_eks_cluster.unrelated"] = {
            "name": "unrelated"
        }
    elif problem == "empty-identity":
        document["destroy_identities"]["aws_eks_cluster.workspace"] = {}
    else:
        document["destroy_identities"] = []
    auth.write_text(json.dumps(document))
    _assert_denied(_run(plan_path, *flags), because="destroy_identities")


@pytest.mark.parametrize(
    "field", ["id", "arn", "name", "role", "subnet_id", "security_group_id"]
)
@pytest.mark.parametrize("mutation", ["alter", "remove", "add"])
def test_authorization_cannot_misdescribe_destroyed_identity(tmp_path, field, mutation):
    from test_plan_safety import _authorize, _genuine_plan

    plan = _genuine_plan(creating=False)
    plan_path = _write(tmp_path, plan)
    flags = _authorize(tmp_path, plan)
    auth = Path(flags[flags.index("--authorize-destroy") + 1])
    document = json.loads(auth.read_text())
    identity = next(
        identity
        for identity in document["destroy_identities"].values()
        if field in identity
    )
    if mutation == "remove":
        del identity[field]
    elif mutation == "add":
        identity[field + "_unreviewed"] = "unrelated"
    else:
        identity[field] = "unrelated-production-resource"
    auth.write_text(json.dumps(document))
    _assert_denied(_run(plan_path, *flags), because="destroy_identities")


@pytest.mark.parametrize(
    "problem", ["changed-user", "changed-role", "missing-evidence"]
)
def test_owned_key_apply_revalidates_canonical_principal_before_terraform(
    tmp_path, problem
):
    command, _, auth, record = _apply_fixture(tmp_path)
    if problem == "missing-evidence":
        document = json.loads(auth.read_text())
        del document["provisioning_principal"]
        auth.write_text(json.dumps(document))
    else:
        principal = (
            f"arn:aws:iam::{ACCOUNT}:user/other-operator"
            if problem == "changed-user"
            else f"arn:aws:iam::{ACCOUNT}:role/other-provisioner"
        )
        (tmp_path / "caller-arn").write_text(principal)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "principal" in result.stderr
    assert not record.exists(), (
        "Terraform apply must not run under an unreviewed owned-key principal"
    )


@pytest.mark.parametrize(
    "apply_role,allowed", [("provisioner-a", True), ("provisioner-b", False)]
)
def test_owned_key_role_sessions_are_canonical_but_same_account_roles_are_distinct(
    tmp_path, apply_role, allowed
):
    command, _, _, record = _apply_fixture(
        tmp_path, principal=f"arn:aws:iam::{ACCOUNT}:role/provisioner-a"
    )
    (tmp_path / "caller-arn").write_text(
        f"arn:aws:sts::{ACCOUNT}:assumed-role/{apply_role}/new-session"
    )
    result = subprocess.run(command, capture_output=True, text=True)
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    assert record.exists() is allowed
    if not allowed:
        assert "principal" in result.stderr
