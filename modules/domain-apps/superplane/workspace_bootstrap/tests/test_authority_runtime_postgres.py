"""Public bootstrap, real PostgreSQL, and stateful EKS/Kubernetes contracts."""

import base64
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from superplane_bootstrap.access import ObservedNamespace, ProviderIdentity
from superplane_bootstrap.authority_backend import BootstrapClients
from superplane_bootstrap.authority_runtime import BootstrapAuthorityFactory
from superplane_bootstrap.grant_plan import ADMIN_POLICY, BootstrapRelease
from superplane_bootstrap.registry import SqlRegistrationStore
from superplane_bootstrap.target import verify_target
from superplane_bootstrap.workspace import recover_interrupted_bootstrap

from .conftest import (
    ACCOUNT_ID,
    NAMESPACE,
    WORKSPACE_ID,
    CLUSTER_ARN,
    ENFORCE_VERSION,
    WORKSPACE_CRDS,
    CONTROLLER_NAME,
    FakeClusterAccess,
    FakeStateStore,
)
from .test_registry_postgres import database, loop, schema_ddl, server  # noqa: F401
from .test_workspace import _run


class Crash(BaseException):
    pass


class ApiError(Exception):
    def __init__(self, status, code="AccessDeniedException"):
        self.status = status
        self.response = {"Error": {"Code": code}}


RESOURCES = {
    "Deployment": ("apps", "deployments"),
    "Namespace": ("", "namespaces"),
    "Role": ("rbac.authorization.k8s.io", "roles"),
    "RoleBinding": ("rbac.authorization.k8s.io", "rolebindings"),
    "ClusterRole": ("rbac.authorization.k8s.io", "clusterroles"),
    "ClusterRoleBinding": ("rbac.authorization.k8s.io", "clusterrolebindings"),
}


class Cloud:
    def __init__(self, target, cluster):
        self.target, self.cluster = target, cluster
        self.meta = SimpleNamespace(region_name=target.region)
        self.entries, self.policies, self.objects = {}, {}, {}
        self.events, self.crash, self.residual = [], None, None
        self.denied = None
        self.count = 0
        self.namespace_created = None
        self.crash_at = None
        self.mutations = 0

    def event(self, operation, identity):
        self.events.append((operation, identity))
        if operation.startswith(("create-", "delete-")):
            self.mutations += 1
            if self.crash_at == self.mutations:
                self.crash_at = None
                raise Crash()
        if self.crash == (operation, identity):
            self.crash = None
            raise Crash()

    def create_access_entry(self, **args):
        principal = args["principalArn"]
        if principal in self.entries:
            raise ApiError(409)
        self.count += 1
        value = {
            "principalArn": principal,
            "clusterName": args["clusterName"],
            "type": args["type"],
            "kubernetesGroups": args.get("kubernetesGroups", []),
            "username": args["username"],
            "tags": args["tags"],
            "accessEntryArn": self.target.cluster_arn.replace(
                ":cluster/", ":access-entry/"
            )
            + "/role/"
            + ACCOUNT_ID
            + "/"
            + principal.rsplit("/", 1)[1]
            + "/"
            + str(self.count),
        }
        self.entries[principal] = deepcopy(value)
        self.policies[principal] = []
        self.event("create-entry", principal)
        return {"accessEntry": deepcopy(value)}

    def describe_cluster(self, *, name):
        assert name == self.target.cluster_name
        return {
            "cluster": {
                "name": name,
                "arn": self.target.cluster_arn,
                "endpoint": self.target.endpoint,
                "certificateAuthority": {
                    "data": self.target.certificate_authority_data
                },
                "status": "ACTIVE",
                "accessConfig": {"authenticationMode": "API"},
            }
        }

    def describe_access_entry(self, **args):
        if args["principalArn"] not in self.entries:
            raise ApiError(404, "ResourceNotFoundException")
        return {"accessEntry": deepcopy(self.entries[args["principalArn"]])}

    def associate_access_policy(self, **args):
        self.policies[args["principalArn"]].append(
            {
                "policyArn": args["policyArn"],
                "accessScope": args["accessScope"],
                "associatedAt": "2026-01-01T00:00:00Z",
            }
        )
        self.event("create-policy", args["principalArn"])

    def list_associated_access_policies(self, **args):
        self.describe_access_entry(**args)
        return {
            "associatedAccessPolicies": deepcopy(self.policies[args["principalArn"]])
        }

    def list_access_entries(self, **args):
        assert args["clusterName"] == self.target.cluster_name
        return {"accessEntries": list(self.entries)}

    def list_identity_provider_configs(self, **args):
        return {"identityProviderConfigs": []}

    def entry_client(self, arn):
        cloud = self

        class Scoped:
            def check(self, principal):
                if cloud.entries[principal]["accessEntryArn"] != arn:
                    raise ApiError(403)

            def delete_access_entry(self, **args):
                principal = args["principalArn"]
                self.check(principal)
                assert not cloud.policies[principal]
                if cloud.residual != principal:
                    del cloud.entries[principal]
                    del cloud.policies[principal]
                cloud.event("delete-entry", principal)

            def disassociate_access_policy(self, **args):
                principal = args["principalArn"]
                self.check(principal)
                cloud.policies[principal] = [
                    p
                    for p in cloud.policies[principal]
                    if p["policyArn"] != args["policyArn"]
                ]
                cloud.event("delete-policy", principal)

        return Scoped()

    def allowed(
        self, principal, *, verb, resource, namespace=None, name=None, groups=None
    ):
        if self.denied == (verb, resource):
            return False
        group = ""
        if "." in resource:
            resource, group = resource.split(".", 1)
        if groups is None:
            entry = self.entries.get(principal)
            if entry is None:
                return False
            groups = entry["kubernetesGroups"]
            if any(p["policyArn"] == ADMIN_POLICY for p in self.policies[principal]):
                return True
        for (kind, ns, _), binding in self.objects.items():
            if kind not in {"RoleBinding", "ClusterRoleBinding"} or (
                kind == "RoleBinding" and ns != namespace
            ):
                continue
            if not any(
                s["kind"] == "Group" and s["name"] in groups
                for s in binding.get("subjects", [])
            ):
                continue
            ref = binding["roleRef"]
            role = self.objects.get(
                (ref["kind"], ns if ref["kind"] == "Role" else None, ref["name"]), {}
            )
            for rule in role.get("rules", []):
                if (
                    group in rule["apiGroups"]
                    and resource in rule["resources"]
                    and verb in rule["verbs"]
                ):
                    if "resourceNames" not in rule or name in rule["resourceNames"]:
                        return True
        return False

    def kube(self, principal, path):
        cloud = self
        config = SimpleNamespace(
            host=self.target.endpoint,
            ssl_ca_cert=str(path),
            verify_ssl=True,
            assert_hostname=None,
            tls_server_name=None,
            proxy=None,
        )

        class Resource:
            def __init__(self, kind):
                self.kind = kind

            def check(self, verb, namespace, name):
                group, resource = RESOURCES[self.kind]
                if not cloud.allowed(
                    principal,
                    verb=verb,
                    resource=resource + ("." + group if group else ""),
                    namespace=namespace,
                    name=name,
                ):
                    raise ApiError(403)

            def get(self, *, name=None, namespace=None):
                self.check("get" if name else "list", namespace, name)
                if name is None and self.kind == "Deployment":
                    if hasattr(cloud.cluster, "components"):
                        items = []
                        for (
                            ns,
                            deployment,
                        ), value in cloud.cluster.deployments.items():
                            body = cloud.cluster.components.get(
                                ("Deployment", ns, deployment)
                            )
                            items.append(
                                deepcopy(body)
                                if body
                                else {
                                    "apiVersion": "apps/v1",
                                    "kind": "Deployment",
                                    "metadata": {"name": deployment, "namespace": ns},
                                    "spec": {
                                        "template": {
                                            "spec": {
                                                "containers": [
                                                    {"image": value["image"]}
                                                ]
                                            }
                                        }
                                    },
                                }
                            )
                        return {"items": items}
                    images = getattr(cloud.cluster, "controller_images", None)
                    if images is None:
                        images = [
                            d["image"] for d in cloud.cluster.deployments.values()
                        ]
                    return {
                        "items": [
                            {
                                "spec": {
                                    "template": {"spec": {"containers": [{"image": i}]}}
                                }
                            }
                            for i in images
                        ]
                    }
                value = cloud.objects.get((self.kind, namespace, name))
                if value is None:
                    raise ApiError(404)
                return deepcopy(value)

            def create(self, *, body, namespace=None):
                name = body["metadata"]["name"]
                self.check("create", namespace, None)
                key = (self.kind, namespace, name)
                if key in cloud.objects:
                    raise ApiError(409)
                if (
                    namespace not in {None, "kube-system"}
                    and ("Namespace", None, namespace) not in cloud.objects
                ):
                    raise ApiError(404)
                cloud.count += 1
                value = deepcopy(body)
                value["metadata"].update(
                    uid="uid-" + str(cloud.count), resourceVersion="1"
                )
                cloud.objects[key] = value
                if self.kind == "Namespace":
                    if cloud.namespace_created:
                        cloud.namespace_created(value)
                    else:
                        cloud.cluster.namespaces[name] = ObservedNamespace(
                            name, value["metadata"]["uid"], value["metadata"]["labels"]
                        )
                cloud.event("create-kube", name)
                return deepcopy(value)

            def delete(self, *, name, body, namespace=None):
                self.check("delete", namespace, name)
                key = (self.kind, namespace, name)
                if any(
                    cloud.objects[key]["metadata"][k] != v
                    for k, v in body["preconditions"].items()
                ):
                    raise ApiError(409)
                if cloud.residual != name:
                    del cloud.objects[key]
                cloud.event("delete-kube", name)

        return SimpleNamespace(
            client=SimpleNamespace(configuration=config),
            resources=SimpleNamespace(get=lambda **args: Resource(args["kind"])),
        )


class Access:
    controller_service_account = CONTROLLER_NAME

    def __init__(self, cloud, actor, principal):
        self.cloud, self.actor, self.principal = cloud, actor, principal

    def __getattr__(self, name):
        return getattr(self.cloud.cluster, name)

    def bootstrap_permission(self, **attrs):
        return self.cloud.allowed(self.principal, **attrs)

    def can_tenant_change_admission_labels(self, namespace):
        identities = self.tenant_identity_reader()
        self.cloud.event("tenant-inventory", self.actor)
        if self.actor == "supervisor":
            assert all(
                not p.endswith(("/registrar", "/installer")) for p in self.cloud.entries
            )
        return any(
            self.cloud.allowed(
                "", groups=groups, verb=verb, resource="namespaces", name=namespace
            )
            for _, groups in identities
            for verb in ("patch", "update")
        )


@pytest.fixture
def runtime(
    database,  # noqa: F811
    tmp_path,
    binding,
    provider_identity,
    observed_cluster,
    expected_target,  # noqa: F811
):  # noqa: F811
    target = verify_target(
        binding=binding,
        provider=provider_identity,
        observed=observed_cluster,
        cluster_ownership="adp-created",
        **expected_target,
    )
    cluster = FakeClusterAccess(crds=[])
    cloud = Cloud(target, cluster)
    principals = {
        name: f"arn:aws:iam::{ACCOUNT_ID}:role/{name}"
        for name in ("registrar", "installer", "supervisor")
    }
    path = tmp_path / "ca.pem"
    path.write_bytes(base64.b64decode(target.certificate_authority_data))
    release = BootstrapRelease(
        NAMESPACE, CONTROLLER_NAME, CONTROLLER_NAME, ENFORCE_VERSION, WORKSPACE_CRDS
    )
    clients = BootstrapClients(
        binding,
        target,
        principals,
        lambda op, **kw: binding,
        {
            actor: lambda principal=principal: ProviderIdentity(ACCOUNT_ID, principal)
            for actor, principal in principals.items()
        },
        cloud,
        cloud.entry_client,
        cloud.kube(principals["registrar"], path),
        cloud.kube(principals["supervisor"], path),
        Access(cloud, "installer", principals["installer"]),
        Access(cloud, "supervisor", principals["supervisor"]),
    )
    factory = BootstrapAuthorityFactory(lambda *_: clients, release)
    store, state = SqlRegistrationStore(database()), FakeStateStore()
    data = SimpleNamespace(
        cloud=cloud,
        cluster=cluster,
        clients=clients,
        factory=factory,
        store=store,
        state=state,
        target=target,
    )

    def run(**kwargs):
        return _run(
            cluster,
            store,
            binding,
            provider_identity,
            observed_cluster,
            expected_target,
            authority_factory=factory,
            state_store=state,
            **kwargs,
        )

    def recover(**overrides):
        return recover_interrupted_bootstrap(
            access=cluster,
            store=store,
            state_store=state,
            workspace_id=WORKSPACE_ID,
            cluster_arn=CLUSTER_ARN,
            authority_factory=factory,
            binding=overrides.pop("binding", binding),
            target=target,
            **overrides,
        )

    data.run, data.recover = run, recover
    data.binding = binding
    return data


def test_fresh_cluster_public_composition_revokes_before_final_inventory_and_registration(
    runtime,
):
    result = runtime.run()
    assert result.refusal is None, repr(result.refusal)
    assert result.ready
    assert set(runtime.cloud.entries) == {runtime.clients.principals["supervisor"]}
    assert runtime.store.read(WORKSPACE_ID) is not None
    import json
    from superplane_bootstrap.inventory import inventory_from_mapping

    rows = runtime.store.store.execute(
        "SELECT progress_json FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
        {"workspace_id": WORKSPACE_ID},
    )
    durable = inventory_from_mapping(
        json.loads(rows[0]["progress_json"])["prerequisite_inventory"]
    )
    assert durable == result.inventory
    assert runtime.state.load().prerequisite_inventory == durable
    assert not runtime.cluster.taints
    events = runtime.cloud.events
    assert events.index(
        ("delete-entry", runtime.clients.principals["registrar"])
    ) < events.index(("tenant-inventory", "supervisor"))
    assert all("installer" not in key[2] for key in runtime.cloud.objects)
    assert not runtime.clients.supervisor_access.bootstrap_permission(
        verb="create", resource="pods", namespace=NAMESPACE
    )
    assert not runtime.clients.supervisor_access.bootstrap_permission(
        verb="get", resource="secrets", namespace=NAMESPACE
    )


@pytest.mark.parametrize(
    "permission",
    [
        ("create", "clusterroles.rbac.authorization.k8s.io"),
        ("bind", "clusterroles.rbac.authorization.k8s.io"),
    ],
)
def test_missing_installer_permission_refuses_and_removes_all_temporary_grants(
    runtime, permission
):
    runtime.cloud.denied = permission
    result = runtime.run()
    assert result.refusal
    assert not runtime.cloud.entries
    assert runtime.store.read(WORKSPACE_ID) is None
    assert runtime.cluster.taints


def test_wrong_credential_identity_refuses_before_any_grant(runtime):
    runtime.clients.identity_readers["installer"] = lambda: ProviderIdentity(
        ACCOUNT_ID, "arn:aws:iam::000000000000:role/other"
    )
    result = runtime.run()
    assert result.refusal
    assert runtime.cloud.events == []


@pytest.mark.parametrize(
    "changed",
    ["region", "endpoint", "arn", "ca", "status", "legacy-auth", "unknown-auth"],
)
def test_wrong_eks_target_refuses_before_any_grant(runtime, changed):
    cloud = runtime.cloud
    if changed == "region":
        cloud.meta.region_name = "another-region"
    else:
        response = cloud.describe_cluster(name=runtime.target.cluster_name)
        if changed == "ca":
            response["cluster"]["certificateAuthority"]["data"] = "another-ca"
        elif changed == "legacy-auth":
            response["cluster"]["accessConfig"]["authenticationMode"] = (
                "API_AND_CONFIG_MAP"
            )
        elif changed == "unknown-auth":
            response["cluster"].pop("accessConfig")
        else:
            response["cluster"][changed] = "another-target"
        cloud.describe_cluster = lambda **_: response
    assert runtime.run().refusal
    assert not cloud.events


def test_empty_system_inventory_refuses(runtime):
    from superplane_bootstrap.errors import BootstrapRefused

    with pytest.raises(BootstrapRefused, match="system workload inventory"):
        replace(runtime.factory.release, system_workloads=())


def test_registrar_namespace_create_denial_revokes_without_installing(runtime):
    runtime.cloud.denied = ("create", "namespaces")
    result = runtime.run()
    assert result.refusal and result.reservation_released
    assert not runtime.cloud.entries and not runtime.cluster.namespaces
    assert not runtime.cluster.installed_controllers


def test_existing_registrar_is_preserved_without_modifying_its_access(runtime):
    arn = runtime.clients.principals["registrar"]
    runtime.cloud.entries[arn] = {
        "principalArn": arn,
        "clusterName": runtime.target.cluster_name,
        "accessEntryArn": CLUSTER_ARN.replace(":cluster/", ":access-entry/")
        + "/role/000000000000/registrar/adopted",
        "type": "STANDARD",
        "kubernetesGroups": [],
        "username": "adopted",
        "tags": {},
    }
    runtime.cloud.policies[arn] = []
    result = runtime.run()
    assert result.refusal
    assert set(runtime.cloud.entries) == {arn}
    assert runtime.cloud.events == []


def test_residual_registrar_privilege_holds_registration_and_claim(runtime):
    runtime.cloud.residual = runtime.clients.principals["registrar"]
    result = runtime.run()
    assert result.refusal
    assert not result.registration and not result.reservation_released
    assert runtime.state.load().registration_reserved
    assert runtime.cluster.taints


def test_completed_public_replay_performs_no_grants_or_cluster_mutations(runtime):
    assert runtime.run().ready
    events = list(runtime.cloud.events)
    again = runtime.run()
    assert again.registered and again.registration.replayed
    assert not again.taint_cleared
    assert runtime.cloud.events == events


def test_mismatched_release_refuses_before_grants(runtime):
    runtime.factory.release = replace(
        runtime.factory.release, namespace="different-workspace"
    )
    assert runtime.run().refusal
    assert runtime.cloud.events == []


@pytest.mark.parametrize("crash_at", range(1, 27))
def test_public_restart_recovers_every_grant_and_revocation_boundary(runtime, crash_at):
    runtime.cloud.crash_at = crash_at
    with pytest.raises(Crash):
        runtime.run()
    assert runtime.state.load().registration_reserved
    before = list(runtime.cloud.events)
    recovered = runtime.recover()
    assert recovered.refusal is None, repr(recovered.refusal)
    assert recovered.reservation_released
    assert runtime.clients.principals["registrar"] not in runtime.cloud.entries
    assert runtime.clients.principals["installer"] not in runtime.cloud.entries
    assert all(
        not event[0].startswith("create-")
        for event in runtime.cloud.events[len(before) :]
    )
    assert runtime.cluster.taints
    assert not runtime.state.load().recovery_pending
    again = list(runtime.cloud.events)
    assert runtime.recover().refusal is None
    assert runtime.cloud.events == again


def test_wrong_operation_recovery_never_obtains_or_changes_grants(runtime):
    runtime.cloud.crash_at = 6
    with pytest.raises(Crash):
        runtime.run()
    before = list(runtime.cloud.events)
    result = runtime.recover(
        binding=replace(runtime.binding, operation_id="another-operation")
    )
    assert result.refusal and not result.reservation_released
    assert runtime.cloud.events == before


def test_public_stale_recovery_cannot_touch_a_successor_claim(runtime):
    from superplane_bootstrap.registration import reserve_registration

    runtime.cloud.crash_at = 6
    with pytest.raises(Crash):
        runtime.run()
    with runtime.store.store.transaction():
        runtime.store.store.execute(
            "DELETE FROM workspace_bootstrap_reservations WHERE workspace_id=:workspace_id",
            {"workspace_id": WORKSPACE_ID},
        )
        runtime.store.store.execute(
            "UPDATE workspace_bootstrap_authority SET revoked=true WHERE workspace_id=:workspace_id",
            {"workspace_id": WORKSPACE_ID},
        )
    successor = reserve_registration(
        store=runtime.store, target=runtime.target, namespace=NAMESPACE
    )
    before = list(runtime.cloud.events)
    result = runtime.recover()
    assert result.refusal and not result.reservation_released
    assert runtime.cloud.events == before
    assert successor.attempt_token


def test_failed_registration_restores_interlock_after_installer_is_revoked(
    runtime, monkeypatch
):
    original = SqlRegistrationStore.finalize
    monkeypatch.setattr(
        SqlRegistrationStore,
        "finalize",
        lambda *a, **kw: (_ for _ in ()).throw(
            OSError("synthetic registration failure")
        ),
    )
    failed = runtime.run()
    assert failed.refusal and failed.taint_cleared and failed.taint_restored
    assert failed.reservation_released
    assert not failed.nodes_left_schedulable
    assert set(runtime.cloud.entries) == {runtime.clients.principals["supervisor"]}
    old_supervisor = deepcopy(
        runtime.cloud.entries[runtime.clients.principals["supervisor"]]
    )
    monkeypatch.setattr(SqlRegistrationStore, "finalize", original)
    succeeded = runtime.run()
    assert succeeded.ready, repr(succeeded.refusal)
    assert (
        runtime.cloud.entries[runtime.clients.principals["supervisor"]]
        == old_supervisor
    )


def test_interrupted_after_taint_clear_is_restored_through_public_recovery(
    runtime, monkeypatch
):
    monkeypatch.setattr(
        SqlRegistrationStore,
        "finalize",
        lambda *a, **kw: (_ for _ in ()).throw(Crash()),
    )
    with pytest.raises(Crash):
        runtime.run()
    assert not runtime.cluster.taints
    assert set(runtime.cloud.entries) == {runtime.clients.principals["supervisor"]}
    result = runtime.recover()
    assert result.taint_restored and result.reservation_released
    assert runtime.cluster.taints
    assert not runtime.state.load().recovery_pending


@pytest.mark.parametrize("change", ["uid", "labels", "policy", "tenant-admin"])
def test_final_inventory_refuses_privilege_or_identity_changes(runtime, change):
    original = runtime.cloud.event

    def mutate(operation, identity):
        original(operation, identity)
        if (operation, identity) != (
            "delete-entry",
            runtime.clients.principals["registrar"],
        ):
            return
        cloud = runtime.cloud
        if change in {"uid", "labels"}:
            role = next(
                body
                for (kind, _, name), body in cloud.objects.items()
                if kind == "ClusterRole" and name.endswith("supervisor-cluster")
            )
            role["metadata"][change] = (
                "successor-uid"
                if change == "uid"
                else {"rbac.authorization.k8s.io/aggregate-to-admin": "true"}
            )
        elif change == "policy":
            cloud.policies[runtime.clients.principals["supervisor"]].append(
                {"policyArn": ADMIN_POLICY, "accessScope": {"type": "cluster"}}
            )
        else:
            principal = f"arn:aws:iam::{ACCOUNT_ID}:role/tenant"
            cloud.create_access_entry(
                clusterName=runtime.target.cluster_name,
                principalArn=principal,
                type="STANDARD",
                username="tenant",
                tags={},
            )
            cloud.policies[principal].append(
                {"policyArn": ADMIN_POLICY, "accessScope": {"type": "cluster"}}
            )

    runtime.cloud.event = mutate
    result = runtime.run()
    assert result.refusal and not result.registered
    assert runtime.cluster.taints
    assert runtime.store.read(WORKSPACE_ID) is None


def test_recovery_requires_reauthenticated_binding_and_never_reacquires(runtime):
    from datetime import UTC, datetime

    runtime.cloud.crash_at = 6
    with pytest.raises(Crash):
        runtime.run()
    expired = replace(runtime.binding, expires_at=datetime(2020, 1, 1, tzinfo=UTC))
    clients = replace(
        runtime.clients, binding=expired, binding_resolver=lambda *a, **k: expired
    )
    runtime.factory.resolve_clients = lambda *_: clients
    before = list(runtime.cloud.events)
    refused = runtime.recover(binding=expired)
    assert refused.refusal and not refused.reservation_released
    assert runtime.cloud.events == before
    # The service renews authentication for the SAME operation. The old journal's
    # immutable identity and claim still authorize only cleanup, never new grants.
    renewed = replace(runtime.binding, expires_at=datetime(2031, 1, 1, tzinfo=UTC))
    clients = replace(
        runtime.clients, binding=renewed, binding_resolver=lambda *a, **k: renewed
    )
    runtime.factory.resolve_clients = lambda *_: clients
    result = runtime.recover(binding=renewed)
    assert result.reservation_released and not result.refusal
    assert all(
        not event[0].startswith("create-")
        for event in runtime.cloud.events[len(before) :]
    )


def test_failed_restore_keeps_claim_for_public_recovery(runtime, monkeypatch):
    original_restore = runtime.cluster.restore_bootstrap_taint
    monkeypatch.setattr(
        SqlRegistrationStore,
        "finalize",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("registration unavailable")),
    )
    runtime.cluster.restore_bootstrap_taint = lambda key: []
    result = runtime.run()
    assert result.refusal and result.nodes_left_schedulable
    assert not result.reservation_released
    assert runtime.state.load().registration_claim
    runtime.cluster.restore_bootstrap_taint = original_restore
    recovered = runtime.recover()
    assert recovered.taint_restored and recovered.reservation_released
    assert not recovered.nodes_left_schedulable


@pytest.mark.parametrize("interrupted", [False, True])
def test_cli_trusted_runtime_runs_public_bootstrap_and_recovery(
    runtime,
    tmp_path,
    monkeypatch,
    capsys,
    provider_identity,
    observed_cluster,
    interrupted,
):
    import json
    import sys
    from types import ModuleType
    from superplane_bootstrap import cli
    from superplane_bootstrap.authority_runtime import BootstrapRuntime
    from .conftest import FakePrerequisiteAccess
    from .test_cli import _outputs, _write, _bootstrap_argv

    calls = []
    module = ModuleType("synthetic_authority_composer")
    module.binding = lambda operation_id: runtime.binding
    observer = SimpleNamespace(
        provider_identity=lambda: provider_identity,
        cluster_identity=lambda name: observed_cluster,
    )

    def compose(binding, outputs):
        assert binding == runtime.binding
        assert outputs["cluster_arn"] == CLUSTER_ARN
        calls.append(binding.operation_id)
        return BootstrapRuntime(
            runtime.factory,
            observer,
            runtime.cluster,
            FakePrerequisiteAccess(),
            runtime.store,
        )

    module.compose = compose
    monkeypatch.setitem(sys.modules, module.__name__, module)
    from superplane_bootstrap.admission import required_proofs

    outputs = _outputs()
    outputs["tenant_scheduling_prerequisites"]["value"]["required_proofs"] = list(
        required_proofs()
    )
    files = {
        "outputs": _write(tmp_path / "outputs.json", outputs),
        "binding": _write(
            tmp_path / "binding.json", {"operation_id": runtime.binding.operation_id}
        ),
        "state_dir": tmp_path / "state",
    }
    argv = _bootstrap_argv(
        files,
        "--binding-resolver",
        module.__name__ + ":binding",
        "--authority-resolver",
        module.__name__ + ":compose",
    )
    if interrupted:
        monkeypatch.setattr(
            SqlRegistrationStore,
            "finalize",
            lambda *a, **kw: (_ for _ in ()).throw(Crash()),
        )
        with pytest.raises(Crash):
            cli.main(argv)
        assert not runtime.cluster.taints
        parser = cli._build_parser()
        args = parser.parse_args(argv)
        # Use the recover command's real parser, preserving only its supported flags.
        recover_options = {
            action.dest
            for action in parser._subparsers._group_actions[0]
            .choices["recover"]
            ._actions
        }
        recovered_argv = ["recover"]
        for action in (
            parser._subparsers._group_actions[0].choices["bootstrap"]._actions
        ):
            if (
                action.dest in recover_options
                and action.option_strings
                and getattr(args, action.dest, None) is not None
            ):
                recovered_argv += [
                    action.option_strings[0],
                    str(getattr(args, action.dest)),
                ]
        assert (
            cli.main(recovered_argv) == cli._EXIT_REFUSED
        )  # Reports the recovered interruption.
        report = json.loads(capsys.readouterr().out)
        assert report["taint_restored"] and report["reservation_released"]
        assert not report["nodes_left_schedulable"]
        assert len(calls) == 2
    else:
        assert cli.main(argv) == cli._EXIT_OK
        assert json.loads(capsys.readouterr().out)["registered"]
        assert runtime.store.read(WORKSPACE_ID) is not None
        assert len(calls) == 1


def test_managed_bootstrap_journals_dormant_exact_cleanup_grants(runtime):
    import json

    runtime.factory.original_allocation_id = "original-allocation"
    result = runtime.run()
    assert result.ready, repr(result.refusal)
    rows = runtime.store.store.execute(
        "SELECT plan_json, progress_json FROM workspace_bootstrap_authority "
        "WHERE workspace_id=:workspace_id",
        {"workspace_id": WORKSPACE_ID},
    )
    plan, progress = (
        json.loads(rows[0][key]) for key in ("plan_json", "progress_json")
    )
    grants = [s for s in plan["grants"] if s["key"].startswith("cleanup-")]
    assert len(grants) == 6
    assert {s["kind"] for s in grants} == {"kubernetes"}
    assert {s["lifetime"] for s in grants} == {"workspace"}
    assert {s["actor"] for s in grants} == {"registrar"}
    assert {s["original_allocation_id"] for s in grants} == {"original-allocation"}
    (group,) = {s["cleanup_group"] for s in grants}
    assert group.endswith(":cleanup")
    assert all(
        group not in entry["kubernetesGroups"]
        for entry in runtime.cloud.entries.values()
    )
    assert set(runtime.cloud.entries) == {runtime.clients.principals["supervisor"]}
    for spec in grants:
        identity = progress[spec["key"]]["identity"]
        assert identity["uid"] and identity["digest"]
        assert identity["generation"] == spec["generation"]
        assert progress[spec["key"]]["phase"] == "granted"
        rules = spec["body"].get("rules", [])
        assert all(set(rule["verbs"]) == {"get", "delete"} for rule in rules)
        assert all(rule.get("resourceNames") for rule in rules)
    cluster = next(s for s in grants if s["key"] == "cleanup-cluster-role")
    assert cluster["body"]["rules"][0]["resourceNames"] == [NAMESPACE]
    assert all("*" not in rule["resourceNames"] for rule in cluster["body"]["rules"])


def test_existing_eks_mapping_refuses_dormant_cleanup_grant(runtime, monkeypatch):
    from superplane_bootstrap import authority_backend

    runtime.factory.original_allocation_id = "original-allocation"
    compile_grants = authority_backend.compile_grants

    def mapped(*args, **kwargs):
        plan = compile_grants(*args, **kwargs)
        group = next(
            spec["cleanup_group"]
            for spec in plan["grants"]
            if spec["key"] == "cleanup-cluster-binding"
        )
        principal = f"arn:aws:iam::{ACCOUNT_ID}:role/unattributed"
        runtime.cloud.entries[principal] = {
            "principalArn": principal,
            "kubernetesGroups": [group],
        }
        return plan

    monkeypatch.setattr(authority_backend, "compile_grants", mapped)
    result = runtime.run()
    assert result.refusal and not result.ready
    assert "temporary bootstrap authority remains unresolved" in str(result.refusal)
    assert not runtime.cloud.objects
    assert not any(name == "create-kube" for name, _ in runtime.cloud.events)


@pytest.mark.parametrize("change", ["uid", "rules", "subjects"])
def test_cleanup_grant_drift_prevents_bootstrap_completion(runtime, change):
    runtime.factory.original_allocation_id = "original-allocation"
    original = runtime.cloud.event

    def changed(operation, identity):
        original(operation, identity)
        if (operation, identity) != (
            "delete-entry",
            runtime.clients.principals["registrar"],
        ):
            return
        kind = "ClusterRoleBinding" if change == "subjects" else "ClusterRole"
        body = next(
            resource
            for (resource_kind, _, name), resource in runtime.cloud.objects.items()
            if resource_kind == kind and name.endswith("cleanup-cluster")
        )
        if change == "uid":
            body["metadata"]["uid"] = "replaced-uid"
        elif change == "rules":
            body["rules"][0]["verbs"].append("create")
        else:
            body["subjects"][0]["name"] = "another-group"

    runtime.cloud.event = changed
    result = runtime.run()
    assert result.refusal and not result.ready
    assert (
        not runtime.store.read(WORKSPACE_ID)
        or not runtime.store.read(WORKSPACE_ID).state == "registered"
    )


def test_adopted_cluster_refuses_original_cleanup_allocation(runtime):
    from types import SimpleNamespace
    from superplane_bootstrap.errors import BootstrapRefused
    from superplane_bootstrap.grant_plan import compile_grants

    journal = SimpleNamespace(
        target=replace(runtime.target, cluster_ownership="adopted"),
        generation="a" * 64,
        original_allocation_id="original-allocation",
    )
    with pytest.raises(BootstrapRefused, match="original managed allocation"):
        compile_grants(journal, runtime.factory.release, runtime.clients.principals)


def test_existing_bootstrap_without_original_allocation_cannot_gain_cleanup(runtime):
    import json

    result = runtime.run()
    assert result.ready
    rows = runtime.store.store.execute(
        "SELECT plan_json FROM workspace_bootstrap_authority WHERE workspace_id=:workspace_id",
        {"workspace_id": WORKSPACE_ID},
    )
    plan = json.loads(rows[0]["plan_json"])
    assert not any(s["key"].startswith("cleanup-") for s in plan["grants"])
