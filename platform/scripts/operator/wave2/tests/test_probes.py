#!/usr/bin/env python3
"""Tests for the isolation probes (issue #3968, root's blocker 2).

Root: "an applied NetworkPolicy is not isolation proof." These tests target the
ways a probe harness can report proof it does not have -- because that is the
failure mode that matters here. A probe suite that under-reports isolation is
annoying; one that over-reports it lets a control experiment run against a
fixture that is not actually contained.

The three vacuous passes under test:
  1. REFUSED read as "blocked" (the packet arrived; nothing was listening)
  2. TIMEOUT with no positive control (an address routing nowhere also times out)
  3. an all-allow-probe run reporting isolation it never tested

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import probes  # noqa: E402


def result(stdout: str = "", rc: int = 0, stderr: str = "") -> probes.CommandResult:
    return probes.CommandResult(rc, stdout, stderr)


# The endpoint identity a deny-probe and its control must AGREE on before a timeout
# can be attributed to the policy: host, port and the observed pod uid. Defaulted
# to one shared endpoint here so the ordinary helpers build a VALID pair, and the
# tests that care about mismatch state the divergence explicitly.
HOST = "10.0.9.9"
PORT = 8770
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def allow(flow_id: str, outcome: probes.Outcome, *, host: str = HOST,
          port: int = PORT, uid: str | None = UID) -> dict:
    return {"flow_id": flow_id, "description": "", "pod": "p", "namespace": "n",
            "target": f"{host}:{port}", "target_host": host, "target_port": port,
            "target_uid": uid, "expect_allowed": True, "control_for": None,
            "outcome": outcome.value, "detail": "", "probe": ""}


def deny(flow_id: str, outcome: probes.Outcome, control_for: str | None, *,
         host: str = HOST, port: int = PORT, uid: str | None = UID) -> dict:
    return {"flow_id": flow_id, "description": "", "pod": "p", "namespace": "n",
            "target": f"{host}:{port}", "target_host": host, "target_port": port,
            "target_uid": uid, "expect_allowed": False, "control_for": control_for,
            "outcome": outcome.value, "detail": "", "probe": ""}


def verdict_of(records: list[dict], flow_id: str) -> str:
    return next(r["verdict"] for r in probes.evaluate(records) if r["flow_id"] == flow_id)


# ---------------------------------------------------------------------------
# classification: REFUSED is not TIMEOUT
# ---------------------------------------------------------------------------
def test_refused_and_timeout_are_distinguished() -> None:
    """The load-bearing distinction.

    A NetworkPolicy drop is silent, so the client times out. A reachable pod with
    nothing bound answers with RST immediately. Collapsing the two lets a policy
    that permits the flow pass a deny-probe because the listener was down.
    """
    assert probes.classify(result("REFUSED [Errno 111]"))[0] is probes.Outcome.REFUSED
    assert probes.classify(result("TIMEOUT after 5.0s"))[0] is probes.Outcome.TIMEOUT
    assert probes.classify(result("CONNECTED ('10.0.0.1', 8770)"))[0] is probes.Outcome.CONNECTED
    assert probes.classify(result("DNS_FAIL name unknown"))[0] is probes.Outcome.DNS_FAIL


def test_host_unreachable_is_error_not_timeout() -> None:
    """EHOSTUNREACH is a routing ANSWER, not a silent drop.

    Mapping it to TIMEOUT would launder a misconfigured target into a passing
    deny-probe.
    """
    assert probes.classify(result("OSERROR errno=113 unreachable"))[0] is probes.Outcome.ERROR


@pytest.mark.parametrize("stdout,rc", [("", 1), ("garbage", 0), ("", 0), ("Error from server", 1)])
def test_unrecognised_output_is_error_never_timeout(stdout: str, rc: int) -> None:
    """A broken probe must be inconclusive, because TIMEOUT is what passes a deny."""
    assert probes.classify(result(stdout, rc))[0] is probes.Outcome.ERROR


def test_kubectl_exec_failure_is_error() -> None:
    """e.g. the pod was deleted mid-run, or exec is RBAC-denied."""
    outcome, detail = probes.classify(
        result("", 1, 'Error from server (Forbidden): pods "p" is forbidden'))
    assert outcome is probes.Outcome.ERROR
    assert "Forbidden" in detail


# ---------------------------------------------------------------------------
# vacuous pass 1: REFUSED must not read as blocked
# ---------------------------------------------------------------------------
def test_deny_probe_refused_is_a_failure_not_a_pass() -> None:
    """THE headline vacuous pass.

    The packet REACHED the target and got an RST, so the policy did not block it.
    It only looked blocked because nothing was listening. Naive probes report this
    as isolation.
    """
    records = [allow("ctl", probes.Outcome.CONNECTED),
               deny("d", probes.Outcome.REFUSED, "ctl")]
    assert verdict_of(records, "d") == "fail"
    reason = next(r["verdict_reason"] for r in probes.evaluate(records) if r["flow_id"] == "d")
    assert "REACHED the target" in reason


def test_deny_probe_connected_is_a_failure() -> None:
    records = [allow("ctl", probes.Outcome.CONNECTED),
               deny("d", probes.Outcome.CONNECTED, "ctl")]
    assert verdict_of(records, "d") == "fail"


def test_deny_probe_timeout_with_connected_control_passes() -> None:
    """The one passing shape: silently dropped, while the control proves reachability."""
    records = [allow("ctl", probes.Outcome.CONNECTED),
               deny("d", probes.Outcome.TIMEOUT, "ctl")]
    assert verdict_of(records, "d") == "pass"


# ---------------------------------------------------------------------------
# vacuous pass 2: TIMEOUT without a positive control
# ---------------------------------------------------------------------------
def test_deny_probe_without_a_control_is_inconclusive() -> None:
    """An address that routes nowhere times out exactly like a policy drop.

    Without a control, a typo'd hostname is indistinguishable from isolation --
    and it fails in the safe-LOOKING direction, so nobody investigates.
    """
    records = [deny("d", probes.Outcome.TIMEOUT, None)]
    assert verdict_of(records, "d") == "inconclusive"


def test_deny_probe_whose_control_did_not_connect_is_inconclusive() -> None:
    """If the control could not connect, the target is not known to exist."""
    records = [allow("ctl", probes.Outcome.TIMEOUT),
               deny("d", probes.Outcome.TIMEOUT, "ctl")]
    assert verdict_of(records, "d") == "inconclusive"
    reason = next(r["verdict_reason"] for r in probes.evaluate(records) if r["flow_id"] == "d")
    assert "not known to exist" in reason


def test_deny_probe_naming_a_control_that_never_ran_is_inconclusive() -> None:
    records = [deny("d", probes.Outcome.TIMEOUT, "does-not-exist")]
    assert verdict_of(records, "d") == "inconclusive"


# ---------------------------------------------------------------------------
# allow-probes
# ---------------------------------------------------------------------------
def test_allow_probe_timeout_fails_because_the_measured_flow_is_broken() -> None:
    """A blocked flow-under-test yields a meaningless control result, not a failed one.

    This is the defect in the published policy: 53/443 only, so 8770 was dropped
    and every control measurement would have been measuring the policy.
    """
    records = [allow("gw-to-worker", probes.Outcome.TIMEOUT)]
    assert verdict_of(records, "gw-to-worker") == "fail"
    reason = next(r["verdict_reason"] for r in probes.evaluate(records))
    assert "meaningless" in reason


@pytest.mark.parametrize("stdout,stderr", [
    ("TIMEOUT after 5.0s\n", "error: unable to upgrade connection: pod does not exist"),
    ("TIMEOUT after 5.0s\n", 'Error from server (Forbidden): pods "gw" is forbidden: '
                             'cannot create resource "pods/exec"'),
    ("TIMEOUT", "error: Timeout occurred"),
    ("CONNECTED ('10.0.0.5', 8770)\n", "error: lost connection to pod"),
])
def test_a_nonzero_exec_is_an_error_whatever_stdout_says(stdout, stderr) -> None:
    """Root's finding: "reject nonzero exec even when stdout contains TIMEOUT."

    IN_POD_PROBE exits 0 on every outcome it can observe -- TIMEOUT included. So a
    nonzero status never comes from the probe; it comes from the transport around it:
    the pod is gone, exec is RBAC-forbidden, the connection could not be upgraded.

    The previous revision read the stdout token BEFORE the returncode, so a failed
    exec whose output happened to contain TIMEOUT classified as TIMEOUT -- the one
    outcome that makes a deny-probe PASS. The worst transport failure available
    produced the strongest isolation claim, from a probe that never completed.
    """
    outcome, detail = probes.classify(probes.CommandResult(1, stdout, stderr))
    assert outcome is probes.Outcome.ERROR, (
        f"a failed exec must not be read as an observation of the flow: {detail}")
    assert "transport" in detail, "the record must say the transport failed, not the flow"


def test_a_transport_failure_cannot_pass_a_deny_probe() -> None:
    """End to end: the failed exec must not become a passing isolation claim."""
    def runner(argv):
        return result("TIMEOUT after 5.0s\n", rc=1, stderr="error: pod does not exist")

    flow = probes.Flow("d", "must not reach", "gw", "adp-gateway", HOST, PORT,
                       False, control_for="ctl", target_uid=UID)
    record = probes.run_probe(runner, flow)
    assert record["outcome"] == "error"
    records = [allow("ctl", probes.Outcome.CONNECTED), record]
    summary = probes.summarise(probes.evaluate(records))
    assert verdict_of(records, "d") == "inconclusive"
    assert summary["isolation_proven"] is False


def test_a_zero_exit_timeout_is_still_a_real_observation() -> None:
    """The converse, so the fix does not simply reject everything.

    A genuine policy drop: the probe ran to completion inside the pod, printed
    TIMEOUT and exited 0. That must still pass with a valid control.
    """
    records = [
        allow("ctl", probes.Outcome.CONNECTED),
        deny("d", probes.Outcome.TIMEOUT, "ctl"),
    ]
    assert probes.classify(probes.CommandResult(0, "TIMEOUT after 5.0s\n"))[0] \
        is probes.Outcome.TIMEOUT
    assert verdict_of(records, "d") == "pass"
    assert probes.summarise(probes.evaluate(records))["isolation_proven"] is True


def test_allow_probe_refused_is_a_real_finding() -> None:
    """Reachable but not serving: the fixture is not up. Not a pass."""
    assert verdict_of([allow("a", probes.Outcome.REFUSED)], "a") == "fail"


def test_allow_probe_dns_failure_is_inconclusive_not_fail() -> None:
    """Honest about the difference between "blocked" and "we could not test it"."""
    assert verdict_of([allow("a", probes.Outcome.DNS_FAIL)], "a") == "inconclusive"


# ---------------------------------------------------------------------------
# vacuous pass 3: whole-run reporting
# ---------------------------------------------------------------------------
def test_inconclusive_does_not_count_as_proven() -> None:
    """"We could not tell" does not establish isolation.

    This step gates a control experiment; passing on doubt defeats the gate.
    """
    summary = probes.summarise(probes.evaluate(
        [allow("ctl", probes.Outcome.CONNECTED), deny("d", probes.Outcome.TIMEOUT, None)]))
    assert summary["isolation_proven"] is False
    assert summary["counts"]["inconclusive"] == 1


def test_all_pass_is_proven() -> None:
    summary = probes.summarise(probes.evaluate(
        [allow("ctl", probes.Outcome.CONNECTED), deny("d", probes.Outcome.TIMEOUT, "ctl")]))
    assert summary["isolation_proven"] is True
    assert summary["failures"] == []


def test_empty_probe_set_is_not_proof() -> None:
    """Zero probes trivially satisfies "all passed"; it must not read as proven."""
    summary = probes.summarise([])
    assert summary["isolation_proven"] is False


def test_allow_only_run_records_that_it_tested_no_isolation() -> None:
    """has_deny_probes exists so an all-green allow-only run cannot be misread.

    The CLI turns this into isolation_proven=False with a stated reason.
    """
    summary = probes.summarise(probes.evaluate([allow("a", probes.Outcome.CONNECTED)]))
    assert summary["isolation_proven"] is True   # every probe did pass...
    assert summary["has_deny_probes"] is False   # ...but nothing was denied
    assert summary["deny_probes"] == 0


def test_failures_carry_the_reason_not_just_the_flow_id() -> None:
    """The report has to be actionable by root without re-running anything."""
    summary = probes.summarise(probes.evaluate(
        [allow("ctl", probes.Outcome.CONNECTED), deny("d", probes.Outcome.CONNECTED, "ctl")]))
    assert summary["failures"][0]["flow_id"] == "d"
    assert "not isolated" in summary["failures"][0]["reason"]


# ---------------------------------------------------------------------------
# probe execution
# ---------------------------------------------------------------------------
def test_probe_runs_inside_the_pod_and_does_not_log_the_source() -> None:
    """The record identifies the flow without embedding the probe source."""
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return result("CONNECTED ('10.0.0.5', 8770)")

    flow = probes.Flow("f", "d", "gw-pod", "adp-gateway", "10.0.0.5", 8770, True)
    record = probes.run_probe(runner, flow)
    assert calls[0][:6] == ["kubectl", "exec", "-n", "adp-gateway", "gw-pod", "--"]
    assert record["outcome"] == "connected"
    assert "import socket" not in record["probe"]
    assert "10.0.0.5:8770" in record["probe"]


def test_deny_flow_without_an_allowed_source_declares_no_control() -> None:
    """Root's finding: the control must prove THE SAME endpoint reachable.

    This previously wired the FIXTURE worker probe (10.0.0.5:8770) as the control
    for the ORDINARY worker deny-probe (10.0.9.9:8770) -- a different pod at a
    different address, matching only on port NUMBER. That control establishes that
    something answers on 8770 somewhere; it says nothing about whether 10.0.9.9:8770
    exists or is bound, so the deny-probe's timeout was equally consistent with a
    stale IP, a recycled IP, or a worker with no control listener at all.

    A valid control has to come from a source the policy PERMITS, reaching the
    identical endpoint -- which the fixture gateway by definition cannot do. So
    without --allowed-source-pod the deny-probe correctly carries NO control and
    grades inconclusive. Fabricating a same-port control from a different pod is
    precisely the defect; declaring none is the honest alternative.
    """
    flows = probes.build_flows(
        run_id="w2-test", gateway_pod="gw", gateway_namespace="adp-gateway",
        agent_namespace="adp-agents", fixture_worker_pod="wk",
        fixture_worker_host="10.0.0.5", ordinary_worker_host="10.0.9.9")
    deny_flows = [f for f in flows if not f.expect_allowed]
    assert deny_flows, "the flow set must include a deny-probe"
    ordinary = next(f for f in deny_flows if f.flow_id == "gw-to-ordinary-worker-control")
    assert ordinary.control_for is None, (
        "with no permitted source available, no control can honestly be claimed")

    # And it must therefore NOT pass: an uncontrolled timeout is inconclusive.
    records = [probes.run_probe(lambda argv: result("TIMEOUT after 5.0s"), f) for f in flows]
    summary = probes.summarise(probes.evaluate(records))
    ordinary_record = next(
        r for r in summary["probes"] if r["flow_id"] == "gw-to-ordinary-worker-control")
    assert ordinary_record["verdict"] == "inconclusive"
    assert summary["isolation_proven"] is False


def test_deny_flow_control_targets_the_identical_endpoint_when_a_source_is_given() -> None:
    """Given a permitted source, the control must probe the SAME host, port and uid."""
    flows = probes.build_flows(
        run_id="w2-test", gateway_pod="gw", gateway_namespace="adp-gateway",
        agent_namespace="adp-agents", fixture_worker_pod="wk",
        fixture_worker_host="10.0.0.5", ordinary_worker_host="10.0.9.9",
        ordinary_worker_uid=UID, allowed_source_pod="permitted-pod",
        allowed_source_namespace="adp-agents")
    ordinary = next(f for f in flows if f.flow_id == "gw-to-ordinary-worker-control")
    control = next(f for f in flows if f.flow_id == ordinary.control_for)

    assert (control.host, control.port) == (ordinary.host, ordinary.port), \
        "the control must probe the identical address and port, not merely the same port"
    assert control.target_uid == ordinary.target_uid == UID, \
        "both must name the same observed pod uid, or a recycled IP could vouch for itself"
    assert control.expect_allowed is True, "a control must run from a PERMITTED source"
    assert control.pod == "permitted-pod" and control.pod != ordinary.pod, \
        "the control cannot originate from the fixture pod the policy is blocking"


@pytest.mark.parametrize("control_kwargs,why", [
    ({"host": "10.0.0.5"}, "a control against a DIFFERENT host (the original defect)"),
    ({"port": 9999}, "a control against a different port on the right host"),
    ({"uid": None}, "no observed uid, so an IP match cannot identify the pod"),
    ({"uid": "99999999-9999-9999-9999-999999999999"}, "a recycled IP: same address, other pod"),
])
def test_a_control_for_another_endpoint_cannot_make_a_timeout_pass(control_kwargs, why) -> None:
    """Each way the control can fail to vouch for the probed endpoint.

    In every case the deny-probe timed out and the control CONNECTED, which is the
    exact shape that used to report isolation_proven=true.
    """
    records = [
        allow("ctl", probes.Outcome.CONNECTED, **control_kwargs),
        deny("d", probes.Outcome.TIMEOUT, "ctl"),
    ]
    summary = probes.summarise(probes.evaluate(records))
    assert verdict_of(records, "d") == "inconclusive", f"passed despite {why}"
    assert summary["isolation_proven"] is False, f"reported proven despite {why}"


def test_a_deny_probe_cannot_be_its_own_kind_of_control() -> None:
    """A control that is itself a deny-probe proves nothing about reachability."""
    records = [
        deny("ctl", probes.Outcome.CONNECTED, None),
        deny("d", probes.Outcome.TIMEOUT, "ctl"),
    ]
    assert verdict_of(records, "d") == "inconclusive"


def test_a_connected_deny_probe_fails_even_with_a_useless_control() -> None:
    """Control quality must never soften a DIRECT positive observation.

    A deny-probe that connected reached the target: the policy is not blocking the
    flow, full stop. Controls exist to stop a NEGATIVE result being over-read, so
    ordering them ahead of this check would downgrade a proven isolation breach to
    "inconclusive" over a bookkeeping complaint -- hiding the breach.
    """
    records = [
        allow("ctl", probes.Outcome.CONNECTED, host="10.0.0.5", uid=None),
        deny("d", probes.Outcome.CONNECTED, "ctl"),
    ]
    assert verdict_of(records, "d") == "fail"
    reason = next(r["verdict_reason"] for r in probes.evaluate(records) if r["flow_id"] == "d")
    assert "not isolated" in reason


def test_control_port_flow_is_present_when_a_worker_exists() -> None:
    """8770 is the flow W2-03/04/05 measure; the published policy dropped it."""
    flows = probes.build_flows(
        run_id="w2-test", gateway_pod="gw", gateway_namespace="adp-gateway",
        agent_namespace="adp-agents", fixture_worker_pod="wk",
        fixture_worker_host="10.0.0.5", ordinary_worker_host=None)
    assert any(f.port == 8770 and f.expect_allowed for f in flows)
