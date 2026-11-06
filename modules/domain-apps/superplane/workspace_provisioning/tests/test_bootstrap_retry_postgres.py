"""A real original bootstrap retry consumes its receipt and retains expired evidence."""

# ruff: noqa: F811
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.provider_retry import recover_uncredentialed
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant

from workspace_provisioning import runtime
from workspace_provisioning.artifacts import continuation_parameters, digest
from workspace_provisioning.recovery import LifecycleRecovery
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import requires_harness_postgres
from .test_applied_recovery_postgres import interrupted_apply
from .test_lifecycle_effects_postgres import effects_ddl
from .test_lifecycle_runtime_postgres import harness as harness  # noqa: F401
from .test_retirement_execution_postgres import _principal

pytestmark = requires_harness_postgres


async def recover_failed_retry(scenario, operation, artifact):
    """The real recovery reader must settle only the latest authorized call."""
    harness = scenario.harness
    oid = operation.grant.lease.operation_id
    principal = replace(
        operation.grant.principal,
        subject="post-retry-recovery",
        permissions=frozenset({"workspace:recover"}),
    )

    class Authority:
        async def resolve_recovery(self, claim):
            grant = RecoveryGrant(principal, claim)
            async with harness.connect() as connection, connection.transaction():
                assert await lock_recovery_grant(connection, grant)
            return SimpleNamespace(**{**vars(operation), "grant": grant})

    context = SimpleNamespace(**{**vars(scenario.context), "authority": Authority()})
    pool = SimpleNamespace(acquire=harness.connect)
    recovery = LifecycleRecovery(
        SimpleNamespace(domain_pool=pool, execution_pool=pool),
        principal=principal,
        operation_id=oid,
        context=context,
    )
    async with harness.connect() as connection:
        await connection.execute(
            effects_ddl("017_add_workspace_bootstrap_reservations.py")
        )
        await connection.execute(
            "UPDATE workspaces SET provisioning_operation_id=$1 WHERE id=$2",
            oid,
            principal.workspace_id,
        )
        before = await connection.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 ORDER BY fence_token",
            oid,
        )
        retries = await connection.fetch(
            "SELECT * FROM harness_provider_retries WHERE operation_id=$1 ORDER BY source_fence",
            oid,
        )
    bootstrap_calls = len(scenario.bootstrap_calls)
    for _ in range(3):
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                oid,
            )
            await connection.execute(
                "UPDATE harness_provider_call_intent SET reconcile_after=NULL WHERE operation_id=$1",
                oid,
            )
        (result,) = await recovery.run(limit=1)
        if result.action != "deferred":
            break
    assert result.action == "unknown", result
    assert result.budget_disposition == "retain"
    assert len(scenario.bootstrap_calls) == bootstrap_calls
    async with harness.connect() as connection:
        after = await connection.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 ORDER BY fence_token",
            oid,
        )
        assert len(after) == len(before)
        assert after[:-1] == before[:-1]
        assert after[-1]["idempotency_key"] == retries[-1]["replacement_key"]
        assert after[-1]["stage"] == "unresolved"
        assert (
            await connection.fetch(
                "SELECT * FROM harness_provider_retries WHERE operation_id=$1 ORDER BY source_fence",
                oid,
            )
            == retries
        )
        (receipt,) = await connection.fetch(
            "SELECT accounting FROM harness_recovery_settlements WHERE operation_id=$1",
            oid,
        )
        accounting = json.loads(receipt["accounting"])
        assert accounting["budget"] == "retain"
        assert accounting["call_dispositions"] == {
            retries[-1]["replacement_key"]: "retain"
        }
        assert accounting["release_permitted"] is False
        assert accounting["may_mark_released"] is False
        partial = accounting["partial_lifecycle"]
        assert partial["phase"] == "bootstrap-workspace"
        assert partial["source_artifact_id"] == artifact["artifact_id"]
        assert partial["workflow_complete"] is False
        assert partial["cleanup_authorized"] is False
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)",
            oid,
        )


@pytest.mark.parametrize("denied_first_grant", [False, True])
def test_retry_executes_original_bootstrap_after_apply_artifact_expires(
    harness, tmp_path, monkeypatch, denied_first_grant
):
    async def run():
        scenario, _, _, artifact, recovery, _ = await interrupted_apply(
            harness, tmp_path, monkeypatch, observed=True
        )
        (result,) = await recovery.run(limit=1)
        assert result.action == "succeeded"

        async def observed_public_network(*args, **kwargs):
            # Scope only covers public management access, which has no network mutation.
            return SimpleNamespace(mode="public")

        monkeypatch.setattr(
            "workspace_provisioning.network.establish_network", observed_public_network
        )
        bootstrap = await scenario.admit(continuation_parameters(artifact))
        oid = bootstrap.grant.lease.operation_id
        with pytest.raises(Exception):
            await runtime.run_lifecycle(bootstrap, scenario.context)
        assert len(scenario.bootstrap_calls) == 1
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operations SET state='running' WHERE operation_id=$1",
                oid,
            )
        old_directory = scenario.bootstrap_calls[0][6].directory
        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE workspace_lifecycle_artifacts SET created_at=now()-interval '3 hours' WHERE artifact_id=$1",
                artifact["artifact_id"],
            )
            await connection.execute(
                "UPDATE harness_execution_audit SET recorded_at=now()-interval '3 hours' WHERE event='recovery.settled'"
            )
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',runtime_deadline=now()-interval '1 hour' WHERE operation_id=$1",
                oid,
            )
            original = dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                    oid,
                )
            )
            version = await connection.fetchval(
                "SELECT version FROM harness_operations WHERE operation_id=$1", oid
            )
        with pytest.raises(LifecycleRefused, match="expired"):
            await runtime.validate_phase(bootstrap, scenario.context)

        async def authorize(*_):
            return datetime.now(UTC) + timedelta(hours=1)

        async def prove(operation, lease, call):
            owner = {
                k: operation[k]
                for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
            }
            owner.update(
                record_id="test-owner-proof",
                evidence_kind="bootstrap-journal-barrier-v1",
                source_fence=call["fence_token"],
                source_key=call["idempotency_key"],
                source_image="test-qualified-image",
                source_hashes={"runtime": "test-qualified"},
            )
            proof = dict(
                evidence_kind=owner["evidence_kind"],
                owner_evidence_id=owner["record_id"],
                owner_evidence=owner,
                original_authority_expired=True,
                issued_sessions_expired=True,
                bootstrap_mutations_started=False,
                source_artifact_id=artifact["artifact_id"],
                source_artifact_sha256=digest(
                    {
                        k: artifact[k]
                        for k in (
                            "artifact_id",
                            "source_operation_id",
                            "artifact_metadata_json",
                            "source_payload_digest",
                        )
                    }
                ),
            )

            if denied_first_grant:
                denial = {
                    **{
                        k: operation[k]
                        for k in ("operation_id", "org_id", "workspace_id")
                    },
                    **{
                        k: "d" * 64
                        for k in (
                            "generation",
                            "claim",
                            "plan_sha256",
                            "cloudtrail_sha256",
                            "inventory_sha256",
                        )
                    },
                    "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/fixture",
                    "first_grant": {"key": "registrar-entry"},
                    "event_ids": ["verified-denial"],
                    "denied_request_verified": True,
                    "absence_verified": True,
                }
                owner.update(
                    evidence_kind="bootstrap-denied-first-grant-v1", denied_grant=denial
                )
                proof.update(
                    evidence_kind=owner["evidence_kind"],
                    bootstrap_mutations_started=True,
                    bootstrap_grants_created=False,
                    bootstrap_journal_revoked=True,
                    denied_grant=denial,
                )
            return proof

        async with harness.connect() as connection:
            receipt = await recover_uncredentialed(
                connection,
                principal=_principal(),
                operation_id=oid,
                expected_version=version,
                reauthorization_id="test-fresh-bootstrap-decision",
                authorize=authorize,
                prove_not_started=prove,
                recovery_scope="unstarted-bootstrap",
            )
        lease = await harness.lease(oid, holder="replacement", attempt="replacement")
        current = SimpleNamespace(**vars(bootstrap))
        current.grant = ExecutionGrant(_principal(lease.holder), lease)
        scenario.operations[oid] = current
        assert (await runtime.validate_phase(current, scenario.context))[3][
            "artifact_id"
        ] == artifact["artifact_id"]
        with pytest.raises(Exception):
            await runtime.run_lifecycle(current, scenario.context)
        # The real executor reached the adapter again with a distinct private directory.
        # This test's adapter refuses deliberately; it makes no readiness claim.
        assert len(scenario.bootstrap_calls) == 2
        assert scenario.bootstrap_calls[1][6].directory != old_directory
        assert old_directory.is_dir()
        async with harness.connect() as connection:
            assert (
                dict(
                    await connection.fetchrow(
                        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                        oid,
                    )
                )
                == original
            )
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 ORDER BY fence_token",
                oid,
            )
            assert (
                len(calls) == 2
                and calls[1]["idempotency_key"] == receipt["replacement_key"]
            )
            assert calls[0]["stage"] == "reconciled" and calls[1]["stage"] == "intended"
        assert lease.max_attempts == bootstrap.grant.lease.max_attempts
        assert lease.attempts == bootstrap.grant.lease.attempts + 1

        # A second interrupted call needs a new proof/decision. The existing
        # worker follows the latest receipt while retaining both earlier paths.
        async def fresh_proof(*args):
            proof = await prove(*args)
            proof["owner_evidence_id"] = f"fresh-owner-{args[1]['attempts']}"
            proof["owner_evidence"]["record_id"] = proof["owner_evidence_id"]
            if denied_first_grant:
                prior = json.loads(receipt["proof"])
                reference = {
                    "reauthorization_id": receipt["reauthorization_id"],
                    "source_fence": receipt["source_fence"],
                    "proof_sha256": digest(prior),
                    "journal": prior.get("retained_revocation", {}).get("journal")
                    or {
                        k: prior["denied_grant"][k]
                        for k in (
                            "operation_id",
                            "org_id",
                            "workspace_id",
                            "generation",
                            "claim",
                            "plan_sha256",
                            "cluster_arn",
                            "first_grant",
                        )
                    },
                }
                if args[1]["attempts"] == 6:
                    journal = {
                        **reference["journal"],
                        **{
                            k: "e" * 64
                            for k in (
                                "generation",
                                "claim",
                                "plan_sha256",
                                "revoked_progress_sha256",
                            )
                        },
                    }
                    revoked = {
                        "journals": sorted(
                            [reference["journal"], journal],
                            key=lambda j: j["generation"],
                        ),
                        "current_generation": journal["generation"],
                        "predecessor": {
                            k: reference[k]
                            for k in (
                                "reauthorization_id",
                                "source_fence",
                                "proof_sha256",
                            )
                        },
                    }
                    for value in (proof, proof["owner_evidence"]):
                        value.pop("denied_grant")
                        value.update(
                            evidence_kind="bootstrap-pre-namespace-revoked-v1",
                            revoked_bootstrap=revoked,
                        )
                    proof.update(
                        bootstrap_mutations_started=True,
                        bootstrap_grants_created=True,
                        bootstrap_journal_revoked=True,
                        bootstrap_components_started=False,
                    )
                    return proof
                for value in (proof, proof["owner_evidence"]):
                    value.pop("denied_grant")
                    value.update(
                        evidence_kind="bootstrap-retained-revocation-barrier-v1",
                        retained_revocation=reference,
                    )
                proof.pop("bootstrap_grants_created")
                proof.pop("bootstrap_journal_revoked")
                proof["bootstrap_mutations_started"] = False
                if args[1]["attempts"] == 5:
                    extension = dict(
                        authorized_by=_principal().subject,
                        authorization="Authorize five further bootstrap attempts",
                        previous_limit=5,
                        recorded_attempts=5,
                        additional_attempts=5,
                    )
                    proof["attempt_extension"] = extension
                    proof["owner_evidence"]["attempt_extension"] = dict(extension)
            return proof

        async with harness.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',"
                "runtime_deadline=now()-interval '1 hour' WHERE operation_id=$1",
                oid,
            )
            version = await connection.fetchval(
                "SELECT version FROM harness_operations WHERE operation_id=$1", oid
            )
            second = await recover_uncredentialed(
                connection,
                principal=_principal(),
                operation_id=oid,
                expected_version=version,
                reauthorization_id="second-bootstrap-decision",
                authorize=authorize,
                prove_not_started=fresh_proof,
                recovery_scope="unstarted-bootstrap",
            )
        third = await harness.lease(oid, holder="third", attempt="third")
        current.grant = ExecutionGrant(_principal(third.holder), third)
        with pytest.raises(Exception):
            await runtime.run_lifecycle(current, scenario.context)
        assert len(scenario.bootstrap_calls) == 3
        paths = [call[6].directory for call in scenario.bootstrap_calls]
        assert len(set(paths)) == 3 and all(path.is_dir() for path in paths)
        assert third.attempts == bootstrap.grant.lease.attempts + 2
        assert third.max_attempts == bootstrap.grant.lease.max_attempts
        async with harness.connect() as connection:
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 "
                "ORDER BY fence_token",
                oid,
            )
            assert [call["stage"] for call in calls] == [
                "reconciled",
                "reconciled",
                "intended",
            ]
            assert calls[-1]["idempotency_key"] == second["replacement_key"]
            assert (
                dict(
                    await connection.fetchrow(
                        "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                        oid,
                    )
                )
                == original
            )

        if denied_first_grant:
            receipt = second
            for stopped_attempt in range(3, 7):
                async with harness.connect() as connection:
                    await connection.execute(
                        "UPDATE harness_operation_leases SET expires_at=now()-interval '1 hour',"
                        "runtime_deadline=now()-interval '1 hour' WHERE operation_id=$1",
                        oid,
                    )
                    version = await connection.fetchval(
                        "SELECT version FROM harness_operations WHERE operation_id=$1",
                        oid,
                    )
                    kwargs = dict(
                        principal=_principal(),
                        operation_id=oid,
                        expected_version=version,
                        reauthorization_id=f"decision-{stopped_attempt}",
                        authorize=authorize,
                        prove_not_started=fresh_proof,
                        recovery_scope="unstarted-bootstrap",
                    )
                    next_receipt = await recover_uncredentialed(connection, **kwargs)
                    assert (
                        await recover_uncredentialed(connection, **kwargs)
                        == next_receipt
                    )
                    assert (
                        await connection.fetchval(
                            "SELECT attempts FROM harness_operation_leases WHERE operation_id=$1",
                            oid,
                        )
                        == stopped_attempt
                    )
                receipt = next_receipt
                next_lease = await harness.lease(
                    oid,
                    holder=f"worker-{stopped_attempt + 1}",
                    attempt=f"attempt-{stopped_attempt + 1}",
                )
                current.grant = ExecutionGrant(
                    _principal(next_lease.holder), next_lease
                )
                with pytest.raises(Exception):
                    await runtime.run_lifecycle(current, scenario.context)
                assert next_lease.attempts == stopped_attempt + 1
                assert next_lease.max_attempts == (10 if stopped_attempt >= 5 else 5)
                assert len(scenario.bootstrap_calls) == stopped_attempt + 1
            paths = [call[6].directory for call in scenario.bootstrap_calls]
            assert len(set(paths)) == 7 and all(path.is_dir() for path in paths)
            async with harness.connect() as connection:
                assert (
                    dict(
                        await connection.fetchrow(
                            "SELECT * FROM harness_approval_consumption WHERE operation_id=$1",
                            oid,
                        )
                    )
                    == original
                )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM harness_execution_audit WHERE operation_id=$1 AND event='execution-attempt-limit-extended'",
                        oid,
                    )
                    == 1
                )

        await recover_failed_retry(scenario, current, artifact)

    harness.run(run())
