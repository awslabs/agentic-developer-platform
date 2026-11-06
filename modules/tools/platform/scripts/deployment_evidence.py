"""Record the tools-platform deployment context and bind its release to this exact workflow run.

Both documents are published to the private S3 evidence store by
``modules/gateway/scripts/publish-deployment-evidence.py``, which refuses any
document whose run identity, account or workflow revision differ from the run
that publishes it. This script therefore derives every identity field from the
runner environment and the deploy script's own release output, never from
inputs a caller could choose.
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

WORKFLOW = ".github/workflows/tools-platform-deploy.yml"
GATEWAY_ARN = re.compile(r"^arn:aws:bedrock-agentcore:([a-z0-9-]{1,64}):([0-9]{12}):gateway/([A-Za-z0-9_-]{1,128})$")


def _identity(env):
    source, revision = env["CONTEXT_SOURCE"], env["CONTEXT_REVISION"]
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (source, revision)):
        raise ValueError("immutable source and definition required")
    correlation = env.get("CONTEXT_CORRELATION", "")
    if correlation and not re.fullmatch(r"[0-9a-f]{64}|orch:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", correlation):
        raise ValueError("invalid engine correlation")
    identity = {
        "repository_id": int(env["CONTEXT_REPOSITORY_ID"]),
        "run_id": int(env["GITHUB_RUN_ID"]),
        "run_attempt": int(env["GITHUB_RUN_ATTEMPT"]),
    }
    if any(value <= 0 for value in identity.values()):
        raise ValueError("positive workflow identity required")
    return source, revision, correlation, identity


def _release(env):
    path = Path(env["ADP_TOOLS_RELEASE_DIR"]) / "release.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("bounded release output required")
    release = json.loads(path.read_bytes())
    if release.get("component") != "tools-platform" or release.get("source_revision") != env["CONTEXT_SOURCE"]:
        raise ValueError("release output does not describe this deployment")
    if release.get("definition_revision") != env["CONTEXT_REVISION"]:
        raise ValueError("release output definition differs from this workflow revision")
    match = GATEWAY_ARN.fullmatch(str(release.get("gateway_arn", "")))
    if not match or match.group(1) != env["AWS_REGION"] or match.group(2) != env["ACCOUNT_ID"]:
        raise ValueError("release gateway identity differs from the deployment account and region")
    if release.get("gateway_id") != match.group(3):
        raise ValueError("release gateway id differs from its ARN")
    return path, release


def context(env, read):
    source, revision, correlation, identity = _identity(env)
    environment, mode = env["INPUT_ENVIRONMENT"], env["INPUT_MODE"]
    # The engine compares these inputs with every workflow_dispatch input's default
    # (adp_* excluded); an input the document omits makes the run unbindable.
    manual_source = env.get("INPUT_MANUAL_SOURCE_REVISION", "")
    if environment not in {"dev", "staging", "prod"} or mode not in {"release", "drift"}:
        raise ValueError("invalid effective deployment inputs")
    if manual_source and not re.fullmatch(r"[0-9a-f]{40}", manual_source):
        raise ValueError("invalid manual source revision input")
    account = read(["sts", "get-caller-identity"])["Account"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != env["ACCOUNT_ID"]:
        raise ValueError("deployment credential account differs from configuration")
    _, release = _release(env)
    return {
        "schema_version": 1,
        **identity,
        "workflow_path": WORKFLOW,
        "workflow_revision": revision,
        "source_revision": source,
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_kind": "agentcore-gateway",
        "resource_id": release["gateway_arn"],
        "inputs": {"environment": environment, "mode": mode, "manual_source_revision": manual_source},
        "correlation": correlation,
    }


def bind_release(env, read):
    """Add the run identity the engine's ReleaseArtifact contract requires, without inventing evidence."""
    source, revision, _, identity = _identity(env)
    account = read(["sts", "get-caller-identity"])["Account"]
    if account != env["ACCOUNT_ID"]:
        raise ValueError("deployment credential account differs from configuration")
    path, release = _release(env)
    targets = release.get("targets")
    if not isinstance(targets, dict) or any(not isinstance(value, str) for value in targets.values()):
        raise ValueError("release targets must map names to target ids")
    bound = {
        **release,
        "schema_version": 1,
        **identity,
        "workflow_path": WORKFLOW,
        "workflow_revision": revision,
        "account_id": account,
        "region": env["AWS_REGION"],
        "resource_id": release["gateway_arn"],
        "target_ids": sorted(targets.values()),
        "produced_at": datetime.now(UTC).isoformat(),
    }
    return path, bound


def main(argv):
    def read(args):
        import subprocess

        return json.loads(subprocess.check_output(["aws", *args, "--output", "json"], timeout=20))

    if argv[1:] == ["context"]:
        payload = json.dumps(context(os.environ, read), sort_keys=True).encode()
        if len(payload) > 65536:
            raise ValueError("workflow context too large")
        target = Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-context" / "deployment-context.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    elif argv[1:] == ["bind-release"]:
        path, bound = bind_release(os.environ, read)
        path.write_bytes(json.dumps(bound, sort_keys=True).encode())
    else:
        raise SystemExit("usage: deployment_evidence.py context|bind-release")


if __name__ == "__main__":
    main(sys.argv)
