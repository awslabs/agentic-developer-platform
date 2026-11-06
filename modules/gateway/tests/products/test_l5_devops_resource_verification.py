"""Resource consent, installed worker and authentic Product workload references."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from l5_services import runtime_support
from l5_services.models.binding import Binding
from l5_services.models.devops_space import DevopsSpaceIntent

from tests.products.test_l5_devops_setup_activation import (
    activation_settings,
    complete_setup,
)
from tests.products.test_l5_devops_setup_activation import (
    api_setup as api_setup,
)
from tests.products.test_l5_devops_setup_activation import (
    l5_owner as l5_owner,
)

__all__ = ["api_setup", "l5_owner"]


async def assert_report_recovery_after_consent_expiry(client, db, setup, run, original, parent, prior_work):
    from fastapi import HTTPException
    from l5_services.models.work import Work
    from sqlalchemy import select

    from src.auth.l5_execution_scope import current_execution_scope
    from src.internal.l5_credential_routes import CredentialSessionRequest, active_grant, get_l5_work_authorizer
    from src.shared.models.vault import CredentialServiceDelegation

    now = datetime.now(UTC)
    old = (run.deadline_at, deepcopy(run.monitoring), original.expires_at, parent.expires_at)
    admission, inputs, native = deepcopy(run.admission), deepcopy(run.input), deepcopy(run.native_job_ref)
    run.state, run.deadline_at = "expired", now - timedelta(hours=1)
    original.expires_at = parent.expires_at = now - timedelta(minutes=1)
    prior_work.completed_at, prior_work.lease_until = now, None
    await db.commit()
    request = {"reason": "Collect this existing report", "authorize_readback": True, "expected_revision": run.revision, "native_job_ref": native}
    headers = {"Idempotency-Key": "devops_report_fresh_consent_01"}
    path = setup.path + "/runs/" + run.run_id + "/reconcile"
    rejected = await client.post(path, json={**request, "expected_revision": run.revision + 1}, headers=headers)
    assert rejected.status_code == 412, rejected.text
    recovered = await client.post(path, json=request, headers=headers)
    assert recovered.status_code == 202, recovered.text
    replay = await client.post(path, json=request, headers=headers)
    assert replay.status_code == 202 and replay.json() == recovered.json()
    conflict = await client.post(path, json={**request, "reason": "Changed request"}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    await db.refresh(run)
    authority = run.monitoring["devops_recovery"]["authorization"]
    fresh = await db.get(CredentialServiceDelegation, authority["delegation_id"], populate_existing=True)
    assert fresh.id != original.id and fresh.parent_delegation_id is None
    assert fresh.operations == ["product:read", "credential:session"]
    assert fresh.expires_at.replace(tzinfo=UTC) <= now + timedelta(minutes=16)
    assert original.expires_at.replace(tzinfo=UTC) < now and parent.expires_at.replace(tzinfo=UTC) < now
    assert run.admission == admission and run.input == inputs and run.native_job_ref == native
    assert run.deadline_at.replace(tzinfo=UTC) == now - timedelta(hours=1)
    assert "authorization" not in str(recovered.json().get("monitoring"))
    assert await current_execution_scope(db, fresh)
    work = await db.scalar(select(Work).where(Work.tenant_id == run.tenant_id, Work.run_id == run.run_id, Work.step == run.revision))
    assert work.kind in {"poll", "collect"}
    work.lease_owner, work.fence, work.lease_until = "report-worker", 1, now + timedelta(minutes=3)
    await db.commit()
    body = CredentialSessionRequest(
        delegation_id=fresh.id,
        delegation_revision=fresh.revision,
        operation_id=run.run_id,
        attempt_id=work.work_id,
        lease_owner=work.lease_owner,
        lease_fence=1,
        connection_ref={**setup.fixture.reference, "record_type": "credential"},
    )
    permit = await get_l5_work_authorizer(db)(fresh, body)
    assert "CreateBacklogTask" not in permit.session_policy and "UpdateBacklogTask" not in permit.session_policy
    active_grant(fresh, body, permit, minimum_remaining=timedelta(seconds=1))
    expiry = fresh.expires_at
    for fault in ("submit", "cancel", "expired", "revoked", "original_revoked", "parent_revoked", "foreign_job", "stale_fence"):
        if fault in {"submit", "cancel"}:
            work.kind = fault
        elif fault == "expired":
            fresh.expires_at = now - timedelta(seconds=1)
        elif fault == "revoked":
            fresh.revoked_at = now
        elif fault == "original_revoked":
            original.revoked_at = now
        elif fault == "parent_revoked":
            parent.revoked_at = now
        elif fault == "foreign_job":
            run.native_job_ref = {**native, "record_id": "foreign"}
        elif fault == "stale_fence":
            work.fence = 2
        await db.commit()
        with pytest.raises(HTTPException):
            await get_l5_work_authorizer(db)(fresh, body)
        work.kind, work.fence = "poll", 1
        fresh.expires_at, fresh.revoked_at = expiry, None
        original.revoked_at = parent.revoked_at = None
        run.native_job_ref = native
        await db.commit()
    parent.revoked_at = now
    work.completed_at, work.lease_until = now, None
    await db.commit()
    rejected = await client.post(
        path, json={**request, "expected_revision": run.revision}, headers={"Idempotency-Key": "devops_report_revoked_consent"}
    )
    assert rejected.status_code == 403, rejected.text
    parent.revoked_at = None
    run.deadline_at, run.monitoring, original.expires_at, parent.expires_at = old
    run.state = "running"
    await db.commit()
    setup.native.create_backlog_task.assert_not_called()


async def resource_consent(client, setup, operation, selection, key="devops_resource_consent_01"):
    request = {
        "binding_id": operation["binding_id"],
        "for_control": True,
        "control_action": "devops_resource_verification",
        "resource_selection": selection,
    }
    preview = await client.post(setup.path + "/delegation-scope", json=request)
    assert preview.status_code == 200, preview.text
    grant = await client.post(
        setup.path + "/delegations",
        json={**request, "scope_digest": preview.json()["scope_digest"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": key},
    )
    assert grant.status_code == 200, grant.text
    return grant.json()


@pytest.fixture
async def resource_setup(test_client, api_setup, monkeypatch):
    setup = api_setup
    consent, operation = await complete_setup(test_client, setup)
    revoked = await test_client.delete(setup.path + "/delegations/" + consent["delegation_id"], headers={"If-Match": '"1"'})
    assert revoked.status_code == 200, revoked.text
    selection = {
        "account_id": "123456789012",
        "region": "us-east-1",
        "role_arn": "arn:aws:iam::123456789012:role/resource",
        "setup_role_arn": setup.fixture.role,
        "connection_ref": {**setup.fixture.reference, "record_type": "credential"},
        "workload_kind": "lambda",
        "cluster_name": None,
        "namespace": None,
        "resource_arn": "arn:aws:lambda:us-east-1:123456789012:function:dedicated",
        "telemetry_arn": "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/dedicated:*",
    }
    iam, function, logs = Mock(), Mock(), Mock()
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "aidevops.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": "123456789012"},
                    "ArnEquals": {"aws:SourceArn": "arn:aws:aidevops:us-east-1:123456789012:agentspace/new-space"},
                },
            }
        ],
    }
    iam.get_role.return_value = {"Role": {"Arn": selection["role_arn"], "AssumeRolePolicyDocument": trust}}
    iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
    iam.list_role_policies.return_value = {"PolicyNames": ["bounded"]}
    iam.get_role_policy.return_value = {
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "lambda:GetFunction", "Resource": selection["resource_arn"]},
                {"Effect": "Allow", "Action": "logs:FilterLogEvents", "Resource": selection["telemetry_arn"]},
            ],
        }
    }
    iam.simulate_principal_policy.return_value = {"EvaluationResults": [{"EvalDecision": "allowed"}]}
    function.get_function.return_value = {"Configuration": {"FunctionArn": selection["resource_arn"]}}
    logs.filter_log_events.return_value = {"events": [{"message": "fixture error"}]}
    association = {
        "associationId": "association-one",
        "agentSpaceId": "new-space",
        "serviceId": "aws",
        "status": "valid",
        "configuration": {"aws": {"accountId": selection["account_id"], "accountType": "monitor", "assumableRoleArn": selection["role_arn"]}},
    }
    associations = []
    setup.native.list_associations.side_effect = lambda **kwargs: {"associations": associations.copy()}

    def associate(**kwargs):
        associations.append(deepcopy(association))
        return {"association": association}

    setup.native.associate_service.side_effect = associate
    setup.native.get_association.return_value = {"association": association}
    clients = {"devops-agent": setup.native, "iam": iam, "lambda": function, "logs": logs}
    monkeypatch.setattr(runtime_support, "provider_session", lambda credentials, region: SimpleNamespace(client=lambda name, **kwargs: clients[name]))
    return SimpleNamespace(setup=setup, operation=operation, selection=selection, iam=iam, function=function, logs=logs, association=association)


async def begin_verification(client, db, fixture):
    grant = await resource_consent(client, fixture.setup, fixture.operation, fixture.selection)
    binding = await db.get(Binding, (fixture.setup.tenant, fixture.operation["binding_id"]), populate_existing=True)
    accepted = await client.post(
        fixture.setup.path + "/binding-operations",
        json={"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id, "resource_selection": fixture.selection},
        headers={"If-Match": f'"{binding.revision}"', "Idempotency-Key": "devops_resource_check_0001"},
    )
    assert accepted.status_code == 202, accepted.text
    return grant, accepted.json()


@pytest.mark.parametrize("api_setup", ["create", "adopt"], indirect=True)
async def test_resource_worker_and_authentic_selection(test_client, db_session, resource_setup, monkeypatch):
    item, setup = resource_setup, resource_setup.setup
    before = await db_session.get(DevopsSpaceIntent, (setup.tenant, item.operation["operation_id"]), populate_existing=True)
    old_goal = before.prevention_observation["observed_at"]
    grant, accepted = await begin_verification(test_client, db_session, item)
    assert grant["operations"] == ["binding:verify-resource", "credential:session"] and grant["capabilities"] == []
    assert await setup.worker.execute(setup.messages.pop())
    completed = await test_client.get(setup.path + "/binding-operations/" + accepted["operation_id"])
    assert completed.json()["state"] == "succeeded", completed.text
    resources = await test_client.get(setup.path + "/bindings/" + item.operation["binding_id"] + "/resources")
    assert resources.status_code == 200, resources.text
    refs = resources.json()
    assert refs["native_readiness"] == "verifying"
    assert refs["resource_refs"][0]["record_id"] == item.selection["resource_arn"]
    assert refs["telemetry_refs"][0]["record_id"] == item.selection["telemetry_arn"]
    refreshed = await db_session.get(DevopsSpaceIntent, (setup.tenant, item.operation["operation_id"]), populate_existing=True)
    assert refreshed.prevention_observation["observed_at"] > old_goal
    assert refreshed.resource_observation["effective_scope"]["policy_snapshot"]["role_arn"] == item.selection["role_arn"]
    assert setup.native.create_agent_space.call_count == int(setup.draft["settings"]["devops"]["space_mode"] == "create")
    setup.native.update_goal.assert_not_called()
    setup.native.create_backlog_task.assert_not_called()
    setup.native.associate_service.assert_called_once_with(agentSpaceId="new-space", serviceId="aws", configuration=item.association["configuration"])
    policy = json.loads(setup.assumed.call_args.kwargs["session_policy"])
    actions = [
        action
        for statement in policy["Statement"]
        for action in (statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    ]
    assert len(setup.assumed.call_args.kwargs["session_policy"]) <= 2048
    assert "aidevops:AssociateService" in actions
    assert not any(action.startswith("iam:Put") or action in {"aidevops:CreateBacklogTask", "aidevops:UpdateGoal"} for action in actions)
    settings = await activation_settings(db_session, setup, item.operation)
    settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = refs["resource_refs"]
    settings["devops"]["trigger_policies"][0].update(resource_refs=refs["resource_refs"], telemetry_refs=refs["telemetry_refs"])
    activated = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + setup.draft["hash"] + '"', "Idempotency-Key": "devops_resource_activate_01"},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["state"] == "ready"
    revoked = await test_client.delete(setup.path + "/delegations/" + grant["delegation_id"], headers={"If-Match": '"1"'})
    assert revoked.status_code == 200, revoked.text
    preview = await test_client.get(setup.path + "/delegation-scope", params={"binding_id": item.operation["binding_id"]})
    assert preview.status_code == 200, preview.text
    normal = await test_client.post(
        setup.path + "/delegations",
        json={
            "scope_digest": preview.json()["scope_digest"],
            "binding_id": item.operation["binding_id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers={"If-Match": preview.headers["etag"], "Idempotency-Key": "devops_resource_execution_01"},
    )
    assert normal.status_code == 200, normal.text
    run = await test_client.post(
        setup.path + "/runs",
        json={
            "service_id": "devops",
            "configuration_revision": 2,
            "binding_id": item.operation["binding_id"],
            "capability": "investigation",
            "devops_input": {
                "kind": "investigation",
                "question": "Investigate the selected failed fixture invocation.",
                "resource_refs": refs["resource_refs"],
                "telemetry_refs": refs["telemetry_refs"],
            },
        },
        headers={"Idempotency-Key": "devops_resource_run_0001"},
    )
    assert run.status_code == 202, run.text
    # Admission is permission to run; no native task is submitted by this test.
    setup.native.create_backlog_task.assert_not_called()
    from src.auth.l5_execution_scope import current_execution_scope
    from src.shared.models.vault import CredentialServiceDelegation

    execution = await db_session.get(CredentialServiceDelegation, normal.json()["delegation_id"])
    assert await current_execution_scope(db_session, execution)
    # Simulate the durable native acceptance while preserving the original proof
    # bytes. Moving the clock must stop new work, but not lose this known job.
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.run import Run

    stored = await db_session.get(Run, (setup.tenant, run.json()["run_id"]))
    child = await db_session.get(CredentialServiceDelegation, stored.admission["delegation_id"])
    native_job = {**setup.fixture.reference, "record_type": "native_job", "record_id": "execution-one", "record_revision": "task-one"}
    stored.native_job_ref, stored.state = native_job, "running"
    child.operation_id = stored.run_id
    attempt = WorkerAttempt(
        tenant_id=setup.tenant,
        attempt_id="accepted-attempt",
        run_id=stored.run_id,
        operation="submit",
        first_work_id="first-work",
        first_fence=1,
        idempotency_token="a" * 64,
        input_hash="b" * 64,
        started_at=datetime.now(UTC),
        status="accepted",
        native_job_ref=native_job,
    )
    db_session.add(attempt)
    await db_session.commit()
    future = datetime.now(UTC) + timedelta(minutes=20)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    with monkeypatch.context() as clock:
        clock.setattr("src.l5_devops_resources.datetime", Later)
        assert not await current_execution_scope(db_session, execution)
        assert await current_execution_scope(db_session, child)
        from l5_services.models.work import Work
        from sqlalchemy import select

        from src.internal.l5_credential_routes import CredentialSessionRequest, get_l5_work_authorizer

        work = await db_session.scalar(select(Work).where(Work.tenant_id == setup.tenant, Work.run_id == stored.run_id))
        work.kind, work.lease_owner, work.fence = "poll", "fixture-worker", 1
        work.lease_until = datetime.now(UTC) + timedelta(minutes=5)
        child.attempt_id, child.lease_fence = work.work_id, 1
        await db_session.commit()
        permit = await get_l5_work_authorizer(db_session)(
            child,
            CredentialSessionRequest(
                delegation_id=child.id,
                delegation_revision=child.revision,
                operation_id=stored.run_id,
                attempt_id=work.work_id,
                lease_owner="fixture-worker",
                lease_fence=1,
                connection_ref={**setup.fixture.reference, "record_type": "credential"},
            ),
        )
        assert "CreateBacklogTask" not in permit.session_policy and "UpdateBacklogTask" not in permit.session_policy
        # Explicit late recovery reads the accepted task under current consent;
        # it cannot renew that consent or gain submission/cancellation access.
        from fastapi import HTTPException
        from l5_services.devops.recovery import request_recovery

        request = CredentialSessionRequest(
            delegation_id=child.id,
            delegation_revision=child.revision,
            operation_id=stored.run_id,
            attempt_id=work.work_id,
            lease_owner="fixture-worker",
            lease_fence=1,
            connection_ref={**setup.fixture.reference, "record_type": "credential"},
        )
        old_deadline, old_monitoring, old_expiry = stored.deadline_at, stored.monitoring, child.expires_at
        stored.state = "expired"
        stored.deadline_at = datetime.now(UTC) - timedelta(hours=1)
        stored.monitoring = request_recovery(stored, datetime.now(UTC))
        await db_session.commit()
        late = await get_l5_work_authorizer(db_session)(child, request)
        assert "CreateBacklogTask" not in late.session_policy and "UpdateBacklogTask" not in late.session_policy
        assert datetime.now(UTC) < late.expires_at <= work.lease_until.replace(tzinfo=UTC)
        for fault in ("submit", "cancel", "cancelled", "expired_grant", "parent_revoked", "elapsed_window", "foreign_job", "stale_fence"):
            if fault in {"submit", "cancel"}:
                work.kind = fault
            elif fault == "cancelled":
                stored.state = "cancelled"
            elif fault == "expired_grant":
                child.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            elif fault == "parent_revoked":
                execution.revoked_at = datetime.now(UTC)
            elif fault == "elapsed_window":
                stored.monitoring = request_recovery(stored, datetime.now(UTC) - timedelta(minutes=16))
            elif fault == "foreign_job":
                stored.native_job_ref = {**native_job, "record_id": "another-execution"}
            elif fault == "stale_fence":
                work.fence = 2
            await db_session.commit()
            with pytest.raises(HTTPException):
                await get_l5_work_authorizer(db_session)(child, request)
            work.kind, work.fence = "poll", 1
            stored.state, stored.native_job_ref = "expired", native_job
            stored.monitoring = request_recovery(stored, datetime.now(UTC))
            child.expires_at, execution.revoked_at = old_expiry, None
            await db_session.commit()
        stored.state, stored.deadline_at, stored.monitoring = "running", old_deadline, old_monitoring
        await db_session.commit()
        for changed in (None, {**native_job, "record_id": "different-execution"}):
            stored.native_job_ref = changed
            await db_session.commit()
            assert not await current_execution_scope(db_session, child)
        stored.native_job_ref = native_job
        admitted = stored.admission["admitted_at"]
        stored.admission = {**stored.admission, "admitted_at": future.isoformat()}
        await db_session.commit()
        assert not await current_execution_scope(db_session, child)
        stored.admission = {**stored.admission, "admitted_at": admitted}
        await db_session.commit()
        assert await current_execution_scope(db_session, child)
        await assert_report_recovery_after_consent_expiry(test_client, db_session, setup, stored, child, execution, work)
    observed = deepcopy(refreshed.resource_observation)
    observed["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    refreshed.resource_observation = observed
    await db_session.commit()
    assert not await current_execution_scope(db_session, execution)
    assert not await current_execution_scope(db_session, child)  # Changed proof is not the admitted proof.
    # Viewing saved setup/history must not depend on an expired provider check.
    current = await test_client.get(setup.path + "/configuration")
    assert current.status_code == 200, current.text
    assert current.json()["settings"] == activated.json()["settings"]
    bindings = await test_client.get(setup.path + "/bindings")
    assert bindings.status_code == 200, bindings.text
    detail = await test_client.get(setup.path + "/bindings/" + item.operation["binding_id"])
    assert detail.status_code == 200, detail.text
    # A read does not renew readiness or authorize saving those expired pins.
    expired_save = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + activated.json()["hash"] + '"', "Idempotency-Key": "devops_expired_pins_save"},
    )
    assert expired_save.status_code == 409, expired_save.text
    assert not await current_execution_scope(db_session, execution)
    with monkeypatch.context() as denied:
        from unittest.mock import AsyncMock

        denied.setattr("src.l5_integration.authorized_connections", AsyncMock(return_value=[]))
        assert (await test_client.get(setup.path + "/configuration")).status_code == 404
    # Expired readbacks do not trap the owner in an unrecoverable configuration.
    settings["enabled"] = False
    settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = []
    settings["devops"]["trigger_policies"] = []
    disabled = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + activated.json()["hash"] + '"', "Idempotency-Key": "devops_resource_disable_01"},
    )
    assert disabled.status_code == 200, disabled.text
    assert (await test_client.delete(setup.path + "/delegations/" + normal.json()["delegation_id"], headers={"If-Match": '"1"'})).status_code == 200
    await resource_consent(test_client, setup, item.operation, item.selection, key="devops_resource_consent_refresh")
    binding = await db_session.get(Binding, (setup.tenant, item.operation["binding_id"]), populate_existing=True)
    checked = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "check", "configuration_revision": 3, "binding_id": binding.binding_id, "resource_selection": item.selection},
        headers={"If-Match": f'"{binding.revision}"', "Idempotency-Key": "devops_resource_check_refresh"},
    )
    assert checked.status_code == 202, checked.text
    # The admitted run remains in the queue. Execute only the setup envelope.
    assert await setup.worker.execute(setup.messages.pop())
    refreshed_options = await test_client.get(setup.path + "/bindings/" + binding.binding_id + "/resources")
    assert refreshed_options.status_code == 200, refreshed_options.text
    settings["enabled"] = True
    settings["devops"]["accounts"][0]["environments"][0]["resource_refs"] = refreshed_options.json()["resource_refs"]
    settings["devops"]["trigger_policies"] = [
        {
            "trigger_id": "manual-investigation",
            "kind": "manual",
            "enabled": True,
            "capability": "investigation",
            "binding_id": binding.binding_id,
            "environment_ref": setup.environment,
            "resource_refs": refreshed_options.json()["resource_refs"],
            "telemetry_refs": refreshed_options.json()["telemetry_refs"],
        }
    ]
    reactivated = await test_client.put(
        setup.path + "/configuration",
        json=settings,
        headers={"If-Match": '"' + disabled.json()["hash"] + '"', "Idempotency-Key": "devops_resource_reactivate_01"},
    )
    assert reactivated.status_code == 200, reactivated.text
    assert setup.native.create_agent_space.call_count == int(setup.draft["settings"]["devops"]["space_mode"] == "create")
    setup.native.associate_service.assert_called_once()
    setup.native.create_backlog_task.assert_not_called()
    original = await db_session.get(DevopsSpaceIntent, (setup.tenant, item.operation["operation_id"]), populate_existing=True)
    assert original.configuration_revision == 1 and original.prevention_observation["configuration_revision"] == 3


@pytest.mark.parametrize("api_setup", ["adopt_enabled"], indirect=True)
async def test_adoption_recovers_same_link_after_separately_disabled_goal(test_client, db_session, resource_setup):
    from l5_services.devops.setup import original_setup
    from l5_services.models.setup import BindingOperation, SetupIntent
    from sqlalchemy import select

    item, setup = resource_setup, resource_setup.setup
    binding = await db_session.get(Binding, (setup.tenant, item.operation["binding_id"]), populate_existing=True)
    assert binding.state == "blocked" and binding.gaps == ["native_prevention_enabled"]
    original = await original_setup(db_session, binding)
    assert original.operation_id == item.operation["operation_id"]
    assert original.prevention_observation["native_schedule"]["enabled"] is True
    setup.native.update_goal.assert_not_called()
    # Model an independently authorized owner action outside ADP's link/check path.
    setup.state["goal_state"] = "DISABLED"
    _, checked = await begin_verification(test_client, db_session, item)
    assert await setup.worker.execute(setup.messages.pop())
    completed = await test_client.get(setup.path + "/binding-operations/" + checked["operation_id"])
    assert completed.json()["state"] == "succeeded", completed.text
    await db_session.refresh(binding)
    assert binding.state == "ready" and binding.gaps == []
    refreshed = await original_setup(db_session, binding)
    assert refreshed.operation_id == original.operation_id and refreshed.configuration_revision == 1
    assert refreshed.prevention_observation["native_schedule"]["enabled"] is False
    links = (await db_session.scalars(select(SetupIntent).where(SetupIntent.request["action"].as_string() == "link"))).all()
    assert [row.operation_id for row in links] == [original.operation_id]
    linked = await db_session.get(BindingOperation, (setup.tenant, original.operation_id), populate_existing=True)
    assert linked.state == "succeeded" and not linked.gaps
    assert binding.ownership == "customer_linked" and binding.creation_receipt is None
    setup.native.create_agent_space.assert_not_called()
    setup.native.update_goal.assert_not_called()
    setup.native.delete_agent_space.assert_not_called()


@pytest.mark.parametrize("change", ["revoke", "connection", "purpose", "operations", "role", "membership"])
async def test_resource_worker_rechecks_current_authority(test_client, db_session, resource_setup, l5_owner, change):
    from sqlalchemy import delete

    from src.shared.models.organization import TeamMembership
    from src.shared.models.vault import CredentialServiceDelegation

    item, setup = resource_setup, resource_setup.setup
    view, _ = await begin_verification(test_client, db_session, item)
    grant = await db_session.get(CredentialServiceDelegation, view["delegation_id"])
    if change == "revoke":
        grant.revoked_at = datetime.now(UTC)
    elif change == "connection":
        setup.fixture.credential.aws_verified_version_id = "changed"
    elif change == "purpose":
        grant.execution_scope = {**grant.execution_scope, "purpose": "devops_setup"}
    elif change == "operations":
        grant.operations = [*grant.operations, "product:read"]
    elif change == "role":
        grant.execution_scope = {
            **grant.execution_scope,
            "resource_selection": {**grant.execution_scope["resource_selection"], "role_arn": "arn:aws:iam::123456789012:role/other"},
        }
    else:
        await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == l5_owner.user_id))
    await db_session.commit()
    setup.native.reset_mock()
    setup.assumed.reset_mock()
    assert not await setup.worker.execute(setup.messages.pop())
    assert setup.native.mock_calls == []
    assert item.iam.mock_calls == []
    setup.assumed.assert_not_called()


@pytest.mark.parametrize("change", ["account", "role", "role_wildcard", "connection", "function", "logs", "kind"])
async def test_resource_preview_rejects_unbounded_selection(test_client, resource_setup, change):
    item = resource_setup
    selection = deepcopy(item.selection)
    if change == "account":
        selection["account_id"] = "000000000001"
    elif change == "role":
        selection["role_arn"] = selection["setup_role_arn"]
    elif change == "role_wildcard":
        selection["role_arn"] = "arn:aws:iam::123456789012:role/*"
    elif change == "connection":
        selection["connection_ref"]["record_revision"] = "foreign"
    elif change == "function":
        selection["resource_arn"] += "*"
    elif change == "logs":
        selection["telemetry_arn"] = "*"
    else:
        selection["workload_kind"] = "eks"
    response = await test_client.post(
        item.setup.path + "/delegation-scope",
        json={"control_action": "devops_resource_verification", "binding_id": item.operation["binding_id"], "resource_selection": selection},
    )
    assert response.status_code == 422, response.text
    item.setup.native.associate_service.assert_not_called()


@pytest.mark.parametrize("failure", ["role_scope", "managed_policy", "function", "logs", "association"])
async def test_resource_verification_retains_actionable_failure(test_client, db_session, resource_setup, failure):
    item, setup = resource_setup, resource_setup.setup
    if failure == "role_scope":
        item.iam.get_role.return_value["Role"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]["ArnEquals"]["aws:SourceArn"] += "other"
    elif failure == "managed_policy":
        item.iam.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::123456789012:policy/extra"}]}
    elif failure == "function":
        item.function.get_function.return_value = {"Configuration": {"FunctionArn": "other"}}
    elif failure == "logs":
        item.logs.filter_log_events.return_value = {}
    else:
        item.association["configuration"]["aws"]["assumableRoleArn"] += "other"
    _, accepted = await begin_verification(test_client, db_session, item)
    assert await setup.worker.execute(setup.messages.pop())
    binding = await db_session.get(Binding, (setup.tenant, item.operation["binding_id"]), populate_existing=True)
    assert binding.state == "blocked" and binding.gaps
    expected = {
        "role_scope": "resource_role_trust_outside_selected_space",
        "managed_policy": "resource_role_requires_inline_policy",
        "function": "resource_readback_outside_selection",
        "logs": "telemetry_readback_incomplete",
        "association": "association_readback_mismatch",
    }
    assert binding.gaps == [expected[failure]]
    resources = await test_client.get(setup.path + "/bindings/" + binding.binding_id + "/resources")
    assert resources.status_code == 409, resources.text
    item.iam.get_policy.assert_not_called()
    setup.native.create_backlog_task.assert_not_called()
    if failure != "association":
        setup.native.associate_service.assert_not_called()


async def test_resource_lost_association_response_reconciles_without_recreation(test_client, db_session, resource_setup):
    item, setup = resource_setup, resource_setup.setup
    original = setup.native.associate_service.side_effect

    def lost(**kwargs):
        original(**kwargs)
        raise TimeoutError("lost association response")

    setup.native.associate_service.side_effect = lost
    grant, _ = await begin_verification(test_client, db_session, item)
    assert await setup.worker.execute(setup.messages.pop())
    receipt = await db_session.get(DevopsSpaceIntent, (setup.tenant, item.operation["operation_id"]), populate_existing=True)
    assert receipt.resource_observation["reason"] == "association_outcome_unknown"
    assert (await test_client.delete(setup.path + "/delegations/" + grant["delegation_id"], headers={"If-Match": '"1"'})).status_code == 200
    await resource_consent(test_client, setup, item.operation, item.selection, key="devops_resource_consent_retry")
    binding = await db_session.get(Binding, (setup.tenant, item.operation["binding_id"]), populate_existing=True)
    response = await test_client.post(
        setup.path + "/binding-operations",
        json={"action": "check", "configuration_revision": 1, "binding_id": binding.binding_id, "resource_selection": item.selection},
        headers={"If-Match": f'"{binding.revision}"', "Idempotency-Key": "devops_resource_check_retry"},
    )
    assert response.status_code == 202, response.text
    assert await setup.worker.execute(setup.messages.pop())
    resources = await test_client.get(setup.path + "/bindings/" + binding.binding_id + "/resources")
    assert resources.status_code == 200, resources.text
    setup.native.associate_service.assert_called_once()


async def test_resource_stale_lease_denied_before_provider_call(test_client, db_session, resource_setup, monkeypatch):
    from l5_services.models.setup import SetupWork
    from sqlalchemy import update

    item, setup = resource_setup, resource_setup.setup
    _, accepted = await begin_verification(test_client, db_session, item)

    async def change_fence():
        async with setup.sessions() as db:
            await db.execute(update(SetupWork).where(SetupWork.operation_id == accepted["operation_id"]).values(fence=SetupWork.fence + 1))
            await db.commit()

    original_authorize = setup.worker.binding_authorize

    async def authorize(intent, claim):
        result = await original_authorize(intent, claim)
        await change_fence()
        return result

    setup.worker.binding_authorize = authorize
    setup.native.reset_mock()
    assert not await setup.worker.execute(setup.messages.pop())
    assert setup.native.mock_calls == [] and item.iam.mock_calls == []
