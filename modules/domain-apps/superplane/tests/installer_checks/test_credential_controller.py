"""Privileged renewal lives in a separate process with separate installer DB rights."""

import json
from uuid import uuid4

import pytest

from installation.config import MODULE, Refusal
from installation.credential_controller import documents, registration_job, validate

from .test_execution_isolation import configured


def installation(environment, release):
    env, lock = configured(environment, release)
    # Declared installer input, not an import of another suite's test module.
    account, region = env["account_id"], env["region"]

    def target(name):
        return {
            "account_id": account,
            "region": region,
            "cluster_name": name,
            "cluster_arn": f"arn:aws:eks:{region}:{account}:cluster/{name}",
            "endpoint": f"https://{name}.example.test",
            "certificate_authority_data": "cHVibGljLWNh",
        }

    def actor(name, cluster):
        return {
            "role_arn": f"arn:aws:iam::{account}:role/{name}",
            "role_id": "AROA" + name,
            "access_entry_arn": f"arn:aws:eks:{region}:{account}:access-entry/{cluster}/role/{account}/{name}/immutable",
            "username": "superplane:" + name,
            "group": "superplane:" + name,
        }

    document = {
        "version": 1,
        "org_id": env["org_id"],
        "cluster_id": env["cluster_id"],
        "controller_role_arn": f"arn:aws:iam::{account}:role/credential-controller",
        "controller_role_id": "AROAcontroller",
        "target": target("shared"),
        "management_target": target(env["cluster"]),
        "issuer": {
            **actor("issuer", "shared"),
            "policy_uid": "policy-uid",
            "binding_uid": "binding-uid",
        },
        "projector": actor("projector", env["cluster"]),
        "projection": {
            "namespace": env["namespace"],
            "namespace_uid": "cp-ns-uid",
            "reader_secret": "superplane-workspace-access",
            "reader_secret_uid": "reader-uid",
            "mutator_secret": env["execution"]["workspace_credentials_secret"],
            "mutator_secret_uid": "mutator-uid",
        },
        "audience": "https://kubernetes.default.svc",
    }
    env["credential_controller"] = {
        "role_arn": document["controller_role_arn"],
        "database_secret": "credential-renewal-database",
        "registration_database_secret": "credential-registration-database",
        "authorities": [{"authority_id": str(uuid4()), "document": document}],
    }
    return env, lock


def test_distinct_installer_and_runtime_credential_authority(environment, release):
    env, lock = installation(environment, release)
    validate(env, lock)
    rendered = documents(env, lock)
    deployment = rendered[-1]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "superplane-credential-controller"
    assert len(pod["containers"]) == 1
    assert (
        pod["containers"][0]["command"][-1]
        == "workspace_provisioning.credential_controller"
    )
    assert pod["volumes"][1]["secret"]["secretName"] == "credential-renewal-database"
    assert "credential-registration-database" not in json.dumps(deployment)
    assert "readinessProbe" in pod["containers"][0]
    job = registration_job(env, lock, str(uuid4()))
    installer = job["spec"]["template"]["spec"]
    assert installer["containers"][0]["args"] == ["--register"]
    assert (
        installer["volumes"][1]["secret"]["secretName"]
        == "credential-registration-database"
    )
    assert "readinessProbe" not in installer["containers"][0]
    assert rendered[1]["immutable"] is True
    assert not any(
        d["kind"] in {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}
        for d in rendered
    )


@pytest.mark.parametrize(
    "changed", ["database", "reader", "mutator", "org", "role", "unbuilt"]
)
def test_controller_installation_cannot_alias_authority_or_consumer_mounts(
    environment, release, changed
):
    env, lock = installation(environment, release)
    config = env["credential_controller"]
    doc = config["authorities"][0]["document"]
    if changed == "database":
        config["database_secret"] = config["registration_database_secret"]
    elif changed == "reader":
        doc["projection"]["reader_secret"] = "unmounted-reader"
    elif changed == "mutator":
        doc["projection"]["mutator_secret"] = "unmounted-mutator"
    elif changed == "org":
        doc["org_id"] = str(uuid4())
    elif changed == "role":
        config["role_arn"] = "arn:aws:iam::879318057152:role/unregistered"
    else:
        lock["images"].pop("superplane-executor")
    with pytest.raises(Refusal):
        validate(env, lock)


def test_source_installer_loads_runtime_schema_without_sibling_pythonpath(
    environment, release
):
    import subprocess
    import sys

    env, lock = installation(environment, release)
    program = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
original = list(sys.path)
from installation.credential_controller import validate
value = json.load(sys.stdin)
validate(value['environment'], value['release'])
assert sys.path == original
for name, relative in [('superplane_bootstrap', 'workspace_bootstrap/superplane_bootstrap'),
                       ('superplane_contracts', 'contracts/superplane_contracts'),
                       ('workspace_provisioning', 'workspace_provisioning')]:
    module = sys.modules.get(name)
    if module is not None:
        assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[1]) / relative)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program, str(MODULE)],
        input=json.dumps({"environment": env, "release": lock}),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_validation_refuses_mixed_checkout_and_restores_paths(
    environment, release, monkeypatch
):
    import sys
    from types import SimpleNamespace

    env, lock = installation(environment, release)
    before = list(sys.path)
    monkeypatch.setitem(
        sys.modules,
        "superplane_bootstrap",
        SimpleNamespace(__file__="/foreign/checkout/__init__.py"),
    )
    with pytest.raises(Refusal, match="mix source checkouts"):
        validate(env, lock)
    assert sys.path == before
