"""Real assignment/claim/policy fences with canonical provider check selection."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.merge_evidence import CheckEvidence, CheckRequirement, GitHubMergeObserver, RepositoryRequirements, evaluate_required_checks
from src.orchestration.models import OrchestrationWorkClaim
from src.orchestration.reviewer_checks import observe_reviewer_checks
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from tests.orchestration.test_merge_controller import prepared_merge
from tests.orchestration.test_merge_evidence import collision_checks
from tests.orchestration.test_review_cycle import ORG, tick
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


def observation(head):
    return SimpleNamespace(
        head_sha=head,
        base_sha="b" * 40,
        open=True,
        merged=False,
        mergeable=True,
        mergeable_state="clean",
        checks=(),
        requirements=RepositoryRequirements((), 0, False, False, False, False),
    )


@pytest.mark.parametrize("case", ["none", "missing", "pending", "pass", "failed", "optional", "wrong_app", "legacy_failed"])
def test_same_ci_policy_as_merge(case):
    obs = observation("a" * 40)
    if case != "none":
        obs.requirements = replace(obs.requirements, checks=(CheckRequirement("unit", 9),), checks_declared=True)
    if case not in {"none", "missing"}:
        state = {"pending": "pending", "failed": "failure"}.get(case, "success")
        obs.checks = (CheckEvidence("unit", 8 if case == "wrong_app" else 9, state, 17, "check_run"),)
    if case == "optional":
        obs.checks += (CheckEvidence("optional", 9, "failure", 18, "check_run"),)
    if case == "legacy_failed":
        obs.checks += (CheckEvidence("unit", None, "failure", 18, "status"),)
    reasons, checks = evaluate_required_checks(obs)
    expected = {
        "missing": "required_check_missing",
        "wrong_app": "required_check_missing",
        "pending": "required_check_pending",
        "failed": "required_check_failed",
        "legacy_failed": "required_check_failed",
    }.get(case)
    assert [reason.value for reason in reasons] == ([expected] if expected else [])
    if case == "optional":
        assert len(checks) == 1


async def test_no_ci_report_uses_trusted_bound_pr_without_terminal_receipt(shared, monkeypatch):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]
    observe = AsyncMock(return_value=observation(shared.head))
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)
    provider = SimpleNamespace(token=AsyncMock(return_value="scoped-token"))
    async with shared.factory() as db:
        result = await observe_reviewer_checks(db, credential=envelope["run_report"]["credential"], head_sha=shared.head, provider=provider)
        assert result["state"] == "passed" and result["checks"] == []
        row = await db.get(OrchestrationRunReport, envelope["message_id"])
        assert row.terminal_receipt is None and row.review_receipt is None
    assert observe.call_args.args[0].id == shared.binding.id
    assert provider.token.call_args.kwargs == {"evidence": True, "actions": True}


@pytest.mark.parametrize("change", ["head", "claim", "terminal"])
async def test_ci_observation_rechecks_current_assignment_after_provider_io(shared, monkeypatch, change):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]

    async def observe(self, binding):
        if change == "head":
            return observation("f" * 40)
        async with shared.factory() as db:
            if change == "claim":
                claim = await db.get(OrchestrationWorkClaim, shared.identity.claim_id)
                claim.active_run_id = "another-run"
            else:
                row = await db.get(OrchestrationRunReport, envelope["message_id"])
                row.terminal_receipt = {"outcome": "complete"}
            await db.commit()
        return observation(shared.head)

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)
    async with shared.factory() as db:
        with pytest.raises(RunReportError):
            await observe_reviewer_checks(
                db,
                credential=envelope["run_report"]["credential"],
                head_sha=shared.head,
                provider=SimpleNamespace(token=AsyncMock(return_value="scoped-token")),
            )


@pytest.mark.parametrize("for_merge", [False, True])
async def test_concurrent_binding_revision_requires_a_fresh_retry_not_worker_exit(shared, monkeypatch, for_merge):  # noqa: F811
    from src.orchestration.models import OrchestrationPullRequestBinding

    async with prepared_merge(shared, monkeypatch) as ctx:
        envelope = ctx.calls[-1]
        async with ctx.factory() as db:
            report = await db.get(OrchestrationRunReport, envelope["message_id"])
            report.terminal_receipt = None
            report.worker_receipt = {"ownership_nonce": "a" * 32}
            await db.commit()
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", lambda: datetime.now(UTC)).observe(ctx.binding)
        observations = 0

        async def observe(self, binding):
            nonlocal observations
            observations += 1
            if observations == 1:
                # The scheduler may reconcile the same published head while the
                # reviewer is reading GitHub. The old observation must be refused.
                async with ctx.factory() as db:
                    current = await db.get(OrchestrationPullRequestBinding, binding.id)
                    current.revision += 1
                    await db.commit()
            return observed

        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)
        arguments = dict(
            credential=envelope["run_report"]["credential"],
            head_sha=ctx.head,
            for_merge=for_merge,
            provider=ctx.merge_services.provider,
            storage=ctx.merge_services.storage,
        )
        async with ctx.factory() as db:
            with pytest.raises(RunReportError) as refused:
                await observe_reviewer_checks(db, **arguments)
            assert refused.value.code == "review_checks_unavailable"
            assert refused.value.retryable
        # A new request rereads the assignment, authority and provider evidence.
        async with ctx.factory() as db:
            result = await observe_reviewer_checks(db, **arguments)
            assert result["head_sha"] == ctx.head and result["state"] == "passed"
            if for_merge:
                assert result["merge_state"] == "eligible"
            assert (await db.get(OrchestrationRunReport, envelope["message_id"])).terminal_receipt is None
        assert observations == 2 and ctx.mutations == []


@pytest.mark.parametrize("missing_evidence", [False, True])
async def test_active_reviewer_merge_permission_requires_accepted_evidence_and_never_merges(shared, monkeypatch, missing_evidence):  # noqa: F811
    from sqlalchemy import delete

    from src.orchestration.models import OrchestrationAction
    from src.orchestration.review_cycle import CycleBlockedError

    async with prepared_merge(shared, monkeypatch) as ctx:
        envelope = ctx.calls[-1]
        async with ctx.factory() as db:
            report = await db.get(OrchestrationRunReport, envelope["message_id"])
            report.terminal_receipt = None
            report.worker_receipt = {"ownership_nonce": "a" * 32}
            if missing_evidence:
                await db.execute(delete(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
            await db.commit()
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", lambda: datetime.now(UTC)).observe(ctx.binding)
        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=observed))
        async with ctx.factory() as db:
            if missing_evidence:
                with pytest.raises(CycleBlockedError, match="fresh_verified_review_missing"):
                    await observe_reviewer_checks(
                        db,
                        credential=envelope["run_report"]["credential"],
                        head_sha=ctx.head,
                        for_merge=True,
                        provider=ctx.merge_services.provider,
                        storage=ctx.merge_services.storage,
                    )
            else:
                result = await observe_reviewer_checks(
                    db,
                    credential=envelope["run_report"]["credential"],
                    head_sha=ctx.head,
                    for_merge=True,
                    provider=ctx.merge_services.provider,
                    storage=ctx.merge_services.storage,
                )
                assert result["merge_state"] == "eligible" and result["merge_method"] == "squash"
                assert (await db.get(OrchestrationRunReport, envelope["message_id"])).terminal_receipt is None
        assert ctx.mutations == []


@pytest.mark.parametrize("changed_identity", [None, "app", "suite", "name"])
async def test_reviewer_and_merge_keep_failed_same_named_workflow_identity(shared, monkeypatch, changed_identity):  # noqa: F811
    async with prepared_merge(shared, monkeypatch) as ctx:
        envelope = ctx.calls[-1]
        async with ctx.factory() as db:
            report = await db.get(OrchestrationRunReport, envelope["message_id"])
            report.terminal_receipt = None
            report.worker_receipt = {"ownership_nonce": "a" * 32}
            await db.commit()
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", lambda: datetime.now(UTC)).observe(ctx.binding)
        raw_checks = collision_checks(ctx.head)["check_runs"]
        observed = replace(
            observed,
            checks=GitHubMergeObserver._checks(raw_checks, [], ctx.head),
            requirements=replace(observed.requirements, checks=(CheckRequirement("verify", 15368),), checks_declared=True),
        )
        failed = next(check for check in raw_checks if check["conclusion"] == "failure")
        failed["output"] = {"summary": "Tools CI failed both recovery assertions."}
        if changed_identity == "app":
            failed["app"]["id"] = 99
        elif changed_identity == "suite":
            failed["check_suite"]["id"] = 106003565981
        elif changed_identity == "name":
            failed["name"] = "unrelated"
        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=observed))
        read = AsyncMock(return_value=failed)
        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.get", read)
        async with ctx.factory() as db:
            arguments = dict(
                credential=envelope["run_report"]["credential"],
                head_sha=ctx.head,
                for_merge=True,
                provider=ctx.merge_services.provider,
                storage=ctx.merge_services.storage,
            )
            if changed_identity:
                with pytest.raises(RunReportError, match="review_checks_identity_changed"):
                    await observe_reviewer_checks(db, **arguments)
            else:
                result = await observe_reviewer_checks(db, **arguments)
                assert result["state"] == "failed" and result["merge_state"] == "blocked"
                assert result["reasons"] == ["required_check_failed"]
                assert "required_check_failed" in result["merge_reasons"]
                assert len(result["checks"]) == 3 and len(result["failures"]) == 1
                failure = result["failures"][0]
                assert failure["provider_id"] == 117644278210 and failure["check_suite_id"] == 106003565956
                assert failure["app_id"] == 15368 and failure["details"] == failed["output"]["summary"]
            assert (await db.get(OrchestrationRunReport, envelope["message_id"])).terminal_receipt is None
        read.assert_awaited_once_with(f"/repos/{ctx.binding.repo}/check-runs/117644278210", kind="failed_check")
        assert ctx.mutations == []


@pytest.mark.parametrize("change", ["none", "claim"])
async def test_protected_reviewer_uses_same_checks_and_rechecks_ownership(cycle, monkeypatch, change):  # noqa: F811
    from src.orchestration.review_cycle import CycleBlockedError

    await tick(cycle)
    envelope = cycle.calls[-1]
    record = SimpleNamespace(tenant_id=cycle.node.org_id, invocation_id=envelope["message_id"])
    runtime = SimpleNamespace(store=cycle.store)
    monkeypatch.setattr("src.agentauth.run_services.live_context", AsyncMock(return_value=(None, None, record, None)))
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: cycle.service.writer)
    monkeypatch.setattr("src.orchestration.reviewer_checks.get_session_factory", lambda: cycle.factory)

    async def observe(self, binding):
        assert cycle.factory.kw["bind"].pool.checkedout() == 0
        if change == "claim":
            async with cycle.factory() as db:
                claim = await db.get(OrchestrationWorkClaim, cycle.identity.claim_id)
                claim.active_run_id = "another-run"
                await db.commit()
        return observation(cycle.head)

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", observe)

    async def token(*args, **kwargs):
        assert cycle.factory.kw["bind"].pool.checkedout() == 0
        return "scoped-token"

    async with cycle.factory() as db:
        call = observe_reviewer_checks(db, head_sha=cycle.head, protected=(object(), runtime), provider=SimpleNamespace(token=token))
        if change == "claim":
            with pytest.raises((CycleBlockedError, RunReportError)):
                await call
        else:
            result = await call
            assert result["run_id"] == envelope["message_id"]
            assert result["state"] == "passed"
            assert await db.get(OrchestrationRunReport, envelope["message_id"]) is None


async def test_protected_delivery_requires_provider_merge_and_engine_only_observes(cycle, monkeypatch):  # noqa: F811
    from src.orchestration.review_assignment import require_reviewer_merge
    from tests.orchestration.test_merge_controller import tick as merge_tick
    from tests.orchestration.test_review_cycle import state

    async with prepared_merge(cycle, monkeypatch, reviewer_owned=True) as ctx:
        run_id = ctx.calls[-1]["message_id"]
        async with ctx.factory() as db:
            with pytest.raises(RunReportError, match="reviewer_merge_not_delivered"):
                await require_reviewer_merge(db, org_id=ctx.node.org_id, node_id=ctx.node.id, run_id=run_id, provider=ctx.merge_services.provider)
        await merge_tick(ctx)
        assert ctx.mutations == [], "engine must never merge a reviewer-owned PR"
        ctx.merge_remote()  # The reviewer performs the provider mutation.
        async with ctx.factory() as db:
            await require_reviewer_merge(db, org_id=ctx.node.org_id, node_id=ctx.node.id, run_id=run_id, provider=ctx.merge_services.provider)
        await merge_tick(ctx)
        execution, _, _, _ = await state(ctx)
        assert execution.phase != "merge_ready"
        assert ctx.mutations == []


@pytest.mark.parametrize("missing_evidence", [False, True])
async def test_protected_merge_permission_requires_accepted_review(cycle, monkeypatch, missing_evidence):  # noqa: F811
    from sqlalchemy import delete

    from src.orchestration.models import OrchestrationAction
    from src.orchestration.review_cycle import CycleBlockedError

    async with prepared_merge(cycle, monkeypatch, reviewer_owned=True) as ctx:
        run_id = ctx.calls[-1]["message_id"]
        record = SimpleNamespace(tenant_id=ctx.node.org_id, invocation_id=run_id)
        monkeypatch.setattr("src.agentauth.run_services.live_context", AsyncMock(return_value=(None, None, record, None)))
        monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: ctx.service.writer)
        monkeypatch.setattr("src.orchestration.reviewer_checks.get_session_factory", lambda: ctx.factory)
        observed = await GitHubMergeObserver(ctx.merge_services.provider.client, "scoped-test-token", lambda: datetime.now(UTC)).observe(ctx.binding)
        monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=observed))
        async with ctx.factory() as db:
            if missing_evidence:
                await db.execute(delete(OrchestrationAction).where(OrchestrationAction.kind == "review_evidence"))
                await db.commit()
            call = observe_reviewer_checks(
                db,
                head_sha=ctx.head,
                for_merge=True,
                protected=(object(), SimpleNamespace(store=ctx.store)),
                provider=ctx.merge_services.provider,
                storage=ctx.merge_services.storage,
            )
            if missing_evidence:
                with pytest.raises(CycleBlockedError, match="fresh_verified_review_missing"):
                    await call
            else:
                result = await call
                assert result["merge_state"] == "eligible"
        assert ctx.mutations == []


async def test_reviewer_owned_delivery_that_ends_without_a_merge_takes_another_delivery_pass(cycle, monkeypatch):  # noqa: F811
    """An eligible PR the reviewer did not merge is redelivered by a reviewer, never merged by the engine (#7472)."""
    from tests.orchestration.test_merge_controller import tick as merge_tick
    from tests.orchestration.test_review_cycle import state

    async with prepared_merge(cycle, monkeypatch, reviewer_owned=True) as ctx:
        result = await merge_tick(ctx)
        execution, _, node, _ = await state(ctx)
        assert ctx.mutations == [], "engine must never merge a reviewer-owned PR"
        assert execution.phase == "awaiting_review" and execution.status != "blocked", (execution.block_detail, result)
        assert node.state == "running"


async def test_reviewer_owned_delivery_that_ends_with_failed_checks_hands_off_to_repair(cycle, monkeypatch):  # noqa: F811
    from tests.orchestration.test_merge_controller import tick as merge_tick
    from tests.orchestration.test_review_cycle import state

    async with prepared_merge(cycle, monkeypatch, reviewer_owned=True) as ctx:
        ctx.remote["checks"]["check_runs"][0]["conclusion"] = "failure"
        result = await merge_tick(ctx)
        execution, _, _, _ = await state(ctx)
        assert execution.phase == "repairing", (execution.block_detail, result)
        assert ctx.mutations == []


async def test_reviewer_that_approved_then_exited_failed_does_not_park_the_merge(cycle, monkeypatch):  # noqa: F811
    """An exited reviewer has nothing more to say; the recorded exact-head approval decides (#7486)."""
    from tests.orchestration.test_merge_controller import tick as merge_tick
    from tests.orchestration.test_review_cycle import state

    async with prepared_merge(cycle, monkeypatch, reviewer_owned=True) as ctx:
        run_id = ctx.calls[-1]["message_id"]
        raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run_id}")
        raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
        ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
        result = await merge_tick(ctx)
        execution, _, _, _ = await state(ctx)
        assert execution.block_detail != "reviewer_still_active", result
        assert execution.phase == "awaiting_review" and ctx.mutations == []


@pytest.mark.parametrize(
    ("annotation_count", "message_padding", "log_padding", "cleanup_lines"),
    [(1, 0, 0, 0), (2, 500, 0, 0), (20, 500, 5000, 0), (20, 500, 5000, 1000)],
    ids=["short", "long-annotations", "full-budget", "failure-before-cleanup"],
)
async def test_failed_check_details_from_annotations_and_job_log(
    shared,  # noqa: F811
    monkeypatch,
    annotation_count,
    message_padding,
    log_padding,
    cleanup_lines,
):
    await tick(shared)
    envelope = shared.calls[-1]
    obs = observation(shared.head)
    obs.requirements = replace(obs.requirements, checks=(CheckRequirement("compile", 9),), checks_declared=True)
    obs.checks = (CheckEvidence("compile", 9, "failure", 17, "check_run", 42),)
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=obs))
    check = {
        "id": 17,
        "head_sha": shared.head,
        "name": "compile",
        "app": {"id": 9},
        "output": {},
        "check_suite": {"id": 42},
        "details_url": f"https://github.com/{shared.binding.repo}/actions/runs/25/job/90",
    }
    calls = []

    async def get(self, path, *, kind):
        calls.append(path)
        if path.endswith("/check-runs/17"):
            return check
        if path.endswith("/annotations?per_page=20&page=1"):
            return [{"annotation_level": "failure", "path": "src/main.rs", "message": "compile failed" + "x" * message_padding}] * annotation_count
        if path.endswith("/actions/runs/25"):
            return {"head_sha": shared.head, "check_suite_id": 42, "run_attempt": 2}
        if path.endswith("/actions/jobs/90"):
            return {
                "head_sha": shared.head,
                "run_id": 25,
                "id": 90,
                "check_run_url": f"https://api.github.com/repos/{shared.binding.repo}/check-runs/17",
                "run_attempt": 2,
                "steps": [{"name": "Compile Rust", "conclusion": "failure"}],
            }
        raise AssertionError(path)

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.get", get)
    diagnostic = "error[E0425]: cannot find value `widget`"
    download = AsyncMock(return_value=("x" * log_padding + "\n" + diagnostic + "\nPost job cleanup" * cleanup_lines, None))
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", download)
    async with shared.factory() as db:
        result = await observe_reviewer_checks(
            db,
            credential=envelope["run_report"]["credential"],
            head_sha=shared.head,
            provider=SimpleNamespace(token=AsyncMock(return_value="scoped-token")),
        )
    assert result["state"] == "failed"
    details = result["failures"][0]["details"]
    assert "src/main.rs: compile failed" in details
    assert "Failed step: Compile Rust" in details
    assert diagnostic in details
    if not cleanup_lines:
        assert details.endswith(diagnostic)
    assert len(details) <= 4000
    assert result["failures"][0]["details_source"] == "annotations_and_job_log"
    assert len(calls) == 4
    download.assert_awaited_once()


async def test_uncorrelated_job_keeps_annotations_without_inventing_logs(shared, monkeypatch):  # noqa: F811
    from src.orchestration.ci_failure_evidence import failure_details

    check = {
        "id": 17,
        "check_suite": {"id": 42},
        "details_url": f"https://github.com/{shared.binding.repo}/actions/runs/25/job/90",
    }
    get = AsyncMock(
        side_effect=[
            [{"annotation_level": "warning", "path": "src/main.rs", "message": "type mismatch"}],
            {"head_sha": shared.head, "check_suite_id": 42, "run_attempt": 2},
            {"head_sha": shared.head, "run_id": 25, "id": 90, "run_attempt": 1},
        ]
    )
    observer = SimpleNamespace(get=get, client=object(), headers={})
    download = AsyncMock()
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", download)
    result = await failure_details(observer, shared.binding.repo, check, shared.head)
    assert "src/main.rs: type mismatch" in result["details"]
    assert result["details_unavailable_reason"] == "job_not_correlated"
    download.assert_not_awaited()


async def test_correlated_job_log_unavailable_keeps_annotations(monkeypatch):
    from src.orchestration.ci_failure_evidence import failure_details

    repo = "fictional/sample"
    head = "a" * 40
    check = {
        "id": 17,
        "check_suite": {"id": 42},
        "details_url": f"https://github.com/{repo}/actions/runs/25/job/90",
    }
    observer = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                [{"annotation_level": "failure", "path": "lib/mod.rs", "message": "compilation failed"}],
                {"head_sha": head, "check_suite_id": 42, "run_attempt": 2},
                {
                    "id": 90,
                    "head_sha": head,
                    "run_id": 25,
                    "run_attempt": 2,
                    "check_run_url": f"https://api.github.com/repos/{repo}/check-runs/17",
                    "steps": [{"name": "Build", "conclusion": "failure"}],
                },
            ]
        ),
        client=object(),
        headers={},
    )
    download = AsyncMock(return_value=("", "actions_read_unavailable"))
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", download)

    result = await failure_details(observer, repo, check, head)

    assert "lib/mod.rs: compilation failed" in result["details"]
    assert "Failed step: Build" in result["details"]
    assert "Job log unavailable (actions_read_unavailable)" in result["details"]
    assert result["details_unavailable_reason"] == "actions_read_unavailable"
    assert result["details_source"] == "annotations"
    download.assert_awaited_once()


@pytest.mark.parametrize(
    "destination,approved",
    [
        ("https://pipelines.actions.githubusercontent.com/logs?sig=fictional-signature", True),
        ("https://productionresultssa0.blob.core.windows.net/logs?sig=fictional-signature", True),
        ("https://attacker.invalid/logs?sig=fictional-signature", False),
        ("http://pipelines.actions.githubusercontent.com/logs", False),
        ("https://user@pipelines.actions.githubusercontent.com/logs", False),
        ("https://pipelines.actions.githubusercontent.com:444/logs", False),
        ("https://pipelines.actions.githubusercontent.com/logs#fragment", False),
    ],
)
async def test_job_log_redirect_never_forwards_installation_token(monkeypatch, destination, approved):
    import httpx

    from src.orchestration import ci_failure_evidence

    events = []
    token = "fictional-installation-token"

    def api_response(request):
        events.append("api")
        assert str(request.url) == "https://api.github.com/repos/fictional/sample/actions/jobs/90/logs"
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(302, headers={"Location": destination})

    def signed_response(request):
        events.append("signed")
        assert str(request.url) == destination
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b"compiler failed on another language"))

    client_type = httpx.AsyncClient

    def signed_client(**kwargs):
        assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
        return client_type(transport=httpx.MockTransport(signed_response), **kwargs)

    async with client_type(transport=httpx.MockTransport(api_response), trust_env=False) as client:
        monkeypatch.setattr(ci_failure_evidence.httpx, "AsyncClient", signed_client)
        log, reason = await ci_failure_evidence.download_job_log(client, {"Authorization": f"Bearer {token}"}, "fictional/sample", 90)

    assert events == (["api", "signed"] if approved else ["api"])
    assert (log, reason) == (("compiler failed on another language", None) if approved else ("", "unapproved_log_redirect"))
    assert token not in log and "fictional-signature" not in log


@pytest.mark.parametrize("header_name", ["Authorization", "Proxy-Authorization"])
@pytest.mark.parametrize("quote", ["", '"', "'"])
async def test_job_log_excerpt_is_bounded_and_redacted(monkeypatch, header_name, quote):
    from src.orchestration.ci_failure_evidence import failure_details, redact_details

    repo = "fictional/sample"
    head = "a" * 40
    token = "fictional-installation-token"
    configured = "fictional-configured-secret"
    basic_credential = "ZmljdGlvbmFsOnJldmlldy1maXh0dXJl"
    assert redact_details(basic_credential) == basic_credential
    header = f"{quote}{header_name}{quote}: {quote}Basic {basic_credential}{quote}"
    header_log = '{"headers":{' + header + "}}"
    github_token = "ghp_" + "x" * 32
    aws_key = "AKIA" + "A" * 16
    monkeypatch.setenv("CI_API_KEY", configured)
    check = {"id": 17, "check_suite": {"id": 42}, "details_url": f"https://github.com/{repo}/actions/runs/25/job/90"}
    observer = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                [{"annotation_level": "failure", "path": "src/main.rs", "message": f"compile error {configured}\n{header_log}"}],
                {"head_sha": head, "check_suite_id": 42, "run_attempt": 2},
                {
                    "head_sha": head,
                    "run_id": 25,
                    "id": 90,
                    "run_attempt": 2,
                    "check_run_url": f"https://api.github.com/repos/{repo}/check-runs/17",
                    "steps": [{"conclusion": "failure", "name": f"\x1b[31mCompile {token}\x1b[0m"}],
                },
            ]
        ),
        client=object(),
        headers={"Authorization": f"Bearer {token}"},
    )
    log = (
        "x" * 200_000
        + f"\nAuthorization: Basic {configured}\nBearer {token}\n{github_token}\n{aws_key}\n"
        + f"{header_log}\n"
        + f"https://pipelines.actions.githubusercontent.com/logs?sig={configured}\n"
        + "error: could not compile crate `sample`\n"
        + "Post job cleanup\n" * 1000
    )
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", AsyncMock(return_value=(log, None)))

    result = await failure_details(observer, repo, check, head)

    details = result["details"]
    assert "src/main.rs: compile error" in details
    assert "Failed step: Compile" in details
    assert "error: could not compile crate `sample`" in details
    assert len(details) <= 4000
    assert "[REDACTED]" in details or "[REDACTED_URL]" in details
    assert all(secret not in details for secret in (token, configured, github_token, aws_key, basic_credential, "sig="))
    assert "\x1b" not in details


async def test_reviewer_missing_actions_grant_keeps_annotations(shared, monkeypatch):  # noqa: F811
    import httpx

    await tick(shared)
    envelope = shared.calls[-1]
    obs = observation(shared.head)
    obs.requirements = replace(obs.requirements, checks=(CheckRequirement("compile", 9),), checks_declared=True)
    obs.checks = (CheckEvidence("compile", 9, "failure", 17, "check_run", 42),)
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=obs))
    called = []

    async def get(self, path, *, kind):
        called.append(path)
        if path.endswith("/check-runs/17"):
            return {
                "id": 17,
                "head_sha": shared.head,
                "name": "compile",
                "app": {"id": 9},
                "output": {},
                "details_url": "",
                "check_suite": {"id": 42},
            }
        if path.endswith("/annotations?per_page=20&page=1"):
            return [{"annotation_level": "failure", "path": "src/main.rs", "message": "missing manifest entry"}]
        raise AssertionError(f"Actions endpoint must not be called: {path}")

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.get", get)
    request = httpx.Request("POST", "https://api.github.com/app/installations/1/access_tokens")
    denial = httpx.HTTPStatusError(
        "permission not granted", request=request, response=httpx.Response(422, text="Permissions requested are not granted", request=request)
    )
    provider = SimpleNamespace(token=AsyncMock(side_effect=[denial, "fallback-scoped-token"]))
    async with shared.factory() as db:
        result = await observe_reviewer_checks(db, credential=envelope["run_report"]["credential"], head_sha=shared.head, provider=provider)
    assert result["state"] == "failed"
    assert "src/main.rs: missing manifest entry" in result["failures"][0]["details"]
    assert result["failures"][0]["details_unavailable_reason"] == "actions_read_unavailable"
    assert len(called) == 2
    assert [call.kwargs for call in provider.token.await_args_list] == [{"evidence": True, "actions": True}, {"evidence": True}]


async def test_evidence_token_requests_actions_read_only_for_reviewer(monkeypatch):
    from datetime import timedelta

    from src.orchestration.merge_provider import MergeProvider

    monkeypatch.setattr("src.orchestration.merge_provider.resolve_tenant_app_credentials", AsyncMock(return_value=(1, "fictional-key")))
    minted = []

    async def mint(app, key, installation, *, repositories, permissions):
        minted.append((repositories, permissions))
        return "fictional-token", (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    monkeypatch.setattr("src.orchestration.merge_provider.mint_installation_token_with_expiry", mint)
    binding = SimpleNamespace(org_id="fictional", repo="fictional/sample", installation_id=5)
    provider = MergeProvider()
    await provider.token(binding, evidence=True, actions=True)
    await provider.token(binding, evidence=True)
    assert minted[0][0] == ["sample"]
    assert minted[0][1]["actions"] == "read"
    assert "actions" not in minted[1][1]


async def test_job_log_download_rejects_oversized_transfer(monkeypatch):
    import httpx

    from src.orchestration import ci_failure_evidence

    requests = []
    token = "fictional-installation-token"
    signed_url = "https://pipelines.actions.githubusercontent.com/logs?sig=fictional-signature"
    payload = b"secret-never-in-evidence\n" + b"x" * ci_failure_evidence.MAX_LOG_BYTES

    def api_response(request):
        requests.append(request)
        assert request.headers["Authorization"] == f"Bearer {token}"
        return httpx.Response(302, headers={"Location": signed_url})

    def signed_response(request):
        requests.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    client_type = httpx.AsyncClient

    def signed_client(**kwargs):
        assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
        return client_type(transport=httpx.MockTransport(signed_response), **kwargs)

    async with client_type(transport=httpx.MockTransport(api_response), trust_env=False) as client:
        monkeypatch.setattr(ci_failure_evidence.httpx, "AsyncClient", signed_client)
        log, reason = await ci_failure_evidence.download_job_log(client, {"Authorization": f"Bearer {token}"}, "fictional/sample", 90)

    assert len(requests) == 2
    assert (log, reason) == ("", "job_log_too_large")
    assert all(value not in str((log, reason)) for value in (token, signed_url, "secret-never-in-evidence"))


@pytest.mark.parametrize("status,reason", [(403, "actions_read_unavailable"), (404, "job_log_unavailable"), (401, "job_log_unavailable")])
async def test_job_log_download_refuses_unavailable_api_without_redirect(status, reason):
    import httpx

    from src.orchestration.ci_failure_evidence import download_job_log

    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(status, text="not available")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
        log, actual = await download_job_log(client, {"Authorization": "Bearer fictional-token"}, "fictional/sample", 90)
    assert (log, actual) == ("", reason)
    assert calls == ["/repos/fictional/sample/actions/jobs/90/logs"]


@pytest.mark.parametrize("mismatch", ["run_head", "attempt", "check_run_url"])
async def test_job_identity_mismatch_refuses_log_even_with_annotations(monkeypatch, mismatch):
    from src.orchestration.ci_failure_evidence import failure_details

    repo = "fictional/sample"
    head = "a" * 40
    check = {"id": 17, "check_suite": {"id": 42}, "details_url": f"https://github.com/{repo}/actions/runs/25/job/90"}
    run = {"head_sha": head, "check_suite_id": 42, "run_attempt": 2}
    job = {
        "head_sha": head,
        "run_id": 25,
        "id": 90,
        "run_attempt": 2,
        "check_run_url": f"https://api.github.com/repos/{repo}/check-runs/17",
    }
    if mismatch == "run_head":
        run["head_sha"] = "b" * 40
    elif mismatch == "attempt":
        job["run_attempt"] = 1
    else:
        job["check_run_url"] = f"https://api.github.com/repos/{repo}/check-runs/18"
    observer = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                [{"annotation_level": "failure", "path": "src/main.rs", "message": "compilation failed"}],
                run,
                job,
            ]
        ),
        headers={},
    )
    download = AsyncMock()
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", download)
    result = await failure_details(observer, repo, check, head)
    assert "src/main.rs: compilation failed" in result["details"]
    assert result["details_unavailable_reason"] == "job_not_correlated"
    download.assert_not_awaited()


@pytest.mark.parametrize("outage", ["annotation", "run", "malformed_run", "broken_link"])
async def test_partial_github_outage_yields_safe_unavailable_details(monkeypatch, outage):
    from src.orchestration.ci_failure_evidence import failure_details
    from src.orchestration.merge_evidence import EligibilityReason, EvidenceUnavailableError

    repo = "fictional/sample"
    head = "a" * 40
    check = {"id": 17, "check_suite": {"id": 42}, "details_url": f"https://github.com/{repo}/actions/runs/25/job/90"}
    if outage == "broken_link":
        check["details_url"] = "https://[invalid-url"
    annotation = [{"annotation_level": "failure", "path": "src/main.rs", "message": "compilation failed"}]
    get = AsyncMock(
        side_effect=[
            EvidenceUnavailableError(EligibilityReason.PROVIDER_UNAVAILABLE) if outage == "annotation" else annotation,
            EvidenceUnavailableError(EligibilityReason.PROVIDER_UNAVAILABLE)
            if outage == "run"
            else []
            if outage == "malformed_run"
            else {"head_sha": head, "check_suite_id": 42, "run_attempt": 2},
            EvidenceUnavailableError(EligibilityReason.PROVIDER_UNAVAILABLE),
        ]
    )
    observer = SimpleNamespace(get=get, headers={})
    download = AsyncMock()
    monkeypatch.setattr("src.orchestration.ci_failure_evidence.download_job_log", download)
    result = await failure_details(observer, repo, check, head)
    assert result["details_unavailable_reason"] in {"job_metadata_unavailable", "job_not_correlated"}
    assert "Job log unavailable" in result["details"]
    if outage != "annotation":
        assert "src/main.rs: compilation failed" in result["details"]
    download.assert_not_awaited()


async def test_check_run_head_changes_during_detail_fetch_returns_typed_conflict(shared, monkeypatch):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]
    obs = observation(shared.head)
    obs.requirements = replace(obs.requirements, checks=(CheckRequirement("compile", 9),), checks_declared=True)
    obs.checks = (CheckEvidence("compile", 9, "failure", 17, "check_run", 42),)
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=obs))
    monkeypatch.setattr(
        "src.orchestration.reviewer_checks.GitHubMergeObserver.get",
        AsyncMock(return_value={"id": 17, "head_sha": "b" * 40, "output": {}}),
    )
    async with shared.factory() as db:
        with pytest.raises(RunReportError, match="review_checks_head_changed"):
            await observe_reviewer_checks(
                db,
                credential=envelope["run_report"]["credential"],
                head_sha=shared.head,
                provider=SimpleNamespace(token=AsyncMock(return_value="scoped-token")),
            )


@pytest.mark.parametrize("header_name", [None, "Authorization", "Proxy-Authorization"])
async def test_required_failures_are_capped_and_nonempty_provider_output_is_preserved(shared, monkeypatch, header_name):  # noqa: F811
    await tick(shared)
    envelope = shared.calls[-1]
    obs = observation(shared.head)
    obs.requirements = replace(
        obs.requirements,
        checks=tuple(CheckRequirement(f"build {number}", 9) for number in range(6)),
        checks_declared=True,
    )
    obs.checks = tuple(CheckEvidence(f"build {number}", 9, "failure", 17 + number, "check_run", 42) for number in range(6))
    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.observe", AsyncMock(return_value=obs))
    requests = []
    basic_credential = "ZmljdGlvbmFsOnJldmlldy1maXh0dXJl"

    async def get(self, path, *, kind):
        requests.append(path)
        check_id = int(path.rsplit("/", 1)[-1])
        assert path.endswith(f"/check-runs/{check_id}")
        output = {"summary": f"provided failure {check_id}"}
        if header_name:
            output["text"] = '{"headers":{"' + header_name + '":"Basic ' + basic_credential + '"}}'
        return {
            "id": check_id,
            "head_sha": shared.head,
            "name": f"build {check_id - 17}",
            "app": {"id": 9},
            "check_suite": {"id": 42},
            "output": output,
        }

    monkeypatch.setattr("src.orchestration.reviewer_checks.GitHubMergeObserver.get", get)
    fallback = AsyncMock()
    monkeypatch.setattr("src.orchestration.reviewer_checks.failure_details", fallback)
    async with shared.factory() as db:
        result = await observe_reviewer_checks(
            db,
            credential=envelope["run_report"]["credential"],
            head_sha=shared.head,
            provider=SimpleNamespace(token=AsyncMock(return_value="scoped-token")),
        )
    assert result["state"] == "failed"
    assert len(result["checks"]) == 6 and len(result["failures"]) == 4
    assert len(requests) == 4
    for number, failure in enumerate(result["failures"]):
        assert failure["details"].startswith(f"provided failure {17 + number}")
        if header_name:
            assert basic_credential not in failure["details"]
            assert "[REDACTED]" in failure["details"]
        else:
            assert failure["details"] == f"provided failure {17 + number}"
    fallback.assert_not_awaited()


async def test_unavailable_job_exposes_only_canonical_safe_link():
    from src.orchestration.ci_failure_evidence import failure_details
    from src.orchestration.merge_evidence import EligibilityReason, EvidenceUnavailableError

    repo = "fictional/sample"
    observer = SimpleNamespace(get=AsyncMock(side_effect=EvidenceUnavailableError(EligibilityReason.PROVIDER_UNAVAILABLE)), headers={})
    check = {"id": 17, "details_url": "https://attacker.invalid/logs?sig=fictional-signature"}
    result = await failure_details(observer, repo, check, "a" * 40)
    assert result["details_unavailable_reason"] == "job_not_correlated"
    assert result["details_url"] == "https://github.com/fictional/sample/runs/17"
    assert "fictional-signature" not in str(result)
