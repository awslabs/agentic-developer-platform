"""Exact-head, read-only repository eligibility and the production R1/A1 seam."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

# Reuse R1's real PostgreSQL ledger fixture and its serialized producer document.
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration.execution_policy import Action, AuthorizationContext, CredentialScope, ExecutionPolicy, PolicyLimits, stamp_policy
from src.orchestration.merge_evidence import (
    EligibilityReason,
    EligibilityState,
    EvidenceUnavailableError,
    GitHubMergeObserver,
    MergeEligibility,
    bounded_merge_summary,
    evaluate_observation,
)
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationNode, OrchestrationPullRequestBinding, OrchestrationWorkClaim
from src.orchestration.results import GitHubEvidenceSource
from src.orchestration.review_evidence import require_verified_state, validate_review_result
from tests.orchestration.test_review_evidence import _all_refs
from tests.orchestration.test_review_ingest_postgres import APPROVE, INSTALLATION, ORG
from tests.orchestration.test_review_ingest_postgres import pg_engine as r1_pg_engine
from tests.orchestration.test_review_ingest_postgres import pg_server as r1_pg_server
from tests.orchestration.test_review_ingest_postgres import pg_url as r1_pg_url
from tests.orchestration.test_review_ingest_postgres import sessions as r1_sessions
from tests.orchestration.test_review_ingest_postgres import story as r1_story

pg_server = r1_pg_server
pg_url = r1_pg_url
pg_engine = r1_pg_engine
sessions = r1_sessions
story = r1_story

NOW = datetime(2026, 9, 20, tzinfo=UTC)
HEAD = "a" * 40
BASE = "b" * 40
REPO = "acme/app"
FIXTURES = Path(__file__).parent / "fixtures" / "merge_evidence"


def required_rules():
    return [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "test", "integration_id": 9}], "strict_required_status_checks_policy": True},
        },
        {
            "type": "pull_request",
            "parameters": {
                "required_approving_review_count": 1,
                "require_code_owner_review": False,
                "require_last_push_approval": False,
                "dismiss_stale_reviews_on_push": True,
                "required_review_thread_resolution": True,
            },
        },
    ]


def provider_data():
    # Projected real GitHub response shape, with explicit scenario substitutions.
    pr = deepcopy(json.loads((FIXTURES / "pr-5542.json").read_text())["response"])
    pr.update(node_id="PR_12", number=12, state="open", merged=False, draft=False, mergeable=True, mergeable_state="clean", user={"id": 1})
    pr["head"] = {"sha": HEAD}
    pr["base"] = {"sha": BASE, "ref": "main", "repo": {"id": 42, "full_name": REPO}}
    return {
        "pr": pr,
        "rules": required_rules(),
        "protection": None,
        "branch": {"protected": False, "commit": {"sha": BASE}},
        "repository": {"id": 42, "full_name": REPO, "allow_merge_commit": True, "allow_squash_merge": True, "allow_rebase_merge": True},
        "checks": {
            "total_count": 1,
            "check_runs": [{"id": 55, "name": "test", "head_sha": HEAD, "status": "completed", "conclusion": "success", "app": {"id": 9}}],
        },
        "statuses": [],
        "reviews": [{"id": 7, "state": "APPROVED", "commit_id": HEAD, "user": {"id": 2}, "submitted_at": NOW.isoformat()}],
        "graphql": {
            "data": {
                "repository": {
                    "databaseId": 42,
                    "mergeCommitAllowed": True,
                    "squashMergeAllowed": True,
                    "rebaseMergeAllowed": True,
                    "pullRequest": {
                        "id": "PR_12",
                        "headRefOid": HEAD,
                        "baseRefOid": BASE,
                        "baseRef": {"name": "main", "target": {"oid": BASE}},
                        "reviewDecision": "APPROVED",
                    },
                }
            }
        },
    }


def binding():
    return SimpleNamespace(repo=REPO, pr_number=12, provider_repository_id=42, provider_pr_node_id="PR_12")


def transport(data, calls, on_request=None):
    def respond(request):
        calls.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer scoped-test-token"
        assert request.url.host == "api.github.com"
        if on_request:
            on_request(request)
        path = request.url.path
        if path == "/graphql":
            assert request.method == "POST"
            assert json.loads(request.content)["query"].lstrip().startswith("query(")
            key = (
                "capability"
                if "rulesets(first:100,includeParents:true)" in json.loads(request.content)["query"] and "capability" in data
                else "graphql"
            )
        else:
            assert request.method == "GET", "No repository mutation is permitted"
            if path.endswith("/rules/branches/main"):
                key = "rules"
            elif path.endswith("/protection"):
                key = "protection"
            elif path.endswith("/branches/main"):
                key = "branch"
            elif path.endswith("/check-runs"):
                key = "checks"
            elif path.endswith("/statuses"):
                key = "statuses"
            elif path.endswith("/reviews"):
                key = "reviews"
            elif path.endswith("/pulls/12"):
                key = "pr"
            elif path == "/repos/acme/app":
                key = "repository"
            else:
                pytest.fail(f"Unexpected provider read {path}")
        value = data[key]
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(404 if value is None else 200, json=value)

    return httpx.MockTransport(respond)


async def observe(data=None, on_request=None):
    calls = []
    async with httpx.AsyncClient(transport=transport(data or provider_data(), calls, on_request)) as client:
        observed = await GitHubMergeObserver(client, "scoped-test-token", lambda: NOW).observe(binding())
    return observed, calls


async def test_current_review_and_explicit_repository_checks_pass_without_writes():
    observed, calls = await observe()
    decision = evaluate_observation(observed)
    assert decision.eligible
    assert len(observed.sources) == len(calls) == 11
    assert observed.requirements.allowed_merge_methods == ("merge", "rebase", "squash")
    assert all(source.payload_sha256 and source.observed_at == NOW for source in observed.sources)
    assert bounded_merge_summary(decision.ledger_detail()) == decision.summary()


async def test_saved_pr_base_can_lag_the_current_merge_target():
    data = provider_data()
    tip = "c" * 40
    data["branch"]["commit"]["sha"] = tip
    data["graphql"]["data"]["repository"]["pullRequest"]["baseRef"]["target"]["oid"] = tip
    observed, _ = await observe(data)
    assert evaluate_observation(observed).eligible
    assert observed.base_sha == tip
    assert data["pr"]["base"]["sha"] == BASE


async def test_read_only_rest_projection_uses_explicit_graphql_merge_settings():
    data = provider_data()
    data["repository"] = {"id": 42, "full_name": REPO}
    data["graphql"]["data"]["repository"].update(mergeCommitAllowed=False, rebaseMergeAllowed=False)
    observed, calls = await observe(data)
    assert evaluate_observation(observed).eligible
    assert observed.requirements.allowed_merge_methods == ("squash",)
    assert len(calls) == 11


@pytest.mark.parametrize("defect", ["missing", "null", "string", "contradiction", "rest_null"])
async def test_unverified_merge_settings_refuse_even_with_passing_review_and_checks(defect):
    data = provider_data()
    repository = data["graphql"]["data"]["repository"]
    if defect == "missing":
        del repository["mergeCommitAllowed"]
    elif defect in {"null", "string"}:
        repository["mergeCommitAllowed"] = None if defect == "null" else "true"
    elif defect == "contradiction":
        repository["mergeCommitAllowed"] = False
    else:
        data["repository"]["allow_merge_commit"] = None
    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data)
    assert exc.value.reason is EligibilityReason.INCOMPLETE_OBSERVATION


async def test_all_merge_methods_disabled_remains_blocked_for_read_only_token():
    data = provider_data()
    data["repository"] = {"id": 42, "full_name": REPO}
    data["graphql"]["data"]["repository"].update(mergeCommitAllowed=False, rebaseMergeAllowed=False, squashMergeAllowed=False)
    observed, _ = await observe(data)
    assert observed.requirements.allowed_merge_methods == ()
    assert EligibilityReason.UNSUPPORTED_RULE in evaluate_observation(observed).reasons


def unavailable_rules_data():
    data = provider_data()
    captured = json.loads((FIXTURES / "main-rules.json").read_text())
    data["rules"] = httpx.Response(403, json=captured["response"])
    data["capability"] = {
        "data": {
            "repository": {
                "databaseId": 42,
                "rulesets": {"totalCount": 0, "nodes": [], "pageInfo": {"hasNextPage": False}},
                "ref": {"name": "main", "target": {"oid": BASE}, "branchProtectionRule": None},
            }
        }
    }
    return data


async def test_plan_unavailable_rules_require_positive_empty_graphql_evidence():
    observed, _ = await observe(unavailable_rules_data())
    assert evaluate_observation(observed).eligible
    assert observed.requirements.required_approvals == 0
    assert any(source.kind == "rules_capability_verification" for source in observed.sources)


@pytest.mark.parametrize(
    "defect", ["permission_denied", "protected", "repository", "base", "retargeted", "legacy_rule", "ruleset", "partial", "missing", "graphql_error"]
)
async def test_rules_capability_fallback_refuses_unverified_or_configured_rules(defect):
    data = unavailable_rules_data()
    repository = data["capability"]["data"]["repository"]
    if defect == "permission_denied":
        data["rules"] = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    elif defect == "protected":
        data["branch"]["protected"] = True
    elif defect == "repository":
        repository["databaseId"] = 43
    elif defect == "base":
        repository["ref"]["target"]["oid"] = "c" * 40
    elif defect == "retargeted":
        repository["ref"]["name"] = "other"
    elif defect == "legacy_rule":
        repository["ref"]["branchProtectionRule"] = {"id": "legacy"}
    elif defect == "ruleset":
        repository["rulesets"].update(totalCount=1, nodes=[{"id": "inherited"}])
    elif defect == "partial":
        repository["rulesets"]["pageInfo"]["hasNextPage"] = True
    elif defect == "missing":
        del repository["ref"]["branchProtectionRule"]
    else:
        data["capability"]["errors"] = [{"message": "unavailable"}]
    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data)
    assert exc.value.reason is EligibilityReason.RULES_UNAVAILABLE


@pytest.mark.parametrize("change", ["tip", "branch", "deleted", "protection", "late_tip"])
async def test_current_base_change_during_observation_refuses(change):
    data = provider_data()

    def mutate(request):
        if request.url.path == "/graphql":
            live = data["graphql"]["data"]["repository"]["pullRequest"]
            if change == "tip":
                live["baseRef"]["target"]["oid"] = "c" * 40
            elif change == "branch":
                live["baseRef"]["name"] = "other"
            elif change == "deleted":
                live["baseRef"] = None
            elif change == "protection":
                data["branch"]["protected"] = True
            elif change == "late_tip":
                data["branch"]["commit"]["sha"] = "c" * 40

    with pytest.raises(EvidenceUnavailableError):
        await observe(data, on_request=mutate)


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required", "stale"])
async def test_required_check_non_success_never_passes(conclusion):
    data = provider_data()
    data["checks"]["check_runs"][0]["conclusion"] = conclusion
    observed, _ = await observe(data)
    assert EligibilityReason.REQUIRED_CHECK_FAILED in evaluate_observation(observed).reasons


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
async def test_github_successful_check_conclusions_do_not_block_merge(conclusion):
    data = provider_data()
    data["checks"]["check_runs"][0]["conclusion"] = conclusion
    observed, _ = await observe(data)
    assert evaluate_observation(observed).eligible
    assert observed.checks[0].state == conclusion


@pytest.mark.parametrize(
    "defect,reason",
    [
        ("missing_check", EligibilityReason.REQUIRED_CHECK_MISSING),
        ("wrong_app", EligibilityReason.REQUIRED_CHECK_MISSING),
        ("pending", EligibilityReason.REQUIRED_CHECK_PENDING),
        ("missing_review", EligibilityReason.REVIEW_REQUIRED),
        ("self_review", EligibilityReason.REVIEW_REQUIRED),
        ("stale_review", EligibilityReason.REVIEW_REQUIRED),
        ("dismissed", EligibilityReason.REVIEW_REQUIRED),
        ("changes_requested", EligibilityReason.CHANGES_REQUESTED),
        ("draft", EligibilityReason.DRAFT),
        ("conflict", EligibilityReason.CONFLICT),
        ("unknown", EligibilityReason.MERGEABILITY_UNKNOWN),
        ("behind", EligibilityReason.BASE_OUTDATED),
        ("closed", EligibilityReason.PR_CLOSED),
        ("code_owner", EligibilityReason.REVIEW_REQUIRED),
    ],
)
async def test_provider_requirements_have_typed_non_eligible_outcomes(defect, reason):
    data = provider_data()
    if defect == "missing_check":
        data["checks"] = {"total_count": 0, "check_runs": []}
    elif defect == "wrong_app":
        data["checks"]["check_runs"][0]["app"]["id"] = 99
    elif defect == "pending":
        data["checks"]["check_runs"][0].update(status="in_progress", conclusion=None)
    elif defect == "missing_review":
        data["reviews"] = []
    elif defect == "self_review":
        data["reviews"][0]["user"]["id"] = 1
    elif defect == "stale_review":
        data["reviews"][0]["commit_id"] = BASE
    elif defect in {"dismissed", "changes_requested"}:
        data["reviews"][0]["state"] = defect.upper()
    elif defect == "draft":
        data["pr"]["draft"] = True
    elif defect == "conflict":
        data["pr"]["mergeable"] = False
    elif defect == "unknown":
        data["pr"]["mergeable"] = None
    elif defect == "behind":
        data["pr"]["mergeable_state"] = "behind"
    elif defect == "closed":
        data["pr"]["state"] = "closed"
    elif defect == "code_owner":
        data["rules"][1]["parameters"]["require_code_owner_review"] = True
        data["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
    observed, _ = await observe(data)
    outcome = evaluate_observation(observed)
    assert not outcome.eligible
    assert reason in outcome.reasons
    assert (outcome.state is EligibilityState.WAITING) == (defect in {"pending", "unknown"})


async def test_latest_submitted_opinion_wins_even_when_created_earlier():
    data = provider_data()
    later = deepcopy(data["reviews"][0])
    later.update(id=3, state="CHANGES_REQUESTED", submitted_at=(NOW + timedelta(minutes=1)).isoformat())
    data["reviews"].append(later)
    observed, _ = await observe(data)
    assert EligibilityReason.CHANGES_REQUESTED in evaluate_observation(observed).reasons


@pytest.mark.parametrize("rules", ["no-review-rule", "zero-approvals"])
async def test_repository_without_approval_requirement_needs_no_second_github_identity(rules):
    data = provider_data()
    data["reviews"] = []
    data["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
    if rules == "no-review-rule":
        data["rules"] = data["rules"][:1]
    else:
        data["rules"][1]["parameters"]["required_approving_review_count"] = 0
    observed, _ = await observe(data)
    assert observed.requirements.required_approvals == 0
    assert evaluate_observation(observed).eligible


async def test_optional_failure_is_ignored_only_when_repository_declared_required_contexts():
    data = provider_data()
    optional = deepcopy(data["checks"]["check_runs"][0])
    optional.update(id=77, name="optional", conclusion="failure")
    data["checks"]["check_runs"].append(optional)
    data["checks"]["total_count"] = 2
    observed, _ = await observe(data)
    assert evaluate_observation(observed).eligible
    data["rules"] = []
    observed, _ = await observe(data)
    assert EligibilityReason.REQUIRED_CHECK_FAILED in evaluate_observation(observed).reasons


@pytest.mark.parametrize("kind", ["required_deployments", "required_workflows", "code_scanning", "required_signatures", "new_unknown_requirement"])
async def test_unknown_or_unsupported_rules_deny(kind):
    data = provider_data()
    data["rules"].append({"type": kind})
    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data)
    assert exc.value.reason is EligibilityReason.UNSUPPORTED_RULE


@pytest.mark.parametrize("fixture,key", [("main-rules.json", "rules"), ("main-protection.json", "protection")])
async def test_actual_captured_inaccessible_rules_never_become_empty_policy(fixture, key):
    data = provider_data()
    captured = json.loads((FIXTURES / fixture).read_text())
    assert str(captured["response"]["status"]) == "403"
    data[key] = httpx.Response(403, json=captured["response"])
    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data)
    assert exc.value.reason is EligibilityReason.RULES_UNAVAILABLE


@pytest.mark.parametrize(
    "defect", ["partial_checks", "wrong_repository", "wrong_pr", "malformed_head", "unknown_review", "duplicate_review", "unavailable_protection"]
)
async def test_incomplete_or_mismatched_observation_refuses(defect):
    data = provider_data()
    if defect == "partial_checks":
        data["checks"]["total_count"] = 2
    elif defect == "wrong_repository":
        data["pr"]["base"]["repo"]["id"] = 43
    elif defect == "wrong_pr":
        data["pr"]["node_id"] = "another"
    elif defect == "malformed_head":
        data["pr"]["head"]["sha"] = ""
    elif defect == "unknown_review":
        data["reviews"][0]["state"] = "UNKNOWN_NEW"
    elif defect == "duplicate_review":
        data["reviews"].append(deepcopy(data["reviews"][0]))
    elif defect == "unavailable_protection":
        data["branch"]["protected"] = True
    with pytest.raises(EvidenceUnavailableError):
        await observe(data)


@pytest.mark.parametrize("part,reason", [("head", EligibilityReason.HEAD_CHANGED), ("base", EligibilityReason.BASE_CHANGED)])
async def test_pr_revision_is_rechecked_after_all_evidence_reads(part, reason):
    data = provider_data()
    reads = 0

    def change(request):
        nonlocal reads
        if request.url.path.endswith("/pulls/12"):
            reads += 1
            if reads == 2:
                data["pr"][part]["sha"] = "c" * 40

    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data, change)
    assert exc.value.reason is reason


async def test_queue_and_repository_merge_method_requirements_are_retained():
    data = provider_data()
    data["rules"].append({"type": "merge_queue", "parameters": {"merge_method": "SQUASH"}})
    observed, _ = await observe(data)
    assert evaluate_observation(observed).eligible
    assert observed.requirements.queue_required
    assert observed.requirements.allowed_merge_methods == ("squash",)


def test_read_model_whitelists_summary_and_ignores_other_action_details():
    from src.orchestration.execution_read import _action_view
    from src.orchestration.models import OrchestrationAction

    result = MergeEligibility(EligibilityState.BLOCKED, (EligibilityReason.AUTHORITY_DENIED,), NOW)
    detail = result.ledger_detail()
    detail["secret"] = "must not appear"
    row = OrchestrationAction(id="a", operation_key="op", kind="merge_eligibility", status="succeeded", attempt=1, detail=detail)
    view = _action_view(row)
    assert view.evidence_summary == result.summary()
    assert "secret" not in json.dumps(view.evidence_summary)
    row.kind = "another_action"
    assert _action_view(row).evidence_summary is None


@pytest.mark.parametrize(
    "raw", ["[]", "not JSON", "x" * 2049, '{"state":"eligible","reasons":["authority_denied"],"observed_at":"2026-09-20T00:00:00+00:00"}']
)
def test_bad_saved_evidence_does_not_reach_the_operator(raw):
    assert bounded_merge_summary({"merge_eligibility": raw}) is None


@pytest.fixture
async def merge_subject(sessions, story, monkeypatch):
    policy = stamp_policy(
        ExecutionPolicy(
            org_id=ORG,
            repository_ids=[APPROVE["repository"]["repo"]],
            allowed_actions=[Action.MERGE],
            expires_at=NOW + timedelta(days=1),
            limits=PolicyLimits(max_wall_clock_seconds=86400, max_spend_usd=Decimal("10"), max_attempts_per_node=10, max_concurrent_actions=5),
        ),
        principal_id="plan-owner",
        org_id=ORG,
    )
    async with sessions() as session:
        plan = (await session.scalars(select(OrchestrationAcceptedPlan))).one()
        plan.plan_document = {"execution_policy": policy.model_dump(mode="json")}
        node = (await session.scalars(select(OrchestrationNode))).one()
        row = (await session.scalars(select(OrchestrationPullRequestBinding))).one()
        row.accepted_scope = json.dumps({"node": {"kind": node.kind, "issue_ref": node.issue_ref, "title": node.title}})
        await session.commit()
        doc = deepcopy(APPROVE)
        doc["scope"].update(flow_id=story["flow_id"], node_id=story["node_id"], execution_id=story["execution_id"])
        doc["lineage"]["author_run_id"] = story["author_run"]
        evidence = validate_review_result(
            doc,
            identity=story["identity"],
            binding=row,
            flow_id=story["flow_id"],
            author_run_id=story["author_run"],
            reviewer_run_id=doc["lineage"]["reviewer_run_id"],
            execution_id=story["execution_id"],
            actual_head_sha=row.head_sha,
            trusted_artifact_refs=_all_refs(doc),
        )
        require_verified_state(evidence)
    data = provider_data()
    repo = row.repo
    data["pr"].update(number=row.pr_number, node_id=row.provider_pr_node_id)
    data["pr"]["base"]["repo"].update(id=row.provider_repository_id, full_name=repo)
    data["pr"]["head"]["sha"] = row.head_sha
    data["checks"]["check_runs"][0]["head_sha"] = row.head_sha
    data["reviews"][0]["commit_id"] = row.head_sha
    data["repository"].update(id=row.provider_repository_id, full_name=repo)
    data["graphql"]["data"]["repository"].update(databaseId=row.provider_repository_id)
    data["graphql"]["data"]["repository"]["pullRequest"].update(id=row.provider_pr_node_id, headRefOid=row.head_sha)
    ctx = AuthorizationContext(
        policy=policy,
        accepted_plan_version=story["identity"].accepted_plan_version,
        in_force_plan_version=story["identity"].accepted_plan_version,
        principal_id="plan-owner",
        member_org_id=ORG,
        principal_can_authorize=True,
        credential_scope=CredentialScope.SCOPED,
        observed_spend_usd=Decimal("1"),
        now=NOW,
    )
    contexts = [ctx, ctx]
    calls = []

    async def authorization():
        return contexts.pop(0)

    async def credentials(org):
        assert org == ORG
        return "app-id", "test-key"

    mint = AsyncMock(return_value=("scoped-test-token", (NOW + timedelta(hours=1)).isoformat()))
    monkeypatch.setattr("src.orchestration.dispatch_pass.resolve_installation_id", AsyncMock(return_value=INSTALLATION))
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", credentials)
    monkeypatch.setattr("src.knowledge.github_app_service.mint_installation_token_with_expiry", mint)
    original_transport = transport

    def runtime_transport(request):
        # Route the real bound repo and PR into the same representative provider fixture.
        original_path = request.url.path
        fixture_path = original_path.replace(f"/repos/{repo}", "/repos/acme/app").replace(f"/pulls/{row.pr_number}", "/pulls/12")
        translated = httpx.Request(request.method, request.url.copy_with(path=fixture_path), headers=request.headers, content=request.content)
        handler = original_transport(data, calls)
        return handler.handle_request(translated)

    return SimpleNamespace(
        story=story,
        row=row,
        evidence=evidence,
        context=ctx,
        contexts=contexts,
        authorization=authorization,
        calls=calls,
        data=data,
        mint=mint,
        transport=httpx.MockTransport(runtime_transport),
    )


async def invoke_subject(sessions, subject, **kwargs):
    async with sessions() as session, httpx.AsyncClient(transport=subject.transport) as client:
        return await GitHubEvidenceSource().merge_eligibility(
            session=session,
            identity=kwargs.get("identity", subject.story["identity"]),
            review=kwargs.get("review", subject.evidence),
            authorization_reader=subject.authorization,
            client=client,
            clock=lambda: NOW,
        )


async def test_public_adapter_reloads_r1_claim_policy_and_mints_only_scoped_reads(sessions, merge_subject):
    result = await invoke_subject(sessions, merge_subject)
    assert result.eligible, result
    assert merge_subject.contexts == []
    kwargs = merge_subject.mint.call_args.kwargs
    assert kwargs["repositories"] == [merge_subject.row.repo.split("/", 1)[1]]
    assert kwargs["permissions"] and set(kwargs["permissions"].values()) == {"read"}
    assert len(merge_subject.calls) == 11


async def test_public_adapter_accepts_verified_reviewer_run_without_formal_github_approval(sessions, merge_subject):
    data = merge_subject.data
    data["reviews"] = []
    data["rules"][1]["parameters"]["required_approving_review_count"] = 0
    data["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
    result = await invoke_subject(sessions, merge_subject)
    assert result.eligible, result


@pytest.mark.parametrize("change", ["revoked", "missing_context", "stale_context", "human_gate", "unknown_spend", "unknown_scope", "wrong_tenant"])
async def test_current_a1_denials_never_request_a_provider_token(sessions, merge_subject, change):
    ctx = merge_subject.context
    if change == "revoked":
        ctx = replace(ctx, grant_revoked=True)
    elif change == "missing_context":
        ctx = None
    elif change == "stale_context":
        ctx = replace(ctx, now=NOW - timedelta(minutes=2))
    elif change == "human_gate":
        ctx = replace(ctx, policy=ctx.policy.model_copy(update={"human_gates": [Action.MERGE]}))
    elif change == "unknown_spend":
        ctx = replace(ctx, observed_spend_usd=None)
    elif change == "unknown_scope":
        ctx = replace(ctx, credential_scope=CredentialScope.UNKNOWN)
    elif change == "wrong_tenant":
        ctx = replace(ctx, member_org_id="another-tenant")
    merge_subject.contexts[0] = ctx
    result = await invoke_subject(sessions, merge_subject)
    assert not result.eligible
    assert result.reasons[0] in {EligibilityReason.AUTHORITY_DENIED, EligibilityReason.AUTHORITY_UNAVAILABLE}
    merge_subject.mint.assert_not_called()
    assert merge_subject.calls == []


async def test_authority_revoked_during_provider_read_is_checked_again(sessions, merge_subject):
    merge_subject.contexts[1] = replace(merge_subject.context, grant_revoked=True)
    result = await invoke_subject(sessions, merge_subject)
    assert result.reasons == (EligibilityReason.AUTHORITY_DENIED,)
    assert result.authority_reason == "grant_revoked"
    assert merge_subject.calls


@pytest.mark.parametrize("change", ["claim", "scope", "halted", "attempt", "wrong_identity"])
async def test_protected_database_scope_blocks_before_provider_io(sessions, merge_subject, change):
    identity = merge_subject.story["identity"]
    async with sessions() as session:
        if change == "claim":
            claim = (await session.scalars(select(OrchestrationWorkClaim))).one()
            claim.generation += 1
        else:
            node = (await session.scalars(select(OrchestrationNode))).one()
            if change == "scope":
                node.title += " expanded"
            elif change == "halted":
                node.state = "halted"
            elif change == "attempt":
                node.attempts += 1
            elif change == "wrong_identity":
                identity = replace(identity, org_id="another-tenant")
        await session.commit()
    result = await invoke_subject(sessions, merge_subject, identity=identity)
    assert result.reasons == (EligibilityReason.SCOPE_CHANGED,)
    merge_subject.mint.assert_not_called()


async def test_required_check_with_colliding_failed_status_is_blocked():
    data = provider_data()
    data["statuses"] = [{"id": 91, "context": "test", "state": "failure"}]
    observed, _ = await observe(data)
    assert EligibilityReason.REQUIRED_CHECK_FAILED in evaluate_observation(observed).reasons


async def test_unverified_r1_evidence_cannot_reach_provider_reads(sessions, merge_subject):
    unverified = replace(merge_subject.evidence, unverified=("provider_head",))
    result = await invoke_subject(sessions, merge_subject, review=unverified)
    assert result.reasons == (EligibilityReason.REVIEW_INCOMPLETE,)
    merge_subject.mint.assert_not_called()


@pytest.mark.parametrize("change", ["claim", "binding", "scope"])
async def test_database_authority_changes_during_provider_read_are_reloaded(sessions, merge_subject, change):
    original = merge_subject.transport
    changed = False

    async def during(request):
        nonlocal changed
        if not changed and request.url.path.endswith("/check-runs"):
            changed = True
            async with sessions() as session:
                if change == "claim":
                    row = (await session.scalars(select(OrchestrationWorkClaim))).one()
                    row.generation += 1
                elif change == "binding":
                    row = (await session.scalars(select(OrchestrationPullRequestBinding))).one()
                    row.revision += 1
                else:
                    row = (await session.scalars(select(OrchestrationNode))).one()
                    row.title += " changed"
                await session.commit()
        return original.handle_request(request)

    merge_subject.transport = httpx.MockTransport(during)
    result = await invoke_subject(sessions, merge_subject)
    assert result.reasons == (EligibilityReason.SCOPE_CHANGED,)
    assert changed


async def test_authority_resolver_failure_is_typed_and_does_not_mint(sessions, merge_subject):
    async def unavailable():
        raise RuntimeError("authority store unavailable")

    merge_subject.authorization = unavailable
    result = await invoke_subject(sessions, merge_subject)
    assert result.reasons == (EligibilityReason.AUTHORITY_UNAVAILABLE,)
    merge_subject.mint.assert_not_called()


async def test_provider_redirect_is_not_followed_even_with_redirecting_injected_client():
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://another.example/leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        with pytest.raises(EvidenceUnavailableError):
            await GitHubMergeObserver(client, "scoped-test-token", lambda: NOW).observe(binding())
    assert len(calls) == 1


async def test_incomplete_pagination_reaches_a_fixed_bound():
    data = provider_data()
    data["rules"] = [{"type": "non_fast_forward"}] * 100
    calls = []
    async with httpx.AsyncClient(transport=transport(data, calls)) as client:
        with pytest.raises(EvidenceUnavailableError):
            await GitHubMergeObserver(client, "scoped-test-token", lambda: NOW).observe(binding())
    assert sum(path.endswith("/rules/branches/main") for _, path in calls) == 10


async def test_repeated_check_run_in_partial_pages_is_not_complete_evidence():
    data = provider_data()
    data["checks"]["check_runs"] *= 2
    data["checks"]["total_count"] = 2
    with pytest.raises(EvidenceUnavailableError):
        await observe(data)


@pytest.mark.parametrize("part", ["rule", "parameter", "legacy"])
async def test_new_repository_policy_fields_do_not_silently_pass(part):
    data = provider_data()
    if part == "rule":
        data["rules"].append({"type": "future_security_rule"})
    elif part == "parameter":
        data["rules"][1]["parameters"]["future_approval_requirement"] = True
    else:
        data["protection"] = {"future_security_requirement": {"enabled": True}}
    with pytest.raises(EvidenceUnavailableError) as exc:
        await observe(data)
    assert exc.value.reason is EligibilityReason.UNSUPPORTED_RULE


def test_captured_check_runs_and_provider_reviews_parse_as_the_actual_provider_shape():
    checks = json.loads((FIXTURES / "head-5542-check-runs.json").read_text())["response"]["check_runs"]
    reviews = json.loads((FIXTURES / "pr-5545-reviews.json").read_text())["response"]
    assert checks and reviews
    parsed_checks = GitHubMergeObserver._checks(checks, [], checks[0]["head_sha"])
    parsed_reviews = GitHubMergeObserver._reviews(reviews)
    assert parsed_checks and parsed_reviews
    assert all(c.provider_id > 0 for c in parsed_checks)
    assert all(r.submitted_at.tzinfo is not None for r in parsed_reviews)


async def test_unavailable_scoped_credentials_block_without_fallback(sessions, merge_subject):
    merge_subject.mint.side_effect = ValueError("scope unavailable")
    result = await invoke_subject(sessions, merge_subject)
    assert result.reasons == (EligibilityReason.AUTHORITY_UNAVAILABLE,)
    merge_subject.mint.assert_awaited_once()
    assert merge_subject.calls == []


async def test_short_page_with_next_link_cannot_hide_a_later_requested_change():
    data = provider_data()
    first = deepcopy(data["reviews"][0])
    second = deepcopy(first)
    second.update(id=8, state="CHANGES_REQUESTED", submitted_at=(NOW + timedelta(minutes=1)).isoformat())

    def pages(request):
        if request.url.path.endswith("/reviews"):
            if request.url.params["page"] == "1":
                data["reviews"] = httpx.Response(
                    200, json=[first], headers={"Link": '<https://api.github.com/repos/acme/app/pulls/12/reviews?page=2>; rel="next"'}
                )
            else:
                data["reviews"] = [second]

    observed, calls = await observe(data, pages)
    assert sum(path.endswith("/reviews") for _, path in calls) == 2
    assert EligibilityReason.CHANGES_REQUESTED in evaluate_observation(observed).reasons
