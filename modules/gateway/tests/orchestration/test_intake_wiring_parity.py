"""The intake wiring lands in all four places, or it lands nowhere (#5331, EPIC #4191).

`intake_wiring.py` degrades to "unavailable" whenever its environment is absent, which
is the right runtime behaviour and a terrible failure mode for a *deployment* mistake:
a half-landed wiring edit produces a gateway that starts cleanly, serves every other
route, and answers 503 on intake forever. Nothing crashes, no log says "misconfigured",
and CI is green — because the three variables are read through `os.environ.get` with a
`None` default, which is exactly what a missing configmap key looks like.

So the four places are checked against each other here, at source level, because no
runtime test can see a manifest or a workflow:

1. `intake_wiring.py` — the env var names the code actually reads.
2. `k8s/configmap.yaml` — the keys the pod receives, as placeholders.
3. `.github/workflows/gateway-deploy.yml` — the SSM reads and the `sed` substitutions
   that render those placeholders.
4. `modules/agent-factory/infra/gateway-intake-access.tf` — the SSM parameters that
   stack publishes, and the IAM policy that makes the reads possible at all.

The specific breakages this catches, each of which is silent:

- a placeholder in the configmap that nothing substitutes, so the pod receives the
  literal `__BG_INTAKE_SESSIONS_TABLE__` and treats it as a table name;
- a `get_ssm` read whose parameter path differs by a character from the one Terraform
  publishes, so the default wins forever;
- a renamed constant in `intake_wiring.py` that leaves the manifest keyed to the old
  name, which reads as "configured" to an operator and as "absent" to the code.

Modelled on `tests/activity/test_agent_control_flag_parity.py`, which exists for the
same reason on the neighbouring feature: reading the manifest and the workflow as
*text* is deliberate, because trusting the convention is what lets the fourth edit get
dropped.

This file asserts on *wiring*, not on IAM being correct in production. The policy
assertions below are the negative kind — what must NOT be granted — because those are
the ones a reviewer cannot re-derive from the plan output, and because this PR's
author cannot apply Terraform to find out.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.orchestration import intake_wiring

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _GATEWAY_ROOT.parents[1]
_K8S_CONFIGMAP = _GATEWAY_ROOT / "k8s" / "configmap.yaml"
_DEPLOY_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "gateway-deploy.yml"
_INTAKE_TF = _REPO_ROOT / "modules" / "agent-factory" / "infra" / "gateway-intake-access.tf"

# Derived from the module under test rather than written out, so a rename cannot leave
# this file asserting that the old names are still wired somewhere.
_ENV_VARS = {
    "sessions": intake_wiring.SESSIONS_TABLE_ENV,
    "context": intake_wiring.CONTEXT_TABLE_ENV,
    "function": intake_wiring.INTAKE_FUNCTION_ENV,
}

# The SSM parameter each variable is rendered from. The one place the mapping between
# a pod env var and a parameter path is written down in this test; both the workflow
# and the Terraform are checked against it, so agreeing with each other is not enough
# — they have to agree with this.
_SSM_PARAMS = {
    "sessions": "/adp/${ENVIRONMENT}/agent-gateway/sessions-table",
    "context": "/adp/${ENVIRONMENT}/agent-gateway/context-table",
    "function": "/adp/${ENVIRONMENT}/agent-gateway/ingest-function",
}

_KEYS = sorted(_ENV_VARS)


def _without_comments(text: str) -> str:
    """The same text with whole-line `#` comments removed.

    Every negative assertion below ("this action is not granted", "this variable is
    not read") has to run against the *effective* configuration, because all three
    files document at length the things they deliberately do not do — naming
    `dynamodb:Scan`, `sqs:SendMessage` and `BG_INTAKE_QUEUE_URL` precisely to explain
    their absence. Matching raw text would turn each of those explanations into a
    failure, and the obvious way to make such a test pass is to delete the comment
    that explains the boundary.

    Whole-line only, and `#` inside a quoted string is therefore safe: HCL and YAML
    both use `#`, neither file has a trailing-comment-after-code line that matters
    here, and a conservative rule that leaves too much in can only ever make a
    negative assertion stricter — never falsely pass one.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def configmap() -> str:
    assert _K8S_CONFIGMAP.exists(), f"{_K8S_CONFIGMAP} not found"
    return _K8S_CONFIGMAP.read_text()


@pytest.fixture(scope="module")
def workflow() -> str:
    assert _DEPLOY_WORKFLOW.exists(), f"{_DEPLOY_WORKFLOW} not found"
    return _DEPLOY_WORKFLOW.read_text()


@pytest.fixture(scope="module")
def intake_tf() -> str:
    assert _INTAKE_TF.exists(), (
        f"{_INTAKE_TF} not found. The gateway cannot read the sessions table, the "
        "context table or invoke the ingest Lambda without the grants in this file; "
        "without it every intake verb is AccessDenied, which the reader reports as "
        "unavailable."
    )
    return _INTAKE_TF.read_text()


class TestEveryVariableIsRenderedIntoThePod:
    """A placeholder with no substitution is worse than a missing key.

    A missing key leaves `os.environ.get` returning `None`, which the builders map to
    an honest "unavailable". An unsubstituted placeholder is a non-empty string, so
    `is_configured` is True and the gateway confidently calls DynamoDB with
    `__BG_INTAKE_SESSIONS_TABLE__` as the table name.
    """

    @pytest.mark.parametrize("key", _KEYS)
    def test_the_configmap_declares_the_variable_as_a_placeholder(self, configmap, key):
        env_var = _ENV_VARS[key]
        declared = [line.strip() for line in configmap.splitlines() if line.strip().startswith(f"{env_var}:")]
        assert len(declared) == 1, f"expected exactly one {env_var} in k8s/configmap.yaml, found {declared!r}"
        assert declared[0] == f'{env_var}: "__{env_var}__"', (
            f"{env_var} must ship as its own placeholder, got {declared[0]!r}. A committed literal would "
            "name one environment's table in every environment — and pointing at another environment's "
            "table is a cross-environment read, not a missing one."
        )

    @pytest.mark.parametrize("key", _KEYS)
    def test_the_workflow_substitutes_that_placeholder(self, workflow, key):
        env_var = _ENV_VARS[key]
        assert f"s|__{env_var}__|" in workflow, (
            f"nothing in gateway-deploy.yml substitutes __{env_var}__, so the pod would receive the "
            "literal placeholder. That is a non-empty value, so the gateway reports itself configured "
            "and fails on every call instead of reporting unavailable."
        )

    @pytest.mark.parametrize("key", _KEYS)
    def test_the_workflow_reads_the_parameter_terraform_publishes(self, workflow, key):
        param = _SSM_PARAMS[key]
        assert f'get_ssm "{param}"' in workflow, (
            f"gateway-deploy.yml must read {param}. A path that differs by one character from the "
            "published parameter silently takes the default on every deploy, forever."
        )

    @pytest.mark.parametrize("key", _KEYS)
    def test_terraform_publishes_the_parameter_the_workflow_reads(self, intake_tf, key):
        # `${ENVIRONMENT}` is the workflow's shell variable; Terraform writes the same
        # path with its own interpolation, so compare on the environment-independent
        # remainder.
        suffix = _SSM_PARAMS[key].split("}", 1)[1]
        assert f'/adp/${{var.environment}}{suffix}"' in intake_tf, (
            f"no aws_ssm_parameter in gateway-intake-access.tf publishes ...{suffix}, but "
            "gateway-deploy.yml reads it. The deploy would fall back to its convention-derived "
            "default, which is right until the day the convention changes."
        )


class TestTheQueueShortcutCannotComeBack:
    """`BG_INTAKE_QUEUE_URL` is not read anywhere, and must stay unread.

    The audited defect was a dispatcher that wrote straight to the worker's SQS queue,
    producing a turn for a conversation that had no session row, no thread and no
    registered run. Tolerating the old variable as a fallback would let a deployment
    still carrying it resume exactly that path — and it would look like a successful
    dispatch, which is how the original defect survived CI.
    """

    def test_the_gateway_does_not_read_the_old_queue_variable(self):
        source = Path(intake_wiring.__file__).read_text()
        reads = re.findall(r'os\.environ(?:\.get)?\(\s*["\']?(BG_INTAKE_QUEUE_URL)', source)
        assert not reads, "intake_wiring.py must not read BG_INTAKE_QUEUE_URL, even as a fallback"

    def test_the_function_variable_names_a_function_not_a_queue(self):
        assert intake_wiring.INTAKE_FUNCTION_ENV == "BG_INTAKE_INGEST_FUNCTION"
        assert "QUEUE" not in intake_wiring.INTAKE_FUNCTION_ENV

    def test_nothing_renders_a_queue_url_for_intake(self, configmap, workflow):
        # Comment-stripped: both files name the old variable in prose to explain why it
        # is gone, and a test that failed on the explanation would be fixed by deleting
        # the explanation.
        assert "BG_INTAKE_QUEUE_URL" not in _without_comments(configmap)
        assert "BG_INTAKE_QUEUE_URL" not in _without_comments(workflow)


class TestTheGrantIsReadOnlyAndNarrow:
    """What the policy must NOT allow.

    Asserted negatively and at source level because a reviewer cannot re-derive it
    from a plan they cannot run, and because every item here is a boundary some other
    test depends on rather than a preference:

    - legacy intake remains read-only; the separately scoped chat-* Task journal
      has standing PutItem permission, asserted independently below;
    - `dynamodb:Scan` would make a cross-tenant read a matter of forgetting a filter
      instead of a permission error, and `latest_for_user` filters tenant in code
      precisely because the GSI cannot;
    - `sqs:SendMessage` is the queue shortcut again, one grant away.
    """

    _FORBIDDEN = [
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:Scan",
        "sqs:SendMessage",
    ]

    @pytest.mark.parametrize("action", _FORBIDDEN)
    def test_the_policy_does_not_grant(self, intake_tf, action):
        # Against the effective policy, not the prose: the file names every one of
        # these to explain its absence.
        assert action not in _without_comments(intake_tf), (
            f"gateway-intake-access.tf grants {action}, which this path must not have. "
            "See the class docstring: each of these turns a tested invariant back into an accident."
        )

    def test_put_item_is_only_for_the_task_chat_journal(self, intake_tf):
        effective = _without_comments(intake_tf)
        statements = re.split(r'Sid\s*=\s*"', effective)
        writes = [part for part in statements if "dynamodb:PutItem" in part]
        assert len(writes) == 1
        grant = writes[0]
        assert grant.startswith('HostedTaskChatSessionsWrite"')
        assert re.search(r'Action\s*=\s*\["dynamodb:PutItem"\]', grant)
        assert re.search(r"Resource\s*=\s*\[module.gateway_sessions.table_arn\]", grant)
        assert re.search(r'"ForAllValues:StringLike"\s*=\s*\{\s*"dynamodb:LeadingKeys"\s*=\s*\["chat-\*"\]\s*\}', grant)

    def test_no_wildcard_resource(self, intake_tf):
        effective = _without_comments(intake_tf)
        assert '"*"' not in effective, "an intake grant must name its resources; a wildcard resource makes the scoping untestable"
        # `dynamodb:*` / `lambda:*` would satisfy the action list above while granting
        # everything, so the action wildcard is refused separately.
        assert not re.search(r'"(?:dynamodb|lambda|kms|sqs):\*"', effective), "an intake grant must name its actions, not a service wildcard"

    def test_the_index_is_granted_alongside_the_table(self, intake_tf):
        """DynamoDB treats a GSI as a separate resource.

        A policy with only the table ARN makes `GET /intake/sessions/latest`
        AccessDenied while `GET /intake/sessions/{id}` succeeds — which reads as
        "resume is broken" rather than as a missing permission, and sends whoever
        debugs it into the query code.
        """
        assert "/index/*" in intake_tf, "the resume path queries user-workspace-index, which needs its own ARN in the policy"

    def test_the_grant_attaches_to_the_gateway_service_role(self, intake_tf):
        """By literal name, which is the established cross-stack idiom here.

        The alternative — `terraform_remote_state` on agent-factory from the gateway
        stack — would invert a dependency that runs gateway → agent-factory and create
        a cycle. Pinned so a later edit does not "fix" the literal name into a data
        source.
        """
        assert 'role = "adp-${var.environment}-role-gateway-service"' in intake_tf
        # The file explains in prose why it does not use a remote-state read, so the
        # check is against the effective configuration.
        assert "terraform_remote_state" not in _without_comments(intake_tf)

    def test_kms_decrypt_is_granted(self, intake_tf):
        """Both tables are SSE-KMS, so without this every read fails at the KMS layer.

        Its own test because the symptom is indistinguishable from a broken table
        reference: an AccessDenied that names KMS, not DynamoDB, in a log nobody reads
        until drafts come back empty.
        """
        assert "kms:Decrypt" in intake_tf
