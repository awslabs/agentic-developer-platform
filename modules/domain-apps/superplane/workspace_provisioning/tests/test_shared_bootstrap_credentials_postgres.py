"""Real bootstrap and credential journals composed with stateful provider APIs."""
# ruff: noqa: F811

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from superplane_bootstrap.kube_grants import KubeGrants

from workspace_bootstrap.tests.test_shared_workspace_postgres import shared_runtime  # noqa: F401
from workspace_provisioning.shared_bootstrap_credentials import SharedCredentialServices

from .test_member_credentials import Resource, fixture  # noqa: F401


@pytest.mark.parametrize("lost_projection_reply", [None, "exception", "process-loss"])
def test_shared_engine_composes_real_credential_journals(
    shared_runtime,
    fixture,
    tmp_path,
    monkeypatch,
    lost_projection_reply,
):
    runtime, transport = shared_runtime, fixture
    # Expose the already-owned asyncpg connection/loop through the production
    # bridge interface; journal calls still use the REAL authority transaction.
    bridge = runtime.store.store
    bridge.connection = bridge._connection
    bridge.wait = bridge._loop.run
    clients = runtime.factory.resolve_clients(
        runtime.binding, runtime.target, runtime.factory.release
    )
    issuer = KubeGrants(clients.registrar_kubernetes, runtime.target)
    tokens = []

    def token_request(path, method, **kwargs):
        assert method == "POST" and path.endswith("/token")
        assert bridge.connection.is_in_transaction()
        tokens.append(path)
        return {
            "status": {
                "token": "private-member-token",
                "expirationTimestamp": (
                    datetime.now(UTC) + timedelta(minutes=10)
                ).isoformat(),
            }
        }

    clients.registrar_kubernetes.client.call_api = token_request
    original_sync = runtime.resources.sync

    def namespace_active(body):
        if body["kind"] == "Namespace":
            body["status"] = {"phase": "Active"}
        original_sync(body)

    monkeypatch.setattr(runtime.resources, "sync", namespace_active)
    management = transport.issuer.grants
    reader = transport.api.objects[("Secret", "control", "reader-credentials")]
    mutator = deepcopy(reader)
    mutator["metadata"].update(name="mutator-credentials", uid="mutator-secret-uid")
    transport.api.objects[("Secret", "control", "mutator-credentials")] = mutator
    installation = SimpleNamespace(
        document={"audience": "https://kubernetes.default.svc"},
        projection=lambda scope: dict(
            namespace="control",
            namespace_uid="control-uid",
            secret_name=scope + "-credentials",
            secret_uid="secret-uid" if scope == "reader" else "mutator-secret-uid",
            scope=scope,
        ),
    )
    proven = []

    def consumer(projector, binding, receipt, **kwargs):
        # The separate consumer-proof suite tests provider SSR/read/denial. Here
        # require the exact real CAS projection before acknowledging its journal.
        _, body = projector._read(binding, "verify")
        assert projector._owned(body, *projector._names(binding), receipt)
        assert kwargs["expect_closed"]
        proven.append(binding.scope)

    monkeypatch.setattr(
        "workspace_provisioning.shared_bootstrap_credentials.verify_projected_credential",
        consumer,
    )
    services = SharedCredentialServices(
        installation,
        issuer,
        management,
        bridge,
        tmp_path,
        lambda: None,
        lambda _: None,
        lambda _: [],
    )
    runtime.factory.services = services.services()
    if lost_projection_reply:
        original_patch = Resource.patch
        failed = []

        def lose_reply(resource, *args, **kwargs):
            original_patch(resource, *args, **kwargs)
            if not failed:
                failed.append(True)
                if lost_projection_reply == "process-loss":
                    raise KeyboardInterrupt("worker terminated after Secret write")
                raise OSError("lost Secret publish acknowledgement")

        monkeypatch.setattr(Resource, "patch", lose_reply)
    if lost_projection_reply == "process-loss":
        from superplane_bootstrap.workspace import recover_interrupted_bootstrap

        with pytest.raises(KeyboardInterrupt, match="worker terminated"):
            runtime.run()
        assert bridge._transaction is None
        durable = bridge.execute(
            "SELECT state,content_digest,projection_uid,projection_version FROM membership_credentials",
            {},
        )
        assert len(durable) == 1
        assert durable[0]["state"] == "issued"
        assert durable[0]["content_digest"] and durable[0]["projection_uid"]
        assert durable[0]["projection_version"] is None
        assert (
            runtime.membership.workspace_id + ".kubeconfig"
            in transport.api.objects[("Secret", "control", "reader-credentials")][
                "data"
            ]
        )
        result = recover_interrupted_bootstrap(
            access=runtime.access,
            store=runtime.store,
            state_store=runtime.state,
            workspace_id=runtime.membership.workspace_id,
            cluster_arn=runtime.membership.cluster_arn,
            authority_factory=runtime.factory,
            binding=runtime.binding,
            target=runtime.target,
        )
        result.raise_for_failure()
        assert result.reservation_released and result.namespace_gate_restored
    else:
        result = runtime.run()
    rows = bridge.execute(
        "SELECT scope,state,service_account_uid,content_digest FROM membership_credentials ORDER BY scope",
        {},
    )
    assert tokens and rows
    if lost_projection_reply:
        if lost_projection_reply == "exception":
            assert result.refusal is not None and result.namespace_gate_restored
        assert {row["state"] for row in rows} == {"revoked"}
        assert not any(key[0] == "ServiceAccount" for key in runtime.resources.objects)
        assert runtime.store.read(runtime.membership.workspace_id) is None
    else:
        result.raise_for_failure()
        assert result.ready and {row["state"] for row in rows} == {"active"}
        assert len(rows) == 2 and set(proven) == {"reader", "mutator"}
    assert all(row["service_account_uid"] and row["content_digest"] for row in rows)
    for name in ("reader-credentials", "mutator-credentials"):
        body = transport.api.objects[("Secret", "control", name)]
        assert body["data"]["peer.kubeconfig"] == "cGVlcg=="
        if lost_projection_reply:
            assert runtime.membership.workspace_id + ".kubeconfig" not in body["data"]
