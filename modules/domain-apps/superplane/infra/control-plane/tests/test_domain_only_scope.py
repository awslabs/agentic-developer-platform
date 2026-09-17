"""`--superplane-only` runs the platform too, and that is a known limitation — Issue #5042 (U3).

## What the issue asked for

The confirmed platform-isolation requirement (2026-09-16) records this as a known limitation:

    `deploy-all.sh --superplane-only` means platform PLUS superplane, so U3 must provide
    dedicated domain workflows, and any command advertised as domain-only must be tested to
    execute only that scope.

Two obligations, and they pull in different directions. The dedicated workflows are the five
`superplane-*.yml` lanes this unit adds — `superplane-infra-apply.yml` applies
`modules/domain-apps/superplane/infra/control-plane/` and nothing else, which is the
domain-only path an operator should use for routine work. This suite discharges the second
obligation: it tests what `--superplane-only` actually does, so the limitation is pinned
rather than described.

## Why a test and not a note in a README

A note describes the behaviour at the time it was written. The risk here is not that someone
misreads the flag today; it is that the flag's scope silently widens — someone adds a step
without a scope guard, and a command advertised as domain-only starts redeploying the gateway.
The requirement's own words are that routine ops "must not redeploy gateway, frontend or Agent
Factory as a side effect", and a side effect is precisely the thing nobody notices.

So this suite calls the real scope resolver and asserts what it resolves to.

## What is verified, and what is honestly NOT

Verified: `resolve_deploy_scope()` in `platform/scripts/upgrade-scope.sh` sets
DEPLOY_GATEWAY, DEPLOY_WEBHOOK, DEPLOY_FACTORY and DEPLOY_AGENT_CONTEXT all false under
SUPERPLANE_ONLY, and every step in `deploy-all.sh` that deploys one of those modules is
guarded by the corresponding variable.

NOT verified, and not verifiable here: that a real `--superplane-only` run touches nothing
else in an AWS account. That is a live-account claim, and it belongs to the deferred R3
acceptance (a backend-contacting plan and a second apply producing an empty plan), which is
gated on a named account, spend authorization and a named cleanup owner — all unresolved.
A test that claimed it from a shell-variable check would be overstating its evidence.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
UPGRADE_SCOPE = REPO_ROOT / "platform" / "scripts" / "upgrade-scope.sh"
DEPLOY_ALL = REPO_ROOT / "platform" / "scripts" / "deploy-all.sh"

# The scope variables that gate the other modules' deploy steps.
OTHER_MODULE_FLAGS = (
    "DEPLOY_GATEWAY",
    "DEPLOY_WEBHOOK",
    "DEPLOY_FACTORY",
    "DEPLOY_AGENT_CONTEXT",
)


def _resolve_scope(flag: str) -> dict[str, str]:
    """Run the real resolver with one scope flag set and return the resulting variables.

    Calls the actual shell function rather than reimplementing its logic, so the assertions
    below cannot pass against a reimplementation that has drifted from the script.
    """
    assert UPGRADE_SCOPE.is_file(), f"{UPGRADE_SCOPE} must exist."

    reads = "\n".join(f'printf "%s=%s\\n" {v} "${v}"' for v in OTHER_MODULE_FLAGS)
    script = f"""
set -euo pipefail
fail() {{ echo "fail: $*" >&2; exit 1; }}
source "{UPGRADE_SCOPE}"
{flag}=true
resolve_deploy_scope
{reads}
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=30,
        # Explicit: the returncode is asserted below so a resolver failure reports the
        # script's own stderr, which is more useful than a CalledProcessError traceback.
        check=False,
    )
    assert result.returncode == 0, (
        f"resolve_deploy_scope failed with {flag}=true:\n{result.stderr}"
    )
    return dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )


def test_superplane_only_deploys_no_other_module() -> None:
    """The core assertion: --superplane-only excludes gateway, webhook, factory, context.

    This is what makes the flag's limitation precisely "platform plus superplane" rather than
    something broader. If a future edit made one of these true, the flag would silently start
    redeploying a module the requirement says routine domain ops must not touch.
    """
    scope = _resolve_scope("SUPERPLANE_ONLY")

    enabled = sorted(name for name, value in scope.items() if value != "false")
    assert not enabled, (
        f"--superplane-only resolved {enabled} to something other than false. A command "
        "advertised as domain-only must not deploy another module: the platform-isolation "
        "requirement (2026-09-16) states routine ops must not redeploy gateway, frontend or "
        "Agent Factory as a side effect."
    )


def test_the_resolver_is_not_vacuously_returning_false() -> None:
    """Premise check: the resolver must be capable of returning true.

    Without this, a resolver that had been broken into always printing `false` — or a helper
    whose variable names had drifted, so every lookup missed — would make the test above pass
    while proving nothing. The complement is the evidence that false means false.
    """
    scope = _resolve_scope("GATEWAY_ONLY")
    assert scope.get("DEPLOY_GATEWAY") == "true", (
        "GATEWAY_ONLY must resolve DEPLOY_GATEWAY=true. If it does not, this suite's "
        "harness is broken rather than the scope logic being especially safe."
    )


# Each module-deploying step in deploy-all.sh, and the scope flag that must gate it.
#
# Keyed by step banner rather than by flag: the first version of this test merely checked that
# each flag appeared in SOME conditional, and mutation testing showed that to be too weak —
# DEPLOY_AGENT_CONTEXT is read in three places, so deleting one guard left the test green.
# Pinning the step to its guard is the property that actually matters, because a step is what
# deploys a module.
GUARDED_STEPS = (
    ("Step 3/12", "DEPLOY_GATEWAY"),
    ("Step 4/12", "DEPLOY_GATEWAY"),
    ("Step 9/12", "DEPLOY_WEBHOOK"),
    ("Step 10/12", "DEPLOY_FACTORY"),
    ("Step 11/12", "DEPLOY_AGENT_CONTEXT"),
)


@pytest.mark.parametrize(
    ("step", "flag"), GUARDED_STEPS, ids=[s for s, _ in GUARDED_STEPS]
)
def test_each_other_module_step_is_scope_guarded(step: str, flag: str) -> None:
    """The step that deploys another module must be gated by that module's scope flag.

    `resolve_deploy_scope` setting DEPLOY_GATEWAY=false accomplishes nothing if the gateway
    step does not read it. This is the link between "the scope resolved correctly" and "the
    step was actually skipped".

    The guard may sit just after the step banner (steps 3 and 4) or wrap it (steps 9-11), so
    a window on both sides is searched rather than assuming one style.
    """
    assert DEPLOY_ALL.is_file(), f"{DEPLOY_ALL} must exist."
    body = DEPLOY_ALL.read_text()

    match = re.search(rf'step "{re.escape(step)}:[^"]*"', body)
    assert match, (
        f'no `step "{step}: ..."` banner found in deploy-all.sh. If the steps were '
        "renumbered, update GUARDED_STEPS — a silently missing step means this test "
        "stops checking anything."
    )

    window = body[max(0, match.start() - 600) : match.end() + 600]
    assert re.search(rf'\[\s*"\${flag}"\s*(?:=|!=)\s*(?:true|false)\s*\]', window), (
        f"{step} deploys another module but no `${flag}` guard appears within 600 "
        f"characters of its banner. Under --superplane-only, {flag} resolves to false; if "
        "the step does not read it, the flag is computed and ignored and a domain-only "
        "command redeploys that module as a side effect."
    )


def test_the_platform_phases_are_deliberately_not_skippable() -> None:
    """Document the limitation by asserting it, rather than asserting it away.

    Steps 1 (bootstrap) and 2 (platform infra) have no scope guard: `--superplane-only`
    runs them. That is the known limitation, and this test exists so it stays a *known* one
    — if someone later makes the platform phases skippable, this test fails and points at
    the docs that must be updated with it.

    The right response to needing a genuinely domain-only apply is not to weaken the platform
    phases; it is `superplane-infra-apply.yml`, which applies only this module.
    """
    body = DEPLOY_ALL.read_text()

    match = re.search(r'step "Step 2/12:[^"]*"', body)
    assert match, (
        "deploy-all.sh must still have a Step 2/12 for this test to be meaningful."
    )

    # The 40 lines after the step banner, where a scope guard would have to appear.
    following = body[match.end() : match.end() + 2000]
    guarded = re.search(r'\[\s*"\$(SUPERPLANE_ONLY|DEPLOY_PLATFORM)"', following)

    assert not guarded, (
        "Step 2/12 (platform infra) now appears to be scope-guarded. That is a behaviour "
        "change to a documented limitation: `--superplane-only` has always meant platform "
        "PLUS superplane. If this is intentional, update "
        "modules/domain-apps/superplane/infra/README.md and the note in "
        "environments/dev/modules/superplane.tfvars in the same change."
    )
