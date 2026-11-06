"""Real HTTP read protocol with offline runtimes; never installed acceptance."""

from copy import deepcopy
import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import qualification_well_architected_compatibility as compatibility
import qualification_well_architected_acceptance as acceptance
from test_installed_artifact_series import selection as installed_selection
from l5_services.findings import normalize_result
from l5_services.models.run import Run, view_run


@pytest.fixture
def artifact_selection(tmp_path):
    return installed_selection.__wrapped__(tmp_path)


async def serialized_record(item, now, actor):
    """Use the production public projections and durable recovery export."""
    from src.products import l5_devops_recovery, l5_security_readback

    read_recovery_state = (l5_devops_recovery if item["service_id"] == "devops" else l5_security_readback).read_recovery_state

    scope = {**item["scope"], "owner_id": actor.user_id, "relationship_revision": "1", "visibility": "only_me"}
    reference = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
                 "source_connection_id": "connection", "record_type": "connection", "record_id": "connection",
                 "record_revision": "1"}
    space = {**reference, "record_type": "agent_space", "record_id": "space"}
    native = {**reference, "record_type": "native_job", "record_id": "original-native-job"}
    identity = {"account_id": "000000000000", "region": "us-east-1",
                "role_arn": "arn:aws:iam::000000000000:role/example"}
    run_input = {"service_id": "devops", "capability": "release_readiness", "sources": []}
    if item["service_id"] == "security":
        from l5_services.run_input import validate_run_input

        run_input = {"configuration_revision": 2, "binding_id": "binding", "capability": "threat_model",
                     "sources": [{**reference, "record_type": "document", "record_id": "architecture"}]}
        validate_run_input(run_input)
        assert "service_id" not in run_input  # Production admission stores this unmaterialized default.
    run = Run(tenant_id=scope["tenant_id"], product_id=scope["product_id"], project_id=scope["project_id"],
              service_id=item["service_id"], run_id=item["run_id"], configuration_id="original-config",
              configuration_revision=2, binding_id="binding", binding_generation=1, trigger_id="manual",
              revision=3, origin="adp_submitted", scope=scope, state="succeeded", provider_state="completed",
              deadline_at=now + timedelta(hours=1), native_job_ref=native, manifest={"sources": []},
              input=run_input, admission={"scope": scope, "connection_ref": reference,
                                         "provider_identity": identity, "native_space_ref": space})
    result, _ = normalize_result(run, item["result_id"], item["result_revision"],
                                  {"findings": [], "coverage": "complete"}, now)
    binding = SimpleNamespace(tenant_id=run.tenant_id, product_id=run.product_id, project_id=run.project_id,
                               service_id=run.service_id, binding_id=run.binding_id, revision=1,
                               generation=1, state="ready", gaps=[], configuration_id=run.configuration_id,
                               connection_ref=reference, scope=scope, provider_identity=identity, native_resource_ref=space)
    grant = SimpleNamespace(revision=1, binding_generation=1, capabilities=[run.input["capability"]], revoked_at=None,
                             expires_at=now + timedelta(hours=1))
    attempt = SimpleNamespace(attempt_id="original-attempt", operation="submit", status="completed",
                               first_work_id="original-work", first_fence=2, idempotency_token="d" * 64, input_hash="e" * 64,
                               native_job_ref=native, reconcile_checks=1, started_at=now)
    work = SimpleNamespace(work_id="original-work", kind="run", generation=1, fence=2, attempts=1,
                            completed_at=now, last_enqueued_at=now, step=1, due_at=now, blocked_at=None,
                            lease_until=None, dispatch_until=None, redeliver_after=None)
    publications = [SimpleNamespace(publication_id="publication-" + destination, result_id=item["result_id"],
                        result_version=item["result_revision"], destination=destination, revision=1, state="published",
                        schema_version="1.0", attempts=1, receipt={"receipt_id": destination},
                        invalidation_attempts=0, invalidation_watermark=0, applied_watermark=0, invalidation_receipt=None)
                    for destination in ("product-context", "engineering-insights")] if item["service_id"] == "devops" else []
    async def rows(statement):
        entity = statement.column_descriptions[0]["entity"].__name__
        return SimpleNamespace(all=lambda: {"WorkerAttempt": [attempt], "Work": [work], "Publication": publications}[entity])
    async def get_grant(*args, **kwargs):
        return grant
    async def authority(*args):
        return None, {"authorization": {"delegation_id": "grant"}, "scope": scope,
                      "connection_ref": reference, "configuration_id": run.configuration_id, "configuration_revision": 2}
    async def get_run(*args):
        return run
    async def get_binding(*args):
        return binding
    async def bindings(*args):
        return SimpleNamespace(binding=get_binding)
    backend = SimpleNamespace(actor=actor,
                              db=SimpleNamespace(expire_all=lambda: None, get=get_grant, scalars=rows),
                              binding_authority=authority, get_run=get_run, binding_store=bindings)
    recovery = await read_recovery_state(backend, scope["product_id"], scope["project_id"], run.run_id)
    return {"run": view_run(run), "result": result.payload, "recovery": recovery}


@pytest.fixture
def observed(artifact_selection, tmp_path, monkeypatch):
    monkeypatch.setenv("BG_TOKEN_SECRET_KEY", "offline-compatibility-test-token-key-not-for-production")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    config, fixture, _ = artifact_selection
    records = [{"service_id": service, "gateway_url": "https://product.example/", "token_env": "OWNER_TOKEN",
                "scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"},
                "run_id": service + "-run", "result_id": service + "-result", "result_revision": 1}
               for service in ("security", "devops")]
    selected = {"schema_version": 1, "source_revision": "a" * 40, "environment": "dev",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "records": records, "stages": {}}
    state, calls = {"fault": None, "changed": False, "counts": {}}, []
    runtime = {}
    for index, stage in enumerate(compatibility.STAGES):
        values = {**vars(fixture), "source_revision": ("b" if index == 0 else "a") * 40,
                  "database_head": "old-head" if index == 0 else "head-1", "product_id": "product",
                  "probe_id": "probe", "nonce": "f" * 64, "result_sha256": "e" * 64,
                  "deadline_seconds": 30, "max_spend_usd": "1.00", "max_attempts": 1,
                  "owned_resources": ["selected-resource"]}
        path = tmp_path / (stage + "-fixture.json")
        path.write_text(json.dumps({"schema_version": 1, "installed_release": values}))
        path.chmod(0o600)
        selected["stages"][stage] = {"fixture_path": str(path), "fixture_sha256": sha256(path.read_bytes()).hexdigest(),
                                     "runtime": config}
        runtime[stage] = {"source": "protected-installed-gateway-read", "account_id": fixture.account_id,
                         "region": "us-east-1", "role_arn": fixture.role_arn, "source_revision": values["source_revision"],
                         "schema_sha256": fixture.schema_sha256, "wheel_sha256": fixture.wheel_sha256,
                         "gateway_image_digest": fixture.gateway_image_digest, "proofs_sha256": "f" * 64,
                         "gateway": {"pod_uids": ["pod-" + str(index)], "migration_head": values["database_head"],
                                     "deployment_uid": "deployment", "deployment_generation": index + 1}}

    from src import l5_integration
    from src.auth.routes import get_current_user
    from src.products import l5_devops_recovery
    from src.shared.schemas.auth import TokenContext

    login = TokenContext(user_id="cognito-subject", org_id="tenant", department_id="department", team_id="team",
                         account_type="human", auth_source="jwt", token_use="access",
                         expires_at=datetime.now(UTC) + timedelta(hours=1))
    login._authenticated_subject_id = login.user_id

    async def current_owner(db, actor):
        assert actor is login and actor.org_id == "tenant"
        return SimpleNamespace(id="reader", team_id="team")

    monkeypatch.setattr(l5_integration.product_routes, "current_owner", current_owner)
    canonical_actor = asyncio.run(l5_integration.l5_actor(None, login))
    public_actor = asyncio.run(get_current_user(login))
    assert canonical_actor.user_id == "reader" != public_actor["user_id"] == login.user_id
    assert canonical_actor._authenticated_subject_id == login._authenticated_subject_id

    async def owner(*args):
        return None

    monkeypatch.setattr(l5_devops_recovery, "_owner", owner)
    wire = {item["service_id"]: asyncio.run(serialized_record(item, datetime.now(UTC), canonical_actor)) for item in records}

    def connection(item):
        def respond(request):
            assert request.method == "GET"
            calls.append(request.url.path)
            path = request.url.path
            key = (item["service_id"], path)
            state["counts"][key] = state["counts"].get(key, 0) + 1
            count = state["counts"][key]
            if path == "/api/auth/me":
                actor = {**public_actor}
                if state["fault"] == "actor-drift" and count > 1:
                    actor["user_id"] = "changed-login-subject"
                return httpx.Response(200, json=actor)
            if state["fault"] == "missing":
                return httpx.Response(404, json={"detail": {"code": "not_found"}})
            if state["fault"] == "redirect":
                return httpx.Response(302, headers={"location": "https://unselected.example/"})
            serialized = deepcopy(wire[item["service_id"]])
            if state.get("modify"):
                state["modify"](item, path, serialized, count)
            run, result, recovery = (serialized[name] for name in ("run", "result", "recovery"))
            if state["changed"]:
                result["coverage"] = "partial"
            if state["fault"] == "foreign":
                result["scope"] = {**item["scope"], "project_id": "foreign"}
            if state["fault"] == "result-revision":
                result["revision"] = 2
            if path.endswith("/recovery-state"):
                if state["fault"] == "empty-recovery":
                    recovery["attempts"] = []
                return httpx.Response(200, json=recovery)
            if "/results/" in path:
                return httpx.Response(200, json=result)
            if "/runs/" in path:
                return httpx.Response(200, json=run)
            pytest.fail("unexpected route: " + path)
        return httpx.Client(base_url=item["gateway_url"], transport=httpx.MockTransport(respond), follow_redirects=False)

    def read_runtime(value, stage):
        assert value == selected
        return deepcopy(runtime[stage])

    series = compatibility.CompatibilitySeries(tmp_path / "compatibility-checkpoints", selected, read_runtime=read_runtime,
                read_records=lambda value: compatibility.records_read(value, connection))
    return series, selected, runtime, state, calls


def checkpoints(series):
    for stage in compatibility.STAGES:
        series.checkpoint(stage)


def test_collects_exact_old_records_and_recovery_without_claiming_rollback_or_acceptance(observed):
    series, selected, _, _, calls = observed
    checkpoints(series)
    report = series.verify()
    assert report["old_records_retained"] is True and report["gateway_pods_replaced"] is True
    assert report["source_revision"] == selected["source_revision"]
    assert report["migration_heads"] == ["old-head", "head-1", "head-1"]
    assert report["board_tasks"] == ["AC-01-t2", "SCN-01-t1"]
    assert report["outcome"] == "incomplete" and report["native_qualified"] is False
    assert "rollback_operation_not_observed" in report["limitations"]
    assert "product_origin_not_bound_to_gateway_probe" in report["limitations"]
    assert len(set(report["checkpoint_sha256"])) == 3
    rows = series.read("before-transition")["snapshot"]["records"]
    assert rows[0]["run"]["revision"] == 3 and rows[0]["result"]["revision"] == 1
    assert rows[0]["run"]["admission"]["configuration_revision"] == 2
    assert rows[0]["result"]["scope"]["owner_id"] == "reader"
    assert rows[0]["recovery"]["work"][0]["fence"] == 2
    assert any("/services/security/" in path and path.endswith("/recovery-state") for path in calls)
    assert any("/services/devops/" in path and path.endswith("/recovery-state") for path in calls)
    assert rows[1]["recovery"]["attempts"][0]["idempotency_token"] == "d" * 64
    assert {row["destination"] for row in rows[1]["recovery"]["publications"]} == {"product-context", "engineering-insights"}
    assert "devops_durable_recovery_rows_not_exposed" not in report["limitations"]
    assert series.read("before-transition")["contract"] == compatibility.CHECKPOINT_CONTRACT


def test_production_security_omitted_default_survives_fresh_reads_and_checkpoints(observed):
    series = observed[0]
    checkpoints(series)
    for stage in compatibility.STAGES:
        security = series.read(stage)["snapshot"]["records"][0]
        assert "service_id" not in security["run"]["input"]
        assert security["recovery"]["run"] == security["run"]
    assert series.verify()["outcome"] == "incomplete"


def test_production_login_subject_and_canonical_owner_stay_distinct_in_fresh_records(observed):
    series = observed[0]
    checkpoints(series)
    for stage in compatibility.STAGES:
        for record in series.read(stage)["snapshot"]["records"]:
            assert record["actor"]["user_id"] == "cognito-subject"
            assert record["run"]["scope"]["owner_id"] == record["result"]["scope"]["owner_id"] == "reader"
            assert record["recovery"]["run"] == record["run"]
    assert series.verify()["outcome"] == "incomplete"


@pytest.mark.parametrize("service,value", [("security", "devops"), ("security", None),
                                          ("devops", "security"), ("devops", None), ("devops", "absent")])
def test_security_default_does_not_allow_wrong_explicit_service_or_omitted_devops(observed, service, value):
    series, _, _, state, _ = observed

    def modify(item, path, values, count):
        if item["service_id"] == service:
            for run in (values["run"], values["recovery"]["run"]):
                if value == "absent":
                    run["input"].pop("service_id", None)
                else:
                    run["input"]["service_id"] = value
    state["modify"] = modify
    with pytest.raises(ValueError, match="another service"):
        series.checkpoint("before-transition")
    assert series.journal.read("before-transition.json") is None


@pytest.mark.parametrize("fault", ["missing", "redirect", "foreign", "result-revision", "empty-recovery"])
def test_missing_or_unselected_product_evidence_is_rejected(observed, fault):
    series, _, _, state, _ = observed
    state["fault"] = fault
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        series.checkpoint("before-transition")
    assert series.journal.read("before-transition.json") is None


def test_after_cannot_invent_missing_historical_baseline(observed):
    with pytest.raises(ValueError, match="missing original"):
        observed[0].checkpoint("after-transition")


def test_existing_checkpoint_is_never_overwritten_with_new_evidence(observed):
    series, _, _, state, _ = observed
    first = series.checkpoint("before-transition")
    state["changed"] = True
    assert series.checkpoint("before-transition") == first


def test_changed_retained_result_during_transition_cannot_be_called_compatible(observed):
    series, _, _, state, _ = observed
    series.checkpoint("before-transition")
    state["changed"] = True
    series.checkpoint("after-transition")
    series.checkpoint("after-recovery")
    with pytest.raises(ValueError, match="old public records"):
        series.verify()


def test_current_record_drift_invalidates_previously_matching_checkpoints(observed):
    series, _, _, state, _ = observed
    checkpoints(series)
    state["changed"] = True
    with pytest.raises(ValueError, match="changed after"):
        series.verify()


@pytest.mark.parametrize("fault", ["source", "schema", "empty-pods", "target", "same-pods"])
def test_installed_identity_and_recovery_replacement_are_observed_not_assumed(observed, fault):
    series, _, runtime, _, _ = observed
    if fault == "source":
        runtime["before-transition"]["source_revision"] = "a" * 40
    elif fault == "schema":
        runtime["after-transition"]["gateway"]["migration_head"] = "different-head"
    elif fault == "empty-pods":
        runtime["after-transition"]["gateway"]["pod_uids"] = []
    elif fault == "target":
        runtime["after-transition"]["gateway"]["deployment_uid"] = "another-deployment"
    else:
        runtime["after-recovery"]["gateway"]["pod_uids"] = runtime["after-transition"]["gateway"]["pod_uids"]
    if fault == "same-pods":
        checkpoints(series)
        assert series.verify()["gateway_pods_replaced"] is False
    else:
        with pytest.raises(ValueError):
            checkpoints(series)
            series.verify()


def rewrite(series, stage, change):
    path = series.journal.directory / (stage + ".json")
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


@pytest.mark.parametrize("fault", ["historical", "broken-chain", "selection", "rehash-wrong-source"])
def test_historical_or_mismatched_checkpoint_rejected_even_with_rehashed_content(observed, fault):
    series = observed[0]
    checkpoints(series)
    def change(value):
        if fault == "historical":
            value["read_started_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        elif fault == "broken-chain":
            value["previous_sha256"] = "0" * 64
        elif fault == "selection":
            value["selection_sha256"] = "0" * 64
        else:
            value["snapshot"]["runtime"]["source_revision"] = "f" * 40
            value["snapshot_sha256"] = compatibility.artifacts.canonical(value["snapshot"])
    rewrite(series, "after-transition", change)
    with pytest.raises(ValueError):
        series.verify()


def test_runtime_racing_row_read_does_not_create_checkpoint(observed):
    series, _, runtime, _, _ = observed
    calls = 0
    def changing(selection, stage):
        nonlocal calls
        calls += 1
        value = deepcopy(runtime[stage])
        value["gateway"]["deployment_generation"] = calls
        return value
    series.read_runtime = changing
    with pytest.raises(ValueError, match="changed during"):
        series.checkpoint("before-transition")


def test_partial_compatibility_does_not_promote_original_acceptance(observed):
    series = observed[0]
    checkpoints(series)
    progress = series.verify()
    case = {"run_id": "review", "result_revision": 1, "result": {"scope": {"tenant_id": "tenant"}}}
    observation = {"run_id": "review", "result_revision": 1, "scope": case["result"]["scope"]}
    report = acceptance.evaluate([case, case], [observation, observation],
                                  {"source_revision": "a" * 40, "environment": "dev"}, compatibility=progress)
    assert report["compatibility_progress"] == progress
    assert next(row for row in report["story_criteria"] if row["id"] == "WA07/AC-01")["outcome"] == "incomplete"
    assert next(row for row in report["live_checks"] if row["id"] == "schema-compatible")["outcome"] == "incomplete"
    progress["outcome"] = "passed"
    with pytest.raises(ValueError, match="cannot promote"):
        acceptance.evaluate([case, case], [observation, observation],
                              {"source_revision": "a" * 40, "environment": "dev"}, compatibility=progress)


@pytest.mark.parametrize("fault", [None, "source", "migration", "wheel", "environment"])
def test_release_emitter_rechecks_exact_installed_linkage(observed, monkeypatch, fault):
    series, _, runtime, _, _ = observed
    checkpoints(series)
    context = {"source_revision": "a" * 40, "environment": "dev"}
    current = runtime["after-recovery"]
    installed = {"source": "independent-installed-read", "source_revision": "a" * 40, "migration_head": "head-1",
                 "digests": {"schema": current["schema_sha256"], "wheel": current["wheel_sha256"],
                             "gateway": current["gateway_image_digest"]}}
    if fault == "source":
        installed["source_revision"] = "b" * 40
    elif fault == "migration":
        installed["migration_head"] = "unrelated-head"
    elif fault == "wheel":
        installed["digests"]["wheel"] = "0" * 64
    elif fault == "environment":
        context["environment"] = "prod"
    def factory(path, selected):
        assert path == series.journal.directory and selected == series.selection
        return series
    monkeypatch.setattr(compatibility, "CompatibilitySeries", factory)
    if fault:
        with pytest.raises(ValueError):
            compatibility.read_progress(series.journal.directory.parent, context, installed)
    else:
        assert compatibility.read_progress(series.journal.directory.parent, context, installed)["old_records_retained"] is True


@pytest.mark.parametrize("fault", ["missing", "wrong-run", "missing-key", "extra-key", "empty-work", "row-type", "wrong-service", "wrong-owner"])
def test_devops_recovery_requires_exact_original_envelope(observed, fault):
    series, _, _, state, _ = observed

    def modify(item, path, values, count):
        if item["service_id"] != "devops":
            return
        recovery = values["recovery"]
        if fault == "missing":
            values["recovery"] = None
        elif fault == "wrong-run":
            recovery["run"]["run_id"] = "another-run"
        elif fault == "missing-key":
            recovery.pop("publications")
        elif fault == "extra-key":
            recovery["truncated"] = False
        elif fault == "empty-work":
            recovery["work"] = []
        elif fault == "row-type":
            recovery["attempts"] = ["not-a-row"]
        elif fault == "wrong-service":
            values["run"]["input"]["service_id"] = "security"
        else:
            values["result"]["scope"] = {**values["result"]["scope"], "owner_id": "another-owner"}
    state["modify"] = modify
    with pytest.raises(ValueError):
        series.checkpoint("before-transition")
    assert series.journal.read("before-transition.json") is None


@pytest.mark.parametrize("field", ["run", "result", "attempt", "work", "publication", "actor"])
def test_devops_original_records_and_actor_must_stay_stable_during_reads(observed, field):
    series, _, _, state, _ = observed

    def modify(item, path, values, count):
        if item["service_id"] != "devops" or count < 2:
            return
        if field in {"run", "result"}:
            values[field]["revision"] += 1
        elif field == "attempt":
            values["recovery"]["attempts"][0]["idempotency_token"] = "0" * 64
        elif field == "work":
            values["recovery"]["work"][0]["fence"] += 1
        elif field == "publication":
            values["recovery"]["publications"][0]["revision"] += 1
    state["modify"] = modify
    if field == "actor":
        state["fault"] = "actor-drift"
    with pytest.raises(ValueError, match="changed during"):
        series.checkpoint("before-transition")
    assert series.journal.read("before-transition.json") is None


def test_legal_near_eight_mib_devops_response_survives_exact_journal_serialization(observed):
    series, _, _, state, _ = observed

    def modify(item, path, values, count):
        if item["service_id"] == "devops":
            recovery = values["recovery"]
            # Receipt metadata is retained, not interpreted as native evidence.
            recovery["publications"][0]["receipt"] = {"detail": ""}
            gap = compatibility.DEVOPS_RECOVERY_BYTES - len(compatibility.encoded(recovery))
            recovery["publications"][0]["receipt"]["detail"] = "x" * gap
            assert len(compatibility.encoded(recovery)) == compatibility.DEVOPS_RECOVERY_BYTES
    state["modify"] = modify
    checkpoint = series.checkpoint("before-transition")
    path = series.journal.directory / "before-transition.json"
    assert compatibility.DEVOPS_RECOVERY_BYTES < path.stat().st_size < compatibility.CHECKPOINT_BYTES
    assert path.read_bytes() == compatibility.encoded(checkpoint)
    assert series.read("before-transition") == checkpoint
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("target", ["devops-recovery", "security-recovery", "devops-run", "devops-result"])
def test_larger_limit_applies_only_to_exact_devops_recovery_read(observed, target):
    series, _, _, state, _ = observed

    def modify(item, path, values, count):
        service, field = target.split("-")
        if item["service_id"] != service:
            return
        size = compatibility.DEVOPS_RECOVERY_BYTES if target == "devops-recovery" else compatibility.PRODUCT_READ_BYTES
        if field == "recovery":
            values[field]["work"][0]["detail"] = "x" * size
        else:
            values[field]["detail"] = "x" * size
    state["modify"] = modify
    with pytest.raises(ValueError, match="exceeds its bound"):
        series.checkpoint("before-transition")
    assert series.journal.read("before-transition.json") is None


def test_stream_and_canonical_unicode_bounds_are_both_enforced():
    payload = '{"text":"' + '\u0800' * (compatibility.PRODUCT_READ_BYTES // 4) + '"}'
    raw = payload.encode()
    assert len(raw) < compatibility.PRODUCT_READ_BYTES < len(compatibility.encoded(json.loads(raw)))
    with httpx.Client(base_url="https://product.example/", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=raw))) as client:
        with pytest.raises(ValueError, match="retained byte bound"):
            compatibility.get(client, "/api/auth/me")


def legacy_checkpoints(series, *, stages=compatibility.STAGES):
    """Build the original v1 writer's exact shape, not a backfilled observation."""
    original = series.read_records
    def old_reader(selection):
        records = original(selection)
        next(row for row in records if row["service_id"] == "devops")["recovery"] = None
        return records
    for stage in stages:
        index = compatibility.STAGES.index(stage)
        prior = series.journal.read(compatibility.STAGES[index - 1] + ".json") if index else None
        snapshot = {"runtime": series.read_runtime(series.selection, stage), "records": old_reader(series.selection)}
        value = {"contract": compatibility.LEGACY_CHECKPOINT_CONTRACT, "stage": stage,
                 "selection_sha256": compatibility.artifacts.canonical(series.selection),
                 "read_started_at": datetime.now(UTC).isoformat(), "observed_at": datetime.now(UTC).isoformat(),
                 "previous_sha256": compatibility.artifacts.canonical(prior) if prior else None,
                 "snapshot": snapshot, "snapshot_sha256": compatibility.artifacts.canonical(snapshot)}
        series.journal.retain(stage + ".json", value)


def test_legacy_null_history_stays_readable_unchanged_and_explicitly_incomplete(observed):
    series = observed[0]
    legacy_checkpoints(series)
    before = {path.name: path.read_bytes() for path in series.journal.directory.iterdir()}
    for stage in compatibility.STAGES:
        value = series.read(stage)
        for record in value["snapshot"]["records"]:
            assert record["actor"]["user_id"] == "cognito-subject"
            assert record["run"]["scope"]["owner_id"] == record["result"]["scope"]["owner_id"] == "reader"
        assert "service_id" not in value["snapshot"]["records"][0]["run"]["input"]
        assert value["snapshot"]["records"][1]["recovery"] is None
        assert series.checkpoint(stage) == value
    with pytest.raises(ValueError, match="remains incomplete: original DevOps recovery was not collected"):
        series.verify()
    assert {path.name: path.read_bytes() for path in series.journal.directory.iterdir()} == before


def test_legacy_cli_explains_the_unfillable_original_evidence_gap(observed, monkeypatch):
    series = observed[0]
    legacy_checkpoints(series)
    monkeypatch.setattr(compatibility.driver, "private_json", lambda path: series.selection)
    monkeypatch.setattr(compatibility, "CompatibilitySeries", lambda *args: series)
    with pytest.raises(SystemExit, match="original checkpoints did not collect DevOps recovery"):
        compatibility.main(["verify", "/private/selection.json", str(series.journal.directory)])


def test_mixed_legacy_and_new_checkpoints_do_not_ignore_missing_history(observed):
    series = observed[0]
    legacy_checkpoints(series, stages=compatibility.STAGES[:1])
    before = (series.journal.directory / "before-transition.json").read_bytes()
    series.checkpoint("after-transition")
    series.checkpoint("after-recovery")
    with pytest.raises(ValueError, match="old public records"):
        series.verify()
    assert (series.journal.directory / "before-transition.json").read_bytes() == before


def test_new_checkpoint_cannot_disguise_missing_recovery_even_when_rehashed(observed):
    series = observed[0]
    checkpoints(series)
    def change(value):
        value["snapshot"]["records"][1]["recovery"] = None
        value["snapshot_sha256"] = compatibility.artifacts.canonical(value["snapshot"])
    rewrite(series, "before-transition", change)
    with pytest.raises(ValueError, match="recovery readback"):
        series.read("before-transition")


def test_legacy_incomplete_progress_is_not_promoted_by_acceptance(observed):
    series = observed[0]
    checkpoints(series)
    progress = series.verify()
    progress["limitations"] = compatibility.LEGACY_LIMITATIONS
    case = {"run_id": "review", "result_revision": 1, "result": {"scope": {"tenant_id": "tenant"}}}
    observation = {"run_id": "review", "result_revision": 1, "scope": case["result"]["scope"]}
    report = acceptance.evaluate([case, case], [observation, observation],
                                  {"source_revision": "a" * 40, "environment": "dev"}, compatibility=progress)
    assert next(row for row in report["story_criteria"] if row["id"] == "WA07/AC-01")["outcome"] == "incomplete"
    assert next(row for row in report["live_checks"] if row["id"] == "schema-compatible")["outcome"] == "incomplete"
