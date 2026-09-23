"""Provider-contract failures cannot turn aliases or failed reads into ownership."""

import base64
from copy import deepcopy
from types import SimpleNamespace

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION, KubeGrants


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class Resource:
    def __init__(self):
        self.body = None
        self.error = None
        self.before_delete = None
        self.deletes = []

    def get(self, **args):
        if self.error:
            raise ApiError(self.error)
        if self.body is None:
            raise ApiError(404)
        assert args["name"] == self.body["metadata"]["name"]
        return deepcopy(self.body)

    def create(self, body):
        if self.body:
            raise ApiError(409)
        self.body = deepcopy(body)
        self.body["metadata"].update(uid="original-uid", resourceVersion="1")
        return deepcopy(self.body)

    def delete(self, *, name, body):
        if self.before_delete:
            self.before_delete(self.body)
        # Model the API's atomic UID and resourceVersion precondition, rather
        # than assuming name-based deletion is safe after a separate read.
        pre = body["preconditions"]
        if any(self.body["metadata"][key] != value for key, value in pre.items()):
            raise ApiError(409)
        self.deletes.append((name, deepcopy(body)))
        self.body = None


@pytest.fixture
def grants(tmp_path, binding, provider_identity, observed_cluster, expected_target):
    from superplane_bootstrap.target import verify_target

    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    config = SimpleNamespace(
        host=target.endpoint,
        verify_ssl=True,
        assert_hostname=None,
        tls_server_name=None,
        proxy=None,
    )
    ca = tmp_path / "ca.pem"
    ca.write_bytes(base64.b64decode(target.certificate_authority_data))
    config.ssl_ca_cert = str(ca)
    resource = Resource()

    def resolve(**args):
        assert args == {
            "api_version": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
        }
        return resource

    client = SimpleNamespace(
        client=SimpleNamespace(configuration=config),
        resources=SimpleNamespace(get=resolve),
    )
    spec = {
        "key": "worker-role",
        "cluster_arn": target.cluster_arn,
        "generation": "a" * 64,
        "body": {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": "bootstrap-unique-generation",
                "annotations": {GENERATION_ANNOTATION: "a" * 64},
            },
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["namespaces"],
                    "verbs": ["get"],
                    "resourceNames": ["selected-workspace"],
                }
            ],
        },
    }
    return KubeGrants(client, target), resource, spec


def test_owned_grant_create_and_uid_precondition_delete(grants):
    backend, api, spec = grants
    identity = backend.create(spec)
    backend.delete(spec, identity)
    assert backend.observe(spec) is None
    assert api.deletes[0][1]["preconditions"] == {
        "uid": "original-uid",
        "resourceVersion": "1",
    }


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_unanswered_read_is_not_absence(grants, status):
    backend, api, spec = grants
    api.error = status
    with pytest.raises(ApiError):
        backend.observe(spec)


def test_existing_grant_is_not_adopted_by_create(grants):
    backend, api, spec = grants
    api.body = deepcopy(spec["body"])
    with pytest.raises(ApiError) as exc:
        backend.create(spec)
    assert exc.value.status == 409
    assert not api.deletes


@pytest.mark.parametrize("changed", ["uid", "resourceVersion"])
def test_replacement_or_update_between_read_and_delete_fails_atomically(
    grants, changed
):
    backend, api, spec = grants
    identity = backend.create(spec)
    api.before_delete = lambda body: body["metadata"].update({changed: "successor"})
    with pytest.raises(ApiError) as exc:
        backend.delete(spec, identity)
    assert exc.value.status == 409
    assert api.body is not None
    assert not api.deletes


def test_same_name_with_different_generation_is_not_owned(grants):
    backend, api, spec = grants
    backend.create(spec)
    api.body["metadata"]["annotations"][GENERATION_ANNOTATION] = "different"
    with pytest.raises(BootstrapRefused, match="different ownership"):
        backend.verify(spec, backend.observe(spec))


def test_added_privilege_is_not_the_pinned_grant(grants):
    backend, api, spec = grants
    backend.create(spec)
    api.body["rules"].append({"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]})
    with pytest.raises(BootstrapRefused, match="different ownership or permissions"):
        backend.verify(spec, backend.observe(spec))


def test_unlabelled_journal_digest_is_backward_compatible(grants):
    import hashlib
    import json
    from superplane_bootstrap.kube_grants import _digest

    _, _, spec = grants
    expected = hashlib.sha256(
        json.dumps(
            {"rules": spec["body"]["rules"]}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert _digest(spec["body"]) == expected


def test_aggregation_label_changes_pinned_authority(grants):
    backend, api, spec = grants
    backend.create(spec)
    api.body["metadata"]["labels"] = {
        "rbac.authorization.k8s.io/aggregate-to-admin": "true"
    }
    with pytest.raises(BootstrapRefused, match="different ownership or permissions"):
        backend.verify(spec, backend.observe(spec))


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "https://other.invalid"),
        ("verify_ssl", False),
        ("assert_hostname", False),
        ("tls_server_name", "other.invalid"),
        ("proxy", "https://other.invalid"),
        ("ssl_ca_cert", "/missing/ca.pem"),
    ],
)
def test_retargeted_transport_refuses_before_provider_io(grants, field, value):
    backend, api, spec = grants
    setattr(backend.client.client.configuration, field, value)
    with pytest.raises(BootstrapRefused, match="transport"):
        backend.create(spec)
    assert api.body is None


def test_replaced_ca_file_refuses_before_provider_io(grants):
    from pathlib import Path

    backend, api, spec = grants
    Path(backend.client.client.configuration.ssl_ca_cert).write_bytes(b"other CA")
    with pytest.raises(BootstrapRefused, match="transport"):
        backend.create(spec)
    assert api.body is None


def test_other_cluster_grant_refuses_before_provider_io(grants):
    backend, api, spec = grants
    with pytest.raises(BootstrapRefused, match="different cluster"):
        backend.create({**spec, "cluster_arn": "arn:other"})
    assert api.body is None
