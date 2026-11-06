import json
import os
import re
import subprocess
from pathlib import Path


def context(env, read):
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
    environment = env["INPUT_ENVIRONMENT"]
    rollback = env["INPUT_ROLLBACK_TO_VERSION"]
    if environment not in {"dev", "staging", "prod"} or (rollback and not re.fullmatch(r"[1-9][0-9]*", rollback)):
        raise ValueError("invalid effective deployment inputs")
    account = read(["sts", "get-caller-identity"])["Account"]
    if not re.fullmatch(r"[0-9]{12}", account) or account != env["ACCOUNT_ID"]:
        raise ValueError("deployment credential account differs from configuration")
    region, function_name = env["AWS_REGION"], env["TOOLS_INTERCEPTOR_FUNCTION_NAME"]
    if not re.fullmatch(r"[a-z0-9-]{1,64}", region) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", function_name):
        raise ValueError("invalid Lambda target configuration")
    function = read(["lambda", "get-function", "--function-name", function_name, "--region", region])["Configuration"]
    arn = function["FunctionArn"]
    parts = arn.split(":")
    if (
        function["FunctionName"] != function_name
        or parts[:2] not in [["arn", "aws"], ["arn", "aws-cn"], ["arn", "aws-us-gov"]]
        or parts[2:] != ["lambda", region, account, "function", function_name]
    ):
        raise ValueError("deployment Lambda identity differs from configuration")
    return {
        "schema_version": 1,
        **identity,
        "workflow_path": ".github/workflows/tools-interceptor-deploy.yml",
        "workflow_revision": revision,
        "source_revision": source,
        "account_id": account,
        "region": region,
        "resource_kind": "lambda-function",
        "resource_id": arn,
        "inputs": {"environment": environment, "rollback_to_version": rollback},
        "correlation": correlation,
    }


def main():
    def read(args):
        return json.loads(subprocess.check_output(["aws", *args, "--output", "json"], timeout=20))

    payload = json.dumps(context(os.environ, read), sort_keys=True).encode()
    if len(payload) > 65536:
        raise ValueError("workflow context too large")
    target = Path(os.environ["RUNNER_TEMP"]) / "adp-deployment-context" / "deployment-context.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


if __name__ == "__main__":
    main()
