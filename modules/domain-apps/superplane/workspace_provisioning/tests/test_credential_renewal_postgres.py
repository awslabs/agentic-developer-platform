"""Exercise production renewal, real journals and stateful Kubernetes transports."""
# ruff: noqa: F811

from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from superplane_bootstrap.errors import BootstrapRefused

from workspace_provisioning.credential_controller import renewal

from .credential_renewal_harness import RenewalHarness
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


@pytest.fixture
def controller(bootstrap_harness, tmp_path, monkeypatch):
    return RenewalHarness(bootstrap_harness, tmp_path, monkeypatch)


def test_rotate_acknowledge_revoke_and_preserve_other_member(controller, monkeypatch):
    main, peer = controller.members
    controller.reconcile(main)
    controller.reconcile(peer)
    originals = {
        scope: deepcopy(controller.secret(scope)) for scope in ("reader", "mutator")
    }
    original_accounts = {
        key
        for key in controller.issuer_api.objects
        if key[0] == "ServiceAccount" and key[1] == main.namespace
    }
    assert len(original_accounts) == 2
    assert {row["state"] for row in controller.rows(main)} == {"active"}

    class RenewalClock:
        @staticmethod
        def now(tz):
            return datetime.now(tz) + timedelta(minutes=6)

    with monkeypatch.context() as future:
        future.setattr(renewal, "datetime", RenewalClock)
        controller.reconcile(main)
    assert {(r["revision"], r["state"]) for r in controller.rows(main)} == {
        (1, "revoking"),
        (2, "active"),
    }
    # The next real process cycle consumes durable old-revision cleanup state.
    controller.reconcile(main)
    assert {(r["revision"], r["state"]) for r in controller.rows(main)} == {
        (1, "revoked"),
        (2, "active"),
    }
    assert not original_accounts.intersection(controller.issuer_api.objects)
    assert (
        len(
            [
                key
                for key in controller.issuer_api.objects
                if key[0] == "ServiceAccount" and key[1] == main.namespace
            ]
        )
        == 2
    )
    for scope, old in originals.items():
        current = controller.secret(scope)
        assert (
            current["data"][peer.workspace_id + ".kubeconfig"]
            == old["data"][peer.workspace_id + ".kubeconfig"]
        )
        assert (
            current["data"]["unrelated.kubeconfig"]
            == old["data"]["unrelated.kubeconfig"]
        )
        assert current["metadata"]["annotations"]["unrelated"] == "preserve"
        assert (
            current["data"][main.workspace_id + ".kubeconfig"]
            != old["data"][main.workspace_id + ".kubeconfig"]
        )
    assert {row["revision"] for row in controller.rows(peer)} == {1}
    assert any(
        path.endswith("/selfsubjectaccessreviews") and "mutator" in name
        for _, name, path, _ in controller.proofs
    )
    assert all("private-test-member" not in str(row) for row in controller.rows(main))


def test_lost_publish_response_reuses_exact_recorded_token_and_projection(controller):
    member = controller.members[0]
    controller.projector_api.lose_publish = True
    with pytest.raises(BootstrapRefused, match="CAS failed"):
        controller.reconcile(member)
    pending = controller.rows(member)
    assert len(pending) == 1 and pending[0]["state"] == "issued"
    assert pending[0]["content_digest"] and pending[0]["service_account_uid"]
    reader_bytes = controller.secret("reader")["data"][
        member.workspace_id + ".kubeconfig"
    ]
    issued_tokens = len(controller.issuer_api.tokens)
    # Reconstruct Renewal instead of retaining any issued-token object in memory.
    controller.reconcile(member)
    assert {row["state"] for row in controller.rows(member)} == {"active"}
    assert (
        controller.secret("reader")["data"][member.workspace_id + ".kubeconfig"]
        == reader_bytes
    )
    assert (
        len(controller.issuer_api.tokens) == issued_tokens + 1
    )  # only missing mutator issued


@pytest.mark.parametrize("gate", ["disabled", "takeover"])
def test_current_installed_authority_fences_entire_renewal_before_provider_effect(
    controller, gate
):
    if gate == "disabled":
        controller.sql(
            "UPDATE cluster_credential_authorities SET enabled=false WHERE authority_id=$1",
            UUID(controller.authority.authority_id),
        )
    else:
        controller.sql(
            "UPDATE cluster_credential_authorities SET fence_token=fence_token+1,holder='replacement-controller' WHERE authority_id=$1",
            UUID(controller.authority.authority_id),
        )
    with pytest.raises(BootstrapRefused, match="revoked or fenced"):
        controller.reconcile(controller.members[0])
    assert not controller.issuer_api.effects and not controller.projector_api.effects


def test_member_revocation_during_token_request_never_projects_and_preserves_peer(
    controller,
):
    member, peer = controller.members
    controller.reconcile(peer)
    peer_before = {
        scope: deepcopy(controller.secret(scope)) for scope in ("reader", "mutator")
    }

    def retire():
        controller.sql(
            "UPDATE cluster_memberships SET state='removed' WHERE workspace_id=$1",
            UUID(member.workspace_id),
        )

    controller.issuer_api.on_token = retire
    with pytest.raises(BootstrapRefused, match="no longer authorizes"):
        controller.reconcile(member)
    assert controller.rows(member)[0]["state"] == "reserved"
    assert controller.rows(member)[0]["service_account_uid"]
    for scope in ("reader", "mutator"):
        assert controller.secret(scope) == peer_before[scope]
    controller.reconcile(member, retiring=True)
    assert {row["state"] for row in controller.rows(member)} == {"revoked"}
    assert not [
        key
        for key in controller.issuer_api.objects
        if key[0] == "ServiceAccount" and key[1] == member.namespace
    ]
    assert {row["state"] for row in controller.rows(peer)} == {"active"}
