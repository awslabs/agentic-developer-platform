#!/usr/bin/env python3
"""A reviewed invocation facade over the canonical deployment checkout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

GUIDE = "docs/adp-platform-deployment/deploy-with-agent.md"
QUICKSTART = "docs/adp-platform-deployment/deploy-quickstart.md"
DEPLOY = "platform/scripts/deploy-all.sh"
TEARDOWN = "platform/scripts/undeploy.sh"
SCHEMA = "canonical-platform-invocation-v1"


def parser():
    root = common.Parser(prog="adp platform")
    commands = root.add_subparsers(dest="action", required=True)
    status = commands.add_parser("status")
    status.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    status.add_argument("--json", action="store_true")
    plan = commands.add_parser("plan", help="Prepare a reviewed canonical update invocation; Terraform plans remain owned by deploy-all")
    apply = commands.add_parser("apply")
    resume = commands.add_parser("resume")
    resume.add_argument("--state-file", required=True)
    teardown = commands.add_parser("teardown").add_subparsers(dest="teardown_action", required=True)
    teardown_plan = teardown.add_parser("plan")
    teardown_apply = teardown.add_parser("apply")
    for p in (plan, teardown_plan):
        p.add_argument("--source-checkout", required=True)
        p.add_argument("--source-revision", required=True)
        p.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
        p.add_argument("--profile", required=True)
        p.add_argument("--region", required=True)
        p.add_argument("--output", required=True)
        p.add_argument("--json", action="store_true")
    plan.add_argument("--scope", choices=["full", "gateway"], default="full")
    for p in (apply, resume, teardown_apply):
        p.add_argument("--plan-file", required=True)
        p.add_argument("--expect-plan-hash", required=True)
        p.add_argument("--confirm-account", required=True, help="The actual 12-digit STS account reviewed in the plan")
        p.add_argument("--json", action="store_true")
    return root


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise common.CliError("Canonical source or AWS identity check failed; no deployment was started.", "precondition_failed", 4)
    return result.stdout.strip()


def source(path, revision, *, env=None):
    root = Path(path).resolve()
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise common.CliError("Use a full immutable source commit SHA.", "usage_error", 1)
    if (
        command(["git", "rev-parse", "--show-toplevel"], cwd=root, env=env) != str(root)
        or command(["git", "rev-parse", "HEAD"], cwd=root, env=env) != revision
    ):
        raise common.CliError("Source checkout does not match the reviewed commit.", "source_changed", 4)
    # Canonical deployment owns its journal. Its presence is not evidence of applied resources.
    changed = command(["git", "diff", "HEAD", "--name-only"], cwd=root, env=env).splitlines()
    if set(changed) - {".adp-deploy-state.json", ".adp-undeploy-state.json"}:
        raise common.CliError("Use a clean pinned source checkout; deployment source was modified.", "source_changed", 4)
    untracked = command(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root, env=env).split("\0")
    allowed_journals = {".adp-deploy-state.json", ".adp-undeploy-state.json"}
    if any(name and name not in allowed_journals and not name.startswith(".adp-platform-invocations/") for name in untracked):
        raise common.CliError(
            "Pinned source contains untracked inputs. Use a clean checkout and store invocation plans outside it.", "source_changed", 4
        )
    for relative in ("config/deployment.yml", "modules/agent-context/config.local.env"):
        if (root / relative).exists() and not command(["git", "ls-files", "--", relative], cwd=root, env=env):
            raise common.CliError(
                "An untracked local deployment override is outside the pinned revision; use the canonical tooling directly for this configuration.",
                "source_changed",
                4,
            )
    hashes = {}
    for relative in (GUIDE, QUICKSTART, DEPLOY, TEARDOWN):
        target = root / relative
        if target.is_symlink() or not target.is_file():
            raise common.CliError("Canonical guide/tool is missing from this checkout.", "unsupported_operation", 4)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    return root, hashes


def environment(profile, region, env):
    if (
        not isinstance(profile, str)
        or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", profile)
        or not isinstance(region, str)
        or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region)
        or env not in {"dev", "staging", "prod"}
    ):
        raise common.CliError("Supply an explicit AWS profile, region and supported environment.", "usage_error", 1)
    # Credential files and ordinary tool discovery remain usable. Shell/Python
    # startup hooks, raw environment credentials, TF_VAR/TF_CLI_ARGS and ADP
    # config/source/release overrides cannot change the reviewed invocation.
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "USER",
        "LOGNAME",
        "SSH_AUTH_SOCK",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "AWS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_EC2_METADATA_DISABLED",
        "AWS_SDK_LOAD_CONFIG",
    }
    result = {key: value for key, value in os.environ.items() if key in allowed}
    for key in ("AGENT_CONTEXT_ENABLED", "SUPERPLANE_ENABLED"):
        value = os.environ.get(key, "false")
        if value not in {"true", "false"}:
            raise common.CliError("Scope toggles must be literal true or false.", "usage_error", 1)
        result[key] = value
    result.update(
        AWS_PROFILE=profile, AWS_DEFAULT_PROFILE=profile, AWS_REGION=region, AWS_DEFAULT_REGION=region, ADP_REGION=region, ADP_ENVIRONMENT=env
    )
    return result


def identity(env):
    value = json.loads(command(["aws", "sts", "get-caller-identity", "--output", "json"], env=env))
    if not re.fullmatch(r"[0-9]{12}", str(value.get("Account", ""))) or not isinstance(value.get("Arn"), str):
        raise common.CliError("AWS returned no verified account identity.", "invalid_response", 5)
    return {"account_id": value["Account"], "arn": value["Arn"]}


def journal_hash(root, name):
    path = root / name
    if path.is_symlink():
        raise common.CliError("Canonical state must not be a symlink.", "invalid_state", 4)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def read_journal(path):
    # Canonical scripts create ordinary readable JSON journals, not token stores.
    # Preserve their format/mode while refusing symlinks and writable foreign state.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise common.CliError("Canonical journal must be owned by you and not writable by others.", "invalid_state", 4)
        return json.load(stream)


def status(args):
    api = common.Api()
    capabilities = api.request("GET", "/me/cli-capabilities")
    if not isinstance(capabilities, dict) or not isinstance(capabilities.get("operations"), list):
        raise common.CliError("Malformed deployment capability readback.", "invalid_response", 5)
    rows = capabilities["operations"]
    components = {}
    for name, prefixes in {
        "gateway": ("capabilities.",),
        "factory": ("agents.", "activity."),
        "webhook": ("github.",),
        "models": ("models.",),
        "github_wiring": ("github.",),
    }.items():
        found = [row for row in rows if isinstance(row, dict) and any(str(row.get("id", "")).startswith(p) for p in prefixes)]
        components[name] = {"state": "capability_metadata_only" if found else "unknown", "operations": found}
    return common.envelope(
        "ok",
        "platform status",
        {
            "requested_environment": args.environment,
            "gateway": common.gateway_url(),
            "components": components,
            "artifact_verification": "unknown",
            "environment_verified": False,
            "full_deployment_verified": False,
        },
        "Capability metadata is not AWS resource or placeholder verification. Follow the canonical deployment verification guide.",
    )


def prepare(args):
    env = environment(args.profile, args.region, args.environment)
    root, hashes = source(args.source_checkout, args.source_revision, env=env)
    actor = identity(env)
    env["ADP_ACCOUNT_ID"] = actor["account_id"]
    teardown = args.action == "teardown"
    name = ".adp-undeploy-state.json" if teardown else ".adp-deploy-state.json"
    value = {
        "schema": SCHEMA,
        "action": "teardown" if teardown else "update",
        "source_checkout": str(root),
        "source_revision": args.source_revision,
        "source_files": hashes,
        "environment": args.environment,
        "region": args.region,
        "profile": args.profile,
        **actor,
        "scope": "full" if teardown else args.scope,
        "state_file": str(root / name),
        "state_hash": journal_hash(root, name),
        "expires_at": int(time.time()) + 3600,
        "scope_toggles": {key: env.get(key, "") for key in ("AGENT_CONTEXT_ENABLED", "SUPERPLANE_ENABLED")},
        "kind": "invocation_preview_not_terraform_plan",
        "state_is_live_evidence": False,
        "guide": GUIDE,
        "github_setup": "at_end",
        "placeholder_verification": "required_after_canonical_publish",
    }
    if teardown:
        # Existing read-only resource inventory, never the legacy --destroy entry point.
        report = command(["bash", str(root / TEARDOWN), "--dry-run", "--environment", args.environment], cwd=root, env=env)
        if len(report) > 100000:
            raise common.CliError("Destruction inventory exceeds the review bound; use canonical tooling directly.", "inventory_too_large", 4)
        value["destruction_inventory"] = report
        value["impact"] = "Canonical undeploy removes application data and resources; state backend and protected secrets survive by default."
    validate_plan(value)
    value["plan_hash"] = digest(value)
    destination = Path(args.output).absolute()
    if destination.is_relative_to(root):
        raise common.CliError("Store the reviewed invocation plan outside the clean source checkout.", "usage_error", 1)
    if destination.exists() or destination.is_symlink():
        raise common.CliError("Plan output already exists; choose a new explicit file.", "file_exists", 4)
    common.write_json(destination, value)
    return common.envelope(
        "dry_run",
        "platform plan",
        value,
        "Review account, source and scope. Apply requires this hash and the explicit account confirmation; "
        "canonical Terraform saved-plan gates remain in force.",
    )


def validate_plan(value):
    required = {
        "schema",
        "action",
        "source_checkout",
        "source_revision",
        "source_files",
        "environment",
        "region",
        "profile",
        "account_id",
        "arn",
        "scope",
        "state_file",
        "state_hash",
        "expires_at",
        "scope_toggles",
        "kind",
        "state_is_live_evidence",
        "guide",
        "github_setup",
        "placeholder_verification",
    }
    extra = {"destruction_inventory", "impact"} if value.get("action") == "teardown" else set()
    valid = (
        set(value) == required | extra
        and isinstance(value.get("action"), str)
        and value.get("action") in {"update", "teardown"}
        and value.get("scope") in {"full", "gateway"}
        and (value["action"] != "teardown" or value["scope"] == "full")
        and type(value.get("expires_at")) is int
        and isinstance(value.get("source_checkout"), str)
        and Path(value["source_checkout"]).is_absolute()
        and isinstance(value.get("state_file"), str)
        and isinstance(value.get("account_id"), str)
        and re.fullmatch(r"[0-9]{12}", value["account_id"])
        and isinstance(value.get("arn"), str)
        and re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):(sts|iam)::" + value["account_id"] + r":[^\s]+", value["arn"])
        and value.get("guide") == GUIDE
        and value.get("kind") == "invocation_preview_not_terraform_plan"
        and value.get("state_is_live_evidence") is False
        and value.get("github_setup") == "at_end"
        and value.get("placeholder_verification") == "required_after_canonical_publish"
        and isinstance(value.get("source_files"), dict)
        and set(value["source_files"]) == {GUIDE, QUICKSTART, DEPLOY, TEARDOWN}
        and all(isinstance(h, str) and re.fullmatch(r"[a-f0-9]{64}", h) for h in value["source_files"].values())
        and (value.get("state_hash") is None or isinstance(value["state_hash"], str) and re.fullmatch(r"[a-f0-9]{64}", value["state_hash"]))
        and isinstance(value.get("scope_toggles"), dict)
        and set(value["scope_toggles"]) == {"AGENT_CONTEXT_ENABLED", "SUPERPLANE_ENABLED"}
        and all(isinstance(item, str) and item in {"true", "false"} for item in value["scope_toggles"].values())
    )
    if not valid:
        raise common.CliError("Invocation plan fields do not match the reviewed schema.", "invalid_plan", 4)
    environment(value["profile"], value["region"], value["environment"])
    if extra and any(not isinstance(value.get(key), str) for key in extra):
        raise common.CliError("Teardown inventory is malformed.", "invalid_plan", 4)


def recheck(value, args):
    if value["expires_at"] < time.time():
        raise common.CliError("Invocation preview expired; prepare and review it again.", "plan_expired", 4)
    env = environment(value["profile"], value["region"], value["environment"])
    root, hashes = source(value["source_checkout"], value["source_revision"], env=env)
    if hashes != value["source_files"]:
        raise common.CliError("Canonical deployment tooling changed.", "source_changed", 4)
    actor = identity(env)
    if actor != {"account_id": value["account_id"], "arn": value["arn"]} or args.confirm_account != actor["account_id"]:
        raise common.CliError("AWS identity differs from the reviewed account/ARN confirmation.", "account_changed", 4)
    env["ADP_ACCOUNT_ID"] = actor["account_id"]
    if {key: env[key] for key in value["scope_toggles"]} != value["scope_toggles"]:
        raise common.CliError("Deployment scope toggles changed; prepare a new preview.", "scope_changed", 4)
    name = ".adp-undeploy-state.json" if value["action"] == "teardown" else ".adp-deploy-state.json"
    if value["state_file"] != str(root / name):
        raise common.CliError("Plan state is not the canonical checkout journal.", "invalid_state", 4)
    if args.action == "resume" and Path(args.state_file).resolve() != root / name:
        raise common.CliError("Resume must use the canonical state file in the pinned checkout.", "invalid_state", 4)
    if journal_hash(root, name) != value["state_hash"]:
        raise common.CliError("Canonical state changed; prepare a new preview for resume.", "state_changed", 4)
    if (root / name).exists() or args.action == "resume":
        journal = read_journal(root / name)
        if not isinstance(journal, dict) or journal.get("account_id") != actor["account_id"] or journal.get("environment") != value["environment"]:
            raise common.CliError("Resume journal belongs to another deployment.", "invalid_state", 4)
    return root, env, name


def apply(args):
    path = Path(args.plan_file)
    if path.stat().st_size > 150000:
        raise common.CliError("Plan file is too large.", "usage_error", 1)
    value = common.read_private_json(path)
    if not isinstance(value, dict):
        raise common.CliError("Invalid invocation plan.", "invalid_response", 5)
    recorded = value.pop("plan_hash", None)
    expected_action = "teardown" if args.action == "teardown" else "update"
    if value.get("schema") != SCHEMA or value.get("action") != expected_action or digest(value) != recorded or recorded != args.expect_plan_hash:
        raise common.CliError("Plan type or hash does not match the reviewed intent.", "plan_changed", 4)
    validate_plan(value)
    root, env, name = recheck(value, args)
    argv = ["bash", str(root / (TEARDOWN if expected_action == "teardown" else DEPLOY))]
    if expected_action == "teardown":
        if not sys.stdin.isatty():
            raise common.CliError("Canonical teardown requires its typed-account terminal confirmation.", "confirmation_required", 4)
        argv += ["--environment", value["environment"]]
    else:
        argv += ["--update", "--env", value["environment"], "--region", value["region"]]
        if value["scope"] == "gateway":
            argv.append("--gateway-only")
        elif value["scope"] != "full":
            raise common.CliError("Unknown reviewed update scope.", "usage_error", 1)
    with common.file_lock(root / ".adp-platform-invocations" / ".lock", "A canonical deployment invocation is already running."):
        # A caller may have waited while another invocation changed the journal
        # or checkout. Revalidate after acquiring the serialization boundary.
        root, env, name = recheck(value, args)
        if expected_action == "teardown":
            inventory = command(["bash", str(root / TEARDOWN), "--dry-run", "--environment", value["environment"]], cwd=root, env=env)
            if inventory != value["destruction_inventory"]:
                raise common.CliError("Destruction inventory changed; prepare and review a new teardown plan.", "inventory_changed", 4)
        receipt_path = root / ".adp-platform-invocations" / (recorded + ".json")
        if receipt_path.exists() or receipt_path.is_symlink():
            return common.envelope(
                "pending",
                "platform " + args.action,
                {"outcome": "previously_started", "plan_hash": recorded, "full_deployment_verified": False},
                "This reviewed invocation was already started and was not repeated. Inspect canonical evidence; resume requires a new reviewed plan.",
            )
        common.write_json(receipt_path, {"plan_hash": recorded, "outcome": "started", "state_file": str(root / name)})
        # Stream canonical output to stderr so --json retains one envelope on stdout.
        try:
            result = subprocess.run(argv, cwd=root, env=env, stdout=sys.stderr, stderr=sys.stderr)
            common.write_json(
                receipt_path, {"plan_hash": recorded, "outcome": "returned", "canonical_exit_code": result.returncode, "state_file": str(root / name)}
            )
        except (OSError, subprocess.SubprocessError):
            return common.envelope(
                "pending",
                "platform " + args.action,
                {"outcome": "unknown", "plan_hash": recorded, "full_deployment_verified": False},
                "Invocation receipt remains reserved. Inspect canonical state; this invocation was not replayed.",
            )
    return common.envelope(
        "pending" if result.returncode == 0 else "failed",
        "platform " + args.action,
        {"canonical_exit_code": result.returncode, "state_file": str(root / name), "full_deployment_verified": False},
        "Canonical tooling returned; retain its phase/verification evidence. "
        "This facade does not infer completed deployment or cleanup from an exit code.",
    )


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        result = (
            status(args)
            if args.action == "status"
            else prepare(args)
            if args.action == "plan" or (args.action == "teardown" and args.teardown_action == "plan")
            else apply(args)
        )
        return common.emit(result, args.json)
    except (common.CliError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return common.report_error(exc, "platform", "--json" in argv)
    except KeyboardInterrupt:
        common.emit(
            common.envelope(
                "pending", "platform", {"outcome": "unknown"}, "Canonical invocation interrupted; inspect state and actual resources before resuming."
            ),
            "--json" in argv,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
