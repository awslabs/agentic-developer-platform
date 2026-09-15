"""The Superplane UI route and nav entry are unavailable with the flag off (#5037).

R1 acc. 1 is that while the gate is off, no Superplane route is reachable on any tenant
and existing ADP flows are unaffected. This suite covers the parts of that claim that can
be established without a deployed environment, and is explicit about the part that cannot.

**Why these are text assertions.** The gating decision lives in the SPA bundle: `App.tsx`
wraps the route in `FeatureGate`, `Navigation.tsx` pushes the nav entry behind
`features.superplane`, and `features.ts` supplies the fail-closed default used while
`/features` is in flight or failing. No Python runtime test can observe any of those, and
the failure mode being guarded is a PR that lands the flag but forgets one of the three
wirings — which every runtime test on the backend alone would pass.

The browser-level check (navigate to /superplane in a flag-off environment, confirm the
dashboard renders instead) is inherently live and belongs with the deferred criteria; it
is not silently claimed here. What *is* established offline: the flag defaults off in
both the backend payload and the frontend fallback, the route is gate-wrapped rather than
bare, the nav entry is conditional, and no existing route or nav entry gained a gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.features import routes as features_routes

pytestmark = [pytest.mark.e2e]

FLAG_KEY = "superplane"
FLAG_ENV_VAR = "FEATURE_SUPERPLANE_ENABLED"
ROUTE_PATH = "/superplane"

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_APP_TSX = _GATEWAY_ROOT / "frontend" / "src" / "App.tsx"
_NAVIGATION_TSX = _GATEWAY_ROOT / "frontend" / "src" / "components" / "Navigation.tsx"
_FRONTEND_FEATURES = _GATEWAY_ROOT / "frontend" / "src" / "services" / "features.ts"


@pytest.fixture(autouse=True)
def _no_flag_env(monkeypatch):
    """No inherited value for the flag — otherwise defaults-off passes for the wrong reason."""
    monkeypatch.delenv(FLAG_ENV_VAR, raising=False)


class TestRouteIsGated:
    """The route exists only inside a FeatureGate for this flag."""

    def test_route_is_wrapped_in_feature_gate(self):
        """A bare `<Route path="/superplane">` would be reachable on every tenant."""
        app = _APP_TSX.read_text()
        route_lines = [ln for ln in app.splitlines() if f'path="{ROUTE_PATH}"' in ln]
        assert route_lines, f'No <Route path="{ROUTE_PATH}"> found in App.tsx'

        for line in route_lines:
            assert f'FeatureGate feature="{FLAG_KEY}"' in line, (
                f"The {ROUTE_PATH} route is declared without FeatureGate "
                f'feature="{FLAG_KEY}": {line.strip()!r}. '
                "Without the gate the route resolves in every environment, which is exactly "
                "what R1 acc. 1 forbids."
            )

    def test_feature_gate_redirects_when_flag_is_off(self):
        """The gate's off-behaviour is a redirect, not a render.

        Asserted because `FeatureGate` is shared: if it were ever changed to render its
        children while merely hiding navigation, every gated route in the app — including
        this one — would become reachable by typing the URL.
        """
        gate = (_GATEWAY_ROOT / "frontend" / "src" / "components" / "FeatureGate.tsx").read_text()
        assert "Navigate" in gate and "!features[feature]" in gate, (
            "FeatureGate no longer redirects on a disabled flag; /superplane would become "
            "reachable by direct URL entry."
        )


class TestNavEntryIsGated:
    """The nav entry is conditional on the flag."""

    def test_nav_push_is_inside_the_flag_conditional(self):
        nav = _NAVIGATION_TSX.read_text()
        assert f"to: '{ROUTE_PATH}'" in nav, f"No nav entry for {ROUTE_PATH} in Navigation.tsx"

        # The push must be governed by `features.superplane`. Checked by locating the
        # conditional and confirming the push falls inside its block, rather than merely
        # confirming both strings appear somewhere in a 200-line file.
        match = re.search(
            rf"if \(features\.{FLAG_KEY}\) \{{(.*?)\}}",
            nav,
            re.DOTALL,
        )
        assert match, f"No `if (features.{FLAG_KEY})` conditional found in Navigation.tsx"
        assert f"to: '{ROUTE_PATH}'" in match.group(1), (
            f"The {ROUTE_PATH} nav entry is not inside the `if (features.{FLAG_KEY})` block. "
            "An unconditional push advertises a menu item whose route redirects away."
        )


class TestFailsClosedOnBothSides:
    """Backend payload and frontend fallback both default to off."""

    @pytest.mark.asyncio
    async def test_backend_reports_flag_off_by_default(self):
        payload = await features_routes.get_features(_current_user={"sub": "test-user"})
        assert payload["features"][FLAG_KEY] is False

    def test_frontend_fallback_defaults_off(self):
        """`ALL_FEATURES_ENABLED` renders while /features is pending AND when it errors.

        A `true` here is the single edit that would make the route appear on every cold
        load and stay visible during a backend outage, so it is asserted as text.
        """
        features_ts = _FRONTEND_FEATURES.read_text()
        match = re.search(
            r"export const ALL_FEATURES_ENABLED: FeatureFlags = \{(.*?)\n\};",
            features_ts,
            re.DOTALL,
        )
        assert match, "Could not locate ALL_FEATURES_ENABLED in features.ts"
        assert re.search(rf"^\s*{FLAG_KEY}:\s*false,\s*$", match.group(1), re.MULTILINE), (
            f"{FLAG_KEY} is not `false` in ALL_FEATURES_ENABLED. That object is the value "
            "useFeatures returns while the /features fetch is in flight and whenever it "
            "fails, so a `true` would reveal the route on every cold load."
        )

    def test_flag_declared_in_frontend_interface(self):
        """Present in the TS interface, so a missing key is a compile error not a silent undefined."""
        features_ts = _FRONTEND_FEATURES.read_text()
        assert re.search(rf"^\s*{FLAG_KEY}:\s*boolean;", features_ts, re.MULTILINE), (
            f"{FLAG_KEY} missing from the FeatureFlags interface; `features.{FLAG_KEY}` "
            "would be `undefined` — falsy today, but unchecked by the type system."
        )


class TestExistingFlowsUnaffected:
    """R1 acc. 1's second half: existing ADP surfaces keep their previous behaviour."""

    def test_existing_routes_did_not_gain_a_superplane_gate(self):
        """Only the new route may reference this flag in App.tsx."""
        app = _APP_TSX.read_text()
        gated = [ln for ln in app.splitlines() if f'feature="{FLAG_KEY}"' in ln]
        assert len(gated) == 1, (
            f"Expected exactly one route gated on {FLAG_KEY}, found {len(gated)}. "
            "Gating an existing route on this flag would remove it from every environment "
            "where the flag is off — i.e. all of them."
        )
        assert f'path="{ROUTE_PATH}"' in gated[0]

    def test_login_and_setup_routes_remain_ungated_by_this_flag(self):
        """Named explicitly because the story lists login and local-assistant setup as impacted surfaces."""
        app = _APP_TSX.read_text()
        for path in ('path="/login"', 'path="/setup"'):
            line = next((ln for ln in app.splitlines() if path in ln), None)
            assert line is not None, f"Expected {path} to still exist in App.tsx"
            assert FLAG_KEY not in line, f"{path} must not be gated on {FLAG_KEY}: {line.strip()!r}"

    @pytest.mark.asyncio
    async def test_other_flags_keep_their_defaults(self, monkeypatch):
        """Adding this flag must not have altered any other flag's default."""
        payload = await features_routes.get_features(_current_user={"sub": "u"})
        features = payload["features"]
        # Core flags stay fail-open; the established add-ons stay fail-closed.
        for key in ("chat", "knowledge", "connections", "credentials", "logs"):
            assert features[key] is True, f"Core flag {key} is no longer enabled by default"
        for key in ("gitlab", "orchestration_engine", "budget_spend", "agent_control", "new_ui"):
            assert features[key] is False, f"Fail-closed flag {key} is no longer off by default"
