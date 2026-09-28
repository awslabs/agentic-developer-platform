"""The shipped signup/admission defaults must fail closed — #5666 (A11).

Three separate layers decide whether an unapproved caller can sign up and spend,
and each has been permissive at some point in this repo's history:

1. ``infra/variables.tf`` — the allowlist mode the broker Lambda is deployed with.
2. ``environments/*/modules/gateway.tfvars`` — the per-environment override.
3. ``k8s/configmap.yaml`` / ``src/shared/config.py`` — the runtime approval gate.

Layer 1 was hardened by #3986 and layer 3 by this issue. What none of them had was
a test pinning the *default*, so a later edit could relax any of them with nothing
failing. Defaults are exactly the thing that needs a test: nobody reviews a value
that has always been there, and an environment created next year inherits it.

On the dev override (layer 2), this file deliberately ASSERTS THE DEVIATION rather
than the safe value, and requires it to stay documented. Dev is pinned to
``open`` + ``allow_open_signup`` because ``mode=org`` locks every user out while the
org check runs on the signing-in user's OAuth token — an outage that fired twice
(2026-08-24, 2026-08-26). Flipping it here would re-cause that, and PR #4141's
open-signup premise is under CHANGES_REQUESTED, so silently adopting either
position is wrong. What this file CAN enforce is that the deviation is confined to
the environment layer, acknowledged by the explicit flag the validation demands,
and carries its justification and exit condition in a comment — so it stays a
recorded, temporary exception instead of decaying into the platform default.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2]
_REPO = _GATEWAY.parents[1]
_VARIABLES_TF = _GATEWAY / "infra" / "variables.tf"
_CONFIGMAP = _GATEWAY / "k8s" / "configmap.yaml"
_DEV_TFVARS = _REPO / "environments" / "dev" / "modules" / "gateway.tfvars"


def _variable_block(name: str, text: str) -> str:
    """Extract one ``variable "name" { ... }`` block by brace matching.

    Same approach as ``test_gateway_log_retention.py`` — a real HCL parser is not a
    test dependency here, and the blocks are regular enough that brace counting is
    reliable and reviewable.
    """
    start = re.search(rf'variable\s+"{re.escape(name)}"\s*\{{', text)
    assert start, f'variable "{name}" not found in {_VARIABLES_TF}'
    depth = 0
    for i in range(start.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start.start() : i + 1]
    raise AssertionError(f'unbalanced braces in variable "{name}"')


def _default_of(block: str) -> str:
    m = re.search(r"^\s*default\s*=\s*(.+?)\s*$", block, re.M)
    assert m, f"no default found in block: {block[:120]}"
    return m.group(1).strip().strip('"')


@pytest.fixture(scope="module")
def variables_tf() -> str:
    return _VARIABLES_TF.read_text()


class TestTerraformDefaultsFailClosed:
    def test_allowlist_mode_defaults_to_a_granting_check_not_open(self, variables_tf):
        """#3986's default. Pinned so it cannot quietly become "open" again."""
        default = _default_of(_variable_block("github_auth_allowlist_mode", variables_tf))
        assert default != "open", "the shipped allowlist default must never be 'open' — that provisions a Cognito account for ANY GitHub user"
        assert default in ("org", "platform"), f"the default must be a mode that actually checks something, got {default!r}"

    def test_open_signup_acknowledgement_defaults_off(self, variables_tf):
        """``open`` mode is inert without this flag, so the flag is the real gate."""
        assert _default_of(_variable_block("github_auth_allow_open_signup", variables_tf)) == "false"

    def test_open_mode_requires_the_explicit_acknowledgement(self, variables_tf):
        """A validation, not just a default — so 'open' cannot be set by itself.

        This is what makes open signup a deliberate two-variable act rather than a
        one-line tfvars edit somebody skims past in review.
        """
        block = _variable_block("github_auth_allowlist_mode", variables_tf)
        assert "github_auth_allow_open_signup" in block, (
            "the allowlist_mode variable must keep the cross-variable validation requiring github_auth_allow_open_signup when mode is 'open'"
        )

    def test_org_mode_requires_a_non_empty_org_list(self, variables_tf):
        """An empty org list denies everyone; catching it at plan time beats an outage."""
        block = _variable_block("github_auth_allowlist_mode", variables_tf)
        assert "github_auth_allowed_orgs" in block and "trimspace" in block


class TestRuntimeApprovalGateDefaults:
    """Layer 3 — the server-side gate this issue turns on. See also
    ``tests/auth/test_paid_route_admission_effective.py``, which asserts the
    behaviour; these assert the shipped values."""

    def test_configmap_enforces_org_assignment(self):
        text = _CONFIGMAP.read_text()
        assert 'BG_ENFORCE_ORG_ASSIGNMENT: "true"' in text, (
            "the deployed ConfigMap must enable approval enforcement; a 'false' here means the gate is "
            "off in the cluster regardless of the Python default"
        )

    def test_configmap_ships_the_break_glass_disabled(self):
        assert 'BG_APPROVAL_FAIL_OPEN: "false"' in _CONFIGMAP.read_text()

    def test_python_default_matches_the_configmap(self):
        """Drift between these two is how a fix becomes cosmetic."""
        import os

        from src.shared.config import Settings

        for var in ("BG_ENFORCE_ORG_ASSIGNMENT", "BG_APPROVAL_FAIL_OPEN"):
            os.environ.pop(var, None)
        settings = Settings()
        assert settings.enforce_org_assignment is True
        assert settings.approval_fail_open is False


class TestDevOverrideIsAnAcknowledgedExceptionNotADefault:
    """The dev deviation must stay confined, acknowledged and documented.

    Not asserting mode == "org" here on purpose: doing so would re-cause the
    lockout recorded in that file (twice), and PR #4141's open-signup premise is
    under CHANGES_REQUESTED. The deviation is a live operational fact; what is
    testable is that it remains a *recorded exception*.
    """

    @pytest.fixture(scope="class")
    def dev_tfvars(self) -> str:
        return _DEV_TFVARS.read_text()

    def test_open_mode_in_dev_carries_the_explicit_acknowledgement(self, dev_tfvars):
        """If dev is 'open', the acknowledging flag must be set in the same file.

        Written as an implication so this test stays correct — and still
        meaningful — on the day dev is moved back to 'org'.
        """
        mode = re.search(r"^github_auth_allowlist_mode\s*=\s*\"([^\"]+)\"", dev_tfvars, re.M)
        assert mode, "github_auth_allowlist_mode is not set explicitly in dev tfvars"
        if mode.group(1) == "open":
            assert re.search(r"^github_auth_allow_open_signup\s*=\s*true", dev_tfvars, re.M), (
                "dev pins allowlist_mode = 'open' without the acknowledgement flag; Terraform validation "
                "would reject this, so the pin must be explicit about disabling enforcement"
            )

    def test_the_open_pin_documents_its_reason_and_exit_condition(self, dev_tfvars):
        """A temporary exception with no stated exit becomes permanent.

        The comment must say why it is pinned and what removes it, so the next
        person can tell a deliberate hold from an abandoned one.
        """
        mode = re.search(r"^github_auth_allowlist_mode\s*=\s*\"([^\"]+)\"", dev_tfvars, re.M)
        if mode and mode.group(1) == "open":
            head = dev_tfvars[: mode.start()]
            assert "TEMPORARILY" in head or "temporarily" in head, "the 'open' pin must be marked as temporary"
            assert re.search(r"#\d{3,}", head), "the 'open' pin must reference the issue that removes it"

    def test_no_environment_disables_the_runtime_approval_gate(self):
        """Layer 2 must not undo layer 3.

        The allowlist governs who may *sign up*; the approval gate governs who may
        *spend*. Dev's open signup is survivable precisely because the approval gate
        still requires a platform admin to approve. An environment that turned both
        off would have no admission control at all on the paid paths.
        """
        env_root = _REPO / "environments"
        offenders = []
        for path in env_root.rglob("*.tfvars"):
            text = path.read_text()
            if re.search(r"enforce_org_assignment\s*=\s*false", text) or re.search(r'BG_ENFORCE_ORG_ASSIGNMENT\s*=\s*"?false', text):
                offenders.append(str(path.relative_to(_REPO)))
        assert not offenders, f"these environments disable the runtime approval gate, leaving the paid paths ungated: {offenders}"
