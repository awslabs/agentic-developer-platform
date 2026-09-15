"""The `superplane` feature flag is fail-closed (#5037, EPIC #4910).

The story's first acceptance criterion (R1 acc. 1) is that while this gate is off, no
existing ADP surface changes behaviour and no Superplane route is reachable on any
tenant. That criterion is only met if *off* is what absent, empty and malformed
configuration all resolve to — not merely what an explicit `"false"` produces.

Two failure modes this suite exists to catch:

**A fail-open default arms every environment on the next deploy.** `_is_enabled` (the
non-strict helper) returns True unless the var says `"false"`. Had this flag used it, the
route would be live everywhere the moment the gateway image shipped, and *unsetting* the
var would not turn it back off — which defeats the documented rollback.

**The infrastructure behind this flag is not this unit's to deploy.** Terraform is U3's
and pinned images are U2's, so a `true` today would advertise a route whose backing
services do not exist. Off is correct until those units land, and that makes the default
a correctness property rather than a preference.

The frontend half of the same contract (the `ALL_FEATURES_ENABLED` default, which renders
both while `/features` is in flight and whenever it fails) is asserted as text in
`test_superplane_registration.py`, because no runtime Python test can see it.
"""

from __future__ import annotations

import pytest

from src.features import routes as features_routes

FLAG_KEY = "superplane"
FLAG_ENV_VAR = "FEATURE_SUPERPLANE_ENABLED"


@pytest.fixture(autouse=True)
def _no_flag_env(monkeypatch):
    """Start every test from an environment that says nothing about this flag.

    Autouse because a leaked env var — including one inherited from the agent runtime
    this suite may itself run inside — would make the defaults-off assertions pass for
    the wrong reason.
    """
    monkeypatch.delenv(FLAG_ENV_VAR, raising=False)


class TestFailsClosed:
    """Absent, empty and malformed configuration all resolve to off."""

    def test_absent_env_var_resolves_false(self):
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    def test_empty_string_resolves_false(self, monkeypatch):
        """An env var set but empty is a misconfiguration, not an opt-in."""
        monkeypatch.setenv(FLAG_ENV_VAR, "")
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize(
        "value",
        ["False", "false", "0", "no", "off", "1", "yes", "enabled", "on", "TRUE-ish", "true ", " true", "null", "None"],
    )
    def test_only_the_literal_true_enables_it(self, monkeypatch, value):
        """Truthy-looking strings must read as off, not as an opt-in.

        `"1"`, `"yes"` and `"on"` are included deliberately: they are what somebody
        reaching for a boolean env var writes by habit, and reading them as True would
        enable the gate for a value nobody deliberately used to mean "true" here. The
        whitespace variants matter for the same reason — a trailing space from a copied
        SSM value must not silently arm the flag.
        """
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE"])
    def test_explicit_true_enables_it(self, monkeypatch, value):
        """Case-insensitive literal `true` is the single enabling value."""
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is True


class TestFlagIsRegistered:
    """The flag is present in the payload and uses the strict reader."""

    def test_flag_uses_the_strict_reader(self):
        """Guards against a later edit swapping in the fail-open helper.

        Read as source text because both helpers return a plain bool: a runtime call
        cannot distinguish which one produced the answer when the var is absent, since
        both would be consulted with the same missing input. The distinguishing
        behaviour only appears for values like `"0"`, which the parametrized test above
        covers — this assertion catches the swap directly at its call site.
        """
        import inspect

        source = inspect.getsource(features_routes.get_features)
        assert f'"{FLAG_KEY}": _is_enabled_strict("{FLAG_ENV_VAR}")' in source, (
            f"{FLAG_KEY} must be read with _is_enabled_strict. The non-strict _is_enabled "
            "defaults to ENABLED when the env var is absent, which would make the "
            "Superplane route reachable in every environment on the next gateway deploy."
        )

    @pytest.mark.asyncio
    async def test_payload_contains_flag_and_defaults_off(self):
        """The endpoint reports the flag, and reports it as off by default."""
        payload = await features_routes.get_features(_current_user={"sub": "test-user"})
        assert FLAG_KEY in payload["features"], (
            f"{FLAG_KEY} missing from the /features payload — the frontend gate reads this key, "
            "so its absence makes the route's visibility depend on a lookup miss."
        )
        assert payload["features"][FLAG_KEY] is False

    @pytest.mark.asyncio
    async def test_enabling_superplane_changes_no_other_flag(self, monkeypatch):
        """R1 acc. 1: this gate must not alter any other surface's behaviour.

        The criterion is about the gate being *off*, but the stronger property is worth
        pinning: even turning it on must leave every other flag exactly as it was. A
        shared-helper refactor that accidentally coupled two flags would be invisible to
        a test that only ever reads this one.
        """
        before = (await features_routes.get_features(_current_user={"sub": "u"}))["features"]
        monkeypatch.setenv(FLAG_ENV_VAR, "true")
        after = (await features_routes.get_features(_current_user={"sub": "u"}))["features"]

        assert after[FLAG_KEY] is True
        others_before = {k: v for k, v in before.items() if k != FLAG_KEY}
        others_after = {k: v for k, v in after.items() if k != FLAG_KEY}
        assert others_before == others_after, "Toggling superplane changed another feature flag"
