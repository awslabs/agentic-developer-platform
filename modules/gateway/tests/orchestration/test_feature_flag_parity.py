"""The `orchestration_engine` flag is fail-closed and lands in all three places (#4209).

Two properties, both adversarial, because both failure modes are silent:

**Fail-closed.** The flag must resolve *false* when nothing says otherwise and
*false* when the lookup itself breaks. The module has two helpers a dozen lines
apart — `_is_enabled` (fail-open) and `_is_enabled_strict` (fail-closed) — and
picking the wrong one is a one-word mistake that no behavioural test catches while
the env var happens to be absent, because both return the same value for an absent
var under normal conditions. They diverge exactly when it matters: on an
*explicitly* wrong value and on a lookup error. So the value tests are paired with
an AST assertion that pins *which helper* this key is wired to.

**Three-place parity.** The flag is only real if the backend dict, the frontend
`FeatureFlags` interface + `ALL_FEATURES_ENABLED` default, and the k8s env all
carry it. A PR that lands two of the three produces a backend that enables what the
UI cannot show (or vice versa), which is why the check is source-level: a runtime
test cannot see the frontend or the manifest.

Reading the frontend and the manifest as *text* is deliberate. The alternative —
trusting a convention — is what lets the third edit get dropped.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.features import routes as features_routes

# The one place the flag's identity is written down in this test. Everything below
# derives from these two, so a rename cannot leave a half-updated test asserting
# the old key still exists somewhere.
FLAG_KEY = "orchestration_engine"
FLAG_ENV_VAR = "FEATURE_ORCHESTRATION_ENGINE_ENABLED"

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_FEATURES = _GATEWAY_ROOT / "frontend" / "src" / "services" / "features.ts"
_K8S_DEPLOYMENT = _GATEWAY_ROOT / "k8s" / "deployment.yaml"


@pytest.fixture(autouse=True)
def _no_flag_env(monkeypatch):
    """Every test starts from an environment that says nothing about this flag.

    Autouse because a leaked env var from another test would make the
    defaults-off assertions pass for the wrong reason.
    """
    monkeypatch.delenv(FLAG_ENV_VAR, raising=False)


class TestFailsClosed:
    """The default must be off, and an error must not turn it on."""

    def test_absent_env_var_resolves_false(self):
        """No env var set anywhere → the engine path is invisible."""
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize("value", ["", "False", "0", "no", "off", "TRUE-ish", "1", "yes", "enabled"])
    def test_only_the_literal_true_enables_it(self, monkeypatch, value):
        """Anything that is not "true" (case-insensitive) leaves it off.

        `"1"`, `"yes"` and `"enabled"` are in this list on purpose: an operator who
        assumes truthy-string semantics must get *off*, not a silently enabled
        engine path.
        """
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "tRuE"])
    def test_explicit_true_enables_it(self, monkeypatch, value):
        """The opt-in still has to work, or the flag is just a wall."""
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is True

    def test_lookup_error_never_yields_an_enabled_flag(self, monkeypatch):
        """A broken lookup must never resolve to *on*.

        The requirement is "on lookup error, off". The honest statement of that
        here is: the error propagates and is never converted into `True`. A
        swallowed error that returned a default would be the dangerous shape, and
        for a fail-open helper that default would be `True` — which is the whole
        reason this key must not use one.

        The consumer-side half of this property (a failed *fetch* shows the engine
        as off, rather than flashing it on) is asserted in the frontend suite
        against `ALL_FEATURES_ENABLED`.
        """

        def _boom(*_args, **_kwargs):
            raise RuntimeError("environment unavailable")

        monkeypatch.setattr(features_routes.os.environ, "get", _boom)
        with pytest.raises(RuntimeError):
            features_routes._is_enabled_strict(FLAG_ENV_VAR)

    async def test_endpoint_reports_false_by_default(self):
        """End to end through the route function, with no env var set."""
        payload = await features_routes.get_features(_current_user=object())
        assert payload["features"][FLAG_KEY] is False

    async def test_endpoint_reports_true_when_explicitly_enabled(self, monkeypatch):
        monkeypatch.setenv(FLAG_ENV_VAR, "true")
        payload = await features_routes.get_features(_current_user=object())
        assert payload["features"][FLAG_KEY] is True


class TestWiredToTheStrictHelper:
    """Source-level: the *fail-closed* helper is the one this key calls.

    A value test cannot tell the two helpers apart while the env var is absent —
    both return False. This can.
    """

    @staticmethod
    def _flag_dict_call(key: str) -> ast.Call:
        """The `ast.Call` node that produces `key`'s value in the features dict."""
        tree = ast.parse(Path(inspect.getfile(features_routes)).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for dict_key, dict_value in zip(node.keys, node.values, strict=False):
                if isinstance(dict_key, ast.Constant) and dict_key.value == key:
                    assert isinstance(dict_value, ast.Call), f"{key!r} must be produced by a helper call, got {type(dict_value).__name__}"
                    return dict_value
        pytest.fail(f"no {key!r} entry found in any dict literal in features/routes.py")

    def test_flag_uses_is_enabled_strict(self):
        """`_is_enabled`, the fail-open helper, would be a defect here."""
        call = self._flag_dict_call(FLAG_KEY)
        func = call.func
        assert isinstance(func, ast.Name), f"expected a plain helper call for {FLAG_KEY!r}"
        assert func.id == "_is_enabled_strict", (
            f"{FLAG_KEY!r} is wired to {func.id!r}; it must use '_is_enabled_strict'. "
            "The engine is an opt-in add-on: a fail-open lookup would enable it on error."
        )

    def test_flag_reads_the_expected_env_var(self):
        """Pins the env var name the k8s manifest and operators rely on."""
        call = self._flag_dict_call(FLAG_KEY)
        assert call.args, f"{FLAG_KEY!r} helper call takes no argument"
        first = call.args[0]
        assert isinstance(first, ast.Constant) and first.value == FLAG_ENV_VAR, f"{FLAG_KEY!r} must read {FLAG_ENV_VAR!r}"

    def test_no_fallback_var_widens_the_flag(self):
        """A `fallback_var` would let an unrelated env var switch the engine on."""
        call = self._flag_dict_call(FLAG_KEY)
        assert len(call.args) == 1 and not call.keywords, (
            f"{FLAG_KEY!r} must take exactly one argument; a fallback would let another variable enable the engine"
        )


class TestThreePlaceParity:
    """Backend dict, frontend interface + default, k8s env — or CI fails."""

    def test_backend_dict_declares_the_flag(self):
        source = Path(inspect.getfile(features_routes)).read_text()
        assert f'"{FLAG_KEY}"' in source, f"{FLAG_KEY!r} missing from the backend features dict"

    def test_frontend_interface_declares_the_flag(self):
        source = _FRONTEND_FEATURES.read_text()
        interface_body = source.split("export interface FeatureFlags")[1].split("}")[0]
        assert f"{FLAG_KEY}:" in interface_body, (
            f"{FLAG_KEY!r} missing from the frontend FeatureFlags interface. The backend would enable a feature the UI has no key for."
        )

    def test_frontend_default_is_false(self):
        """The fail-open default object must make *this* flag an exception.

        `ALL_FEATURES_ENABLED` is what `useFeatures` returns while the fetch is in
        flight or after it fails. Defaulting this flag to `true` there would flash
        the engine UI to every user on every slow load.
        """
        source = _FRONTEND_FEATURES.read_text()
        default_body = source.split("ALL_FEATURES_ENABLED: FeatureFlags = {")[1].split("};")[0]
        assert f"{FLAG_KEY}: false" in default_body, f"{FLAG_KEY!r} must default to false in ALL_FEATURES_ENABLED (fail-closed)"

    def test_k8s_manifest_declares_the_env_var(self):
        manifest = _K8S_DEPLOYMENT.read_text()
        assert FLAG_ENV_VAR in manifest, f"{FLAG_ENV_VAR!r} missing from k8s/deployment.yaml — the operator has nothing to flip"

    def test_k8s_manifest_ships_the_flag_off(self):
        """The deployed value, not just the code default, must be off.

        Asserted on the *active* entry: a commented-out line would satisfy a bare
        substring check while shipping nothing.
        """
        lines = [line.strip() for line in _K8S_DEPLOYMENT.read_text().splitlines()]
        active = [line for line in lines if line.startswith("- name:") or line.startswith("value:")]
        assert f"- name: {FLAG_ENV_VAR}" in active, f"{FLAG_ENV_VAR!r} is present but commented out in k8s/deployment.yaml"
        index = active.index(f"- name: {FLAG_ENV_VAR}")
        assert active[index + 1] == 'value: "false"', f'{FLAG_ENV_VAR!r} must ship as "false"; got {active[index + 1]!r}'


class TestLegacyModeRemainsTheDefault:
    """Ruling D-R20: legacy is a supported product mode, not a migration state.

    AC-31 deviation, APPROVED BY THE ISSUE OWNER (#4209, 2026-08-29): the issue
    asked for a byte-for-byte baseline of the issue set an AIDLC flow emits. AIDLC
    emission is an LLM-driven SKILL.md prompt, not Python, so a byte-exact
    baseline would assert model output — green today, flaky tomorrow. The owner
    accepted the source-level checks in this class (legacy-is-default plus
    no-deprecation-signal) as the substitute. Do not re-add the byte baseline.
    """

    def test_no_mode_specified_resolves_to_legacy(self):
        """With nothing opted in, the engine is off — so the flow is legacy.

        "Legacy mode is the default" is exactly "the flag is off unless somebody
        opts in", asserted rather than assumed.
        """
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    def test_engine_flag_is_not_coupled_to_any_broad_enable_switch(self):
        """No `AGENT_CONTEXT_ENABLED`-style inheritance may switch the engine on.

        The knowledge/indexing flags inherit from a broader variable by design.
        This one must not: inheriting would mean enabling an unrelated subsystem
        silently enables the engine for every org in the deployment.
        """
        call = TestWiredToTheStrictHelper._flag_dict_call(FLAG_KEY)
        literals = [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
        assert literals == [FLAG_ENV_VAR], f"{FLAG_KEY!r} reads more than its own env var: {literals}"

    def test_no_deprecation_signal_in_the_features_module(self):
        """Nothing in the flag surface may mark the GitHub/legacy path as dying.

        The compatibility promise is not only behavioural — a deprecation warning
        or a "legacy" log line tells users the path is going away, which is the
        message ruling D-R20 forbids.
        """
        source = Path(inspect.getfile(features_routes)).read_text().lower()
        for signal in ("deprecationwarning", "deprecated", "will be removed", "sunset"):
            assert signal not in source, f"features/routes.py contains a deprecation signal ({signal!r})"
