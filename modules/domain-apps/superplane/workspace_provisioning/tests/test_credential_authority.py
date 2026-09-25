"""Installed authority rejects target, tenant, principal and projection substitution."""

import base64
from copy import deepcopy
import json
from uuid import uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.credential_controller.registry import Authority


def authority_document(
    *,
    org_id=None,
    cluster_id=None,
    account="123456789012",
    management_cluster="management",
):
    def target(name):
        return {
            "account_id": account,
            "region": "us-east-1",
            "cluster_name": name,
            "cluster_arn": f"arn:aws:eks:us-east-1:{account}:cluster/{name}",
            "endpoint": f"https://{name}.example.test",
            "certificate_authority_data": base64.b64encode(b"public-ca").decode(),
        }

    def actor(name, cluster):
        return {
            "role_arn": f"arn:aws:iam::{account}:role/{name}",
            "role_id": "AROA" + name,
            "access_entry_arn": f"arn:aws:eks:us-east-1:{account}:access-entry/{cluster}/role/{account}/{name}/immutable",
            "username": "superplane:" + name,
            "group": "superplane:" + name,
        }

    return {
        "version": 1,
        "org_id": org_id or str(uuid4()),
        "cluster_id": cluster_id or str(uuid4()),
        "controller_role_arn": f"arn:aws:iam::{account}:role/credential-controller",
        "controller_role_id": "AROAcontroller",
        "target": target("shared"),
        "management_target": target(management_cluster),
        "issuer": {
            **actor("issuer", "shared"),
            "policy_uid": "policy-uid",
            "binding_uid": "binding-uid",
        },
        "projector": actor("projector", management_cluster),
        "projection": {
            "namespace": "superplane",
            "namespace_uid": "cp-ns-uid",
            "reader_secret": "superplane-workspace-access",
            "reader_secret_uid": "reader-uid",
            "mutator_secret": "selected-workspace-execution",
            "mutator_secret_uid": "mutator-uid",
        },
        "audience": "https://kubernetes.default.svc",
    }


def test_installed_authority_separates_target_and_projection_without_tokens():
    doc = authority_document()
    authority = Authority.read(str(uuid4()), json.dumps(doc))
    workspace = str(uuid4())
    assert authority.target(workspace).cluster_arn == doc["target"]["cluster_arn"]
    assert (
        authority.target(workspace, management=True).cluster_arn
        == doc["management_target"]["cluster_arn"]
    )
    assert (
        authority.cluster_reference().access_entry_arn
        == doc["issuer"]["access_entry_arn"]
    )
    assert (
        authority.projection("reader")["secret_uid"]
        != authority.projection("mutator")["secret_uid"]
    )
    assert "token" not in authority.document_json


@pytest.mark.parametrize(
    "changed", ["tenant", "ca", "endpoint", "role", "entry", "scope", "field"]
)
def test_installed_authority_refuses_unbound_references(changed):
    doc = deepcopy(authority_document())
    if changed == "tenant":
        doc["org_id"] = "unbound-organization"
    elif changed == "ca":
        doc["target"]["certificate_authority_data"] = "bad"
    elif changed == "endpoint":
        doc["target"]["endpoint"] = "https://user:secret@foreign.test"
    elif changed == "role":
        doc["issuer"]["role_arn"] = "arn:aws:iam::000000000000:role/issuer"
    elif changed == "entry":
        doc["issuer"]["access_entry_arn"] = doc["projector"]["access_entry_arn"]
    elif changed == "scope":
        doc["projection"]["mutator_secret"] = doc["projection"]["reader_secret"]
    else:
        doc["bootstrap_run_credential"] = "must-not-be-retained"
    with pytest.raises(BootstrapRefused):
        Authority.read(str(uuid4()), json.dumps(doc))
