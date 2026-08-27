"""
Regression tests for the ingest Lambda's chat-dispatch tenant gate (issue #4233,
A-14 slice of #4071).

The bug: `handle_github_dispatch()` split `classification.repo` into owner/name
and immediately acted on it — created an issue, labeled it, or posted a comment —
without ever checking that the owner belonged to the caller's organization. The
one field that could identify the caller (`org_id`) was allowed to arrive empty
or as the placeholder `"default"`, in which case the request was let through and
merely logged. A user in tenant A could therefore steer a run at a repository
owned by tenant B.

These tests assert the OUTCOME — whether the dispatch happened, i.e. whether the
GitHub-touching helpers were called at all — not the presence of any particular
line of source. They fail on pre-fix code, where every case below dispatches.

Layering under test:
  1. org_id absent / "default"        -> rejected (fail closed)
  2. repo_owner outside configured org -> rejected (code-only allowlist)
  3. identity-index ownership mismatch -> rejected (needs IDENTITY_INDEX_TABLE)
  4. in-org owner + owning org_id      -> allowed (regression)
"""

from __future__ import annotations

import io
import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.conftest import mock_apigw_event

HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "gateway", "lambdas", "ingest"
)

# The org the ingest App is configured for, via GH_APP_SECRET_PREFIX.
CONFIGURED_ORG = "acme-corp"
FOREIGN_ORG = "evil-corp"

# Installation ids GitHub reports per org. Distinct so an ownership mismatch is
# unambiguous rather than an accidental collision.
INSTALLATION_BY_ORG = {CONFIGURED_ORG: 111, FOREIGN_ORG: 222}

SESSIONS_TABLE = "adp-dev-agent-gateway-sessions"
TASKS_QUEUE = "adp-dev-agent-gateway-tasks"
RESPONSES_QUEUE = "adp-dev-agent-gateway-responses.fifo"
TASKS_QUEUE_URL = f"https://sqs.us-east-1.amazonaws.com/123/{TASKS_QUEUE}"


@pytest.fixture(autouse=True)
def _patch_sys_path():
    original = sys.path.copy()
    sys.path.insert(0, HANDLER_DIR)
    yield
    sys.path = original


@pytest.fixture
def gate_env(monkeypatch):
    """Env for the ingest handler with a single configured GitHub org.

    GH_APP_SECRET_PREFIX is the ONLY place the org is stated — the gate derives
    it from there, so this fixture also pins that derivation.
    """
    monkeypatch.setenv("INPUT_QUEUE_URL", TASKS_QUEUE_URL)
    monkeypatch.setenv(
        "RESPONSE_QUEUE_URL",
        f"https://sqs.us-east-1.amazonaws.com/123/{RESPONSES_QUEUE}",
    )
    monkeypatch.setenv("SESSIONS_TABLE_NAME", SESSIONS_TABLE)
    monkeypatch.setenv("AWS_REGION_NAME", "us-east-1")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_USER_ID", "")
    monkeypatch.setenv("GH_APP_SECRET_PREFIX", f"adp/{CONFIGURED_ORG}/gh-app-ops")
    # Ownership layer off by default — enabled per-test via _enable_ownership().
    monkeypatch.delenv("IDENTITY_INDEX_TABLE", raising=False)


@pytest.fixture
def aws(gate_env):
    """moto DynamoDB + SQS with the tables/queues the handler expects."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=SESSIONS_TABLE,
            KeySchema=[{"AttributeName": "session_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "session_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs", region_name="us-east-1")
        sqs.create_queue(QueueName=TASKS_QUEUE)
        sqs.create_queue(QueueName=RESPONSES_QUEUE, Attributes={"FifoQueue": "true"})
        yield {"ddb": ddb, "table": table, "sqs": sqs}


def _bedrock_returning(classification: dict) -> MagicMock:
    """A Bedrock stub whose classification routes to github_actions."""
    body = json.dumps({
        "content": [{"type": "text", "text": json.dumps(classification)}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }).encode()
    client = MagicMock()
    client.invoke_model.return_value = {"body": io.BytesIO(body)}
    return client


def _dispatch_classification(repo: str) -> dict:
    return {
        "path": "github_actions",
        "persona": "developer",
        "repo": repo,
        "create_issue": True,
        "issue_title": "Fix the thing",
        "enriched_message": "Please fix the thing.",
        "escalation_note": "On it.",
        "thread_action": "new",
        "reasoning": "Code work on a specific repo",
    }


def _load_handler(classification: dict, monkeypatch):
    """Import the handler fresh with GitHub dispatch helpers stubbed out.

    The stubs are the assertion surface: if the gate lets a request through,
    `calls` records it. Real code would hit api.github.com here.
    """
    for mod in ("handler", "classifier", "channels", "channels.base",
                "channels.webchat", "channels.slack", "github_dispatch",
                "installation_resolver"):
        sys.modules.pop(mod, None)

    import github_dispatch
    import handler

    calls: dict[str, list] = {"create": [], "label": [], "comment": [], "token": []}

    def _create(repo_owner, repo_name, *a, **kw):
        calls["create"].append((repo_owner, repo_name))
        if not repo_owner:
            # Mirrors the real helper: no owner -> no installation token ->
            # {"dispatched": False}, which makes the handler fall through to
            # long_running. Faithfulness matters here, otherwise the owner-less
            # regression below would pass for the wrong reason.
            return {"dispatched": False, "error": "Could not get GitHub App token"}
        return {
            "dispatched": True,
            "issue_number": 7,
            "issue_url": f"https://github.com/{repo_owner}/{repo_name}/issues/7",
            "label": "agent-developer",
        }

    def _label(repo_owner, repo_name, issue_number, *a, **kw):
        calls["label"].append((repo_owner, repo_name, issue_number))
        if not repo_owner:
            return {"dispatched": False, "error": "Could not get GitHub App token"}
        return {
            "dispatched": True,
            "issue_number": issue_number,
            "issue_url": f"https://github.com/{repo_owner}/{repo_name}/issues/{issue_number}",
            "label": "agent-developer",
        }

    def _post_comment(token, owner, repo, issue_number, body):
        calls["comment"].append((owner, repo, issue_number))

    def _token(org):
        calls["token"].append(org)
        return "ghs_fake"

    monkeypatch.setattr(handler, "create_issue_and_dispatch", _create)
    monkeypatch.setattr(handler, "label_existing_issue", _label)
    monkeypatch.setattr(github_dispatch, "_post_comment", _post_comment)
    monkeypatch.setattr(github_dispatch, "_get_installation_token", _token)
    # The App's real view of which installation covers an org. Stubbed at the
    # GitHub boundary so the gate's own logic is what's under test.
    #
    # raising=False on purpose: on pre-fix code this helper does not exist, and
    # these tests must fail because the dispatch WENT THROUGH — not because a
    # symbol was missing. A setup AttributeError would prove nothing about the
    # vulnerability.
    monkeypatch.setattr(
        github_dispatch,
        "installation_id_for_org",
        lambda org: INSTALLATION_BY_ORG.get((org or "").strip().lower()),
        raising=False,
    )

    import classifier
    classifier._bedrock_client = _bedrock_returning(classification)

    return handler, calls


def _enable_ownership(monkeypatch, org_to_installation: dict):
    """Turn on the identity-index ownership layer with a canned org→installation map.

    Stands in for the manual `agent-factory-infra-apply.yml` that adds
    IDENTITY_INDEX_TABLE + the IAM/KMS grants.
    """
    monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")

    module = type(sys)("installation_resolver")
    module.resolve_installation_for_tenant = lambda org_id: org_to_installation.get(org_id)
    sys.modules["installation_resolver"] = module


def _send(handler, *, repo: str, org_id: str | None, session: str):
    """Deliver a webchat message whose classification targets `repo`."""
    claims = {"sub": f"user-{session}", "email": f"{session}@example.com"}
    if org_id is not None:
        claims["custom:org_id"] = org_id
    return handler.lambda_handler(
        mock_apigw_event(
            route_key="$default",
            body={"action": "message", "text": f"Fix the bug in {repo}",
                  "session_id": session},
            connection_id=f"conn-{session}",
            authorizer_claims=claims,
        ),
        None,
    )


def _no_github_side_effects(calls):
    return not (calls["create"] or calls["label"] or calls["comment"])


# ---------------------------------------------------------------------------
# 1. Foreign-owner repo -> rejected (the headline cross-tenant case)
# ---------------------------------------------------------------------------


class TestForeignOwnerRejected:
    def test_foreign_repo_owner_is_not_dispatched(self, aws, monkeypatch):
        """A chat message naming another tenant's repo creates nothing.

        Pre-fix: create_issue_and_dispatch is called with the foreign owner.
        """
        handler, calls = _load_handler(
            _dispatch_classification(f"{FOREIGN_ORG}/secret-repo"), monkeypatch
        )
        result = _send(handler, repo=f"{FOREIGN_ORG}/secret-repo",
                       org_id=CONFIGURED_ORG, session="sess-foreign")

        assert result["statusCode"] == 403
        assert json.loads(result["body"])["status"] == "rejected_cross_tenant"
        assert _no_github_side_effects(calls)

    def test_foreign_owner_leaves_no_thread_record(self, aws, monkeypatch):
        """A denied dispatch must not persist a thread the user can follow up on."""
        handler, calls = _load_handler(
            _dispatch_classification(f"{FOREIGN_ORG}/secret-repo"), monkeypatch
        )
        _send(handler, repo=f"{FOREIGN_ORG}/secret-repo",
              org_id=CONFIGURED_ORG, session="sess-foreign-thread")

        item = aws["table"].get_item(
            Key={"session_id": "sess-foreign-thread"}
        ).get("Item", {})
        assert item.get("threads", {}) == {}

    def test_foreign_owner_does_not_fall_through_to_long_running(self, aws, monkeypatch):
        """Denial is terminal — it must not silently become a worker task.

        Falling through to long_running would hand the same foreign repo_owner
        to the agent worker over SQS, which is the same escalation by another
        route.
        """
        handler, calls = _load_handler(
            _dispatch_classification(f"{FOREIGN_ORG}/secret-repo"), monkeypatch
        )
        _send(handler, repo=f"{FOREIGN_ORG}/secret-repo",
              org_id=CONFIGURED_ORG, session="sess-foreign-sqs")

        msgs = aws["sqs"].receive_message(
            QueueUrl=TASKS_QUEUE_URL, MaxNumberOfMessages=10, WaitTimeSeconds=0
        ).get("Messages", [])
        assert msgs == []
        # Asserted together: an empty queue alone would also be satisfied by
        # pre-fix code, where the dispatch SUCCEEDED and therefore never reached
        # the long_running fallback. Both must hold — no issue, and no task.
        assert _no_github_side_effects(calls)

    def test_case_differences_do_not_bypass_the_allowlist(self, aws, monkeypatch):
        """Owner comparison is case-insensitive — GitHub logins are."""
        repo = f"{FOREIGN_ORG.upper()}/secret-repo"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-foreign-case")

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)

    def test_foreign_owner_rejected_on_the_follow_up_comment_path(self, aws, monkeypatch):
        """The gate also covers follow-ups, which post a comment via the App.

        The follow-up branch runs before issue creation and mints a token for
        repo_owner, so a gate placed only on the create path would leave it open.
        """
        classification = _dispatch_classification(f"{FOREIGN_ORG}/secret-repo")
        classification.update({"thread_action": "follow_up",
                               "follow_up_thread_id": "thr-1"})
        handler, calls = _load_handler(classification, monkeypatch)

        # Seed a session carrying a github thread for that follow-up id.
        aws["table"].put_item(Item={
            "session_id": "sess-foreign-followup",
            "threads": {"thr-1": {
                "topic": "earlier work",
                "path": "github_actions",
                "github_issue_number": 5,
                "github_issue_url": f"https://github.com/{FOREIGN_ORG}/secret-repo/issues/5",
                "created_at": 1,
            }},
            "messages": [],
        })

        result = _send(handler, repo=f"{FOREIGN_ORG}/secret-repo",
                       org_id=CONFIGURED_ORG, session="sess-foreign-followup")

        assert result["statusCode"] == 403
        assert calls["comment"] == []
        assert calls["token"] == []


# ---------------------------------------------------------------------------
# 2. Absent / placeholder org_id -> rejected (fail closed)
# ---------------------------------------------------------------------------


class TestOrgIdFailsClosed:
    @pytest.mark.parametrize(
        "org_id, label",
        [(None, "absent"), ("", "empty"), ("default", "placeholder"),
         ("DEFAULT", "placeholder-uppercase"), ("   ", "whitespace")],
    )
    def test_unusable_org_id_is_rejected(self, aws, monkeypatch, org_id, label):
        """An org_id that names no tenant cannot authorize a dispatch.

        Pre-fix this was logged and allowed — the gap that made cross-tenant
        targeting reachable on the actions path. Note the in-org repo: the ONLY
        reason to deny is the unusable org_id.
        """
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        result = _send(handler, repo=repo, org_id=org_id,
                       session=f"sess-org-{label}")

        assert result["statusCode"] == 403
        assert json.loads(result["body"])["status"] == "rejected_cross_tenant"
        assert _no_github_side_effects(calls)

    def test_tenant_id_cannot_substitute_for_org_id(self, aws, monkeypatch):
        """The gate reads org_id, not tenant_id.

        tenant_id is always "" on this path. If the gate had been written
        against tenant_id it would be a silent no-op; if it accepted tenant_id
        as a fallback, a caller could pass this check without an org_id. A
        tenant_id-only caller must still be rejected.
        """
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        result = handler.lambda_handler(
            mock_apigw_event(
                route_key="$default",
                body={"action": "message", "text": f"Fix {repo}",
                      "session_id": "sess-tenant-only"},
                connection_id="conn-tenant-only",
                authorizer_claims={"sub": "user-tenant-only",
                                   "custom:tenant_id": CONFIGURED_ORG},
            ),
            None,
        )

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)


# ---------------------------------------------------------------------------
# 3. Ownership layer (identity-index)
# ---------------------------------------------------------------------------


class TestOwnershipLayer:
    def test_org_id_owning_a_different_installation_is_rejected(self, aws, monkeypatch):
        """An org_id spelled like the org but owning another installation fails.

        This is what makes the check an OWNERSHIP assertion rather than label
        equality: the owner passes the allowlist, and is still denied because
        the caller's org resolves to a different installation.
        """
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        _enable_ownership(monkeypatch, {CONFIGURED_ORG: INSTALLATION_BY_ORG[FOREIGN_ORG]})

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-own-mismatch")

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)

    def test_org_id_resolving_to_no_installation_is_rejected(self, aws, monkeypatch):
        """No installation for the caller's org -> unverifiable -> denied."""
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        _enable_ownership(monkeypatch, {})

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-own-unresolved")

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)

    def test_resolver_error_fails_closed(self, aws, monkeypatch):
        """A resolver that raises must deny, not degrade to allow."""
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        monkeypatch.setenv("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")

        module = type(sys)("installation_resolver")

        def _boom(org_id):
            raise RuntimeError("AccessDeniedException on dynamodb:GetItem")

        module.resolve_installation_for_tenant = _boom
        sys.modules["installation_resolver"] = module

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-own-error")

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)

    def test_matching_ownership_is_allowed(self, aws, monkeypatch):
        """In-org repo + org_id owning that installation -> dispatched."""
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        _enable_ownership(
            monkeypatch, {CONFIGURED_ORG: INSTALLATION_BY_ORG[CONFIGURED_ORG]}
        )

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-own-match")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "dispatched_github"
        assert calls["create"] == [(CONFIGURED_ORG, "app")]


# ---------------------------------------------------------------------------
# 4. Regressions — legitimate in-org dispatch keeps working
# ---------------------------------------------------------------------------


class TestInOrgDispatchStillWorks:
    def test_in_org_dispatch_allowed_before_the_terraform_apply(self, aws, monkeypatch):
        """With IDENTITY_INDEX_TABLE unset, the org allowlist alone allows in-org.

        This is the on-merge posture: the code gate ships with the Lambda, the
        ownership layer stays inert until `agent-factory-infra-apply.yml` runs.
        """
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)
        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-inorg")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "dispatched_github"
        assert calls["create"] == [(CONFIGURED_ORG, "app")]

    def test_in_org_label_existing_issue_allowed(self, aws, monkeypatch):
        """The label-an-existing-issue branch is unaffected for in-org repos."""
        repo = f"{CONFIGURED_ORG}/app"
        classification = _dispatch_classification(repo)
        classification.update({"create_issue": False, "issue_number": 42})
        handler, calls = _load_handler(classification, monkeypatch)

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-inorg-label")

        assert result["statusCode"] == 200
        assert calls["label"] == [(CONFIGURED_ORG, "app", 42)]

    def test_owner_less_repo_still_falls_through_to_long_running(self, aws, monkeypatch):
        """A repo with no owner ("myrepo") is not a cross-tenant target.

        It cannot dispatch anywhere, so it keeps its pre-existing behaviour of
        falling through to long_running rather than being rejected — rejecting
        it would deny a legitimate in-org ask that merely omitted the owner.
        """
        classification = _dispatch_classification("myrepo")
        handler, calls = _load_handler(classification, monkeypatch)

        result = _send(handler, repo="myrepo", org_id=CONFIGURED_ORG,
                       session="sess-noowner")

        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "processing"
        # It reached the dispatch attempt (not a 403) and that attempt could not
        # target any owner — so nothing cross-tenant happened.
        assert calls["create"] == [("", "myrepo")]

    def test_unconfigured_github_org_fails_closed(self, aws, monkeypatch):
        """No GH_APP_SECRET_PREFIX -> no derivable org -> deny.

        The prefix being unset is already a Terraform misconfiguration that
        makes dispatch fail at token-fetch time; the gate must not treat
        "cannot determine my org" as "any org is fine".
        """
        monkeypatch.setenv("GH_APP_SECRET_PREFIX", "")
        repo = f"{CONFIGURED_ORG}/app"
        handler, calls = _load_handler(_dispatch_classification(repo), monkeypatch)

        result = _send(handler, repo=repo, org_id=CONFIGURED_ORG,
                       session="sess-noprefix")

        assert result["statusCode"] == 403
        assert _no_github_side_effects(calls)


# ---------------------------------------------------------------------------
# 5. Org derivation from GH_APP_SECRET_PREFIX
# ---------------------------------------------------------------------------


class TestConfiguredOrgDerivation:
    """Unit coverage for the derivation helper itself.

    Unlike the classes above, these are white-box: they name the new function,
    so on pre-fix code they fail with AttributeError rather than by
    demonstrating the vulnerability. The behavioural proof lives above; this
    class exists to pin the parsing edge cases (malformed prefix -> "", which
    the callers turn into a denial) that a black-box test cannot reach.
    """

    @pytest.mark.parametrize(
        "prefix, expected",
        [
            ("adp/acme-corp/gh-app-ops", "acme-corp"),
            ("adp/ACME-Corp/gh-app-ops", "acme-corp"),
            ("adp/acme-corp/gh-app-ops/", "acme-corp"),
            ("", ""),
            ("adp", ""),
            ("adp/acme-corp", ""),
        ],
    )
    def test_org_derived_from_prefix(self, gate_env, monkeypatch, prefix, expected):
        """The org comes from the existing secret prefix — no second env var.

        Malformed prefixes must yield "" so callers fail closed rather than
        matching a partial string.
        """
        monkeypatch.setenv("GH_APP_SECRET_PREFIX", prefix)
        sys.modules.pop("github_dispatch", None)
        import github_dispatch

        assert github_dispatch.configured_github_org() == expected
