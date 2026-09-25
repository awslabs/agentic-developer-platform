"""Privileged renewal lives in a separate process with separate installer DB rights."""

import json
from uuid import uuid4

import pytest

from installation.config import Refusal
from installation.credential_controller import documents, registration_job, validate
from workspace_provisioning.tests.test_credential_authority import authority_document

from .test_execution_isolation import configured


def installation(environment, release):
    env, lock = configured(environment, release)
    document = authority_document(
        org_id=env["org_id"],
        cluster_id=env["cluster_id"],
        account=env["account_id"],
        management_cluster=env["cluster"],
    )
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
