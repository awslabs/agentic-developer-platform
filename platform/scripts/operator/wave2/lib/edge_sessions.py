#!/usr/bin/env python3
"""Human-session verification against the fixture, over a KNOWN transport.

Issue #3968, root's blocker 7: "Deliver actual human-session/trusted-edge fixture
routing and an executable orchestration path. Verify auth-NONE human bearer versus
AWS_IAM internal paths correctly; do not fake caller headers. Sessions must be
real."

WHY THIS MODULE EXISTS AT ALL
----------------------------
The first attempt at blocker 7 recorded ``fixture_scoped: true`` for the human
bearer path as a LITERAL in the artifact. That is the same defect the rest of this
PR exists to remove: a claim about what was measured, written down without
measuring it. The script it sat in sends its requests to whatever ``--gateway-url``
it was handed. That URL is usually the ordinary gateway -- an operator host cannot
route to a fixture ClusterIP at all -- so the artifact asserted a fixture
measurement while exercising the ordinary deployment.

So ``fixture_scoped`` is DERIVED here, from the transport that actually carried the
request, and the only transport that earns ``True`` is an exec into a pod verified
to belong to this run's fixture. An external URL cannot earn it, because nothing
about a URL demonstrates what it terminates at.

THE TWO PATHS, AND WHAT THIS SCRIPT MEASURES
--------------------------------------------
* Human bearer (auth-NONE routes). API Gateway BLANKS ``X-Caller-Identity`` and
  the provenance header on these routes and FastAPI authenticates the JWT. A
  request that asserts no identity header skips the provenance branch entirely
  (``src/auth/dependencies.py:207``), so a real session works against the fixture
  over its ClusterIP with nothing fabricated. **Fixture-scoped is achievable**,
  and needs no edge at all.

* SigV4 internal (AWS_IAM routes). The identity header is written from
  ``context.identity.userArn`` and a provenance secret is injected. On the
  ORDINARY edge those routes are ``http_proxy``/VPC_LINK to one ALB ARN
  terminating at Service ``bedrockgateway`` -> pods labelled
  ``app=bedrockgateway``, so genuine-provenance traffic reaches the ORDINARY pods
  by label. THIS SCRIPT measures that edge, so it records an observation of the
  deployed configuration and marks ``fixture_scoped: false``.

  That flag says which edge a measurement used; it is not a claim the path is
  unmeasurable. #5836's fixture edge (a SEPARATE REST API whose internal plane
  backs the fixture's own ClusterIP Service) makes the same logical path
  fixture-scoped, and that is the endpoint ``10-create-fixture.sh --worker-job``
  requires. See FIXTURE-ROUTING-CONSTRAINT.md.

Forging the pair to close that gap does not work even on its own terms: a failed
provenance check is a 403 (``auth_deps.py:181-194``), so a fabricated header
proves nothing. It is excluded by the task and by the code.

TOKENS
------
Token values are passed to the in-pod interpreter on **stdin**, never as argv.
Process arguments are world-readable via ``/proc``, so a token in argv leaks to
every process in that pod. Only env var NAMES ever reach an artifact.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence

# Runs inside the fixture pod. Reads the bearer token from stdin so it never
# appears in argv (see module docstring), issues ONE request, and prints a single
# JSON line. Dependency-free: the gateway image has python3 but no guaranteed
# curl.
#
# It deliberately sends ONLY Authorization. No X-Caller-Identity and no
# X-Adp-Edge-Provenance: on this path the edge blanks both, and a fabricated pair
# would be rejected with a 403 rather than granting anything.
IN_POD_SESSION = r"""
import json, sys, urllib.error, urllib.request
url = sys.argv[1]
token = sys.stdin.read().strip()
out = {"endpoint_url": url, "headers_sent": ["Authorization"]}
if not token:
    out["error"] = "no token on stdin"
    print(json.dumps(out)); sys.exit(0)
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        out["status"] = resp.status
        out["body"] = json.loads(resp.read().decode())
except urllib.error.HTTPError as exc:
    out["status"] = exc.code
    out["error"] = exc.read().decode(errors="replace")[:300]
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""

# Identity fields copied out of a response. An allowlist, not a copy of the whole
# body: a gateway response may carry budget/billing detail that has no business in
# an evidence artifact, and nothing bearer-shaped must ever be recorded.
IDENTITY_FIELDS = ("user_id", "org_id", "tenant_id", "email", "github_login")


class TransportKind(str, Enum):
    """How a session request reached the gateway."""

    # `kubectl exec` into a pod verified to carry this run's fixture label.
    FIXTURE_POD_EXEC = "fixture_pod_exec"
    # An HTTP(S) URL from the operator host. Terminates wherever it terminates;
    # this is not evidence about which deployment served it.
    EXTERNAL_URL = "external_url"


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str], str], CommandResult]


@dataclass
class Transport:
    """The verified path a session request will take.

    `fixture_verified` is NOT a caller-supplied assertion. It is set by
    `verify_fixture_pod`, which reads the pod's own labels back from the API
    server and requires this run's id. A caller cannot hand in `True`.
    """

    kind: TransportKind
    fixture_verified: bool
    description: str
    pod: str | None = None
    namespace: str | None = None
    base_url: str = ""
    evidence: dict[str, Any] | None = None


def verify_fixture_pod(
    run: Runner, *, pod: str, namespace: str, run_id: str, label: str = "adp.io/w2-fixture"
) -> dict[str, Any]:
    """Confirm a pod really is this run's fixture, from the API server.

    Returning the pod's uid alongside the label is deliberate: the uid is what
    makes the observation about a specific object rather than a name that could
    have been recreated between steps.

    A `kubectl get` failure is NOT treated as "not the fixture" in a way that
    quietly downgrades the transport: it is reported with `error` set, and
    `verified` false. The distinction matters because an unreachable API server
    and a genuinely mislabelled pod need different operator responses.
    """
    # `-o json` rather than a jsonpath: the label key contains a dot and a slash
    # (`adp.io/w2-fixture`), and jsonpath needs the dot backslash-escaped. Building
    # that escape by string substitution produced an unreviewable expression, and a
    # jsonpath that silently matches nothing yields an EMPTY label -- which would
    # then compare unequal and report a correctly-labelled fixture as foreign.
    # Parsing the document has no such failure mode.
    result = run(["kubectl", "get", "pod", pod, "-n", namespace, "-o", "json"], "")
    if result.returncode != 0:
        return {
            "verified": False,
            "pod": pod,
            "namespace": namespace,
            "error": (result.stderr or result.stdout or "").strip()[:300]
            or f"kubectl get exited {result.returncode}",
            "reason": "could not read the pod back from the API server, so it is not known to be "
                      "this run's fixture. An unreadable pod is not a verified one.",
        }
    try:
        doc = json.loads(result.stdout or "")
    except ValueError:
        return {
            "verified": False, "pod": pod, "namespace": namespace,
            "error": f"unparseable kubectl output: {(result.stdout or '')[:200]!r}",
            "reason": "the pod document could not be parsed, so nothing about this pod was "
                      "actually observed.",
        }

    metadata = doc.get("metadata") or {}
    observed_label = (metadata.get("labels") or {}).get(label, "")
    uid = metadata.get("uid") or ""
    phase = (doc.get("status") or {}).get("phase") or ""

    if observed_label != run_id:
        return {
            "verified": False,
            "pod": pod,
            "namespace": namespace,
            "observed_fixture_label": observed_label or None,
            "expected_fixture_label": run_id,
            "reason": "this pod does not carry this run's fixture label. Exercising it would "
                      "measure some other pod -- very likely the ordinary deployment -- while "
                      "recording the result as a fixture measurement.",
        }
    if not uid:
        return {
            "verified": False, "pod": pod, "namespace": namespace,
            "reason": "the pod returned no metadata.uid, so there is no evidence identifying the "
                      "specific object that was probed.",
        }
    if phase != "Running":
        return {
            "verified": False, "pod": pod, "namespace": namespace, "phase": phase,
            "reason": f"pod phase is {phase!r}, not Running; a session cannot be served by it.",
        }
    return {
        "verified": True, "pod": pod, "namespace": namespace,
        "fixture_label": observed_label, "uid": uid, "phase": phase,
    }


def choose_transport(
    run: Runner,
    *,
    run_id: str,
    fixture_pod: str | None,
    fixture_namespace: str,
    gateway_url: str,
    fixture_port: int = 8080,
) -> Transport:
    """Pick the transport, and let only a verified fixture pod claim fixture scope.

    There is no fallback from a FAILED fixture verification to the external URL.
    If the operator asked for a fixture measurement and the pod does not check
    out, silently measuring something else is precisely the substitution this
    module exists to prevent -- the transport is returned unverified so the caller
    refuses.
    """
    if fixture_pod:
        evidence = verify_fixture_pod(
            run, pod=fixture_pod, namespace=fixture_namespace, run_id=run_id
        )
        return Transport(
            kind=TransportKind.FIXTURE_POD_EXEC,
            fixture_verified=bool(evidence.get("verified")),
            description=(
                f"kubectl exec into {fixture_namespace}/{fixture_pod}, request to "
                f"127.0.0.1:{fixture_port} -- served by the fixture's own process"
            ),
            pod=fixture_pod,
            namespace=fixture_namespace,
            base_url=f"http://127.0.0.1:{fixture_port}",
            evidence=evidence,
        )
    return Transport(
        kind=TransportKind.EXTERNAL_URL,
        # An external URL can never earn fixture scope. Nothing about a URL
        # demonstrates which pods terminate it, and on this cluster the routable
        # URLs all terminate at the ordinary deployment by Service selector.
        fixture_verified=False,
        description=f"HTTP request from this host to {gateway_url}",
        base_url=gateway_url.rstrip("/"),
        evidence={
            "verified": False,
            "reason": "no --fixture-pod was given, so the requests went to a routable URL. That "
                      "URL terminates at the ordinary deployment's pods (Service bedrockgateway "
                      "selects app=bedrockgateway), so the result is not a fixture measurement.",
        },
    )


def run_session(
    run: Runner, transport: Transport, *, role: str, env_var: str, token: str, path: str
) -> dict[str, Any]:
    """Resolve one token to its principal THROUGH the gateway, over `transport`.

    Decoding the JWT locally would describe what the token CLAIMS, not who the
    gateway believes it is -- and every ownership check turns on the gateway's
    belief. A token the gateway rejects must be recorded as rejected, never
    back-filled from its own payload.
    """
    url = f"{transport.base_url}{path}"
    record: dict[str, Any] = {
        "role": role,
        "env_var": env_var,          # the NAME only; the value is never recorded
        "endpoint": path,
        "transport": transport.kind.value,
        "fixture_scoped": transport.fixture_verified,
    }
    if not token:
        record["error"] = "env var unset or empty at read time"
        return record

    if transport.kind is TransportKind.FIXTURE_POD_EXEC:
        # -i so the token arrives on stdin rather than in argv.
        argv = [
            "kubectl", "exec", "-i", "-n", str(transport.namespace), str(transport.pod),
            # This probe uses only the standard library. Do not initialize the
            # gateway image's injected sitecustomize/OpenTelemetry application
            # instrumentation in this short-lived observer process.
            "--", "python3", "-S", "-c", IN_POD_SESSION, url,
        ]
        result = run(argv, token)
    else:
        argv = ["python3", "-S", "-c", IN_POD_SESSION, url]
        result = run(argv, token)

    if result.returncode != 0 and not (result.stdout or "").strip():
        record["error"] = (result.stderr or "").strip()[:300] or f"exited {result.returncode}"
        return record
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        # Unparseable output is an ERROR, never an empty success. A record with no
        # status must not read as a rejection that was observed.
        record["error"] = f"unparseable session output: {(result.stdout or '')[:200]!r}"
        return record

    if "status" in payload:
        record["status"] = payload["status"]
    if "error" in payload:
        record["error"] = str(payload["error"])[:300]
    body = payload.get("body") or {}
    for field_name in IDENTITY_FIELDS:
        if field_name in body:
            record[field_name] = body[field_name]
    return record


def _tenant(record: dict[str, Any]) -> Any:
    return record.get("org_id") or record.get("tenant_id")


def _principal(record: dict[str, Any]) -> tuple[Any, Any]:
    return (record.get("user_id"), _tenant(record))


def assess_sessions(observed: dict[str, dict[str, Any]]) -> list[str]:
    """What is wrong with this set of sessions, in operator language.

    The distinctness checks are not pedantry. If owner and nonowner are the same
    principal, every "not yours" check is asking the owner about its own row and a
    pass means nothing. If other_tenant shares owner's tenant, cross-tenant
    isolation was never exercised.
    """
    problems: list[str] = []
    for role, record in sorted(observed.items()):
        if record.get("status") != 200:
            problems.append(
                f"{role} ({record.get('env_var')}) did not authenticate: "
                f"status={record.get('status')} {str(record.get('error', ''))[:120]}"
            )
    if not all(r.get("status") == 200 for r in observed.values()):
        return problems

    owner = observed.get("owner", {})
    nonowner = observed.get("nonowner", {})
    other = observed.get("other_tenant", {})

    if _principal(owner) == _principal(nonowner):
        problems.append(
            "owner and nonowner resolve to the SAME principal. Every 'not yours' check would "
            "then be asking the owner about its own row, and a pass would mean nothing."
        )
    owner_tenant, other_tenant = _tenant(owner), _tenant(other)
    if owner_tenant and other_tenant and owner_tenant == other_tenant:
        problems.append(
            f"other_tenant is in the same tenant as owner ({owner_tenant}). Cross-tenant "
            "isolation cannot be observed with two identities in one tenant."
        )
    if not owner_tenant:
        problems.append(
            "owner's response carried no org_id/tenant_id, so tenant distinctness is unverified"
        )
    return problems


def build_report(
    *, transport: Transport, observed: dict[str, dict[str, Any]], problems: list[str]
) -> dict[str, Any]:
    """Assemble the session artifact with fixture scope DERIVED, never asserted."""
    authenticated = bool(observed) and all(r.get("status") == 200 for r in observed.values())
    return {
        "sessions": observed,
        "all_authenticated": authenticated,
        "distinct_principals": authenticated and not problems,
        "problems": problems,
        # The load-bearing field. True only when every session actually traversed a
        # pod verified to carry this run's fixture label.
        "fixture_scoped": transport.fixture_verified
        and bool(observed)
        and all(r.get("fixture_scoped") for r in observed.values()),
        "transport": {
            "kind": transport.kind.value,
            "description": transport.description,
            "fixture_verified": transport.fixture_verified,
            "pod": transport.pod,
            "namespace": transport.namespace,
            "verification": transport.evidence,
        },
        "_provenance": {
            "method": (
                "each token resolved through the gateway's own endpoint over the transport "
                "recorded above; identities are the gateway's verdict, not a locally decoded "
                "JWT payload"
            ),
            "headers_sent": ["Authorization"],
            "forged_provenance_headers_sent": False,
            "token_handling": (
                "token values are passed to the interpreter on stdin, never as process "
                "arguments (argv is world-readable via /proc); only env var NAMES are recorded"
            ),
            "fixture_scope_note": (
                "fixture_scoped is derived from the verified transport, not declared. An "
                "ORDINARY-edge URL cannot earn it: every routable URL on that edge "
                "terminates at the ordinary deployment's pods by Service selector, so only "
                "the in-pod ClusterIP transport earns it here. #5836's separate fixture edge "
                "does route to fixture pods, but this script does not use it -- so this flag "
                "reports which edge was measured, not what is measurable. See "
                "platform/scripts/operator/wave2/FIXTURE-ROUTING-CONSTRAINT.md"
            ),
        },
    }


def write_atomic(path: str, payload: dict) -> None:
    """Write whole or not at all: a half-written artifact reads as a truncated one."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".sessions-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
