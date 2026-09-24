# =============================================================================
# Tests for the fixture internal ALB composition — Issue #5836
# =============================================================================
# WHAT THESE COVER AND WHY
# ------------------------
# ../fixture-alb.yaml.tmpl and ../scripts/create-fixture-alb.sh supply the fixture
# ALB that nothing previously created (#3968's renderer emits no Ingress). The
# properties below are the ones the trusted-edge argument actually rests on, so
# each is asserted rather than described:
#
#   * run-bound     — name, labels and the AdpFixtureRun tag carry this run only,
#                     because main.tf's run_binding_gate REFUSES to plan against an
#                     ALB that is not tagged for the run
#   * internal      — a public ALB in front of a header-trusting gateway would let
#                     the fixture pod be addressed directly, bypassing the edge
#   * reachable     — the reused VPC Link's security group has restricted egress,
#                     so the ALB must reuse a permitted group
#   * refuses       — foreign namespace, adoption of an existing object, untagged
#                     or public resolved ALB, and a failed ledger write all stop
#   * never adopts  — `kubectl create`, never `apply`
#
# These run with FAKE kubectl/aws/ledger binaries on PATH. They make NO cloud or
# cluster calls: this component is under review, not deployed, and a test that
# needed live credentials could not be part of the CI gate this issue also owes.
# =============================================================================

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent
SCRIPT = COMPONENT / "scripts" / "create-fixture-alb.sh"
TEMPLATE = COMPONENT / "fixture-alb.yaml.tmpl"

RUN_ID = "w2-5836-a1b2c3"
RUN_NONCE = "a1b2c3d4e5f60718"
NAMESPACE = "adp-gateway"
SERVICE = "bedrockgw-w2fx-fixture"
# The group the reused VPC Link is already permitted to reach on tcp/80 (dev).
PERMITTED_SG = "sg-0623ec399f4a20b87"
EXPECTED_NAME = f"bedrockgw-w2fx-{RUN_ID}"
FIXTURE_DNS = "internal-k8s-fixture-abc-123.us-east-1.elb.amazonaws.com"
FIXTURE_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:879318057152:"
    "loadbalancer/app/k8s-fixture-abc/0123456789abcdef"
)

# --- fake binaries ----------------------------------------------------------
# Scenario knobs come from the environment so one fake serves every case. Each
# invocation is appended to $FAKE_LOG, which is how "never uses kubectl apply" and
# "does not record on the adoption path" are asserted rather than assumed.

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
args = sys.argv[1:]
log = os.environ["FAKE_LOG"]
with open(log, "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")

def die(msg, code=1):
    sys.stderr.write(msg + "\n"); sys.exit(code)

if args[:1] == ["get"] and args[1] == "service":
    if os.environ.get("FAKE_SERVICE_MISSING") == "1":
        die('Error from server (NotFound): services "%s" not found' % args[2])
    sys.exit(0)

if args[:1] == ["get"] and args[1] == "ingress":
    if os.environ.get("FAKE_ALB_NEVER_READY") == "1":
        print(""); sys.exit(0)
    print(os.environ["FAKE_ALB_DNS"]); sys.exit(0)

if args[:1] == ["create"]:
    manifest = args[args.index("-f") + 1]
    # Hand the rendered manifest to the test for inspection.
    dest = os.environ.get("FAKE_MANIFEST_COPY")
    if dest:
        pathlib.Path(dest).write_text(pathlib.Path(manifest).read_text())
    if "--dry-run=server" in args:
        if os.environ.get("FAKE_DRYRUN_REJECT") == "1":
            die("Error from server: admission webhook denied the request")
        print("ingress.networking.k8s.io/%s" % os.environ["FAKE_EXPECTED_NAME"])
        sys.exit(0)
    if os.environ.get("FAKE_ALREADY_EXISTS") == "1":
        die('Error from server (AlreadyExists): ingresses.networking.k8s.io "x" already exists')
    uid = "" if os.environ.get("FAKE_NO_UID") == "1" else os.environ["FAKE_UID"]
    print(json.dumps({"kind": "Ingress", "metadata": {"uid": uid}}))
    sys.exit(0)

die("fake kubectl: unhandled %r" % (args,))
'''

FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("aws " + " ".join(args) + "\n")
q = ""
if "--query" in args:
    q = args[args.index("--query") + 1]

if args[:2] == ["sts", "get-caller-identity"]:
    print(os.environ.get("FAKE_ACCOUNT", "879318057152")); sys.exit(0)

if args[:2] == ["elbv2", "describe-tags"]:
    print(os.environ.get("FAKE_ALB_TAG", os.environ["FAKE_RUN_NONCE"])); sys.exit(0)

if args[:2] == ["elbv2", "describe-load-balancers"]:
    if "LoadBalancerArn" in q:
        print(os.environ.get("FAKE_ALB_ARN", "")); sys.exit(0)
    if "Scheme" in q:
        print(os.environ.get("FAKE_ALB_SCHEME", "internal")); sys.exit(0)
    if "SecurityGroups" in q:
        print('["%s"]' % os.environ["FAKE_PERMITTED_SG"]); sys.exit(0)
    if "VpcId" in q:
        print("vpc-0d6115bead9301d25"); sys.exit(0)

sys.stderr.write("fake aws: unhandled %r\n" % (args,)); sys.exit(1)
'''

FAKE_OWNERSHIP = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("ownership " + " ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_LEDGER_FAIL") == "1":
    sys.stderr.write("ledger write failed\n"); sys.exit(1)
# Mirror the real record-k8s side effect: append the recorded object.
idx = sys.argv.index("--ledger")
with open(sys.argv[idx + 1], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(0)
'''


@pytest.fixture
def harness(tmp_path):
    """Fake kubectl/aws/ownership on PATH; returns a runner and the call log."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("kubectl", FAKE_KUBECTL), ("aws", FAKE_AWS)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    ownership = tmp_path / "ownership.py"
    ownership.write_text(FAKE_OWNERSHIP)
    ownership.chmod(0o755)

    log = tmp_path / "calls.log"
    log.write_text("")
    manifest_copy = tmp_path / "rendered.yaml"
    ledger = tmp_path / "ledger.json"
    ledger.write_text("")

    def run(extra_env=None, args=None):
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env.update(
            W2_OWNERSHIP_LIB=str(ownership),
            FAKE_LOG=str(log),
            FAKE_MANIFEST_COPY=str(manifest_copy),
            FAKE_UID="11111111-2222-3333-4444-555555555555",
            FAKE_ALB_DNS=FIXTURE_DNS,
            FAKE_ALB_ARN=FIXTURE_ARN,
            FAKE_RUN_NONCE=RUN_NONCE,
            FAKE_PERMITTED_SG=PERMITTED_SG,
            FAKE_EXPECTED_NAME=EXPECTED_NAME,
            # Keep the timeout branch fast; the real defaults are 60x10s.
            W2_ALB_WAIT_ATTEMPTS="2",
            W2_ALB_WAIT_INTERVAL="0",
        )
        env.update(extra_env or {})
        argv = [
            str(SCRIPT),
            "--run-id", RUN_ID,
            "--run-nonce", RUN_NONCE,
            "--ledger", str(ledger),
            "--namespace", NAMESPACE,
            "--service", SERVICE,
            "--alb-security-groups", PERMITTED_SG,
        ] + (args or [])
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)

    harness.run = run
    harness.log = log
    harness.manifest = manifest_copy
    harness.ledger = ledger
    return harness


def rendered(harness) -> dict:
    assert harness.manifest.exists(), "the script never handed a manifest to kubectl"
    return yaml.safe_load(harness.manifest.read_text())


# =============================================================================
# The composition itself
# =============================================================================

def test_creates_an_internal_alb_only(harness):
    """A public ALB would let the header-trusting fixture pod be addressed directly."""
    r = harness.run()
    assert r.returncode == 0, r.stderr
    ann = rendered(harness)["metadata"]["annotations"]
    assert ann["alb.ingress.kubernetes.io/scheme"] == "internal"


def test_is_bound_to_this_run(harness):
    """Name, labels and the ownership tag must name THIS run.

    main.tf's run_binding_gate refuses to plan unless the discovered ALB carries
    AdpFixtureRun = run_nonce, so an untagged or foreign-tagged ALB is not merely
    untidy — the edge cannot be built against it at all.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    doc = rendered(harness)
    assert doc["metadata"]["name"] == EXPECTED_NAME
    assert doc["metadata"]["labels"]["adp.io/w2-fixture"] == RUN_ID
    assert doc["metadata"]["labels"]["adp.io/w2-nonce"] == RUN_NONCE

    tags = dict(
        kv.split("=", 1)
        for kv in doc["metadata"]["annotations"]["alb.ingress.kubernetes.io/tags"].split(",")
    )
    assert tags["AdpFixtureRun"] == RUN_NONCE
    assert tags["Disposable"] == "true"


def test_reuses_a_security_group_the_vpc_link_can_reach(harness):
    """A fresh controller-created group would pass every other check, then time out.

    Setting the security-groups annotation is what stops the controller creating a
    frontend group the reused VPC Link has no egress rule for.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    ann = rendered(harness)["metadata"]["annotations"]
    assert ann["alb.ingress.kubernetes.io/security-groups"] == PERMITTED_SG
    assert ann["alb.ingress.kubernetes.io/manage-backend-security-group-rules"] == "true"


def test_listens_only_on_the_port_the_vpc_link_permits(harness):
    r = harness.run()
    assert r.returncode == 0, r.stderr
    ann = rendered(harness)["metadata"]["annotations"]
    assert json.loads(ann["alb.ingress.kubernetes.io/listen-ports"]) == [{"HTTP": 80}]


def test_serves_only_the_internal_plane_to_the_fixture_service(harness):
    r = harness.run()
    assert r.returncode == 0, r.stderr
    rules = rendered(harness)["spec"]["rules"]
    paths = [p for rule in rules for p in rule["http"]["paths"]]
    assert [p["path"] for p in paths] == ["/internal"]
    assert paths[0]["backend"]["service"]["name"] == SERVICE
    assert paths[0]["backend"]["service"]["port"]["number"] == 80


def test_no_placeholder_survives_into_the_manifest(harness):
    """Checked on the PARSED object, not the file text, so the explanatory comments
    in the template (which legitimately name the placeholders) cannot mask a real
    unsubstituted token in a name, label or tag."""
    r = harness.run()
    assert r.returncode == 0, r.stderr
    flat = json.dumps(rendered(harness))
    assert not re.search(r"__[A-Z_]+__", flat), f"unsubstituted placeholder in {flat}"


def test_template_placeholders_are_all_substituted_by_the_script(harness):
    """Every placeholder the template declares must be one the script substitutes.

    Drift here is silent: a new __TOKEN__ in the template would reach the API server
    literally, and a k8s object carrying '__RUN_NONCE__' as its tag would then be
    invisible to both the Terraform gate and teardown.
    """
    declared = set(re.findall(r"__[A-Z_]+__", TEMPLATE.read_text()))
    substituted = set(re.findall(r'"(__[A-Z_]+__)"', SCRIPT.read_text()))
    assert declared == substituted, f"template/script placeholder drift: {declared ^ substituted}"


# =============================================================================
# Refusals
# =============================================================================

def test_refuses_when_the_fixture_service_does_not_exist(harness):
    """Otherwise the ALB comes up with no healthy target and reads as a broken fixture."""
    r = harness.run({"FAKE_SERVICE_MISSING": "1"})
    assert r.returncode != 0
    assert "10-create-fixture.sh FIRST" in r.stderr
    assert "kubectl create" not in harness.log.read_text()


def test_refuses_to_adopt_an_existing_object_and_records_nothing(harness):
    """Adoption would put an object this run did not create into the delete-authorised ledger."""
    r = harness.run({"FAKE_ALREADY_EXISTS": "1"})
    assert r.returncode != 0
    assert "REFUSING to adopt" in r.stderr
    assert "ownership " not in harness.log.read_text()
    assert harness.ledger.read_text() == ""


def test_never_uses_kubectl_apply(harness):
    """`apply` would mutate a pre-existing same-named object instead of failing.

    Asserted on the kubectl VERBS rather than on the whole log, because paths in
    the log can contain the substring 'apply' and a test that passes for the wrong
    reason is worse than no test.
    """
    harness.run()
    verbs = [
        line.split()[1]
        for line in harness.log.read_text().splitlines()
        if line.startswith("kubectl ")
    ]
    assert "create" in verbs
    assert "apply" not in verbs


def test_refuses_an_alb_whose_ownership_tag_is_not_this_run(harness):
    """The Terraform gate would refuse later; failing here keeps the ledger honest."""
    r = harness.run({"FAKE_ALB_TAG": "deadbeefdeadbeef"})
    assert r.returncode != 0
    assert "AdpFixtureRun" in r.stderr
    # Already created, so it MUST be in the ledger for teardown to reach it.
    assert "Ingress" in harness.ledger.read_text()
    assert "teardown will remove it" in r.stderr


def test_refuses_an_alb_that_resolved_as_internet_facing(harness):
    r = harness.run({"FAKE_ALB_SCHEME": "internet-facing"})
    assert r.returncode != 0
    assert "not internal" in r.stderr
    assert "Ingress" in harness.ledger.read_text()


def test_fails_loudly_when_the_ledger_write_fails(harness):
    """A created-but-unrecorded object is the one teardown can never prove is ours."""
    r = harness.run({"FAKE_LEDGER_FAIL": "1"})
    assert r.returncode != 0
    assert "could NOT record it in the ledger" in r.stderr
    assert "kubectl delete ingress" in r.stderr


def test_fails_when_the_server_returns_no_uid(harness):
    r = harness.run({"FAKE_NO_UID": "1"})
    assert r.returncode != 0
    assert "no metadata.uid" in r.stderr
    assert harness.ledger.read_text() == ""


def test_alb_provisioning_timeout_is_reported_as_recorded(harness):
    r = harness.run({"FAKE_ALB_NEVER_READY": "1"})
    assert r.returncode != 0
    assert "no ALB hostname appeared" in r.stderr
    assert "IN THE LEDGER" in r.stderr


# =============================================================================
# Read-only mode
# =============================================================================

def test_check_only_creates_nothing_and_records_nothing(harness):
    """The mode a non-deploying review can legitimately exercise."""
    r = harness.run(args=["--check-only"])
    assert r.returncode == 0, r.stderr
    calls = harness.log.read_text()
    assert "--dry-run=server" in calls
    assert "ownership " not in calls
    assert harness.ledger.read_text() == ""


def test_check_only_surfaces_a_server_rejection(harness):
    r = harness.run({"FAKE_DRYRUN_REJECT": "1"}, args=["--check-only"])
    assert r.returncode != 0
    assert "server dry-run rejected" in r.stderr


# =============================================================================
# Reported outputs
# =============================================================================

def test_reports_the_discovered_arn_for_terraform_and_not_a_dns_input(harness):
    """fixture_alb_dns is deliberately NOT an input; main.tf reads it from the ARN."""
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert f'fixture_alb_arn = "{FIXTURE_ARN}"' in r.stdout
    assert "fixture_alb_dns is NOT an input" in r.stdout
    assert "expected_vpc_id" in r.stdout


def test_rejects_a_non_80_port_rather_than_widening_a_shared_group(harness):
    r = harness.run(args=["--port", "8443"])
    assert r.returncode != 0
    assert "SHARED VPC Link security group" in r.stderr
