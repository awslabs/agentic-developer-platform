#!/usr/bin/env python3
"""Executable isolation probes for the Wave 2 fixture (issue #3968).

Root's blocker 2: "an applied NetworkPolicy is not isolation proof." Applying a
policy and reading it back only proves the API server accepted the YAML. This
module actually opens sockets and classifies what happened.

WHY CLASSIFICATION IS THE WHOLE PROBLEM
---------------------------------------
The naive probe is `nc -z host port; if it failed, the policy works`. That is the
same defect as the published cleanup treating any error as absence: a probe that
fails because the pod name was wrong, DNS did not resolve, or the target was
never created "proves" isolation that does not exist. It is a VACUOUS PASS, and
it fails in the safe-looking direction, so nobody notices.

So every probe resolves to one of five outcomes, and a deny-probe passes on
exactly one of them:

    CONNECTED  TCP handshake completed                  -> allow-probe passes
    REFUSED    RST: reachable, nothing listening        -> NOT policy-blocked
    TIMEOUT    packets silently dropped                 -> deny-probe passes
    DNS_FAIL   name did not resolve                     -> inconclusive
    ERROR      the probe itself broke                   -> inconclusive

REFUSED versus TIMEOUT is the load-bearing distinction. A NetworkPolicy drop is
silent, so the client waits and times out. A reachable pod with nothing bound
sends a TCP RST immediately. Treating REFUSED as "blocked" would let a policy
that permits the flow pass a deny-probe purely because the listener was down.

THE POSITIVE CONTROL REQUIREMENT
--------------------------------
A TIMEOUT alone still is not proof: an address that routes nowhere also times
out. So a deny-probe must additionally carry evidence that the target EXISTS and
is otherwise reachable -- normally an allow-probe against the same target on a
permitted port. Without that control the verdict is INCONCLUSIVE, never PASS.
`evaluate()` enforces this; it is not left to the caller's discretion.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

# The in-pod probe. Kept deliberately tiny and dependency-free: it runs inside the
# gateway image, which has python3 but no guarantee of nc, curl or bash.
#
# It prints ONE token so the classification cannot be confused by incidental
# output, and it distinguishes the refusal modes rather than lumping them:
#   ECONNREFUSED -> REFUSED (reachable, nothing listening)
#   timeout      -> TIMEOUT (silently dropped: the NetworkPolicy signature)
#   gaierror     -> DNS_FAIL
IN_POD_PROBE = r"""
import socket, sys
host, port, timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
try:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
except socket.gaierror as exc:
    print("DNS_FAIL %s" % exc); sys.exit(0)
family, socktype, proto, _, sockaddr = infos[0]
s = socket.socket(family, socktype, proto)
s.settimeout(timeout)
try:
    s.connect(sockaddr)
    print("CONNECTED %s" % (sockaddr,))
except socket.timeout:
    print("TIMEOUT after %ss" % timeout)
except ConnectionRefusedError as exc:
    print("REFUSED %s" % exc)
except OSError as exc:
    # EHOSTUNREACH / ENETUNREACH are also drops, but they are distinguishable
    # from a silent policy drop and are reported as themselves.
    print("OSERROR errno=%s %s" % (exc.errno, exc))
finally:
    s.close()
"""


class Outcome(str, Enum):
    CONNECTED = "connected"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    DNS_FAIL = "dns_fail"
    ERROR = "error"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


@dataclass
class Flow:
    """One probe: reach `host:port` from inside `pod`, and what we expect.

    `expect_allowed=True` means the fixture's policy is supposed to PERMIT this.
    `control_for` names another flow id that proves THIS EXACT TARGET is reachable
    from a source the policy permits, which is mandatory for a deny-probe (see
    module docstring).

    `target_uid` is the observed `metadata.uid` of the pod whose IP `host` is. Pod
    IPs are recycled by the CNI, so an IP alone does not identify a target: a probe
    can time out against an address that now belongs to a different pod, or to no
    pod at all. The uid is carried into the record and compared between the deny
    probe and its control, so a control that proved a DIFFERENT pod reachable
    cannot vouch for this one.
    """

    flow_id: str
    description: str
    pod: str
    namespace: str
    host: str
    port: int
    expect_allowed: bool
    control_for: str | None = None
    timeout: float = 5.0
    target_uid: str | None = None


@dataclass
class Sleeper:
    calls: list[float] = field(default_factory=list)
    real: bool = False

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.real:  # pragma: no cover - not exercised under test
            import time

            time.sleep(seconds)


def classify(result: CommandResult) -> tuple[Outcome, str]:
    """Map a probe execution to an outcome. Unrecognised output is ERROR.

    Deliberately NOT a catch-all to TIMEOUT: an unparseable probe must be
    inconclusive, because TIMEOUT is the value that makes a deny-probe pass.
    """
    combined = (result.stdout or "") + (result.stderr or "")
    token = (result.stdout or "").strip().split(" ", 1)
    head = token[0] if token and token[0] else ""
    detail = combined.strip()[:400]

    # A NONZERO EXIT IS AN ERROR, whatever is on stdout. This check is FIRST and
    # unconditional, and the ordering is the entire point.
    #
    # IN_POD_PROBE exits 0 on every path it handles -- including TIMEOUT, which it
    # prints and returns 0 for. So a nonzero status never comes from the probe; it
    # comes from the transport around it: `kubectl exec` failing because the pod is
    # gone, exec is RBAC-forbidden, the connection could not be upgraded, or the
    # API server dropped it.
    #
    # The previous revision read the stdout token before the returncode, so a failed
    # exec whose stdout happened to contain "TIMEOUT" -- e.g. a partially written
    # line, or a retry's output interleaved with an error -- classified as TIMEOUT.
    # TIMEOUT is the one outcome that makes a deny-probe PASS. So the worst transport
    # failure available produced the strongest possible isolation claim, from a probe
    # that never ran to completion inside the pod.
    if result.returncode != 0:
        return Outcome.ERROR, (
            f"probe transport exited {result.returncode}; the in-pod probe exits 0 on "
            f"every outcome it can observe, so this is an exec/transport failure and "
            f"NOT an observation of the flow: {detail or '<no output>'}"
        )

    if head == "CONNECTED":
        return Outcome.CONNECTED, detail
    if head == "REFUSED":
        return Outcome.REFUSED, detail
    if head == "TIMEOUT":
        return Outcome.TIMEOUT, detail
    if head == "DNS_FAIL":
        return Outcome.DNS_FAIL, detail
    if head == "OSERROR":
        # Reported as ERROR, not TIMEOUT: EHOSTUNREACH is a routing answer, not a
        # silent drop, and must not be laundered into a passing deny-probe.
        return Outcome.ERROR, detail
    return Outcome.ERROR, f"unrecognised probe output: {detail!r}"


def run_probe(run: Runner, flow: Flow) -> dict[str, Any]:
    """Execute one flow's probe inside its pod and record the raw outcome."""
    argv = [
        "kubectl", "exec", "-n", flow.namespace, flow.pod, "--",
        "python3", "-c", IN_POD_PROBE,
        flow.host, str(flow.port), str(flow.timeout),
    ]
    result = run(argv)
    outcome, detail = classify(result)
    return {
        "flow_id": flow.flow_id,
        "description": flow.description,
        "pod": flow.pod,
        "namespace": flow.namespace,
        "target": f"{flow.host}:{flow.port}",
        "target_host": flow.host,
        "target_port": flow.port,
        "target_uid": flow.target_uid,
        "expect_allowed": flow.expect_allowed,
        "control_for": flow.control_for,
        "outcome": outcome.value,
        "detail": detail,
        # Never the full argv: it embeds the probe source and would bloat every
        # record. The flow id identifies what ran.
        "probe": f"tcp connect {flow.host}:{flow.port} timeout={flow.timeout}s",
    }


def _same_endpoint(deny: dict[str, Any], control: dict[str, Any]) -> tuple[bool, str]:
    """Does `control` prove that the endpoint `deny` probed is reachable?

    THE defect this closes. Root's finding, quoted: "A negative timeout only means
    denied if the SAME observed target UID/IP/port is reachable from an allowed
    source."

    The previous flow set paired a deny-probe against the ORDINARY worker
    (10.0.0.7:8770) with a control against the FIXTURE worker (10.0.0.9:8770) --
    a different pod at a different address. That control establishes only that
    *something* answers on 8770 somewhere. It says nothing about whether
    10.0.0.7:8770 exists, is listening, or would answer if the policy allowed it.
    So the deny-probe's timeout was attributable to any of: the policy (the claim),
    a stale pod IP, a recycled IP now owned by another pod, a worker that never
    binds a control listener, or a crashed listener. Only the first is isolation,
    and the artifact reported `isolation_proven: true` for all five.

    Sameness is judged on host, port AND the observed pod uid. The uid is what makes
    the IP trustworthy: pod IPs are recycled, so two probes minutes apart can name
    the same address and mean different pods. Where a uid was not observed for
    either side, the pair is REJECTED rather than accepted on the IP alone -- an
    unobserved identity is not a matching one.
    """
    if deny.get("target_port") != control.get("target_port"):
        return False, (
            f"control probed port {control.get('target_port')} but this deny-probe "
            f"targeted port {deny.get('target_port')}"
        )
    if deny.get("target_host") != control.get("target_host"):
        return False, (
            f"control proved {control.get('target_host')} reachable, but this deny-probe "
            f"targeted {deny.get('target_host')} -- a DIFFERENT address. Reachability of "
            f"one host does not establish that another exists or is listening, so this "
            f"timeout is not attributable to the policy"
        )
    deny_uid, control_uid = deny.get("target_uid"), control.get("target_uid")
    if not deny_uid or not control_uid:
        return False, (
            "the target pod uid was not observed for both this probe and its control "
            f"(deny={deny_uid!r}, control={control_uid!r}). Pod IPs are recycled, so an "
            "IP match alone does not establish the two probes addressed the same pod"
        )
    if deny_uid != control_uid:
        return False, (
            f"control proved pod uid {control_uid} reachable but this deny-probe "
            f"addressed uid {deny_uid} at the same IP -- the address was reassigned "
            "between the two observations"
        )
    return True, "same observed endpoint (host, port and pod uid)"


def evaluate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign a verdict to each probe record, enforcing the positive control.

    Allow-probe: PASS only on CONNECTED. REFUSED means the port is reachable but
    nothing is bound -- which is a real finding (the fixture is not serving), not
    a pass.

    Deny-probe: PASS only on TIMEOUT **and** only when its named control probe
    CONNECTED **to the same observed endpoint** (see :func:`_same_endpoint`) from a
    source the policy permits. Everything else is FAIL or INCONCLUSIVE, never PASS.
    """
    by_id = {r["flow_id"]: r for r in records}
    out: list[dict[str, Any]] = []

    for record in records:
        outcome = Outcome(record["outcome"])
        verdict: Verdict
        reason: str

        if record["expect_allowed"]:
            if outcome is Outcome.CONNECTED:
                verdict, reason = Verdict.PASS, "connected as expected"
            elif outcome is Outcome.REFUSED:
                verdict, reason = (
                    Verdict.FAIL,
                    "reachable but nothing is listening: the fixture is not serving this port",
                )
            elif outcome is Outcome.TIMEOUT:
                verdict, reason = (
                    Verdict.FAIL,
                    "timed out: a flow the fixture needs is being dropped, so any "
                    "control result measured through it would be meaningless",
                )
            else:
                verdict, reason = Verdict.INCONCLUSIVE, f"probe did not complete: {outcome.value}"

        else:
            control_id = record.get("control_for")
            control = by_id.get(control_id) if control_id else None

            # ORDER MATTERS, in a specific direction: outcomes that are DECISIVE ON
            # THEIR OWN are judged before any control quality check.
            #
            # A deny-probe that CONNECTED or was REFUSED reached the target. That is a
            # direct positive observation -- the policy is not blocking the flow -- and
            # it needs no control to be believed, because nothing about a weak control
            # could make it better news. Gating these behind the control checks (as an
            # earlier draft of this fix did) downgraded a proven isolation FAILURE to
            # "inconclusive" whenever the control was imperfect, which is the wrong
            # direction to fail in: it hides a real breach behind a bookkeeping
            # complaint. Controls exist to stop a NEGATIVE result (a timeout) being
            # over-read, so they gate only the timeout path below.
            if outcome is Outcome.CONNECTED:
                verdict, reason = (
                    Verdict.FAIL,
                    "CONNECTED to a flow that must be blocked: the fixture is not isolated",
                )
            elif outcome is Outcome.REFUSED:
                verdict, reason = (
                    Verdict.FAIL,
                    "refused rather than dropped: the packet REACHED the target, so the "
                    "policy is not blocking it; it only looked blocked because nothing "
                    "was listening",
                )
            elif outcome is not Outcome.TIMEOUT:
                verdict, reason = Verdict.INCONCLUSIVE, f"probe did not complete: {outcome.value}"
            # From here the outcome IS a timeout, which is the only outcome that can
            # make a deny-probe pass -- and therefore the only one whose attribution
            # has to be earned.
            elif control_id is None:
                verdict, reason = (
                    Verdict.INCONCLUSIVE,
                    "deny-probe declares no positive control, so a timeout cannot be "
                    "distinguished from an address that routes nowhere",
                )
            elif control is None:
                verdict, reason = (
                    Verdict.INCONCLUSIVE,
                    f"declared control '{control_id}' was not run",
                )
            elif Outcome(control["outcome"]) is not Outcome.CONNECTED:
                verdict, reason = (
                    Verdict.INCONCLUSIVE,
                    f"control '{control_id}' did not connect "
                    f"({control['outcome']}), so this target is not known to exist; "
                    "a timeout here proves nothing",
                )
            elif control.get("expect_allowed") is not True:
                # The control must run from a source the policy PERMITS. A control
                # that is itself a deny-probe proves nothing about reachability.
                verdict, reason = (
                    Verdict.INCONCLUSIVE,
                    f"control '{control_id}' is not an allow-probe, so it does not "
                    "establish that this endpoint is reachable from a permitted source",
                )
            else:
                same, why = _same_endpoint(record, control)
                if not same:
                    verdict, reason = (
                        Verdict.INCONCLUSIVE,
                        f"control '{control_id}' does not vouch for this endpoint: {why}",
                    )
                else:
                    verdict, reason = (
                        Verdict.PASS,
                        f"silently dropped while control '{control_id}' connected to the "
                        f"SAME observed endpoint ({record.get('target')}, pod uid "
                        f"{record.get('target_uid')}): the policy is blocking this flow",
                    )

        out.append({**record, "verdict": verdict.value, "verdict_reason": reason})

    return out


def summarise(evaluated: list[dict[str, Any]]) -> dict[str, Any]:
    """Whole-run isolation verdict.

    `isolation_proven` requires every probe to PASS. An inconclusive probe is not
    a pass: the point of this step is to establish isolation before a control
    experiment runs, and "we could not tell" does not establish it.
    """
    counts = {v.value: 0 for v in Verdict}
    for record in evaluated:
        counts[record["verdict"]] += 1

    allow = [r for r in evaluated if r["expect_allowed"]]
    deny = [r for r in evaluated if not r["expect_allowed"]]

    return {
        "isolation_proven": bool(evaluated) and counts[Verdict.PASS.value] == len(evaluated),
        "counts": counts,
        "allow_probes": len(allow),
        "deny_probes": len(deny),
        # Recorded explicitly so a run with no deny-probes cannot read as proof.
        "has_deny_probes": bool(deny),
        "failures": [
            {"flow_id": r["flow_id"], "verdict": r["verdict"], "reason": r["verdict_reason"]}
            for r in evaluated
            if r["verdict"] != Verdict.PASS.value
        ],
        "probes": evaluated,
    }


def build_flows(
    *,
    run_id: str,
    gateway_pod: str,
    gateway_namespace: str,
    agent_namespace: str,
    fixture_worker_pod: str | None,
    fixture_worker_host: str | None,
    ordinary_worker_host: str | None,
    control_port: int = 8770,
    ordinary_worker_uid: str | None = None,
    allowed_source_pod: str | None = None,
    allowed_source_namespace: str | None = None,
) -> list[Flow]:
    """The fixture-scoped flow set.

    Allow-probes cover what the evaluation needs to work at all. Deny-probes cover
    what must NOT be reachable -- and each names a positive control that proves THE
    SAME ENDPOINT is reachable from a permitted source.

    THE CONTROL FOR THE ORDINARY-WORKER DENY-PROBE
    ----------------------------------------------
    The previous revision used the FIXTURE worker probe as that control: a different
    pod at a different IP, on the same port number. That does not establish that the
    ordinary worker's address exists or that anything is bound to it, so its timeout
    was equally consistent with a stale IP, a recycled IP, or a worker that never
    binds a control listener. See :func:`_same_endpoint`.

    A valid control must reach `ordinary_worker_host:control_port` -- the identical
    endpoint -- from a source the policy PERMITS. The fixture gateway is by
    definition not such a source, so this cannot be synthesised from the fixture:
    it needs a pod that is allowed to reach ordinary workers, which the operator
    supplies via `allowed_source_pod`.

    Without it, the deny-probe is emitted with NO control and therefore grades
    INCONCLUSIVE. That is the correct outcome and it is deliberate: this module does
    not select an arbitrary ordinary pod to exec into, because pods of unknown
    ownership are not this evaluation's to touch. An honest "not established" is the
    whole point of the step.
    """
    gateway_host = f"w2-fixture-gateway-{run_id.removeprefix('w2-')}.{gateway_namespace}.svc.cluster.local"
    flows: list[Flow] = [
        Flow("gw-self-8080", "fixture gateway serves HTTP on 8080",
             gateway_pod, gateway_namespace, "127.0.0.1", 8080, True),
        Flow("gw-dns-53", "fixture gateway can resolve DNS",
             gateway_pod, gateway_namespace, "kube-dns.kube-system.svc.cluster.local", 53, True),
    ]

    if fixture_worker_host:
        flows.append(
            Flow("gw-to-fixture-worker-control",
                 "fixture gateway reaches the FIXTURE worker's control listener "
                 "(the flow W2-03/04/05 measure)",
                 gateway_pod, gateway_namespace, fixture_worker_host, control_port, True))

    if ordinary_worker_host:
        control_id: str | None = None
        if allowed_source_pod:
            # Same host, same port, same observed uid -- from a PERMITTED source.
            # Emitted immediately before the deny-probe it vouches for, so the two
            # observations are contemporaneous and in the same discovery pass.
            control_id = "allowed-to-ordinary-worker-control"
            flows.append(
                Flow(control_id,
                     "a PERMITTED source reaches the ordinary worker's control listener: "
                     "contemporaneous evidence that this exact endpoint exists and is bound",
                     allowed_source_pod, allowed_source_namespace or agent_namespace,
                     ordinary_worker_host, control_port, True,
                     target_uid=ordinary_worker_uid))

        flows.append(
            Flow("gw-to-ordinary-worker-control",
                 "fixture gateway must NOT reach an ORDINARY worker's control listener",
                 gateway_pod, gateway_namespace, ordinary_worker_host, control_port,
                 False, control_for=control_id, target_uid=ordinary_worker_uid))

    if fixture_worker_pod and fixture_worker_host:
        flows.append(
            Flow("worker-to-gw-8080",
                 "fixture worker reaches the fixture gateway for bootstrap/task pull",
                 fixture_worker_pod, agent_namespace, gateway_host, 8080, True))

    return flows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _real_runner(argv: Sequence[str]) -> CommandResult:
    import subprocess

    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=120)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _write_atomic(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".probes-", suffix=".json")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="probe fixture isolation and classify results")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--gateway-pod", required=True)
    parser.add_argument("--gateway-namespace", default="adp-gateway")
    parser.add_argument("--agent-namespace", default="adp-agents")
    parser.add_argument("--fixture-worker-pod")
    parser.add_argument("--fixture-worker-host")
    parser.add_argument("--ordinary-worker-host")
    parser.add_argument(
        "--ordinary-worker-uid",
        help="Observed metadata.uid of the deny target. Required for a deny-probe to "
             "PASS: pod IPs are recycled, so an IP alone does not identify a pod.",
    )
    parser.add_argument(
        "--allowed-source-pod",
        help="A pod the policy PERMITS to reach ordinary workers, used as the positive "
             "control against the SAME endpoint as the deny-probe. Without it the "
             "deny-probe has no valid control and grades inconclusive.",
    )
    parser.add_argument("--allowed-source-namespace")
    parser.add_argument("--control-port", type=int, default=8770)
    parser.add_argument("--out", required=True)
    parser.add_argument("--generated-at", required=True)
    args = parser.parse_args(argv)

    flows = build_flows(
        run_id=args.run_id,
        gateway_pod=args.gateway_pod,
        gateway_namespace=args.gateway_namespace,
        agent_namespace=args.agent_namespace,
        fixture_worker_pod=args.fixture_worker_pod,
        fixture_worker_host=args.fixture_worker_host,
        ordinary_worker_host=args.ordinary_worker_host,
        control_port=args.control_port,
        ordinary_worker_uid=args.ordinary_worker_uid,
        allowed_source_pod=args.allowed_source_pod,
        allowed_source_namespace=args.allowed_source_namespace,
    )

    records = [run_probe(_real_runner, flow) for flow in flows]
    summary = summarise(evaluate(records))
    summary["run_id"] = args.run_id
    summary["generated_at"] = args.generated_at

    # Say out loud when the deny half was not exercised. A run with only
    # allow-probes would otherwise report a tidy all-pass that proves nothing
    # about isolation.
    if not summary["has_deny_probes"]:
        summary["isolation_proven"] = False
        summary.setdefault("failures", []).append({
            "flow_id": "-",
            "verdict": "inconclusive",
            "reason": "no deny-probes were run (pass --ordinary-worker-host); "
                      "allow-probes alone cannot establish isolation",
        })

    _write_atomic(args.out, summary)
    print(json.dumps({k: summary[k] for k in ("isolation_proven", "counts", "failures")},
                     indent=2, sort_keys=True))
    return 0 if summary["isolation_proven"] else 5


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
