#!/usr/bin/env python3
"""Tests for human-session fixture routing (issue #3968, root's blocker 7).

The property under test is narrow and load-bearing: `fixture_scoped` must be
DERIVED from the transport that actually carried the request, never asserted.

The first attempt at blocker 7 wrote `fixture_scoped: true` as a literal into the
artifact while the script sent its requests to whatever `--gateway-url` it was
handed -- normally the ordinary gateway, because an operator host cannot route to a
fixture ClusterIP. The artifact claimed a fixture measurement and described an
ordinary-deployment one. Several tests below exist specifically to keep that from
coming back.

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

import edge_sessions as es  # noqa: E402

RUN_ID = "w2-20260924-abc123"
POD = "w2-fixture-gateway-20260924-abc123-7f9c-xyz"
NS = "adp-gateway"
UID = "11111111-2222-3333-4444-555555555555"


def pod_doc(*, label=RUN_ID, uid=UID, phase="Running", labels=None):
    meta = {"uid": uid, "labels": labels if labels is not None else {"adp.io/w2-fixture": label}}
    return json.dumps({"metadata": meta, "status": {"phase": phase}})


class Recorder:
    """A runner that records argv AND stdin, so token handling is observable."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv, stdin=""):
        self.calls.append((list(argv), stdin))
        joined = " ".join(argv)
        for match, reply in self.replies:
            if match in joined:
                return reply
        return es.CommandResult(1, "", f"no stub for {joined}")

    @property
    def argv_blob(self) -> str:
        return "\n".join(" ".join(a) for a, _ in self.calls)


def session_reply(status=200, body=None, error=None):
    payload = {"endpoint_url": "http://127.0.0.1:8080/me/budget", "headers_sent": ["Authorization"]}
    if status is not None:
        payload["status"] = status
    if body is not None:
        payload["body"] = body
    if error is not None:
        payload["error"] = error
    return es.CommandResult(0, json.dumps(payload) + "\n")


def principal(user, tenant):
    return {"user_id": user, "org_id": tenant, "github_login": user}


# ---------------------------------------------------------------------------
# transport verification: only a verified fixture pod may claim fixture scope
# ---------------------------------------------------------------------------
def test_verified_fixture_pod_is_fixture_scoped() -> None:
    run = Recorder([("get pod", es.CommandResult(0, pod_doc()))])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=POD, fixture_namespace=NS, gateway_url="")
    assert transport.kind is es.TransportKind.FIXTURE_POD_EXEC
    assert transport.fixture_verified is True
    assert transport.evidence["uid"] == UID


def test_external_url_can_never_be_fixture_scoped() -> None:
    """THE defect this module was written to remove.

    An operator host cannot route to a fixture ClusterIP, so a URL transport is
    always the ordinary deployment. Nothing about a URL demonstrates what serves
    it, so it must not be able to earn fixture scope under any circumstances.
    """
    run = Recorder([])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=None, fixture_namespace=NS,
        gateway_url="https://gw.example.com")
    assert transport.kind is es.TransportKind.EXTERNAL_URL
    assert transport.fixture_verified is False
    assert "ordinary deployment" in transport.evidence["reason"]
    assert run.calls == [], "the URL path must not need the cluster at all"


def test_pod_with_a_foreign_fixture_label_is_refused() -> None:
    """A pod from ANOTHER run is not this run's fixture.

    Root's constraint names `authority-probe-gateway-20260920` as an object of
    unknown ownership that must not be reused. Accepting any fixture-ish label
    would let a previous run's leftovers be measured and filed as this run's.
    """
    run = Recorder([("get pod", es.CommandResult(0, pod_doc(label="w2-20260920-other")))])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=POD, fixture_namespace=NS, gateway_url="")
    assert transport.fixture_verified is False
    assert transport.evidence["observed_fixture_label"] == "w2-20260920-other"


def test_unlabelled_pod_is_refused() -> None:
    """The ordinary gateway pod carries no fixture label. Exercising it and
    recording a fixture measurement is exactly the substitution under test."""
    run = Recorder([("get pod", es.CommandResult(0, pod_doc(labels={"app": "bedrockgateway"})))])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=POD, fixture_namespace=NS, gateway_url="")
    assert transport.fixture_verified is False
    assert transport.evidence["observed_fixture_label"] is None


def test_unreachable_api_server_is_not_a_verified_pod() -> None:
    """An error is not an answer -- the recurring defect class in this PR.

    A `kubectl get` that fails because the cluster is unreachable must not resolve
    to any claim about the pod's labels.
    """
    run = Recorder([("get pod", es.CommandResult(1, "", "Unable to connect to the server"))])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=POD, fixture_namespace=NS, gateway_url="")
    assert transport.fixture_verified is False
    assert "Unable to connect" in transport.evidence["error"]
    assert "not known to be" in transport.evidence["reason"]


def test_unparseable_pod_document_is_refused() -> None:
    run = Recorder([("get pod", es.CommandResult(0, "not json at all"))])
    result = es.verify_fixture_pod(run, pod=POD, namespace=NS, run_id=RUN_ID)
    assert result["verified"] is False
    assert "unparseable" in result["error"]


def test_pod_without_a_uid_is_refused() -> None:
    """A name can be recreated between steps; the uid identifies the object."""
    run = Recorder([("get pod", es.CommandResult(0, pod_doc(uid="")))])
    result = es.verify_fixture_pod(run, pod=POD, namespace=NS, run_id=RUN_ID)
    assert result["verified"] is False
    assert "metadata.uid" in result["reason"]


@pytest.mark.parametrize("phase", ["Pending", "Failed", "Succeeded", ""])
def test_non_running_pod_is_refused(phase: str) -> None:
    run = Recorder([("get pod", es.CommandResult(0, pod_doc(phase=phase)))])
    result = es.verify_fixture_pod(run, pod=POD, namespace=NS, run_id=RUN_ID)
    assert result["verified"] is False


def test_failed_fixture_verification_does_not_fall_back_to_the_url() -> None:
    """No silent substitution.

    If the operator asked for a fixture measurement and the pod does not check
    out, quietly measuring the ordinary gateway instead is the whole failure mode.
    The transport stays FIXTURE_POD_EXEC-and-unverified so the caller refuses.
    """
    run = Recorder([("get pod", es.CommandResult(1, "", "NotFound"))])
    transport = es.choose_transport(
        run, run_id=RUN_ID, fixture_pod=POD, fixture_namespace=NS,
        gateway_url="https://gw.example.com")
    assert transport.kind is es.TransportKind.FIXTURE_POD_EXEC
    assert transport.fixture_verified is False
    assert "gw.example.com" not in (transport.base_url or "")


# ---------------------------------------------------------------------------
# token handling
# ---------------------------------------------------------------------------
def test_token_goes_on_stdin_never_into_argv() -> None:
    """argv is world-readable via /proc, so a token there leaks to every process
    in the pod. This asserts the absence, which is the only way to catch it."""
    secret = "ghu_SUPERSECRETVALUE123"
    run = Recorder([("kubectl exec", session_reply(body=principal("u1", "t1")))])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    es.run_session(run, transport, role="owner", env_var="OWNER_TOKEN",
                   token=secret, path="/me/budget")
    assert secret not in run.argv_blob, "token must never be a process argument"
    assert run.calls[0][1] == secret, "token must arrive on stdin"
    assert "-i" in run.calls[0][0], "kubectl exec needs -i to forward stdin"


def test_only_the_env_var_name_is_recorded() -> None:
    run = Recorder([("kubectl exec", session_reply(body=principal("u1", "t1")))])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    record = es.run_session(run, transport, role="owner", env_var="OWNER_TOKEN",
                            token="ghu_secret", path="/me/budget")
    assert record["env_var"] == "OWNER_TOKEN"
    assert "ghu_secret" not in json.dumps(record)


def test_no_provenance_headers_are_ever_sent() -> None:
    """Forging X-Caller-Identity is forbidden AND useless: a failed provenance
    check is a 403, so a fabricated header proves nothing."""
    assert "X-Caller-Identity" not in es.IN_POD_SESSION
    assert "X-Adp-Edge-Provenance" not in es.IN_POD_SESSION
    assert "Authorization" in es.IN_POD_SESSION


def test_only_allowlisted_identity_fields_are_copied() -> None:
    """A gateway response may carry budget detail that has no place in evidence."""
    body = dict(principal("u1", "t1"), spend_usd=42.5, api_key="sk-leak")
    run = Recorder([("kubectl exec", session_reply(body=body))])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    record = es.run_session(run, transport, role="owner", env_var="V",
                            token="t", path="/me/budget")
    blob = json.dumps(record)
    assert "sk-leak" not in blob and "spend_usd" not in blob
    assert record["user_id"] == "u1"


# ---------------------------------------------------------------------------
# session records: an unobserved result must not read as an observed one
# ---------------------------------------------------------------------------
def test_unparseable_session_output_is_an_error_not_a_rejection() -> None:
    """A record with no status must not be read as "the gateway said no".

    Recording silence as a rejection is the mirror image of recording an error as
    absence, and both manufacture an observation nobody made.
    """
    run = Recorder([("kubectl exec", es.CommandResult(0, "kubectl: some warning\n"))])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    record = es.run_session(run, transport, role="owner", env_var="V",
                            token="t", path="/me/budget")
    assert "status" not in record
    assert "unparseable" in record["error"]


def test_empty_token_is_reported_without_a_request() -> None:
    run = Recorder([])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    record = es.run_session(run, transport, role="owner", env_var="V",
                            token="", path="/me/budget")
    assert "unset or empty" in record["error"]
    assert run.calls == [], "no request should be made for an absent token"


def test_http_error_status_is_preserved() -> None:
    run = Recorder([("kubectl exec", session_reply(status=403, error="forbidden"))])
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, base_url="http://127.0.0.1:8080")
    record = es.run_session(run, transport, role="owner", env_var="V",
                            token="t", path="/me/budget")
    assert record["status"] == 403


# ---------------------------------------------------------------------------
# distinctness assessment
# ---------------------------------------------------------------------------
def three(owner=("u1", "t1"), nonowner=("u2", "t1"), other=("u3", "t2"), status=200):
    def rec(role, ident):
        return dict(role=role, env_var=f"{role.upper()}_T", status=status,
                    user_id=ident[0], org_id=ident[1], fixture_scoped=True)
    return {"owner": rec("owner", owner), "nonowner": rec("nonowner", nonowner),
            "other_tenant": rec("other_tenant", other)}


def test_three_distinct_principals_have_no_problems() -> None:
    assert es.assess_sessions(three()) == []


def test_owner_equal_to_nonowner_is_a_problem() -> None:
    """Otherwise every "not yours" check asks the owner about its own row."""
    problems = es.assess_sessions(three(nonowner=("u1", "t1")))
    assert any("SAME principal" in p for p in problems)


def test_other_tenant_in_owners_tenant_is_a_problem() -> None:
    problems = es.assess_sessions(three(other=("u3", "t1")))
    assert any("same tenant as owner" in p for p in problems)


def test_missing_tenant_is_unverified_not_assumed_distinct() -> None:
    observed = three()
    del observed["owner"]["org_id"]
    problems = es.assess_sessions(observed)
    assert any("tenant distinctness is unverified" in p for p in problems)


def test_a_failed_authentication_is_reported_before_distinctness() -> None:
    """Distinctness of unauthenticated sessions is meaningless, so it must not be
    computed from whatever fields a 403 body happened to carry."""
    observed = three()
    observed["nonowner"]["status"] = 401
    problems = es.assess_sessions(observed)
    assert any("did not authenticate" in p for p in problems)
    assert not any("SAME principal" in p for p in problems)


# ---------------------------------------------------------------------------
# the report: fixture_scoped is derived
# ---------------------------------------------------------------------------
def test_report_is_fixture_scoped_only_with_a_verified_transport() -> None:
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True,
        description="d", pod=POD, namespace=NS, evidence={"verified": True, "uid": UID})
    report = es.build_report(transport=transport, observed=three(), problems=[])
    assert report["fixture_scoped"] is True
    assert report["distinct_principals"] is True
    assert report["transport"]["verification"]["uid"] == UID


def test_report_from_an_external_url_is_not_fixture_scoped() -> None:
    """The exact regression: this combination previously recorded `true`."""
    transport = es.Transport(
        kind=es.TransportKind.EXTERNAL_URL, fixture_verified=False,
        description="d", base_url="https://gw.example.com", evidence={"verified": False})
    observed = three()
    for record in observed.values():
        record["fixture_scoped"] = False
    report = es.build_report(transport=transport, observed=observed, problems=[])
    assert report["fixture_scoped"] is False
    assert report["all_authenticated"] is True, "the sessions were still real"


def test_one_unscoped_session_makes_the_whole_report_unscoped() -> None:
    """Partial scope is not scope: a mixed set cannot be filed as a fixture run."""
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True, description="d")
    observed = three()
    observed["other_tenant"]["fixture_scoped"] = False
    assert es.build_report(transport=transport, observed=observed,
                           problems=[])["fixture_scoped"] is False


def test_no_sessions_is_not_a_fixture_scoped_pass() -> None:
    """Vacuous truth: `all([])` is True, so an empty set would otherwise report
    both fully-authenticated and fixture-scoped having measured nothing."""
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True, description="d")
    report = es.build_report(transport=transport, observed={}, problems=[])
    assert report["fixture_scoped"] is False
    assert report["all_authenticated"] is False
    assert report["distinct_principals"] is False


def test_problems_block_distinctness_even_when_authenticated() -> None:
    transport = es.Transport(
        kind=es.TransportKind.FIXTURE_POD_EXEC, fixture_verified=True, description="d")
    report = es.build_report(transport=transport, observed=three(), problems=["something"])
    assert report["distinct_principals"] is False


def test_report_records_that_no_headers_were_forged() -> None:
    transport = es.Transport(
        kind=es.TransportKind.EXTERNAL_URL, fixture_verified=False, description="d")
    report = es.build_report(transport=transport, observed={}, problems=[])
    assert report["_provenance"]["forged_provenance_headers_sent"] is False
    assert "FIXTURE-ROUTING-CONSTRAINT.md" in report["_provenance"]["fixture_scope_note"]


# ---------------------------------------------------------------------------
# atomic write
# ---------------------------------------------------------------------------
def test_write_atomic_leaves_no_partial_file(tmp_path: Path) -> None:
    target = tmp_path / "sessions.json"
    es.write_atomic(str(target), {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert list(tmp_path.glob(".sessions-*")) == [], "no temp file may survive"
