"""Terraform/manifest guards for the live-control pod surface (Issue #3960).

Three things about the control channel are decided in Terraform rather than in
code, and all three are invisible at runtime until they are wrong:

1. **The ingress NetworkPolicy exists at all, and is namespace-scoped.** The
   listener requires a bearer token, but the policy is what decides who may open a
   TCP connection to it. Delete the policy and a token-comparison bug becomes
   exploitable from any pod in the cluster instead of only from the gateway.
2. **The policy is deployed before the listener can start.** The policy is
   unconditional; the listener is flag-gated. Reverse that and there is a window
   where the port is open to the cluster with nothing guarding reachability.
3. **The pod binds its own IP, not every interface.** `POD_IP` comes from the
   downward API. If it is missing the worker refuses to listen at all — so the
   variable's presence is load-bearing, not cosmetic.

These read the ``.tf`` source as text, matching the approach already established
in ``test_scaledjob_manifest.py``: the ScaledJob is a heredoc applied via kubectl
local-exec, so there is no plan or cluster harness available in the unit suite. No
AWS, no cluster, no terraform binary, no new dependencies.

The tests are written to fail if a rule is *weakened*, not merely if it is absent —
a policy that selects all namespaces, or that ORs its selectors instead of ANDing
them, is worse than a missing one because it reads as protection in a diff.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).resolve().parents[1] / "infra"
SCALEDJOB_TF = INFRA / "scaledjob.tf"
NETPOL_TF = INFRA / "scaledjob-netpol.tf"
VARIABLES_TF = INFRA / "variables.tf"

# Must equal the gateway's DEFAULT_CONTROL_PORT
# (modules/gateway/src/activity/control_service.py). The gateway pins the port it
# will dial instead of reading it from the invocation row, so a mismatch between
# the two sides is not a fallback — it is a 409 on every control click.
EXPECTED_DEFAULT_PORT = "8770"


def _read(path: Path) -> str:
    assert path.is_file(), f"terraform source not found: {path}"
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop ``#`` comment lines.

    Essential here: these files document what they reject *by name*, so a naive
    substring search would match the prose warning against a setting rather than
    the setting itself. Every assertion about live configuration runs against this.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def netpol() -> str:
    return _strip_comments(_read(NETPOL_TF))


@pytest.fixture(scope="module")
def netpol_raw() -> str:
    return _read(NETPOL_TF)


@pytest.fixture(scope="module")
def scaledjob() -> str:
    return _strip_comments(_read(SCALEDJOB_TF))


@pytest.fixture(scope="module")
def variables() -> str:
    return _read(VARIABLES_TF)


def _resource_block(text: str, kind: str, name: str) -> str:
    """Extract one ``resource "kind" "name" { ... }`` block by brace balance.

    Brace-counted rather than regex-delimited because these blocks nest several
    levels deep; a lazy regex would stop at the first inner ``}`` and quietly
    return a fragment, making assertions pass or fail for the wrong reason.
    """
    marker = f'resource "{kind}" "{name}"'
    start = text.find(marker)
    assert start != -1, f"{marker} not found"
    depth = 0
    for index in range(text.index("{", start), len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces in {marker}")


class TestControlListenerIngressPolicy:
    """The reachability boundary for the in-pod listener (AC-S6, FR-1.6)."""

    def test_the_policy_exists(self, netpol: str):
        """Its absence is not a degraded feature — it is an open port.

        Kubernetes ingress is allow-all until some policy selects a pod for
        Ingress. The namespace's default-deny sets `policy_types = ["Egress"]`
        only, so nothing else in this namespace creates that deny.
        """
        assert 'resource "kubernetes_network_policy" "agent_control_listener_ingress"' in netpol, (
            "The control-listener ingress NetworkPolicy is missing. Without it the "
            "listener port is reachable from every pod in the cluster and the "
            "per-run bearer token becomes the only barrier — see #3960 FR-1.6."
        )

    def test_it_declares_the_ingress_policy_type(self, netpol: str):
        """`policy_types` must name Ingress, or the rules deny nothing.

        A policy whose type list omits Ingress is inert for ingress no matter what
        `ingress` blocks it contains — the most convincing possible false positive.
        """
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        assert re.search(r'policy_types\s*=\s*\[\s*"Ingress"\s*\]', block), (
            'policy_types must be exactly ["Ingress"]. A policy that omits the '
            "Ingress type creates no deny, so its ingress rules restrict nothing."
        )

    def test_it_selects_the_agent_worker_pods(self, netpol: str, scaledjob: str):
        """The selector must match the label the ScaledJob template actually sets.

        Asserted against the ScaledJob source rather than a hardcoded string: a
        selector that matches nothing produces a policy that protects nothing, and
        that is invisible in review if the two files are read separately.
        """
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        pod_selector = block[block.index("pod_selector") : block.index("policy_types")]
        assert '"app.kubernetes.io/name" = "agent-scaledjob"' in pod_selector

        template_labels = scaledjob[
            scaledjob.index("template:") : scaledjob.index("serviceAccountName")
        ]
        assert "app.kubernetes.io/name: agent-scaledjob" in template_labels, (
            "The ScaledJob pod template no longer carries the label the control "
            "ingress policy selects on. The policy would select zero pods."
        )

    def test_the_allowlisted_port_is_the_control_port_variable(self, netpol: str):
        """One port, from the variable — not a literal that can drift."""
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        ingress = block[block.index("ingress {") :]
        assert re.search(r"port\s*=\s*var\.agent_control_port", ingress), (
            "The ingress rule must allow var.agent_control_port. A hardcoded port "
            "here can drift from the port the worker binds and the gateway dials."
        )

    def test_it_admits_only_the_gateway_namespace(self, netpol: str):
        """A cluster-wide `from` would admit every workload; an empty one, everything."""
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        ingress = block[block.index("ingress {") :]
        assert "namespace_selector" in ingress, (
            "The ingress rule must be namespace-scoped. An unscoped `from` (or no "
            "`from` at all) admits the whole cluster to the control port."
        )
        assert re.search(r'"kubernetes\.io/metadata\.name"\s*=\s*var\.gateway_namespace', ingress)

    def test_it_admits_only_the_gateway_pods(self, netpol: str):
        """Namespace alone is not enough: it would admit any sidecar or debug pod."""
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        ingress = block[block.index("ingress {") :]
        assert re.search(r'"app"\s*=\s*"bedrockgateway"', ingress), (
            "The ingress rule must also select the gateway pods by label. "
            "Namespace-only scoping admits anything scheduled into that namespace."
        )

    def test_the_two_selectors_are_anded_in_one_from_block(self, netpol: str):
        """This is the subtle one, and it is a real Kubernetes footgun.

        Two selectors inside ONE `from` block are ANDed ("these pods in that
        namespace"). Split across TWO `from` blocks they are ORed, which
        additionally admits any pod labelled `app=bedrockgateway` in ANY namespace
        — a tenant could then create such a pod in their own namespace and reach
        every agent's control port. The two forms differ by one level of
        indentation and are nearly indistinguishable in a diff.
        """
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        ingress = block[block.index("ingress {") :]
        assert ingress.count("from {") == 1, (
            f"Expected exactly one `from` block, found {ingress.count('from {')}. "
            "Selectors in separate `from` blocks are ORed, not ANDed: that would "
            "admit any pod labelled app=bedrockgateway in ANY namespace, which a "
            "tenant can create."
        )

    def test_the_policy_is_not_flag_conditional(self, netpol: str):
        """Policy-before-listener (FR-8.4), expressed as the absence of a count.

        The listener is flag-gated; this policy must not be. A `count`/`for_each`
        tied to `agent_control_enabled` would mean flipping the flag on in an
        environment that had skipped the policy opens the port to the cluster —
        and the ordering between two Terraform resources in one apply is not a
        guarantee you want a security boundary to rest on. Guarding an unused port
        costs nothing.
        """
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        assert not re.search(r"^\s*(count|for_each)\s*=", block, re.MULTILINE), (
            "The control-listener ingress policy must be unconditional. Gating it "
            "on agent_control_enabled creates a window in which the flag is on and "
            "the policy is absent (#3960 FR-8.4)."
        )

    def test_the_policy_is_verb_blind(self, netpol: str):
        """It names a port, never a path or a method.

        A path-aware rule would have to be revisited by every later story (pause,
        abort, and the reserved /agent/events stream), which is how a boundary gets
        re-litigated and eventually widened (ADR-9).
        """
        block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_control_listener_ingress"
        )
        for verb in ("pause", "resume", "steer", "abort", "/agent/"):
            assert verb not in block, (
                f"The ingress policy mentions {verb!r}. It must be verb-blind — a "
                "port-level rule so no later story needs to change the policy."
            )

    def test_the_rationale_survives_in_the_file(self, netpol_raw: str):
        """The next reader will see a policy for a port with no listener.

        Without the in-file reason, "this guards nothing, delete it" is a
        reasonable-looking cleanup.
        """
        comments = "\n".join(
            line for line in netpol_raw.splitlines() if line.lstrip().startswith("#")
        )
        for token in ("3960", "token", "0.0.0.0"):
            assert token in comments, (
                f"scaledjob-netpol.tf comments must explain {token!r} — otherwise the "
                "policy reads as guarding nothing and gets removed."
            )


class TestNoInClusterEgressToTheGateway:
    """Agent pods must have NO direct network path to the gateway (#3960).

    A dead rule here previously *claimed* to allow agent pods → gateway on 8080
    while matching zero namespaces. Repairing it so the selector genuinely matched
    would open a live privilege-escalation path, because the gateway treats
    `X-Caller-Identity` as an authenticated identity assertion and only API Gateway
    sanitizes that header — so a compromised agent pod could reach `/internal/v1/*`
    directly and assert an internal-plane identity. It was removed instead.

    These tests pin the *absence*. An absence is the hardest thing to keep: nothing
    breaks when someone adds the rule back, so without a test the next person who
    wants in-cluster gateway access simply adds it and the escalation path returns
    silently. Reopening it requires a pod_selector, a gateway-side ingress policy
    and an app-side header guard — which is a deliberate change that should have to
    delete a failing test that says so, not a one-line addition.
    """

    def test_no_egress_rule_targets_the_gateway_namespace(self, netpol: str):
        egress_block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_scaledjob_egress"
        )
        assert "var.gateway_namespace" not in egress_block, (
            "An egress rule targets the gateway namespace again. Agent pods must "
            "reach the gateway only through the sigv4-proxy → API Gateway path "
            "(covered by the blanket 443 rule). A direct in-cluster path bypasses "
            "the only component that sanitizes X-Caller-Identity, which the gateway "
            "trusts as an authenticated identity — see the comment in "
            "scaledjob-netpol.tf for the three prerequisites to reopening it."
        )

    def test_the_dead_component_label_selector_is_not_reintroduced(self, netpol: str):
        """The original selector, kept out by name.

        `app.kubernetes.io/component=gateway` on a `namespace_selector` matches
        nothing (the label is on the gateway's pods, not the namespace object).
        Re-adding it would restore a rule that reads as granting access and grants
        none — the misleading state that started this.
        """
        egress_block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_scaledjob_egress"
        )
        assert '"app.kubernetes.io/component" = "gateway"' not in egress_block

    def test_the_gateway_service_ports_are_not_opened_by_any_egress_rule(self, netpol: str):
        """No egress rule may name 80 or 8080.

        Checked in addition to the namespace assertion above because a rule with no
        `to` block is broader still: it would allow those ports cluster-wide. The
        gateway Service is port 80 → targetPort 8080, and which one an egress policy
        observes is CNI-dependent, so both have to stay closed for the path to be
        closed on either.
        """
        egress_block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_scaledjob_egress"
        )
        for port in ("80", "8080"):
            assert not re.search(rf"port\s*=\s*{port}\b", egress_block), (
                f"An egress rule opens port {port}. The gateway Service is 80 → "
                "targetPort 8080; opening either restores a direct pod → gateway "
                "path. Ports genuinely needed by agent pods are 53, 443, 22, 4317 "
                "and 5100."
            )

    def test_the_ports_agent_pods_genuinely_need_are_still_allowed(self, netpol: str):
        """The removal must not have taken a working path with it.

        Without this, the assertions above are satisfiable by deleting the whole
        egress policy — which would break every agent run rather than close one
        path.
        """
        egress_block = _resource_block(
            netpol, "kubernetes_network_policy", "agent_scaledjob_egress"
        )
        for port in ("53", "443", "22", "4317", "5100"):
            assert re.search(rf"port\s*=\s*{port}\b", egress_block), (
                f"Egress port {port} is missing. Agent pods need DNS (53), HTTPS to "
                "GitHub/registries/AWS incl. API Gateway (443), git-over-SSH (22), "
                "the ADOT collector (4317) and the agent-context MCP server (5100)."
            )


class TestPodIpIsSuppliedForAnExplicitBind:
    """The worker refuses to listen without POD_IP (FR-1.7)."""

    def test_pod_ip_comes_from_the_downward_api(self, scaledjob: str):
        assert "fieldPath: status.podIP" in scaledjob, (
            "POD_IP must be injected from the downward API. Without it the worker "
            "starts no listener at all — it does NOT fall back to 0.0.0.0."
        )

    def test_pod_ip_is_named_exactly_as_the_worker_reads_it(self, scaledjob: str):
        """`entrypoint.py` reads `POD_IP`. A renamed variable silently disables control."""
        assert re.search(r"^\s*- name: POD_IP\s*$", scaledjob, re.MULTILINE), (
            "The env var must be named POD_IP exactly — entrypoint.py reads that "
            "name, and a mismatch disables the listener with no error at apply time."
        )

    def test_pod_ip_is_supplied_unconditionally(self, scaledjob: str):
        """Not inside the flag-gated block, so enabling the feature is one variable.

        Asserted structurally: POD_IP must appear in the always-rendered heredoc,
        not in `agent_control_env_block`.
        """
        gated = scaledjob[
            scaledjob.index("agent_control_env_block = ") : scaledjob.index(
                "keda_trigger_auth_yaml"
            )
        ]
        assert "POD_IP" not in gated, (
            "POD_IP must not live in the flag-gated env block. Supplying it always "
            "means there is no configuration where the flag is on and the bind "
            "address is missing."
        )

    def test_no_wildcard_bind_address_is_configured(self, scaledjob: str):
        """A 0.0.0.0 bind would make the ingress policy the only boundary."""
        assert "0.0.0.0" not in scaledjob, (
            "No control bind address may be 0.0.0.0. The explicit pod-IP bind is "
            "one of three independent layers (token, policy, bind)."
        )


class TestFlagAndPortPropagation:
    """What the pod is told, and when (AC-F2)."""

    def test_listener_env_requires_read_or_mutation_enablement(self, scaledjob: str):
        assert "(var.agent_control_enabled || var.agent_explanations_enabled) ?" in scaledjob
        assert ': ""' in scaledjob

    def test_read_and_mutation_flags_use_independent_booleans(self, scaledjob: str):
        gated = scaledjob[scaledjob.index("agent_control_env_block = "):scaledjob.index("keda_trigger_auth_yaml")]
        for flag, variable in (("CONTROL", "agent_control_enabled"), ("EXPLANATIONS", "agent_explanations_enabled")):
            lines = gated.splitlines()
            index = next(i for i, line in enumerate(lines) if f"FEATURE_AGENT_{flag}_ENABLED" in line)
            assert "${var." + variable + "}" in lines[index + 1]

    def test_the_port_is_passed_from_the_variable(self, scaledjob: str):
        gated = scaledjob[
            scaledjob.index("agent_control_env_block = ") : scaledjob.index(
                "keda_trigger_auth_yaml"
            )
        ]
        assert "ADP_CONTROL_PORT" in gated
        assert "${var.agent_control_port}" in gated, (
            "ADP_CONTROL_PORT must be rendered from var.agent_control_port so the "
            "bound port, the allowlisted port and the dialled port cannot diverge."
        )

    def test_the_pod_deadline_is_passed_to_the_worker(self, scaledjob: str):
        """The token TTL is derived from it, so the pod has to be told what it is.

        Without this variable the worker falls back to its 6h ceiling regardless of
        the real deadline, which on a short-deadline environment leaves a valid
        credential for hours after the pod is gone — and pod IPs are reused.
        """
        gated = scaledjob[
            scaledjob.index("agent_control_env_block = ") : scaledjob.index(
                "keda_trigger_auth_yaml"
            )
        ]
        assert "ADP_POD_DEADLINE_SECONDS" in gated, (
            "ADP_POD_DEADLINE_SECONDS must be injected: entrypoint.py bounds the "
            "control token's expiry by it."
        )
        assert "${var.agent_pod_deadline_seconds}" in gated, (
            "It must render from var.agent_pod_deadline_seconds — the same variable "
            "as activeDeadlineSeconds — so the credential's lifetime and the pod's "
            "cannot drift apart."
        )

    def test_the_deadline_the_worker_reads_is_the_deadline_kubernetes_enforces(
        self, scaledjob: str
    ):
        """One variable feeds both, asserted as such.

        A literal in either place would let the two diverge silently: the pod would
        be killed at one time and its token expire at another, and no apply-time or
        runtime error would say so.
        """
        assert "activeDeadlineSeconds: ${var.agent_pod_deadline_seconds}" in scaledjob, (
            "activeDeadlineSeconds must come from var.agent_pod_deadline_seconds, "
            "the same variable ADP_POD_DEADLINE_SECONDS is rendered from."
        )

    def test_no_control_token_appears_in_the_manifest(self, scaledjob: str):
        """The token is minted per run inside the pod, never injected.

        A manifest-supplied token would be one value shared by every run, visible
        in `kubectl describe`, and impossible to rotate per run.
        """
        assert "ADP_CONTROL_TOKEN" not in scaledjob, (
            "ADP_CONTROL_TOKEN must never be set in the manifest. It is minted per "
            "run by secrets.token_urlsafe in entrypoint.py and written to the "
            "invocation row."
        )

    def test_the_declared_container_port_matches_the_variable(self, scaledjob: str):
        assert re.search(r"containerPort:\s*\$\{var\.agent_control_port\}", scaledjob), (
            "The declared containerPort must come from var.agent_control_port."
        )


class TestControlVariables:
    """Defaults are the off/closed position (DP-INV-1)."""

    def test_the_feature_defaults_off(self, variables: str):
        """Ordinary workloads must not acquire a control listener by upgrading."""
        block = variables[variables.index('variable "agent_control_enabled"') :]
        block = block[: block.index("\n}") + 2]
        assert re.search(r"default\s*=\s*false", block), (
            "agent_control_enabled must default to false. This story ships the "
            "authenticated path with every verb answering 501; enabling it by "
            "default would open a channel with no working verb behind it."
        )

    def test_the_port_default_matches_the_gateway(self, variables: str):
        """A mismatch is a 409 on every click, not a fallback."""
        block = variables[variables.index('variable "agent_control_port"') :]
        block = block[: block.index("\n}\n") + 3]
        assert re.search(rf"default\s*=\s*{EXPECTED_DEFAULT_PORT}\b", block), (
            f"agent_control_port must default to {EXPECTED_DEFAULT_PORT}, matching "
            "DEFAULT_CONTROL_PORT in modules/gateway/src/activity/control_service.py."
        )

    def test_the_port_rejects_privileged_values(self, variables: str):
        """The pod runs as UID 1001 with capabilities dropped — <1024 cannot bind."""
        block = variables[variables.index('variable "agent_control_port"') :]
        block = block[: block.index("\n}\n") + 3]
        assert "validation" in block and "1024" in block, (
            "agent_control_port needs a validation block rejecting privileged "
            "ports: the agent pod runs as non-root with NET_BIND_SERVICE dropped, "
            "so a value below 1024 fails at bind time inside the pod rather than "
            "at plan time."
        )

    def test_the_gateway_namespace_variable_exists(self, variables: str):
        assert 'variable "gateway_namespace"' in variables
        block = variables[variables.index('variable "gateway_namespace"') :]
        assert re.search(r'default\s*=\s*"adp-gateway"', block)
