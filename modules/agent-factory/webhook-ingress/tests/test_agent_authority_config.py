"""Deployment-config guards for the protected dispatch path (issue #5365).

#5365 is a dispatch-authority outage: a coordinator lost the ability to dispatch
because chain depth was read from the newest row anywhere on the shared
correlation chain. The fix is that coordinators dispatch over the *authenticated*
per-run route, where the parent is derived server-side from a verified run
credential. Whether a worker actually takes that route is decided entirely here,
in Terraform, by environment variables on the pod — not by any code path that a
unit test of the client would exercise.

That makes three things load-bearing and invisible at runtime until they are
wrong, which is what these tests guard:

1. **The worker-side flag and the control endpoint ship together.** The client
   (``adp_trigger/client.py``) branches on ``ADP_AGENT_AUTHORITY_ENABLED`` and
   then derives its control base URL. Setting the flag without the endpoint, or
   the endpoint without the flag, does not fail loudly — it silently leaves the
   normal coordinator path on the legacy shared-IAM route, which is precisely the
   route that cannot authenticate *which invocation* is calling. A fallback to
   legacy is the bug, not a safe default.

2. **The pod gets a projected workload token.** The gateway verifies the run
   credential together with a projected serviceAccountToken of a dedicated
   audience. If the volume or mount disappears the credential cannot be
   presented, and every protected dispatch fails closed.

3. **Enabling authority cannot outrun its prerequisites.** The approved worker
   image digest allowlist is what TokenReview bootstrap admits pods against.
   Terraform must refuse to enable authority with an empty allowlist rather than
   create signing material for an unverifiable fleet.

These read the ``.tf`` source as text, matching ``test_scaledjob_manifest.py``
and ``test_agent_control_manifest.py``: the ScaledJob is a heredoc applied via
kubectl local-exec, so no plan or cluster harness exists in the unit suite. No
AWS, no cluster, no terraform binary, no new dependencies.

The committed-tfvars test deliberately asserts that the flag is **off** in the
committed file. That is not a preference for the feature being off — it is the
deployment-ordering rule this module already documents twice in
``terraform.tfvars``: ``webhook-ingress-deploy.yml`` auto-applies on push under
``infra/**``, so a committed ``true`` applies on merge, ahead of the operator
step that supplies the image digests it depends on.
"""

from __future__ import annotations

from pathlib import Path
import json

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra"
BOOTSTRAP_TF = INFRA / "agent-authority-bootstrap.tf"
SCALEDJOB_TF = INFRA / "scaledjob.tf"
VARIABLES_TF = INFRA / "variables.tf"
TFVARS = INFRA / "terraform.tfvars"

# Must match adp_trigger.client (CONTROL_ENDPOINT_ENV / the enable flag) and
# lib.run_identity (the workload token file). A mismatch between either side and
# Terraform is not a fallback: it is a silent return to the legacy route.
ENABLE_ENV = "ADP_AGENT_AUTHORITY_ENABLED"
CONTROL_ENDPOINT_ENV = "ADP_AGENT_CONTROL_ENDPOINT"
WORKLOAD_TOKEN_FILE_ENV = "ADP_WORKLOAD_TOKEN_FILE"
BOOTSTRAP_AUDIENCE = "adp-agent-bootstrap"


def _read(path: Path) -> str:
    assert path.is_file(), f"terraform source not found: {path}"
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop ``#`` comment lines.

    Essential here: these files document the rejected settings *by name* and at
    length, so a naive substring search would match a prose warning against a
    setting rather than the live setting itself.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _block(text: str, marker: str) -> str:
    """Extract one bracket-balanced block or list beginning at ``marker``.

    Balance-counted rather than regex-delimited: these blocks nest several levels
    deep, so a lazy regex would stop at the first inner terminator and return a
    fragment that makes assertions pass for the wrong reason.

    Both ``{}`` and ``[]`` are tracked, because the ``local`` values here are
    ``join("\\n", [...])`` lists rather than brace blocks, and their elements
    contain ``${...}`` interpolations. Counting braces alone would close the
    block on the first interpolation and silently truncate it — which is exactly
    the kind of false pass this helper exists to avoid.
    """
    start = text.find(marker)
    assert start != -1, f"{marker} not found"
    opening = min(
        (index for index in (text.find("{", start), text.find("[", start)) if index != -1),
        default=-1,
    )
    assert opening != -1, f"no block or list opens after {marker}"
    depth = 0
    for index in range(opening, len(text)):
        if text[index] in "{[":
            depth += 1
        elif text[index] in "}]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced brackets in {marker}")


@pytest.fixture(scope="module")
def bootstrap() -> str:
    return _strip_comments(_read(BOOTSTRAP_TF))


@pytest.fixture(scope="module")
def scaledjob() -> str:
    return _strip_comments(_read(SCALEDJOB_TF))


@pytest.fixture(scope="module")
def variables() -> str:
    return _read(VARIABLES_TF)


@pytest.fixture(scope="module")
def tfvars() -> str:
    return _strip_comments(_read(TFVARS))


class TestProtectedRouteIsWiredAsOneUnit:
    """The flag, the endpoint and the token must be enabled by a single switch.

    Each assertion is about a *pairing*. Any one of these present without the
    others yields a worker that looks configured and still dispatches over the
    legacy shared-IAM route.
    """

    def test_enable_flag_and_control_endpoint_ship_in_the_same_block(self, bootstrap: str):
        env = json.loads((INFRA / "protected-worker-pod.json").read_text())["container"]["env"]
        names = {entry["name"] for entry in env}
        assert {ENABLE_ENV, WORKLOAD_TOKEN_FILE_ENV} <= names
        assert "local.agent_authority_pod.container.env" in bootstrap
        assert CONTROL_ENDPOINT_ENV in bootstrap
        assert 'jsondecode(file("${path.module}/protected-worker-pod.json"))' in bootstrap

    def test_the_enable_flag_is_the_exact_string_true(self, bootstrap: str):
        env = json.loads((INFRA / "protected-worker-pod.json").read_text())["container"]["env"]
        assert [e for e in env if e["name"] == ENABLE_ENV] == [{"name": ENABLE_ENV, "value": "true"}]

    def test_the_whole_env_block_is_gated_on_one_variable(self, bootstrap: str):
        for name in ("env", "mount", "volume"):
            line = next(line for line in bootstrap.splitlines() if f"agent_authority_{name}_block" in line)
            assert "var.agent_authority_enabled ?" in line

    def test_control_endpoint_points_at_the_internal_agent_prefix(self, bootstrap: str):
        assert '/internal/v1/agent' in bootstrap

    def test_the_pod_mounts_a_dedicated_audience_bootstrap_token(self, bootstrap: str):
        pod = json.loads((INFRA / "protected-worker-pod.json").read_text())
        volume = next(v for v in pod["volumes"] if v["name"] == "adp-workload-identity")
        token = volume["projected"]["sources"][0]["serviceAccountToken"]
        assert token == {"audience": BOOTSTRAP_AUDIENCE, "expirationSeconds": 3600, "path": "token"}
        mount = next(m for m in pod["container"]["volumeMounts"] if m["name"] == volume["name"])
        assert mount["readOnly"] is True
        assert "local.agent_authority_pod.container.volumeMounts" in bootstrap
        assert "local.agent_authority_pod.volumes" in bootstrap

    def test_the_scaledjob_interpolates_every_authority_block(self, scaledjob: str):
        """A block defined but never interpolated configures nothing.

        This is the failure that would make every other test here pass while the
        pod still received no authority configuration at all.
        """
        for block in (
            "agent_authority_env_block",
            "agent_authority_mount_block",
            "agent_authority_volume_block",
        ):
            assert f"${{local.{block}}}" in scaledjob, (
                f"{block} is defined but not interpolated into the ScaledJob manifest"
            )


class TestAuthorityCannotOutrunItsPrerequisites:
    """Fail-closed guards. These are the reason the flag is not simply flipped."""

    def test_signing_material_requires_approved_worker_digests(self, bootstrap: str):
        """TokenReview bootstrap admits pods against the digest allowlist.

        Creating signing material for a fleet that cannot be verified would let
        authority be "enabled" while admitting unapproved images.
        """
        secret = _block(bootstrap, 'resource "kubernetes_secret" "agent_authority"')
        assert "precondition" in secret, (
            "enabling authority must be preconditioned, not best-effort"
        )
        assert "length(var.agent_authority_worker_image_digests) > 0" in secret, (
            "authority must refuse to enable with an empty worker image digest allowlist"
        )

    def test_worker_digests_must_be_immutable_sha256_not_tags(self, variables: str):
        """A mutable tag would let an unreviewed image inherit authority."""
        block = _block(variables, 'variable "agent_authority_worker_image_digests"')
        assert "validation" in block, "the digest allowlist must be validated"
        assert "sha256:" in block, "worker images must be pinned by sha256 digest, never by tag"

    def test_authority_defaults_to_disabled(self, variables: str):
        """Default-off keeps a fresh or partial apply on the fail-closed side."""
        block = _strip_comments(_block(variables, 'variable "agent_authority_enabled"'))
        assert "default     = false" in block or "default = false" in block, (
            "agent_authority_enabled must default to false"
        )

    def test_committed_tfvars_does_not_auto_enable_authority_on_merge(self, tfvars: str):
        """This module auto-applies on push under ``infra/**``.

        ``webhook-ingress-deploy.yml`` triggers on merge to main, so a committed
        ``true`` here applies *before* the operator step that supplies the
        approved worker image digests it depends on — inverting the required
        order. This file already documents that hazard twice, for
        ``gh_token_broker_enabled`` (a flag flip that took down all agent
        dispatch on dev) and for the EventBridge security-agent rule. Enabling
        authority is a deliberate follow-up apply with digests supplied, not a
        merge side effect.

        Deleting this test to "just enable it" is the regression. Enable it by
        running the deploy with the digests supplied.
        """
        assert "agent_authority_enabled = true" not in tfvars.replace("  ", " "), (
            "agent_authority_enabled must not be committed as true: this module auto-applies on merge, "
            "ahead of the operator step supplying agent_authority_worker_image_digests"
        )


@pytest.mark.parametrize("enabled", [True, False])
def test_terraform_renders_protected_fields_only_when_enabled(tmp_path, enabled):
    """Evaluate the actual production locals, including YAML quoting/indentation."""
    import shutil
    import subprocess

    terraform = shutil.which("terraform")
    if not terraform:
        pytest.skip("Terraform is needed to evaluate protected pod composition")
    source = BOOTSTRAP_TF.read_text()
    start = source.index("  agent_authority_pod =")
    end = source.index("\n}", start)
    block = source[start:end].replace(
        "data.aws_ssm_parameter.gateway_apigw_invoke_url.value", "local.test_gateway_url")
    (tmp_path / "main.tf").write_text(
        'variable "agent_authority_enabled" { type = bool }\n'
        'variable "task_api_worker_enabled" { default = false }\nlocals {\n'
        ' test_gateway_url = "https://fixture.example/dev"\n'
        ' agent_control_verification_keys = { test = "public-key" }\n' + block + '\n}\n')
    shutil.copy(INFRA / "protected-worker-pod.json", tmp_path)
    expression = ('jsonencode({env = yamldecode(local.agent_authority_env_block), '
                  'mounts = try(yamldecode(local.agent_authority_mount_block), null), '
                  'volumes = try(yamldecode(local.agent_authority_volume_block), null)})\n')
    result = subprocess.run([terraform, "console", f"-var=agent_authority_enabled={str(enabled).lower()}"],
                            input=expression, text=True, capture_output=True, cwd=tmp_path, timeout=30)
    assert result.returncode == 0, result.stderr
    actual = json.loads(json.loads(result.stdout))
    env = {entry["name"]: entry["value"] for entry in actual["env"]}
    assert env[CONTROL_ENDPOINT_ENV] == "https://fixture.example/dev/internal/v1/agent"
    if enabled:
        canonical = json.loads((INFRA / "protected-worker-pod.json").read_text())
        assert env[ENABLE_ENV] == "true"
        assert env[WORKLOAD_TOKEN_FILE_ENV] == "/var/run/adp-workload/token"
        assert actual["mounts"]["volumeMounts"] == canonical["container"]["volumeMounts"]
        assert actual["volumes"]["volumes"] == canonical["volumes"]
    else:
        assert set(env) == {CONTROL_ENDPOINT_ENV}
        assert actual["mounts"] is None
        assert actual["volumes"] is None
