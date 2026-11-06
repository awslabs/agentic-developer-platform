"""Isolated native prevention readback and reconciliation fixtures."""

from datetime import UTC, datetime
from dataclasses import asdict

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.devops.prevention import PreventionAdapter
from l5_services.adapters.devops.observations import readback_prevention
from l5_services.adapters.devops.spaces import SpaceAdapter, SpaceIdentityError, SpaceSelection
from l5_services.devops.lifecycle import SpaceLifecycle
from l5_services.devops.prevention import PreventionPolicy, prevention_readiness
from l5_services.models import Binding, Configuration, ConfigurationVersion, DevopsSpaceIntent
from l5_services.models.base import Base


NOW = datetime(2026, 10, 27, 9, tzinfo=UTC)


class NativeSpaces:
    def __init__(self):
        self.spaces = {}
        self.calls = []
        self.goal_state = "ENABLED"
        self.fail_readback = False
        self.fail_after_update = False
        self.drop_update = False

    def list_agent_spaces(self, **kwargs):
        return {"agentSpaces": [space["agentSpace"] for space in self.spaces.values()]}

    def create_agent_space(self, **kwargs):
        self.calls.append("create")
        self.spaces["space-example"] = {"agentSpace": {
            "agentSpaceId": "space-example", "name": kwargs["name"], "preferences": {}},
            "tags": kwargs["tags"]}
        return {"agentSpace": {"agentSpaceId": "space-example"}}

    def get_agent_space(self, *, agentSpaceId):
        self.calls.append("get")
        return self.spaces[agentSpaceId]

    def list_triggers(self, **kwargs):
        self.calls.append("list_triggers")
        return {"items": []}

    def list_goals(self, **kwargs):
        self.calls.append("list_goals")
        if self.fail_readback:
            raise ConnectionError("goal readback unavailable")
        return {"goals": [{"goalId": "weekly-example", "goalType": "ONCALL_REPORT",
                           "evaluationSchedule": {"state": self.goal_state, "expression": "rate(7 days)"}}]}

    def update_goal(self, **kwargs):
        self.calls.append(("update_goal", kwargs))
        if not self.drop_update:
            self.goal_state = kwargs["evaluationSchedule"]["state"]
        if self.fail_after_update:
            self.fail_readback = True
        return {"goal": {"goalId": kwargs["goalId"], "evaluationSchedule": {
            "state": self.goal_state, "expression": "rate(7 days)"}}}


@pytest.fixture
def selection():
    return SpaceSelection("tenant-example", "product-example", "project-example", "binding-example",
                          "123456789012", "example-region-1")


def project_settings(selection):
    project_policy = {"tenant_id": selection.tenant_id, "project_id": selection.project_id,
                      "source_connection_id": "connection-example", "record_revision": "revision-one",
                      "record_type": "policy", "record_id": "budget-example"}
    settings = {"devops": {"prevention_enabled": False, "prevention_policy": {
        "goal_id": "weekly-example", "expression": "rate(7 days)"}},
        "limits": {"max_cost": "5", "currency": "USD", "deadline_seconds": 3600,
                   "policy_ref": project_policy}, "exclusions": []}
    budget = {"enforcement_mode": "soft", "tenant_id": selection.tenant_id,
              "project_id": selection.project_id, "policy_ref": project_policy}
    return settings, budget


@pytest.mark.asyncio
async def test_prevention_defaults(tmp_path, selection):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'prevention.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = NativeSpaces()
    adapter = SpaceAdapter(provider, account_id=selection.account_id, region=selection.region)
    lifecycle = SpaceLifecycle(sessions, adapter, clock=lambda: NOW)
    try:
        await lifecycle.prepare_create(selection, "operation-example", 1)
        await lifecycle.execute_create(selection.tenant_id, "operation-example")
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.prevention_observation["native_space_id"] == "space-example"
            assert receipt.prevention_observation["native_schedule"]["enabled"] is True
        status = await lifecycle.prevention_status(selection, "operation-example", {
            "devops": {"prevention_enabled": False, "trigger_policies": []}})
        assert status["desired"] == {"enabled": False}
        assert status["observed"]["goals"][0]["goal_id"] == "weekly-example"
        assert (status["status"], status["reason"]) == ("blocked", "prevention_state_mismatch")
        assert "list_triggers" not in provider.calls
        assert prevention_readiness(PreventionPolicy(), receipt.prevention_observation,
                                    configuration_revision=2,
                                    scope_fingerprint=selection.fingerprint())["reason"] == "prevention_readback_missing"
    finally:
        await engine.dispose()


def test_default_readback_rejects_foreign_scope(selection):
    provider = NativeSpaces()
    adapter = PreventionAdapter(provider, account_id=selection.account_id, region=selection.region)
    with pytest.raises(SpaceIdentityError, match="selected binding"):
        adapter.read(SpaceSelection(selection.tenant_id, selection.product_id, "other-project",
                                    selection.binding_id, "000000000000", selection.region), "space-example", 1)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_prevention_lifecycle(tmp_path, selection):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = NativeSpaces()
    provider.goal_state = "DISABLED"
    lifecycle = SpaceLifecycle(sessions, SpaceAdapter(provider, account_id=selection.account_id,
                                                      region=selection.region), clock=lambda: NOW)
    policy = PreventionPolicy(enabled=True, goal_id="weekly-example", expression="rate(7 days)")
    paused = PreventionPolicy(enabled=False, goal_id="weekly-example", expression="rate(7 days)")
    settings, budget = project_settings(selection)
    try:
        await lifecycle.prepare_create(selection, "operation-example", 1)
        await lifecycle.execute_create(selection.tenant_id, "operation-example")
        observation = await lifecycle.configure_prevention(selection, "operation-example", policy,
                                                           action_id="enable-example", settings=settings, budget=budget)
        assert observation["native_schedule"]["enabled"] is True
        updates = [call for call in provider.calls if isinstance(call, tuple)]
        assert updates[0][1]["goalId"] == "weekly-example"
        assert updates[0][1]["evaluationSchedule"] == {"state": "ENABLED"}
        assert (await lifecycle.configure_prevention(selection, "operation-example", policy,
                                                     action_id="enable-example", settings=settings, budget=budget)) == observation
        assert len([call for call in provider.calls if isinstance(call, tuple)]) == 1
        with pytest.raises(ValueError, match="reused"):
            await lifecycle.configure_prevention(selection, "operation-example", paused,
                                                 action_id="enable-example", settings=settings)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            receipt.active_executions = ["already-running"]
            await session.commit()
        observed_pause = await lifecycle.configure_prevention(selection, "operation-example", paused,
                                                               action_id="pause-example", settings=settings)
        assert observed_pause["native_schedule"]["enabled"] is False
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.active_executions == ["already-running"]
        await lifecycle.configure_prevention(selection, "operation-example", policy,
                                             action_id="resume-example", settings=settings, budget=budget)
        assert provider.goal_state == "ENABLED"
        assert len([call for call in provider.calls if isinstance(call, tuple)]) == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_prevention_limits(tmp_path, selection):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = NativeSpaces()
    provider.goal_state = "DISABLED"
    lifecycle = SpaceLifecycle(sessions, SpaceAdapter(provider, account_id=selection.account_id,
                                                      region=selection.region), clock=lambda: NOW)
    settings, budget = project_settings(selection)
    enabled = PreventionPolicy(True, "weekly-example", "rate(7 days)")
    paused = PreventionPolicy(False, "weekly-example", "rate(7 days)")
    try:
        await lifecycle.prepare_create(selection, "operation-example", 1)
        await lifecycle.execute_create(selection.tenant_id, "operation-example")
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            original = receipt.prevention_observation
            assert original["native_schedule"]["enabled"] is False
        invalid = [
            (settings, None),
            (settings, budget | {"enforcement_mode": "hard"}),
            (settings, budget | {"project_id": "another-project"}),
            (settings | {"exclusions": [{"reason": "maintenance"}]}, budget),
            (settings | {"limits": settings["limits"] | {"max_cost": "0"}}, budget),
            (settings | {"limits": settings["limits"] | {"policy_ref": {
                **budget["policy_ref"], "project_id": "another-project"}}}, budget),
        ]
        for selected_settings, selected_budget in invalid:
            with pytest.raises(ValueError):
                await lifecycle.configure_prevention(selection, "operation-example", enabled,
                                                     action_id="refused-example", settings=selected_settings,
                                                     budget=selected_budget)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.prevention_policy == {"enabled": False}
            assert receipt.prevention_observation == original
        assert not any(isinstance(call, tuple) for call in provider.calls)

        provider.fail_after_update = True
        with pytest.raises(ConnectionError, match="readback unavailable"):
            await lifecycle.configure_prevention(selection, "operation-example", enabled,
                                                 action_id="enable-example", settings=settings, budget=budget)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.prevention_policy["state"] == "pending"
            assert receipt.prevention_observation == original
        status = await lifecycle.prevention_status(selection, "operation-example", settings)
        assert status["status"] == "blocked" and status["observed"] == original["native_schedule"]
        assert status["reason"] == "native_schedule_readback_unavailable"
        assert provider.goal_state == "ENABLED"

        provider.fail_after_update = provider.fail_readback = False
        recovered = await lifecycle.configure_prevention(selection, "operation-example", enabled,
                                                         action_id="enable-example", settings=settings,
                                                         budget=budget)
        assert recovered["native_schedule"]["enabled"] is True
        current_status = await lifecycle.prevention_status(selection, "operation-example", settings)
        assert current_status["status"] == "ready"
        assert current_status["desired"]["per_run_limits"] == settings["limits"]
        assert current_status["desired"]["cost_control"] == "observation_only"
        assert len([call for call in provider.calls if isinstance(call, tuple)]) == 1
        provider.drop_update = True
        with pytest.raises(ValueError, match="not confirmed"):
            await lifecycle.configure_prevention(selection, "operation-example", paused,
                                                 action_id="pause-example", settings=settings)
        with pytest.raises(ValueError, match="retain intent"):
            await lifecycle.reconcile_prevention(selection, "operation-example", paused)
        async with sessions() as session:
            receipt = await session.get(DevopsSpaceIntent, (selection.tenant_id, "operation-example"))
            assert receipt.prevention_policy["state"] == "pending"
            assert {key: value for key, value in receipt.prevention_observation.items() if key != "observed_at"} == {
                key: value for key, value in recovered.items() if key != "observed_at"
            }
            assert receipt.prevention_observation["observed_at"] >= recovered["observed_at"]
            assert receipt.prevention_operation_id == "pause-example"
        assert (await lifecycle.prevention_status(selection, "operation-example", settings))["status"] == "blocked"
        assert len([call for call in provider.calls if isinstance(call, tuple)]) == 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize("override", [
    {"timezone": "America/New_York"}, {"missed_run_policy": "replay"},
    {"expression": "rate(365 days)"},
])
def test_prevention_rejects_unbounded_or_replayed_cadence(override):
    with pytest.raises(ValueError):
        PreventionPolicy(**({"enabled": True, "goal_id": "weekly-example",
                            "expression": "rate(7 days)"} | override))


@pytest.mark.asyncio
async def test_prevention_occurrences_have_distinct_native_runs(tmp_path, selection):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'occurrences.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scope = {"tenant_id": selection.tenant_id, "product_id": selection.product_id,
             "project_id": selection.project_id, "relationship_revision": "1"}
    native_ref = {"source_connection_id": "connection-example", "record_type": "agent_space",
                  "record_id": "space-example"}
    policy = PreventionPolicy(True, "weekly-example", "rate(7 days)")
    identity = {"account_id": selection.account_id, "region": selection.region,
                "role_arn": "arn:aws:iam::123456789012:role/example"}

    class Provider:
        account_id, region, role_arn = identity.values()
        last_task_id = "evaluation-one"
        tasks = []

        def client(self, name):
            assert name == "devops-agent"
            return self

        def get_agent_space(self, *, agentSpaceId):
            return {"agentSpace": {"agentSpaceId": agentSpaceId}}

        def list_goals(self, **kwargs):
            return {"goals": [{"goalId": "weekly-example", "goalType": "ONCALL_REPORT",
                               "lastTaskId": self.last_task_id, "evaluationSchedule": {
                                   "state": "ENABLED", "expression": "rate(7 days)"}}]}

        def get_backlog_task(self, *, agentSpaceId, taskId):
            self.tasks.append(taskId)
            return {"task": {"agentSpaceId": agentSpaceId, "taskType": "EVALUATION",
                             "taskId": taskId, "executionId": "execution-" + taskId}}

    provider = Provider()
    try:
        async with sessions() as session:
            session.add(Configuration(tenant_id=selection.tenant_id, configuration_id="configuration-example",
                                      product_id=selection.product_id, project_id=selection.project_id,
                                      service_id="devops", relationship_revision="1", owner_id="owner-example",
                                      current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=selection.tenant_id,
                                             configuration_id="configuration-example", revision=1,
                                             settings={}, inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
            binding = Binding(tenant_id=selection.tenant_id, binding_id=selection.binding_id,
                              configuration_id="configuration-example", product_id=selection.product_id,
                              project_id=selection.project_id, service_id="devops", scope=scope,
                              revision=1, generation=1, state="ready", ownership="adopted",
                              connection_ref=native_ref, connection_key="a" * 64,
                              native_resource_ref=native_ref, native_key="b" * 64,
                              readiness=[], gaps=[], provider_identity=identity)
            session.add(binding)
            session.add(DevopsSpaceIntent(tenant_id=selection.tenant_id,
                                          operation_id="operation-example", selection=asdict(selection),
                                          configuration_revision=1, intent_hash="c" * 64, state="succeeded",
                                          native_space_id="space-example",
                                          prevention_policy=policy.as_dict() | {
                                              "state": "verified", "per_run_limits": {"max_cost": "5", "currency": "USD"},
                                              "cost_control": "observation_only"}))
            await session.commit()
            first = (await readback_prevention(session, binding, scope, provider, now=NOW))[0]
            replay = (await readback_prevention(session, binding, scope, provider, now=NOW))[0]
            assert first.run_id == replay.run_id
            provider.last_task_id = "evaluation-two"
            second = (await readback_prevention(session, binding, scope, provider, now=NOW))[0]
            assert first.run_id != second.run_id
            assert first.origin == second.origin == "native"
            assert first.admission is second.admission is None
            assert first.capture_limits == second.capture_limits == {"max_cost": "5", "currency": "USD"}
            assert first.input["native_goal_id"] == second.input["native_goal_id"] == "weekly-example"
            assert first.native_job_ref["record_id"] != second.native_job_ref["record_id"]
            assert provider.tasks == ["evaluation-one", "evaluation-one", "evaluation-two"]
    finally:
        await engine.dispose()
