"""Credential lifecycle races with real transport checks and simulated API CAS."""

import base64
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.membership import SharedMembership
from superplane_bootstrap.target import VerifiedTarget

from workspace_provisioning.member_credentials import (
    CredentialBinding,
    MemberIssuer,
    SecretProjector,
    delegation_specs,
)

NOW = datetime(2026, 9, 25, tzinfo=UTC)
CA = base64.b64encode(b"pinned-ca").decode()


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class Resource:
    def __init__(self, api, kind):
        self.api, self.kind = api, kind

    def get(self, name, namespace=None):
        key = (self.kind, namespace, name)
        if key not in self.api.objects:
            raise ApiError(404)
        return deepcopy(self.api.objects[key])

    def patch(self, name, namespace, body, content_type):
        assert content_type == "application/json-patch+json"
        key = (self.kind, namespace, name)
        if self.api.before_patch:
            self.api.before_patch(self.api.objects[key])
        current = deepcopy(self.api.objects[key])
        for step in body:
            parts = [
                x.replace("~1", "/").replace("~0", "~")
                for x in step["path"].split("/")[1:]
            ]
            parent = current
            for part in parts[:-1]:
                parent = parent[part]
            leaf = parts[-1]
            if step["op"] == "test":
                if parent.get(leaf) != step["value"]:
                    raise ApiError(409)
            elif step["op"] == "remove":
                del parent[leaf]
            else:
                parent[leaf] = step["value"]
        current["metadata"]["resourceVersion"] = str(
            int(current["metadata"]["resourceVersion"]) + 1
        )
        self.api.objects[key] = current
        self.api.patches += 1

    def delete(self, name, namespace, body):
        key = (self.kind, namespace, name)
        current = self.api.objects[key]
        if self.api.before_delete:
            self.api.before_delete(current)
        if any(
            current["metadata"].get(k) != v for k, v in body["preconditions"].items()
        ):
            raise ApiError(409)
        del self.api.objects[key]


class API:
    def __init__(self, configuration):
        self.configuration = configuration
        self.client = self
        self.resources = self
        self.objects, self.calls = {}, []
        self.before_patch = self.before_delete = self.after_token = None
        self.patches = 0
        self.expiry = NOW + timedelta(seconds=900)

    def get(self, api_version, kind):
        return Resource(self, kind)

    def call_api(self, path, method, **kwargs):
        self.calls.append((path, method, kwargs))
        if self.after_token:
            self.after_token()
        return {
            "status": {
                "token": "private-token-material",
                "expirationTimestamp": self.expiry.isoformat(),
            }
        }


@pytest.fixture
def fixture(tmp_path):
    member = SharedMembership.create(
        org_id="11111111-1111-4111-8111-111111111111",
        workspace_id="22222222-2222-4222-8222-222222222222",
        cluster_id="33333333-3333-4333-8333-333333333333",
        request_id="44444444-4444-4444-8444-444444444444",
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example.test",
    )
    binding = CredentialBinding(member, "namespace-uid", 1, "reader")
    ca = tmp_path / "ca.pem"
    ca.write_bytes(base64.b64decode(CA))
    config = SimpleNamespace(
        host=member.endpoint,
        ssl_ca_cert=str(ca),
        verify_ssl=True,
        assert_hostname=None,
        tls_server_name=None,
        proxy=None,
    )
    api = API(config)
    target = VerifiedTarget(
        member.org_id,
        member.workspace_id,
        "123456789012",
        "us-east-1",
        "shared",
        member.cluster_arn,
        member.endpoint,
        CA,
        "arn:aws:iam::123456789012:role/issuer",
        "adopted",
    )
    grants = KubeGrants(api, target)
    for namespace, namespace_uid in (
        (member.namespace, binding.namespace_uid),
        ("control", "control-uid"),
    ):
        api.objects[("Namespace", None, namespace)] = {
            "metadata": {"name": namespace, "uid": namespace_uid},
            "status": {"phase": "Active"},
        }
    for spec in delegation_specs(binding):
        body = deepcopy(spec["body"])
        body["metadata"].update(uid="uid-" + body["kind"], resourceVersion="1")
        api.objects[(body["kind"], member.namespace, body["metadata"]["name"])] = body
    api.objects[("Secret", "control", "reader-credentials")] = {
        "metadata": {
            "name": "reader-credentials",
            "uid": "secret-uid",
            "resourceVersion": "1",
            "annotations": {"peer": "preserve"},
        },
        "data": {"peer.kubeconfig": "cGVlcg=="},
        "type": "Opaque",
    }
    authorized = []

    def authorize(binding, action):
        authorized.append((binding, action))

    issuer = MemberIssuer(
        grants, authorize, audience="https://kubernetes.default.svc", now=lambda: NOW
    )
    projector = SecretProjector(
        grants,
        authorize,
        namespace="control",
        namespace_uid="control-uid",
        secret_name="reader-credentials",
        secret_uid="secret-uid",
        scope="reader",
        now=lambda: NOW,
    )
    return SimpleNamespace(
        api=api,
        binding=binding,
        issuer=issuer,
        projector=projector,
        authorized=authorized,
    )


def issue(f):
    return f.issuer.issue(f.binding, service_account_uid="uid-ServiceAccount")


@pytest.mark.parametrize("marker", ["a" * 32, "invalid-marker"])
def test_journalled_creation_marker_does_not_change_credential_scope(fixture, marker):
    from superplane_bootstrap.component_journal import ANNOTATION

    f = fixture
    for spec in delegation_specs(f.binding):
        key = (
            spec["body"]["kind"],
            f.binding.membership.namespace,
            spec["body"]["metadata"]["name"],
        )
        f.api.objects[key]["metadata"]["annotations"][ANNOTATION] = marker
    if marker == "invalid-marker":
        with pytest.raises(BootstrapRefused, match="creation marker"):
            issue(f)
    else:
        assert issue(f).metadata["scope"] == "reader"


def test_issue_project_and_revoke_preserves_peers(fixture):
    f = fixture
    credential = issue(f)
    assert "private-token" not in repr(credential)
    config = json.loads(credential.kubeconfig(CA))
    assert config["extensions"][0]["extension"] == credential.metadata
    assert set(config["users"][0]["user"]) == {"token"}
    path, method, arguments = f.api.calls[0]
    assert (
        path.endswith("/" + f.binding.service_account + "/token") and method == "POST"
    )
    assert arguments["body"]["spec"]["expirationSeconds"] == 900
    assert arguments["auth_settings"] == ["BearerToken"]
    receipt = f.projector.publish(credential, certificate_authority_data=CA)
    assert f.projector.publish(credential, certificate_authority_data=CA) == receipt
    assert f.api.patches == 1
    f.projector.remove(f.binding, receipt)
    f.issuer.revoke(f.binding, service_account_uid="uid-ServiceAccount")
    f.issuer.revoke(f.binding, service_account_uid="uid-ServiceAccount")
    secret = f.api.objects[("Secret", "control", "reader-credentials")]
    assert secret["data"] == {"peer.kubeconfig": "cGVlcg=="}
    assert secret["metadata"]["annotations"] == {"peer": "preserve"}
    assert receipt.secret_uid == "secret-uid" and receipt.resource_version == "2"


@pytest.mark.parametrize(
    "replacement", ["namespace", "serviceaccount", "permissions", "transport"]
)
def test_issuance_rejects_identity_or_authority_drift(fixture, replacement):
    f = fixture
    if replacement == "namespace":
        f.api.objects[("Namespace", None, f.binding.membership.namespace)]["metadata"][
            "uid"
        ] = "replacement"
    elif replacement == "serviceaccount":
        f.api.objects[
            (
                "ServiceAccount",
                f.binding.membership.namespace,
                f.binding.service_account,
            )
        ]["metadata"]["uid"] = "replacement"
    elif replacement == "permissions":
        f.api.objects[
            ("Role", f.binding.membership.namespace, f.binding.service_account)
        ]["rules"][0]["resources"] = ["secrets"]
    else:
        f.api.configuration.host = "https://another.example.test"
    with pytest.raises(BootstrapRefused):
        issue(f)
    assert not f.api.calls


def test_replacement_during_token_request_is_not_returned(fixture):
    f = fixture

    def replace_sa():
        f.api.objects[
            (
                "ServiceAccount",
                f.binding.membership.namespace,
                f.binding.service_account,
            )
        ]["metadata"]["uid"] = "replacement"

    f.api.after_token = replace_sa
    with pytest.raises(BootstrapRefused):
        issue(f)


def test_excessive_provider_expiry_and_stale_authority_refused(fixture):
    f = fixture
    f.api.expiry += timedelta(seconds=1)
    with pytest.raises(BootstrapRefused, match="issuance refused"):
        issue(f)

    def revoked(*_):
        raise BootstrapRefused("current renewal authority revoked")

    f.issuer.authorize = revoked
    before = len(f.api.calls)
    with pytest.raises(BootstrapRefused, match="renewal authority revoked"):
        issue(f)
    assert len(f.api.calls) == before


def test_projection_conflict_preserves_concurrent_peer_change(fixture):
    f = fixture
    credential = issue(f)

    def concurrent(body):
        body["data"]["peer.kubeconfig"] = "bmV3LXBlZXI="
        body["metadata"]["resourceVersion"] = "2"

    f.api.before_patch = concurrent
    with pytest.raises(BootstrapRefused, match="CAS failed"):
        f.projector.publish(credential, certificate_authority_data=CA)
    assert f.api.objects[("Secret", "control", "reader-credentials")]["data"] == {
        "peer.kubeconfig": "bmV3LXBlZXI="
    }


def test_rotation_requires_previous_receipt_and_old_cleanup_cannot_remove_new(fixture):
    f = fixture
    old = issue(f)
    receipt = f.projector.publish(old, certificate_authority_data=CA)
    new = replace(
        old,
        binding=replace(f.binding, revision=2),
        service_account_uid="new-sa-uid",
        _token="new-private-token",
    )
    with pytest.raises(BootstrapRefused, match="another revision"):
        f.projector.publish(new, certificate_authority_data=CA)
    newer = f.projector.publish(new, certificate_authority_data=CA, previous=receipt)
    with pytest.raises(BootstrapRefused, match="another credential projection"):
        f.projector.remove(f.binding, receipt)
    assert newer != receipt
    f.projector.remove(new.binding, newer)


def test_revoke_uid_conflict_cannot_delete_replacement(fixture):
    f = fixture

    def concurrent(body):
        body["metadata"]["uid"] = "replacement"

    f.api.before_delete = concurrent
    with pytest.raises(ApiError):
        f.issuer.revoke(f.binding, service_account_uid="uid-ServiceAccount")
    assert (
        f.api.objects[
            (
                "ServiceAccount",
                f.binding.membership.namespace,
                f.binding.service_account,
            )
        ]["metadata"]["uid"]
        == "replacement"
    )


def test_delegations_are_namespaced_and_scope_separated(fixture):
    f = fixture
    reader, mutator = f.binding, replace(f.binding, scope="mutator")
    assert reader.service_account != mutator.service_account
    for binding in (reader, mutator):
        specs = delegation_specs(binding)
        assert {s["body"]["kind"] for s in specs} == {
            "ServiceAccount",
            "Role",
            "RoleBinding",
        }
        for spec in specs:
            assert spec["body"]["metadata"]["namespace"] == binding.membership.namespace
            for rule in spec["body"].get("rules", []):
                assert not set(rule["resources"]) & {
                    "*",
                    "secrets",
                    "serviceaccounts/token",
                    "nodepools",
                    "nodes",
                    "roles",
                    "rolebindings",
                }
                if binding.scope == "reader":
                    assert set(rule["verbs"]) <= {"get", "list", "watch"}

        probe_actions = {
            (resource, verb)
            for rule in specs[1]["body"]["rules"]
            for resource in rule["resources"]
            for verb in rule["verbs"]
            if resource in {"services", "pods/log", "services/proxy", "pods/exec"}
        }
        assert probe_actions == (
            {("services", "get"), ("pods/log", "get")}
            if binding.scope == "mutator"
            else set()
        )


def test_projection_refuses_ca_scope_and_secret_identity_substitution(fixture):
    f = fixture
    credential = issue(f)
    with pytest.raises(BootstrapRefused, match="pinned cluster CA"):
        f.projector.publish(
            credential,
            certificate_authority_data=base64.b64encode(b"different-ca").decode(),
        )
    with pytest.raises(BootstrapRefused, match="scope differs"):
        f.projector.publish(
            replace(credential, binding=replace(f.binding, scope="mutator")),
            certificate_authority_data=CA,
        )
    f.api.objects[("Secret", "control", "reader-credentials")]["metadata"]["uid"] = (
        "replacement"
    )
    with pytest.raises(BootstrapRefused, match="Secret identity changed"):
        f.projector.publish(credential, certificate_authority_data=CA)
    assert f.api.patches == 0


def test_projection_refuses_unowned_existing_workspace_key(fixture):
    f = fixture
    credential = issue(f)
    f.api.objects[("Secret", "control", "reader-credentials")]["data"][
        f.binding.membership.workspace_id + ".kubeconfig"
    ] = "cGVlcg=="
    with pytest.raises(BootstrapRefused, match="another revision or owner"):
        f.projector.publish(credential, certificate_authority_data=CA)
    assert f.api.patches == 0


def test_authorization_revoked_immediately_before_patch_prevents_effect(fixture):
    f = fixture
    credential = issue(f)
    original = f.projector._patch

    def expired(*_):
        raise BootstrapRefused("projection authority expired")

    def lose_authority(*args):
        f.projector.authorize = expired
        return original(*args)

    f.projector._patch = lose_authority
    with pytest.raises(BootstrapRefused, match="authority expired"):
        f.projector.publish(credential, certificate_authority_data=CA)
    assert f.api.patches == 0
