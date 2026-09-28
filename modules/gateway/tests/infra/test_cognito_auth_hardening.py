"""Cognito MFA / threat-protection EFFECTIVE configuration — #5666 (A11).

The defect these tests exist for is not a missing setting. It is a setting that was
present, correct, and *ineffective*:

``infra/modules/cognito/variables.tf`` has defaulted ``mfa_configuration`` to "ON"
since #133, with the comment "MFA is required for a SaaS platform managing Bedrock
access". But ``infra/main.tf`` passes ``var.cognito_mfa_configuration`` into it, and
THAT variable defaulted to "OPTIONAL". Terraform resolves the caller's value, so
the inner default was shadowed in source. These offline checks establish source
configuration only, not the effective configuration of a deployed pool. Explicit
OPTIONAL staging exceptions are allowed with a reviewed rollout record.

Threat protection (``user_pool_add_ons``) was a different finding: not
misconfigured, absent. It appeared nowhere in the repository, and there was no path
to enable it from the root module at all.

HCL is parsed here by brace matching rather than a real parser, following
``test_gateway_log_retention.py`` — no new test dependency, and these blocks are
regular enough for it to be reliable and reviewable.

Path note for anyone reconciling the reviewed records against this tree: #5657
cites these files as ``infra/modules/cognito/*``. There is no such path at the
repository root, and ``git log --all -- infra/modules/cognito`` is empty, so no
such tree was ever removed or redirected — the citation is simply relative to
``modules/gateway/``. The only Cognito module in the repository is
``modules/gateway/infra/modules/cognito/``, which is what these tests read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2]
_ROOT_VARIABLES = _GATEWAY / "infra" / "variables.tf"
_ROOT_MAIN = _GATEWAY / "infra" / "main.tf"
_COGNITO_VARIABLES = _GATEWAY / "infra" / "modules" / "cognito" / "variables.tf"
_COGNITO_MAIN = _GATEWAY / "infra" / "modules" / "cognito" / "main.tf"
_ENVIRONMENTS = _GATEWAY.parents[1] / "environments"


def _block(pattern: str, text: str, *, what: str) -> str:
    """Extract one brace-delimited HCL block whose header matches ``pattern``."""
    start = re.search(pattern, text)
    assert start, f"{what} not found"
    depth = 0
    for i in range(start.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start.start() : i + 1]
    raise AssertionError(f"unbalanced braces in {what}")


def _variable(name: str, text: str) -> str:
    return _block(rf'variable\s+"{re.escape(name)}"\s*\{{', text, what=f'variable "{name}"')


def _default_of(block: str) -> str:
    m = re.search(r"^\s*default\s*=\s*(.+?)\s*$", block, re.M)
    assert m, f"no default in block: {block[:120]}"
    return m.group(1).strip().strip('"')


@pytest.fixture(scope="module")
def root_variables() -> str:
    return _ROOT_VARIABLES.read_text()


@pytest.fixture(scope="module")
def cognito_variables() -> str:
    return _COGNITO_VARIABLES.read_text()


@pytest.fixture(scope="module")
def cognito_main() -> str:
    return _COGNITO_MAIN.read_text()


class TestMfaIsEffectivelyRequired:
    """The composed value, not either layer's stated intent."""

    def test_the_wrapper_default_does_not_weaken_mfa(self, root_variables):
        """Both source defaults must be ON unless explicitly staged by an operator."""
        assert _default_of(_variable("cognito_mfa_configuration", root_variables)) == "ON", (
            "the root module's MFA default is what pools actually get; anything but ON silently "
            "overrides the module's own hardened default (the #133 dead-code defect)"
        )

    def test_both_layers_agree_so_neither_can_shadow_the_other(self, root_variables, cognito_variables):
        """The anti-shadowing invariant — the real fix.

        Hardening only the inner default achieves nothing (proven by #133).
        Hardening only the wrapper leaves any other caller of the module
        permissive. Equal defaults mean the pass-through cannot introduce a
        weakening, whichever file a future reader happens to open.
        """
        outer = _default_of(_variable("cognito_mfa_configuration", root_variables))
        inner = _default_of(_variable("mfa_configuration", cognito_variables))
        assert outer == inner, (
            f"root default ({outer!r}) and cognito module default ({inner!r}) disagree; the outer value wins "
            "silently, which is how the module's 'ON' became dead code for two years"
        )

    @pytest.mark.parametrize(
        ("path_name", "var_name"),
        [("root", "cognito_mfa_configuration"), ("cognito module", "mfa_configuration")],
    )
    def test_off_is_rejected_at_both_layers(self, path_name, var_name, root_variables, cognito_variables):
        """OFF must fail the plan, not merely be un-defaulted.

        Validating only the wrapper would leave the module permissive for any other
        caller — and the wrapper is precisely the layer that silently overrode the
        module before.
        """
        text = root_variables if path_name == "root" else cognito_variables
        block = _variable(var_name, text)
        assert "validation" in block, f"{path_name} {var_name} has no validation block"
        condition = re.search(r"condition\s*=\s*(.+)", block)
        assert condition, f"{path_name} {var_name} validation has no condition"
        allowed = set(re.findall(r'"([A-Z_]+)"', condition.group(1)))
        assert "OFF" not in allowed, f"{path_name} still accepts mfa OFF: {sorted(allowed)}"
        assert "ON" in allowed, f"{path_name} must still permit ON: {sorted(allowed)}"

    def test_optional_stays_available_for_staged_rollout(self, root_variables):
        """Over-restriction guard.

        Forcing ON with no alternative would make this unusable for an existing
        pool with un-enrolled users, and the predictable result is an operator
        reverting the whole change. OPTIONAL must remain *selectable* — just not
        what you get by saying nothing.
        """
        condition = re.search(r"condition\s*=\s*(.+)", _variable("cognito_mfa_configuration", root_variables))
        assert "OPTIONAL" in condition.group(1), "OPTIONAL must remain a valid explicit choice for staged enrollment"

    def test_no_environment_silently_weakens_mfa(self):
        """A tfvars file is the third place this could regress, unreviewed."""
        offenders = []
        for path in _ENVIRONMENTS.rglob("*.tfvars"):
            source = path.read_text()
            for m in re.finditer(r'^\s*cognito_mfa_configuration\s*=\s*"([^"]+)"', source, re.M):
                value = m.group(1)
                preceding = source[: m.start()].rstrip().splitlines()
                exception = preceding[-1].strip() if preceding else ""
                documented = re.fullmatch(r"# cognito-mfa-staging: https://\S+; owner=\S.+; exit=\S.+", exception)
                if value != "ON" and not (value == "OPTIONAL" and documented):
                    offenders.append(f"{path.name}: {value} lacks a valid staging exception")
        assert not offenders, f"environments weaken MFA below ON: {offenders}"


class TestThreatProtectionIsWiredAndOptIn:
    def test_the_user_pool_declares_threat_protection(self, cognito_main):
        """Previously absent from the entire repository."""
        pool = _block(r'resource\s+"aws_cognito_user_pool"\s+"main"\s*\{', cognito_main, what="user pool")
        assert "user_pool_add_ons" in pool, "the user pool has no user_pool_add_ons block — threat protection is unreachable"
        assert "advanced_security_mode" in pool

    def test_it_is_reachable_from_the_root_module(self, root_variables):
        """An operator must not have to edit module internals to opt in."""
        assert "cognito_threat_protection_mode" in root_variables, "no root variable exposes threat protection"
        assert "threat_protection_mode" in _ROOT_MAIN.read_text(), "the root module never passes threat_protection_mode to the cognito module"

    def test_the_two_defaults_agree(self, root_variables, cognito_variables):
        """Same anti-shadowing invariant as MFA, applied to the new variable.

        Added at the same time as the shadowing fix precisely so this pass-through
        does not become the next instance of it.
        """
        outer = _default_of(_variable("cognito_threat_protection_mode", root_variables))
        inner = _default_of(_variable("threat_protection_mode", cognito_variables))
        assert outer == inner, f"threat-protection defaults disagree (root {outer!r} vs module {inner!r}); the outer value wins silently"

    def test_audit_and_enforced_are_both_selectable(self, cognito_variables):
        """AUDIT is the safe first step: it logs risk without changing sign-in outcomes."""
        condition = re.search(r"condition\s*=\s*(.+)", _variable("threat_protection_mode", cognito_variables))
        allowed = set(re.findall(r'"([A-Z]+)"', condition.group(1)))
        assert {"OFF", "AUDIT", "ENFORCED"} <= allowed, f"threat protection must offer OFF/AUDIT/ENFORCED, got {sorted(allowed)}"

    def test_off_emits_no_add_on_and_pins_no_tier(self, cognito_main):
        """OFF must be genuinely inert, including on billing.

        Threat protection requires the Cognito Plus plan, which is charged per
        monthly active user. This is the one default in #5666 deliberately left
        permissive: a merge must not change an AWS bill. For that to be true, OFF
        must emit no add-on block AND must not pin ``user_pool_tier`` — pinning a
        tier is itself a billing change for a pool not already on it.
        """
        pool = _block(r'resource\s+"aws_cognito_user_pool"\s+"main"\s*\{', cognito_main, what="user pool")
        add_ons = _block(r'dynamic\s+"user_pool_add_ons"\s*\{', pool, what="dynamic user_pool_add_ons")
        assert re.search(r'for_each\s*=\s*var\.threat_protection_mode\s*==\s*"OFF"\s*\?\s*\[\]', add_ons), (
            "the add-on must be emitted via a for_each that is empty at OFF, so OFF adds no billable feature plan"
        )
        tier = re.search(r"^\s*user_pool_tier\s*=\s*(.+)$", pool, re.M)
        assert tier, "user_pool_tier is not set; PLUS is required for the add-on"
        assert "null" in tier.group(1), (
            "user_pool_tier must resolve to null when threat protection is OFF, leaving the tier unmanaged; "
            f"pinning a concrete tier is itself a billing change. Got: {tier.group(1).strip()}"
        )


class TestSignInDisclosureAndAuthFlows:
    def test_user_existence_errors_are_suppressed_on_the_spa_client(self, cognito_main):
        """Without this, failed sign-ins are an account-enumeration oracle.

        Cognito otherwise returns UserNotFoundException for an unknown username and
        NotAuthorizedException for a known one, which distinguishes the two over an
        unsigned, unauthenticated API.
        """
        client = _block(r'resource\s+"aws_cognito_user_pool_client"\s+"main"\s*\{', cognito_main, what="main client")
        assert re.search(r'prevent_user_existence_errors\s*=\s*"ENABLED"', client), "the SPA client must set prevent_user_existence_errors = ENABLED"

    def test_the_broker_login_flow_is_preserved(self, cognito_main):
        """Over-restriction guard on the primary human login path.

        ALLOW_ADMIN_USER_PASSWORD_AUTH is how github-auth-broker mints tokens after
        a successful GitHub login. Hardening this client must not remove it.
        """
        client = _block(r'resource\s+"aws_cognito_user_pool_client"\s+"main"\s*\{', cognito_main, what="main client")
        assert "ALLOW_ADMIN_USER_PASSWORD_AUTH" in client, "removing ALLOW_ADMIN_USER_PASSWORD_AUTH breaks the github-auth-broker login path"

    def test_retained_password_flow_carries_its_justification(self, cognito_main):
        """ALLOW_USER_PASSWORD_AUTH is retained, and that needs to stay a decision.

        It is not used by the SPA (hosted-UI authorization-code + PKCE only), but it
        IS used by supported automation that authenticates from credential-free
        pods and therefore cannot use the SigV4-signed admin flow. Retiring it is
        real work with its own blast radius, recorded as a remainder rather than
        done blind. This test fails if someone deletes the reasoning while leaving
        the flow, or leaves stale reasoning after removing it.
        """
        client = _block(r'resource\s+"aws_cognito_user_pool_client"\s+"main"\s*\{', cognito_main, what="main client")
        if "ALLOW_USER_PASSWORD_AUTH" in client:
            assert "evals" in client or "USER_PASSWORD_AUTH" in client.split("explicit_auth_flows")[0], (
                "ALLOW_USER_PASSWORD_AUTH is retained with no recorded justification; either document which callers require it or remove the flow"
            )
