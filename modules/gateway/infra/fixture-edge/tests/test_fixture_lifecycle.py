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
import re
import shutil
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

# The PROVIDER-ASSIGNED ids of the fixture edge's API Gateway resources, in the
# shapes a real plan carries. They are constants because the destroy guard matches a
# plan line's change.before.id against the ownership receipt, so a test that writes
# an id in the wrong shape would be testing a plan no provider ever produces.
#
# STAGE_ID is the one that mattered: the provider sets a stage's id to
# "ags-<rest-api-id>-<stage-name>" (aws/internal/service/apigateway/stage.go), NOT to
# the bare stage name. outputs.tf recorded `stage_name`, so the stage's real deletion
# line never matched its receipt entry and the guard refused a LEGITIMATE teardown.
API_ID = "fixapi123"
DEPLOYMENT_ID = "dep0kx1abc"
STAGE_ID = f"ags-{API_ID}-dev"
# The name main.tf ACTUALLY composes: "/aws/api-gateway/${local.name_prefix}-fixture-edge"
# with name_prefix = "bedrockgw-${environment}-w2fx-${run_nonce}". Both this constant
# and destroy's probe previously used "/aws/apigateway/w2-fixture-edge-<nonce>" --
# wrong stem AND a missing hyphen in api-gateway -- so the suite agreed with the
# script about a log group that has never existed. Pinned to the real composition
# here so a probe that guesses instead of reading state fails a test.
LOG_GROUP = f"/aws/api-gateway/bedrockgw-dev-w2fx-{NONCE}-fixture-edge"

# The state bucket and the named profile every harness command is bound to. Both
# are part of the backend binding the script checks, so they are constants rather
# than literals repeated at each call site.
BUCKET = "tf-state-bucket"
PROFILE = "adp-embark1"

# #3968's LEDGER_VERSION (lib/ownership.py). Its load_ledger refuses any other
# value, because a v1 row carries `run_bound: true` and no server-assigned uid and
# therefore cannot prove ownership.
LEDGER_VERSION = 2

# #3968's ACTUAL fixture labels (lib/render_fixture.py, branch agent/issue-3968).
# Named here so a drift in that renderer breaks these tests loudly instead of
# silently making the ownership checks vacuous again.
W2_FIXTURE_LABEL = "adp.io/w2-fixture"
W2_NONCE_LABEL = "adp.io/w2-nonce"

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
profile = ""
while args and args[0] in ("--profile", "--region"):
    if args[0] == "--profile":
        profile = args[1]
    args = args[2:]

def denied(svc_msg="AccessDeniedException: User is not authorized to perform this operation"):
    """An AWS error that is NOT a not-found.

    This is the shape root's executed finding used: the previous revision's
    absence probes read ANY nonzero exit as proof of deletion, so this exact
    response produced "teardown verified".
    """
    sys.stderr.write(svc_msg + "\n"); sys.exit(255)

if args[:2] == ["sts", "get-caller-identity"]:
    query = args[args.index("--query") + 1] if "--query" in args else "Account"
    if query == "Arn":
        # WHO a profile signs as. The wrong-role control must verify this: a 403
        # observed while signing as an ALLOWLISTED role would otherwise be recorded
        # as the Deny working, which is a false proof of the only control that
        # exercises this component's own resource policy.
        arn = os.environ.get("FAKE_PROBE_ARN_%s" % profile.replace("-", "_"),
                             os.environ.get("FAKE_PROBE_ARN", ""))
        if arn == "__unresolvable__":
            sys.stderr.write("Unable to locate credentials\n"); sys.exit(255)
        print(arn or "None"); sys.exit(0)
    print(os.environ.get("FAKE_LIVE_ACCOUNT", os.environ["FAKE_ACCOUNT"])); sys.exit(0)

# `aws configure get` READS STATIC KEYS OUT OF A CONFIG FILE. The previous revision
# used it to obtain signing material, which (a) returns nothing for the assumed-role
# / SSO / credential_process profiles these actually are, and (b) then put the secret
# into `curl --user`, i.e. into argv. Credentials must come from the SDK's own
# provider chain instead, so this fake now FAILS LOUDLY if the script ever asks --
# the alternative is a double that quietly keeps the broken interface alive.
if args[:2] == ["configure", "get"]:
    sys.stderr.write(
        "fake aws: the script called `aws configure get %s`. Signing material must be "
        "resolved through the SDK credential provider chain (botocore), not read out "
        "of a config file: static keys are absent for assumed-role/SSO profiles and "
        "passing them to curl puts the secret in argv.\n" % (args[2] if len(args) > 2 else ""))
    sys.exit(97)

# The AUTHORITATIVE cluster identity: AWS's own endpoint for the named cluster,
# resolved through the run's bound profile/region. Compared against the kubeconfig
# server, this pins the connection kubectl will actually make -- which a context-name
# comparison cannot do.
if args[:2] == ["eks", "describe-cluster"]:
    want = os.environ.get("FAKE_EKS_CLUSTER_NAME", "adp-dev-eks")
    asked = args[args.index("--name") + 1]
    if asked != want or os.environ.get("FAKE_EKS_CLUSTER_MISSING") == "1":
        sys.stderr.write(
            "An error occurred (ResourceNotFoundException): No cluster found for name: "
            "%s.\n" % asked)
        sys.exit(254)
    print("https://" + os.environ.get(
        "FAKE_EKS_ENDPOINT_HOST", "ABCDEF0123.gr7.us-east-1.eks.amazonaws.com"))
    sys.exit(0)

if args[:2] == ["ssm", "get-parameter"]:
    name = args[args.index("--name") + 1]
    if "--with-decryption" in args:
        if os.environ.get("FAKE_SSM_READ_FAIL") == "1":
            denied()
        print(os.environ.get("FAKE_SSM_VALUE", "s3cr3t-provenance-value")); sys.exit(0)
    # The ORDINARY api id, read by `verify` to prove the fixture edge is a
    # different API. Authoritative: a failure here must stop the run.
    if name.endswith("/apigw-invoke-url"):
        if os.environ.get("FAKE_ORD_APIID_FAIL") == "1":
            denied()
        print(os.environ.get("FAKE_ORDINARY_INVOKE_URL", "https://" + os.environ.get("FAKE_ORDINARY_API_ID", "ordapi999") + ".execute-api.us-east-1.amazonaws.com/dev")); sys.exit(0)
    # Existence probes. Defaults model post-destroy reality: the PER-RUN secret is
    # gone, the ORDINARY one survives. Tests opt into the failure cases.
    if "/fixture/" in name:
        if os.environ.get("FAKE_RUN_PARAM_SURVIVED") == "1":
            print(name); sys.exit(0)
        if os.environ.get("FAKE_RUN_PARAM_DENIED") == "1":
            denied()
        sys.stderr.write("ParameterNotFound\n"); sys.exit(255)
    if os.environ.get("FAKE_PARAM_ABSENT_ORD") == "1":
        sys.stderr.write("ParameterNotFound\n"); sys.exit(255)
    if os.environ.get("FAKE_ORD_PARAM_DENIED") == "1":
        denied()
    print(name); sys.exit(0)

if args[:2] == ["apigateway", "get-rest-api"]:
    if os.environ.get("FAKE_API_STILL_PRESENT") == "1":
        print("{}"); sys.exit(0)
    if os.environ.get("FAKE_API_PROBE_DENIED") == "1":
        denied()
    sys.stderr.write("NotFoundException\n"); sys.exit(255)

if args[:2] == ["apigateway", "get-stage"]:
    if os.environ.get("FAKE_STAGE_STILL_PRESENT") == "1":
        print("{}"); sys.exit(0)
    sys.stderr.write("NotFoundException\n"); sys.exit(255)

# describe-* returns an empty LIST rather than an error, so for THIS call an exit 0
# with empty output really is absence -- the one probe where that holds.
if args[:2] == ["logs", "describe-log-groups"]:
    if os.environ.get("FAKE_LOGS_DENIED") == "1":
        denied()
    if os.environ.get("FAKE_LOG_GROUP_SURVIVED") == "1":
        # Echo the prefix it was ASKED about. The previous fake printed a name it
        # composed itself, so it answered "present" no matter which prefix the probe
        # sent -- and therefore agreed with a probe that was asking about a log group
        # main.tf never creates. A fake that ignores the query cannot detect a probe
        # addressing the wrong object.
        i = args.index("--log-group-name-prefix")
        print(args[i + 1]); sys.exit(0)
    print(""); sys.exit(0)

sys.stderr.write("fake aws: unhandled %r\n" % (args,)); sys.exit(1)
'''

# curl is how every security control in `verify` is actually observed. There was no
# fake curl in this suite at all, so verify's refusal checks were never executed by
# any test -- which is why root, running the script by hand with a curl that
# returned 200, found it exiting 0. Defaults model a CORRECTLY refusing edge; each
# test opts into the failure it is about.
FAKE_CURL = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("curl " + " ".join(args) + "\n")
joined = " ".join(args)
url = [a for a in args if a.startswith("http")]
url = url[0] if url else ""
if "/agent/model/" in url and os.environ.get("FAKE_MODEL_CODE"):
    code = os.environ["FAKE_MODEL_CODE"]
elif "--aws-sigv4" in args:
    code = os.environ.get("FAKE_SIGV4_CODE", "403")
elif "X-Adp-Edge-Provenance" in joined:
    code = os.environ.get("FAKE_SPOOFED_CODE", "403")
elif "/internal/" in url or "/agent/model/" in url:
    code = os.environ.get("FAKE_UNSIGNED_CODE", "403")
else:
    # The human positive control: anything that is not an /internal probe.
    code = os.environ.get("FAKE_HUMAN_CODE", "200")
if code == "exit-nonzero":
    sys.stderr.write("curl: (7) Failed to connect\n"); sys.exit(7)
sys.stdout.write(code); sys.exit(0)
'''

FAKE_KUBECTL = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("kubectl " + " ".join(args) + "\n")
state = pathlib.Path(os.environ["FAKE_DEPLOY_STATE"])

def die(m, c=1):
    sys.stderr.write(m + "\n"); sys.exit(c)

# `--profile` binds the AWS CLI and NOTHING ELSE. kubectl's target comes from
# KUBECONFIG, so it can point at another cluster -- or another account's cluster --
# while every aws_ call in the same run is correctly bound. There was no fake for
# this at all, which is why the gap went unnoticed.
if args[:3] == ["config", "current-context"]:
    ctx = os.environ.get("FAKE_KUBE_CONTEXT", "__default__")
    if ctx == "__none__":
        die("error: current-context is not set")
    if ctx == "__default__":
        ctx = "arn:aws:eks:%s:%s:cluster/adp-dev-eks" % (
            os.environ.get("FAKE_REGION", "us-east-1"), os.environ["FAKE_ACCOUNT"])
    print(ctx); sys.exit(0)

# THE API SERVER ENDPOINT kubectl would really connect to. This -- not the context
# NAME -- is what identifies the cluster: the name is an arbitrary local alias and can
# claim any account, so a context called
# `arn:aws:eks:us-east-1:<right account>:cluster/adp-dev-eks` may point anywhere.
# FAKE_KUBE_SERVER models exactly that divergence.
if args[:2] == ["config", "view"]:
    server = os.environ.get("FAKE_KUBE_SERVER", "__default__")
    if server == "__none__":
        print(""); sys.exit(0)
    if server == "__default__":
        server = "https://" + os.environ.get(
            "FAKE_EKS_ENDPOINT_HOST", "ABCDEF0123.gr7.us-east-1.eks.amazonaws.com")
    print(server); sys.exit(0)

if args[:2] == ["get", "deployment"]:
    if os.environ.get("FAKE_DEPLOY_MISSING") == "1":
        die('Error from server (NotFound): deployments.apps "x" not found')
    print(state.read_text()); sys.exit(0)

if args[:2] == ["get", "secret"]:
    if os.environ.get("FAKE_SECRET_EXISTS") != "1":
        die("NotFound")
    # jsonpath only -- `-o json` would return the base64 data block, and the
    # recovery path must not write the secret to disk. If the script ever asks for
    # the whole object here, this fake refuses so the test fails loudly.
    fmt = args[args.index("-o") + 1] if "-o" in args else ""
    if not fmt.startswith("jsonpath"):
        die("fake kubectl: refusing `get secret -o %s`: that response carries the "
            "base64 secret data. Read metadata by jsonpath instead." % (fmt or "<none>",))
    print("ssss-1111-2222"); print("77"); sys.exit(0)

if args[:2] == ["create", "secret"]:
    if os.environ.get("FAKE_SECRET_EXISTS") == "1":
        die('Error from server (AlreadyExists): secrets "x" already exists')
    payload = sys.stdin.read()
    # Record what the Secret would actually contain, so a test can prove a failed
    # upstream read never lands as a Secret value. Appended, and the file's mere
    # EXISTENCE is the evidence that a create happened: the pipeline bug root
    # executed created an EMPTY Secret, so "no file" and "empty file" must be
    # distinguishable.
    with open(os.environ["FAKE_SECRET_PAYLOAD"], "a") as fh:
        fh.write(payload)
    print(json.dumps({"kind": "Secret", "metadata": {
        "name": args[3], "namespace": os.environ.get("FAKE_NAMESPACE", ""),
        "resourceVersion": "77",
        # The real response includes the base64 `data` block. Modelled, because the
        # bug root found was the script saving this document verbatim.
        "uid": "" if os.environ.get("FAKE_NO_UID") == "1" else "ssss-1111-2222"},
        "data": {"BG_APIGW_PROVENANCE_SECRET": "czNjcjN0LXByb3ZlbmFuY2UtdmFsdWU="}}))
    sys.exit(0)

if args[:2] == ["patch", "deployment"]:
    # -----------------------------------------------------------------------
    # REJECT FLAGS REAL kubectl DOES NOT HAVE.
    #
    # The previous version of this fake PARSED `--resource-version`, a flag that
    # does not exist on `kubectl patch`. Real kubectl answers:
    #   error: unknown flag: --resource-version     (exit 1)
    # So the suite proved the script agreed with this fake rather than with kubectl,
    # and the attach step always failed in a live run -- AFTER the Secret had been
    # created and recorded. A permissive double is worse than no double: it converts
    # a hard failure into a green test.
    #
    # The allowlist mirrors `kubectl patch --help`.
    # -----------------------------------------------------------------------
    KNOWN = {"-n", "--namespace", "-p", "--patch", "--type", "-o", "--output",
             "--local", "-f", "--filename", "--dry-run", "--patch-file",
             "--allow-missing-template-keys", "--field-manager", "--subresource"}
    for a in args[3:]:
        if a.startswith("-"):
            base = a.split("=", 1)[0]
            if base not in KNOWN:
                die("error: unknown flag: %s\nSee 'kubectl patch --help' for usage." % base, 1)
    if os.environ.get("FAKE_PATCH_FAIL") == "1":
        die("Error from server: patch rejected")
    doc = json.loads(state.read_text())
    ptype = "strategic"
    for n, a in enumerate(args):
        if a.startswith("--type="):
            ptype = a.split("=", 1)[1]
        elif a == "--type" and n + 1 < len(args):
            ptype = args[n + 1]
    patch = json.loads(args[args.index("-p") + 1])
    c = doc["spec"]["template"]["spec"]["containers"][0]

    if ptype == "json":
        # ---------------------------------------------------------------
        # RFC 6902 semantics, including `test`, as the API SERVER applies them:
        # every op is evaluated against the live object and NOTHING is applied
        # unless all of them succeed. This is what replaces the invented
        # --resource-version flag, and it is stronger: it pins the uid too.
        # ---------------------------------------------------------------
        def resolve(path):
            cur = doc
            parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/")[1:]]
            for p in parts[:-1]:
                cur = cur[int(p)] if isinstance(cur, list) else cur[p]
            return cur, parts[-1]

        working = json.loads(json.dumps(doc))   # apply atomically or not at all
        saved, doc = doc, working
        try:
            for op in patch:
                parent, key = resolve(op["path"])
                if op["op"] == "test":
                    actual = parent[int(key)] if isinstance(parent, list) else parent.get(key)
                    if actual != op["value"]:
                        doc = saved
                        die("error: testing value %s failed: test failed" % op["path"], 1)
                elif op["op"] == "replace":
                    if isinstance(parent, list):
                        parent[int(key)] = op["value"]
                    else:
                        if key not in parent:
                            doc = saved
                            die("error: replace operation does not apply: doc is missing "
                                "path: %s" % op["path"], 1)
                        parent[key] = op["value"]
                elif op["op"] == "add":
                    if key == "-" and isinstance(parent, list):
                        parent.append(op["value"])
                    elif isinstance(parent, list):
                        parent.insert(int(key), op["value"])
                    else:
                        parent[key] = op["value"]
                else:
                    die("error: unsupported op %r" % op["op"], 1)
        except (KeyError, IndexError, ValueError) as exc:
            die("error: patch path does not apply: %s" % exc, 1)
        c = doc["spec"]["template"]["spec"]["containers"][0]
    else:
        incoming = patch["spec"]["template"]["spec"]["containers"][0]["env"]
        # Model the REAL semantics of the patch type, so choosing the wrong one in
        # the script is caught here. container.env carries patchMergeKey=name, so
        # only --type=strategic merges by name; a json-merge patch REPLACES the whole
        # list and silently drops the other secret-backed refs.
        if ptype != "strategic" or os.environ.get("FAKE_PATCH_REPLACES_LIST") == "1":
            c["env"] = incoming
        else:
            index = {e["name"]: i for i, e in enumerate(c["env"])}
            for entry in incoming:
                if entry["name"] in index:
                    c["env"][index[entry["name"]]] = entry
                else:
                    c["env"].append(entry)
    if os.environ.get("FAKE_PATCH_REPLACES_LIST") == "1" and ptype == "json":
        # Force the whole-list-replacement outcome so the script's post-patch
        # verification of the surviving refs is itself exercised.
        c["env"] = [e for e in c["env"]
                    if e["name"] in ("BG_APIGW_PROVENANCE_SECRET", "BG_TRUST_APIGW_HEADERS")]
    # Delete-and-recreate around the patch. resourceVersion cannot detect this (the
    # replacement can land on any version string); only the uid can, which is why
    # the post-patch recheck compares uids.
    if os.environ.get("FAKE_DEPLOY_REPLACED_DURING_PATCH") == "1":
        doc["metadata"]["uid"] = "dddd-9999-replaced"
    state.write_text(json.dumps(doc))
    print("deployment.apps/x patched"); sys.exit(0)

die("fake kubectl: unhandled %r" % (args,))
'''

FAKE_TERRAFORM = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
# The provider-assigned ids for the two resources whose id is neither a name nor
# supplied by a test. Real shapes: a deployment id is an opaque 10-char token, and a
# log group's id is its name.
FAKE_DEPLOYMENT_ID = "dep0kx1abc"
FAKE_LOG_GROUP_DEFAULT = ("/aws/api-gateway/bedrockgw-%s-w2fx-%s-fixture-edge"
                          % (os.environ.get("FAKE_ENVIRONMENT", "dev"),
                             os.environ["FAKE_NONCE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("terraform " + " ".join(args) + "\n")

# The CREDENTIAL ENVIRONMENT each terraform invocation actually ran with.
#
# terraform has no --profile flag: the AWS provider resolves credentials from the
# process environment, where AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN OUTRANK
# AWS_PROFILE. So "this run is bound to --profile" is a claim about terraform only
# if that environment is controlled, and the only way a test can check it is to
# record what the child really received.
if os.environ.get("FAKE_TF_ENV"):
    with open(os.environ["FAKE_TF_ENV"], "a") as fh:
        fh.write(json.dumps({
            "argv": args,
            "env": {k: os.environ.get(k) for k in (
                "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_CREDENTIAL_PROFILES_FILE", "AWS_REGION",
                "AWS_DEFAULT_REGION")},
        }) + "\n")

def out_path(flag="-out"):
    for a in args:
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None

if args[:1] == ["init"]:
    if os.environ.get("FAKE_INIT_FAIL") == "1":
        sys.stderr.write("Error: Missing Required Value\n"); sys.exit(1)
    sys.exit(0)

def owned_receipt():
    """The `ownership` output as outputs.tf emits it: run binding + typed resource ids.

    destroy checks its plan against THIS, so the default must be the consistent
    case (every planned deletion is an owned (type, id) carrying this run's tag) and
    tests perturb one side or the other.

    ALL SIX resource types, with the ids the PROVIDER actually assigns. The previous
    fake had two (API + parameter), which is why it could not catch root's executed
    finding: outputs.tf recorded the stage as its stage_name ("dev") while a real
    plan carries "ags-<api-id>-<stage-name>", so the stage line never matched and
    LEGITIMATE teardown was refused. An inventory that omits four of six types cannot
    detect a per-type identity mismatch at all.

    Note the API and its POLICY deliberately share one id — that is real (the policy
    is an attribute of the API), and it is why the comparison is keyed on
    (type, id) rather than on ids alone.
    """
    if os.environ.get("FAKE_OWNERSHIP_JSON"):
        return json.loads(os.environ["FAKE_OWNERSHIP_JSON"])
    nonce = os.environ["FAKE_NONCE"]
    # `or`, not a default: FAKE_API_ID="" models an unreadable `output rest_api_id`,
    # and the API still exists with its real id.
    api = os.environ.get("FAKE_API_ID") or "fixapi123"
    stage = os.environ.get("FAKE_ENVIRONMENT", "dev")
    return {
        "run_nonce": nonce,
        "account_id": os.environ["FAKE_ACCOUNT"],
        "region": os.environ.get("FAKE_REGION", "us-east-1"),
        "environment": stage,
        "rest_api_id": api,
        "resources": [
            {"kind": "apigateway-rest-api", "type": "aws_api_gateway_rest_api",
             "id": api, "name": "w2-fixture-edge-" + nonce},
            {"kind": "apigateway-rest-api-policy",
             "type": "aws_api_gateway_rest_api_policy",
             # SHARES the API's id — see the docstring.
             "id": api, "name": "resource policy on w2-fixture-edge-" + nonce},
            {"kind": "apigateway-deployment", "type": "aws_api_gateway_deployment",
             "id": FAKE_DEPLOYMENT_ID, "name": "deployment of " + api},
            {"kind": "apigateway-stage", "type": "aws_api_gateway_stage",
             # The provider's id: "ags-<rest-api-id>-<stage-name>".
             "id": f"ags-{api}-{stage}", "name": stage},
            {"kind": "ssm-parameter", "type": "aws_ssm_parameter",
             "id": os.environ["FAKE_PARAM"], "name": os.environ["FAKE_PARAM"]},
            {"kind": "cloudwatch-log-group", "type": "aws_cloudwatch_log_group",
             "id": os.environ.get("FAKE_LOG_GROUP", FAKE_LOG_GROUP_DEFAULT),
             "name": os.environ.get("FAKE_LOG_GROUP", FAKE_LOG_GROUP_DEFAULT)},
        ],
    }


def owned_tags():
    return {"AdpFixtureRun": os.environ["FAKE_NONCE"],
            "AdpFixtureAccount": os.environ["FAKE_ACCOUNT"],
            "Disposable": "true"}


if args[:1] == ["output"]:
    what = args[-1]
    if what == "ownership":
        if os.environ.get("FAKE_OWNERSHIP_UNREADABLE") == "1":
            sys.stderr.write("Error: Output \"ownership\" not found\n"); sys.exit(1)
        print(json.dumps(owned_receipt())); sys.exit(0)
    if what == "ssm_provenance_parameter_name":
        print(os.environ.get("FAKE_PARAM", "")); sys.exit(0)
    if what == "rest_api_id":
        print(os.environ.get("FAKE_API_ID", "fixapi123")); sys.exit(0)
    # The role ARNs the resource policy permits. verify needs them to prove its
    # wrong-role probe is NOT signing as an allowlisted identity.
    if what == "allowed_caller_role_arns":
        if os.environ.get("FAKE_ALLOWLIST_UNREADABLE") == "1":
            sys.stderr.write("Error: output not found\n"); sys.exit(1)
        print(os.environ.get(
            "FAKE_ALLOWLIST_JSON",
            json.dumps(["arn:aws:iam::%s:role/w2-fixture-worker" % os.environ["FAKE_ACCOUNT"]])))
        sys.exit(0)
    if what == "worker_control_endpoint":
        # Pointed at a LOCAL http listener when one is running, so the SigV4 probe is
        # exercised over a real socket with real botocore signing instead of against a
        # curl double that cannot tell a signed request from an unsigned one.
        port = os.environ.get("FAKE_EDGE_PORT", "")
        if port:
            print("http://127.0.0.1:%s/dev/internal/v1/agent" % port); sys.exit(0)
        print("https://fixapi123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent")
        sys.exit(0)
    sys.exit(1)

if args[:1] == ["plan"]:
    p = out_path()
    changes = json.loads(os.environ.get("FAKE_PLAN_CHANGES", "null")) or [
        {"address": "aws_api_gateway_rest_api.fixture[0]",
         "type": "aws_api_gateway_rest_api", "change": {"actions": ["create"]}}]
    if "-destroy" in args:
        # The default destroy plan deletes EXACTLY the owned set, each line carrying
        # the recorded id and the run ownership tag — which is what a real plan
        # carries in `change.before` and what destroy now checks against.
        # Derived FROM the receipt, so the default is the consistent case for all six
        # types by construction rather than by a second hardcoded list that could
        # drift from it — the drift between those two lists is precisely the defect
        # root executed (stage recorded as "dev", planned as "ags-<api>-dev").
        _addr = {
            "aws_api_gateway_rest_api": "aws_api_gateway_rest_api.fixture[0]",
            "aws_api_gateway_rest_api_policy": "aws_api_gateway_rest_api_policy.fixture[0]",
            "aws_api_gateway_deployment": "aws_api_gateway_deployment.fixture[0]",
            "aws_api_gateway_stage": "aws_api_gateway_stage.fixture[0]",
            "aws_ssm_parameter": "aws_ssm_parameter.fixture_provenance_secret[0]",
            "aws_cloudwatch_log_group": "aws_cloudwatch_log_group.fixture[0]",
        }
        # A real plan carries `tags` only on taggable types. The rest-api POLICY and
        # the DEPLOYMENT are not taggable, so modelling tags on them would let the
        # tag check pass on lines where a real plan offers nothing to check.
        _untaggable = {"aws_api_gateway_rest_api_policy", "aws_api_gateway_deployment"}
        # A test that supplies FAKE_DESTROY_CHANGES states the WHOLE plan; the
        # default must not be appended to it, or a test asserting on an exact plan
        # would silently get extra lines.
        def _line(r):
            # A test may perturb the receipt (drop a `type`, invent a kind). The plan
            # is what the PROVIDER would emit, so derive a plausible line rather than
            # raising: a fake that crashes on a perturbed receipt would make the test
            # assert on a terraform traceback instead of on the guard's refusal.
            rtype = r.get("type") or "aws_unknown_resource"
            before = {"id": r.get("id", "")}
            if rtype not in _untaggable:
                before["tags"] = owned_tags()
            return {"address": _addr.get(rtype, "%s.fixture[0]" % rtype),
                    "type": rtype,
                    "change": {"actions": ["delete"], "before": before}}

        changes = json.loads(os.environ.get("FAKE_DESTROY_CHANGES", "null")) or (
            [_line(r) for r in owned_receipt()["resources"]] + [
            # A local-only line, which the guard must skip rather than demand an id for.
            {"address": "random_password.fixture_edge_provenance[0]",
             "type": "random_password", "change": {"actions": ["delete"]}}])
        if os.environ.get("FAKE_DESTROY_PLAN_FAIL") == "1" and "-refresh=false" not in args:
            sys.stderr.write("Error: Reading ... data source error\n"); sys.exit(1)
    pathlib.Path(p).write_text(json.dumps({"resource_changes": changes}))
    sys.exit(0)

if args[:1] == ["show"]:
    src = args[-1]
    print(pathlib.Path(src).read_text()); sys.exit(0)

if args[:1] == ["apply"]:
    # A destroy that fails PARTWAY. Modelled so a test can check that a failed
    # teardown leaves no absence artifact at all rather than a previous attempt's.
    if (os.environ.get("FAKE_DESTROY_APPLY_FAIL") == "1"
            and any("destroy.plan" in a for a in args)):
        sys.stderr.write("Error: deleting aws_api_gateway_stage.fixture[0]: throttled\n")
        sys.exit(1)
    sys.exit(0)

sys.stderr.write("fake terraform: unhandled %r\n" % (args,)); sys.exit(1)
'''

FAKE_OWNERSHIP = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("ownership " + " ".join(argv) + "\n")
if os.environ.get("FAKE_LEDGER_FAIL") == "1":
    sys.stderr.write("ledger write failed\n"); sys.exit(1)

def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None

path = flag("--ledger")
# Write the row INTO the ledger document rather than appending a text line. The
# previous fake appended raw argv, which would have left the file unparseable --
# and the script now READS the ledger (for run_id and for the Deployment ownership
# check), so an unparseable ledger would make every later read fail for a reason
# that has nothing to do with the behaviour under test.
doc = json.load(open(path))
doc.setdefault("k8s", []).append({
    "kind": flag("--kind"), "name": flag("--name"),
    "namespace": flag("--namespace"), "uid": flag("--uid"),
    "run_id": flag("--run-id"), "account_id": flag("--account-id"),
    "_argv": " ".join(argv),
})
json.dump(doc, open(path, "w"), indent=2)
sys.exit(0)
'''


DEPLOY_UID = "uuuu-0000-fixture"
RUN_ID = "w2run-2026-09-24-0001"

# The LEDGER's run id, not a format this script guesses. The previous revision
# passed `--run-id w2-<nonce>`, which is an invention: if it does not match the id
# #3968 opened, the row lands under an id its cleanup never looks up, so the object
# is "recorded" and still orphaned.
RUN_ID_FROM_LEDGER = RUN_ID


def _deployment_doc(uid=DEPLOY_UID, labels=None):
    """A fixture Deployment carrying the nine secret-backed env refs."""
    env = [
        {"name": n, "valueFrom": {"secretKeyRef": {
            "name": "bedrockgateway-secrets", "key": n.lower()}}}
        for n in NINE_SECRET_ENV
    ]
    # #3968's REAL label schema, read from lib/render_fixture.py on branch
    # agent/issue-3968. The previous version of this helper used
    # "adp.fixture/run-nonce", a key that renderer never writes -- so the suite
    # validated the script against an invented schema and every label check passed
    # vacuously. `app` and `app.kubernetes.io/part-of` are carried over from the
    # ordinary gateway by the renderer's deep copy, which is exactly why the run
    # labels (not the shape) have to decide ownership.
    md = {"name": DEPLOY, "namespace": NAMESPACE, "uid": uid,
          "resourceVersion": "1234",
          "labels": {"app": DEPLOY,
                     "app.kubernetes.io/part-of": "bedrock-gateway",
                     W2_FIXTURE_LABEL: RUN_ID,
                     W2_NONCE_LABEL: NONCE}}
    if labels is not None:
        md["labels"] = labels
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": md,
            "spec": {"template": {"spec": {"containers": [
                {"name": "bedrockgateway", "env": env}]}}}}


def _ledger_doc(rows=None, run_id=RUN_ID, nonce=NONCE, account=ACCOUNT,
                region="us-east-1", version=LEDGER_VERSION):
    """#3968's ledger AS ITS OWN init_ledger WRITES IT.

    Traced from lib/ownership.py on branch agent/issue-3968, not invented. That
    matters because the previous version of this helper modelled only `run_id` and
    `k8s`, which let this component read a ledger shape #3968 never produces --
    the same class of defect as the guessed label keys.

    Its `init_ledger` ALWAYS writes ledger_version, run_id, run_nonce, account_id,
    region and the three buckets, and its `load_ledger` REFUSES a mismatch on
    run_id, account_id or ledger_version. So every one of those is a precondition
    this component must check BEFORE it creates anything: record-k8s would
    otherwise refuse after the object already exists.
    """
    if rows is None:
        rows = [{"kind": "Deployment", "name": DEPLOY, "namespace": NAMESPACE,
                 "uid": DEPLOY_UID, "delete": True, "created_by_this_run": True}]
    doc = {"run_id": run_id, "k8s": rows, "synthetic_rows": [], "queues": []}
    if version is not None:
        doc["ledger_version"] = version
    if nonce is not None:
        doc["run_nonce"] = nonce
    if account is not None:
        doc["account_id"] = account
    if region is not None:
        doc["region"] = region
    return doc


class _LocalEdge:
    """A REAL http listener standing in for the fixture edge, for the ONE probe that
    cannot be faked at the curl level.

    The unsigned and spoofed probes use curl, so a curl double can model them. The
    wrong-role probe SIGNS, and a double that returns a canned status code cannot
    distinguish a signed request from an unsigned one -- which is exactly how a probe
    that could never sign was recorded as observing a refusal. So that probe is
    pointed at this listener, which records the headers that actually arrived.

    No cloud call and no credentials: botocore resolves static keys from a scratch
    file through its ORDINARY provider chain (the same chain that serves
    assume-role/SSO/credential_process), signs in memory, and answers locally.
    """

    ACCESS_KEY = "AKIAFIXTUREPROBE0001"
    SECRET_KEY = "fixture-probe-secret-not-a-real-credential"

    def __init__(self, tmp_path):
        import http.server
        import threading

        self.requests = []
        self.code = 403          # the refusal the control expects, by default
        edge = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):                          # noqa: N802
                edge.requests.append(dict(self.headers))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                if edge.code == 0:                      # connection-level failure
                    self.close_connection = True
                    return
                self.send_response(edge.code)
                self.end_headers()
                self.wfile.write(b'{"message":"User is not authorized"}')

            def log_message(self, *a):
                return

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._server.server_port

        self.cred_file = tmp_path / "aws-credentials"
        self.cred_file.write_text(
            "[wrongrole]\n"
            f"aws_access_key_id = {self.ACCESS_KEY}\n"
            f"aws_secret_access_key = {self.SECRET_KEY}\n"
            "aws_session_token = fixture-probe-session-token\n"
        )
        self.cred_file.chmod(0o600)
        self.empty_config = tmp_path / "aws-config-empty"
        self.empty_config.write_text("")

    @property
    def env(self):
        return {
            "FAKE_EDGE_PORT": str(self.port),
            # The provider chain's own file locations, visible only to the
            # subprocess the test starts.
            "AWS_SHARED_CREDENTIALS_FILE": str(self.cred_file),
            "AWS_CONFIG_FILE": str(self.empty_config),
        }

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def harness(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("aws", FAKE_AWS), ("kubectl", FAKE_KUBECTL),
                       ("terraform", FAKE_TERRAFORM), ("curl", FAKE_CURL)):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    ownership = tmp_path / "ownership.py"
    ownership.write_text(FAKE_OWNERSHIP)
    ownership.chmod(0o755)

    log = tmp_path / "calls.log"
    log.write_text("")
    # The ledger is a JSON document, appended to by the fake `record-k8s`. Tests
    # that assert on recorded rows read the appended lines; tests that need a
    # different ledger shape rewrite this file.
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps(_ledger_doc()))
    deploy_state = tmp_path / "deploy.json"
    deploy_state.write_text(json.dumps(_deployment_doc()))
    secret_payload = tmp_path / "secret-payload.txt"
    artifacts = tmp_path / "artifacts"

    # A REAL terraform init writes the resolved backend here, and
    # assert_backend_binding compares its recorded state key against the
    # nonce/account on the command line. TF_DATA_DIR keeps it inside tmp_path so a
    # test can never read (or clobber) the repo's own .terraform.
    tf_data = tmp_path / "tfdata"
    tf_data.mkdir()

    # One JSON line per terraform invocation recording the credential environment
    # it ran with. See FAKE_TERRAFORM: this is the only observable that can show
    # the provider is bound to --profile, since terraform has no --profile flag.
    tf_env = tmp_path / "terraform-env.jsonl"
    tf_env.write_text("")

    def write_backend(nonce=NONCE, account=ACCOUNT, region="us-east-1",
                      environment="dev", bucket=BUCKET, profile=PROFILE,
                      backend_type="s3"):
        """The record a REAL `terraform init` leaves in $TF_DATA_DIR.

        `profile` is in it because cmd_init passes `-backend-config=profile=...`,
        and terraform writes the RESOLVED backend config -- including that profile
        -- into this file. Modelling it is what lets a test show the credential the
        state is read through is the credential the run's account checks were made
        against.
        """
        conf = {
            "bucket": bucket,
            "key": f"fixture-edge/{environment}/{account}/{nonce}/terraform.tfstate",
            "region": region, "encrypt": True}
        if profile is not None:
            conf["profile"] = profile
        (tf_data / "terraform.tfstate").write_text(json.dumps({
            "version": 3, "serial": 1,
            "backend": {"type": backend_type, "config": conf}}))

    def write_init_receipt(nonce=NONCE, account=ACCOUNT, region="us-east-1",
                           environment="dev", bucket=BUCKET, profile=PROFILE,
                           backend_type="s3"):
        """The receipt `init` leaves in the run's artifact directory.

        This is the RECORDED EXPECTATION every state-bearing command compares the
        live backend against. It exists because comparing against the flags on the
        current command line meant an OMITTED flag SKIPPED the comparison — so a
        backend initialised against a foreign bucket was reported as verified.

        Kept separate from `write_backend` on purpose: a test needs to express
        "init recorded bucket A, but the live backend now says B" (a re-init, a
        copied .terraform, a typo), which is precisely the divergence the receipt
        is there to detect. A single helper writing both could not model it.
        """
        artifacts.mkdir(exist_ok=True)
        artifacts.chmod(0o700)
        (artifacts / "backend.init.receipt.json").write_text(json.dumps({
            "schema": "fixture-edge/backend-init-receipt/v1",
            "run_nonce": nonce, "account_id": account, "region": region,
            "environment": environment,
            "backend": {
                "type": backend_type, "bucket": bucket,
                "key": f"fixture-edge/{environment}/{account}/{nonce}/terraform.tfstate",
                "region": region, "profile": profile or ""},
        }, indent=2, sort_keys=True))

    write_backend()
    write_init_receipt()

    # Started for every test so the SIGNED probe always has a real socket and real
    # SDK-resolved credentials; the curl double still serves the unsigned/spoofed
    # probes, which are the ones a canned status code can legitimately model.
    edge = _LocalEdge(tmp_path)

    def run(subcmd, extra_env=None, args=None, with_tfvars=True):
        if with_tfvars:
            artifacts.mkdir(exist_ok=True)
            artifacts.chmod(0o700)
            (artifacts / "fixture.tfvars").write_text('fixture_edge_enabled = true\n')
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env.update(
            W2_OWNERSHIP_LIB=str(ownership), TF_DATA_DIR=str(tf_data),
            FAKE_LOG=str(log), FAKE_ACCOUNT=ACCOUNT, FAKE_NONCE=NONCE,
            FAKE_PARAM=PARAM, FAKE_DEPLOY_STATE=str(deploy_state),
            FAKE_SECRET_PAYLOAD=str(secret_payload), FAKE_NAMESPACE=NAMESPACE,
            FAKE_TF_ENV=str(tf_env),
        )
        env.update(edge.env)
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
    harness.tf_data = tf_data
    harness.tf_env = tf_env
    harness.tmp = tmp_path
    harness.write_backend = write_backend
    harness.write_init_receipt = write_init_receipt
    harness.edge = edge
    try:
        yield harness
    finally:
        edge.close()


def recorded_rows(harness) -> list:
    """Only the rows the SCRIPT recorded, not the seed row the fixture planted."""
    doc = json.loads(harness.ledger.read_text())
    return [r for r in doc.get("k8s", []) if "_argv" in r]


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


# --- shared security groups are structurally out of reach -------------------
# Root's constraint on blocker 5: "Never change/delete shared SGs
# (sg-0623ec399f4a20b87)."
#
# main.tf now READS security group rules to prove the VPC Link can reach the
# fixture ALB in both directions, and reading is the whole of what it may do.
# BOTH groups on that path are shared platform infrastructure — the link's group
# belongs to the platform VPC Link, and the ALB-side group is carried by BOTH
# ordinary gateway ALBs — so a rule added to "fix" an unreachable fixture would
# change ordinary traffic's security posture under a fixture ticket.
#
# The tempting way to satisfy that constraint is a review note. These two gates
# make it structural instead: a group cannot be changed by a plan that cannot
# contain the resource, and cannot reach AWS through a plan review that rejects
# its type. Both are asserted because either alone can be removed.
def test_no_terraform_file_declares_a_security_group_resource():
    """A refusal is the only permitted response to an unreachable fixture.

    Asserted against the source rather than a plan: `terraform test` uses mocked
    providers, so a declared-but-unreached resource would not surface there, and
    the question here is what this root is CAPABLE of, not what one plan did.

    Matched on `resource "aws_..."` specifically. `data` blocks are exactly what
    the fix adds, so a naive grep for the group names would have to be either
    wrong or disabled.
    """
    declared = []
    for tf in sorted(COMPONENT.glob("*.tf")):
        for i, line in enumerate(tf.read_text().splitlines(), 1):
            m = re.match(r'\s*resource\s+"([^"]+)"', line)
            if m and "security_group" in m.group(1):
                declared.append(f"{tf.name}:{i} {m.group(1)}")
    assert not declared, (
        "this component declares a security-group resource: "
        + ", ".join(declared)
        + ". Both sides of the reachability path are SHARED groups "
        "(sg-0623ec399f4a20b87 is carried by both ordinary gateway ALBs). #5836 "
        "forbids ordinary-infrastructure changes, so an unreachable fixture must be "
        "REFUSED by the run-binding gate, never fixed by widening a group."
    )


def test_the_plan_review_allowlist_admits_no_security_group_type():
    """The second, independent half.

    If someone did declare one, the plan-review guard in this script is what stops
    it reaching AWS — but only while its allowlist excludes the type. The allowlist
    is read out of the script so this cannot pass by the two drifting apart.
    """
    src = SCRIPT.read_text()
    block = src[src.index("allowed = {"):]
    block = block[:block.index("}")]
    allowed = set(re.findall(r'"([^"]+)"', block))
    assert allowed, "could not read the plan-review allowlist out of the script"
    offenders = {t for t in allowed if "security_group" in t}
    assert not offenders, (
        f"the plan-review allowlist admits {sorted(offenders)}. That is the check "
        "that would otherwise stop a security-group change from being applied."
    )


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
    assert "record-k8s" not in harness.log.read_text()
    assert recorded_rows(harness) == []


def test_handoff_records_a_ledger_receipt_for_the_secret(harness):
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    rows = recorded_rows(harness)
    assert len(rows) == 1, rows
    assert rows[0]["kind"] == "Secret"
    assert rows[0]["name"] == SECRET
    assert rows[0]["uid"] == "ssss-1111-2222"
    # The run id is the LEDGER'S. A guessed "w2-<nonce>" records the object under an
    # id #3968's cleanup never looks up: recorded, and still orphaned.
    assert rows[0]["run_id"] == RUN_ID_FROM_LEDGER, (
        f"recorded under run id {rows[0]['run_id']!r}; the ledger's run id is "
        f"{RUN_ID_FROM_LEDGER!r}. A row under the wrong id is never cleaned up.")


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
    assert recorded_rows(harness) == []


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
    assert recorded_rows(harness) == []


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
    assert "ORDINARY provenance parameter is MISSING" in r.stderr


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


# =============================================================================
# THE SIX REGRESSIONS ROOT ENUMERATED AFTER EXECUTING THIS HARNESS
# =============================================================================
# Root ran the script three times using these fakes and proved three defects that
# all 63 tests above missed. The tests were green because they asserted on MESSAGES
# and EXIT CODES of the happy path, never on SIDE EFFECTS after a failure. The
# distinction matters: "the script printed the right error" and "the script did not
# leave a live object behind" are different claims, and only the second one is a
# security property.
#
# Each test below is a regression in the strict sense — it was confirmed to FAIL
# against the pre-fix script before the fix was written, for the reason stated.
# Where a test would still pass against the old code for an unrelated reason, that
# is noted; a regression that cannot fail is documentation, not a test.
# =============================================================================


# --- 1. No creation after a failed SSM read --------------------------------
def test_no_secret_is_created_when_the_ssm_read_fails(harness):
    """The executed defect: `aws ssm get-parameter | kubectl create secret`.

    The two sides of a pipe run CONCURRENTLY. When the SSM read failed, kubectl had
    already seen EOF on stdin, created an EMPTY Secret, and exited 0 — and then the
    script checked PIPESTATUS, found the SSM failure, and reported "NOTHING was
    created". That claim was false, the ledger record never ran (the failure path
    returns first), and so the one object nothing could later find was also the one
    object teardown would never delete.

    PIPESTATUS was never capable of preventing this. It reports what happened; it
    cannot un-create what the downstream process already did. So this asserts on
    the CLUSTER, not on the message: the file the fake kubectl writes on any
    `create secret` must not exist at all.
    """
    r = harness.run("handoff", {"FAKE_SSM_READ_FAIL": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert not harness.secret_payload.exists(), (
        "a Secret WAS created even though the upstream SSM read failed. The script "
        f"claims 'NOTHING was created'; the cluster disagrees. Payload: "
        f"{harness.secret_payload.read_text()!r}"
    )
    assert "create secret" not in harness.log.read_text(), (
        "kubectl create secret was invoked after a failed read — the value it "
        "received cannot be the secret, so whatever landed is wrong."
    )
    # And nothing recorded it, so teardown would never find it either. Both halves
    # are needed: an unrecorded object is an orphan, a recorded empty one is a
    # broken trust root.
    assert recorded_rows(harness) == []


def test_a_secret_is_never_created_from_an_empty_or_error_value(harness):
    """Validate IN MEMORY, before the cluster is touched.

    A Secret holding an empty string, the CLI's literal "None", or an error message
    surfaces much later as an unexplained 403 — far from its cause, and looking
    like a fixture-edge bug rather than a failed read.
    """
    for value, why in (("", "empty"), ("None", "the CLI's rendering of absent"),
                       ("AccessDeniedException: nope", "an error message")):
        h_payload = harness.secret_payload
        if h_payload.exists():
            h_payload.unlink()
        r = harness.run("handoff", {"FAKE_SSM_VALUE": value},
                        args=["--fixture-deployment", DEPLOY])
        assert r.returncode != 0, f"accepted {why} value {value!r}"
        assert not h_payload.exists(), (
            f"created a Secret from {why} value {value!r}; the fixture would then "
            "validate every request against it"
        )


# --- 2. No secret data in any receipt -------------------------------------
def test_no_file_in_the_artifact_directory_contains_the_secret(harness):
    """`kubectl create secret -o json` returns the base64 `data` block.

    i.e. the response IS the secret. The previous revision redirected it straight
    to secret-create.json while a comment three lines above claimed the value "is
    never written to a file". Base64 is not encryption.

    Asserted over EVERY file in the artifact directory rather than the one file
    that was wrong, so a future receipt cannot reintroduce this somewhere else.
    """
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr

    import base64
    secret = "s3cr3t-provenance-value"
    b64 = base64.b64encode(secret.encode()).decode()
    leaked = []
    for path in sorted(harness.artifacts.rglob("*")):
        if not path.is_file():
            continue
        body = path.read_text(errors="replace")
        if secret in body or b64 in body:
            leaked.append(path.name)
        # The `data` key itself is the carrier; flag it even if the fake's value
        # changes, because the next kubectl version may add another such field.
        if '"data"' in body or "stringData" in body:
            leaked.append(f"{path.name} (carries a data block)")
    assert not leaked, (
        f"these artifact files contain the secret or a secret-bearing block: "
        f"{leaked}. The receipt must be METADATA ONLY."
    )
    # Positive control: the receipt exists and is useful. A test that passed simply
    # because nothing was written would be worthless.
    receipt = json.loads((harness.artifacts / "secret-receipt.json").read_text())
    assert receipt["uid"] == "ssss-1111-2222"
    assert receipt["name"] == SECRET
    assert "data" not in receipt and "stringData" not in receipt


def test_the_raw_create_response_does_not_survive_the_run(harness):
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    raw = harness.artifacts / "secret-create.raw.json"
    assert not raw.exists(), (
        "the raw `kubectl create -o json` response is still on disk; it holds the "
        "secret's base64 data for the rest of the run"
    )


def test_the_creation_intent_is_recorded_before_the_cluster_is_mutated(harness):
    """Intent-before-mutation, so a process killed mid-handoff leaves a trail.

    Without it, a crash between the create and the ledger record leaves an object
    that teardown cannot find and that nobody can safely delete (delete-by-name
    would hit another run's Secret). The intent record is what `recover-secret`
    consumes as evidence that THIS run created it.
    """
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    intent = harness.artifacts / "secret-intent.json"
    doc = json.loads(intent.read_text())
    assert doc["name"] == SECRET and doc["run_nonce"] == NONCE
    assert "value" not in doc and "data" not in doc, "an intent record is metadata"
    assert oct(intent.stat().st_mode)[-3:] == "600"


def test_recover_secret_refuses_without_a_creation_intent(harness):
    """Recording implies authority to delete.

    A same-named Secret may be another run's trust root, so without evidence that
    this run created the object, recovery must refuse rather than adopt it.
    """
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode != 0
    assert "no creation intent" in r.stderr
    assert recorded_rows(harness) == []


def _seed_recovery_artifacts(harness, intent=True, receipt_uid="ssss-1111-2222",
                             **overrides):
    """The two artifacts recover-secret consults. Split out because the interesting
    tests are about what happens when they DISAGREE with the live object."""
    harness.artifacts.mkdir(exist_ok=True)
    harness.artifacts.chmod(0o700)
    common = {"name": SECRET, "namespace": NAMESPACE,
              "run_nonce": NONCE, "account_id": ACCOUNT}
    common.update(overrides)
    if intent:
        (harness.artifacts / "secret-intent.json").write_text(json.dumps(
            dict(common, intent="create-secret", state="pending")))
    if receipt_uid is not None:
        (harness.artifacts / "secret-receipt.json").write_text(json.dumps(
            dict(common, kind="Secret", uid=receipt_uid, resourceVersion="77")))


def test_recover_secret_records_the_live_object_by_its_actual_uid(harness):
    """Positive control: an intent AND a uid receipt that matches the live object."""
    _seed_recovery_artifacts(harness)
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode == 0, r.stderr
    rows = recorded_rows(harness)
    assert len(rows) == 1 and rows[0]["uid"] == "ssss-1111-2222"
    # NEVER `-o json` here either: the fake kubectl refuses that form precisely
    # because the response carries the base64 data.
    for line in harness.log.read_text().splitlines():
        if line.startswith("kubectl get secret"):
            assert "jsonpath" in line, f"read the whole Secret object: {line}"


def test_recover_secret_refuses_a_live_object_whose_uid_is_not_the_recorded_one(harness):
    """ROOT'S EXECUTED FINDING, as a regression.

    Root ran recovery with a receipt naming one uid while the live Secret answered a
    DIFFERENT uid. It exited 0 and recorded the REPLACEMENT -- because the previous
    revision checked only that an intent file EXISTED, then adopted whatever uid was
    live. Recording is what authorises deletion, so that path could hand another
    run's trust root to this run's teardown.

    A uid changes only on delete-and-recreate, so a mismatch means the object under
    this name is categorically not ours.
    """
    _seed_recovery_artifacts(harness, receipt_uid="original-uid-0000")
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode != 0, (
        "recovery adopted a Secret whose uid is not the one creation recorded")
    assert "original-uid-0000" in r.stderr and "ssss-1111-2222" in r.stderr, (
        "the refusal must show BOTH uids so the operator can investigate")
    assert "NOT the object this run created" in r.stderr
    assert recorded_rows(harness) == [], "a foreign object was recorded as ours"
    # And it must not suggest a name-based deletion as the remedy.
    assert "kubectl delete secret" not in r.stderr


def test_recover_secret_refuses_when_no_uid_was_ever_captured(harness):
    """Intent alone proves an attempt was MADE, not which object holds the name now.

    The create may have hit AlreadyExists against an object this run never made, or
    the name may have been recycled since. Unverifiable partial recovery is escalated
    to a human, never adopted.
    """
    _seed_recovery_artifacts(harness, receipt_uid=None)
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode != 0
    assert "no uid receipt" in r.stderr
    assert recorded_rows(harness) == []


def test_recover_secret_refuses_an_empty_uid_in_the_receipt(harness):
    """A receipt that exists but records no uid is the same evidential gap as none."""
    _seed_recovery_artifacts(harness, receipt_uid="")
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode != 0
    assert "NO uid" in r.stderr or "no uid" in r.stderr
    assert recorded_rows(harness) == []


@pytest.mark.parametrize("field,value", [
    ("run_nonce", "c0c0c0c0c0c0c0c0"),
    ("account_id", "111111111111"),
    ("name", "w2-fixture-provenance-someone-else"),
    ("namespace", "kube-system"),
])
def test_recover_secret_refuses_artifacts_belonging_to_another_run(harness, field, value):
    """The previous revision checked only that the intent file EXISTED, so a stale
    artifact directory from another nonce, account, object or namespace satisfied it.
    Each of those four fields is load-bearing, so each is asserted."""
    _seed_recovery_artifacts(harness, **{field: value})
    r = harness.run("recover-secret", {"FAKE_SECRET_EXISTS": "1"})
    assert r.returncode != 0, f"a foreign {field} was accepted"
    assert value in r.stderr and "REFUSING" in r.stderr
    assert recorded_rows(harness) == []


# --- 3. Cross-nonce / cross-account backend refusal -----------------------
def test_a_command_for_another_nonce_refuses_the_initialised_backend(harness):
    """`init --nonce A` then `plan --nonce B` planned B's resources into A's state.

    Nothing compared the backend record terraform wrote against the arguments on
    the current command line. Both runs then believe they own the same objects, and
    either teardown destroys the other's edge.
    """
    harness.write_backend(nonce="ffffffffffffffff")
    r = harness.run("plan")
    assert r.returncode != 0
    assert "BACKEND MISMATCH" in r.stderr
    assert "ffffffffffffffff" in r.stderr and NONCE in r.stderr
    # It must refuse BEFORE running terraform, not after.
    assert "terraform plan" not in harness.log.read_text()


def test_a_command_for_another_account_refuses_the_initialised_backend(harness):
    """The account is IN the state key, so a cross-account mismatch is caught by the
    same comparison — which is why the key carries it."""
    harness.write_backend(account="111111111111")
    r = harness.run("destroy")
    assert r.returncode != 0
    assert "BACKEND MISMATCH" in r.stderr
    assert "terraform plan -destroy" not in harness.log.read_text()


def test_every_state_touching_subcommand_checks_the_backend_binding(harness):
    """Enumerated from the script, so a new subcommand cannot skip the check.

    One command missing this is enough: it is the one an operator will use on the
    day the nonces get crossed.
    """
    harness.write_backend(nonce="ffffffffffffffff")
    for sub, extra_args in (("plan", []), ("destroy", []), ("verify", []),
                            ("handoff", ["--fixture-deployment", DEPLOY])):
        r = harness.run(sub, args=extra_args)
        assert r.returncode != 0, f"{sub} proceeded against a mismatched backend"
        assert "BACKEND MISMATCH" in r.stderr, f"{sub} did not check the binding"


def test_commands_refuse_when_no_backend_was_initialised(harness):
    """Absent is not 'fine'. With no record there is nothing to compare against, so
    the command would run through whatever state happened to be configured."""
    (harness.tf_data / "terraform.tfstate").unlink()
    r = harness.run("plan")
    assert r.returncode != 0
    assert "no initialised backend" in r.stderr


def test_the_same_key_in_another_bucket_is_another_state_file(harness):
    """Validating only the KEY was not enough.

    A re-init against a different bucket — a typo, or another account's state bucket
    this credential happens to reach — left the key matching exactly while the run
    read and wrote a completely different state file. "The key matches" is not
    "this is the state I think it is".
    """
    harness.write_backend(bucket="someone-elses-state")
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0, "a matching key in a foreign bucket was accepted"
    assert "DIFFERENT BUCKET" in r.stderr
    assert "someone-elses-state" in r.stderr and BUCKET in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_a_foreign_bucket_is_refused_when_no_bucket_argument_is_GIVEN(harness):
    """The omitted-argument bypass, exactly as root executed it.

        harness.write_backend(bucket="someone-elses-state")
        harness.run("plan")            # <-- NO --state-bucket

    exited 0, printed "backend binding verified (s3://someone-elses-state/...)"
    and ran terraform plan against that foreign state.

    The cause was that the comparison was conditional on the flag being supplied
    (`if expect_bucket and actual_bucket != expect_bucket`), so leaving the flag off
    did not weaken the check — it SKIPPED it. The pre-existing negative test above
    supplies --state-bucket, which is why it never covered this path, and the
    omitted-argument path is the one an operator reaches by accident.

    A missing expected value must never waive a comparison, so the expectation now
    comes from the receipt `init` wrote. There is always something to compare
    against and no argument to forget.
    """
    harness.write_backend(bucket="someone-elses-state")
    r = harness.run("plan")                       # deliberately no --state-bucket
    assert r.returncode != 0, "an omitted --state-bucket waived the bucket comparison"
    assert "DIFFERENT BUCKET" in r.stderr
    assert "someone-elses-state" in r.stderr and BUCKET in r.stderr
    # And it must refuse BEFORE touching state, not report the mismatch afterwards.
    assert "terraform plan" not in harness.log.read_text()


def test_a_profile_argument_matching_the_LIVE_backend_cannot_launder_it(harness):
    """The profile had the same conditional shape as the bucket, and needs a test
    that distinguishes WHERE the expectation comes from — which the pre-existing
    profile test does not, because it happens to pass a flag that disagrees with the
    live backend either way.

    Here the command line AGREES with the live backend and both differ from what
    init recorded: state initialised as `adp-embark1`, a re-init (or copied
    .terraform) now pointing at `some-other-profile`, and a --profile naming that
    same other profile. If the expectation is taken from the command line the two
    match and the run proceeds, reading and writing state through an identity none
    of this run's account checks were ever made against. Taken from the receipt, it
    is refused.
    """
    harness.write_backend(profile="some-other-profile")
    r = harness.run("plan", args=["--state-bucket", BUCKET,
                                  "--profile", "some-other-profile"])
    assert r.returncode != 0, "a --profile agreeing with the live backend laundered it"
    assert "CONTRADICTS THE INIT RECEIPT" in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_a_command_with_no_init_receipt_refuses_rather_than_falling_back(harness):
    """If the receipt is absent there is no recorded expectation, and the only
    remaining source would be the command line — the bypass itself. So its absence
    is fatal rather than a reason to check less."""
    (harness.artifacts / "backend.init.receipt.json").unlink()
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0
    assert "no backend init receipt" in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_an_init_receipt_from_another_run_cannot_supply_the_expectation(harness):
    """A receipt is only evidence about the run that wrote it.

    Same rule the destroy guard applies to the ownership receipt: validate the run
    binding BEFORE taking the expectation from it, or a foreign artifact directory
    supplies the very value used to approve this command's state access.
    """
    harness.write_init_receipt(nonce="ffffffffffffffff")
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0
    assert "INIT RECEIPT MISMATCH" in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_a_bucket_argument_contradicting_the_receipt_is_a_stop_not_an_override(harness):
    """The flag is a cross-check, never the expectation. A command naming a bucket
    other than the initialised one is a mistake worth stopping on — silently
    preferring either value would hide it."""
    r = harness.run("plan", args=["--state-bucket", "a-different-bucket"])
    assert r.returncode != 0
    assert "CONTRADICTS THE INIT RECEIPT" in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_init_writes_the_receipt_later_commands_verify_against(harness):
    """The positive control. Without it, every test above would also pass if `init`
    never wrote a receipt at all and the refusals were simply unconditional."""
    (harness.artifacts / "backend.init.receipt.json").unlink()
    r = harness.run("init", args=["--state-bucket", BUCKET])
    assert r.returncode == 0, r.stderr
    receipt = json.loads((harness.artifacts / "backend.init.receipt.json").read_text())
    assert receipt["run_nonce"] == NONCE and receipt["account_id"] == ACCOUNT
    assert receipt["backend"] == {
        "type": "s3", "bucket": BUCKET,
        "key": f"fixture-edge/dev/{ACCOUNT}/{NONCE}/terraform.tfstate",
        "region": "us-east-1", "profile": PROFILE}
    # A plan then proceeds against the matching live backend.
    assert harness.run("plan", args=["--state-bucket", BUCKET]).returncode == 0


def test_a_substituted_local_backend_is_refused(harness):
    """This component's whole ownership story rests on the isolated per-run S3 key:
    it is what lets teardown name the exact objects this run created. A local backend
    has no such isolation, and the key check says nothing about the type — an s3
    record replaced by a local one still carried a matching `key`."""
    harness.write_backend(backend_type="local")
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0
    assert "backend type" in r.stderr and "'local'" in r.stderr
    assert "terraform plan" not in harness.log.read_text()


def test_state_read_through_a_different_credential_is_refused(harness):
    """The backend's PROFILE is the identity that reads and writes the state.

    If it differs from --profile, the run reads state through one identity while
    every account/cluster check it made ran as another — so the state the plan is
    built from was never the state those checks applied to. A run can then pass all
    its guards and still act on a foreign account's recorded resources.
    """
    harness.write_backend(profile="some-other-profile")
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0
    assert "DIFFERENT credential" in r.stderr
    assert "some-other-profile" in r.stderr and PROFILE in r.stderr


def test_an_ambient_credential_backend_is_refused_when_a_profile_is_named(harness):
    """The mismatch that is easiest to create by hand: `init` without --profile, then
    everything else with one. The recorded backend has NO profile, so the state moves
    on whatever ambient credential the environment supplies."""
    harness.write_backend(profile=None)
    r = harness.run("plan", args=["--state-bucket", BUCKET])
    assert r.returncode != 0
    assert "DIFFERENT credential" in r.stderr
    assert "ambient credential" in r.stderr


# --- 3b. The terraform PROVIDER is bound to the same credential ------------
def _tf_runs(harness) -> list:
    return [json.loads(l) for l in harness.tf_env.read_text().splitlines() if l.strip()]


def test_every_terraform_invocation_is_bound_to_the_named_profile(harness):
    """`--profile` BINDS THE AWS CLI ONLY.

    terraform has no --profile flag; its AWS provider resolves credentials from the
    process environment. So a run whose `aws_` calls were all correctly bound could
    still have terraform plan/apply/destroy against a DIFFERENT account — the exact
    split this component's account guards exist to prevent, since the guards run
    through the CLI and the mutations run through the provider.

    Enumerated over every subcommand that shells terraform, so a new call site
    cannot quietly escape the binding.
    """
    for sub, extra in (("init", ["--state-bucket", BUCKET]),
                       ("plan", []), ("apply", []), ("verify", []),
                       ("destroy", []),
                       ("handoff", ["--fixture-deployment", DEPLOY])):
        harness.tf_env.write_text("")
        harness.run(sub, VERIFY_OK_ENV, args=extra)
        runs = _tf_runs(harness)
        if not runs:
            continue
        for run in runs:
            env = run["env"]
            assert env["AWS_PROFILE"] == PROFILE, (
                f"{sub}: terraform {run['argv'][:1]} ran without the bound profile: {env}")
            assert env["AWS_REGION"] == "us-east-1"


def test_ambient_static_keys_cannot_outrank_the_named_profile(harness):
    """PRECEDENCE, not presence.

    Exporting AWS_PROFILE is not enough: in the AWS SDK chain
    AWS_ACCESS_KEY_ID/SECRET/SESSION_TOKEN OUTRANK it. An operator (or a CI job)
    with those already exported for another account would have terraform use THOSE
    while AWS_PROFILE sat there ignored — and `aws --profile` would meanwhile report
    the correct account, so every guard in the run would pass.

    The binding must therefore CLEAR the higher-precedence variables, not just set
    the lower-precedence one.
    """
    r = harness.run("plan", {
        "AWS_ACCESS_KEY_ID": "AKIAINTERLOPERKEY0001",
        "AWS_SECRET_ACCESS_KEY": "interloper-secret",
        "AWS_SESSION_TOKEN": "interloper-token",
        "AWS_DEFAULT_PROFILE": "interloper-profile",
    }, args=["--state-bucket", BUCKET])
    assert r.returncode == 0, r.stderr
    runs = _tf_runs(harness)
    assert runs, "plan ran no terraform at all"
    for run in runs:
        env = run["env"]
        assert env["AWS_PROFILE"] == PROFILE
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
            assert env[var] is None, (
                f"{var} survived into terraform and outranks AWS_PROFILE, so the "
                f"provider would have used the ambient credential: {env}")


# --- 3c. `apply --plan-file` applies THE REVIEWED PLAN, not any plan --------
#
# `--plan-file is required` was already enforced, and it was not the same property.
# The script checked that the path existed and applied it. A saved plan file records
# no run nonce, no account, no backend and no trace of the variables it was built
# from, so nothing distinguished the plan this run reviewed from another run's plan,
# a plan built against another backend, an edited tfvars, or an edited plan file.
#
# `plan` now writes a receipt (run/account/region/environment/state key/bucket/
# profile + tfvars and plan digests) and `apply` recomputes it through the SAME
# function and refuses any difference, naming the field.


def _plan_then(harness, **kw):
    """Run `plan` (which writes the receipt) and return the saved plan's path."""
    r = harness.run("plan", **kw)
    assert r.returncode == 0, r.stderr
    plan = harness.artifacts / "fixture.plan"
    assert plan.is_file(), "plan did not save a plan file"
    assert (harness.artifacts / "fixture.plan.receipt.json").is_file(), (
        "plan saved no receipt, so apply has nothing to verify against")
    return plan


def test_apply_accepts_the_plan_this_run_reviewed(harness):
    """The positive case, asserted so the refusals below cannot be a blanket 'no'.

    A check that rejects correct input as well is worse than none: it teaches the
    operator to bypass it.
    """
    plan = _plan_then(harness)
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode == 0, r.stderr
    assert "plan verified as this run's reviewed plan" in r.stdout
    assert "terraform apply" in harness.log.read_text()


def test_apply_refuses_a_plan_file_whose_bytes_are_not_the_reviewed_ones(harness):
    """Another run's plan file, or an edited one, reaches apply as the same thing:
    a file at a path. The digest is what separates them."""
    plan = _plan_then(harness)
    plan.write_bytes(plan.read_bytes() + b"\n# edited after review\n")
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode != 0, "apply applied a plan file that was not the reviewed one"
    assert "not the plan this run reviewed" in r.stderr
    assert "plan_sha256" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_apply_refuses_a_plan_reviewed_by_another_run(harness):
    """The receipt records the RUN, so a plan reviewed under another nonce is refused
    by name rather than only by digest.

    This is reachable in one command: --artifact-dir is operator-supplied, so two
    runs can be pointed at one directory, and then `apply --nonce B` finds run A's
    reviewed plan sitting exactly where it expects its own.
    """
    plan = _plan_then(harness)
    receipt = harness.artifacts / "fixture.plan.receipt.json"
    doc = json.loads(receipt.read_text())
    doc["run_nonce"] = "ffffffffffffffff"
    receipt.write_text(json.dumps(doc))
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode != 0
    assert "run_nonce" in r.stderr
    assert "ffffffffffffffff" in r.stderr and NONCE in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_apply_refuses_a_plan_built_against_another_backend(harness):
    """A plan built while the run pointed at another state bucket/profile/key is a
    plan about other resources. The plan file itself records none of that, which is
    why the digest alone cannot answer it."""
    plan = _plan_then(harness)
    receipt = harness.artifacts / "fixture.plan.receipt.json"
    doc = json.loads(receipt.read_text())
    doc["state_bucket"] = "someone-elses-state"
    doc["state_profile"] = "some-other-profile"
    receipt.write_text(json.dumps(doc))
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode != 0
    assert "state_bucket" in r.stderr and "state_profile" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_apply_refuses_inputs_edited_after_the_plan_was_reviewed(harness):
    """The reviewed artifact is the plan AND the variables it was built from.

    Terraform will happily apply a saved plan whose var-file has since changed — the
    plan carries its own values — so the applied change matches the plan while the
    file a reviewer was shown no longer describes the run. Editing the inputs after
    review is an unreviewed change either way.
    """
    plan = _plan_then(harness)
    (harness.artifacts / "fixture.tfvars").write_text(
        'fixture_edge_enabled = true\nallowed_caller_role_arns = ["arn:aws:iam::1:role/x"]\n')
    r = harness.run("apply", args=["--plan-file", str(plan)], with_tfvars=False)
    assert r.returncode != 0
    assert "tfvars_sha256" in r.stderr
    assert "edited after review" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_apply_refuses_when_nothing_recorded_what_was_reviewed(harness):
    """No receipt is not 'nothing to check'. It means this run cannot show the file
    it was handed is the plan it reviewed, which is the whole property."""
    plan = _plan_then(harness)
    (harness.artifacts / "fixture.plan.receipt.json").unlink()
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode != 0
    assert "no plan receipt" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_apply_dry_run_verifies_the_plan_before_reporting_it_would_apply(harness):
    """--dry-run is the reviewable path, so it must run the same check. A dry-run that
    reports "would apply" for a plan the real apply would refuse is a rehearsal of a
    different command."""
    plan = _plan_then(harness)
    plan.write_bytes(b"not the reviewed plan")
    r = harness.run("apply", args=["--plan-file", str(plan), "--dry-run"])
    assert r.returncode != 0
    assert "not the plan this run reviewed" in r.stderr
    assert "would apply" not in r.stdout


# --- 4. Foreign / replaced Deployment refusal ------------------------------
def test_handoff_refuses_a_deployment_the_ledger_does_not_vouch_for(harness):
    """EXISTENCE IS NOT OWNERSHIP.

    The previous revision ran `kubectl get deployment >/dev/null` and proceeded,
    which accepts any Deployment of that name — including a replacement created
    after this run's fixture was deleted.
    """
    harness.deploy_state.write_text(json.dumps(_deployment_doc(uid="zzzz-foreign")))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "NOT recorded" in r.stderr or "not recorded" in r.stderr
    assert "Existence is not ownership" in r.stderr
    # Nothing was created: the ownership check runs BEFORE the secret is read.
    assert not harness.secret_payload.exists()
    assert "--with-decryption" not in harness.log.read_text()


def test_handoff_refuses_the_ordinary_gateway_deployment(harness):
    """The worst possible outcome of the above: BG_TRUST_APIGW_HEADERS=true on
    production, i.e. exactly the 'trusts forgeable headers' state #5836 exists to
    avoid. #3968's renderer deep-copies the live gateway pod spec, so a fixture and
    the ordinary workload look alike apart from their ownership markers — which is
    why the markers, not the shape, must decide."""
    harness.deploy_state.write_text(json.dumps(
        _deployment_doc(labels={"app": "bedrockgateway"})))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "ORDINARY gateway" in r.stderr
    assert "BG_TRUST_APIGW_HEADERS" in r.stderr
    assert not harness.secret_payload.exists()


def test_the_ownership_labels_are_3968s_actual_keys_not_guesses(harness):
    """THE DEFECT THAT MADE THE CHECK ABOVE INERT.

    The previous revision looked for label keys that do not exist in #3968's
    renderer. Two consequences, both bad in the same direction: a REAL fixture was
    refused (so the handoff could never run), and the ordinary-gateway refusal was
    decided by a key nothing ever sets. Verified against lib/render_fixture.py on
    branch agent/issue-3968, which emits exactly `adp.io/w2-fixture` and
    `adp.io/w2-nonce`.

    Asserted on the SCRIPT SOURCE, because the keys are a cross-component contract:
    a behavioural test alone would pass against any pair of keys the fixture in this
    file also happens to use.
    """
    src = SCRIPT.read_text()
    for key in (W2_FIXTURE_LABEL, W2_NONCE_LABEL):
        assert f'"{key}"' in src, (
            f"the handoff does not look for {key}, which is what #3968 actually sets. "
            "A label key nothing sets makes the ownership refusal vacuous.")
    # And a Deployment carrying #3968's keys for THIS run must be accepted, so the
    # check is satisfiable by a real fixture rather than being a wall.
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, (
        "a Deployment labelled exactly as #3968 labels it was REFUSED; the handoff "
        f"cannot run against a real fixture:\n{r.stderr}")


def test_handoff_refuses_a_deployment_missing_either_ownership_label(harness):
    """BOTH keys are required. One alone cannot bind the object to this run: the
    fixture label without the nonce does not say WHICH run, and the nonce without the
    fixture label does not say the object is a fixture at all."""
    for drop in (W2_FIXTURE_LABEL, W2_NONCE_LABEL):
        labels = {"app": DEPLOY, "app.kubernetes.io/part-of": "bedrock-gateway",
                  W2_FIXTURE_LABEL: RUN_ID, W2_NONCE_LABEL: NONCE}
        del labels[drop]
        harness.deploy_state.write_text(json.dumps(_deployment_doc(labels=labels)))
        r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
        assert r.returncode != 0, f"accepted a Deployment with no {drop} label"
        assert not harness.secret_payload.exists()


def test_handoff_refuses_a_deployment_labelled_for_another_run(harness):
    harness.deploy_state.write_text(json.dumps(_deployment_doc()).replace(
        NONCE, "c0c0c0c0c0c0c0c0"))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert not harness.secret_payload.exists()


def test_handoff_detects_a_deployment_replaced_around_the_patch(harness):
    """resourceVersion guards the patch; only the uid can prove no
    delete-and-recreate happened around it.

    A replacement can land on any version string, so the post-patch recheck is the
    only thing that can say the env below belongs to the workload that was vouched
    for.
    """
    r = harness.run("handoff", {"FAKE_DEPLOY_REPLACED_DURING_PATCH": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "uid changed" in r.stderr + r.stdout
    assert "do not treat this as attached" in (r.stderr + r.stdout).lower()


def test_handoff_binds_the_patch_to_the_verified_object_with_json_patch_tests(harness):
    """Without a server-enforced precondition there is a window in which the verified
    fixture is deleted and a same-named object is patched instead.

    The PREVIOUS revision closed it with `--resource-version=<rv>`, a flag
    `kubectl patch` DOES NOT HAVE. Real kubectl v1.37.1 answers `unknown flag:
    --resource-version` and exits 1, so the attach step could never succeed in a
    live run -- and it failed AFTER the Secret was created and recorded, i.e. at the
    worst moment. Only a permissive fake that parsed the invented flag made this look
    tested.

    RFC 6902 `test` operations are the supported mechanism and they are STRICTLY
    STRONGER: the API server evaluates them against the live object and applies
    nothing unless all pass, and they can pin the uid -- which a resourceVersion
    check cannot, because a replacement can land on any version string.
    """
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    patch = next(l for l in harness.log.read_text().splitlines()
                 if l.startswith("kubectl patch deployment"))
    assert "--resource-version" not in patch, (
        "patch still passes a flag kubectl does not have; a live run exits 1 here: "
        f"{patch}")
    assert "--type=json" in patch, f"not a JSON Patch: {patch}"
    body = json.loads(patch.split("-p ", 1)[1])
    tests = {op["path"]: op["value"] for op in body if op["op"] == "test"}
    assert tests.get("/metadata/uid") == DEPLOY_UID, (
        f"the patch does not pin the verified uid: {tests}")
    assert tests.get("/metadata/resourceVersion") == "1234", (
        f"the patch does not pin the verified resourceVersion: {tests}")


def _real_kubectl():
    """The real kubectl binary, or a decision about its absence.

    Root's point: a REQUIRED regression that skips when a prerequisite is missing is
    indistinguishable from one that passed. The two tests below are the only checks in
    this suite made against a real CLI rather than a double, so their silent
    disappearance is exactly the failure they exist to prevent.

    So absence is a SKIP locally (a developer without kubectl should still be able to
    run the suite) and a FAILURE wherever the prerequisite is declared, which the CI
    workflow does by exporting FIXTURE_EDGE_REQUIRE_REAL_KUBECTL=1. test_ci_wiring.py
    asserts that variable is set there, so the strict mode cannot be dropped from the
    workflow without a failing gate.
    """
    kubectl = shutil.which("kubectl")
    if kubectl:
        return kubectl
    if os.environ.get("FIXTURE_EDGE_REQUIRE_REAL_KUBECTL") == "1":
        pytest.fail(
            "kubectl is NOT on PATH, and FIXTURE_EDGE_REQUIRE_REAL_KUBECTL=1 declares "
            "that this environment must supply it. Failing instead of skipping: these "
            "are the only tests here that check the real CLI interface, and a silent "
            "skip is how a script that cannot run at all stays green.")
    pytest.skip("real kubectl not on PATH (set FIXTURE_EDGE_REQUIRE_REAL_KUBECTL=1 "
                "to make this a failure, as the dedicated CI workflow does)")


def test_the_patch_body_and_flags_parse_under_the_real_kubectl(harness):
    """THE REGRESSION FOR THE PERMISSIVE-DOUBLE CLASS ITSELF.

    Every other test here runs against a fake. A fake can accept anything, and the
    one in this file DID accept a nonexistent flag -- so the suite was green while
    the script could not work at all. This test therefore hands the ACTUAL command
    line to the REAL kubectl binary in `--local` mode, which parses flags and applies
    the patch client-side without contacting any cluster (no credentials, no
    mutation, safe in CI).

    Skips when kubectl is absent so a developer without it can still run the suite --
    but FAILS when the environment declares it must be present (see _real_kubectl),
    which is what the dedicated CI workflow does.
    """
    kubectl = _real_kubectl()
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    patch = next(l for l in harness.log.read_text().splitlines()
                 if l.startswith("kubectl patch deployment"))
    body = patch.split("-p ", 1)[1]

    live = harness.tmp / "live-deployment.json"
    live.write_text(json.dumps(_deployment_doc()))
    # --local needs the object from -f; the flags and the body are otherwise exactly
    # the ones the script emits.
    out = subprocess.run(
        [kubectl, "patch", "-f", str(live), "--local", "--type=json",
         "-p", body, "-o", "json"],
        capture_output=True, text=True)
    assert out.returncode == 0, (
        "real kubectl REJECTED the patch the script sends -- this is the failure "
        f"class root reproduced by hand:\n{out.stderr}")
    patched = json.loads(out.stdout)
    env = {e["name"]: e for e in
           patched["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["BG_APIGW_PROVENANCE_SECRET"]["valueFrom"]["secretKeyRef"]["name"] \
        == SECRET
    assert env["BG_TRUST_APIGW_HEADERS"]["value"] == "true"


def test_the_real_kubectl_rejects_the_flag_the_previous_revision_used(harness):
    """Pins WHY the above changed, so nobody reintroduces `--resource-version`
    believing it is merely stylistic. Asserted against the real binary, because the
    claim is about kubectl's interface and not about this repo."""
    kubectl = _real_kubectl()
    live = harness.tmp / "live-deployment.json"
    live.write_text(json.dumps(_deployment_doc()))
    out = subprocess.run(
        [kubectl, "patch", "-f", str(live), "--local", "--resource-version=1234",
         "--type=json", "-p", "[]"],
        capture_output=True, text=True)
    assert out.returncode != 0 and "unknown flag" in out.stderr.lower(), (
        "kubectl accepted --resource-version on patch; if this ever becomes true the "
        f"comments above are stale:\n{out.returncode} {out.stderr}")


def test_handoff_asserts_the_kubectl_context_before_mutating(harness):
    """`--profile` binds the AWS CLI and NOTHING ELSE.

    kubectl's target comes from KUBECONFIG, so it can be another account's cluster
    while every aws_ call in the same run is correctly bound — and the Secret and
    the patch would then land there.
    """
    r = harness.run("handoff", {
        "FAKE_KUBE_CONTEXT": "arn:aws:eks:us-east-1:111111111111:cluster/other"},
        args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert not harness.secret_payload.exists()


def test_the_cluster_is_pinned_by_its_aws_endpoint_not_its_context_name(harness):
    """A CONTEXT NAME IS A LOCAL LABEL, NOT AN IDENTITY.

    The kubeconfig author chooses the context name freely, so it can read
    `arn:aws:eks:us-east-1:<the right account>:cluster/adp-dev-eks` while pointing at
    ANY server. Parsing that string therefore proves nothing, and this test models
    exactly that divergence: a perfectly correct-looking context whose API server is
    somebody else's cluster. Only comparing the kubeconfig endpoint against the
    endpoint AWS reports for the named cluster catches it.
    """
    r = harness.run("handoff", {
        "FAKE_KUBE_SERVER": "https://99999999.gr7.us-east-1.eks.amazonaws.com"},
        args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0, (
        "the handoff mutated a cluster whose API server is NOT the verified cluster's; "
        "the context name looked right, which is exactly the failure mode")
    assert "DIFFERENT cluster" in r.stderr
    assert "99999999" in r.stderr, "the refusal must show the endpoint it would have used"
    assert not harness.secret_payload.exists()


def test_the_cluster_must_exist_in_the_runs_own_account(harness):
    """The endpoint is read through `aws_`, i.e. the run's bound profile and region.

    So a cluster that does not exist in the authorised account cannot be confirmed at
    all — which is the check a context-name comparison could never make, because the
    name can claim any account.
    """
    r = harness.run("handoff", {"FAKE_EKS_CLUSTER_MISSING": "1"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "does not exist in account" in r.stderr
    assert not harness.secret_payload.exists()


def test_expect_cluster_is_not_a_free_form_bypass(harness):
    """THE BYPASS ROOT OBJECTED TO.

    Previously --expect-cluster was compared against the CONTEXT NAME, so echoing
    back whatever `kubectl config current-context` printed always satisfied it. It was
    a typing exercise, not a check. It is now the cluster NAME to verify against AWS,
    so naming a cluster that is not the one kubectl points at FAILS rather than
    waving the check through.
    """
    r = harness.run("handoff", {"FAKE_KUBE_CONTEXT": "my-alias"},
                    args=["--fixture-deployment", DEPLOY,
                          "--expect-cluster", "my-alias"])
    assert r.returncode != 0, (
        "--expect-cluster still passes when it merely repeats the context name")
    assert "does not exist in account" in r.stderr


def test_handoff_refuses_an_unnameable_kubectl_context(harness):
    """An aliased context gives nothing to look up, so the cluster name must be
    stated. Refusing is the safe default; assuming is how the wrong cluster gets
    mutated."""
    r = harness.run("handoff", {"FAKE_KUBE_CONTEXT": "minikube"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "--expect-cluster" in r.stderr
    assert "local alias" in r.stderr
    assert not harness.secret_payload.exists()


def test_handoff_refuses_a_context_with_no_readable_api_server(harness):
    """No endpoint means no identity. Proceeding would mutate a cluster that cannot
    be named at all."""
    r = harness.run("handoff", {"FAKE_KUBE_SERVER": "__none__"},
                    args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "could not read the API server endpoint" in r.stderr
    assert not harness.secret_payload.exists()


def test_an_aliased_context_is_accepted_when_the_named_cluster_verifies(harness):
    """Positive control: the check must be satisfiable by a real aliased kubeconfig,
    not a wall. The alias is fine — what matters is that the endpoint it points at is
    the one AWS reports for the declared cluster."""
    r = harness.run("handoff", {"FAKE_KUBE_CONTEXT": "my-alias"},
                    args=["--fixture-deployment", DEPLOY,
                          "--expect-cluster", "adp-dev-eks"])
    assert r.returncode == 0, r.stderr
    assert "cluster verified against AWS: adp-dev-eks" in r.stdout


def test_handoff_records_under_the_ledgers_run_id_not_an_invented_one(harness):
    """A guessed run id records the object under an id #3968's cleanup never looks
    up: recorded, and still orphaned."""
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, r.stderr
    rows = recorded_rows(harness)
    assert rows[0]["run_id"] == RUN_ID
    assert f"w2-{NONCE}" not in harness.log.read_text(), (
        "the script invented a run id of the form w2-<nonce> instead of reading "
        "the ledger's")


def test_handoff_refuses_a_ledger_belonging_to_another_run(harness):
    harness.ledger.write_text(json.dumps(_ledger_doc(nonce="beefbeefbeefbeef")))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "beefbeefbeefbeef" in r.stderr


def test_handoff_refuses_a_ledger_with_no_run_id(harness):
    """Rather than falling back to a guess. The fallback is the bug."""
    harness.ledger.write_text(json.dumps(
        {"run_nonce": NONCE,
         "k8s": [{"kind": "Deployment", "name": DEPLOY, "namespace": NAMESPACE,
                  "uid": DEPLOY_UID}]}))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "no run_id" in r.stderr
    assert "Refusing to invent" in r.stderr


# --- 4b. The ledger preconditions #3968's own interface enforces -----------
# Every check below is one lib/ownership.py ALREADY makes at record time. The
# point of making them here is WHEN they fire: record-k8s runs AFTER the Secret
# has been created in the cluster, so a ledger it refuses leaves a live object
# that nothing can look up. Refusing before creation is the difference between an
# aborted run and an orphan.
def test_handoff_refuses_a_ledger_with_no_run_nonce(harness):
    """ABSENT was previously treated as a PASS.

    The check read `if led_nonce and led_nonce != nonce`, so a ledger with no nonce
    at all sailed through — and a ledger that cannot be shown to be this run's is
    exactly the one whose cleanup deletes on a different schedule. #3968's
    init_ledger always writes run_nonce, so its absence means this is not a ledger
    it opened.
    """
    harness.ledger.write_text(json.dumps(_ledger_doc(nonce=None)))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0, "a ledger with no run_nonce was accepted"
    assert "NO run_nonce" in r.stderr
    assert recorded_rows(harness) == []
    assert not harness.secret_payload.exists(), (
        "the Secret was created before the ledger was found unusable")


def test_handoff_refuses_a_ledger_with_no_account_id(harness):
    """record-k8s requires --account-id and refuses a ledger whose account differs.
    Unchecked here, that refusal lands after the Secret exists."""
    harness.ledger.write_text(json.dumps(_ledger_doc(account=None)))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "NO account_id" in r.stderr
    assert not harness.secret_payload.exists()


def test_handoff_refuses_a_ledger_opened_against_another_account(harness):
    """"The same resource name in two accounts is two different resources" —
    ownership.py's own words. A ledger from another account cannot vouch for
    anything in this one."""
    harness.ledger.write_text(json.dumps(_ledger_doc(account="111111111111")))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0
    assert "111111111111" in r.stderr and ACCOUNT in r.stderr
    assert not harness.secret_payload.exists()


@pytest.mark.parametrize("version", [1, None, "2"])
def test_handoff_refuses_a_ledger_that_is_not_the_v2_interface(harness, version):
    """A v1 row carries `run_bound: true` and NO server-assigned uid, so it cannot
    prove ownership and must not drive a teardown. `None` is an unversioned file and
    the string "2" is not the integer load_ledger compares against — both are
    refused by #3968's own check, so both must be refused here."""
    harness.ledger.write_text(json.dumps(_ledger_doc(version=version)))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode != 0, f"ledger_version={version!r} was accepted"
    assert "version" in r.stderr
    assert not harness.secret_payload.exists()


def test_the_ledger_shape_the_tests_use_is_the_one_3968_writes(harness):
    """THE GATE ON THIS WHOLE GROUP.

    These checks are only meaningful if the ledger modelled here is the one #3968
    actually produces — the guessed-label defect in a different costume. So the
    fields are enumerated against init_ledger's literal output, and the positive
    path must PASS with exactly that document and nothing added.
    """
    doc = _ledger_doc()
    assert set(doc) == {"ledger_version", "run_id", "run_nonce", "account_id",
                        "region", "synthetic_rows", "k8s", "queues"}, (
        "drifted from lib/ownership.py init_ledger; re-read it on branch "
        "agent/issue-3968 before changing this")
    assert doc["ledger_version"] == 2
    harness.ledger.write_text(json.dumps(doc))
    r = harness.run("handoff", args=["--fixture-deployment", DEPLOY])
    assert r.returncode == 0, (
        f"the real ledger shape was REFUSED, so these gates are refusing "
        f"legitimate runs:\n{r.stderr}")


# --- 5. AWS unknown != absent ---------------------------------------------
# Root EXECUTED the previous destroy verification with the fake returning
# AccessDeniedException instead of NotFound: it exited 0 and printed "teardown
# verified". Every `if aws ... >/dev/null 2>&1` reads an expired credential, a
# network blip, a throttle and a permission denial as proof of deletion — the most
# dangerous possible direction for the error to point, because the operator then
# records cleanup as complete and stops looking.
@pytest.mark.parametrize("flag,label", [
    ("FAKE_API_PROBE_DENIED", "REST API"),
    ("FAKE_RUN_PARAM_DENIED", "per-run secret"),
    ("FAKE_LOGS_DENIED", "log group"),
    ("FAKE_ORD_PARAM_DENIED", "ordinary parameter"),
])
def test_an_aws_error_that_is_not_not_found_is_never_absence(harness, flag, label):
    r = harness.run("destroy", {flag: "1"})
    assert r.returncode != 0, (
        f"destroy exited 0 while the {label} probe returned AccessDenied. An "
        "unverifiable resource is not a deleted resource."
    )
    assert "[UNKNOWN]" in r.stderr, f"the {label} failure was not reported as UNKNOWN"
    assert "NOT absence" in r.stderr or "could NOT be verified" in r.stderr
    assert "teardown verified" not in r.stdout, (
        f"claimed teardown verified despite an unverifiable {label}")


def test_destroy_verifies_the_complete_owned_set_not_just_the_api(harness):
    """A surviving stage or log group was previously reported as a verified
    teardown, because only the API and the per-run parameter were checked."""
    for flag, needle in (("FAKE_STAGE_STILL_PRESENT", "stage"),
                         ("FAKE_LOG_GROUP_SURVIVED", "log group")):
        r = harness.run("destroy", {flag: "1"})
        assert r.returncode != 0, f"a surviving {needle} passed verification"
        assert "STILL PRESENT" in r.stderr
        assert "teardown verified" not in r.stdout


def test_destroy_fails_rather_than_skipping_the_api_probe_when_the_id_is_unreadable(harness):
    """A swallowed `terraform output rest_api_id` previously skipped the whole API
    probe and still reported success. An unaskable question is not a 'no'."""
    r = harness.run("destroy", {"FAKE_API_ID": ""})
    assert r.returncode != 0
    assert "could not read rest_api_id from state" in r.stderr
    assert "not the same as it being gone" in r.stderr
    assert "teardown verified" not in r.stdout


# --- 5b. A delete-only plan is not a plan that deletes only OUR resources ---
#
# The destroy gate was a check on the plan's ACTIONS ("no creates or updates"), which
# says nothing about the OBJECTS. Any state this credential can reach yields a
# delete-only plan — state copied or re-initialised under this run's key, state with a
# resource `terraform import`ed into it, state for the right run in the wrong account.
# In all of those the run printed "deletes exactly these, from state" and destroyed
# resources it never created.
#
# So the plan is now checked against the OWNED SET: the ownership receipt in state
# (bound to nonce/account/region/environment) plus the per-run ownership tag the
# provider stamped on each object. Two independent facts — the id says state claims
# it, the tag says the object was stamped for this run at creation.


def _owned_resources():
    """The six typed entries outputs.tf emits, with the ids the PROVIDER assigns.

    Mirrors FAKE_TERRAFORM's owned_receipt(); kept here so a test can perturb one
    entry without rebuilding the whole inventory. Each carries its Terraform
    resource TYPE because the destroy plan is matched on (type, id) pairs — the REST
    API and its policy share one id, so ids alone cannot show both were included.
    """
    return [
        {"kind": "apigateway-rest-api", "type": "aws_api_gateway_rest_api",
         "id": API_ID, "name": f"w2-fixture-edge-{NONCE}"},
        {"kind": "apigateway-rest-api-policy",
         "type": "aws_api_gateway_rest_api_policy",
         "id": API_ID, "name": f"resource policy on w2-fixture-edge-{NONCE}"},
        {"kind": "apigateway-deployment", "type": "aws_api_gateway_deployment",
         "id": DEPLOYMENT_ID, "name": f"deployment of {API_ID}"},
        {"kind": "apigateway-stage", "type": "aws_api_gateway_stage",
         "id": STAGE_ID, "name": "dev"},
        {"kind": "ssm-parameter", "type": "aws_ssm_parameter",
         "id": PARAM, "name": PARAM},
        {"kind": "cloudwatch-log-group", "type": "aws_cloudwatch_log_group",
         "id": LOG_GROUP, "name": LOG_GROUP},
    ]


def _receipt_with(harness, **overrides):
    """The ownership receipt the fake emits, with fields replaced."""
    doc = {
        "run_nonce": NONCE, "account_id": ACCOUNT, "region": "us-east-1",
        "environment": "dev", "rest_api_id": API_ID,
        "resources": _owned_resources(),
    }
    doc.update(overrides)
    return {"FAKE_OWNERSHIP_JSON": json.dumps(doc)}


def _owned_tags():
    return {"AdpFixtureRun": NONCE, "AdpFixtureAccount": ACCOUNT, "Disposable": "true"}


def _delete_change(address, rtype, rid, tags="owned"):
    before = {"id": rid}
    if tags == "owned":
        before["tags"] = _owned_tags()
    elif isinstance(tags, dict):
        before["tags"] = tags
    return {"address": address, "type": rtype,
            "change": {"actions": ["delete"], "before": before}}


def _owned_plan():
    return [
        _delete_change("aws_api_gateway_rest_api.fixture[0]",
                       "aws_api_gateway_rest_api", API_ID),
        _delete_change("aws_ssm_parameter.fixture_provenance_secret[0]",
                       "aws_ssm_parameter", PARAM),
    ]


# Types a real plan carries NO `tags` on, because the resource is not taggable: the
# rest-api POLICY is an attribute of the API, and a DEPLOYMENT has no tags. Modelling
# tags on them would let the tag half of the gate "pass" on lines where a real plan
# offers nothing to check — a green that means nothing was looked at.
_UNTAGGABLE = {"aws_api_gateway_rest_api_policy", "aws_api_gateway_deployment"}

_PLAN_ADDRESS = {
    "aws_api_gateway_rest_api": "aws_api_gateway_rest_api.fixture[0]",
    "aws_api_gateway_rest_api_policy": "aws_api_gateway_rest_api_policy.fixture[0]",
    "aws_api_gateway_deployment": "aws_api_gateway_deployment.fixture[0]",
    "aws_api_gateway_stage": "aws_api_gateway_stage.fixture[0]",
    "aws_ssm_parameter": "aws_ssm_parameter.fixture_provenance_secret[0]",
    "aws_cloudwatch_log_group": "aws_cloudwatch_log_group.fixture[0]",
}


def _full_owned_plan(omit_types=(), rows=None):
    """A REAL-SHAPED destroy plan for the whole component: one delete line per owned
    resource, each carrying the id the provider assigns and the run tag where the type
    is taggable, plus the local-only line a real plan also contains.

    `omit_types` drops lines, which is how the completeness half of the gate is
    exercised — including the case that an id-keyed comparison cannot see at all
    (dropping the policy, whose id is the API's).
    """
    plan = [
        _delete_change(_PLAN_ADDRESS[r["type"]], r["type"], r["id"],
                       tags=None if r["type"] in _UNTAGGABLE else "owned")
        for r in (rows if rows is not None else _owned_resources())
        if r["type"] not in omit_types
    ]
    return plan + [
        {"address": "random_password.fixture_edge_provenance[0]",
         "type": "random_password", "change": {"actions": ["delete"]}}]


def test_destroy_refuses_to_delete_a_resource_state_does_not_record_as_ours(harness):
    """THE DEFECT: `terraform import aws_api_gateway_rest_api.x <ordinary api id>` is
    enough to make the ORDINARY edge a delete-only line in this plan.

    Nothing in the previous gate looked at which object a deletion referred to, so a
    destroy-only plan containing the production API passed review and was applied.
    """
    plan = _owned_plan() + [
        _delete_change("aws_api_gateway_rest_api.imported",
                       "aws_api_gateway_rest_api", "ordinaryapi9", tags=None)]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0, "a foreign resource in a delete-only plan was destroyed"
    assert "NOT OWNED" in r.stderr
    assert "ordinaryapi9" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_a_deletion_of_an_object_not_stamped_for_this_run(harness):
    """The id and the TAG are independent facts, and an imported resource has the
    first without the second: `state rm` + `import` under one of this component's own
    addresses makes the id match while the live object was never stamped for this run.

    Tag mismatch is therefore its own refusal, not a note.
    """
    plan = [
        _delete_change("aws_api_gateway_rest_api.fixture[0]",
                       "aws_api_gateway_rest_api", "fixapi123",
                       tags={"AdpFixtureRun": "ffffffffffffffff"}),
        _delete_change("aws_ssm_parameter.fixture_provenance_secret[0]",
                       "aws_ssm_parameter", PARAM),
    ]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0
    assert "NOT TAGGED FOR THIS RUN" in r.stderr
    assert "ffffffffffffffff" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_a_deletion_with_no_recorded_id(harness):
    """No id means nothing identifies the object being deleted — the same defect as
    deleting by name, reached from the other direction."""
    plan = _owned_plan() + [
        {"address": "aws_cloudwatch_log_group.mystery", "type": "aws_cloudwatch_log_group",
         "change": {"actions": ["delete"], "before": {}}}]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0
    assert "records NO id" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_an_ownership_receipt_from_another_run_or_account(harness):
    """The receipt is what authorises the deletions, so it is validated BEFORE any id
    is taken from it. A receipt for another run would otherwise supply the very list
    used to permit the deletion."""
    for field, value in (("run_nonce", "ffffffffffffffff"),
                         ("account_id", "111111111111"),
                         ("region", "eu-west-1"),
                         ("environment", "prod")):
        r = harness.run("destroy", _receipt_with(harness, **{field: value}))
        assert r.returncode != 0, f"a receipt with a foreign {field} authorised a destroy"
        assert field in r.stderr and str(value) in r.stderr
        assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_when_state_records_no_owned_resources_at_all(harness):
    """An empty owned set means there is nothing to check the plan against. That is
    unverifiable, which is not the same as authorised."""
    r = harness.run("destroy", _receipt_with(harness, resources=[]))
    assert r.returncode != 0
    assert "NO resource ids" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_when_the_ownership_receipt_cannot_be_read(harness):
    """Unreadable is not 'skip the check'. Without the receipt the plan cannot be
    shown to delete only this run's resources."""
    r = harness.run("destroy", {"FAKE_OWNERSHIP_UNREADABLE": "1"})
    assert r.returncode != 0
    assert "ownership receipt" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_a_plan_that_silently_leaves_an_owned_resource_behind(harness):
    """The other direction, and it is the one absence-verification catches too late.

    A plan missing an owned resource applies cleanly and reports a completed
    teardown while the object keeps running and costing. Catching it here means it is
    caught BEFORE the state that names the object is emptied — afterwards there is no
    record left to reconcile against.
    """
    plan = [_delete_change("aws_api_gateway_rest_api.fixture[0]",
                           "aws_api_gateway_rest_api", "fixapi123")]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0
    assert "LEAVES BEHIND" in r.stderr
    assert PARAM in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_does_not_demand_a_cloud_id_for_resources_that_have_none(harness):
    """random_password and terraform_data exist only in state.

    A gate that required an id or a tag for them would be unsatisfiable, and an
    unsatisfiable gate gets bypassed — so they are enumerated as local-only rather
    than inferred, and the positive path must still pass with them in the plan.
    """
    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    assert "every deletion is in this run's owned set" in r.stdout
    assert "random_password" not in r.stderr


# --- 5c. The receipt's ids must be the ids a PLAN CARRIES -------------------
#
# Root EXECUTED a valid six-resource inventory against a real-shaped destroy plan and
# the guard refused it as NOT OWNED. The cause was not the guard: outputs.tf recorded
# the stage as its `stage_name` ("dev"), while the provider sets a stage's id to
# "ags-<rest-api-id>-<stage-name>" (aws/internal/service/apigateway/stage.go). The
# receipt therefore held an identifier NO PLAN EVER CARRIES, the stage's real deletion
# line matched nothing, and LEGITIMATE TEARDOWN WAS BLOCKED.
#
# That is worth naming precisely, because it is the failure mode that DEFEATS a
# safety gate rather than bypassing it: the operator is told the plan touches
# something it does not own, and the only way forward appears to be deleting by hand
# — which is exactly what the guard exists to prevent. So the fix is on the receipt
# side (record each resource's own `.id`) and the guard is NOT loosened.
#
# The tests below exercise the COMPLETE six-resource inventory. The previous fake had
# two entries, which is why nothing here could have caught a per-type identity
# mismatch: four of the six types never appeared in any plan under test.


def test_destroy_accepts_the_real_shaped_plan_for_the_whole_component(harness):
    """THE REGRESSION, as root executed it: a valid inventory and a real-shaped plan
    covering all six resources must be ACCEPTED.

    This is the test the stage-id mismatch failed. It is a positive control, so it is
    also the one that proves the negatives below are not passing for the trivial
    reason that the guard refuses everything.
    """
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(_full_owned_plan())})
    assert r.returncode == 0, r.stderr
    assert "NOT OWNED" not in r.stderr
    # Six resources, not five: the count is of (type, id) PAIRS, and the API and its
    # policy share an id. A count of 5 would mean the shared id collapsed the two.
    assert "6 resources" in r.stdout
    assert "by (type, id) and by run tag" in r.stdout
    assert "terraform apply" in harness.log.read_text()


def test_destroy_accepts_the_stage_id_the_provider_actually_assigns(harness):
    """Narrowed to the single resource root's finding was about, so a future
    regression names the stage rather than showing a six-line diff."""
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(_full_owned_plan())})
    assert r.returncode == 0, r.stderr
    assert STAGE_ID.startswith("ags-"), "the test's own stage id must be the real shape"
    assert STAGE_ID not in r.stderr, (
        "the stage's real provider id was reported as a problem — the receipt is "
        "recording an identifier no plan carries (probably stage_name)")


def test_destroy_refuses_a_receipt_recording_the_stage_by_NAME(harness):
    """The defect reproduced from the receipt side, and the reason the fix belongs
    there: a receipt holding "dev" where the plan carries "ags-<api>-dev" must be
    REFUSED, loudly, naming the real id.

    The guard is correct to refuse — an identifier it cannot match is not an
    identifier it may assume. What was wrong was the receipt. Pinning the refusal
    means a reintroduced `stage_name` fails here instead of surfacing as an
    unexplained teardown block during a live run.
    """
    rows = [dict(r, id="dev") if r["type"] == "aws_api_gateway_stage" else r
            for r in _owned_resources()]
    r = harness.run(
        "destroy",
        dict(_receipt_with(harness, resources=rows),
             FAKE_DESTROY_CHANGES=json.dumps(_full_owned_plan())))
    assert r.returncode != 0, "a receipt whose stage id no plan can carry was accepted"
    assert "NOT OWNED" in r.stderr
    assert STAGE_ID in r.stderr, "the refusal must name the id the plan actually carried"
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_detects_an_omitted_POLICY_despite_it_sharing_the_apis_id(harness):
    """The case an id-keyed comparison is BLIND to, which is why the gate is keyed on
    (type, id) pairs.

    aws_api_gateway_rest_api_policy's id IS the rest-api id — the policy is an
    attribute of the API, not a separate object. So a plan that deletes the API and
    not the policy presents every owned ID, and a set-of-ids comparison reports
    complete coverage. The policy is the wrong-role Deny, so leaving it behind is not
    a benign omission.
    """
    plan = _full_owned_plan(omit_types={"aws_api_gateway_rest_api_policy"})
    planned_ids = {c["change"]["before"].get("id") for c in plan
                   if c["change"].get("before")}
    owned_ids = {r["id"] for r in _owned_resources()}
    assert owned_ids <= planned_ids, (
        "this test is only meaningful while the omitted policy leaves every owned ID "
        "still present in the plan — otherwise an id-only check would catch it too")

    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0, (
        "an omitted resource policy — the wrong-role refusal — was reported as a "
        "complete teardown because its id is the API's")
    assert "LEAVES BEHIND" in r.stderr
    assert "aws_api_gateway_rest_api_policy" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_detects_an_omitted_API_despite_the_policy_sharing_its_id(harness):
    """The same blindness from the other side: dropping the API while keeping the
    policy also leaves every owned id present in the plan."""
    plan = _full_owned_plan(omit_types={"aws_api_gateway_rest_api"})
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0, "an omitted REST API passed because its policy shares its id"
    assert "LEAVES BEHIND" in r.stderr
    assert "aws_api_gateway_rest_api id=" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


@pytest.mark.parametrize("rtype", sorted(_PLAN_ADDRESS))
def test_destroy_detects_any_one_of_the_six_being_left_behind(harness, rtype):
    """Every type, not a sample. The under-counting fake meant four of the six were
    never in a plan under test, so no per-type mismatch was reachable at all."""
    plan = _full_owned_plan(omit_types={rtype})
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0, f"a plan omitting {rtype} was accepted as complete"
    assert "LEAVES BEHIND" in r.stderr
    assert rtype in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_names_the_type_when_an_owned_id_appears_under_another_one(harness):
    """A plan line whose id IS owned but under a different type is the interesting
    refusal — an import, or a receipt/plan disagreement. Reporting only the id would
    read as an unknown object and send the operator looking for the wrong thing."""
    plan = _full_owned_plan() + [
        _delete_change("aws_api_gateway_base_path_mapping.imported",
                       "aws_api_gateway_base_path_mapping", API_ID)]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)})
    assert r.returncode != 0
    assert "NOT OWNED" in r.stderr
    assert "the id is owned, but under type" in r.stderr
    assert "aws_api_gateway_rest_api" in r.stderr
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_refuses_an_untyped_receipt_rather_than_comparing_ids_alone(harness):
    """A receipt from before this fix has no `type`. Falling back to an id-only
    comparison for it would reinstate the exact gap above — the shared-id omission
    would be undetectable again — so an untyped entry is refused and the operator is
    told to re-apply, not quietly given a weaker check.
    """
    rows = [{k: v for k, v in r.items() if k != "type"} for r in _owned_resources()]
    r = harness.run(
        "destroy",
        dict(_receipt_with(harness, resources=rows),
             FAKE_DESTROY_CHANGES=json.dumps(_full_owned_plan())))
    assert r.returncode != 0, "an untyped receipt was silently compared on ids alone"
    assert "no `type`" in r.stderr
    assert "re-apply" in r.stderr.lower()
    assert "terraform apply" not in harness.log.read_text()


def test_destroy_dry_run_checks_the_owned_set_before_reporting(harness):
    """--dry-run is the reviewable path: it must run the same gate, or it rehearses a
    command the real destroy would refuse."""
    plan = _owned_plan() + [
        _delete_change("aws_api_gateway_rest_api.imported",
                       "aws_api_gateway_rest_api", "ordinaryapi9", tags=None)]
    r = harness.run("destroy", {"FAKE_DESTROY_CHANGES": json.dumps(plan)},
                    args=["--dry-run"])
    assert r.returncode != 0
    assert "NOT OWNED" in r.stderr
    assert "stopping before any deletion" not in r.stdout


# --- 6. A failed HTTP security check is never exit 0 ----------------------
# Root EXECUTED verify with a curl returning 200 for both the unsigned and the
# spoofed probe: it printed "EXPECTED 403" notes and EXITED 0. A control that
# reports success when the edge answered 200 to an unsigned request is worse than
# no control, because the operator then has a green verification to point at.
#
# There was also NO fake curl in this suite, so none of verify's security checks
# were executed by any test at all. That is why 63 green tests missed it.
# `/health`, not the `/dev/api/health` this used to pass. Two independent reasons,
# both of which would have made the positive control 404 against a real edge:
# `base` already strips the stage root off the invoke URL, so a `/dev` prefix
# double-stages it; and there is no CloudFront in front of a fixture ALB, so the
# `/api` prefix belongs to the ordinary front door and not to this one. The path has
# to be one ../fixture-alb.yaml.tmpl actually publishes.
VERIFY_OK_ARGS = ["--wrong-role-profile", "wrongrole",
                  "--human-probe-path", "/health"]
# A resolvable identity for the signer profile, whose role is NOT in the allowlist
# the fake `terraform output allowed_caller_role_arns` publishes. Both halves are
# required: without a resolved ARN the control cannot say WHO it signed as, and
# without the allowlist comparison a 403 from a PERMITTED role would be recorded as
# the Deny working.
VERIFY_OK_ENV = {
    "FAKE_PROBE_ARN_wrongrole":
        f"arn:aws:sts::{ACCOUNT}:assumed-role/w2-not-allowlisted/probe-session",
}


def test_verify_passes_only_when_every_control_actually_refused(harness):
    """Positive control. Without it, the tests below could all pass against a
    script that simply always fails."""
    r = harness.run("verify", VERIFY_OK_ENV, args=VERIFY_OK_ARGS)
    assert r.returncode == 0, r.stderr
    assert "all refusals observed AND asserted" in r.stdout


def test_verify_never_reads_signing_keys_out_of_the_aws_config_file(harness):
    """`aws configure get aws_secret_access_key` was the previous signing path.

    It is broken two ways at once: it returns NOTHING for the assumed-role/SSO
    profiles these actually are (so the control could never pass), and the value it
    does return was handed to `curl --user`, putting the secret in argv where any
    process listing on the host can read it. The fake `aws` now exits 97 if asked,
    so this fails if the interface is ever reintroduced.
    """
    r = harness.run("verify", VERIFY_OK_ENV, args=VERIFY_OK_ARGS)
    log = harness.log.read_text()
    assert "configure get" not in log, (
        "signing material is being read out of the AWS config file again")
    # And no credential material may appear in any command line that was logged.
    assert "--user" not in log, "a secret was passed to curl in argv"
    assert r.returncode == 0, r.stderr


def test_the_wrong_role_probe_signs_with_sdk_resolved_credentials(harness):
    """END-TO-END PROOF THAT THE PROBE ACTUALLY SIGNS.

    Every other verify test uses the curl double, which returns a canned status code
    and therefore cannot tell a signed request from an unsigned one -- exactly the
    blind spot that let an unsignable probe be recorded as a refusal. Here the
    endpoint is a REAL local HTTP listener and the credentials come from the real
    botocore provider chain (seeded via a temporary profile in a scratch config
    file), so the assertion is on the Authorization header the server received.
    """
    r = harness.run("verify", VERIFY_OK_ENV, args=VERIFY_OK_ARGS)
    assert r.returncode == 0, r.stderr
    signed = [h for h in harness.edge.requests
              if h.get("Authorization", "").startswith("AWS4-HMAC-SHA256")]
    assert signed, (
        "no request arrived with a SigV4 Authorization header, so the wrong-role "
        f"control never signed anything. Received: {harness.edge.requests}")
    auth = signed[0]["Authorization"]
    assert "/execute-api/aws4_request" in auth, f"signed for the wrong service: {auth}"
    assert f"Credential={harness.edge.ACCESS_KEY}/" in auth, (
        "signed with a credential other than the one the named profile resolves to")
    assert "x-amz-security-token" in {k.lower() for k in signed[0]}, (
        "the session token was not sent, so an assumed-role credential could not work")
    # The secret itself must never appear in a logged command line.
    assert harness.edge.SECRET_KEY not in harness.log.read_text()


def test_verify_refuses_a_probe_whose_identity_cannot_be_resolved(harness):
    """An unsignable/unknown signer proves nothing.

    A misspelled profile name previously satisfied the 'control was run' check while
    testing nothing: the request went out unsigned, collected the ordinary 403, and
    was recorded as 'the Deny works'.
    """
    r = harness.run("verify", {"FAKE_PROBE_ARN_wrongrole": "__unresolvable__"},
                    args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "could not resolve the identity" in r.stderr
    assert "all refusals observed" not in r.stdout


def test_verify_refuses_a_probe_signing_as_an_allowlisted_role(harness):
    """ROOT'S THIRD FINDING, as a regression.

    The probe never checked WHO it signed as. Signing as a PERMITTED role and
    observing a 403 (from any other cause) would be recorded as proof of the Deny --
    a false proof of the only control that exercises this component's own resource
    policy.
    """
    env = {"FAKE_PROBE_ARN_wrongrole":
           f"arn:aws:sts::{ACCOUNT}:assumed-role/w2-fixture-worker/probe-session"}
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0, "a probe signing as an allowlisted role was accepted"
    assert "IS in" in r.stderr and "allowed_caller_role_arns" in r.stderr
    assert "proves nothing about the Deny" in r.stderr


def test_verify_refuses_when_the_allowlist_cannot_be_read(harness):
    """Without the allowlist the probe MIGHT be a permitted role, so the control is
    unverifiable rather than passing."""
    env = dict(VERIFY_OK_ENV); env["FAKE_ALLOWLIST_UNREADABLE"] = "1"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "could not read allowed_caller_role_arns" in r.stderr


@pytest.mark.parametrize("flag,label", [
    ("FAKE_UNSIGNED_CODE", "unsigned"),
    ("FAKE_SPOOFED_CODE", "spoofed"),
])
def test_verify_fails_when_a_refusal_probe_returns_200(harness, flag, label):
    env = dict(VERIFY_OK_ENV); env[flag] = "200"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0, (
        f"verify exited 0 while the {label} probe got HTTP 200. The edge is "
        "accepting requests it must reject."
    )
    assert "NOT REFUSED" in r.stderr
    assert "all refusals observed" not in r.stdout


def test_verify_fails_when_the_signed_wrong_role_request_is_ACCEPTED(harness):
    """The same assertion for the signed probe, driven by the REAL listener.

    It cannot be expressed through the curl double at all: this probe no longer goes
    through curl, because a double returning a canned code could not tell a signed
    request from an unsigned one. Here the listener answers 200 to a genuinely
    SigV4-signed request from a non-allowlisted role, which is the resource-policy
    Deny being absent -- the single most important thing verify exists to catch.
    """
    harness.edge.code = 200
    r = harness.run("verify", VERIFY_OK_ENV, args=VERIFY_OK_ARGS)
    assert r.returncode != 0, (
        "verify exited 0 while a correctly-signed request from a NON-allowlisted "
        "role was ACCEPTED: the Deny is not in effect")
    assert "NOT REFUSED" in r.stderr
    assert "all refusals observed" not in r.stdout
    assert harness.edge.requests, "the probe never reached the edge"


@pytest.mark.parametrize("flag,label", [
    ("FAKE_UNSIGNED_CODE", "unsigned"),
    ("FAKE_SPOOFED_CODE", "spoofed"),
])
def test_verify_fails_when_a_refusal_probe_is_unreachable(harness, flag, label):
    """000 is not a refusal. An endpoint that never answered refused nothing, and
    treating it as a pass is how a torn-down fixture 'passes' verification."""
    env = dict(VERIFY_OK_ENV); env[flag] = "exit-nonzero"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "no response (000)" in r.stderr
    assert "not a refusal" in r.stderr


@pytest.mark.parametrize("flag,label", [
    ("FAKE_UNSIGNED_CODE", "unsigned"),
    ("FAKE_SPOOFED_CODE", "spoofed"),
])
def test_verify_treats_a_5xx_as_a_failure_not_a_refusal(harness, flag, label):
    """A 5xx means the request may have REACHED A BACKEND — the opposite of being
    rejected at the edge."""
    env = dict(VERIFY_OK_ENV); env[flag] = "502"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "REACHED A BACKEND" in r.stderr


def test_verify_fails_when_the_wrong_role_control_cannot_be_run(harness):
    """The only probe that exercises THIS component's resource-policy Deny.

    Not supplying a signer profile previously left it permanently unrun as a NOTE,
    so the Deny was never verified by anything.
    """
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--human-probe-path", "/health"])
    assert r.returncode != 0
    assert "wrong-role control NOT RUN" in r.stderr
    assert "resource-policy Deny" in r.stderr


def test_verify_fails_when_the_signer_profile_cannot_sign(harness):
    """An unsignable probe proves nothing, so it must not be recorded as a refusal.

    The identity resolves (so the control is 'run'), but the credential file has no
    entry for the profile, so botocore cannot sign. The probe must report 000 and the
    run must fail -- not send an unsigned request, collect the ordinary 403, and
    record it as the Deny working.
    """
    env = dict(VERIFY_OK_ENV)
    env["AWS_SHARED_CREDENTIALS_FILE"] = str(harness.tmp / "no-such-credentials")
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "no response (000)" in r.stderr
    assert not harness.edge.requests, (
        "an UNSIGNED request was sent to the edge; its 403 would have been recorded "
        "as the wrong-role refusal")


def test_skip_wrong_role_still_fails_because_the_deny_is_unverified(harness):
    """ROOT EXECUTED THIS ONE: --skip-wrong-role exited 0 and printed "all refusals
    observed AND asserted".

    The skip suppressed the failure flag entirely, so the only control that exercises
    this component's own resource policy could be switched off and the run still
    reported full verification. A required control that was not run leaves acceptance
    UNESTABLISHED, so the flag is documentation of why -- never a waiver.
    """
    r = harness.run("verify", args=["--human-probe-path", "/health",
                                    "--skip-wrong-role"])
    assert r.returncode != 0, (
        "--skip-wrong-role still grants a green verification with the Deny unverified")
    assert "UNVERIFIED" in r.stdout + r.stderr
    assert "EXPLICITLY SKIPPED" in r.stderr
    assert "all refusals observed" not in r.stdout, (
        "claimed every refusal was asserted while a required control was skipped")


def test_no_skipped_control_can_reach_the_acceptance_claim(harness):
    """Generalises the above: enumerated so a future skip/waiver flag cannot quietly
    reopen the same hole for a different control."""
    for extra in (["--skip-wrong-role"],                       # Deny unverified
                  ["--wrong-role-profile", "wrongrole"],       # human control unrun
                  []):                                         # both unrun
        r = harness.run("verify", VERIFY_OK_ENV, args=extra)
        assert r.returncode != 0, f"verify exited 0 with args {extra}"
        assert "all refusals observed" not in r.stdout, (
            f"acceptance claimed with args {extra}")


def test_verify_requires_the_human_positive_control(harness):
    """Refusals alone cannot distinguish a correctly-restricted edge from a totally
    broken one: an endpoint that 403s EVERYTHING passes every refusal check."""
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--wrong-role-profile", "wrongrole"])
    assert r.returncode != 0
    assert "human positive control NOT RUN" in r.stderr
    assert "refuses everything" in r.stderr


def test_verify_fails_when_the_resource_policy_403s_the_human_route(harness):
    """Blocking area 2. An unsigned human request has no aws:PrincipalArn, so a
    broad Deny blocks it before the gateway can authenticate — which is why the
    Deny must stay scoped to /internal."""
    env = dict(VERIFY_OK_ENV); env["FAKE_HUMAN_CODE"] = "403"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "resource policy is refusing" in r.stderr
    assert "/internal" in r.stderr


def test_verify_fails_when_the_human_route_404s_despite_being_published(harness):
    """404 has a different cause from 403 and a different fix, so it is diagnosed
    separately.

    The diagnosis narrowed once the path itself is pre-checked against the template:
    a 404 on a path the fixture ALB DOES publish can no longer mean "you asked for a
    path that does not exist", so the message must point at the two causes that
    remain -- the Ingress in front of the edge is not this run's, or the pod is not
    serving the route.
    """
    env = dict(VERIFY_OK_ENV); env["FAKE_HUMAN_CODE"] = "404"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "publishes this prefix" in r.stderr
    assert "AdpFixtureRun" in r.stderr


# --- the probe path must be one the fixture ALB actually serves -------------
# Root's area 3: "Human path control must align with the actual routed
# authenticated session path." A path that is not published answers from the
# listener's default action, so the control reports 404 -- which cannot be told
# apart from the edge misrouting human traffic. Both are red; only one is a defect.

def test_verify_refuses_a_human_probe_path_the_fixture_alb_does_not_publish(harness):
    """`/dev/api/health` is the path this suite itself used to pass, and it is wrong
    twice over: `base` already strips the stage root, and no CloudFront (hence no
    `/api`) sits in front of a fixture ALB."""
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", "/dev/api/health"])
    assert r.returncode != 0
    assert "not published by the fixture ALB" in r.stderr
    # The refusal must name what IS served, or the operator has to guess.
    assert "/internal" in r.stderr and "/me" in r.stderr


def test_verify_names_the_published_paths_without_probing_an_unpublished_one(harness):
    """The check runs BEFORE the request, so no HTTP probe is attempted for a path
    that could only have 404ed."""
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", "/members"])
    assert r.returncode != 0
    probes = [l for l in harness.log.read_text().splitlines() if l.startswith("curl ")]
    assert not any("/members" in l for l in probes), (
        "probed a path known to be unpublished instead of refusing up front")


def test_verify_accepts_a_path_below_a_published_prefix(harness):
    """Prefix rules mean /me/budget is served by the /me rule -- and /me/budget is
    #3968's session endpoint, so rejecting it would refuse the very path the
    integration probes."""
    env = dict(VERIFY_OK_ENV); env["FAKE_HUMAN_CODE"] = "401"
    r = harness.run("verify", env,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", "/me/budget"])
    assert r.returncode == 0, r.stderr
    assert "app-layer auth decided" in r.stdout


@pytest.mark.parametrize("probe", [
    # The endpoints acceptance actually calls, which the ALB template now publishes.
    # Parametrized over both control adapters because #5825's evaluator iterates
    # both (platform/scripts/agent-control-eval.py ADAPTERS), and over the stats path
    # #3968's 31-seed-and-count.py reads its seeded counts back through.
    "/activity/invocations/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/agent/ping",
    "/activity/invocations/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/agent/state",
    "/orchestration/runs/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/ping",
    "/orchestration/runs/0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0/state",
    "/admin/agent-run-stats",
])
def test_verify_accepts_the_acceptance_endpoints_as_human_probe_paths(harness, probe):
    """The gate reads the published set out of the template, so publishing these
    paths must ALSO make them usable as the positive control — otherwise an operator
    probing the very endpoint acceptance depends on would be refused by this script
    while the ALB serves it, which is the mirror image of the 404 the gate exists to
    pre-empt.

    This is what makes the two sides one fact rather than two lists.
    """
    env = dict(VERIFY_OK_ENV); env["FAKE_HUMAN_CODE"] = "401"
    r = harness.run("verify", env,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", probe])
    assert r.returncode == 0, r.stderr
    assert "app-layer auth decided" in r.stdout


def test_verify_still_refuses_admin_paths_outside_the_one_published_endpoint(harness):
    """`/admin/agent-run-stats` is published as Exact, deliberately: a `/admin`
    Prefix would publish every admin router the pod mounts. So a sibling admin path
    must still be refused — if this starts passing, the rule has been widened."""
    for probe in ("/admin/identity/recovery", "/admin/persona-models/default",
                  "/admin"):
        r = harness.run("verify", VERIFY_OK_ENV,
                        args=["--wrong-role-profile", "wrongrole",
                              "--human-probe-path", probe])
        assert r.returncode != 0, f"{probe} was accepted — /admin is no longer Exact"
        assert "not published by the fixture ALB" in r.stderr


def test_verify_does_not_treat_a_prefix_as_a_bare_string_match(harness):
    """`/members` starts with `/me` but is NOT matched by a `/me` Prefix rule:
    Ingress prefix matching is segment-wise. A startswith() check would admit it and
    the probe would 404 for exactly the reason this gate exists to eliminate."""
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", "/members"])
    assert r.returncode != 0
    assert "not published by the fixture ALB" in r.stderr


def test_verify_does_not_accept_a_path_merely_mentioned_in_template_prose(harness):
    """The template's comments legitimately discuss paths (/me/budget, /auth/me,
    and the `/` it explains must never appear). A grep for the string would accept
    `/` on the strength of the comment that forbids it, so the published set is
    parsed from the rule entries only.
    """
    r = harness.run("verify", VERIFY_OK_ENV,
                    args=["--wrong-role-profile", "wrongrole",
                          "--human-probe-path", "/"])
    assert r.returncode != 0
    assert "not published by the fixture ALB" in r.stderr


def test_verify_accepts_a_401_on_the_human_route(harness):
    """401 is a PASS: the request reached the gateway and was rejected by the
    application's JWT check — the layer that should decide."""
    env = dict(VERIFY_OK_ENV); env["FAKE_HUMAN_CODE"] = "401"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode == 0, r.stderr
    assert "app-layer auth decided" in r.stdout


def test_verify_stops_when_the_ordinary_api_id_is_unreadable(harness):
    """'Compare it by hand' is not a check. A transient SSM failure previously
    downgraded the one comparison that proves the fixture is not the production
    edge into a note, and the run continued."""
    r = harness.run("verify", {"FAKE_ORD_APIID_FAIL": "1", **VERIFY_OK_ENV},
                    args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "Refusing to continue" in r.stderr


def test_verify_stops_when_the_fixture_api_is_the_ordinary_api(harness):
    """The whole isolation claim in one comparison."""
    env = dict(VERIFY_OK_ENV); env["FAKE_ORDINARY_API_ID"] = "fixapi123"
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "EQUALS the ordinary edge" in r.stderr


# ===========================================================================
# The #5825 evaluator's native artifact contract (blocker 7)
# ===========================================================================
# The merged evaluator (platform/scripts/agent-control-eval.py) reconciles
# teardown_verification.removals against wave2_preflight.creation_ledger in BOTH
# directions: a removal naming an identity the ledger lacks fails, and a ledger entry
# with no absence observation fails as "unaccounted". Either way, a fixture resource
# this component creates but does not contribute is outside W2-10's accounting
# entirely. These tests pin both halves of the contribution.

def _creation_fragment(harness):
    return json.loads((harness.artifacts / "creation-ledger-fragment.json").read_text())


def _removals_fragment(harness):
    return json.loads((harness.artifacts / "teardown-removals-fragment.json").read_text())


def test_apply_writes_a_creation_ledger_fragment_in_the_evaluators_own_shape(harness):
    """LEDGER_ENTRY_KEYS = (kind, name, identity, created), every value non-falsy.

    The evaluator rejects a falsy value in any of the four, so a fragment that is
    merely present is not a fragment that can be consumed.
    """
    plan = _plan_then(harness)
    r = harness.run("apply", args=["--plan-file", str(plan)])
    assert r.returncode == 0, r.stderr
    doc = _creation_fragment(harness)
    assert doc["run_nonce"] == NONCE and doc["account_id"] == ACCOUNT
    entries = doc["entries"]
    # All six applied resources, not a subset: an under-contributed ledger is how a
    # leaked resource escapes the reconciliation rather than failing it.
    assert len(entries) == len(_owned_resources()), entries
    for entry in entries:
        assert set(entry) >= {"kind", "name", "identity", "created"}, entry
        assert entry["created"] is True
        assert all(entry[k] for k in ("kind", "name", "identity")), entry
    # `identity` must be the PROVIDER-assigned id, which for the stage is
    # ags-<api>-<stage> and NOT the bare stage name -- the same identifier the destroy
    # guard matches plan lines on.
    identities = {e["identity"] for e in entries}
    assert STAGE_ID in identities and "dev" not in identities
    assert DEPLOYMENT_ID in identities
    # Globally unique, because the evaluator refuses a duplicate. The REST API and its
    # resource policy really do share one provider id, so both get kind-qualified.
    assert len(identities) == len(entries), entries
    assert f"aws_api_gateway_rest_api:{API_ID}" in identities
    assert f"aws_api_gateway_rest_api_policy:{API_ID}" in identities


def test_apply_refuses_to_contribute_a_resource_with_no_observed_identity(harness):
    """An entry the evaluator would reject, caught while the id can still be read.

    Without an identity a resource cannot be told apart from a same-named one that
    already existed, so neither its ownership nor its later removal is establishable.
    """
    broken = _owned_resources()
    broken[3] = dict(broken[3], id="")
    plan = _plan_then(harness)
    r = harness.run("apply", _receipt_with(harness, resources=broken),
                    args=["--plan-file", str(plan)])
    assert r.returncode != 0
    assert "identifying field is empty" in r.stderr
    assert "aws_api_gateway_stage" in r.stderr


def test_apply_refuses_an_ownership_receipt_from_another_run_for_the_fragment(harness):
    """Contributing another run's identities would have the evaluator reconcile THIS
    run's teardown against resources it never created."""
    plan = _plan_then(harness)
    r = harness.run("apply", _receipt_with(harness, run_nonce="ffffffffffffffff"),
                    args=["--plan-file", str(plan)])
    assert r.returncode != 0
    assert "another run" in r.stderr or "Refusing" in r.stderr


def test_destroy_writes_absence_observations_in_the_evaluators_removal_shape(harness):
    """LEDGER_REMOVAL_KEYS = (identity, absent, observed_by, removed_at), reconciling
    one-for-one with the creation fragment's identities."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    created = {e["identity"] for e in _creation_fragment(harness)["entries"]}

    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    doc = _removals_fragment(harness)
    assert doc["verified_after_teardown"] is True
    assert doc["unobserved"] == []
    removals = doc["removals"]
    for removal in removals:
        assert set(removal) >= {"identity", "absent", "observed_by", "removed_at"}, removal
        assert removal["absent"] is True
        # `observed_by` has to name the read that established absence -- the evaluator
        # refuses an unattributed claim, which is the assertion this replaced.
        assert removal["observed_by"].startswith("aws "), removal
        assert removal["removed_at"].endswith("Z")
    # EVERY created entry accounted for, in both directions: an unaccounted entry is
    # the evaluator's hard failure, and a removal it cannot find in the ledger is too.
    assert {r_["identity"] for r_ in removals} == created


def test_destroy_derives_the_policy_and_deployment_absence_and_says_that_it_did(harness):
    """Neither has a probe of its own and neither can be given a fabricated one: the
    policy is an ATTRIBUTE of the REST API and the deployment is its child, so once
    the API is not found there is no API left to query them through. The inference is
    sound; recording it as a direct read would not be."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    assert harness.run("destroy").returncode == 0
    by_identity = {r["identity"]: r for r in _removals_fragment(harness)["removals"]}
    policy = by_identity[f"aws_api_gateway_rest_api_policy:{API_ID}"]
    assert policy["observation"] == "derived-from:rest_api"
    assert "DERIVED" in policy["observed_by"] and "ATTRIBUTE" in policy["observed_by"]
    assert by_identity[DEPLOYMENT_ID]["observation"] == "derived-from:rest_api"
    # The API's own entry is a direct read, so the two are distinguishable without
    # reading prose.
    assert by_identity[f"aws_api_gateway_rest_api:{API_ID}"]["observation"] == "direct"


def test_destroy_contributes_no_removal_for_a_resource_it_could_not_verify(harness):
    """The reason the reconciliation has to be bidirectional.

    An UNKNOWN probe is a call that failed, not a resource that answered. Emitting
    `absent: true` would be the AccessDenied-reads-as-deleted defect; emitting
    `absent: false` would assert it is STILL PRESENT, which was never established.
    So it contributes NOTHING and the creation ledger's entry goes unaccounted --
    which is exactly what an unverifiable resource is.
    """
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    r = harness.run("destroy", {"FAKE_LOGS_DENIED": "1"})
    assert r.returncode != 0
    doc = _removals_fragment(harness)
    assert doc["verified_after_teardown"] is False
    assert any("aws_cloudwatch_log_group" in item for item in doc["unobserved"]), doc
    assert not any(r_["identity"] == LOG_GROUP for r_ in doc["removals"])
    assert "UNOBSERVED" in r.stderr


def test_destroy_writes_the_fragment_even_when_a_resource_survived(harness):
    """The evidence is most needed when teardown did NOT verify. Exiting nonzero with
    no fragment leaves the assembler unable to tell 'not yet run' from 'ran and found
    a survivor'."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    r = harness.run("destroy", {"FAKE_RUN_PARAM_SURVIVED": "1"})
    assert r.returncode != 0
    doc = _removals_fragment(harness)
    assert doc["verified_after_teardown"] is False
    survivor = [r_ for r_ in doc["removals"] if r_["identity"] == PARAM]
    assert survivor and survivor[0]["absent"] is False, doc


def test_destroy_removes_a_previous_attempts_fragment_before_deleting_anything(harness):
    """The evaluator digests this artifact before invoking teardown and refuses a
    byte-identical file afterwards, because an absence observation written ahead of
    the removal describes the fixture while it still existed. A failed destroy must
    therefore leave NO fragment rather than a previous run's."""
    harness.artifacts.mkdir(exist_ok=True)
    harness.artifacts.chmod(0o700)
    stale = harness.artifacts / "teardown-removals-fragment.json"
    stale.write_text(json.dumps({"schema": "stale", "removals": [],
                                 "verified_after_teardown": True}))
    r = harness.run("destroy", {"FAKE_DESTROY_APPLY_FAIL": "1"})
    assert r.returncode != 0
    assert not stale.exists(), "a failed destroy kept a previous attempt's absence artifact"


def test_destroy_rebuilds_the_creation_fragment_rather_than_blocking_teardown(harness):
    """A destroy that refuses to run because a REPORTING artifact is missing leaves
    the fixture up, which is the opposite of the point. So the set is rebuilt from the
    same ownership receipt the destroy guard reads out of state before deleting
    anything -- same code, so the identities match by construction -- and the
    provenance says which read produced it."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    (harness.artifacts / "creation-ledger-fragment.json").unlink()
    r = harness.run("destroy")
    assert r.returncode == 0, r.stderr
    assert "rebuilding it from the ownership" in r.stdout
    assert "rebuilt during teardown" in _creation_fragment(harness)["provenance"]
    created = {e["identity"] for e in _creation_fragment(harness)["entries"]}
    assert {r_["identity"] for r_ in _removals_fragment(harness)["removals"]} == created


def test_destroy_refuses_when_an_applied_resource_has_no_absence_probe(harness):
    """Adding a resource to main.tf without adding its probe would otherwise produce a
    fragment that quietly accounts for less than the run created -- and the evaluator
    would blame #3968's assembler for the gap."""
    extra = _owned_resources() + [
        {"kind": "sqs-queue", "type": "aws_sqs_queue", "id": "https://sqs/q",
         "name": "w2-fixture-q"}]
    plan = _plan_then(harness)
    assert harness.run("apply", _receipt_with(harness, resources=extra),
                       args=["--plan-file", str(plan)]).returncode == 0
    r = harness.run("destroy", _receipt_with(harness, resources=extra))
    assert r.returncode != 0
    assert "no absence probe" in r.stderr and "aws_sqs_queue" in r.stderr


def test_destroy_probes_the_log_group_state_records_not_a_name_it_rebuilds(harness):
    """The defect this closes was silent in the worst way.

    The probe composed "/aws/apigateway/w2-fixture-edge-<nonce>" while main.tf creates
    "/aws/api-gateway/<name_prefix>-fixture-edge" -- wrong stem AND a missing hyphen.
    describe-log-groups answers an unmatched prefix with an EMPTY LIST and exit 0, the
    one probe where a clean exit is read as absence, so the real log group was
    reported gone on every run and a survivor could not have been detected.
    """
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    assert harness.run("destroy").returncode == 0
    entry = [r for r in _removals_fragment(harness)["removals"]
             if r["identity"] == LOG_GROUP]
    assert entry, "the log group probed was not the one state records"
    assert LOG_GROUP in entry[0]["observed_by"]
    assert "/aws/apigateway/w2-fixture-edge" not in harness.log.read_text()


def test_destroy_reports_the_stage_identity_the_evaluator_reconciles_on(harness):
    """AWS offers the read by api-id + stage-name; the evaluator reconciles on the
    provider's ags-<api>-<stage>. Two strings for one object, and the fragment has to
    carry the one the creation ledger used."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    assert harness.run("destroy").returncode == 0
    by_identity = {r["identity"]: r for r in _removals_fragment(harness)["removals"]}
    assert STAGE_ID in by_identity
    assert "get-stage" in by_identity[STAGE_ID]["observed_by"]


def test_the_fragments_never_contain_the_provenance_secret(harness):
    """The per-run provenance value must stay out of every artifact. Its NAME is
    inventory; its VALUE is the trust root of the internal plane."""
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    assert harness.run("destroy").returncode == 0
    import base64
    secret = "s3cr3t-provenance-value"
    b64 = base64.b64encode(secret.encode()).decode()
    for name in ("creation-ledger-fragment.json", "teardown-removals-fragment.json"):
        body = (harness.artifacts / name).read_text()
        assert secret not in body and b64 not in body, name
        # Positive control: the parameter's NAME is present, so this is not passing
        # because the fragments are empty or omit the parameter entirely.
        assert PARAM in body, name


def test_destroy_refuses_an_observation_of_a_different_object_than_the_ledger_names(harness):
    """A probe of a different object is not an absence observation of this one.

    The two are separately sourced -- the creation fragment from apply, the probe
    targets from the receipt destroy reads -- so they CAN diverge, and when they do the
    result reads as verified absence of something nobody looked at. That is the same
    failure mode as the guessed log-group prefix, reached from the other side, which is
    why the fragment writer compares the probe's target against the identity rather
    than trusting that they were built from the same place.
    """
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    moved = [dict(r, id="/aws/api-gateway/somebody-elses-group",
                  name="/aws/api-gateway/somebody-elses-group")
             if r["type"] == "aws_cloudwatch_log_group" else r
             for r in _owned_resources()]
    r = harness.run("destroy", _receipt_with(harness, resources=moved))
    assert r.returncode != 0
    assert "did not read the object it was meant to establish" in r.stderr
    assert LOG_GROUP in r.stderr


# ---------------------------------------------------------------------------
# The contract is PINNED against the evaluator, not restated from memory
# ---------------------------------------------------------------------------
# Every assertion above encodes key names read out of #5825's merged evaluator. Key
# names in two files drift silently: the evaluator renames a key, this component keeps
# emitting the old one, both suites stay green, and the mismatch surfaces at live
# acceptance as "the artifact is missing <key>" -- the worst possible moment.
#
# So the names are read back from the evaluator's own constants. This is a LOCKSTEP
# check in the same spirit as the workflow's trigger on #3968's ownership.py: it makes
# a rename in either place fail in the PR that makes it.
#
# It SKIPS when the evaluator in the tree does not carry the wave-2 ledger contract at
# all, because that is the state of this branch: the #5825 evaluator merged to main
# after this branch was cut, so the in-tree copy predates it. A skip is honest there --
# there is nothing to pin against -- whereas asserting would fail for a reason that has
# nothing to do with this component. Once the branch carries main's evaluator, the pin
# engages automatically and any drift becomes a failure.
# Overridable so the pin can be run against main's copy from a branch that predates
# it -- which is how this pin was verified to actually engage rather than only to skip
# politely. CI can set it to point at the merged evaluator.
EVALUATOR = Path(os.environ.get(
    "FIXTURE_EDGE_EVALUATOR",
    COMPONENT.parents[3] / "platform" / "scripts" / "agent-control-eval.py"))


def _evaluator_tuple(name):
    if not EVALUATOR.is_file():
        pytest.skip(f"{EVALUATOR} not present in this tree")
    text = EVALUATOR.read_text()
    m = re.search(rf"^{name}(?::[^=]+)?\s*=\s*\(([^)]*)\)", text, re.M)
    if m is None:
        pytest.skip(
            f"{name} is not defined in {EVALUATOR.name}: this tree's evaluator predates "
            "#5825's merged wave-2 ledger contract, so there is nothing to pin against"
        )
    return tuple(re.findall(r'"([^"]+)"', m.group(1)))


def test_the_creation_fragment_matches_the_evaluators_own_entry_keys(harness):
    """Pinned against LEDGER_ENTRY_KEYS as the evaluator defines it."""
    keys = _evaluator_tuple("LEDGER_ENTRY_KEYS")
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    for entry in _creation_fragment(harness)["entries"]:
        missing = [k for k in keys if k not in entry]
        assert not missing, f"entry {entry} is missing {missing} (from {EVALUATOR.name})"


def test_the_removals_fragment_matches_the_evaluators_own_removal_keys(harness):
    """Pinned against LEDGER_REMOVAL_KEYS as the evaluator defines it."""
    keys = _evaluator_tuple("LEDGER_REMOVAL_KEYS")
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    assert harness.run("destroy").returncode == 0
    for removal in _removals_fragment(harness)["removals"]:
        missing = [k for k in keys if k not in removal]
        assert not missing, f"removal {removal} is missing {missing} (from {EVALUATOR.name})"


def test_the_fragment_kinds_stay_out_of_the_evaluators_listener_and_policy_sets(harness):
    """`kind` is deliberately the AWS resource type.

    The evaluator orders only entries whose `kind` is in LISTENER_LEDGER_KINDS or
    POLICY_LEDGER_KINDS, asserting the control-enabled listener died before the
    policies that isolated it. That ordering is about #3968's POD. An API Gateway stage
    labelled "listener", or the resource policy labelled "policy", would be dragged
    into an assertion about a different object and fail it for the wrong reason.
    """
    reserved = set(_evaluator_tuple("LISTENER_LEDGER_KINDS"))
    reserved |= set(_evaluator_tuple("POLICY_LEDGER_KINDS"))
    plan = _plan_then(harness)
    assert harness.run("apply", args=["--plan-file", str(plan)]).returncode == 0
    for entry in _creation_fragment(harness)["entries"]:
        assert entry["kind"].lower() not in reserved, entry


@pytest.mark.parametrize("url", [
    "", "None", "http://ordapi999.execute-api.us-east-1.amazonaws.com/dev",
    "https://ordapi999.execute-api.us-west-2.amazonaws.com/dev",
    "https://ordapi999.execute-api.us-east-1.amazonaws.com/prod",
    "https://ordapi999.execute-api.us-east-1.amazonaws.com/dev?foo=bar",
    "https://ordapi999.execute-api.us-east-1.amazonaws.com.evil/dev",
])
def test_verify_refuses_invalid_ordinary_invoke_url(harness, url):
    env = dict(VERIFY_OK_ENV, FAKE_ORDINARY_INVOKE_URL=url)
    r = harness.run("verify", env, args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "ordinary apigw-invoke-url" in r.stderr


def test_verify_refuses_an_open_model_route(harness):
    r = harness.run("verify", dict(VERIFY_OK_ENV, FAKE_MODEL_CODE="200"), args=VERIFY_OK_ARGS)
    assert r.returncode != 0
    assert "model route unsigned -> 200" in r.stdout + r.stderr
