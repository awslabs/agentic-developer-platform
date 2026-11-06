"""Dedicated issuance is isolated from manager, worker and installer authority."""

import base64
import json
from types import SimpleNamespace

import pytest

from installation.config import Refusal, identity
from installation.dedicated_readers import documents, load_material, validate


def configured(env):
    env["control_plane_only"] = True
    owner = identity(env)
    account, region = env["account_id"], env["region"]
    role_name = f"adp-{env['environment']}-spr-{owner}"
    env["dedicated_readers"] = {
        "role_arn": f"arn:aws:iam::{account}:role/{role_name}",
        "role_id": "AROA" + "A" * 17,
        "auth_secret": f"adp/{env['environment']}/superplane/dedicated-reader-auth",
        "projection": {
            "namespace": env["namespace"],
            "secret_name": "superplane-workspace-access",
            "secret_uid": "projection-uid",
            "endpoint": "https://management.example",
        },
        "workspaces": [
            {
                "workspace_id": env["workspace_id"],
                "org_id": env["org_id"],
                "namespace": "superplane-system",
                "cluster_name": "workspace",
                "cluster_arn": f"arn:aws:eks:{region}:{account}:cluster/workspace",
                "endpoint": "https://workspace.example",
                "certificate_authority_data": base64.b64encode(
                    b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----"
                ).decode(),
                "access_entry_arn": f"arn:aws:eks:{region}:{account}:access-entry/workspace/role/{account}/{role_name}/entry",
                "audience": "https://kubernetes.default.svc",
            }
        ],
    }
    return env["dedicated_readers"]


def test_issuer_has_only_its_own_identity_and_one_projection_secret(
    environment, release
):
    configured(environment)
    validate(environment, release)
    docs = documents(environment, release)
    role = next(d for d in docs if d["kind"] == "Role")
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get", "update"],
            "resourceNames": ["superplane-workspace-access"],
        }
    ]
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "superplane-dedicated-readers"
    assert not pod["automountServiceAccountToken"]
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert "readinessProbe" in pod["containers"][0]
    rendered = json.dumps(pod)
    assert (
        "database" not in rendered
        and "paid-worker" not in rendered
        and "superplane-observation" not in rendered
    )


@pytest.mark.parametrize(
    "change", ["full", "role", "secret", "management", "duplicate"]
)
def test_unreviewed_scope_or_competing_projection_refuses(environment, release, change):
    config = configured(environment)
    if change == "full":
        environment["control_plane_only"] = False
    elif change == "role":
        config["role_arn"] += "-different"
    elif change == "secret":
        config["projection"]["secret_name"] = "superplane-db"
    elif change == "management":
        config["workspaces"][0]["endpoint"] = config["projection"]["endpoint"]
    else:
        config["workspaces"].append(dict(config["workspaces"][0]))
    with pytest.raises(Refusal):
        validate(environment, release)


def test_reader_auth_composes_after_recovery_without_replacing_existing_credentials(
    environment,
):
    config = configured(environment)
    owner = identity(environment)
    grants = [
        {
            "submitter_id": name,
            "credential": name + "-credential",
            "signing_key": name + "-key",
            "workspaces": [],
            "lease_scopes": [name + "/scope"],
        }
        for name in ("controller", "monitor", "recovery")
    ]
    material = {"credential": "Bearer " + "r" * 50, "signing_key": "s" * 50}

    def aws(service, method, *args):
        if method == "describe-secret":
            return {"Tags": [{"Key": "SuperplaneInstallation", "Value": owner}]}
        if method == "get-secret-value":
            return {
                "SecretString": json.dumps(material),
                "VersionId": "reader-source-version",
            }
        if method == "describe-cluster":
            return {"cluster": {"endpoint": config["projection"]["endpoint"]}}
        assert method == "get-role"
        return {"Role": {"Arn": config["role_arn"], "RoleId": config["role_id"]}}

    installer = SimpleNamespace(
        env=environment,
        owner=owner,
        receipt={},
        secret_versions={},
        secret_values={"observation": {"submitters": json.dumps(grants)}},
        aws=aws,
        json=lambda x: x,
        existing=lambda _: {
            "metadata": {
                "uid": "projection-uid",
                "labels": {"adp.aws-e.io/installation": owner},
            }
        },
    )
    load_material(installer)
    actual = json.loads(installer.secret_values["observation"]["submitters"])
    assert actual[:3] == grants
    assert actual[3]["workspaces"] == []
    assert actual[3]["lease_scopes"] == [
        f"workspace_reader/{environment['org_id']}/{environment['workspace_id']}"
    ]
    assert installer.secret_values["dedicated_reader_auth"] == {
        "credential": material["credential"]
    }
