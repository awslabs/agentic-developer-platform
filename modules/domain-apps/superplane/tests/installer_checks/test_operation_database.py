import json

import pytest
import yaml

from installation.config import Refusal, digest
from installation.operation_database import (
    COMPONENT,
    OperationInstaller,
    execute,
    main,
    preparation_plan,
)


@pytest.fixture
def inputs(environment, release):
    environment["image_execution"] = "cluster"
    environment["control_plane_only"] = True
    environment["secrets"].pop("workspace_access")
    release["images"][COMPONENT] = "sha256:" + "9" * 64
    release["image_sources"][COMPONENT] = {
        "registry": f"{environment['account_id']}.dkr.ecr.us-east-1.amazonaws.com",
        "repository": "adp-" + COMPONENT,
        "source_revision": release["source_revision"],
    }
    return environment, release


@pytest.mark.parametrize(
    "schema", ["public", "superplane", "skypilot", "pg_catalog", "x;drop table y"]
)
def test_shared_schema_cannot_overlap_domain_or_system(inputs, tmp_path, schema):
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    with pytest.raises(Refusal, match="distinct dedicated"):
        preparation_plan(installer, schema)


def test_approval_refused_before_any_live_action(inputs, tmp_path, monkeypatch):
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    plan = preparation_plan(installer, "superplane_operations")
    monkeypatch.setattr(
        installer, "target", lambda: pytest.fail("live action before approval")
    )
    with pytest.raises(Refusal, match="exact saved"):
        execute(installer, plan, "0" * 64, "private-admin-url")
    with pytest.raises(Refusal, match="SUPERPLANE_DATABASE_ADMIN_URL"):
        execute(installer, plan, digest(plan), "")


def test_offline_plan_replay_requires_same_mode_identity_and_schema(
    inputs, tmp_path, capsys
):
    environment, release = inputs
    env_path, lock_path = tmp_path / "environment.yaml", tmp_path / "release.yaml"
    env_path.write_text(yaml.safe_dump(environment))
    lock_path.write_text(yaml.safe_dump(release))
    output = tmp_path / "plan"
    args = [
        "--environment",
        str(env_path),
        "--release-lock",
        str(lock_path),
        "--output",
        str(output),
        "--schema",
        "superplane_operations",
    ]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    receipt = json.loads((output / "receipt.json").read_text())
    assert result["plan_sha256"] == digest(receipt["operation_database_plan"])
    assert (output / "receipt.json").stat().st_mode & 0o777 == 0o600
    assert main(args) == 2  # Same path never silently replaces a receipt.
    assert main([*args, "--resume"]) == 0
    assert main([*args[:-1], "other_operations", "--resume"]) == 2
    receipt["mode"] = "control-plane-only"
    (output / "receipt.json").write_text(json.dumps(receipt))
    assert main([*args, "--resume"]) == 2


def test_missing_or_unreviewed_paid_image_refuses(inputs, tmp_path):
    inputs[1]["image_sources"][COMPONENT]["source_revision"] = "b" * 40
    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    with pytest.raises(Refusal, match="reviewed paid-worker"):
        preparation_plan(installer, "superplane_operations")


def test_runtime_secret_projection_replay_and_conflict(inputs, tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace
    from installation.config import LABEL
    from installation.operation_database import project_runtime_secrets

    installer = OperationInstaller(*inputs, tmp_path, control_plane_only=True)
    plan = preparation_plan(installer, "superplane_operations")
    existing = {}
    created = []
    verified = []
    monkeypatch.setattr(
        installer, "verify_deployment_identity", lambda: verified.append(True)
    )

    def kube(*args, data=None):
        if args[0] == "create":
            value = json.loads(data)
            name = value["metadata"]["name"]
            assert name not in existing
            created.append(value)
            existing[name] = {
                **value,
                "data": {
                    k: base64.b64encode(v.encode()).decode()
                    for k, v in value.pop("stringData").items()
                },
            }
            return SimpleNamespace(stdout="", returncode=0)
        assert args[:4] == ("-n", installer.env["namespace"], "get", "secret")
        return SimpleNamespace(
            stdout=json.dumps(existing[args[4]]) if args[4] in existing else "",
            returncode=0,
        )

    monkeypatch.setattr(installer, "kube", kube)
    dsns = {
        plan["roles"]["gateway"]: "gateway-shared",
        plan["roles"]["worker"]: "worker-shared",
    }

    def project():
        project_runtime_secrets(
            installer, plan, domain_dsn="domain", operation_dsns=dsns, ca_pem="ca"
        )

    project()
    api = existing["superplane-operation-api-db"]
    assert api["data"] == {"dsn": base64.b64encode(b"gateway-shared").decode()}
    assert len(created) == 2 and len(verified) == 2
    project()
    assert len(created) == 2 and len(verified) == 4
    for conflict in ("dsn", "owner", "extra", "type", "malformed"):
        pristine = json.loads(json.dumps(api))
        if conflict == "dsn":
            api["data"]["dsn"] = base64.b64encode(b"worker-shared").decode()
        elif conflict == "owner":
            api["metadata"]["labels"][LABEL] = "other-owner"
        elif conflict == "extra":
            api["data"]["extra"] = base64.b64encode(b"x").decode()
        elif conflict == "type":
            api["type"] = "kubernetes.io/basic-auth"
        else:
            api["data"]["dsn"] = "!!!"
        with pytest.raises(Refusal, match="replacement refused"):
            project()
        assert len(created) == 2
        api.clear()
        api.update(pristine)
