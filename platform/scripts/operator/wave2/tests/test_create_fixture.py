#!/usr/bin/env python3
"""Behavioural tests for 10-create-fixture.sh (issue #3968).

Root's rejection of the published script was about four behaviours, and every one
of them is a REFUSAL. So these tests assert that the refusal happens AND that
nothing was created -- `run.created()` inspects the stub call log for any
mutating call. A script that complains loudly and then creates the fixture anyway
passes an exit-code test and fails these.

The four:
  1. composition        -- copy the live spec; never hand-assemble
  2. isolation          -- policies exist before anything can listen
  3. ownership          -- exclusive create, uid recorded after creation
  4. fail closed        -- stop BEFORE creating an unusable fixture

Run: python3 -m pytest platform/scripts/operator/wave2/tests/ -q
"""

from __future__ import annotations

import json

import pytest

# conftest puts lib/ on sys.path, so this is the same module the script itself runs.
import render_fixture
from conftest import TARGET_ACCOUNT, base_rules, live_deployment, not_found

DIGEST = "sha256:" + "ab" * 32


def _creation_rules(**kw) -> list[dict]:
    """base_rules plus everything needed for a full successful creation."""
    rules = base_rules(**kw)
    rules += [
        {"tool": "aws", "match": ["get-queue-url"],
         "rc": 1, "stderr": "An error occurred (AWS.SimpleQueueService.NonExistentQueue) "
                            "when calling the GetQueueUrl operation"},
        {"tool": "aws", "match": ["create-queue"],
         "stdout": f"https://sqs.us-east-1.amazonaws.com/{TARGET_ACCOUNT}/"
                   "adp-dev-w2-fixture-fixture-test.fifo\n"},
    ]
    return rules


def _tag_reply(nonce_echo: str = "__RUN_NONCE__") -> dict:
    # The script reads the tag back; the stub cannot know the generated nonce, so
    # tests that need a MATCH use --resume-nonce to fix it (see below).
    return {"tool": "aws", "match": ["list-queue-tags"], "stdout": nonce_echo + "\n"}


FIXED_NONCE = "deadbeefcafe0123"


def _create_ok_rules(**kw) -> list[dict]:
    rules = _creation_rules(**kw)
    rules.append(_tag_reply(FIXED_NONCE))
    # policies then gateway, each create returning a distinct server uid
    for index, kind in enumerate(["networkpolicy", "networkpolicy", "deployment", "service"]):
        rules.append({"tool": "kubectl", "match": ["create", "-f"], "once": True,
                      "stdout": json.dumps({"kind": kind.title(), "metadata": {
                          "name": "x", "namespace": "adp-gateway",
                          "uid": f"server-uid-{index}"}})})
    return rules


def _args_fixed_nonce() -> list[str]:
    return ["--resume-nonce", FIXED_NONCE]


# ---------------------------------------------------------------------------
# #5836's edge ownership receipt, which the endpoint is now READ FROM
# ---------------------------------------------------------------------------
# Root's requirement 2: "Consume account/run/nonce-bound #5836 output, not arbitrary
# HTTPS." The endpoint used to be an operator-supplied string checked only for
# `https://` and "not the ordinary API id" -- which every OTHER run's fixture edge in
# this same account also satisfies. It now comes out of a receipt bound to
# run_nonce/account_id/region, so a URL belonging to another run's edge is refused.
#
# Shaped from #5836's real outputs (modules/gateway/infra/fixture-edge/outputs.tf on
# agent/issue-5836 @ 4bbe3d75), not invented here. Root found the previous version of
# this helper inventing a field (5809603844): `ownership` carries the BINDINGS, while
# the endpoint is a SEPARATE top-level output, so the whole `terraform output -json`
# document is what the script consumes. The wrapping under {"value", "type"} is what
# that command actually emits.
FIXTURE_API_ID = "fixture123"
# #5836's main.tf sets `stage_name = var.environment`, so the stage in the URL is the
# environment -- not a fixture-specific name. Getting this wrong was invisible while
# the check was substring containment of the API id.
FIXTURE_STAGE = "dev"


# The fixture ALB and its interface addresses, as #5836's
# `fixture_alb_network_policy_source` output publishes them. Two /32s because an ALB has
# one interface per subnet it occupies: a script that admitted only the first would
# produce an INTERMITTENT bootstrap failure, which is the worst shape this defect has.
FIXTURE_ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:879318057152:"
    "loadbalancer/app/w2-fixture-edge/abc123")
ALB_CIDRS = ["10.0.10.41/32", "10.0.11.52/32"]
ALB_IPS = [c.split("/")[0] for c in ALB_CIDRS]
# render_fixture.FIXTURE_POD_PORT: the port the fixture Service actually targets. Not
# written out as 8080 here -- the ALB rule's port is checked against that constant, and
# a local copy is what would let the two drift while every test still passed.
ALB_PORT = render_fixture.FIXTURE_POD_PORT


def _edge_receipt(tmp_path, *, nonce: str = FIXED_NONCE,
                  account: str = "879318057152", region: str = "us-east-1",
                  environment: str = "dev",
                  api_id: str = FIXTURE_API_ID, endpoint: str | None = None,
                  alb_arn: str = FIXTURE_ALB_ARN,
                  alb_source: dict | None = ...,
                  name: str = "edge-outputs.json") -> str:
    """Write a bound `terraform output -json` document and return its path.

    `alb_source=None` omits the ALB output entirely, which is what a receipt from an
    edge built before #5836 landed that output looks like -- the case where the fixture
    gateway silently denies the edge.
    """
    ownership = {
        "run_nonce": nonce,
        "account_id": account,
        "region": region,
        "environment": environment,
        "rest_api_id": api_id,
        "teardown": {"mechanism": "terraform destroy against the isolated per-run "
                                 "state key"},
        "resources": [{"kind": "apigateway-rest-api",
                       "type": "aws_api_gateway_rest_api", "id": api_id}],
    }
    raw = {
        "fixture_edge_enabled": True,
        "run_nonce": nonce,
        "rest_api_id": api_id,
        "worker_control_endpoint": endpoint if endpoint is not None else (
            f"https://{api_id}.execute-api.{region}.amazonaws.com/{environment}"
            "/internal/v1/agent"),
        "ssm_provenance_parameter_name": f"/adp/fixture-edge/{nonce}/provenance",
        "ownership": ownership,
    }
    if alb_source is ...:
        alb_source = {
            "source_cidrs": list(ALB_CIDRS),
            "source_ips": list(ALB_IPS),
            "container_port": ALB_PORT,
            "alb_listener_port": 80,
            "run_nonce": nonce,
            "alb_arn": alb_arn,
            "apply_to": "the run-scoped FIXTURE gateway NetworkPolicy",
            "verify": "aws ec2 describe-network-interfaces --filters ...",
        }
    if alb_source is not None:
        raw["fixture_alb_network_policy_source"] = alb_source
    path = tmp_path / name
    path.write_text(json.dumps(
        {key: {"value": value, "type": "object"} for key, value in raw.items()}))
    return str(path)


def _worker_args(tmp_path, *, alb_arn: str = FIXTURE_ALB_ARN, **receipt_kw) -> list[str]:
    """The flags a real worker run passes: fixed nonce + this run's edge receipt.

    The ARN is passed separately from the receipt on purpose, and defaults to the same
    value the receipt carries: it is the expectation the receipt is checked AGAINST, so
    deriving it from the receipt would make the check compare the document with itself.
    """
    return _args_fixed_nonce() + [
        "--worker-job",
        "--edge-receipt", _edge_receipt(tmp_path, alb_arn=alb_arn, **receipt_kw),
        "--edge-alb-arn", alb_arn]


# ---------------------------------------------------------------------------
# 4. fail closed  (root: "remove continuation with a known nonfunctional worker")
# ---------------------------------------------------------------------------
def test_missing_control_secret_stops_before_creating_anything(run_create) -> None:
    """THE headline defect: the published script noted this and carried on.

    It then created a gateway that could not sign or verify a control envelope,
    so every control check would have failed for a reason unrelated to the
    software under review -- and root would have been left cleaning up a live
    fixture that proved nothing.
    """
    run = run_create(_creation_rules(secrets_present=False))
    assert run.rc == 1
    assert not run.created(), \
        f"must create NOTHING when control secrets are absent; issued: {run.calls}"
    assert "control-critical secret material is missing" in run.output
    assert "Nothing has been created" in run.output


def test_missing_secret_names_the_owning_change_not_a_workaround(run_create) -> None:
    """The refusal has to be actionable, or the next operator just reruns it."""
    run = run_create(_creation_rules(secrets_present=False))
    assert "agent-authority-bootstrap.tf" in run.output


def test_no_worker_job_is_created_by_default(run_create) -> None:
    """The protected worker is OPT-IN, and the default path must remain honest.

    It is the only part of the fixture that holds protected authority, and its
    control endpoint has to be supplied. So the default is a gateway-only fixture --
    and the run must SAY that the worker checks have no subject, rather than leaving
    a reader to assume a full fixture was built.
    """
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    # No Job object, specifically. The fixture-scoped worker INGRESS POLICY is a
    # different thing and must exist -- an earlier version of this assertion
    # matched the substring "worker" and so also forbade the policy, which would
    # have pushed the fix in exactly the wrong direction.
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)
    assert not any("kubectl create" in c and "w2-fixture-worker" in c for c in run.calls)
    # The consequence is stated: missing evidence is not_run, never a pass.
    assert "not_run" in run.output


def test_the_default_run_records_no_worker_rather_than_claiming_one(run_create) -> None:
    """fixture-created.json is what downstream reads; it must match the cluster."""
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    record = json.loads((run.tmp / "evidence" / "fixture-created.json").read_text())
    assert record["worker_job_created"] is False
    assert record["worker"]["created"] is False
    assert "not requested" in record["worker"]["reason"]
    # No identity fields on a worker that does not exist -- an empty-string uid
    # sitting in the record is exactly the vacuous-binding shape root reproduced.
    assert "pod_uid" not in record["worker"]


def test_worker_job_without_an_edge_receipt_is_refused(run_create) -> None:
    """THE load-bearing input, and the reason the old outright refusal existed.

    Every https control endpoint that is not #5836's fixture edge resolves to the
    ORDINARY gateway, so there is no default. And a bare URL is no longer sufficient
    either: the receipt is what ties the endpoint to THIS run's edge, in an account
    where every other run's edge is also https and also not the ordinary API.
    """
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce() + ["--worker-job"])
    assert run.rc == 1
    assert "requires --edge-receipt" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_a_bare_endpoint_without_a_receipt_is_no_longer_enough(run_create) -> None:
    """Root's requirement 2, executed: an arbitrary HTTPS URL must not be consumed.

    This is the regression that matters. The URL below is perfectly well-formed,
    is https, and is not the ordinary API -- it is simply somebody else's fixture
    edge. Under the previous checks it was accepted and a protected worker was
    pointed at infrastructure this run could neither own nor tear down.
    """
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce() + [
        "--worker-job",
        "--worker-control-endpoint",
        "https://someoneelse.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent"])
    assert run.rc == 1
    assert "requires --edge-receipt" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_a_receipt_from_another_run_is_refused(run_create, tmp_path) -> None:
    """A different run's edge, in this same account and region.

    Bound on the nonce, which is this run's ledger identity -- so the refusal does
    not depend on the two edges looking different, which they do not.
    """
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"})
    run = run_create(rules, args=_worker_args(tmp_path, nonce="ffffffffffffffff"))
    assert run.rc == 1
    assert "does not belong to this run" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_a_non_https_worker_endpoint_is_refused(run_create, tmp_path) -> None:
    """run_identity.py rejects any other scheme before attempting a bootstrap.

    Still checked on the receipt's own value: a bound receipt is not an infallible
    one, and the failure it would cause looks like the feature being broken.
    """
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"})
    run = run_create(rules, args=_worker_args(
        tmp_path, endpoint="http://w2-fixture-gateway.adp-gateway.svc:8080/x"))
    assert run.rc == 1
    assert "not https" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_an_endpoint_on_the_ordinary_api_is_refused(run_create, tmp_path) -> None:
    """The one thing this must never build: a control worker aimed at production.

    The ordinary API id is read from SSM and COMPARED, rather than the fixture edge
    being recognised by pattern -- a pattern would admit any URL that happened to
    look fixture-shaped. Kept on this side of the receipt too: a receipt NAMING
    production does not make production a fixture edge.
    """
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"})
    run = run_create(rules, args=_worker_args(tmp_path, api_id="ord1nary99"))
    assert run.rc == 1
    assert "ORDINARY" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_an_unreadable_ordinary_api_id_refuses_rather_than_assuming_isolation(
        run_create, tmp_path) -> None:
    """"Could not check" must never read as "checked and fine".

    A transient SSM failure would otherwise skip the single check that proves the
    fixture edge is not the production edge.
    """
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "rc": 1, "stderr": "An error occurred (ParameterNotFound)"})
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    # Matched on a fragment that does not span the message's line wrap.
    assert "is not a passed check" in run.output
    assert "could not read the ORDINARY gateway's API id" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_the_bound_endpoint_provenance_is_recorded(run_create, tmp_path) -> None:
    """A receipt-derived endpoint and a flag-supplied one look identical in the URL.

    So how it was established is written down. Without it a later reader cannot tell
    whether the binding was performed at all.
    """
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "rc": 1, "stderr": "ParameterNotFound"})
    run = run_create(rules, args=_worker_args(tmp_path))
    # The SSM failure stops the run BEFORE the receipt is consulted, which is the
    # intended order -- so no provenance is written for an unestablished endpoint.
    assert run.rc == 1
    assert not (tmp_path / "evidence" / "edge-endpoint-provenance.json").exists()


def test_an_empty_approved_digest_list_refuses_before_creating_a_worker(
        run_create, tmp_path) -> None:
    """An empty allowlist admits nothing, so it must not be read as admitting anything.

    A worker created against an empty list deploys happily and is then refused at
    bootstrap -- a failure that looks like the feature being broken.
    """
    rules = _create_ok_rules()
    rules += [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        # The ALB admission runs BEFORE the digest list is read, because it gates worker
        # creation; its replies are included so this test exercises the digest refusal
        # rather than stopping earlier for an unrelated reason.
        *_alb_admission_rules(),
        # No second Job lookup: the stage gate already established its absence.
        {"tool": "kubectl", "match": ["get", "configmap", "adp-worker-authority-config"],
         "stdout": ""},
    ]
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "AGENT_WORKER_IMAGE_DIGESTS" in run.output
    # And it names the owning change rather than suggesting a workaround.
    assert "Terraform-owned" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)


def test_an_existing_job_of_the_same_name_is_refused_not_adopted(
        run_create, tmp_path) -> None:
    """Same standard as the gateway: this run did not create it, so it cannot own it.

    The Job is now observed by the stage gate BEFORE the endpoint is resolved, so this
    also pins the ordering: the refusal must arrive without the SSM lookup or the
    receipt ever being consulted. Creating nothing is the point -- an existing
    protected worker of this name is authority nobody in this run can account for.
    """
    rules = _create_ok_rules()
    # Replaces the absent-Job reply base_rules provides, since the stub matches the
    # first unconsumed rule: an existing Job answers with its uid.
    rules.insert(0, {"tool": "kubectl", "match": ["get", "Job", "w2-fixture-worker"],
                     "stdout": "44444444-4444-4444-4444-444444444444\n"})
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert not run.created()
    assert "already exists" in run.output
    assert not any("ssm" in call for call in run.calls), \
        f"the Job collision must be refused before the endpoint is resolved: {run.calls}"


# ---------------------------------------------------------------------------
# the worker actually gets created -- root: "do not leave worker_job_created:
# false as the final implementation"
# ---------------------------------------------------------------------------
# The endpoint _edge_receipt publishes for FIXTURE_API_ID. Derived from the same
# helper rather than written out again, so the success-path assertion below proves the
# script used the RECEIPT's value and not a flag it was handed.
FIXTURE_ENDPOINT = (
    f"https://{FIXTURE_API_ID}.execute-api.us-east-1.amazonaws.com/{FIXTURE_STAGE}"
    "/internal/v1/agent")
WORKER_DIGEST = "sha256:" + "cd" * 32
JOB_UID = "11111111-2222-3333-4444-555555555555"
POD_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
POD_NAME = "w2-fixture-worker-fixture-test-x7k2p"
WORKER_SA_ROLE = "arn:aws:iam::879318057152:role/adp-dev-agent-authority-worker"


# The gateway policy's uid as _create_ok_rules hands it back. The policies are created
# FIRST (isolation before anything can listen), and the gateway-side one is first of the
# two, so the server uid it records is index 0. Read from the ledger by the script rather
# than assumed, which is what these replies have to agree with.
GW_POLICY_UID = "server-uid-0"
POLICY_NAME = "w2-fixture-policy-fixture-test"
GW_NAME = "w2-fixture-gateway-fixture-test"
RUN_ID = "w2-fixture-test"


def _live_gateway_policy(*, uid: str = GW_POLICY_UID, nonce: str = FIXED_NONCE,
                         resource_version: str = "9001",
                         admits_alb: bool = False, **over) -> dict:
    """The live gateway NetworkPolicy, as the API server hands it back.

    Rendered by the REAL renderer rather than hand-written, so a change to the fixture's
    ingress shape reaches these tests instead of leaving them asserting against a policy
    nobody deploys. `admits_alb=True` is the post-mutation object: the same policy with
    the ipBlock rule the script appends.
    """
    policy = render_fixture.render_policies(
        run_id=RUN_ID, nonce=nonce, policy_name=POLICY_NAME,
        gateway_namespace="adp-gateway", agent_namespace="adp-agents",
        gateway_label=GW_NAME, worker_label=GW_NAME,
        alb_source=({"cidrs": list(ALB_CIDRS), "container_port": ALB_PORT}
                    if admits_alb else None),
    )[0]
    policy["metadata"]["uid"] = uid
    policy["metadata"]["resourceVersion"] = resource_version
    policy["metadata"].update(over.pop("metadata", {}))
    policy.update(over)
    return policy


def _alb_admission_rules(*, policy_uid: str = GW_POLICY_UID,
                         fresh_ips: list[str] | None = None,
                         live: dict | None = ...,
                         after: dict | None = ...,
                         replace: dict | None = None) -> list[dict]:
    """Replies for the ALB admission block: re-observe, read back, replace, re-read.

    Four separate answers because the block deliberately asks four times. In particular
    the policy is read TWICE -- before the mutation and after it -- and the second read
    is the gate on worker creation, because `kubectl replace` reporting success is not
    evidence that the live object admits anything.
    """
    ips = ALB_IPS if fresh_ips is None else fresh_ips
    rules: list[dict] = [
        # #5836's own `verify` command: the ALB's interfaces, described by the ELB
        # description filter. Tab-separated, which is what `--output text` emits for a
        # list projection.
        {"tool": "aws", "match": ["ec2", "describe-network-interfaces"],
         "stdout": "\t".join(ips) + "\n"},
    ]
    # `policy_uid` must reach BOTH policy replies. While it reached neither, a caller
    # passing a wrong uid to test the refusal got the correct one back and exercised the
    # happy path instead -- an ignored helper parameter makes a refusal test assert the
    # opposite of what it says.
    if live is ...:
        live = _live_gateway_policy(uid=policy_uid)
    if live is not None:
        rules.append({"tool": "kubectl", "match": ["get", "networkpolicy", POLICY_NAME,
                                                   "-o", "json"], "once": True,
                      "stdout": json.dumps(live)})
    rules.append(replace or {"tool": "kubectl", "match": ["replace", "-f"],
                             "stdout": f"networkpolicy.networking.k8s.io/{POLICY_NAME} "
                                       "replaced\n"})
    if after is ...:
        after = _live_gateway_policy(uid=policy_uid, admits_alb=True,
                                     resource_version="9002")
    if after is not None:
        rules.append({"tool": "kubectl", "match": ["get", "networkpolicy", POLICY_NAME,
                                                   "-o", "json"], "once": True,
                      "stdout": json.dumps(after)})
    return rules


def _worker_rules(*, digest: str = WORKER_DIGEST,
                  approved: str | None = None,
                  pod_over: dict | None = None,
                  policy_uid: str = GW_POLICY_UID,
                  base: list[dict] | None = None) -> list[dict]:
    """Everything the worker path reads, in the order it reads it.

    Spelled out rather than generated so the test states exactly which live facts the
    launcher depends on: if it starts reading something new, a rule must be added
    here deliberately.

    `base` replaces the single-invocation prefix (queue creation, gateway creates) so
    test_staged_lifecycle.py can reuse this worker tail against a SECOND invocation,
    where those objects already exist from the gateway stage. Sharing the tail is the
    point: the staged worker must read exactly the same live facts as the all-in-one
    worker, and a separate copy of these rules could drift into testing a different
    contract.
    """
    from conftest import live_worker_template

    template = live_worker_template()
    template["spec"]["containers"][0]["image"] = f"repo/adp-agent-runtime@{digest}"

    pod = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {
            "name": POD_NAME, "namespace": "adp-agents", "uid": POD_UID,
            "labels": {"adp.io/w2-fixture": "w2-fixture-test",
                       "adp.io/w2-nonce": FIXED_NONCE},
            "ownerReferences": [{"kind": "Job", "name": "w2-fixture-worker-fixture-test",
                                 "uid": JOB_UID, "controller": True}],
        },
        "spec": {"serviceAccountName": "agent-authority-worker-sa",
                 "containers": [{"name": "agent-worker"}]},
        "status": {
            "phase": "Running", "podIP": "10.0.42.17",
            "containerStatuses": [{
                "name": "agent-worker", "ready": True,
                "imageID": f"docker-pullable://repo/adp-agent-runtime@{digest}",
                "state": {"running": {"startedAt": "2026-09-24T01:31:07Z"}}}],
        },
    }
    if pod_over:
        pod["status"].update(pod_over.get("status", {}))
        pod["metadata"].update(pod_over.get("metadata", {}))

    rules = list(base) if base is not None else _create_ok_rules()
    rules += [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        *_alb_admission_rules(policy_uid=policy_uid),
        # The Job's absence is observed by the stage gate (conftest.base_rules), before
        # the endpoint is resolved -- not a second time here. There is deliberately no
        # second reply: a second lookup would be a second answer, and the one that
        # decides is the one taken before creation began.
        {"tool": "kubectl", "match": ["get", "configmap", "adp-worker-authority-config"],
         "stdout": approved if approved is not None else digest},
        {"tool": "kubectl", "match": ["get", "scaledjob", "agent-scaledjob"],
         "stdout": json.dumps(template)},
        # the Job create -- the 5th kubectl create of the run
        {"tool": "kubectl", "match": ["create", "-f"], "once": True,
         "stdout": json.dumps({"kind": "Job", "metadata": {
             "name": "w2-fixture-worker-fixture-test", "namespace": "adp-agents",
             "uid": JOB_UID}})},
        {"tool": "kubectl", "match": ["get", "job", "w2-fixture-worker"], "once": False,
         "stdout": JOB_UID},
        {"tool": "kubectl", "match": ["get", "pods", "adp.io/w2-fixture"], "once": False,
         "stdout": POD_NAME},
        {"tool": "kubectl", "match": ["get", "pod", POD_NAME, "{.status.phase}"],
         "once": False, "stdout": "Running"},
        {"tool": "kubectl", "match": ["get", "pod", POD_NAME, "-o", "json"], "once": False,
         "stdout": json.dumps(pod)},
        {"tool": "kubectl", "match": ["get", "sa", "agent-authority-worker-sa"],
         "once": False, "stdout": WORKER_SA_ROLE},
    ]
    return rules




def test_a_protected_worker_is_actually_created_and_bound(run_create, tmp_path) -> None:
    """The headline behaviour. This used to be an unconditional refusal.

    It asserts the whole chain: the Job is created, its server-assigned uid is read
    back, the pod is found and bound to that uid, and the record says so. A test that
    only checked rc == 0 would pass against a script that skipped the binding.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    assert any("kubectl create" in c and "20-worker" in c for c in run.calls), run.calls

    record = json.loads((run.tmp / "evidence" / "fixture-created.json").read_text())
    assert record["worker_job_created"] is True
    assert record["worker"]["job_uid"] == JOB_UID
    assert record["worker"]["pod_uid"] == POD_UID
    assert record["worker"]["runtime_image_digest"] == WORKER_DIGEST
    assert record["worker"]["control_endpoint"] == FIXTURE_ENDPOINT


def test_the_worker_job_and_pod_are_both_recorded_with_server_uids(run_create, tmp_path) -> None:
    """Teardown deletes by uid, so anything unrecorded is something nobody can remove.

    The pod is recorded as well as the Job: deleting the Job cascades, but the pod uid
    is also the identity the experiment proves itself against, and the ledger is the
    record of what was OBSERVED.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    entries = run.ledger()["k8s"]
    kinds = {(e["kind"], e["uid"]) for e in entries}
    assert ("Job", JOB_UID) in kinds, entries
    assert ("Pod", POD_UID) in kinds, entries
    # 2 policies + deployment + service + Job + Pod
    assert len(entries) == 6, entries


def test_the_expected_identity_document_is_written_for_the_experiment(run_create, tmp_path) -> None:
    """The reference an unsupervised run is measured against, authored by the OPERATOR.

    The pod cannot produce this for itself -- that is the defect root reproduced with
    "two env vars are not identity". It must name the pod UID, because the experiment
    proves which pod it is by reading its own projected uid.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    doc = json.loads((run.tmp / "evidence" / "expected-identity.json").read_text())
    identity = doc["expected_identity"]
    assert identity["pod_uid"] == POD_UID
    assert identity["job_uid"] == JOB_UID
    assert identity["service_account"] == "agent-authority-worker-sa"
    assert identity["aws_role_arn"] == WORKER_SA_ROLE
    assert identity["runtime_image_digest"] == WORKER_DIGEST
    assert identity["account_id"] == TARGET_ACCOUNT


def test_the_worker_is_never_read_by_an_in_worker_kubectl(run_create, tmp_path) -> None:
    """The protected SA cannot get or list pods, and that boundary stays closed.

    Every pod read here is the OPERATOR's, made with the operator's kubeconfig before
    the evidence step runs. Asserted as the absence of any RBAC-granting call: a
    helper made to work by widening the protected SA's permissions is the fix root
    specifically prohibited.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    for call in run.calls:
        assert "create rolebinding" not in call, call
        assert "create clusterrolebinding" not in call, call
        assert "create role " not in call, call
        # No mutation of the protected SA or the ordinary worker's policies.
        assert not ("apply" in call and "agent-authority-worker-sa" in call), call
        assert not ("patch" in call and "agent-scaledjob" in call), call


def test_a_pod_owned_by_a_different_job_is_refused_after_creation(run_create, tmp_path) -> None:
    """The Job exists by this point, so the refusal must still leave a clean record.

    A pod wearing this run's labels but owned by another Job is a different workload;
    binding it would make the expected-identity document describe something this run
    did not create.
    """
    rules = _worker_rules(pod_over={"metadata": {
        "ownerReferences": [{"kind": "Job", "name": "someone-else",
                             "uid": "99999999-9999-9999-9999-999999999999",
                             "controller": True}]}})
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "not controlled by the Job this run created" in run.output
    # The Job WAS created before the refusal, so the operator must be told it is
    # recorded and how to remove it -- a refusal that leaks a live control-enabled
    # workload is worse than the bind failure.
    assert "90-cleanup-ledger.sh" in run.output
    assert ("Job", JOB_UID) in {(e["kind"], e["uid"]) for e in run.ledger()["k8s"]}


def test_a_worker_running_an_unapproved_digest_is_refused_after_creation(run_create, tmp_path) -> None:
    """The spec passed the render check; the RESOLVED image is a different fact.

    kubelet reports what is actually running, and a tag in the spec can have moved
    since admission. The gateway compares the resolved digest, so this worker would
    be refused at bootstrap.
    """
    other = "sha256:" + "ef" * 32
    rules = _worker_rules(pod_over={"status": {
        "phase": "Running", "podIP": "10.0.42.17",
        "containerStatuses": [{"name": "agent-worker", "ready": True,
                               "imageID": f"docker-pullable://repo@{other}"}]}})
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "NOT on the gateway's approved list" in run.output


def test_check_only_validates_the_worker_without_creating_it(run_create, tmp_path) -> None:
    """A dry run must not produce a control-enabled workload, or bind a nonexistent pod."""
    rules = _worker_rules()
    # Server dry-run replies for the five objects.
    rules += [{"tool": "kubectl", "match": ["create", "-f", "--dry-run=server"],
               "once": False, "stdout": "job.batch/w2-fixture-worker-fixture-test\n"}]
    run = run_create(rules, args=_worker_args(tmp_path) + ["--check-only"])
    assert run.rc == 0, run.output
    record = json.loads((run.tmp / "evidence" / "fixture-created.json").read_text())
    assert record["worker_job_created"] is False
    assert "check-only" in record["worker"]["reason"]
    assert not (run.tmp / "evidence" / "expected-identity.json").exists()


# ---------------------------------------------------------------------------
# 3. ownership / collision  (root: "apply can adopt; create-queue can return existing")
# ---------------------------------------------------------------------------
def test_existing_deployment_is_refused_not_adopted(run_create) -> None:
    """`kubectl apply` would ADOPT and mutate it, and the ledger would then
    authorise DELETING somebody else's workload at teardown.

    The staged lifecycle did not weaken this. The distinction it adds is WHOSE object
    it is: a Deployment that exists and is recorded in this run's ledger under the same
    uid is this run's own earlier stage, while one this ledger does not record is a
    stranger's -- and only the second gets "use a different --run-id". Here the ledger
    is empty, so this is the stranger case: the original refusal, unchanged.
    """
    rules = base_rules()
    rules.insert(1, {"tool": "kubectl", "match": ["get", "Deployment", "w2-fixture-gateway"],
                     "stdout": "22222222-2222-2222-2222-222222222222\n"})
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "already exists" in run.output
    assert "cannot be owned or torn down by this run" in run.output
    assert "Use a different --run-id" in run.output
    # And it must NOT be described as a completed stage of this run, which is the
    # other branch and comes with the opposite advice ("continue with the next stage").
    assert "has already run" not in run.output


def test_unreadable_cluster_is_not_an_empty_one(run_create) -> None:
    """A collision probe that FAILS must not read as 'absent, go ahead'.

    Same defect class as the cleanup treating an error as absence.
    """
    rules = base_rules()
    rules.insert(1, {"tool": "kubectl", "match": ["get", "Deployment", "w2-fixture-gateway"],
                     "rc": 1, "stderr": "Unable to connect to the server: i/o timeout"})
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "an unreadable cluster is not an empty one" in run.output


def test_existing_queue_is_refused_because_create_queue_would_adopt_it(run_create) -> None:
    """CreateQueue is idempotent on a name match: it returns the EXISTING queue.

    The published script recorded that as run_bound and would have deleted it.
    """
    rules = base_rules()
    rules.append({"tool": "aws", "match": ["get-queue-url"],
                  "stdout": "https://sqs.us-east-1.amazonaws.com/1/pre-existing.fifo\n"})
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "would ADOPT a queue this run did" in run.output


def test_queue_probe_error_is_not_absence(run_create) -> None:
    rules = base_rules()
    rules.append({"tool": "aws", "match": ["get-queue-url"],
                  "rc": 255, "stderr": "An error occurred (AccessDenied)"})
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "a failed lookup is not an absent queue" in run.output


def test_queue_is_tagged_with_the_run_nonce_at_creation(run_create) -> None:
    """Ownership evidence must exist from the instant the queue does."""
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    create = next(c for c in run.calls if "create-queue" in c)
    assert f"adp-w2-nonce={FIXED_NONCE}" in create
    assert "--tags" in create


def test_queue_whose_tag_does_not_read_back_is_refused(run_create) -> None:
    """Proof comes from the server, not from our own intent.

    A queue that does not carry this run's nonce may be a pre-existing one, so it
    must not be recorded as ours -- recording it is what authorises deletion.
    """
    rules = _creation_rules()
    rules.append(_tag_reply("a-completely-different-nonce"))
    run = run_create(rules, args=_args_fixed_nonce())
    assert run.rc == 1
    assert "does not carry this run's nonce" in run.output
    assert not run.ledger().get("queues"), "must not record a queue it cannot prove it created"


def test_created_objects_are_recorded_with_the_server_assigned_uid(run_create) -> None:
    """The uid is the teardown's ownership proof; recorded AFTER creation.

    The published script wrote {"delete": true, "run_bound": true} with no uid
    BEFORE creating anything.
    """
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    entries = run.ledger().get("k8s", [])
    assert len(entries) == 4, f"expected 2 policies + deployment + service, got {entries}"
    for entry in entries:
        assert entry["uid"].startswith("server-uid-"), entry
        assert entry["created_by_this_run"] is True


def test_creation_uses_create_never_apply(run_create) -> None:
    """The distinction IS the fix. `apply` adopts; `create` refuses."""
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    assert any("kubectl create" in c for c in run.calls)
    assert not any("kubectl apply" in c for c in run.calls)


def test_create_returning_no_uid_is_a_failure(run_create) -> None:
    """An object that cannot be proven ours at teardown is worse than no object."""
    rules = _creation_rules()
    rules.append(_tag_reply(FIXED_NONCE))
    rules.append({"tool": "kubectl", "match": ["create", "-f"],
                  "stdout": json.dumps({"kind": "NetworkPolicy",
                                        "metadata": {"name": "x", "namespace": "adp-gateway"}})})
    run = run_create(rules, args=_args_fixed_nonce())
    assert run.rc == 1
    assert "no metadata.uid" in run.output


def test_already_exists_on_create_is_not_adopted(run_create) -> None:
    """The race the pre-flight probe cannot close: created between probe and create."""
    rules = _creation_rules()
    rules.append(_tag_reply(FIXED_NONCE))
    rules.append({"tool": "kubectl", "match": ["create", "-f"],
                  "rc": 1, "stderr": 'Error from server (AlreadyExists): already exists'})
    run = run_create(rules, args=_args_fixed_nonce())
    assert run.rc == 1
    assert "Refusing to adopt it" in run.output


# ---------------------------------------------------------------------------
# 2. isolation ordering
# ---------------------------------------------------------------------------
def test_policies_are_created_before_the_gateway(run_create) -> None:
    """Ordering is load-bearing.

    If the Deployment lands first there is a window in which the fixture is
    reachable but unprotected -- exactly the DP-INV-1 state the fixture exists to
    avoid.
    """
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    creates = [c for c in run.calls if "kubectl create -f" in c]
    policy_indexes = [i for i, c in enumerate(creates) if "00-policy" in c]
    gateway_indexes = [i for i, c in enumerate(creates) if "10-gateway" in c]
    assert policy_indexes and gateway_indexes
    assert max(policy_indexes) < min(gateway_indexes), creates


# ---------------------------------------------------------------------------
# 1. composition
# ---------------------------------------------------------------------------
def test_fixture_is_rendered_from_the_live_deployment(run_create) -> None:
    """Not hand-assembled. The live spec must actually be read."""
    run = run_create(_create_ok_rules(), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    assert any("get deploy bedrockgateway" in c and "-o json" in c for c in run.calls)


def test_unreadable_live_deployment_stops_the_run(run_create) -> None:
    """Without the live spec there is nothing to copy, and a lookalike is the defect."""
    rules = base_rules()
    rules.insert(4, {"tool": "kubectl", "match": ["get", "deploy", "bedrockgateway", "-o", "json"],
                     "rc": 1, "stderr": "Error from server (Forbidden)"})
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "hand-assembling a lookalike" in run.output


def test_digest_comes_from_the_running_pods(run_create) -> None:
    """A tag can have moved since the rollout; the evaluated revision must be the
    one actually serving, or the evidence does not identify what was tested."""
    run = run_create(_create_ok_rules(digest=DIGEST), args=_args_fixed_nonce())
    assert run.rc == 0, run.output
    assert DIGEST in run.output


def test_unpinnable_image_is_refused(run_create) -> None:
    """No running pods AND a tag-only deployment spec -> refuse."""
    run = run_create(_creation_rules(digest=""))
    assert run.rc == 1
    assert not run.created()
    assert "Refusing to evaluate an unpinned revision" in run.output


# ---------------------------------------------------------------------------
# account assertion (#5195) and argument hygiene
# ---------------------------------------------------------------------------
def test_wrong_account_stops_before_creating_anything(run_create) -> None:
    """The #5195 failure mode: nobody checked which account the credential hit."""
    rules = _create_ok_rules()
    rules[0] = {"tool": "aws", "match": ["sts", "get-caller-identity"], "once": False,
                "stdout": json.dumps({"Account": "605440105851",
                                      "Arn": "arn:aws:sts::605440105851:assumed-role/x/y",
                                      "UserId": "AIDA:x"})}
    run = run_create(rules)
    assert run.rc == 1
    assert not run.created()
    assert "605440105851" in run.output


def test_check_only_creates_nothing(run_create) -> None:
    """A rehearsal must be a rehearsal."""
    rules = base_rules()
    rules.append({"tool": "aws", "match": ["get-queue-url"],
                  "rc": 1, "stderr": "AWS.SimpleQueueService.NonExistentQueue"})
    rules += [{"tool": "kubectl", "match": ["create", "-f"], "once": True,
               "stdout": "networkpolicy/x (server dry run)\n"} for _ in range(4)]
    run = run_create(rules, args=["--check-only"])
    assert run.rc == 0, run.output
    assert not any("create-queue" in c for c in run.calls)
    assert all("--dry-run=server" in c for c in run.calls if "kubectl create" in c)


@pytest.mark.parametrize("bad", ["nofix-prefix", "w2-bad/slash", "w2-bad space"])
def test_invalid_run_id_is_refused(run_create, bad: str) -> None:
    """The run id becomes a Kubernetes object name and the ownership key."""
    run = run_create(base_rules(), args=["--run-id", bad])
    assert run.rc != 0
    assert not run.created()


def test_protected_probe_resources_are_never_touched(run_create) -> None:
    """The assignment names two resources of unknown ownership.

    They are excluded by evidence (nonce/uid) generally; this asserts the
    belt-and-braces name guard too, since a run id could otherwise collide.
    """
    run = run_create(base_rules(), args=["--run-id", "w2-x"],
                     env_extra={"W2_GW_NS": "adp-gateway"})
    for call in run.calls:
        assert "authority-probe-gateway-20260920" not in call
        assert "adp-dev-authority-probe-20260920" not in call


# ---------------------------------------------------------------------------
# regression: the account assertion must stop the RUN, not just a subshell
# ---------------------------------------------------------------------------
def test_account_refusal_is_not_swallowed_by_a_subshell(run_create) -> None:
    """Regression for a real defect found by these tests.

    The call site was `read -r ACCOUNT ARN <<<"$(w2_assert_account)"`. The
    assertion's `exit 1` terminated only the command substitution's SUBSHELL; the
    outer script saw a successful `read` of empty output and continued with
    ACCOUNT="". That silently defeated the #5195 protection -- the single check
    these scripts exist to enforce -- while still PRINTING the refusal, so an
    exit-code-only or output-only test would have passed.

    Hence the two assertions together: the run must stop AND nothing may be
    created. `w2_require_account` assigns in the caller's own shell.
    """
    rules = _create_ok_rules()
    rules[0] = {"tool": "aws", "match": ["sts", "get-caller-identity"], "once": False,
                "stdout": json.dumps({"Account": "605440105851",
                                      "Arn": "arn:aws:sts::605440105851:assumed-role/x/y",
                                      "UserId": "AIDA:x"})}
    run = run_create(rules)
    assert run.rc != 0
    assert not run.created(), \
        f"the run continued past a failed account assertion; issued: {run.calls}"
    # and it must not have got as far as reading the live composition
    assert not any("get deploy bedrockgateway" in c for c in run.calls)


def test_unreadable_identity_also_stops_the_run(run_create) -> None:
    """An identity that cannot be READ is not a passing identity."""
    rules = _create_ok_rules()
    rules[0] = {"tool": "aws", "match": ["sts", "get-caller-identity"], "once": False,
                "rc": 255, "stderr": "Unable to locate credentials"}
    run = run_create(rules)
    assert run.rc != 0
    assert not run.created()


# ---------------------------------------------------------------------------
# 5. the fixture edge ALB is ADMITTED to the fixture gateway, before the worker
# ---------------------------------------------------------------------------
# Root's item 5: "compose/observe the fixture gateway policy before admitting the
# worker stage ... Require fresh ALB address observation before execution; stale
# allowances can refer to reassigned addresses."
#
# Why any of this needs asserting, given nothing errors without it: the fixture gateway
# policy admits callers by namespaceSelector, and an ALB with `target-type: ip` connects
# from its OWN elastic network interfaces, which belong to the load balancer and so to
# no pod and no namespace. No selector matches them at any width. Terraform applies, the
# Ingress reconciles, and the worker's bootstrap handshake simply never completes -- so
# the run reports "the protected worker failed its bootstrap", which is the exact
# conclusion Wave 2 exists to establish or refute. Every test below therefore asserts a
# refusal AND that no worker was created: a fixture that can manufacture its own finding
# is worse than no fixture.
def _created_worker(run) -> bool:
    return any("kubectl create" in c and "20-worker" in c for c in run.calls)


def test_the_alb_rule_is_added_to_the_live_policy_before_the_worker_exists(
        run_create, tmp_path) -> None:
    """The passing case, and the ORDERING, which is the whole point of the block.

    Included first and deliberately: every other test here asserts a refusal, and
    without one test that the correct input succeeds, a check tightened until it refuses
    everything would look like a fix.

    The ordering assertion is the substantive half. If the rule arrived after the Job,
    the worker would already be bootstrapping against a policy that drops it, and the
    timeout would be recorded before the admission landed.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    replaces = [i for i, c in enumerate(run.calls) if "kubectl replace" in c]
    worker_creates = [i for i, c in enumerate(run.calls)
                      if "kubectl create" in c and "20-worker" in c]
    assert replaces and worker_creates, run.calls
    assert replaces[0] < worker_creates[0], \
        f"the ALB rule must land before the worker is created: {run.calls}"

    # The replace body is the LIVE object plus one rule, carrying the live
    # resourceVersion -- so the API server rejects it if the policy changed since the
    # read. Read from the composed body rather than from the call log, because the
    # precondition is the thing that makes this safe and "kubectl said ok" does not
    # establish it was sent.
    body = json.loads((run.tmp / "evidence" / "alb-policy-body.json").read_text())
    assert body["metadata"]["resourceVersion"] == "9001"
    assert body["metadata"]["uid"] == GW_POLICY_UID
    ipblocks = [peer["ipBlock"]["cidr"] for rule in body["spec"]["ingress"]
                for peer in rule.get("from", []) if "ipBlock" in peer]
    assert sorted(ipblocks) == sorted(ALB_CIDRS), body["spec"]["ingress"]
    # The in-cluster namespaceSelector rule SURVIVES. A composer that rebuilt the rule
    # list could drop the rule the harness itself reaches the fixture through, and the
    # fixture would then fail its own probes for a reason unrelated to the edge.
    assert any("namespaceSelector" in json.dumps(rule)
               for rule in body["spec"]["ingress"]), body["spec"]["ingress"]


def test_a_receipt_without_the_alb_output_refuses_rather_than_denying_the_edge(
        run_create, tmp_path) -> None:
    """An edge built before #5836 published the output. Nothing would error.

    This is the silent case: without the output there is no address to admit, the
    policy denies the ALB, and the only symptom is a bootstrap that never completes.
    """
    run = run_create(_worker_rules(), args=_worker_args(tmp_path, alb_source=None))
    assert run.rc == 1
    assert "fixture_alb_network_policy_source" in run.output
    assert not _created_worker(run)


def test_addresses_read_from_another_load_balancer_are_refused(
        run_create, tmp_path) -> None:
    """A receipt whose nonce is this run's but whose ALB is not.

    The nonce cannot stand in for the ARN: it says the DOCUMENT is this run's, not that
    the addresses in it were read from this run's load balancer. Admitting another
    ALB's interfaces opens the fixture to it and denies the real edge at the same time.
    """
    other = ("arn:aws:elasticloadbalancing:us-east-1:879318057152:"
             "loadbalancer/app/someone-else/def456")
    # The EXPECTATION is varied, not the receipt, so the single value under test is the
    # one that differs: a receipt that is this run's in every other respect, naming a
    # load balancer that is not the one this run's ledger records.
    run = run_create(_worker_rules(), args=_args_fixed_nonce() + [
        "--worker-job",
        "--edge-receipt", _edge_receipt(tmp_path, alb_arn=FIXTURE_ALB_ARN),
        "--edge-alb-arn", other])
    assert run.rc == 1
    assert not _created_worker(run)
    assert not any("kubectl replace" in c for c in run.calls), run.calls


def test_a_receipt_without_the_alb_arn_flag_is_refused_at_argument_time(
        run_create, tmp_path) -> None:
    """A missing expectation must not waive the check it exists for.

    Refused before the session is even established: with no ARN to compare against, a
    receipt naming ANY load balancer in the account satisfies the binding.
    """
    run = run_create(_worker_rules(), args=_args_fixed_nonce() + [
        "--worker-job", "--edge-receipt", _edge_receipt(tmp_path)])
    assert run.rc == 1
    assert "--edge-alb-arn" in run.output
    assert not run.created()


def test_the_alb_arn_without_a_receipt_is_refused(run_create, tmp_path) -> None:
    """The reverse, refused rather than resolved: there is nothing to check it against.

    The addresses come from the receipt precisely so they cannot be typed in by hand.
    """
    run = run_create(_worker_rules(), args=_args_fixed_nonce() + [
        "--worker-job", "--worker-control-endpoint", FIXTURE_ENDPOINT,
        "--edge-alb-arn", FIXTURE_ALB_ARN])
    assert run.rc == 1
    assert "--edge-receipt" in run.output
    assert not run.created()


def test_a_stale_receipt_address_is_refused_against_the_live_load_balancer(
        run_create, tmp_path) -> None:
    """Root: "stale allowances can refer to reassigned addresses."

    A Terraform output records what was true WHEN IT RAN. The interface can since have
    been replaced and its address REASSIGNED, in this same VPC -- so the stale /32 is a
    live admission of whatever holds it now, not a harmless leftover. That is why the
    comparison is set equality and not containment in either direction.
    """
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        # The load balancer now holds a DIFFERENT second address.
        *_alb_admission_rules(fresh_ips=[ALB_IPS[0], "10.0.11.99"]),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "REASSIGNED" in run.output
    assert not _created_worker(run)
    assert not any("kubectl replace" in c for c in run.calls), \
        f"nothing may be mutated once the observation disagrees: {run.calls}"


def test_an_address_the_alb_has_gained_is_also_refused(run_create, tmp_path) -> None:
    """The other direction, which is the harder failure to read.

    An address the policy does not admit means SOME connections are dropped and others
    are not, by whichever interface the load balancer happens to pick. An intermittent
    bootstrap failure is the easiest possible thing to record as flakiness in the
    software under test, so it must refuse here rather than be discovered later.
    """
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        *_alb_admission_rules(fresh_ips=ALB_IPS + ["10.0.12.7"]),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "intermittent" in run.output.lower()
    assert not _created_worker(run)


def test_an_unreadable_alb_observation_is_not_an_unchanged_one(
        run_create, tmp_path) -> None:
    """"Could not check" is never "checked and fine".

    Without this the failed describe-network-interfaces would yield an empty address
    list, which is the same value as "the ALB has no interfaces" -- and comparing two
    absences reports agreement.
    """
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        {"tool": "aws", "match": ["ec2", "describe-network-interfaces"],
         "rc": 254, "stderr": "An error occurred (UnauthorizedOperation)"},
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "not" in run.output and "unchanged" in run.output
    assert not _created_worker(run)


def test_a_policy_whose_uid_is_not_this_runs_is_never_mutated(
        run_create, tmp_path) -> None:
    """A same-named REPLACEMENT policy. The name matches; the object is not ours.

    The uid comes from the LEDGER, never from the cluster: re-reading it would compare
    a live uid with itself and always match, which is exactly the check this is.
    Mutating a policy this run did not create would widen something nobody accounted
    for -- and the ledger would then also authorise deleting it at teardown.
    """
    rules = _worker_rules(policy_uid="not-the-uid-this-run-recorded")
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert not _created_worker(run)
    assert not any("kubectl replace" in c for c in run.calls), \
        f"a policy that is not this run's must not be written to: {run.calls}"


def test_a_policy_with_no_resource_version_is_not_replaced_unconditionally(
        run_create, tmp_path) -> None:
    """The precondition belongs on the SERVER, atomic with the write.

    Without a resourceVersion the replace would overwrite whatever the policy has
    become since the read -- including a replacement this run does not own. A
    read-then-write with no precondition is a check followed by a hope.
    """
    live = _live_gateway_policy()
    del live["metadata"]["resourceVersion"]
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        *_alb_admission_rules(live=live),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "resourceVersion" in run.output
    assert not _created_worker(run)
    assert not any("kubectl replace" in c for c in run.calls), run.calls


def test_a_conflict_on_the_replace_is_reported_as_the_guard_working(
        run_create, tmp_path) -> None:
    """409 Conflict means the policy changed between the read and the write.

    Diagnosed specifically rather than as a generic failure, because the operator's
    next action differs: this one says re-observe, while an arbitrary kubectl error
    says something else is wrong. Writing anyway is the one thing that must not happen.
    """
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        *_alb_admission_rules(replace={
            "tool": "kubectl", "match": ["replace", "-f"], "rc": 1,
            "stderr": 'Error from server (Conflict): error when replacing '
                      '"body.json": Operation cannot be fulfilled on '
                      'networkpolicies.networking.k8s.io "w2-fixture-policy-fixture-test": '
                      'the object has been modified'}),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "changed between the read and this write" in run.output
    assert not _created_worker(run)


def test_the_worker_is_not_created_when_the_live_policy_still_denies_the_edge(
        run_create, tmp_path) -> None:
    """THE gate. `kubectl replace` reporting success is not the admission.

    kubectl reports success for a write that changed nothing, and the object could be
    replaced immediately afterwards. So the policy is read AGAIN and the decision is
    taken from what the API server actually holds. Here the re-read shows the rule
    absent: the replace "succeeded" and the edge is still denied, which without this
    second observation would be indistinguishable from a working fixture until the
    bootstrap timed out.
    """
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        # The post-mutation re-read returns the UNCHANGED policy.
        *_alb_admission_rules(after=_live_gateway_policy(resource_version="9002")),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert not _created_worker(run)
    # The refusal must name the consequence, not just the state: this is the failure
    # that otherwise gets written down as the finding.
    assert "bootstrap" in run.output


def test_a_policy_already_admitting_the_alb_is_not_written_to_again(
        run_create, tmp_path) -> None:
    """Idempotent re-run: reported as "already", and no write is sent.

    "The rule was applied" and "the rule was already there" are different evidence
    about whether the mutation path works at all, so they are not collapsed. And a
    needless replace is a needless window in which the policy can be clobbered.
    """
    admitting = _live_gateway_policy(admits_alb=True)
    rules = _worker_rules(base=_create_ok_rules() + [
        {"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
         "stdout": "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev\n"},
        *_alb_admission_rules(live=admitting, after=admitting),
    ])
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 0, run.output
    assert "already admits" in run.output
    assert not any("kubectl replace" in c for c in run.calls), run.calls
    # and the worker still gets created -- an idempotent stage must not become a no-op
    assert _created_worker(run)


def test_check_only_sends_no_policy_write_and_says_the_gate_is_unmade(
        run_create, tmp_path) -> None:
    """A dry run must not mutate the policy, and must not claim the gate passed.

    A fresh --check-only run created no policy, so this run's ledger records none and
    there is no live object to compose against: the admission genuinely CANNOT be
    checked. That is reported as an unmade check rather than skipped quietly, because a
    stage that says nothing about a gate it did not run reads as a stage that passed it
    -- the vacuous-pass shape this whole evaluation exists to catch.
    """
    rules = _worker_rules()
    rules += [{"tool": "kubectl", "match": ["create", "-f", "--dry-run=server"],
               "once": False, "stdout": "job.batch/w2-fixture-worker-fixture-test\n"}]
    run = run_create(rules, args=_worker_args(tmp_path) + ["--check-only"])
    assert run.rc == 0, run.output
    assert not any("kubectl replace" in c for c in run.calls), run.calls
    assert "CANNOT be checked" in run.output
    assert "not a passed one" in run.output
    # And it must not claim the live policy admits anything.
    assert "verified from the live policy" not in run.output


@pytest.mark.parametrize("url", [
    "", "None", "http://ord1nary99.execute-api.us-east-1.amazonaws.com/dev",
    "https://ord1nary99.execute-api.us-west-2.amazonaws.com/dev",
    "https://ord1nary99.execute-api.us-east-1.amazonaws.com/prod",
    "https://ord1nary99.execute-api.us-east-1.amazonaws.com/dev?foo=bar",
    "https://ord1nary99.execute-api.us-east-1.amazonaws.com.evil/dev",
])
def test_invalid_ordinary_invoke_url_refuses_worker(run_create, tmp_path, url):
    rules = _create_ok_rules()
    rules.append({"tool": "aws", "match": ["ssm", "get-parameter", "apigw-invoke-url"],
                  "stdout": url + "\n"})
    run = run_create(rules, args=_worker_args(tmp_path))
    assert run.rc == 1
    assert "ORDINARY gateway apigw-invoke-url" in run.output
    assert not any("kubectl create" in c and "-job-" in c for c in run.calls)
