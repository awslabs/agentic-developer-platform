"""IAM least-privilege tests for the cyber sample-read grants (issue #5616).

Finding #4730 was an IAM pattern, `o/*/in/*`, that looked tenant-scoped and was
not: an IAM wildcard also matches `/`, so that one `*` spanned every org, team
and user, at any depth. The mistake is easy to reintroduce and invisible on
review, so these tests read the Terraform and evaluate the patterns the way IAM
does.

What these tests can and cannot prove is worth being precise about. All tenants'
jobs share one worker role, so no static pattern can separate org A from org B —
there is no per-tenant value for IAM to substitute. The cross-tenant boundary is
enforced in the worker (sample_access.py) before any download. These tests pin
the reachable *shape* of keys: `in/` objects at the canonical ingest depth, plus
a separate `scripts/` subtree for Mode B, and nothing else. That is the
defence-in-depth layer, and it is what regresses silently.
"""

import fnmatch
import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[2] / "infra"
WORKER_TF = INFRA / "worker-irsa.tf"
CAPE_TF = INFRA / "cape-host.tf"

ENV = "dev"
ACCOUNT = "123456789012"


def _resource_arns(path: Path) -> list[str]:
    """Every S3 ARN string in a Terraform file, with variables resolved."""
    text = path.read_text()
    arns = re.findall(r'"(arn:aws:s3:::[^"]+)"', text)
    resolved = []
    for arn in arns:
        arn = arn.replace("${var.environment}", ENV)
        arn = arn.replace("${var.sample_bucket_name}", f"adp-{ENV}-chat-artifacts")
        arn = arn.replace("${var.account_id}", ACCOUNT)
        resolved.append(arn)
    return resolved


def _key_patterns(path: Path) -> list[str]:
    """S3 object-key patterns (bucket/key), excluding bucket-only ARNs."""
    patterns = []
    for arn in _resource_arns(path):
        body = arn.removeprefix("arn:aws:s3:::")
        if "/" in body:
            patterns.append(body)
    return patterns


def _artifact_patterns(path: Path) -> list[str]:
    """Only the patterns against the multi-tenant artifacts bucket."""
    return [p for p in _key_patterns(path) if "chat-artifacts" in p]


def _matches(key: str, pattern: str) -> bool:
    """Evaluate an IAM resource pattern against a key.

    IAM `*` matches any sequence including `/`, which is precisely the property
    that made the original pattern unsafe; fnmatchcase has the same semantics.
    """
    return fnmatch.fnmatchcase(key, pattern)


TENANT_A = "o/acme/t/team-a/u/user-1"
TENANT_B = "o/victim-org/t/team-x/u/user-9"
BUCKET = f"adp-{ENV}-chat-artifacts-{ACCOUNT}"

POLICY_FILES = {"worker-irsa.tf": WORKER_TF, "cape-host.tf": CAPE_TF}


@pytest.fixture(params=sorted(POLICY_FILES), ids=sorted(POLICY_FILES))
def policy_file(request):
    return request.param, POLICY_FILES[request.param]


class TestNoUnanchoredOrgWildcard:
    """The exact regression from #4730."""

    def test_no_pattern_uses_bare_org_wildcard(self, policy_file):
        name, path = policy_file
        for pattern in _artifact_patterns(path):
            assert "/o/*/in/" not in pattern, (
                f"{name}: '{pattern}' reintroduces the o/*/in/* grant — the "
                "wildcard spans every org, team and user"
            )

    def test_patterns_are_anchored_through_the_user_segment(self, policy_file):
        """org/team/user must each be their own wildcard segment.

        A pattern that stops anchoring early (o/*/...) permits any depth, which
        is how a single grant ended up covering all tenants.
        """
        name, path = policy_file
        for pattern in _artifact_patterns(path):
            assert re.search(r"/o/\*/t/\*/u/\*/", pattern), (
                f"{name}: '{pattern}' is not anchored through o/<org>/t/<team>/u/<user>"
            )


class TestOnlyIntendedShapesAreReachable:
    def test_canonical_sample_key_is_readable(self, policy_file):
        """The legitimate path must still work, or this gets reverted."""
        name, path = policy_file
        key = f"{BUCKET}/{TENANT_A}/s/sess-1/task-1/in/sample.bin"
        assert any(_matches(key, p) for p in _artifact_patterns(path)), (
            f"{name}: legitimate sample is not readable"
        )

    def test_analysis_output_is_not_readable(self, policy_file):
        """`out/` holds analysis results, which workers never need to read.

        Excluding it means a bypassed code check cannot mine other tenants'
        finished reports.
        """
        name, path = policy_file
        key = f"{BUCKET}/{TENANT_B}/s/sess-9/task-9/out/report.md"
        assert not any(_matches(key, p) for p in _artifact_patterns(path)), (
            f"{name}: '{key}' is readable; out/ must not be"
        )

    def test_shallow_key_outside_canonical_layout_is_not_readable(self, policy_file):
        name, path = policy_file
        for key in (
            f"{BUCKET}/o/victim-org/in/secret.bin",
            f"{BUCKET}/in/secret.bin",
            f"{BUCKET}/o/victim-org/t/team-x/in/secret.bin",
        ):
            assert not any(_matches(key, p) for p in _artifact_patterns(path)), (
                f"{name}: '{key}' is readable outside the canonical layout"
            )

    def test_arbitrary_object_in_tenant_space_is_not_readable(self, policy_file):
        """Being inside a tenant prefix is not sufficient — only in/ and scripts/."""
        name, path = policy_file
        key = f"{BUCKET}/{TENANT_A}/s/sess-1/task-1/notes/private.txt"
        assert not any(_matches(key, p) for p in _artifact_patterns(path)), (
            f"{name}: '{key}' is readable; only in/ and scripts/ should be"
        )


class TestModeBScriptGrant:
    """Scripts and samples must be separately addressable (finding #4729)."""

    def test_worker_can_read_a_script_under_the_tenant_script_prefix(self):
        key = f"{BUCKET}/{TENANT_A}/scripts/art-1/stage-3.py"
        assert any(_matches(key, p) for p in _artifact_patterns(WORKER_TF)), (
            "Mode B script is not readable — the worker cannot fetch what it must verify"
        )

    def test_script_grant_does_not_cover_sample_inputs(self):
        """A grant that covered both would let an uploaded sample be executed."""
        script_patterns = [p for p in _artifact_patterns(WORKER_TF) if "scripts" in p]
        assert script_patterns, "no scripts/ grant found"
        sample_key = f"{BUCKET}/{TENANT_A}/s/sess-1/task-1/in/sample.bin"
        for pattern in script_patterns:
            assert not _matches(sample_key, pattern), (
                f"scripts grant '{pattern}' also matches a sample input"
            )

    def test_cape_host_has_no_script_grant(self):
        """CAPE runs samples dynamically; it has no Mode B path."""
        assert not [p for p in _artifact_patterns(CAPE_TF) if "scripts" in p], (
            "CAPE host has a scripts/ grant it does not need"
        )


class TestNoUnusedCredentialGrant:
    def test_worker_role_has_no_secrets_manager_grant(self):
        """Neither worker reads a secret.

        These pods parse hostile binaries and execute generated scripts, so an
        unused credential grant is exactly what an escape would reach for.
        """
        text = WORKER_TF.read_text()
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        assert "secretsmanager:GetSecretValue" not in code, (
            "worker role grants Secrets Manager access that no worker uses"
        )
