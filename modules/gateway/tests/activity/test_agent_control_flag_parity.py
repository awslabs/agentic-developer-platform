"""The `agent_control` flag is fail-closed and lands in all three places (#3960).

The sibling suite `tests/orchestration/test_feature_flag_parity.py` established
this shape for `orchestration_engine` (#4209). This is the same contract applied
to a flag with a larger blast radius, so it is a parallel suite rather than a
parametrization of that one: the two flags share a mechanism but not their
reasons, and the reasons are what the assertion messages have to say.

Why `agent_control` needs it more than a UI flag does:

**It opens a channel, not a screen.** `orchestration_engine` gates whether a graph
renders. This gates an authenticated path from the gateway into a *running agent
pod*. The rollout invariant for this story is that ordinary workloads stay off,
and the single legitimate `true` is an operator-created isolated fixture
(evaluation #3967). That is an `aws ssm put-parameter` on one environment — which
only works if the value is rendered per environment rather than committed.

**A literal in the manifest is every environment's opt-in.** `k8s/deployment.yaml`
is applied verbatim to whichever environment `gateway-deploy.yml` targets. A
hard-coded `"true"` there would arm prod on its next deploy; a hard-coded `"false"`
is safe today but makes the fixture opt-in an edit to a committed file, which is
the same defect waiting for someone in a hurry. This suite asserts the *absence*
of a literal for that reason.

**Turning it on does not make a verb work.** All four verbs are unsupported in S1;
an authorized caller on a live run gets 501. The flag gates the route surface, the
ping/state read path and the capability report. This is worth stating because the
natural assumption on seeing the flag flip is that pause/abort became available.

Reading the frontend and the manifests as *text* is deliberate, as in the sibling
suite: a runtime test cannot see either, and the failure mode being guarded is a
PR that lands two of the three edits.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src.features import routes as features_routes

# The flag's identity, written down once. Everything else derives from these, so a
# rename cannot leave half of this suite asserting the old name.
FLAG_KEY = "agent_control"
FLAG_ENV_VAR = "FEATURE_AGENT_CONTROL_ENABLED"

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND_FEATURES = _GATEWAY_ROOT / "frontend" / "src" / "services" / "features.ts"
_K8S_DEPLOYMENT = _GATEWAY_ROOT / "k8s" / "deployment.yaml"
_K8S_CONFIGMAP = _GATEWAY_ROOT / "k8s" / "configmap.yaml"
_DEPLOY_WORKFLOW = _GATEWAY_ROOT.parents[1] / ".github" / "workflows" / "gateway-deploy.yml"

_FLAG_PLACEHOLDER = f"__{FLAG_ENV_VAR}__"
_FLAG_SSM_PARAM = "/adp/${ENVIRONMENT}/gateway/feature-agent-control"


@pytest.fixture(autouse=True)
def _no_flag_env(monkeypatch):
    """Start every test from an environment that says nothing about this flag.

    Autouse because a leaked env var — including one inherited from the agent
    runtime this suite may run inside — would make the defaults-off assertions
    pass for the wrong reason.
    """
    monkeypatch.delenv(FLAG_ENV_VAR, raising=False)


class TestFailsClosed:
    """Off by default, and an error must not turn it on."""

    def test_absent_env_var_resolves_false(self):
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize("value", ["", "False", "0", "no", "off", "1", "yes", "enabled", "TRUE-ish"])
    def test_only_the_literal_true_enables_it(self, monkeypatch, value):
        """Truthy-looking strings must read as off, not as an opt-in.

        `"1"`, `"yes"` and `"enabled"` are here deliberately: an operator who
        assumes shell-truthy semantics while setting up the #3967 fixture must get
        a closed control path, not a silently opened one.
        """
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "tRuE"])
    def test_explicit_true_enables_it_on_the_gateway_side(self, monkeypatch, value):
        """The opt-in has to work, or the flag is just a wall.

        Case-insensitive here, because the repo-wide `_is_enabled_strict`
        lowercases. The worker's own reader of this same variable is byte-exact, a
        real asymmetry documented in the control-listener suite. It is safe in this
        direction only: gateway-lenient means a mis-cased value yields "gateway
        routes, pod has no listener" → an honest 409. Gateway-strict would mean a
        pod listening on a port the gateway refuses to dial — a bound port with no
        capability, which is attack surface for nothing.
        """
        monkeypatch.setenv(FLAG_ENV_VAR, value)
        assert features_routes._is_enabled_strict(FLAG_ENV_VAR) is True

    def test_lookup_error_never_yields_an_enabled_flag(self, monkeypatch):
        """A broken lookup must never resolve to *on*.

        The honest statement of "on lookup error, off" is that the error
        propagates and is never converted into `True`. A swallowed error returning
        a default is the dangerous shape, and for a fail-*open* helper that default
        would be `True` — which is why this key must not use one.
        """

        def _boom(*_args, **_kwargs):
            raise RuntimeError("environment unavailable")

        monkeypatch.setattr(features_routes.os.environ, "get", _boom)
        with pytest.raises(RuntimeError):
            features_routes._is_enabled_strict(FLAG_ENV_VAR)

    async def test_endpoint_reports_false_by_default(self):
        payload = await features_routes.get_features(_current_user=object())
        assert payload["features"][FLAG_KEY] is False

    async def test_endpoint_reports_true_when_explicitly_enabled(self, monkeypatch):
        monkeypatch.setenv(FLAG_ENV_VAR, "true")
        payload = await features_routes.get_features(_current_user=object())
        assert payload["features"][FLAG_KEY] is True


class TestWiredToTheStrictHelper:
    """Source-level: this key calls the *fail-closed* helper.

    A value test cannot distinguish the two helpers while the env var is absent —
    both return False. They diverge on an explicitly wrong value and on a lookup
    error, which is exactly when it matters. This can tell them apart.
    """

    @staticmethod
    def _flag_dict_call(key: str) -> ast.Call:
        """The `ast.Call` node producing `key`'s value in the features dict."""
        tree = ast.parse(Path(inspect.getfile(features_routes)).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for dict_key, dict_value in zip(node.keys, node.values, strict=False):
                if isinstance(dict_key, ast.Constant) and dict_key.value == key:
                    assert isinstance(dict_value, ast.Call), (
                        f"{key!r} must be produced by a helper call, got {type(dict_value).__name__}. "
                        "A bare literal would make the flag unswitchable."
                    )
                    return dict_value
        pytest.fail(f"no {key!r} entry found in any dict literal in features/routes.py")

    def test_flag_uses_is_enabled_strict(self):
        """`_is_enabled`, the fail-open helper, would be a defect here."""
        call = self._flag_dict_call(FLAG_KEY)
        func = call.func
        assert isinstance(func, ast.Name), f"expected a plain helper call for {FLAG_KEY!r}"
        assert func.id == "_is_enabled_strict", (
            f"{FLAG_KEY!r} is wired to {func.id!r}; it must use '_is_enabled_strict'. "
            "A fail-open lookup would advertise the control path as available on error — "
            "and the UI would render abort buttons for a channel that cannot deliver."
        )

    def test_flag_reads_the_expected_env_var(self):
        """Pins the name the manifest, the worker and operators all rely on."""
        call = self._flag_dict_call(FLAG_KEY)
        assert call.args, f"{FLAG_KEY!r} helper call takes no argument"
        first = call.args[0]
        assert isinstance(first, ast.Constant) and first.value == FLAG_ENV_VAR, f"{FLAG_KEY!r} must read {FLAG_ENV_VAR!r}"

    def test_no_fallback_var_widens_the_flag(self):
        """A `fallback_var` would let an unrelated variable open the channel.

        `knowledge`/`indexing` legitimately fall back to `AGENT_CONTEXT_ENABLED`;
        this key must not, or a broad "enable agent context" switch somewhere would
        also enable a control path into running pods.
        """
        call = self._flag_dict_call(FLAG_KEY)
        assert len(call.args) == 1 and not call.keywords, (
            f"{FLAG_KEY!r} must take exactly one argument; a fallback would let another variable enable live control"
        )


class TestThreePlaceParity:
    """Backend dict, frontend interface + default, k8s env — or CI fails.

    A PR landing two of the three produces either a backend that enables what the
    UI cannot show, or a UI offering controls the backend never routes. Both are
    silent at merge time.
    """

    def test_backend_dict_declares_the_flag(self):
        source = Path(inspect.getfile(features_routes)).read_text()
        assert f'"{FLAG_KEY}"' in source, f"{FLAG_KEY!r} missing from the backend features dict"

    def test_frontend_interface_declares_the_flag(self):
        source = _FRONTEND_FEATURES.read_text()
        interface_body = source.split("export interface FeatureFlags")[1].split("}")[0]
        assert f"{FLAG_KEY}:" in interface_body, (
            f"{FLAG_KEY!r} missing from the frontend FeatureFlags interface — the backend would enable a feature the UI has no key for"
        )

    def test_frontend_default_is_false(self):
        """The fail-open default object must make this flag an exception.

        `useFeatures` returns `ALL_FEATURES_ENABLED` while the /features fetch is
        in flight AND after it fails. `true` here would render pause/steer/abort on
        every page load before the flags arrive, and keep rendering them through a
        backend outage — precisely when the control path cannot deliver. An
        operator who clicks Abort then and sees no error has been told a run
        stopped when it is still running.
        """
        source = _FRONTEND_FEATURES.read_text()
        default_body = source.split("ALL_FEATURES_ENABLED: FeatureFlags = {")[1].split("};")[0]
        assert f"{FLAG_KEY}: false" in default_body, f"{FLAG_KEY!r} must default to false in ALL_FEATURES_ENABLED (fail-closed)"

    def test_k8s_manifest_declares_the_env_var(self):
        manifest = _K8S_DEPLOYMENT.read_text()
        assert FLAG_ENV_VAR in manifest, f"{FLAG_ENV_VAR!r} missing from k8s/deployment.yaml — the operator has nothing to flip"

    def test_k8s_manifest_renders_the_flag_per_environment(self):
        """The manifest must carry the placeholder, never a hard-coded value.

        Asserted on the *active* entry: a commented-out line satisfies a bare
        substring check while shipping nothing.
        """
        lines = [line.strip() for line in _K8S_DEPLOYMENT.read_text().splitlines()]
        active = [line for line in lines if line.startswith("- name:") or line.startswith("value:")]
        assert f"- name: {FLAG_ENV_VAR}" in active, f"{FLAG_ENV_VAR!r} is present but commented out in k8s/deployment.yaml"
        index = active.index(f"- name: {FLAG_ENV_VAR}")
        assert active[index + 1] == f'value: "{_FLAG_PLACEHOLDER}"', (
            f"{FLAG_ENV_VAR!r} must ship as the {_FLAG_PLACEHOLDER} placeholder so each environment opts in "
            f"on its own; got {active[index + 1]!r}. A literal here arms every environment at once, and makes "
            "the #3967 fixture opt-in an edit to a committed file rather than one SSM parameter."
        )

    def test_the_flag_is_not_also_hardcoded_in_the_configmap(self):
        """Two sources for one variable is a silent-precedence bug.

        The configmap is `envFrom`'d and `deployment.yaml`'s explicit `env` wins, so
        a copy here would be dead for the gateway while still reading to an operator
        as the live value — they would flip it and see nothing change. It also
        reintroduces the committed-literal problem the placeholder exists to avoid.

        The configmap legitimately carries the flag's *neighbours*
        (AGENT_CONTROL_PORT, AGENT_CONTROL_CLUSTER_POD_CIDRS), so this asserts on
        the key specifically, not on the absence of the topic.
        """
        lines = [line.strip() for line in _K8S_CONFIGMAP.read_text().splitlines()]
        declarations = [line for line in lines if line.startswith(f"{FLAG_ENV_VAR}:")]
        assert not declarations, (
            f"{FLAG_ENV_VAR!r} is declared in k8s/configmap.yaml as {declarations!r}. It belongs in "
            "deployment.yaml as a rendered placeholder; the deployment's explicit env would override this "
            "copy, leaving an operator editing a value that has no effect."
        )

    def test_deploy_workflow_defaults_the_flag_off(self):
        """Moving the value out of the manifest only stays fail-closed if the renderer defaults off."""
        assert _DEPLOY_WORKFLOW.exists(), f"{_DEPLOY_WORKFLOW} not found"
        workflow = _DEPLOY_WORKFLOW.read_text()
        assert _FLAG_PLACEHOLDER in workflow, (
            f"{_FLAG_PLACEHOLDER} is in the manifest but nothing in gateway-deploy.yml substitutes it — "
            "pods would receive the placeholder string verbatim. That is not 'true' so it reads as off, "
            "but silently and for the wrong reason, and it would survive a real opt-in."
        )
        assert f'get_ssm "{_FLAG_SSM_PARAM}" "false"' in workflow, (
            f'the {FLAG_ENV_VAR!r} render must read {_FLAG_SSM_PARAM} with an explicit "false" default'
        )

    def test_the_rendered_value_is_normalised_before_substitution(self):
        """`get_ssm` prints "None" for a deleted parameter on some CLI versions.

        Without the guard, deleting the SSM parameter to turn the feature *off*
        would render the literal `None`. That reads as off through the strict
        helper, so the symptom is not an outage — it is a pod whose env says
        `None`, which nobody can tell apart from a bug when the feature next
        misbehaves. The sibling flag normalises for the same reason.
        """
        workflow = _DEPLOY_WORKFLOW.read_text()
        guard = f'if [ -z "${FLAG_ENV_VAR}" ] || [ "${FLAG_ENV_VAR}" = "None" ]; then'
        assert guard in workflow, f'the {FLAG_ENV_VAR!r} render must normalise empty/None to "false"; expected {guard!r}'


class TestControlPortAgreesEverywhereItIsWritten:
    """One port, written in four places, and a mismatch is not a fallback.

    The gateway pins the port it will dial rather than reading it from the
    invocation row (so a rewritten row cannot redirect control traffic at, say,
    the kubelet). That deliberate rigidity is what makes a mismatch total: the
    gateway dials 8770, the pod listens on 8771, the NetworkPolicy admits 8771, and
    every control request becomes a 409 "unavailable" while a healthy listener sits
    there. Nothing degrades gracefully and no log says "wrong port".

    The Terraform default and the ScaledJob wiring are cross-checked in
    `modules/agent-factory/webhook-ingress/tests/test_agent_control_manifest.py`,
    which cannot import gateway code. This closes the other half of the loop: the
    gateway's own default and the configmap value it ships with.
    """

    def test_configmap_port_matches_the_code_default(self):
        """The configmap is where an operator reads the port; the code is where it is used."""
        from src.activity.control_service import DEFAULT_CONTROL_PORT

        lines = [line.strip() for line in _K8S_CONFIGMAP.read_text().splitlines()]
        declared = [line for line in lines if line.startswith("AGENT_CONTROL_PORT:")]
        assert len(declared) == 1, f"expected exactly one AGENT_CONTROL_PORT in k8s/configmap.yaml, found {declared!r}"
        value = declared[0].split(":", 1)[1].strip().strip('"')
        assert value == str(DEFAULT_CONTROL_PORT), (
            f"k8s/configmap.yaml ships AGENT_CONTROL_PORT={value!r} but "
            f"control_service.DEFAULT_CONTROL_PORT is {DEFAULT_CONTROL_PORT}. These must agree: the "
            "configmap value is what the pod actually gets, and the code default is what every test "
            "and every reader assumes it is."
        )

    def test_the_configmap_port_is_a_literal_not_a_placeholder(self):
        """Unlike the flag and the CIDRs, the port is the same in every environment.

        Rendering it per environment would add a way for one environment's port to
        drift from the NetworkPolicy's, which is the 409-with-a-healthy-listener
        failure above. A literal here is the safer choice, so it is pinned as one.
        """
        lines = [line.strip() for line in _K8S_CONFIGMAP.read_text().splitlines()]
        declared = next(line for line in lines if line.startswith("AGENT_CONTROL_PORT:"))
        assert "__" not in declared, (
            f"AGENT_CONTROL_PORT must be a literal, got {declared!r}. A per-environment port lets one "
            "environment's gateway dial a port its NetworkPolicy does not admit."
        )

    def test_the_pod_cidr_allowlist_is_rendered_and_fails_closed(self):
        """The CIDRs *are* per-environment, and empty must deny rather than widen.

        The inverse of the port: a pod CIDR is genuinely environment-specific, so a
        literal would be wrong. What must not vary is the failure direction — an
        environment with no SSM parameter gets an empty allowlist and refuses every
        control request, rather than falling back to a convenient wide range that
        would let the gateway dial any private address including link-local IMDS.
        """
        configmap = _K8S_CONFIGMAP.read_text()
        assert 'AGENT_CONTROL_CLUSTER_POD_CIDRS: "__AGENT_CONTROL_CLUSTER_POD_CIDRS__"' in configmap, (
            "AGENT_CONTROL_CLUSTER_POD_CIDRS must ship as a placeholder — a committed CIDR would be wrong for every environment but one"
        )
        workflow = _DEPLOY_WORKFLOW.read_text()
        assert 'get_ssm "/adp/${ENVIRONMENT}/gateway/agent-control-cluster-pod-cidrs" ""' in workflow, (
            "the CIDR allowlist must default to the EMPTY string, not a range. Empty denies every control "
            "request with a clear misconfiguration; a default range silently grants the SSRF boundary away."
        )
        assert "s|__AGENT_CONTROL_CLUSTER_POD_CIDRS__|" in workflow, (
            "nothing substitutes __AGENT_CONTROL_CLUSTER_POD_CIDRS__ — the pod would receive the literal "
            "placeholder, which parses as no valid CIDR and so denies everything, but for the wrong reason"
        )


class TestFlagDoesNotEnableUnimplementedVerbs:
    """The flag enables routing; runtime support and authority still gate control."""

    def test_only_implemented_verbs_are_available(self, monkeypatch):
        monkeypatch.setenv(FLAG_ENV_VAR, "true")
        from src.activity.control_service import SUPPORTED_ACTIONS

        assert SUPPORTED_ACTIONS == frozenset({"pause", "resume"})
