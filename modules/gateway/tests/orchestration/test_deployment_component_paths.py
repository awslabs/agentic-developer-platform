"""Code-owned component selection for tools deployment."""

import hashlib
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.orchestration.deployment_manifest import EntryStatus, PhysicalTarget, TargetEvidence, load_packaged_manifest, resolve_manifest_entry
from src.orchestration.deployment_workflows import (
    WorkflowServices,
    _require_ungrouped_release,
    canonical,
    components_for_paths,
    select_deployment_entries,
)
from src.orchestration.review_cycle import CycleBlockedError


@pytest.mark.parametrize(
    ("path", "component"),
    [
        ("modules/tools/platform/gateway.tf", "tools-platform"),
        ("modules/tools/facade/server.py", "tools-facade"),
        ("modules/tools/scripts/facade_receipt.py", "tools-facade"),
        (".github/workflows/tools-facade-deploy.yml", "tools-facade"),
        ("modules/tools/interceptor/handler.py", "tools-interceptor"),
        ("modules/agent-context/door/server.py", "knowledge-door"),
    ],
)
def test_supported_tools_path_selects_exact_component(path, component):
    assert components_for_paths([path]) == (component,)


def test_platform_change_is_not_skipped_among_documentation():
    assert components_for_paths(["docs/tools/architecture.md", "modules/tools/platform/main.tf"]) == ("tools-platform",)


def test_unsupported_tools_path_is_rejected():
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["modules/tools/other/x.py"])


def test_unsupported_tools_path_is_not_skipped_among_supported_paths():
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["docs/tools/README.md", "modules/tools/platform/main.tf", "modules/tools/other/x.py"])


@pytest.mark.parametrize(
    ("path", "entry_id", "workflow_path"),
    [
        ("modules/user-services/dev-box/infra/main.tf", "adp-dev-dev-box", ".github/workflows/dev-box-deploy.yml"),
        (".github/workflows/dev-box-ci.yml", "adp-dev-dev-box", ".github/workflows/dev-box-deploy.yml"),
        (".github/workflows/dev-box-deploy.yml", "adp-dev-dev-box", ".github/workflows/dev-box-deploy.yml"),
        ("modules/tools/platform/main.tf", "adp-dev-tools-platform", ".github/workflows/tools-platform-deploy.yml"),
        ("modules/tools/facade/server.py", "adp-dev-tools-facade", ".github/workflows/tools-facade-deploy.yml"),
        ("modules/tools/interceptor/handler.py", "adp-dev-tools-interceptor", ".github/workflows/tools-interceptor-deploy.yml"),
        ("modules/agent-context/door/server.py", "adp-dev-knowledge-door", ".github/workflows/agent-context-deploy.yml"),
    ],
)
def test_changed_path_reaches_exactly_one_packaged_workflow(path, entry_id, workflow_path):
    components = components_for_paths([path])
    manifest = load_packaged_manifest()
    enabled = {entry.entry_id: entry for entry in manifest.entries if entry.status is EntryStatus.ENABLED}
    if entry_id in enabled:
        # An enabled entry is selectable only for a policy that permits its connection
        # (the dev tools-platform grant, 2026-10-14); a policy without it sees no
        # approved manifest rather than silently falling back to another entry.
        with pytest.raises(CycleBlockedError, match="deployment_component_has_no_approved_manifest"):
            select_deployment_entries(manifest, components, frozenset())
        entries = select_deployment_entries(manifest, components, frozenset({enabled[entry_id].connection_id}))
        assert tuple(entries) == (entry_id,)
        assert entries[entry_id].workflow.path == workflow_path
        return
    entries = select_deployment_entries(manifest, components, frozenset())
    assert tuple(entries) == (entry_id,)
    assert entries[entry_id].workflow.path == workflow_path
    assert entries[entry_id].status is EntryStatus.UNRESOLVED


def test_facade_change_routes_to_its_own_enabled_manifest():
    components = components_for_paths(["modules/tools/facade/server.py"])
    manifest = load_packaged_manifest()
    facade = next(entry for entry in manifest.entries if entry.entry_id == "adp-dev-tools-facade")
    assert facade.status is EntryStatus.ENABLED
    entries = select_deployment_entries(manifest, components, frozenset({facade.connection_id}))
    assert tuple(entries) == ("adp-dev-tools-facade",)
    # A policy without the dev connection sees no approved manifest, never another entry.
    with pytest.raises(CycleBlockedError, match="deployment_component_has_no_approved_manifest"):
        select_deployment_entries(manifest, components, frozenset())


def test_ordinary_gateway_discovery_excludes_foundation_targets():
    manifest = load_packaged_manifest()
    ordinary = manifest.entry("adp-dev-gateway")
    entries = select_deployment_entries(manifest, ("gateway-backend",), {ordinary.connection_id})
    assert set(entries) == {"adp-dev-gateway", "adp-dev-embark1-gateway"}
    assert entries[ordinary.entry_id] == ordinary


@pytest.mark.parametrize("component", ["gateway-backend", "tool-adapters"])
def test_grouped_only_coverage_is_not_an_ordinary_deployment(component):
    manifest = load_packaged_manifest()
    grouped = replace(manifest, entries=tuple(entry for entry in manifest.entries if entry.release_group is not None))
    connections = {entry.connection_id for entry in grouped.entries}
    with pytest.raises(CycleBlockedError, match="deployment_component_has_no_approved_manifest"):
        select_deployment_entries(grouped, (component,), connections)


@pytest.mark.parametrize("include_foundation", [False, True])
async def test_packaged_gateway_snapshot_reaches_ordinary_workflow(monkeypatch, include_foundation):
    manifest = load_packaged_manifest()
    ordinary = manifest.entry("adp-dev-gateway")
    if not include_foundation:
        manifest = replace(manifest, entries=tuple(entry for entry in manifest.entries if entry.release_group is None))
    now = datetime.now(UTC)
    node = SimpleNamespace(org_id="tenant", flow_id="flow")
    binding = SimpleNamespace(repo="example/repo")
    policy = SimpleNamespace(
        environment_connection_ids={ordinary.connection_id},
        expires_at=now + timedelta(hours=1),
        limits=SimpleNamespace(max_wall_clock_seconds=3600),
    )
    authority = SimpleNamespace(authority_context=AsyncMock(return_value=(None, None, SimpleNamespace(policy=policy), "owner", None)))
    provider = SimpleNamespace(
        changed_files=AsyncMock(return_value=("modules/gateway/src/example.py",)),
        definition=AsyncMock(side_effect=RuntimeError("ordinary workflow reached")),
    )
    services = WorkflowServices(Mock(return_value=AsyncMock()), authority=authority, provider=provider, manifest_loader=lambda: manifest)
    services.state = AsyncMock(return_value=(node, binding, SimpleNamespace(merge_sha=ordinary.artifact_revision), "run", None))
    services.actions = AsyncMock(return_value=[SimpleNamespace(detail={"manifest_entry_id": "adp-dev-embark1-gateway", "runtime_verified": True})])
    monkeypatch.setattr("src.orchestration.runtime_policy.flow_started_at", AsyncMock(return_value=now))
    with pytest.raises(RuntimeError, match="ordinary workflow reached"):
        await services.snapshot(SimpleNamespace())
    provider.definition.assert_awaited_once_with(binding, ordinary.workflow, ordinary.artifact_revision, for_dispatch=True)


@pytest.mark.parametrize("entry_id", ["adp-dev-atlassian-foundation-gateway", "adp-dev-tool-adapters"])
def test_foundation_named_targets_still_resolve_but_require_shared_admission(entry_id):
    manifest = load_packaged_manifest()
    entry = manifest.entry(entry_id)
    target = PhysicalTarget(
        "aws", "000000000000", "us-east-1", entry.resource_kind, entry.resource_id, TargetEvidence("fixture", "2026-10-22T00:00:00Z")
    )
    resolution = resolve_manifest_entry(
        manifest,
        entry_id=entry_id,
        component=entry.component_selectors[0],
        policy_connection_ids=frozenset({entry.connection_id}),
        target_lookup=lambda _: target,
        requested_inputs={name: next(iter(values)) for name, values in entry.workflow.allowed_inputs.items()},
        resolved_workflow_revision=entry.workflow.definition_revision,
    )
    assert resolution.resolved and resolution.entry == entry
    with pytest.raises(CycleBlockedError, match="deployment_shared_release_admission_unavailable"):
        _require_ungrouped_release(
            manifest,
            {"manifest_entry_id": entry_id, "manifest_digest": hashlib.sha256(canonical(asdict(entry)).encode()).hexdigest()},
        )


@pytest.mark.parametrize("path", [".github/workflows/tools-ci.yml", "codebuild/bs-tools-facade-smoke.yml"])
def test_tools_ci_artifacts_do_not_trigger_a_deployment(path):
    assert components_for_paths([path]) == ("documentation",)


# --- Manifest-driven path rules (#7457) ----------------------------------------


@pytest.mark.parametrize(
    ("path", "component"),
    [
        (".github/workflows/tools-platform-deploy.yml", "tools-platform"),
        (".github/workflows/tools-interceptor-deploy.yml", "tools-interceptor"),
        ("modules/tools/deploy.sh", "tools-platform"),
        ("modules/tools/orchestrate.sh", "tools-platform"),
        ("modules/agent-context/scripts/door_release_evidence.py", "knowledge-door"),
        (".github/workflows/agent-context-deploy.yml", "knowledge-door"),
        ("modules/gateway/frontend/src/App.tsx", "gateway-frontend"),
        ("modules/gateway/alembic/versions/099_x.py", "gateway-migrations"),
        ("modules/gateway/src/orchestration/x.py", "gateway-backend"),
    ],
)
def test_pipeline_and_release_paths_select_their_component(path, component):
    assert components_for_paths([path]) == (component,)


@pytest.mark.parametrize(
    "path",
    [
        ".adp/tasks/7246.json",
        ".github/workflows/tools-platform-ci.yml",
        ".github/workflows/tools-interceptor-ci.yml",
        "modules/agent-context/tests/unit/test_x.py",
        "modules/gateway/tests/orchestration/test_x.py",
        ".gitignore",
    ],
)
def test_records_tests_and_ci_definitions_never_trigger_a_deployment(path):
    assert components_for_paths([path]) == ("documentation",)


def test_rules_are_ordered_so_narrow_gateway_paths_win_over_the_backend_catch_all():
    assert components_for_paths(["modules/gateway/frontend/src/App.tsx", "modules/gateway/src/x.py", "modules/gateway/alembic/v.py"]) == (
        "gateway-backend",
        "gateway-frontend",
        "gateway-migrations",
    )


def test_paths_outside_every_rule_still_block():
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["platform/scripts/teardown.py"])


def test_rules_come_from_the_manifest_not_from_code():
    from src.orchestration.deployment_manifest import DeploymentManifest, PathRule

    manifest = DeploymentManifest(
        schema_version=1,
        entries=(),
        path_rules=(PathRule("services/widgets/*", "widgets"), PathRule("*.md", "ignore")),
    )
    assert components_for_paths(["services/widgets/app.py", "README.md"], manifest) == ("widgets",)
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["services/other/app.py"], manifest)
    with pytest.raises(CycleBlockedError, match="deployment_component_unmapped"):
        components_for_paths(["modules/user-services/dev-box/infra/main.tf"], manifest)


@pytest.mark.parametrize(
    "rule",
    [
        {"pattern": "", "component": "x"},
        {"pattern": "a b", "component": "x"},
        {"pattern": "a/*", "component": ""},
        {"pattern": "a/*", "component": "x", "extra": 1},
        {"pattern": 5, "component": "x"},
        "a/*",
    ],
)
def test_malformed_path_rules_are_refused(rule):
    from src.orchestration.deployment_manifest import ManifestError, parse_manifest

    with pytest.raises(ManifestError):
        parse_manifest({"schema_version": 1, "entries": [], "path_rules": [rule]})


def test_packaged_rules_cover_every_component_an_entry_selects():
    manifest = load_packaged_manifest()
    selectable = {selector for entry in manifest.entries for selector in entry.component_selectors}
    mapped = {rule.component for rule in manifest.path_rules if not rule.ignored}
    assert selectable <= mapped


def test_chat_source_and_crew_controls_select_one_shared_deployment():
    chat_path = "modules/agent-factory/agent/src/complex-task-chat/sandbox-turn.ts"
    crew_path = "modules/agent-factory/agent/k8s/chat-crew-pod-admission.yaml"
    assert components_for_paths([chat_path]) == ("chat-worker",)
    assert components_for_paths([crew_path]) == ("agent-worker",)
    components = components_for_paths([chat_path, crew_path])
    assert components == ("agent-worker", "chat-worker")
    entries = select_deployment_entries(load_packaged_manifest(), components, frozenset())
    assert tuple(entries) == ("adp-dev-agent-worker",)
    assert entries["adp-dev-agent-worker"].workflow.path == ".github/workflows/chat-agent-deploy.yml"
