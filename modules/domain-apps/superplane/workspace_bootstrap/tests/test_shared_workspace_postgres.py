"""Real shared bootstrap journals with stateful provider transport fixtures.

Credential issuer/projector callbacks are external-service fixtures here; their
actual TokenRequest/Secret transport has separate member_credentials tests.
"""
# ruff: noqa: F811

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from superplane_bootstrap.access import ObservedNamespace, ProviderIdentity
from superplane_bootstrap.authority_backend import BootstrapClients
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.grant_plan import BootstrapRelease
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION
from superplane_bootstrap.management_observation import ManagementObservation
from superplane_bootstrap.membership import SharedMembership
from superplane_bootstrap.namespace_admission import (
    ClusterAuthorityReference,
    GATE_LABEL,
    policy_documents,
)
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_bootstrap.registration import reserve_registration
from superplane_bootstrap.shared_authority import (
    SharedBootstrapAuthorityFactory,
    SharedBootstrapServices,
)
from superplane_bootstrap.target import verify_target
from superplane_bootstrap.workspace import recover_interrupted_bootstrap

from .conftest import FakeClusterAccess, FakeStateStore, WORKSPACE_CRDS, ENFORCE_VERSION
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401
from .test_workspace import _run


class ApiError(Exception):
    def __init__(self, status):
        self.status = status


class ProviderResources:
    def __init__(self, access):
        self.access, self.objects, self.effects = access, {}, []
        self.counter = 0
        self.fail_open_reply = False

    def get(self, *, api_version, kind):
        provider = self

        class Resource:
            def get(self, *, name, namespace=None):
                try:
                    return deepcopy(provider.objects[(kind, namespace, name)])
                except KeyError:
                    raise ApiError(404) from None

            def create(self, *, body, namespace=None):
                name = body["metadata"]["name"]
                key = kind, namespace, name
                if key in provider.objects:
                    raise ApiError(409)
                provider.counter += 1
                result = deepcopy(body)
                result["metadata"].update(
                    uid="uid-" + str(provider.counter), resourceVersion="1"
                )
                provider.objects[key] = result
                provider.effects.append(("create", key))
                provider.sync(result)
                return deepcopy(result)

            def patch(self, *, name, body, content_type):
                assert (
                    kind == "Namespace"
                    and content_type == "application/json-patch+json"
                )
                key = kind, None, name
                result = provider.objects[key]
                assert body[:2] == [
                    {
                        "op": "test",
                        "path": "/metadata/uid",
                        "value": result["metadata"]["uid"],
                    },
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": result["metadata"]["resourceVersion"],
                    },
                ]
                assert body[2]["path"] == "/metadata/labels/" + GATE_LABEL.replace(
                    "/", "~1"
                )
                result["metadata"]["labels"][GATE_LABEL] = body[2]["value"]
                result["metadata"]["resourceVersion"] = str(
                    int(result["metadata"]["resourceVersion"]) + 1
                )
                provider.effects.append(("patch", key))
                provider.sync(result)
                if provider.fail_open_reply and body[2]["value"] == "open":
                    provider.fail_open_reply = False
                    raise OSError("lost provider acknowledgement")
                return deepcopy(result)

            def delete(self, *, name, namespace=None, body):
                key = kind, namespace, name
                result = provider.objects[key]
                assert body["preconditions"] == {
                    "uid": result["metadata"]["uid"],
                    "resourceVersion": result["metadata"]["resourceVersion"],
                }
                provider.effects.append(("delete", key))
                del provider.objects[key]

        return Resource()

    def sync(self, body):
        if body["kind"] == "Namespace":
            meta = body["metadata"]
            self.access.namespaces[meta["name"]] = ObservedNamespace(
                meta["name"], meta["uid"], deepcopy(meta["labels"])
            )


@pytest.fixture
def shared_runtime(
    database, tmp_path, binding, provider_identity, observed_cluster, expected_target
):
    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adopted",
        **expected_target,
    )
    membership = SharedMembership.create(
        org_id=target.org_id,
        workspace_id=target.workspace_id,
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn=target.cluster_arn,
        endpoint=target.endpoint,
    )
    store = SqlRegistrationStore(database())
    values = {
        "cluster": membership.cluster_id,
        "org": target.org_id,
        "workspace": target.workspace_id,
        "arn": target.cluster_arn,
        "endpoint": target.endpoint,
        "namespace": membership.namespace,
        "generation": membership.generation,
        "request": membership.request_id,
        "member": str(uuid4()),
    }
    with store.store.transaction():
        store.store.execute(
            "INSERT INTO clusters(id,org_id,name,status,sharing_enabled,eks_cluster_arn,endpoint) "
            "VALUES(CAST(:cluster AS uuid),CAST(:org AS uuid),'shared','Ready',true,:arn,:endpoint)",
            values,
        )
        store.store.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default,cluster_id,shared_cluster_id,namespace_name) "
            "VALUES(CAST(:workspace AS uuid),CAST(:org AS uuid),'member','namespace','Provisioning',false,CAST(:cluster AS uuid),CAST(:cluster AS uuid),:namespace)",
            values,
        )
        store.store.execute(
            "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,state,operation_id) "
            "VALUES(CAST(:member AS uuid),CAST(:org AS uuid),CAST(:workspace AS uuid),CAST(:cluster AS uuid),:generation,:namespace,'reserved',CAST(:request AS uuid))",
            values,
        )
    access = FakeClusterAccess()
    resources = ProviderResources(access)
    ca = tmp_path / "shared-ca.pem"
    ca.write_bytes(base64.b64decode(target.certificate_authority_data))
    dynamic = SimpleNamespace(
        resources=resources,
        client=SimpleNamespace(
            configuration=SimpleNamespace(
                host=target.endpoint,
                ssl_ca_cert=str(ca),
                verify_ssl=True,
                assert_hostname=None,
                tls_server_name=None,
                proxy=None,
            )
        ),
    )
    reference = ClusterAuthorityReference(
        target.org_id,
        target.cluster_arn,
        f"arn:aws:iam::{target.account_id}:role/cluster-issuer",
        target.cluster_arn.replace(":cluster/", ":access-entry/") + "/issuer/immutable",
        "cluster-issuer",
        "superplane:cluster-issuer",
        "policy-uid",
        "binding-uid",
    )
    cluster = {
        "name": target.cluster_name,
        "arn": target.cluster_arn,
        "endpoint": target.endpoint,
        "certificateAuthority": {"data": target.certificate_authority_data},
        "status": "ACTIVE",
        "accessConfig": {"authenticationMode": "API"},
    }
    entry = {
        "principalArn": reference.principal_arn,
        "accessEntryArn": reference.access_entry_arn,
        "username": reference.username,
        "kubernetesGroups": [reference.group],
        "type": "STANDARD",
    }
    eks = SimpleNamespace(
        meta=SimpleNamespace(region_name=target.region),
        describe_cluster=lambda **_: {"cluster": cluster},
        describe_access_entry=lambda **_: {"accessEntry": entry},
    )
    clients = BootstrapClients(
        binding,
        target,
        {"registrar": reference.principal_arn},
        lambda *_args, **_kwargs: binding,
        {
            "registrar": lambda: ProviderIdentity(
                target.account_id, reference.principal_arn
            )
        },
        eks,
        None,
        dynamic,
        dynamic,
        access,
        access,
    )
    for document, uid in zip(
        policy_documents(reference.group),
        (reference.policy_uid, reference.binding_uid),
        strict=True,
    ):
        document["metadata"].update(uid=uid, generation=1)
        document["status"] = {
            "observedGeneration": 1,
            "typeChecking": {"expressionWarnings": []},
        }
        resources.objects[(document["kind"], None, document["metadata"]["name"])] = (
            document
        )
    for name in WORKSPACE_CRDS:
        resources.objects[("CustomResourceDefinition", None, name)] = {
            "status": {"conditions": [{"type": "Established", "status": "True"}]},
        }
    resources.objects[("Namespace", None, "peer")] = {
        "kind": "Namespace",
        "metadata": {"name": "peer", "uid": "peer-uid"},
    }
    preserved = deepcopy(resources.objects)
    events = []

    def verify_member(selected, **_):
        assert selected == membership
        rows = store.store.execute(
            "SELECT state FROM cluster_memberships WHERE generation=:generation", values
        )
        if len(rows) != 1 or rows[0]["state"] not in {"reserved", "active"}:
            raise BootstrapRefused("fixture membership withdrawn")

    def prepare(authority, uid):
        assert uid == authority.backend.gate.namespace_uid
        specs = []
        for scope in ("reader", "mutator"):
            for kind in ("ServiceAccount", "Role", "RoleBinding"):
                specs.append(
                    {
                        "cluster_arn": target.cluster_arn,
                        "body": {
                            "apiVersion": "v1"
                            if kind == "ServiceAccount"
                            else "rbac.authorization.k8s.io/v1",
                            "kind": kind,
                            "metadata": {
                                "name": "member-" + scope,
                                "namespace": membership.namespace,
                                "annotations": {
                                    GENERATION_ANNOTATION: membership.generation
                                },
                            },
                            **(
                                {"automountServiceAccountToken": False}
                                if kind == "ServiceAccount"
                                else {}
                            ),
                            **(
                                {
                                    "rules": [
                                        {
                                            "apiGroups": [""],
                                            "resources": ["pods"],
                                            "verbs": ["get", "list"],
                                        }
                                    ]
                                }
                                if kind == "Role"
                                else {}
                            ),
                            **(
                                {
                                    "roleRef": {
                                        "apiGroup": "rbac.authorization.k8s.io",
                                        "kind": "Role",
                                        "name": "member-" + scope,
                                    },
                                    "subjects": [
                                        {
                                            "kind": "ServiceAccount",
                                            "name": "member-" + scope,
                                            "namespace": membership.namespace,
                                        }
                                    ],
                                }
                                if kind == "RoleBinding"
                                else {}
                            ),
                        },
                    }
                )
        authority.establish_components(specs)
        events.append("issued-and-projected")
        return "member-reference-" + membership.generation

    def verify_credentials(authority, _uid):
        assert events and authority.backend.gate.is_closed()

    class Observation(ManagementObservation):
        def observe(self):
            events.append("management-observed")
            return {}

    services = SharedBootstrapServices(
        verify_member,
        prepare,
        verify_credentials,
        lambda *_: events.append("withdrawn"),
        lambda *_: None,
        lambda *_: [],
    )
    factory = SharedBootstrapAuthorityFactory(
        lambda *_: clients,
        BootstrapRelease(
            membership.namespace,
            "member-reader",
            "manager",
            ENFORCE_VERSION,
            WORKSPACE_CRDS,
        ),
        membership=membership,
        cluster_authority=reference,
        services=services,
        resolve_observation=lambda authority: Observation(
            origin="https://management.example",
            credential=lambda: "unused-fixture-token",
            binding=binding,
            target=target,
            namespace=membership.namespace,
            claim=authority.journal.claim,
        ),
    )
    state = FakeStateStore()

    def run(**changes):
        return _run(
            access,
            store,
            binding,
            provider_identity,
            observed_cluster,
            expected_target,
            authority_factory=factory,
            state_store=state,
            membership=membership,
            namespace=membership.namespace,
            cluster_ownership="adopted",
            **changes,
        )

    return SimpleNamespace(
        run=run,
        store=store,
        factory=factory,
        resources=resources,
        access=access,
        membership=membership,
        preserved=preserved,
        events=events,
        entry=entry,
        state=state,
        binding=binding,
        target=target,
    )


def test_shared_engine_registers_without_mutating_cluster_dependencies(shared_runtime):
    runtime = shared_runtime
    result = runtime.run()
    result.raise_for_failure()
    assert result.ready and result.namespace_gate_open and not result.taint_cleared
    assert (
        result.registration.target.membership_generation
        == runtime.membership.generation
    )
    assert result.registration.target.credential_reference_id.startswith(
        "member-reference-"
    )
    assert (
        runtime.resources.objects[("Namespace", None, runtime.membership.namespace)][
            "metadata"
        ]["labels"][GATE_LABEL]
        == "open"
    )
    assert not runtime.access.removed_taints and not runtime.access.restored_taints
    assert not runtime.access.placed_workloads and not runtime.access.established
    assert all(
        runtime.resources.objects[key] == value
        for key, value in runtime.preserved.items()
    )
    assert all(
        key[0] in {"Namespace", "ServiceAccount", "Role", "RoleBinding"}
        for _, key in runtime.resources.effects
    )


def test_lost_namespace_open_reply_restores_only_member_and_withdraws_credentials(
    shared_runtime,
):
    runtime = shared_runtime
    runtime.resources.fail_open_reply = True
    result = runtime.run()
    assert not result.ready and result.refusal is not None
    assert result.namespace_gate_restored and not result.namespace_gate_restore_failed
    assert (
        runtime.resources.objects[("Namespace", None, runtime.membership.namespace)][
            "metadata"
        ]["labels"][GATE_LABEL]
        == "closed"
    )
    assert "withdrawn" in runtime.events
    assert all(
        runtime.resources.objects[key] == value
        for key, value in runtime.preserved.items()
    )
    assert runtime.store.read(runtime.membership.workspace_id) is None


def test_changed_cluster_issuer_refuses_before_namespace_effect(shared_runtime):
    runtime = shared_runtime
    runtime.entry["accessEntryArn"] += "-replacement"
    result = runtime.run()
    assert result.refusal is not None and not runtime.resources.effects


def test_missing_global_dependency_never_installs_it(shared_runtime):
    runtime = shared_runtime
    del runtime.resources.objects[("CustomResourceDefinition", None, WORKSPACE_CRDS[0])]
    result = runtime.run()
    assert result.refusal is not None and not result.ready
    assert not runtime.access.established and not runtime.access.placed_workloads
    assert all(
        key[0] != "CustomResourceDefinition" for _, key in runtime.resources.effects
    )
    recovered = recover_interrupted_bootstrap(
        access=runtime.access,
        store=runtime.store,
        state_store=runtime.state,
        workspace_id=runtime.membership.workspace_id,
        cluster_arn=runtime.membership.cluster_arn,
        authority_factory=runtime.factory,
        binding=runtime.binding,
        target=runtime.target,
    )
    recovered.raise_for_failure()
    assert recovered.reservation_released and not runtime.resources.effects


def test_public_recovery_releases_closed_member_claim_and_retry_reuses_only_namespace(
    shared_runtime,
):
    runtime = shared_runtime
    runtime.resources.fail_open_reply = True
    assert runtime.run().refusal is not None
    namespace_uid = runtime.resources.objects[
        ("Namespace", None, runtime.membership.namespace)
    ]["metadata"]["uid"]
    recovered = recover_interrupted_bootstrap(
        access=runtime.access,
        store=runtime.store,
        state_store=runtime.state,
        workspace_id=runtime.membership.workspace_id,
        cluster_arn=runtime.membership.cluster_arn,
        authority_factory=runtime.factory,
        binding=runtime.binding,
        target=runtime.target,
    )
    recovered.raise_for_failure()
    assert recovered.reservation_released and recovered.namespace_gate_restored
    retried = runtime.run()
    retried.raise_for_failure()
    assert retried.ready and retried.installation.namespace_uid == namespace_uid
    assert not runtime.access.removed_taints and not runtime.access.restored_taints
    assert all(
        runtime.resources.objects[key] == value
        for key, value in runtime.preserved.items()
    )


def test_policy_uid_replacement_refuses_before_member_effect(shared_runtime):
    runtime = shared_runtime
    key = ("ValidatingAdmissionPolicy", None, "superplane-member-admission")
    runtime.resources.objects[key]["metadata"]["uid"] = "replacement"
    result = runtime.run()
    assert result.refusal is not None and not runtime.resources.effects


def _reserved_authorities(runtime, database):
    reservation = reserve_registration(
        store=runtime.store,
        target=runtime.target,
        namespace=runtime.membership.namespace,
        membership=runtime.membership,
    )
    authority = runtime.factory.create(
        binding=runtime.binding,
        target=runtime.target,
        reservation=reservation,
        store=runtime.store,
        state_store=runtime.state,
    )
    authority.acquire()
    authority.bind_namespace()
    recovery = runtime.factory.recover(
        binding=runtime.binding,
        target=runtime.target,
        store=SqlRegistrationStore(database()),
        state_store=runtime.state,
        claim=authority.journal.claim,
    )
    return authority, recovery


def _recover_after_commit(monkeypatch, authority, recovery, predicate):
    """Schedule a second worker in the durable-intent/provider-I/O gap."""
    journal_type = type(authority.journal)
    original = journal_type.fenced
    fired = []

    @contextmanager
    def interleaved(selected, **kwargs):
        if selected is not authority.journal:
            with original(selected, **kwargs):
                yield
            return
        with original(selected, **kwargs):
            yield
            _, progress = authority.journal.read_locked()
            trigger = not fired and predicate(progress)
        if trigger:
            fired.append(True)
            # Recovery uses an independent connection while the original claim
            # remains reserved; no stale-claim rejection can hide the phase race.
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(recovery.recover_member).result(timeout=10)

    monkeypatch.setattr(journal_type, "fenced", interleaved)
    return fired


def test_recovery_commits_before_delayed_open_cannot_reopen(
    shared_runtime,
    database,
    monkeypatch,
):
    runtime = shared_runtime
    authority, recovery = _reserved_authorities(runtime, database)
    reference = runtime.factory.services.prepare_credentials(
        authority,
        authority.backend.gate.namespace_uid,
    )
    authority.record_components(reference)
    authority.revoke(retain_workspace=True)
    fired = _recover_after_commit(
        monkeypatch,
        authority,
        recovery,
        lambda progress: progress.get("member_gate_intent") == "open",
    )
    with pytest.raises(BootstrapRefused, match="after recovery"):
        authority.gate(closed=False)
    assert fired and recovery.backend.gate.is_closed()
    assert not any(key[0] == "ServiceAccount" for key in runtime.resources.objects)
    _, progress = recovery.journal.read()
    assert progress["member_recovery_complete"]
    assert progress["member_gate"] == "closed"


def test_recovery_commits_before_delayed_component_create_cannot_create(
    shared_runtime,
    database,
    monkeypatch,
):
    runtime = shared_runtime
    authority, recovery = _reserved_authorities(runtime, database)
    fired = _recover_after_commit(
        monkeypatch,
        authority,
        recovery,
        lambda progress: any(
            item["phase"] == "intended"
            for item in progress.get("components", {}).values()
        ),
    )
    with pytest.raises(BootstrapRefused, match="permanently withdrew"):
        runtime.factory.services.prepare_credentials(
            authority,
            authority.backend.gate.namespace_uid,
        )
    assert fired and recovery.backend.gate.is_closed()
    assert not any(
        key[0] in {"ServiceAccount", "Role", "RoleBinding"}
        for _, key in runtime.resources.effects
    )
    _, progress = recovery.journal.read()
    assert progress["member_recovery_complete"]
    assert set(progress["component_cleanup"].values()) == {"absent"}


def test_interrupted_recovery_latch_survives_and_cleanup_resumes(
    shared_runtime,
    database,
    monkeypatch,
):
    runtime = shared_runtime
    authority, recovery = _reserved_authorities(runtime, database)
    reference = runtime.factory.services.prepare_credentials(
        authority,
        authority.backend.gate.namespace_uid,
    )
    authority.record_components(reference)
    original = recovery.remove_owned_components

    def interrupted():
        raise OSError("worker interrupted after credential withdrawal")

    monkeypatch.setattr(recovery, "remove_owned_components", interrupted)
    with pytest.raises(OSError, match="worker interrupted"):
        recovery.recover_member()
    _, progress = authority.journal.read()
    assert progress["member_recovery_started"]
    assert not progress["member_recovery_complete"]
    assert progress["phase"] == "revoking"
    with pytest.raises(BootstrapRefused, match="permanently withdrew"):
        authority.record_components(reference)
    with pytest.raises(BootstrapRefused, match="permanently withdrew"):
        authority.mutate(lambda: pytest.fail("delayed provider mutation ran"))
    monkeypatch.setattr(recovery, "remove_owned_components", original)
    recovery.recover_member()
    recovery.recover_member()
    _, progress = recovery.journal.read()
    assert progress["member_recovery_complete"] and progress["complete"]
    assert recovery.backend.gate.is_closed()
    assert not any(key[0] == "ServiceAccount" for key in runtime.resources.objects)


def test_delayed_finalizer_cannot_publish_after_recovery(
    shared_runtime,
    database,
    monkeypatch,
):
    from superplane_bootstrap.state import claim_fingerprint

    runtime = shared_runtime
    store_type = type(runtime.store)
    original = store_type.finalize
    recovered = []

    def delayed_finalize(store, target, attempt_token=""):
        if store is not runtime.store:
            return original(store, target, attempt_token)
        recovery = runtime.factory.recover(
            binding=runtime.binding,
            target=runtime.target,
            store=SqlRegistrationStore(database()),
            state_store=runtime.state,
            claim=claim_fingerprint(attempt_token),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(recovery.recover_member).result(timeout=10)
        recovered.append(recovery)
        original(store, target, attempt_token)

    # The registration store is frozen. Patch its class while interleaving only
    # this fixture's publication; recovery uses its own real SQL store unchanged.
    monkeypatch.setattr(store_type, "finalize", delayed_finalize)
    result = runtime.run()
    assert recovered and not result.ready
    assert "publication authority" in str(result.refusal)
    assert runtime.store.read(runtime.membership.workspace_id) is None
    assert recovered[0].backend.gate.is_closed()
    rows = runtime.store.store.execute(
        "SELECT state,namespace_uid,credential_reference_id FROM cluster_memberships "
        "WHERE generation=:generation",
        {"generation": runtime.membership.generation},
    )
    assert rows[0]["state"] == "reserved"
    assert rows[0]["namespace_uid"] is None
    assert rows[0]["credential_reference_id"] is None


@pytest.mark.parametrize("gate", [None, "closed", "open"])
def test_admission_binding_preserves_dedicated_peer_and_selects_shared_members(gate):
    from superplane_bootstrap.components import BOOTSTRAP_OWNER_LABEL

    policy, binding = policy_documents("superplane:cluster-issuer")
    labels = {BOOTSTRAP_OWNER_LABEL: "existing-dedicated-peer"}
    if gate is not None:
        labels[GATE_LABEL] = gate
    selector = binding["spec"]["matchResources"]["namespaceSelector"]
    # Kubernetes ANDs all Exists expressions. An owner label by itself, as used
    # by already-running dedicated namespaces, must never select this policy.
    assert not selector.get("matchLabels")
    assert all(item["operator"] == "Exists" for item in selector["matchExpressions"])
    selected = all(item["key"] in labels for item in selector["matchExpressions"])
    assert selected is (gate is not None)
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert policy["spec"]["validations"][0]["expression"] == (
        'namespaceObject.metadata.labels["superplane.aws-e/member-admission"] '
        "== 'open' || \"superplane:cluster-issuer\" in request.userInfo.groups"
    )


def test_removed_shared_gate_label_refuses_instead_of_silently_bypassing_policy(
    shared_runtime,
    database,
):
    runtime = shared_runtime
    authority, _ = _reserved_authorities(runtime, database)
    namespace = runtime.resources.objects[
        ("Namespace", None, runtime.membership.namespace)
    ]
    del namespace["metadata"]["labels"][GATE_LABEL]
    with pytest.raises(BootstrapRefused, match="admission label changed"):
        authority.backend.gate.is_closed()
