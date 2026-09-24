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


def published_paths(harness):
    """{path: pathType} for every rule the rendered Ingress publishes."""
    rules = rendered(harness)["spec"]["rules"]
    paths = [p for rule in rules for p in rule["http"]["paths"]]
    return {p["path"]: p["pathType"] for p in paths}


def test_serves_the_internal_plane_to_the_fixture_service(harness):
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert published_paths(harness).get("/internal") == "Prefix"
    rules = rendered(harness)["spec"]["rules"]
    internal = [
        p
        for rule in rules
        for p in rule["http"]["paths"]
        if p["path"] == "/internal"
    ][0]
    assert internal["backend"]["service"]["name"] == SERVICE
    assert internal["backend"]["service"]["port"]["number"] == 80


def test_publishes_the_human_session_paths_the_edge_actually_routes(harness):
    """The edge has TWO routes; serving only /internal made one of them dead.

    ../main.tf forwards `/{proxy+}` at auth NONE to this same ALB for human
    sessions. When the ALB published only /internal, a human request that API
    Gateway correctly admitted reached the listener's default action and came back
    404 -- indistinguishable, from the operator's side, from "the human plane is
    refusing me". So the human-plane control could not be demonstrated at all.

    The paths are the ones the pod's routers really mount: /me/budget is served by
    the PREFIX-LESS APIRouter in src/budget/me_routes.py (so the prefix to publish
    is /me, and it is #3968's session probe endpoint), and src/auth/routes.py is
    APIRouter(prefix="/auth").
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    for human in ("/me", "/auth"):
        assert paths.get(human) == "Prefix", (
            f"{human} is not published, so the auth-NONE human route forwards here "
            f"and 404s: {sorted(paths)}"
        )


def test_publishes_liveness_as_an_exact_path_for_the_human_positive_control(harness):
    """`verify` needs one unauthenticated path proving the human plane reaches the
    pod: observing only refusals cannot distinguish a correctly-restricted edge
    from one that refuses everything.

    Exact, not Prefix: /health and /ready are single routes at the app root
    (src/app.py:411-418), and a Prefix rule would also admit everything below them.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    assert paths.get("/health") == "Exact", sorted(paths)
    assert paths.get("/ready") == "Exact", sorted(paths)


# =============================================================================
# The ACCEPTANCE endpoints, read from the code that calls them
# =============================================================================
# The published set was previously argued from `/me/budget` alone, and it was
# incomplete: the merged #5825 evaluator and #3968's seed-and-count call
# agent-control and stats endpoints that had no rule here, so each reached the
# ALB's default action and returned 404. A 404 is not a soft failure for those
# collectors -- it is recorded as a failed control-plane probe, so the acceptance
# evidence this component exists to make obtainable was unobtainable.
#
# These tests derive the required paths from the CALLERS' own source rather than
# restating them, so a rename on either side fails here instead of surfacing as an
# unexplained 404 during a live run. Where a caller file is absent (a different
# checkout state) the test SKIPS with the reason rather than passing quietly -- a
# silent pass is how the original omission survived.

GATEWAY_SRC = COMPONENT.parent.parent / "src"
REPO_ROOT = COMPONENT.parents[3]
EVALUATOR = REPO_ROOT / "platform" / "scripts" / "agent-control-eval.py"
SEED_AND_COUNT = (
    REPO_ROOT / "platform" / "scripts" / "operator" / "wave2" / "31-seed-and-count.py"
)


def _source(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path} is not present in this checkout — cannot trace its callers")
    return path.read_text()


def _matches(path: Path, pattern: str) -> set[str]:
    return set(re.findall(pattern, _source(path)))


def _covers(paths: dict, request_path: str) -> bool:
    """Whether the published rule set would route `request_path`, by ALB semantics.

    Exact matches the whole path; Prefix matches on SEGMENT boundaries (so /me does
    not match /membership). Implemented here rather than assumed because the whole
    point of these tests is that a rule which does not actually cover the requested
    path is a 404.
    """
    for rule, kind in paths.items():
        if kind == "Exact" and request_path == rule:
            return True
        if kind == "Prefix" and (request_path == rule
                                 or request_path.startswith(rule.rstrip("/") + "/")):
            return True
    return False


def test_publishes_every_control_path_the_merged_evaluator_calls(harness):
    """#5825's evaluator declares BOTH HTTP adapters onto the one control service
    (platform/scripts/agent-control-eval.py, ADAPTERS) and six of its checks iterate
    `ADAPTERS.items()`.

    So publishing only `/activity/...` would leave half of every one of those checks
    404ing -- and since the pairing exists precisely to prove "the two HTTP edges did
    not drift", a 404 on one side does not weaken the check, it voids it.
    """
    templates = _matches(EVALUATOR, r'"(/(?:activity|orchestration)/[^"]*)"')
    assert templates, (
        "no control path templates found in the evaluator — the pattern this test "
        "traces with has drifted from its source")

    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)

    for template in sorted(templates):
        # Substitute the evaluator's own placeholders with values of the shape it
        # sends, so what is checked is a REQUEST PATH and not a template string.
        request = (template
                   .replace("{run_id}", "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")
                   .replace("{verb}", "pause"))
        assert _covers(paths, request), (
            f"{request} (from the evaluator's {template}) is not routed by "
            f"{sorted(paths)} — it would hit the ALB default action and 404, and the "
            f"evaluator records that as a failed control-plane probe")


def test_publishes_both_control_adapters_and_not_just_the_activity_one(harness):
    """Stated separately and by name: the original omission was not 'a path was
    missed' but 'only one adapter was considered', and a set-equality test elsewhere
    would not say which half is missing."""
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    run = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    for request in (f"/activity/invocations/{run}/agent/ping",
                    f"/activity/invocations/{run}/agent/state",
                    f"/activity/invocations/{run}/agent/pause",
                    f"/orchestration/runs/{run}/ping",
                    f"/orchestration/runs/{run}/state",
                    f"/orchestration/runs/{run}/pause"):
        assert _covers(paths, request), f"{request} is not routed by {sorted(paths)}"


# The path #3968's 31-seed-and-count.py:166 requests, recorded here as a constant.
#
# It is a constant rather than only a grep because that script lives on #3968's
# branch and is absent from this checkout, so a test that could only read the file
# would SKIP here and in CI -- and a skipped requirement is indistinguishable from a
# met one, which is how the original omission survived review. The requirement is
# therefore asserted unconditionally below, and the grep is a SEPARATE cross-check
# that runs when the file is present.
SEED_AND_COUNT_READBACK = "/admin/agent-run-stats"


def test_publishes_the_stats_endpoint_3968_reads_its_seeded_counts_back_through(harness):
    """#3968's 31-seed-and-count.py reads its seeded counts back through
    /admin/agent-run-stats (line 166, with ?days=..&tenant_id=..). A 404 there makes
    the seeded count unverifiable rather than wrong -- the run produces no usable
    evidence and the cause is on this side of the boundary.

    Unconditional: this must hold whether or not their branch is checked out here.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    assert _covers(paths, SEED_AND_COUNT_READBACK), (
        f"{SEED_AND_COUNT_READBACK} is not routed by {sorted(paths)}")
    # The query string must not be part of the rule: ALB path matching is on the path
    # only, so a rule carrying `?days=` would never match the request it was written
    # for.
    assert not any("?" in rule for rule in paths), sorted(paths)


def test_the_recorded_stats_path_still_matches_3968s_script_when_present(harness):
    """The cross-check for the constant above. Skips only when their branch is not
    checked out, and the requirement it guards is already asserted unconditionally --
    so a skip here loses a drift alarm, never the requirement itself.
    """
    urls = _matches(SEED_AND_COUNT, r'\{GATEWAY_URL\}(/[A-Za-z0-9/_.-]+)')
    assert urls, "no gateway URL found in 31-seed-and-count.py — the trace pattern drifted"
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    for url in sorted(urls):
        assert _covers(paths, url), (
            f"{url} (requested by 31-seed-and-count.py) is not routed by "
            f"{sorted(paths)} — their script now reads back through a path this ALB "
            f"does not publish")


def test_the_control_and_stats_paths_exist_on_the_pods_own_routers(harness):
    """The other direction: a rule for a path the pod does not serve would 404 too,
    just from the application instead of the ALB. Checked against the routers'
    declarations, since a rule invented from a caller's expectation is still a guess.

    src/activity/routes.py is PREFIX-LESS (APIRouter(tags=["activity"])), so its
    decorator paths are the full paths; src/orchestration/controls.py declares
    prefix="/orchestration", so its "/runs/..." decorators mount under that.
    """
    activity = _source(GATEWAY_SRC / "activity" / "routes.py")
    controls = _source(GATEWAY_SRC / "orchestration" / "controls.py")

    assert 'APIRouter(tags=["activity"])' in activity, (
        "src/activity/routes.py is no longer prefix-less — every path published for "
        "it must be re-derived")
    assert 'prefix="/orchestration"' in controls, (
        "src/orchestration/controls.py no longer declares prefix=/orchestration")

    for decorator in ('"/activity/invocations/{invocation_id}/agent/ping"',
                      '"/activity/invocations/{invocation_id}/agent/state"',
                      '"/activity/invocations/{invocation_id}/agent/{action}"',
                      '"/admin/agent-run-stats"'):
        assert decorator in activity, f"{decorator} is no longer served by the pod"
    for decorator in ('"/runs/{run_id}/ping"', '"/runs/{run_id}/state"',
                      '"/runs/{run_id}/pause"'):
        assert decorator in controls, f"/orchestration{decorator} is no longer served"


def test_admin_is_published_as_one_exact_path_not_as_a_prefix(harness):
    """`/admin` as a Prefix would publish every admin router the pod mounts --
    identity recovery, persona-model defaults and posture, bedrock routing,
    access-request approve/deny, member budgets -- none of which any acceptance step
    calls. Only the one stats path is needed, so only it is published.

    Same reasoning for `/orchestration`: scoped to `/orchestration/runs` so approval
    gates and node resume/recovery stay unpublished.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    assert "/admin" not in paths, (
        "a bare /admin prefix publishes every admin router on the fixture")
    assert paths.get("/admin/agent-run-stats") == "Exact", sorted(paths)
    assert "/orchestration" not in paths, (
        "a bare /orchestration prefix publishes approval gates and node resume")
    run = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    for unwanted in ("/admin/identity/recovery", "/admin/persona-models/default",
                     f"/orchestration/gates/{run}/approve",
                     f"/orchestration/nodes/{run}/resume"):
        assert not _covers(paths, unwanted), (
            f"{unwanted} is reachable on the fixture ALB but no acceptance step "
            f"calls it — that is surface for no gain")


def test_the_new_paths_do_not_widen_the_signed_internal_plane(harness):
    """Publishing human-plane paths must not make them addressable through the
    AWS_IAM route. It does not, and the reason is structural rather than a policy:
    the edge's internal integration forwards to `/internal/{proxy}` -- it PREPENDS
    the prefix -- so a signed caller's path always lands under /internal.

    Asserted against ../main.tf because it is the property that makes publishing
    these paths safe; if that integration ever changed to strip a prefix instead
    (which is what the ORDINARY edge's /agent/{proxy+} route does), this set would
    need re-reviewing before it stays as is.
    """
    main_tf = _source(COMPONENT / "main.tf")
    assert 'fixture_internal_forward_uri = "http://${local.fixture_alb_dns_discovered}/internal/{proxy}"' in main_tf, (
        "the internal integration no longer prepends /internal — a signed caller may "
        "now be able to address the human-plane paths published here, so the "
        "published set must be re-reviewed")


def test_never_publishes_a_catch_all_that_would_strip_sigv4_from_internal(harness):
    """The isolation argument is that /internal is reachable ONLY via the AWS_IAM
    route. A `/` Prefix rule here would forward every path the pod serves --
    /internal included -- to a target group the auth-NONE route also reaches, i.e.
    an unsigned path to the internal API. Enumerated prefixes, never a catch-all.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    paths = published_paths(harness)
    assert "/" not in paths, f"catch-all rule published: {sorted(paths)}"
    for path, kind in paths.items():
        assert path.startswith("/") and len(path) > 1, f"over-broad rule {path!r}"
        assert kind in ("Prefix", "Exact"), f"{path} has pathType {kind!r}"


def test_publishes_exactly_the_reviewed_path_set_and_nothing_else(harness):
    """Pinned as a SET, so adding a surface to the template is a test change and not
    a silent widening of what the fixture exposes. Every rule must also terminate at
    the fixture's own Service -- a rule pointing elsewhere would make the edge
    measure some other backend while the artifact says 'fixture'.
    """
    r = harness.run()
    assert r.returncode == 0, r.stderr
    assert published_paths(harness) == {
        "/internal": "Prefix",
        "/me": "Prefix",
        "/auth": "Prefix",
        "/activity/invocations": "Prefix",
        "/orchestration/runs": "Prefix",
        "/admin/agent-run-stats": "Exact",
        "/health": "Exact",
        "/ready": "Exact",
    }
    rules = rendered(harness)["spec"]["rules"]
    for p in (p for rule in rules for p in rule["http"]["paths"]):
        assert p["backend"]["service"]["name"] == SERVICE, p
        assert p["backend"]["service"]["port"]["number"] == 80, p


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
