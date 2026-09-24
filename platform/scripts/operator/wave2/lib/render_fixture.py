#!/usr/bin/env python3
"""Render the Wave 2 fixture manifests FROM the live production composition.

Issue #3968 / epic #3959.

WHAT THIS FIXES
---------------
The published ``10-create-fixture.sh`` hand-wrote the fixture gateway's pod spec:
two ``configMapRef`` entries and two env vars. That lost all NINE secret-backed
env references the real gateway carries --

    ADP_MARKER_SIGNING_KEY              agent-run-services/marker-signing-key
    ADP_DOOR_SERVICE_KEY                bedrockgateway-secrets/internal-api-key
    AGENT_RUN_CREDENTIAL_KEY            agent-authority-signing/run-credential-key
    AGENT_CONTROL_ENVELOPE_SIGNING_KEY  agent-authority-signing/envelope-signing-key
    BG_TOKEN_SECRET_KEY                 bedrockgateway-secrets/token-secret-key
    BG_INTERNAL_API_KEY                 bedrockgateway-secrets/internal-api-key
    BG_APIGW_PROVENANCE_SECRET          bedrockgateway-secrets/apigw-provenance-secret
    BG_MAGIC_LINK_SECRET                bedrockgateway-secrets/magic-link-secret
    BG_GITHUB_APP_PRIVATE_KEY           bedrockgateway-secrets/BG_GITHUB_APP_PRIVATE_KEY

-- without which the fixture cannot verify a session token, validate edge
provenance, sign a control envelope, or authenticate the protected worker. Every
identity and control check would fail for reasons unrelated to the software under
review.

THE FIX, AND WHY IT IS SHAPED THIS WAY
--------------------------------------
Do not re-list the composition; COPY it. This module reads the live
``deployment/bedrockgateway`` container spec as JSON and mutates a small, explicit
set of fields. Anything not named in ``OVERRIDES`` is carried over byte-for-byte,
so a tenth secret added to the real gateway tomorrow appears in the fixture
automatically instead of being silently absent.

Hand-listing is how the nine were lost. A copy cannot lose them.

WHAT IS DELIBERATELY OVERRIDDEN (and nothing else)
--------------------------------------------------
* ``FEATURE_AGENT_CONTROL_ENABLED=true``  -- the feature under evaluation. The
  gateway reads this strictly (only the literal "true"), and it is ON only here.
* ``AGENT_AUTHORITY_ENABLED=true``        -- inherited false from the shared
  ConfigMap; without it the protected worker cannot be authenticated at all.
* ``ADP_RUN_TASKS_ENABLED=true``          -- likewise inherited false; the task
  routes 503 without it.
* ``ADP_RUN_TASK_QUEUE_URL=<dedicated>``  -- the gateway's task path reads THIS
  variable (``agentauth/task_routes.py``), and it is normally set only by
  Terraform. The published fixture never set it, so the fixture gateway would
  have published to nothing, or to the shared queue. Pointed at the run's own
  dedicated queue.
* ``AGENT_DISPATCH_QUEUE_URL=<dedicated>`` -- the other queue reference
  (``agentauth/routes.py``), redirected for the same reason: no fixture traffic
  may reach the shared queue.
* ``AGENT_CONTROL_PORT``                  -- carried over, asserted 8770.

Replica count drops to 1, the image is pinned by digest, names/labels/selectors
become run-unique, and probes are retained unchanged. The ordinary Deployment
object is never read for mutation and never written.

THE PROTECTED WORKER (``render_worker_job``)
--------------------------------------------
Same pattern, different subject, and added later than the gateway for a reason.
The worker Job used to be refused outright because its
``ADP_AGENT_CONTROL_ENDPOINT`` had to be an https API Gateway URL and every such
URL routed, by pod label, to the ORDINARY gateway -- so a fixture worker would
have bootstrapped against live traffic. #5836 built a fixture-only edge whose
``worker_control_endpoint`` terminates at the fixture gateway, which removes that
constraint, so ``render_worker_job`` composes a worker the gateway's real verifier
(``modules/gateway/src/agentauth/workload.py``) will accept, and
``assert_verifier_admissible`` checks the RENDERED object against each of that
verifier's decidable conditions before anything is created.

One thing it deliberately does not do: give the worker any way to read the
cluster. The protected service account cannot ``get`` or ``list`` pods, and that
boundary is preserved. The pod's own ``metadata.uid`` is projected through a
downwardAPI volume instead -- the one identity the process cannot rewrite for
itself -- and the operator, which can read the API server, binds everything else
(service account, container, resolved image digest) to that uid.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import re
import sys
from collections.abc import Sequence
from typing import Any
from pathlib import Path

# The nine secret-backed env references the real gateway carries. This list is
# NOT used to build the fixture -- the live spec is copied instead. It is used to
# ASSERT that the copy actually contains them, so a fixture that silently lost a
# secret ref fails here rather than at the first identity check after root has
# already created live resources.
EXPECTED_SECRET_ENV: dict[str, tuple[str, str]] = {
    "ADP_MARKER_SIGNING_KEY": ("agent-run-services", "marker-signing-key"),
    "ADP_DOOR_SERVICE_KEY": ("bedrockgateway-secrets", "internal-api-key"),
    "AGENT_RUN_CREDENTIAL_KEY": ("agent-authority-signing", "run-credential-key"),
    "AGENT_CONTROL_ENVELOPE_SIGNING_KEY": ("agent-authority-signing", "envelope-signing-key"),
    "BG_TOKEN_SECRET_KEY": ("bedrockgateway-secrets", "token-secret-key"),
    "BG_INTERNAL_API_KEY": ("bedrockgateway-secrets", "internal-api-key"),
    "BG_APIGW_PROVENANCE_SECRET": ("bedrockgateway-secrets", "apigw-provenance-secret"),
    "BG_MAGIC_LINK_SECRET": ("bedrockgateway-secrets", "magic-link-secret"),
    "BG_GITHUB_APP_PRIVATE_KEY": ("bedrockgateway-secrets", "BG_GITHUB_APP_PRIVATE_KEY"),
}

# Secret refs that must resolve for the CONTROL path specifically. If one of
# these is absent from the cluster, the fixture cannot mint or verify a control
# envelope, and the evaluation must stop BEFORE creating anything rather than
# produce a gateway that 503s on every control verb.
CONTROL_CRITICAL_SECRETS: tuple[tuple[str, str], ...] = (
    ("agent-authority-signing", "envelope-signing-key"),
    ("agent-authority-signing", "run-credential-key"),
    ("bedrockgateway-secrets", "token-secret-key"),
    ("bedrockgateway-secrets", "apigw-provenance-secret"),
)

ORDINARY_GATEWAY_LABEL = "bedrockgateway"
CONTROL_PORT = "8770"

# The port the fixture gateway's pods serve, named once because three things must
# agree about it: the Service's targetPort, the in-cluster ingress rule, and the ALB
# ingress rule's port. Root found what happens when the third is merely "an integer
# that differs from the listener port" -- #5836's document reporting 8081 rendered a
# rule admitting the load balancer to a port nothing serves, and that denial is
# indistinguishable from the finding under test. The value is passed to
# edge_receipt.resolve_alb_policy_source as the EXPECTATION so the published
# container_port is checked against what is actually rendered here.
FIXTURE_POD_PORT = 8080

# ---------------------------------------------------------------------------
# what the gateway's own verifier requires of a protected worker
# ---------------------------------------------------------------------------
# These are not this file's preferences. Every one is read back out of
# ``modules/gateway/src/agentauth/workload.py`` (``KubernetesWorkloadVerifier``),
# which is the code that decides whether a pod presenting a projected token is
# admitted as a protected worker. A fixture worker that misses any of them is not
# "slightly wrong" -- it is refused, and the refusal looks identical to the
# feature being broken. So they are asserted on the rendered object BEFORE
# anything is created, while the operator is still in a position to fix it.
WORKER_CONTAINER = "agent-worker"
WORKER_NAMESPACE = "adp-agents"
WORKER_SERVICE_ACCOUNT = "agent-authority-worker-sa"
AUTHORITY_FLAG = "ADP_AGENT_AUTHORITY_ENABLED"

# The audience the gateway passes to TokenReview (workload.py BOOTSTRAP_AUDIENCE).
# A projected token minted for any other audience authenticates as nothing here.
BOOTSTRAP_AUDIENCE = "adp-agent-bootstrap"
WORKLOAD_TOKEN_DIR = "/var/run/adp-workload"
WORKLOAD_TOKEN_PATH = f"{WORKLOAD_TOKEN_DIR}/token"
CONTROL_KEYS_DIR = "/var/run/adp-control-keys"
CONTROL_KEYS_PATH = f"{CONTROL_KEYS_DIR}/keys.json"
CONTROL_KEYS_CONFIGMAP = "adp-control-verification-keys"
WORKLOAD_VOLUME = "adp-workload-identity"
CONTROL_KEYS_VOLUME = "adp-control-verification-keys"

# Where the pod's own unforgeable identity is projected, and the only field the
# container can be given that the process cannot rewrite for itself.
#
# A downwardAPI volume fieldRef supports exactly: annotations, labels, name,
# namespace, uid. It CANNOT reference spec.serviceAccountName -- root confirmed
# this, and an earlier revision of the experiment checker depended on a
# `service-account.name` file that no mount ever provides. So the container is
# given `metadata.uid` and the operator does the rest: it reads the pod from the
# API server, where serviceAccountName / container / resolved image digest all
# live, and records them against that uid. The container proves it IS that uid;
# everything the operator observed about that uid then follows.
POD_IDENTITY_DIR = "/var/run/adp-w2-identity"
POD_IDENTITY_VOLUME = "adp-w2-pod-identity"
POD_IDENTITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("pod-uid", "metadata.uid"),
    ("pod-name", "metadata.name"),
    ("pod-namespace", "metadata.namespace"),
    ("pod-labels", "metadata.labels"),
)


class RenderError(Exception):
    """A composition problem that must stop the run before anything is created."""


# ---------------------------------------------------------------------------
# env helpers
# ---------------------------------------------------------------------------
def _env_index(env: list[dict]) -> dict[str, int]:
    return {entry["name"]: pos for pos, entry in enumerate(env) if "name" in entry}


def _set_env(env: list[dict], name: str, value: str) -> None:
    """Set a literal env value, replacing any existing entry for that name.

    Replacement (not append) matters: Kubernetes takes the LAST duplicate, but a
    duplicated name with conflicting values is unreadable in a review, and the
    whole point of the fixture is that a reviewer can see what differs.
    """
    index = _env_index(env)
    entry = {"name": name, "value": value}
    if name in index:
        env[index[name]] = entry
    else:
        env.append(entry)


def secret_env_refs(env: list[dict]) -> dict[str, tuple[str, str]]:
    """Extract the secretKeyRef-backed env vars actually present in a spec."""
    found = {}
    for entry in env:
        ref = (entry.get("valueFrom") or {}).get("secretKeyRef")
        if ref and entry.get("name"):
            found[entry["name"]] = (ref.get("name", ""), ref.get("key", ""))
    return found


# ---------------------------------------------------------------------------
# the gateway
# ---------------------------------------------------------------------------
def render_gateway(
    live_deployment: dict,
    *,
    run_id: str,
    nonce: str,
    name: str,
    namespace: str,
    image: str,
    queue_url: str,
    fixture_worker_digest: str | None = None,
    cluster_pod_cidrs: str | None = None,
) -> tuple[dict, dict, dict]:
    """Return (deployment, service, report) for the fixture gateway.

    ``report`` records what was carried over and what was overridden, so the
    evidence can show the fixture is the real composition rather than asserting
    it.
    """
    try:
        pod_spec = copy.deepcopy(live_deployment["spec"]["template"]["spec"])
        live_meta = live_deployment["spec"]["template"]["metadata"]
    except (KeyError, TypeError) as exc:
        raise RenderError(
            "could not read the live gateway's pod template. The fixture must be a copy of the "
            "real composition; assembling a lookalike by hand is what lost the nine secret "
            f"references in the published version. Underlying error: {exc}"
        ) from exc

    containers = pod_spec.get("containers") or []
    target = next((c for c in containers if c.get("name") == ORDINARY_GATEWAY_LABEL), None)
    if target is None:
        raise RenderError(
            f"the live deployment has no container named {ORDINARY_GATEWAY_LABEL!r}; "
            f"found {[c.get('name') for c in containers]!r}. Refusing to guess which one to clone."
        )

    env = target.setdefault("env", [])

    # --- assert the copy really carries the nine ---------------------------
    present = secret_env_refs(env)
    missing = sorted(set(EXPECTED_SECRET_ENV) - set(present))
    if missing:
        raise RenderError(
            f"the live gateway spec is missing {len(missing)} expected secret-backed env "
            f"reference(s): {missing}. Either the live deployment is not the reviewed "
            "composition, or this expectation is stale. Do NOT proceed: a fixture without "
            "these cannot verify sessions, edge provenance, or control signatures, and every "
            "identity check would fail for the wrong reason."
        )
    mismatched = {
        var: {"live": present[var], "expected": EXPECTED_SECRET_ENV[var]}
        for var in EXPECTED_SECRET_ENV
        if present[var] != EXPECTED_SECRET_ENV[var]
    }
    if mismatched:
        raise RenderError(
            f"secret references differ from the reviewed composition: {mismatched}. The fixture "
            "copies whatever is live, so this is reported rather than silently accepted -- "
            "confirm the change is intended before evaluating against it."
        )

    # --- carried-over control port -----------------------------------------
    # Read from envFrom ConfigMaps at runtime, so it may legitimately be absent
    # from the inline env list. Only assert when it IS inline.
    inline = {e["name"]: e.get("value") for e in env if "name" in e}
    if "AGENT_CONTROL_PORT" in inline and inline["AGENT_CONTROL_PORT"] != CONTROL_PORT:
        raise RenderError(
            f"AGENT_CONTROL_PORT is {inline['AGENT_CONTROL_PORT']!r} inline, expected "
            f"{CONTROL_PORT!r}. The worker listener port is what the fixture NetworkPolicy "
            "must permit; a mismatch would produce a policy that blocks the flow under test."
        )

    # envFrom references do not establish that transport CIDRs are populated.
    # Require an observed inline value or explicit operator input before rendering.
    cidrs = cluster_pod_cidrs if cluster_pod_cidrs is not None else inline.get("AGENT_CONTROL_CLUSTER_POD_CIDRS")
    if not isinstance(cidrs, str) or not cidrs.strip():
        raise RenderError("AGENT_CONTROL_CLUSTER_POD_CIDRS is missing; supply --cluster-pod-cidrs from verified cluster networking before enabling fixture controls")
    try:
        networks = [ipaddress.ip_network(value.strip(), strict=True) for value in cidrs.split(",")]
        private = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
        if any(not any(n.version == p.version and n.subnet_of(p) for p in private) for n in networks):
            raise ValueError("CIDRs must be contained in private pod address space")
    except ValueError as exc:
        raise RenderError("invalid --cluster-pod-cidrs: " + str(exc)) from exc
    cidrs = ",".join(str(network) for network in networks)

    # --- the deliberate overrides -----------------------------------------
    overrides = {
        # The feature under evaluation. Strict reader: only the literal "true".
        "FEATURE_AGENT_CONTROL_ENABLED": "true",
        "AGENT_CONTROL_CLUSTER_POD_CIDRS": cidrs,
        # Inherited false from the shared ConfigMap. Without this the gateway
        # cannot authenticate the protected worker at all.
        "AGENT_AUTHORITY_ENABLED": "true",
        # Also inherited false; agentauth/task_delivery.py reads it and the task
        # routes 503 without it.
        "ADP_RUN_TASKS_ENABLED": "true",
        # The variable the task path actually reads. Normally Terraform-only, so
        # a k8s-only clone leaves it unset -- which is why the published fixture
        # could not deliver a task. Pointed at the run's OWN queue.
        "ADP_RUN_TASK_QUEUE_URL": queue_url,
        # The other queue reference. Redirected so no fixture traffic can reach
        # the shared submit queue.
        "AGENT_DISPATCH_QUEUE_URL": queue_url,
    }
    if fixture_worker_digest is not None:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", fixture_worker_digest):
            raise RenderError("fixture worker approval must be one exact sha256 digest")
        # This literal overrides inherited envFrom only in the nonce-owned clone.
        # The shared registry is never modified for an evaluation.
        overrides["AGENT_WORKER_IMAGE_DIGESTS"] = fixture_worker_digest
    for key, value in overrides.items():
        _set_env(env, key, value)

    target["image"] = image
    # A digest pin is the point; a tag would let the evaluated revision drift.
    if "@sha256:" not in image:
        raise RenderError(f"fixture image {image!r} is not digest-pinned")

    labels = {
        "app": name,
        # Ordinary worker ingress selects the gateway by this label. Carried so
        # the fixture is admitted by the existing allowlist without relabelling
        # it to impersonate the ordinary deployment.
        "app.kubernetes.io/part-of": "bedrock-gateway",
        "adp.io/w2-fixture": run_id,
        "adp.io/w2-nonce": nonce,
    }

    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {
                    "labels": dict(labels),
                    # Keep voluntary node consolidation from interrupting the
                    # single-replica gateway during live control measurements.
                    # The owned fixture Deployment is deleted during cleanup.
                    "annotations": {
                        **(live_meta.get("annotations") or {}),
                        "karpenter.sh/do-not-disrupt": "true",
                    },
                },
                "spec": pod_spec,
            },
        },
    }

    # topologySpreadConstraints select the ORDINARY app label; left unchanged
    # they would spread against the wrong pod set and can block scheduling of a
    # single replica. Retarget to this fixture's own selector.
    for constraint in pod_spec.get("topologySpreadConstraints") or []:
        selector = constraint.get("labelSelector", {}).get("matchLabels", {})
        if selector.get("app") == ORDINARY_GATEWAY_LABEL:
            selector["app"] = name

    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            # Mirrors the ordinary Service: port 80 -> the pod's port. Named from the
            # constant because the ALB ingress rule's port is checked against it.
            "ports": [{"name": "http", "port": 80,
                       "targetPort": FIXTURE_POD_PORT, "protocol": "TCP"}],
        },
    }

    report = {
        "cloned_from": "deployment/bedrockgateway",
        "secret_env_refs_carried": {k: list(v) for k, v in sorted(present.items())},
        "secret_env_ref_count": len(present),
        "overrides_applied": overrides,
        "image": image,
        "replicas": 1,
        "labels": labels,
        "_note": (
            "The pod spec is a deep copy of the live gateway's; only the keys in "
            "overrides_applied, the image, names/labels/selectors and replica count differ. "
            "Any env var not listed here is carried over unchanged."
        ),
    }
    return deployment, service, report


# ---------------------------------------------------------------------------
# the protected worker Job
# ---------------------------------------------------------------------------
def _container_of(pod_spec: dict, name: str) -> dict:
    containers = pod_spec.get("containers") or []
    found = [c for c in containers if c.get("name") == name]
    if len(found) != 1:
        raise RenderError(
            f"the live worker template has {len(found)} container(s) named {name!r}; "
            f"found {[c.get('name') for c in containers]!r}. The gateway's verifier requires "
            "EXACTLY one, in both spec and status (workload.py: len(worker_specs) != 1), so "
            "this cannot be guessed at."
        )
    return found[0]


def assert_verifier_admissible(job: dict) -> None:
    """Would ``KubernetesWorkloadVerifier`` accept a pod from this Job template?

    Checked on the RENDERED object rather than trusted from the code that built it.
    The two are not the same claim: the overrides below are applied by index into a
    deep-copied live spec, and a reordering or a stray second env entry would be
    invisible in the override dict while being exactly what the verifier rejects.

    Only the conditions that are decidable from the template are checked here.
    ``phase == "Running"``, a non-empty ``status.podIP``, the resolved ``imageID``
    digest and ``len(containerStatuses) == 1`` are runtime facts: they are observed
    by the operator after creation (see ``10-create-fixture.sh``'s readiness wait),
    not asserted here, because asserting them from a template would be a guess
    dressed as a check.

    Raises RenderError naming the verifier condition that would refuse the pod.
    """
    spec = (job.get("spec") or {}).get("template", {}).get("spec") or {}
    namespace = (job.get("metadata") or {}).get("namespace")

    if namespace != WORKER_NAMESPACE:
        raise RenderError(
            f"the fixture worker Job is in namespace {namespace!r}, but the verifying gateway "
            f"reads AGENT_WORKER_NAMESPACE and compares it to the TokenReview username "
            f"(`system:serviceaccount:<ns>:<sa>`), so only {WORKER_NAMESPACE!r} authenticates. "
            "Relocating the worker would require changing the ordinary gateway's own config, "
            "which this evaluation must not do."
        )
    if spec.get("serviceAccountName") != WORKER_SERVICE_ACCOUNT:
        raise RenderError(
            f"the fixture worker would run as service account "
            f"{spec.get('serviceAccountName')!r}, but the verifier requires "
            f"{WORKER_SERVICE_ACCOUNT!r} (workload.py compares BOTH the TokenReview username "
            "and the pod's spec.serviceAccountName). A different SA is refused."
        )

    container = _container_of(spec, WORKER_CONTAINER)

    # `command`/`args` are refused outright, so a debug entry point cannot be
    # substituted. This is the condition that makes the fixture worker the REAL
    # program: there is no way to render a worker that both authenticates and does
    # something other than what the image does.
    for field_name in ("command", "args"):
        if container.get(field_name):
            raise RenderError(
                f"the fixture worker container sets {field_name}={container[field_name]!r}. The "
                "verifier refuses any pod that overrides the entry point, so a shell or a "
                "substituted command cannot be authenticated as a protected worker. The Job "
                "must run the image's real program."
            )

    flags = [e for e in container.get("env") or [] if e.get("name") == AUTHORITY_FLAG]
    if flags != [{"name": AUTHORITY_FLAG, "value": "true"}]:
        raise RenderError(
            f"the verifier requires the container's {AUTHORITY_FLAG} entries to be exactly "
            f'[{{"name": "{AUTHORITY_FLAG}", "value": "true"}}], and this renders {flags!r}. '
            "Note this is an equality check on the filtered list, so a duplicate entry, a "
            "valueFrom reference or the string \"True\" all refuse -- even though Kubernetes "
            "itself would accept them."
        )

    image = container.get("image") or ""
    if "@sha256:" not in image:
        raise RenderError(
            f"the fixture worker image {image!r} is not digest-pinned. The verifier compares the "
            "pod's resolved imageID digest against AGENT_WORKER_IMAGE_DIGESTS, so a tag would "
            "make the evaluated worker whatever that tag resolves to at pull time."
        )



def compose_protected_worker(live_template: dict) -> tuple[dict, str]:
    """Overlay the same protected fields Terraform uses, only on a deep copy."""
    source = (Path(__file__).resolve().parents[5] /
              "modules/agent-factory/webhook-ingress/infra/protected-worker-pod.json")
    try:
        raw = source.read_bytes()
        protected = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise RenderError(f"cannot load canonical protected worker composition: {exc}") from exc
    template = copy.deepcopy(live_template)
    spec = template["spec"]
    container = _container_of(spec, WORKER_CONTAINER)
    spec["serviceAccountName"] = protected["serviceAccountName"]
    for entry in protected["container"]["env"]:
        _set_env(container.setdefault("env", []), entry["name"], entry["value"])
    for target, key, entries in (
        (spec, "volumes", protected["volumes"]),
        (container, "volumeMounts", protected["container"]["volumeMounts"]),
    ):
        names = {entry["name"] for entry in entries}
        target[key] = [entry for entry in target.get(key, []) if entry["name"] not in names] + entries
    return template, hashlib.sha256(raw).hexdigest()

def render_worker_job(
    live_template: dict,
    *,
    run_id: str,
    nonce: str,
    name: str,
    namespace: str,
    image: str,
    control_endpoint: str,
    queue_url: str,
    approved_digests: Sequence[str],
    deadline_seconds: int = 1800,
    compose_protected: bool = False,
) -> tuple[dict, dict]:
    """Return ``(job, report)`` for a protected worker the gateway will ACCEPT.

    WHY THIS EXISTS NOW
    -------------------
    The previous revision refused ``--worker-job`` outright, on the grounds that a
    fixture worker's ``ADP_AGENT_CONTROL_ENDPOINT`` had to be an https API Gateway
    URL and every such URL resolved, by label, to the ORDINARY gateway's pods. That
    was true when it was written. #5836 built a fixture-only edge -- its own REST
    API, stage, resource policy and internal ALB -- whose ``worker_control_endpoint``
    output is an https execute-api URL that terminates at the FIXTURE gateway. The
    constraint that justified the refusal is gone, so the refusal is too.

    WHAT IT IS BUILT FROM, AND WHY NOT BY HAND
    ------------------------------------------
    ``live_template`` is the real worker pod template -- taken from the live
    ScaledJob's ``jobTargetRef.template`` (see ``10-create-fixture.sh``), which is
    what KEDA actually instantiates. It is DEEP-COPIED and a small explicit set of
    keys is overridden, for the same reason ``render_gateway`` does: hand-listing a
    pod spec is how the gateway fixture lost nine secret references, and the worker
    spec is larger. A copy cannot lose a mount, a projected volume or a securityContext.

    WHAT IS OVERRIDDEN (and nothing else)
    -------------------------------------
    * ``ADP_AGENT_CONTROL_ENDPOINT`` -- #5836's fixture edge, so the bootstrap this
      worker performs reaches the fixture gateway and not live traffic. THE point of
      the whole change.
    * ``QUEUE_URL`` / ``ADP_RUN_TASK_QUEUE_URL`` / ``AGENT_DISPATCH_QUEUE_URL`` --
      the run's own dedicated FIFO queue, so no fixture message can reach the shared
      submit queue in either direction.
    * ``ADP_AGENT_AUTHORITY_ENABLED=true`` -- the verifier requires it, and requires
      it to be the ONLY entry of that name.
    * the image, pinned to a digest that must ALREADY be on the approved list. This
      fixture cannot introduce a new approved digest: that allowlist is
      Terraform-gated (``agent_authority_worker_image_digests``) and widening it is
      root's change, not an evaluation's.
    * a downwardAPI projection of ``metadata.uid``/name/namespace, so the process can
      prove WHICH pod it is without any cluster read permission. The protected SA
      cannot `get` or `list` pods -- root verified this -- and it must stay that way,
      so nothing here calls kubectl from inside the worker and no RBAC is granted.
    * labels, name, and a bounded ``activeDeadlineSeconds`` so a fixture worker
      cannot outlive the evaluation.

    With compose_protected, the token, mounts, paths and service account are first
    overlaid from the same protected-worker-pod.json consumed by Terraform. Only
    the disposable copy is changed; ordinary authority activation is unnecessary.

    Everything else -- the projected bootstrap token volume and its audience, the
    verification-keys ConfigMap mount, POD_IP, the securityContext, resources -- is
    carried over from the live template unchanged, and ASSERTED present below.
    """
    protected_source_sha256 = None
    if compose_protected:
        live_template, protected_source_sha256 = compose_protected_worker(live_template)
    try:
        pod_spec = copy.deepcopy(live_template["spec"])
        live_meta = live_template.get("metadata") or {}
    except (KeyError, TypeError) as exc:
        raise RenderError(
            "could not read the live worker pod template. The fixture worker must be a copy of "
            "the real composition -- a hand-assembled one would silently omit the projected "
            f"token volume or a mount and then fail verification for the wrong reason: {exc}"
        ) from exc

    container = _container_of(pod_spec, WORKER_CONTAINER)

    # --- the image must be on the list the GATEWAY will check against ----------
    # Pinned to some digest is not pinned to an APPROVED digest. The verifier
    # compares the resolved imageID against AGENT_WORKER_IMAGE_DIGESTS, so a
    # digest-pinned image that is not on that list is refused at bootstrap --
    # after the fixture exists and the operator has spent the setup.
    if "@sha256:" not in image:
        raise RenderError(f"fixture worker image {image!r} is not digest-pinned")
    digest = "sha256:" + image.split("@sha256:", 1)[1].strip()
    approved = [d.strip() for d in approved_digests if d and d.strip()]
    if not approved:
        raise RenderError(
            "the approved worker image digest list is empty, so no image can be shown to be "
            "one the gateway will accept. It is published by Terraform as "
            "AGENT_WORKER_IMAGE_DIGESTS on the gateway's adp-worker-authority-config "
            "ConfigMap; read it from there rather than proceeding with an unchecked digest."
        )
    if digest not in approved:
        raise RenderError(
            f"the fixture worker image digest {digest} is not on the approved list the verifying "
            f"gateway checks ({len(approved)} entry/entries). The pod would be refused at "
            "bootstrap. Adding a digest to that allowlist is a Terraform change "
            "(agent_authority_worker_image_digests) and is root's to make -- an evaluation must "
            "not widen the set of images that can hold protected authority."
        )
    container["image"] = image

    # --- the carried-over identity plumbing, ASSERTED present -----------------
    # Each of these is what makes the bootstrap possible at all. A fixture missing
    # one fails verification in a way that reads as the feature being broken, so it
    # is checked here, against the copy, before creation.
    volumes = pod_spec.get("volumes") or []
    by_name = {v.get("name"): v for v in volumes if v.get("name")}

    projected = by_name.get(WORKLOAD_VOLUME)
    sources = (projected or {}).get("projected", {}).get("sources") or []
    tokens = [s["serviceAccountToken"] for s in sources if s.get("serviceAccountToken")]
    if len(tokens) != 1 or tokens[0].get("audience") != BOOTSTRAP_AUDIENCE:
        raise RenderError(
            f"the live worker template does not project exactly one serviceAccountToken for "
            f"audience {BOOTSTRAP_AUDIENCE!r} on volume {WORKLOAD_VOLUME!r} (found "
            f"{[t.get('audience') for t in tokens]!r}). The gateway calls TokenReview with that "
            "audience and rejects anything else, so without it the worker cannot authenticate "
            "at all. This means agent authority is not enabled on the live cluster; enabling it "
            "is a Terraform change, not something to synthesise here."
        )
    if tokens[0].get("path") != "token":
        raise RenderError(
            f"the projected bootstrap token is written to {tokens[0].get('path')!r}, but the "
            f"worker reads {WORKLOAD_TOKEN_PATH}. A token the process cannot find is the same "
            "as no token."
        )
    if CONTROL_KEYS_VOLUME not in by_name:
        raise RenderError(
            f"the live worker template has no {CONTROL_KEYS_VOLUME!r} volume, so the worker "
            "would have no keys to verify the gateway's control envelopes against and would "
            "refuse every command. The pause evidence would be indistinguishable from a pause "
            "that does not work."
        )

    mounts = {m.get("mountPath"): m for m in container.get("volumeMounts") or []}
    for required in (WORKLOAD_TOKEN_DIR, CONTROL_KEYS_DIR):
        if required not in mounts:
            raise RenderError(
                f"the live worker container does not mount {required}, so the material projected "
                "for it is not visible to the process. Carried from the live template rather "
                "than added here: if it is absent there, the live worker does not have it "
                "either and that is the thing to fix."
            )
        if not mounts[required].get("readOnly"):
            raise RenderError(
                f"the worker's {required} mount is not readOnly. A writable credential mount "
                "lets the process replace its own identity material, which would make every "
                "downstream identity claim self-asserted."
            )

    env = container.setdefault("env", [])
    inline = {e["name"]: e for e in env if "name" in e}
    for required in ("POD_IP",):
        if required not in inline:
            raise RenderError(
                f"the live worker container does not set {required}, which the control listener "
                "binds to explicitly. Without it the worker logs an error and starts NO "
                "listener, so there would be nothing for the pause evidence to measure."
            )
    if (inline["POD_IP"].get("valueFrom") or {}).get("fieldRef", {}).get("fieldPath") != "status.podIP":
        raise RenderError(
            "POD_IP is not projected from status.podIP via the downwardAPI. A literal or "
            "env-derived address is a claim about where this pod is; only the API server's "
            "value is an observation."
        )
    for required in (WORKLOAD_TOKEN_PATH, CONTROL_KEYS_PATH):
        pointers = [e.get("name") for e in env if e.get("value") == required]
        if not pointers:
            raise RenderError(
                f"no env var points the worker at {required}. The material is mounted but "
                "nothing tells the process where to read it, so it would behave as though the "
                "material were absent."
            )

    # --- the deliberate overrides --------------------------------------------
    overrides = {
        # THE change #5836 made possible: the fixture's own edge. Everything else
        # here is bookkeeping around this one value.
        "ADP_AGENT_CONTROL_ENDPOINT": control_endpoint,
        # Model calls must reach this same fixture gateway and its authority
        # configuration; the live template points at the ordinary /agent edge.
        "SIGV4_PROXY_TARGET": control_endpoint.removesuffix("/internal/v1/agent") + "/agent",
        # Required by the verifier, and required to be the sole entry of this name.
        AUTHORITY_FLAG: "true",
        # Every queue reference, so no fixture traffic reaches the shared submit
        # queue and no shared traffic reaches the fixture.
        "QUEUE_URL": queue_url,
        "ADP_RUN_TASK_QUEUE_URL": queue_url,
        "AGENT_DISPATCH_QUEUE_URL": queue_url,
        # The listener the gateway dials. Asserted rather than assumed below.
        "FEATURE_AGENT_CONTROL_ENABLED": "true",
        "ADP_CONTROL_PORT": CONTROL_PORT,
        # Where this pod's own identity files land, so the experiment can prove
        # which pod it is without reading the cluster.
        "W2_POD_IDENTITY_DIR": POD_IDENTITY_DIR,
        "W2_FIXTURE_RUN_ID": run_id,
    }
    for key, value in overrides.items():
        _set_env(env, key, value)

    if not control_endpoint.startswith("https://"):
        raise RenderError(
            f"ADP_AGENT_CONTROL_ENDPOINT {control_endpoint!r} is not https. run_identity.py "
            "rejects any other scheme before attempting a bootstrap, so an in-cluster "
            "http://…:8080 address cannot be used even deliberately. Use #5836's "
            "worker_control_endpoint output."
        )

    # --- the pod's own unforgeable identity, projected -----------------------
    # Added rather than carried: the live worker has no reason to need it. This is
    # the seam that replaces the in-worker `kubectl get pod` the protected SA is
    # denied, and the `service-account.name` file no mount provides.
    pod_spec["volumes"] = [v for v in volumes if v.get("name") != POD_IDENTITY_VOLUME] + [{
        "name": POD_IDENTITY_VOLUME,
        "downwardAPI": {
            # Only the five fieldRefs a downwardAPI volume supports. Notably NOT
            # spec.serviceAccountName, which is unsupported -- the operator reads
            # that from the API server and binds it to the uid below instead.
            "items": [{"path": path, "fieldRef": {"fieldPath": field}}
                      for path, field in POD_IDENTITY_FIELDS],
        },
    }]
    container["volumeMounts"] = [
        m for m in container.get("volumeMounts") or [] if m.get("mountPath") != POD_IDENTITY_DIR
    ] + [{"name": POD_IDENTITY_VOLUME, "mountPath": POD_IDENTITY_DIR, "readOnly": True}]

    labels = {
        # The fixture worker's OWN labels. Deliberately NOT
        # `app.kubernetes.io/name: agent-scaledjob`: that label is what the
        # ordinary agent-control-listener-ingress and agent-scaledjob-egress
        # policies select, and wearing it would put this pod inside the ordinary
        # allowlists. The fixture gets its own policies instead (render_policies),
        # which is the same reasoning as not relabelling the fixture gateway.
        "app": name,
        "adp.io/w2-fixture": run_id,
        "adp.io/w2-nonce": nonce,
    }

    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": dict(labels)},
        "spec": {
            "parallelism": 1,
            "completions": 1,
            # No retries. A replacement pod is a DIFFERENT pod uid, and the whole
            # identity binding is to one uid: a silent retry would leave the
            # operator's recorded identity describing a pod that no longer exists
            # while a new one runs unobserved.
            "backoffLimit": 0,
            # A bound on how long a control-enabled workload can exist at all.
            # The ordinary worker's deadline is hours; an evaluation fixture that
            # outlives the evaluation is exactly what the cleanup ledger is for,
            # and this is the backstop for the case where cleanup never runs.
            "activeDeadlineSeconds": int(deadline_seconds),
            "template": {
                "metadata": {
                    "labels": dict(labels),
                    "annotations": dict(live_meta.get("annotations") or {}),
                },
                "spec": pod_spec,
            },
        },
    }

    # Checked against the rendered object, not against the intent above.
    assert_verifier_admissible(job)

    report = {
        "cloned_from": "scaledjob/agent-scaledjob jobTargetRef.template",
        "image": image,
        "image_digest": digest,
        "digest_on_approved_list": True,
        "approved_digest_count": len(approved),
        "overrides_applied": dict(overrides),
        "labels": labels,
        "active_deadline_seconds": int(deadline_seconds),
        "backoff_limit": 0,
        "verifier_conditions_asserted": [
            f"namespace == {WORKER_NAMESPACE}",
            f"serviceAccountName == {WORKER_SERVICE_ACCOUNT}",
            f"exactly one container named {WORKER_CONTAINER}",
            "no command and no args (the image's real entry point)",
            f'{AUTHORITY_FLAG} env is exactly [{{"name": ..., "value": "true"}}]',
            "image is digest-pinned AND the digest is on AGENT_WORKER_IMAGE_DIGESTS",
        ],
        "verifier_conditions_observed_after_creation": [
            "status.phase == Running",
            "non-empty status.podIP",
            "exactly one containerStatus named agent-worker, state running",
            "containerStatus.imageID digest on the approved list",
            "no metadata.deletionTimestamp",
        ],
        "carried_over": {
            "projected_token_audience": BOOTSTRAP_AUDIENCE,
            "workload_token_mount": WORKLOAD_TOKEN_DIR,
            "control_keys_mount": CONTROL_KEYS_DIR,
            "pod_ip_from": "status.podIP (downwardAPI)",
        },
        "pod_identity_projection": {
            "mount": POD_IDENTITY_DIR,
            "files": {path: field for path, field in POD_IDENTITY_FIELDS},
            "_why": (
                "The protected service account cannot get or list pods, and that boundary is "
                "preserved rather than widened. A downwardAPI volume can project "
                "metadata.uid/name/namespace and nothing else useful here -- notably NOT "
                "spec.serviceAccountName. So the container proves which pod uid it is, and the "
                "operator, which CAN read the API server, records what that uid's pod actually "
                "is (service account, container, resolved image digest)."
            ),
        },
        "_note": (
            "The pod spec is a deep copy of the live worker template; only overrides_applied, "
            "the image, the labels/name and the added pod-identity projection differ. The "
            "projected bootstrap token, its audience, the verification-keys mount and the "
            "securityContext are carried over unchanged and asserted present."
        ),
    }
    if protected_source_sha256:
        report["protected_composition_sha256"] = protected_source_sha256
    return job, report


# ---------------------------------------------------------------------------
# network policies
# ---------------------------------------------------------------------------
def render_policies(
    *,
    run_id: str,
    nonce: str,
    policy_name: str,
    gateway_namespace: str,
    agent_namespace: str,
    gateway_label: str,
    worker_label: str,
    alb_source: dict | None = None,
) -> list[dict]:
    """Fixture-scoped policies that permit the flows under test and nothing wider.

    WHAT THIS FIXES
    ---------------
    The published policy allowed egress to ports 53 and 443 only. That blocks:

      * the worker control listener on 8770 -- the exact flow W2-03/04/05 measure;
      * PostgreSQL on 5432, so the gateway cannot start;
      * in-cluster DNS/service traffic on other ports.

    A policy that blocks the behaviour under test does not produce a failed
    check, it produces a meaningless one.

    It also selected the fixture by a unique label, while the ordinary worker's
    ingress admits only pods labelled as the ordinary gateway -- so the fixture
    gateway could not reach the worker at all. Rather than relabel the fixture to
    impersonate the ordinary gateway (which would widen the ordinary allowlist's
    blast radius), a dedicated ingress policy on the fixture worker admits the
    fixture gateway explicitly.

    THE ALB SOURCE (`alb_source`)
    -----------------------------
    The ingress rule above admits callers by `namespaceSelector`, which is correct for
    a purely in-cluster harness and cannot work for #5836's fixture edge: with
    `target-type: ip` the ALB connects from its OWN elastic network interfaces, which
    belong to the load balancer and to no pod and no namespace. No selector matches it
    at any width.

    So when the fixture is reached through the edge, one additional `ipBlock` rule is
    required, admitting exactly this run's ALB interfaces on the CONTAINER port. This
    function cannot derive that itself -- the addresses belong to a load balancer that
    did not exist when the policy was first rendered -- so #5836 observes them and
    publishes them, and `edge_receipt.resolve_alb_policy_source` binds the document to
    this run before it arrives here. This function renders; it does not decide whether
    the value is trustworthy.

    `alb_source=None` renders the in-cluster-only policy, unchanged. That is the
    correct shape for the gateway stage, which necessarily runs BEFORE the edge exists
    -- and it is why this is a second rendering rather than a parameter the first
    rendering could have taken.

    Only the FIXTURE gateway's policy gains the rule. The ordinary gateway's policy is
    never touched by this tooling, and widening it for a fixture would put production's
    blast radius on the line for a measurement.
    """
    fixture_selector = {"matchLabels": {"adp.io/w2-fixture": run_id}}
    common_labels = {"adp.io/w2-fixture": run_id, "adp.io/w2-nonce": nonce}

    # The edge's ingress rule, built only when the bound source document was supplied.
    # A separate rule rather than another `from` entry on the in-cluster rule: mixing
    # an ipBlock into that peer list would pair the ALB's addresses with the existing
    # namespaceSelectors under one `ports` clause, which reads as wider than it is and
    # would silently follow any future change to that rule's ports.
    alb_rules: list[dict] = []
    if alb_source is not None:
        cidrs = alb_source.get("cidrs") or []
        port = alb_source.get("container_port")
        if not cidrs:
            raise RenderError(
                "the ALB policy source carries no addresses, so the rule it would render "
                "admits nothing. An empty allowance denies the edge, and that denial looks "
                "exactly like the protected worker failing its bootstrap -- the conclusion "
                "this fixture exists to establish or refute. Refusing to render instead."
            )
        # The port this function RENDERS, not merely a usable one. The resolver checks
        # the same identity against the same constant; this is the second half of that
        # pairing, so a caller assembling an alb_source by hand cannot bypass it.
        # Type checked as well as compared: `8080.0 == 8080` and `True == 1` are both
        # true in Python, and a float in a NetworkPolicy port is rejected by the API
        # server -- mid-run, after the gateway exists.
        if not isinstance(port, int) or isinstance(port, bool) or port != FIXTURE_POD_PORT:
            raise RenderError(
                f"the ALB policy source's container_port is {port!r}, but this function "
                f"renders the fixture Service and its in-cluster ingress rule on "
                f"{FIXTURE_POD_PORT}. A rule naming any other port admits the load balancer "
                "to a port nothing serves, which denies the flow under test while reading as "
                "configured. Refused rather than defaulted: the denial is indistinguishable "
                "from the protected worker failing its bootstrap."
            )
        alb_rules.append({
            "from": [{"ipBlock": {"cidr": cidr}} for cidr in cidrs],
            "ports": [{"protocol": "TCP", "port": port}],
        })

    gateway_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": policy_name,
            "namespace": gateway_namespace,
            "labels": dict(common_labels),
        },
        "spec": {
            "podSelector": {"matchLabels": {"app": gateway_label}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    # In-cluster callers only. No external exposure is created by
                    # this run; the harness reaches the fixture from inside.
                    "from": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": gateway_namespace}}},
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": agent_namespace}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": FIXTURE_POD_PORT}],
                },
                # The fixture edge's ALB, when this run has one. Empty otherwise, so
                # the gateway-stage policy is byte-identical to before.
                *alb_rules,
            ],
            "egress": [
                # DNS.
                {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                # AWS APIs, Bedrock, ECR, STS.
                {"ports": [{"protocol": "TCP", "port": 443}]},
                # PostgreSQL. Absent from the published policy, so the fixture
                # gateway could not have reached its database.
                {"ports": [{"protocol": "TCP", "port": 5432}]},
                # Redis/Valkey over TLS.
                {"ports": [{"protocol": "TCP", "port": 6379}]},
                # THE FLOW UNDER TEST: the worker's control listener. Restricted
                # to this run's worker pods so it is not a general allowance.
                {
                    "to": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": agent_namespace}},
                         "podSelector": fixture_selector},
                    ],
                    "ports": [{"protocol": "TCP", "port": int(CONTROL_PORT)}],
                },
            ],
        },
    }

    worker_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{policy_name}-worker",
            "namespace": agent_namespace,
            "labels": dict(common_labels),
        },
        "spec": {
            # Only this run's worker pods. The ordinary worker's own policy is
            # untouched.
            "podSelector": fixture_selector,
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    # Admit the FIXTURE gateway by its own label, rather than
                    # relabelling the fixture as the ordinary gateway.
                    "from": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": gateway_namespace}},
                         "podSelector": {"matchLabels": {"app": gateway_label}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": int(CONTROL_PORT)}],
                }
            ],
            "egress": [
                {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
                {"ports": [{"protocol": "TCP", "port": 443}]},
                {
                    # Back to the fixture gateway, for bootstrap and task pull.
                    "to": [
                        {"namespaceSelector": {"matchLabels": {
                            "kubernetes.io/metadata.name": gateway_namespace}},
                         "podSelector": {"matchLabels": {"app": gateway_label}}},
                    ],
                    "ports": [{"protocol": "TCP", "port": 8080}],
                },
            ],
        },
    }
    return [gateway_policy, worker_policy]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-deployment", required=True,
                        help="path to `kubectl get deploy bedrockgateway -o json` output")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--agent-namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--cluster-pod-cidrs", default=None,
                        help="verified private pod CIDRs, comma-separated; required unless inline on the live Deployment")
    parser.add_argument("--fixture-worker-digest", default=None,
                        help="operator-reviewed digest approved only by this fixture gateway")
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--policy-name", required=True)
    parser.add_argument("--out-dir", required=True)
    # The worker Job is rendered only when all four of its inputs are supplied.
    # All-or-nothing rather than defaulted: a default endpoint would point a
    # control-enabled worker somewhere nobody chose, and a default digest list
    # would make the approved-digest check vacuous.
    parser.add_argument("--worker-template", default=None,
                        help="path to the live worker pod template JSON (the ScaledJob's "
                             "jobTargetRef.template). Supplying it renders the protected "
                             "worker Job.")
    parser.add_argument("--worker-name", default=None)
    parser.add_argument("--worker-image", default=None,
                        help="digest-pinned worker image; the digest must already be on "
                             "--approved-worker-digests")
    parser.add_argument("--worker-control-endpoint", default=None,
                        help="#5836's worker_control_endpoint output: the fixture-only edge URL "
                             "this worker bootstraps against")
    parser.add_argument("--approved-worker-digests", default=None,
                        help="comma-separated AGENT_WORKER_IMAGE_DIGESTS, as the verifying "
                             "gateway has them")
    parser.add_argument("--worker-deadline-seconds", type=int, default=1800)
    # The edge receipt, when the fixture is reached through #5836's ALB. The whole
    # `terraform output -json` document: `--edge-receipt` rather than a list of
    # addresses, so the addresses cannot be typed in by hand and so the freshness and
    # width checks in edge_receipt.resolve_alb_policy_source are not optional.
    parser.add_argument("--edge-receipt", default=None,
                        help="path to #5836's full `terraform output -json` document. When "
                             "given, the fixture gateway policy admits that run's ALB "
                             "interfaces on the container port. Requires every --edge-* flag.")
    parser.add_argument("--edge-account-id", default=None,
                        help="the account this run is bound to; the receipt and the ALB ARN "
                             "must both name it")
    parser.add_argument("--edge-region", default=None,
                        help="the region this run is bound to; the receipt and the ALB ARN "
                             "must both name it")
    parser.add_argument("--edge-environment", default=None,
                        help="this run's environment, which is also #5836's per-run state key "
                             "component and the API Gateway stage name")
    parser.add_argument("--edge-alb-arn", default=None,
                        help="the fixture ALB this run's ledger records. The receipt's "
                             "addresses must have been read from THIS load balancer: a "
                             "matching nonce alone would admit addresses read from any ALB "
                             "in the account.")
    args = parser.parse_args(argv)

    from pathlib import Path

    live = json.loads(Path(args.live_deployment).read_text(encoding="utf-8"))
    out = Path(args.out_dir)
    # Created only once every input has been validated (below). An output directory
    # that exists after a refusal is a hazard rather than a convenience: the shell
    # creates objects FROM this directory, so a half-populated or empty one left by a
    # refused render is something a later step, or a retry with different flags, can
    # apply. The refusal must leave nothing to act on.

    worker_inputs = {
        "--worker-template": args.worker_template,
        "--worker-name": args.worker_name,
        "--worker-image": args.worker_image,
        "--worker-control-endpoint": args.worker_control_endpoint,
        "--approved-worker-digests": args.approved_worker_digests,
    }
    want_worker = any(worker_inputs.values())
    missing_worker = sorted(flag for flag, value in worker_inputs.items() if not value)
    if want_worker and missing_worker:
        print(f"FAIL: rendering the protected worker needs all of {', '.join(missing_worker)}. "
              "Partial input is refused rather than defaulted: a defaulted endpoint would point "
              "a control-enabled worker at something nobody chose, and a defaulted digest list "
              "would make the approved-image check vacuous.", file=sys.stderr)
        return 1

    # The ALB source, resolved BEFORE anything is rendered. All-or-nothing across the
    # whole binding: a receipt consumed without its account, region, environment and
    # ALB ARN is a receipt whose only established property is that SOME run's edge
    # produced it.
    #
    # There is deliberately NO `--run-nonce-expect` flag. The first revision had one,
    # and root found that `--nonce=ffff… --run-nonce-expect=a1b2…` with a receipt for
    # a1b2… was accepted: the policy labels were stamped with one run's nonce while its
    # ipBlock rule came from another run's load balancer. Two names for one fact let
    # them disagree. The expectation is now `--nonce` itself -- the same value the
    # labels, the ledger and the cleanup gate use -- so the rule and the object that
    # carries it cannot belong to different runs.
    alb_source = None
    edge_flags = (
        ("--edge-receipt", args.edge_receipt),
        ("--edge-account-id", args.edge_account_id),
        ("--edge-region", args.edge_region),
        ("--edge-environment", args.edge_environment),
        ("--edge-alb-arn", args.edge_alb_arn),
    )
    if any(value for _, value in edge_flags):
        missing_edge = [flag for flag, value in edge_flags if not value]
        if missing_edge:
            print(f"FAIL: admitting the fixture edge's ALB needs all of "
                  f"{', '.join(flag for flag, _ in edge_flags)}; missing "
                  f"{', '.join(missing_edge)}. None of them is decoration: the account and "
                  "region bind the receipt to this run's edge, the ARN says WHICH load "
                  "balancer the addresses were read from (a matching nonce alone would admit "
                  "addresses from any ALB in the account), and the environment is the API "
                  "Gateway stage. A missing expectation must not waive the check it exists "
                  "for.", file=sys.stderr)
            return 1
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import edge_receipt  # noqa: PLC0415 -- optional dependency of this path only

        try:
            receipt = edge_receipt.load_receipt(args.edge_receipt)
            alb_source = edge_receipt.resolve_alb_policy_source(
                receipt,
                # THIS run's nonce -- the one the labels below are rendered from.
                run_nonce=args.nonce,
                account_id=args.edge_account_id,
                region=args.edge_region,
                environment=args.edge_environment,
                expected_alb_arn=args.edge_alb_arn,
                # The port the fixture Service is rendered to target. Passed rather
                # than left to the resolver's default so the two cannot drift.
                expected_container_port=FIXTURE_POD_PORT,
            )
        except edge_receipt.EdgeReceiptError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    job = None
    worker_report = None
    try:
        deployment, service, report = render_gateway(
            live, run_id=args.run_id, nonce=args.nonce, name=args.name,
            namespace=args.namespace, image=args.image, queue_url=args.queue_url,
            fixture_worker_digest=args.fixture_worker_digest,
            cluster_pod_cidrs=args.cluster_pod_cidrs,
        )
        policies = render_policies(
            run_id=args.run_id, nonce=args.nonce, policy_name=args.policy_name,
            gateway_namespace=args.namespace, agent_namespace=args.agent_namespace,
            gateway_label=args.name, worker_label=args.name,
            alb_source=alb_source,
        )
        if want_worker:
            template = json.loads(Path(args.worker_template).read_text(encoding="utf-8"))
            job, worker_report = render_worker_job(
                template,
                run_id=args.run_id, nonce=args.nonce, name=args.worker_name,
                namespace=args.agent_namespace, image=args.worker_image,
                control_endpoint=args.worker_control_endpoint,
                queue_url=args.queue_url,
                approved_digests=args.approved_worker_digests.split(","),
                deadline_seconds=args.worker_deadline_seconds,
                compose_protected=args.fixture_worker_digest is not None,
            )
    except RenderError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # Everything is rendered and validated; only now is there anything to write.
    out.mkdir(parents=True, exist_ok=True)
    (out / "00-policies.json").write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": policies}, indent=2) + "\n")
    (out / "10-gateway.json").write_text(
        json.dumps({"apiVersion": "v1", "kind": "List", "items": [deployment, service]},
                   indent=2) + "\n")
    if job is not None:
        (out / "20-worker.json").write_text(
            json.dumps({"apiVersion": "v1", "kind": "List", "items": [job]}, indent=2) + "\n")
        report["worker"] = worker_report
    (out / "composition-report.json").write_text(json.dumps(report, indent=2) + "\n")

    print(f"ok   rendered fixture from the live composition into {out}")
    print(f"     {report['secret_env_ref_count']} secret-backed env refs carried over")
    for key, value in sorted(report["overrides_applied"].items()):
        shown = value if len(value) < 60 else value[:57] + "..."
        print(f"     override {key}={shown}")
    if worker_report is not None:
        print(f"ok   rendered the protected worker Job {args.worker_name}")
        print(f"     image digest {worker_report['image_digest']} is on the approved list "
              f"({worker_report['approved_digest_count']} entries)")
        for condition in worker_report["verifier_conditions_asserted"]:
            print(f"     verifier condition asserted: {condition}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
