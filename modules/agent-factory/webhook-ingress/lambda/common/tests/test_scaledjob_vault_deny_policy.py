"""Static checks on the cross-tenant vault Deny in the agent IRSA policies.

Issue #4130 (#4073 finding #4): the agent-worker pod's IRSA role granted
``secretsmanager:GetSecretValue`` + ``DescribeSecret`` on ``secret:adp/*`` (Sid
``SecretsManagerOps``) plus ``ListSecrets`` on ``*`` (Sid ``Multiple``). The
customer vault lives under the same ``adp/`` prefix, so a run triggered by one
customer could read every other customer's stored credentials. The fix is an
explicit, unconditional ``Deny`` on the four vault namespaces.

Vault secret paths, per ``gateway/src/shared/services/secrets_manager.py``::

    adp/users/<cognito_sub>/<service>-<id>
    adp/teams/<team_id>/<service>-<id>
    adp/orgs/<org_id>/<service>-<id>
    adp/domain-apps/<app_id>/<org_id>/<service>-<id>

Note the total absence of an environment segment — cross-checked against
``gateway/infra/main.tf``, which grants the gateway CRUD on exactly these four
env-less namespaces. That is why ``test_deny_resources_have_no_env_segment``
exists: an ``adp/${environment}/*``-shaped Deny matches *nothing*, so the fix
would be completely inert while reading, in review, as though the hole were
closed. That is the failure mode this file is most concerned with.

These tests read the Terraform sources as text — no ``terraform`` binary, no AWS
credentials, no network — so they run in plain unit mode. Deliberately NOT
``iam:SimulatePrincipalPolicy``: unit CI has no AWS credentials (adopted
decision 7 of #4073). Mirrors
``agent-context/tests/terraform/test_s3_vectors_policy.py`` and the cross-module
path arithmetic of ``test_marker_lockstep.py`` (#1696).

WHY THIS FILE LIVES UNDER ``lambda/common/tests/``: ``webhook-ingress-ci.yml``
runs ``pytest lambda/ -m "not integration"`` and nothing else. A test placed in
``webhook-ingress/tests/terraform/`` would never execute in any workflow — a
policy guard that never runs is not a guard.

This file is inert at Lambda runtime: nothing under ``common/`` imports it and no
handler references it. (It *is* present in the built zip — ``package-lambdas.sh``
applies its ``*/tests/*`` exclusion only to the in-process-modules loop, so all
60+ existing ``common/tests/`` files ship too. That is pre-existing packaging
behaviour, not something this test introduces; trimming it belongs in its own
change.)
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

# lambda/common/tests/ -> lambda/common -> lambda -> webhook-ingress
_WEBHOOK_INGRESS = Path(__file__).resolve().parents[3]
_AGENT_FACTORY = _WEBHOOK_INGRESS.parent

SCALEDJOB_IAM_TF = _WEBHOOK_INGRESS / "infra" / "scaledjob-iam.tf"
RUNNER_IAM_TF = _AGENT_FACTORY / "infra" / "modules" / "runner-iam" / "main.tf"

# The Sid carrying the vault lockout, shared by both roles.
DENY_SID = "DenyTenantVaultSecrets"

# The four customer vault namespaces, exactly as they appear in secret ids.
VAULT_NAMESPACES = ("adp/users/", "adp/teams/", "adp/orgs/", "adp/domain-apps/")

# Both actions are required. A GetSecretValue-only Deny still leaks other
# tenants' secret names and metadata via DescribeSecret.
DENIED_ACTIONS = ("secretsmanager:DescribeSecret", "secretsmanager:GetSecretValue")


def _executable_hcl(path: Path) -> str:
    """Return the file's HCL with whole-line ``#`` comments stripped.

    Load-bearing: the comments in both files legitimately quote the vault paths,
    the wrong ``adp/${environment}/*`` shape, and the bare ``adp/*`` form in
    order to explain the fix. Asserting against raw text would let prose satisfy
    a policy assertion — the test would pass on a file whose *comments* describe
    a Deny that its HCL never declares. Same reason
    ``test_s3_vectors_policy.py`` strips comments before matching.
    """
    assert path.is_file(), (
        f"missing {path} — this test's path arithmetic is stale. Fix the path "
        "rather than skipping, or the #4130 cross-tenant vault guard silently "
        "stops covering anything."
    )
    return "\n".join(
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _deny_statement(path: Path) -> str:
    """Return the ``DenyTenantVaultSecrets`` statement body as executable HCL.

    Slices from the Sid to the end of that statement block so an assertion about
    "the Deny" cannot be satisfied by an unrelated Allow elsewhere in the policy
    (``SecretsManagerOps`` also mentions ``adp/``, and ``Multiple`` also mentions
    ``secretsmanager:``).
    """
    hcl = _executable_hcl(path)
    start = hcl.find(f'Sid    = "{DENY_SID}"')
    if start == -1:
        start = hcl.find(DENY_SID)
    assert start != -1, (
        f"no Sid {DENY_SID!r} in {path.name}: the agent role can still read "
        "every tenant's vault secrets (#4130 / #4073 finding #4)"
    )
    end = re.search(r"\n {6}\},?(?=\n)", hcl[start:])
    assert end is not None, "Cannot find the end of the vault deny statement"
    return hcl[start:start + end.start()]


class TestScaledjobVaultDeny:
    """The fix: the agent-worker IRSA role must deny the customer vault.

    This is the sole control for the exposure — vault secrets are created at
    runtime under the AWS-managed key, so #4028's CMK scoping does not reach
    them. Every test in this class fails on the pre-#4130 file.
    """

    def test_deny_statement_present_and_is_a_deny(self):
        """The statement exists and is Effect=Deny, not a narrowed Allow.

        A Deny is required rather than a tightened Allow because the worker
        legitimately reads its own tenant's github-app secret and the tenant is
        unknown at plan time. An explicit Deny beats any Allow, including the
        ``SecretsManagerOps`` grant on ``adp/*`` in the same policy.
        """
        stmt = _deny_statement(SCALEDJOB_IAM_TF)
        assert 'Effect = "Deny"' in stmt, (
            f"{DENY_SID} is not an Effect=Deny statement; an Allow here would be "
            "out-voted by SecretsManagerOps' grant on adp/*"
        )

    @pytest.mark.parametrize("action", DENIED_ACTIONS)
    def test_both_actions_denied(self, action):
        """Both GetSecretValue and DescribeSecret are denied.

        Denying only GetSecretValue still lets a pod enumerate other tenants'
        secret names and metadata.
        """
        assert action in _deny_statement(SCALEDJOB_IAM_TF), (
            f"{action} is not denied — cross-tenant "
            f"{'reads' if 'Get' in action else 'name/metadata enumeration'} "
            "remain possible"
        )

    @pytest.mark.parametrize("namespace", VAULT_NAMESPACES)
    def test_all_four_namespaces_denied(self, namespace):
        """Every one of the four vault namespaces is covered.

        Missing one leaves that entire tenant class fully readable.
        """
        assert namespace in _deny_statement(SCALEDJOB_IAM_TF), (
            f"vault namespace {namespace!r} is not in the Deny — secrets under it "
            "stay readable by every tenant's agent runs"
        )

    def test_deny_resources_have_no_env_segment(self):
        """The Deny resources carry no ``${environment}``/``${var.environment}``.

        THE INERT-FIX GUARD. Vault paths have no env segment, so an
        ``adp/${environment}/*``-shaped Deny matches no real secret while
        appearing, in review, to close the hole. This is the highest-risk way
        for this fix to be wrong, because it fails open and looks closed.
        """
        stmt = _deny_statement(SCALEDJOB_IAM_TF)
        for namespace in VAULT_NAMESPACES:
            tail = namespace[len("adp/") :]
            assert f"adp/{tail}" in stmt, (
                f"{namespace} not present in its env-less form"
            )

        for bad in ("adp/${var.environment}", "adp/${local.env", "adp/${var.env"):
            assert bad not in stmt, (
                f"Deny resource is env-segmented ({bad}...) — vault paths have NO env "
                "segment, so this Deny matches nothing and the fix is inert"
            )

    def test_worker_own_github_app_read_survives(self):
        """The Deny must not cover the worker's own github-app secret.

        The worker resolves its tenant secret to
        ``adp/<env>/tenants/<tenant_id>/github-app``
        (``agent-worker-image/lib/vault_client.py``) — env-segmented and under a
        ``tenants/`` segment, so it sits outside all four vault namespaces. A
        too-broad Deny here would break GitHub authentication on *every* agent
        run, so this asserts the shapes cannot collide.
        """
        stmt = _deny_statement(SCALEDJOB_IAM_TF)
        assert "tenants/" not in stmt, (
            "the Deny covers a tenants/ path — that is where the worker's own "
            "github-app secret lives; every agent run would fail GitHub auth"
        )
        assert "github-app" not in stmt, (
            "the Deny names github-app — the worker's legitimate read of its own "
            "tenant credential must survive"
        )

    def test_deny_not_broadened_to_whole_adp_prefix(self):
        """The Deny is scoped to the four namespaces, not all of ``adp/*``.

        Denying ``secret:adp/*`` would also deny the platform github-app key,
        the marker-signing key and the worker's own tenant secret — a total
        outage of the agent path rather than a security fix.
        """
        stmt = _deny_statement(SCALEDJOB_IAM_TF)
        assert "secret:adp/*" not in stmt, (
            "Deny is broadened to secret:adp/* — this denies the worker's own "
            "github-app and marker-signing-key reads and breaks every agent run"
        )

    def test_deny_is_account_scoped(self):
        """Resources are account-scoped, mirroring the DenyTenantAwsAccess precedent."""
        stmt = _deny_statement(SCALEDJOB_IAM_TF)
        assert "${local.account_id}" in stmt or "aws_caller_identity" in stmt, (
            "Deny resources are not account-scoped; mirror the DenyTenantAwsAccess "
            "precedent in agent-factory/infra/gateway-main.tf"
        )


class TestRunnerBoundaryVaultDeny:
    """Defence in depth ONLY — this does not close #4073 finding #4.

    The binding grant on the runner role is ``aws_iam_policy.runner_base`` Sid
    ``AllowBroadAccess``, holding ``secretsmanager:*`` on ``Resource="*"``. That
    is broader than this finding and is tracked separately in **#4116**; nothing
    in this class should be read as addressing it.

    The Deny is asserted to live in the *permissions boundary*, which is the
    ceiling on the role's effective permissions — so it is reachable regardless
    of what any attached policy allows, and cannot be out-voted by that
    ``secretsmanager:*`` grant.
    """

    def test_runner_boundary_denies_vault_namespaces(self):
        """The boundary denies reads and mutations in all tenant namespaces.

        Accept Terraform formatting, wildcard actions and a referenced local
        list without letting an unrelated statement or a comment satisfy the
        assertion. Rendered-policy tests also cover this in runner-iam/tests.
        """
        hcl = _executable_hcl(RUNNER_IAM_TF)
        boundary = re.search(
            r'^resource "aws_iam_policy" "runner_boundary"\s*\{(.*?)(?=^resource |\Z)',
            hcl,
            re.MULTILINE | re.DOTALL,
        )
        assert boundary is not None, (
            "aws_iam_policy.runner_boundary not found — if the boundary was "
            "renamed or removed, re-home this Deny; a Deny outside the boundary "
            "can be out-voted by runner_base's secretsmanager:* grant (#4116)"
        )

        stmt = _deny_statement(RUNNER_IAM_TF)
        assert stmt in boundary.group(1), (
            f"{DENY_SID} is not inside the runner_boundary policy; placed in an "
            "attached policy instead it is bypassable"
        )
        assert re.search(r'\bEffect\s*=\s*"Deny"', stmt)
        assert not re.search(r"\bCondition\s*=", stmt), (
            "tenant lockout must be unconditional"
        )
        action_value = re.search(r'\bAction\s*=\s*("[^"]+"|\[[^\]]*\])', stmt)
        assert action_value is not None
        actions = re.findall(r'"([^"]+)"', action_value.group(1))
        for action in (
            *DENIED_ACTIONS,
            "secretsmanager:PutSecretValue",
            "secretsmanager:PutResourcePolicy",
        ):
            assert any(fnmatch.fnmatchcase(action, pattern) for pattern in actions), (
                f"runner boundary does not deny {action}"
            )
        resource_value = re.search(r"\bResource\s*=\s*local\.([A-Za-z0-9_]+)", stmt)
        assert resource_value is not None, (
            "resolve the boundary's actual resource expression"
        )
        local_name = resource_value.group(1)
        resources = re.search(
            rf"^\s*{re.escape(local_name)}\s*=\s*\[(.*?)\n\s*\]",
            hcl,
            re.MULTILINE | re.DOTALL,
        )
        assert resources is not None, (
            f"referenced resource local {local_name} is missing"
        )
        arns = re.findall(r'"([^"]+)"', resources.group(1))
        expected = {
            f"arn:aws:secretsmanager:*:${{data.aws_caller_identity.current.account_id}}:secret:{namespace}*"
            for namespace in (*VAULT_NAMESPACES, "adp/*/tenants/")
        }
        assert set(arns) == expected, (
            "the referenced Deny must cover exactly the tenant vault namespaces"
        )

    def test_runner_deny_has_no_env_segment(self):
        """Same inert-fix guard as the scaledjob role."""
        stmt = _deny_statement(RUNNER_IAM_TF)
        for bad in ("adp/${var.environment}", "adp/${local.env", "adp/${var.env"):
            assert bad not in stmt, f"runner boundary Deny is env-segmented ({bad}...)"
