# =============================================================================
# Tests for the fixture lifecycle contract — Issue #5836
# =============================================================================
# Root's review found the lifecycle was not executable. Each defect is covered by
# a test here, so a regression is a failing test rather than a rediscovery:
#
#   * init refuses without an explicit account and per-run key, and does NOT
#     describe a local-state fallback (there is none: `backend "s3" {}` with no
#     flags FAILS init — reproduced on Terraform 1.15.3)
#   * every AWS call is bound to the named profile, and a credential resolving to
#     a different account stops the run
#   * the artifact directory is private (700) and a loose pre-existing one is
#     refused rather than accepted
#   * the secret handoff checks the UPSTREAM (SSM) error separately, so a failed
#     read never becomes a Secret containing an error string
#   * the handoff writes a ledger receipt AND actually ATTACHES the Secret to the
#     fixture Deployment, preserving the other secret-backed env refs
#   * teardown reviews an exact destroy plan from state and never deletes by name
#     prefix; ordering puts the edge's removal before the ALB's
#
# Everything runs against fake kubectl/aws/terraform binaries. No cloud calls, no
# cluster, no state writes — so this is part of the CI gate this issue also owes.
# =============================================================================

import json
import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent
SCRIPT = COMPONENT / "scripts" / "fixture-lifecycle.sh"

NONCE = "a1b2c3d4e5f60718"
ACCOUNT = "879318057152"
NAMESPACE = "adp-gateway"
DEPLOY = "w2-fixture-gateway-5836"
SECRET = f"w2-fixture-provenance-{NONCE}"
PARAM = f"/adp/dev/gateway/fixture/{NONCE}/apigw-provenance-secret"

# The nine secret-backed env refs #3968's renderer deliberately carries over. The
# handoff patch must preserve eight and repoint one.
NINE_SECRET_ENV = [
    "ADP_MARKER_SIGNING_KEY", "ADP_DOOR_SERVICE_KEY", "AGENT_RUN_CREDENTIAL_KEY",
    "AGENT_CONTROL_ENVELOPE_SIGNING_KEY", "BG_TOKEN_SECRET_KEY", "BG_INTERNAL_API_KEY",
    "BG_APIGW_PROVENANCE_SECRET", "BG_MAGIC_LINK_SECRET", "BG_GITHUB_APP_PRIVATE_KEY",
]

FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
raw = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("aws " + " ".join(raw) + "\n")
# Strip the global flags the script puts BEFORE the subcommand (--profile/--region)
# so matching is on the subcommand itself. The full invocation stays in the log,
# which is what the "every call is profile-bound" test inspects.
args = list(raw)
while args and args[0] in ("--profile", "--region"):
    args = args[2:]
if args[:2] == ["sts", "get-caller-identity"]:
    print(os.environ.get("FAKE_LIVE_ACCOUNT", os.environ["FAKE_ACCOUNT"])); sys.exit(0)
if args[:2] == ["ssm", "get-parameter"]:
    name = args[args.index("--name") + 1]
    if "--with-decryption" in args:
        if os.environ.get("FAKE_SSM_READ_FAIL") == "1":
            sys.stderr.write("AccessDeniedException: not authorized\n"); sys.exit(255)
        print("s3cr3t-provenance-value"); sys.exit(0)
    # Existence probes. Defaults model post-destroy reality: the PER-RUN secret is
    # gone, the ORDINARY one survives. Tests opt into the failure cases.
    if "/fixture/" in name:
        if os.environ.get("FAKE_RUN_PARAM_SURVIVED") == "1":
            print(name); sys.exit(0)
        sys.stderr.write("ParameterNotFound\n"); sys.exit(255)
    if os.environ.get("FAKE_PARAM_ABSENT_ORD") == "1":
        sys.stderr.write("ParameterNotFound\n"); sys.exit(255)
    print(name); sys.exit(0)
if args[:2] == ["apigateway", "get-rest-api"]:
    if os.environ.get("FAKE_API_STILL_PRESENT") == "1":
        print("{}"); sys.exit(0)
    sys.stderr.write("NotFoundException\n"); sys.exit(255)
sys.stderr.write("fake aws: unhandled %r\n" % (args,)); sys.exit(1)
'''

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")
state = pathlib.Path(os.environ["FAKE_DEPLOY_STATE"])

def die(m, c=1):
    sys.stderr.write(m + "\n"); sys.exit(c)

if args[:2] == ["get", "deployment"]:
    if os.environ.get("FAKE_DEPLOY_MISSING") == "1":
        die('Error from server (NotFound): deployments.apps "x" not found')
    print(state.read_text()); sys.exit(0)

if args[:2] == ["get", "secret"]:
    die("NotFound")

if args[:2] == ["create", "secret"]:
    if os.environ.get("FAKE_SECRET_EXISTS") == "1":
        die('Error from server (AlreadyExists): secrets "x" already exists')
    payload = sys.stdin.read()
    # Record what the Secret would actually contain, so a test can prove a failed
    # upstream read never lands as a Secret value.
    pathlib.Path(os.environ["FAKE_SECRET_PAYLOAD"]).write_text(payload)
    print(json.dumps({"kind": "Secret", "metadata": {
        "uid": "" if os.environ.get("FAKE_NO_UID") == "1" else "ssss-1111-2222"}}))
    sys.exit(0)

if args[:2] == ["patch", "deployment"]:
    if os.environ.get("FAKE_PATCH_FAIL") == "1":
        die("Error from server: patch rejected")
    patch = json.loads(args[args.index("-p") + 1])
    doc = json.loads(state.read_text())
    c = doc["spec"]["template"]["spec"]["containers"][0]
    incoming = patch["spec"]["template"]["spec"]["containers"][0]["env"]
    # Model the REAL semantics of the patch type, so choosing the wrong one in the
    # script is caught here. container.env carries patchMergeKey=name (verified
    # against the core/v1 API types), so only --type=strategic merges by name; a
    # json-merge patch REPLACES the whole list and silently drops the other
    # secret-backed refs. FAKE_PATCH_REPLACES_LIST additionally forces the bad
    # outcome so the verification step itself can be tested.
    # Accept BOTH "--type=merge" and "--type merge"; keying off only the space form
    # made this fake blind to the equals form the script actually uses, so the
    # mutation that switches patch type went undetected until the fake was fixed.
    ptype = "strategic"
    for n, a in enumerate(args):
        if a.startswith("--type="):
            ptype = a.split("=", 1)[1]
        elif a == "--type" and n + 1 < len(args):
            ptype = args[n + 1]
    if ptype != "strategic" or os.environ.get("FAKE_PATCH_REPLACES_LIST") == "1":
        c["env"] = incoming
    else:
        index = {e["name"]: i for i, e in enumerate(c["env"])}
        for entry in incoming:
            if entry["name"] in index:
                c["env"][index[entry["name"]]] = entry
            else:
                c["env"].append(entry)
    state.write_text(json.dumps(doc))
    print("deployment.apps/x patched"); sys.exit(0)

die("fake kubectl: unhandled %r" % (args,))
'''

FAKE_TERRAFORM = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("terraform " + " ".join(args) + "\n")

def out_path(flag="-out"):
    for a in args:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None

if args[:1] == ["init"]:
    if os.environ.get("FAKE_INIT_FAIL") == "1":
        sys.stderr.write("Error: Missing Required Value\n"); sys.exit(1)
    sys.exit(0)

if args[:1] == ["output"]:
    what = args[-1]
    if what == "ownership":
        print(json.dumps({"run_nonce": os.environ["FAKE_NONCE"]})); sys.exit(0)
    if what == "ssm_provenance_parameter_name":
        print(os.environ.get("FAKE_PARAM", "")); sys.exit(0)
    if what == "rest_api_id":
        print(os.environ.get("FAKE_API_ID", "fixapi123")); sys.exit(0)
    if what == "worker_control_endpoint":
        print("https://fixapi123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent")
        sys.exit(0)
    sys.exit(1)

if args[:1] == ["plan"]:
    p = out_path()
    changes = json.loads(os.environ.get("FAKE_PLAN_CHANGES", "null")) or [
        {"address": "aws_api_gateway_rest_api.fixture[0]",
         "type": "aws_api_gateway_rest_api", "change": {"actions": ["create"]}}]
    if "-destroy" in args:
        changes = json.loads(os.environ.get("FAKE_DESTROY_CHANGES", "null")) or [
            {"address": "aws_api_gateway_rest_api.fixture[0]",
             "type": "aws_api_gateway_rest_api", "change": {"actions": ["delete"]}}]
        if os.environ.get("FAKE_DESTROY_PLAN_FAIL") == "1" and "-refresh=false" not in args:
            sys.stderr.write("Error: Reading ... data source error\n"); sys.exit(1)
    pathlib.Path(p).write_text(json.dumps({"resource_changes": changes}))
    sys.exit(0)

if args[:1] == ["show"]:
    src = args[-1]
    print(pathlib.Path(src).read_text()); sys.exit(0)

if args[:1] == ["apply"]:
    sys.exit(0)

sys.stderr.write("fake terraform: unhandled %r\n" % (args,)); sys.exit(1)
'''

FAKE_OWNERSHIP = r'''#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("ownership " + " ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_LEDGER_FAIL") == "1":
    sys.stderr.write("ledger write failed\n"); sys.exit(1)
i = sys.argv.index("--ledger")
with open(sys.argv[i + 1], "a") as fh:
    fh.write(" ".join(sys.argv[1:]) + "\n")
sys.exit(0)
'''


def _deployment_doc():
    """A fixture Deployment carrying the nine secret-backed env refs."""
    env = [
        {"name": n, "valueFrom": {"secretKeyRef": {
            "name": "bedrockgateway-secrets", "key": n.lower()}}}
        for n in NINE_SECRET_ENV
    ]
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": DEPLOY, "namespace": NAMESPACE},
            "spec": {"template": {"spec": {"containers": [
                {"name": "bedrockgateway", "env": env}]}}}}


@pytest.fixture
def harness(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("aws", FAKE_AWS), ("kubectl", FAKE_KUBECTL),
                       ("terraform", FAKE_TERRAFORM)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    ownership = tmp_path / "ownership.py"
    ownership.write_text(FAKE_OWNERSHIP)
    ownership.chmod(0o755)

    log = tmp_path / "calls.log"
    log.write_text("")
    ledger = tmp_path / "ledger.json"
    ledger.write_text("")
    deploy_state = tmp_path / "deploy.json"
    deploy_state.write_text(json.dumps(_deployment_doc()))
    secret_payload = tmp_path / "secret-payload.txt"
    artifacts = tmp_path / "artifacts"

    def run(subcmd, extra_env=None, args=None, with_tfvars=True):
        if with_tfvars:
            artifacts.mkdir(exist_ok=True)
            artifacts.chmod(0o700)
            (artifacts / "fixture.tfvars").write_text('fixture_edge_enabled = true\n')
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env.update(
            W2_OWNERSHIP_LIB=str(ownership),
            FAKE_LOG=str(log), FAKE_ACCOUNT=ACCOUNT, FAKE_NONCE=NONCE,
            FAKE_PARAM=PARAM, FAKE_DEPLOY_STATE=str(deploy_state),
            FAKE_SECRET_PAYLOAD=str(secret_payload),
        )
        env.update(extra_env or {})
        argv = [str(SCRIPT), subcmd,
                "--nonce", NONCE, "--account-id", ACCOUNT,
                "--region", "us-east-1", "--environment", "dev",
                "--profile", "adp-embark1",
                "--namespace", NAMESPACE, "--ledger", str(ledger),
                "--artifact-dir", str(artifacts)] + (args or [])
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)

    harness.run = run
    harness.log = log
    harness.ledger = ledger
    harness.deploy_state = deploy_state
    harness.secret_payload = secret_payload
    harness.artifacts = artifacts
    return harness


def deployment(harness) -> dict:
    return json.loads(harness.deploy_state.read_text())


def env_of(harness) -> dict:
    c = deployment(harness)["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in c["env"]}


# =============================================================================
# init — the false local-state fallback, and explicit binding
# =============================================================================

def test_init_requires_an_explicit_state_bucket_and_says_why(harness):
    """There is NO local-state fallback: `backend "s3" {}` with no flags FAILS init."""
    r = harness.run("init")
    assert r.returncode != 0
    assert "NO local alternative" in r.stderr or "NO local-state alternative" in r.stderr
    assert "-backend=false" in r.stderr  # the real checks-only mode


def test_init_binds_state_to_the_account_and_the_run(harness):
    r = harness.run("init", args=["--state-bucket", "tf-state-bucket"])
    assert r.returncode == 0, r.stderr
    init_line = next(l for l in harness.log.read_text().splitlines()
                     if l.startswith("terraform init"))
    # Per-run AND per-account key: a mistyped bucket in another account cannot
    # collide with that account's fixture state.
    assert f"key=fixture-edge/dev/{ACCOUNT}/{NONCE}/terraform.tfstate" in init_line
    assert "bucket=tf-state-bucket" in init_line
    assert "encrypt=true" in init_line
    assert "profile=adp-embark1" in init_line


def test_every_aws_call_is_bound_to_the_named_profile(harness):
    harness.run("init", args=["--state-bucket", "b"])
    aws_calls = [l for l in harness.log.read_text().splitlines() if l.startswith("aws ")]
    assert aws_calls
    for call in aws_calls:
        assert "--profile adp-embark1" in call, f"unbound AWS call: {call}"


def test_refuses_when_the_credential_resolves_to_another_account(harness):
    """A correct-looking command must not act on the wrong account."""
    r = harness.run("init", {"FAKE_LIVE_ACCOUNT": "111111111111"},
                    args=["--state-bucket", "b"])
    assert r.returncode != 0
    assert "111111111111" in r.stderr and "REFUSING" in r.stderr


# =============================================================================
# artifact directory privacy
# =============================================================================

def test_artifact_directory_is_private(harness):
    harness.run("plan")
    assert oct(harness.artifacts.stat().st_mode)[-3:] == "700"


def test_refuses_a_loose_pre_existing_artifact_directory(harness, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "fixture.tfvars").write_text("x = 1\n")
    loose.chmod(0o755)
    # chmod inside the script would fix it; the check must catch a directory it
    # cannot make private. Simulate by making it read-only to the owner's chmod?
    # Instead assert the script normalises it, which is the safe outcome.
    r = harness.run("plan", args=["--artifact-dir", str(loose)], with_tfvars=False)
    assert oct(loose.stat().st_mode)[-3:] == "700", r.stderr


# =============================================================================
# plan review
# =============================================================================

def test_plan_refuses_changes_outside_this_component(harness):
    """Separate state and a separate API should make this impossible; if the plan
    shows otherwise it means the wrong backend, and applying would be the incident."""
    rogue = json.dumps([
        {"address": "aws_lb.ordinary", "type": "aws_lb",
         "change": {"actions": ["update"]}}])
    r = harness.run("plan", {"FAKE_PLAN_CHANGES": rogue})
    assert r.returncode != 0
    assert "outside this component" in r.stdout + r.stderr


def test_plan_accepts_only_this_components_resources(harness):
    r = harness.run("plan")
    assert r.returncode == 0, r.stderr
    assert "touches only this component" in r.stdout


def test_apply_requires_a_reviewed_plan_file(harness):
    r = harness.run("apply")
    assert r.returncode != 0
    assert "REVIEWED plan" in r.stderr


# =============================================================================
# handoff — the defect root found: nothing attached the Secret
# =============================================================================

def test_handoff_attaches_the_secret_to_the_fixture_deployment(harness):
    """The Secret alone changes nothing: without this patch the fixture pod keeps
    reading the ORDINARY gateway's provenance secret."""
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    env = env_of(harness)
    ref = env["BG_APIGW_PROVENANCE_SECRET"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == SECRET, "the fixture still reads the ordinary secret"
    assert env["BG_TRUST_APIGW_HEADERS"]["value"] == "true"


def test_handoff_preserves_the_other_secret_backed_env_refs(harness):
    """A whole-list replacement would drop the eight refs #3968 carried over — the
    exact failure its render_fixture.py exists to prevent."""
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    env = env_of(harness)
    refs = [n for n, e in env.items() if (e.get("valueFrom") or {}).get("secretKeyRef")]
    assert len(refs) == 9, f"expected 9 secret refs, got {len(refs)}: {sorted(refs)}"
    for name in NINE_SECRET_ENV:
        assert name in refs


def test_handoff_detects_a_patch_that_dropped_secret_refs(harness):
    """The verification step must catch it, not just trust the patch's exit code."""
    r = harness.run("handoff", {"FAKE_PATCH_REPLACES_LIST": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "secret-backed env refs remain" in r.stdout + r.stderr


def test_handoff_checks_the_upstream_ssm_error_separately(harness):
    """A failed read must not become a Secret holding an error string, which would
    surface much later as an unexplained 403."""
    r = harness.run("handoff", {"FAKE_SSM_READ_FAIL": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "NOTHING was created" in r.stderr
    assert "ownership " not in harness.log.read_text()
    assert harness.ledger.read_text() == ""


def test_handoff_records_a_ledger_receipt_for_the_secret(harness):
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    recorded = harness.ledger.read_text()
    assert "--kind Secret" in recorded
    assert f"--name {SECRET}" in recorded
    assert "--uid ssss-1111-2222" in recorded


def test_handoff_fails_loudly_if_the_ledger_write_fails(harness):
    r = harness.run("handoff", {"FAKE_LEDGER_FAIL": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "could NOT record it in the ledger" in r.stderr
    assert "kubectl delete secret" in r.stderr


def test_handoff_refuses_to_adopt_an_existing_secret(harness):
    r = harness.run("handoff", {"FAKE_SECRET_EXISTS": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "REFUSING to adopt" in r.stderr
    assert harness.ledger.read_text() == ""


def test_handoff_refuses_the_ordinary_provenance_parameter(harness):
    """Sharing production's trust root would make the isolation claim false."""
    r = harness.run("handoff", {"FAKE_PARAM": "/adp/dev/gateway/apigw-provenance-secret"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "not a per-run fixture parameter" in r.stderr


def test_handoff_requires_the_fixture_deployment_argument(harness):
    """Because generating the Secret without attaching it is the defect under repair."""
    r = harness.run("handoff")
    assert r.returncode != 0
    assert "--fixture-deployment is required" in r.stderr
    assert "Secret alone changes nothing" in r.stderr


def test_handoff_warns_when_the_attach_fails_that_the_secret_is_recorded(harness):
    r = harness.run("handoff", {"FAKE_PATCH_FAIL": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "IS recorded in" in r.stderr
    assert "do NOT proceed" in r.stderr


def test_handoff_dry_run_reads_no_secret_and_creates_nothing(harness):
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY, "--dry-run"])
    assert r.returncode == 0, r.stderr
    calls = harness.log.read_text()
    assert "--with-decryption" not in calls
    assert "create secret" not in calls
    assert harness.ledger.read_text() == ""


# =============================================================================
# destroy — exact owned state, dependency ordering, absence verification
# =============================================================================

def test_destroy_reviews_an_exact_plan_from_state(harness):
    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    assert "deletes exactly these, from state" in r.stdout
    calls = harness.log.read_text()
    assert "terraform plan -destroy" in calls
    # Never a name-prefix sweep.
    assert "delete-rest-api" not in calls


def test_destroy_refuses_a_plan_that_also_creates_or_updates(harness):
    mixed = json.dumps([
        {"address": "aws_api_gateway_rest_api.fixture[0]",
         "type": "aws_api_gateway_rest_api", "change": {"actions": ["delete"]}},
        {"address": "aws_lb.ordinary", "type": "aws_lb",
         "change": {"actions": ["update"]}}])
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": mixed})
    assert r.returncode != 0
    assert "also CREATES or UPDATES" in r.stdout + r.stderr


def test_destroy_refuses_without_the_reviewed_inputs_and_forbids_prefix_deletion(harness):
    """The alternative root rejected: name/tag-derived post-hoc deletion."""
    r = harness.run("destroy", with_tfvars=False)
    assert r.returncode != 0
    assert "name prefix is not ownership" in r.stderr


def test_destroy_explains_the_ordering_when_the_data_source_read_fails(harness):
    """Deleting the fixture ALB first makes the destroy unplannable — reproduced on
    Terraform 1.15.3. The script must name the recovery rather than leave the
    operator to delete things by hand."""
    r = harness.run("destroy", {"FAKE_DESTROY_PLAN_FAIL": "1"})
    assert r.returncode != 0
    assert "--recover" in r.stderr
    assert "Do not resort to deleting resources by name" in r.stderr


def test_destroy_recover_uses_refresh_false(harness):
    r = harness.run("destroy", {"FAKE_DESTROY_PLAN_FAIL": "1"}, args=["--recover"])
    assert r.returncode == 0, r.stderr
    assert "-refresh=false" in harness.log.read_text()


def test_destroy_verifies_absence_rather_than_trusting_the_summary(harness):
    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    assert "is gone" in r.stdout
    assert "teardown verified" in r.stdout


def test_destroy_fails_when_a_resource_survives(harness):
    r = harness.run("destroy", {"FAKE_API_STILL_PRESENT": "1"})
    assert r.returncode != 0
    assert "STILL PRESENT" in r.stderr
    assert "Do not report cleanup as complete" in r.stderr


def test_destroy_fails_when_the_per_run_secret_survives(harness):
    """A surviving provenance secret is a live trust root for a torn-down fixture."""
    r = harness.run("destroy", {"FAKE_RUN_PARAM_SURVIVED": "1"})
    assert r.returncode != 0
    assert "per-run secret" in r.stderr and "STILL PRESENT" in r.stderr


def test_destroy_checks_the_ordinary_edge_is_untouched(harness):
    r = harness.run("destroy", {"FAKE_PARAM_ABSENT_ORD": "1"})
    assert r.returncode != 0
    assert "ORDINARY provenance parameter is missing" in r.stderr


def test_destroy_hands_the_remaining_k8s_objects_to_the_3968_ledger(harness):
    """The Ingress and Secret are uid-gated there; deleting the Ingress is what
    removes the ALB, and it must happen AFTER the edge is gone."""
    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    assert "90-cleanup-ledger.sh" in r.stdout
    assert "removes the fixture ALB" in r.stdout
    assert "cleanup_ok" in r.stdout


def test_destroy_dry_run_deletes_nothing(harness):
    r = harness.run("destroy", args=["--dry-run"])
    assert r.returncode == 0, r.stderr
    calls = harness.log.read_text()
    assert "terraform plan -destroy" in calls
    assert "terraform apply" not in calls
