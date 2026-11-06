"""Build a deterministic fixture artifact; optionally publish an existing worker version.

Never creates a stack, grants permissions, invokes code, or deletes resources.
Publishing requires an explicit AWS profile, exact function ARN and switch.
"""

import argparse
import base64
import json
import re
import subprocess
from hashlib import sha256
from pathlib import Path

import boto3
from botocore.config import Config

from src.l5_devops_queue_observer import worker_zip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--publish-version", action="store_true")
    parser.add_argument("--aws-profile")
    parser.add_argument("--function-arn")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.commit):
        parser.error("--commit must be an immutable full Git SHA")
    source = subprocess.run(["git", "-C", args.repository, "show", args.commit + ":index.mjs"], check=True, capture_output=True).stdout
    artifact = worker_zip(source)
    output = Path(args.output)
    with output.open("xb") as handle:
        handle.write(artifact)
    digest = base64.b64encode(sha256(artifact).digest()).decode()
    result = {"source_commit": args.commit, "zip_sha256_base64": digest, "artifact": str(output)}
    if args.publish_version:
        if not args.aws_profile or not re.fullmatch(r"arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]{1,64}", args.function_arn or ""):
            parser.error("publishing requires --aws-profile and an exact unqualified --function-arn")
        region, account = args.function_arn.split(":")[3:5]
        session = boto3.Session(profile_name=args.aws_profile, region_name=region)
        config = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 0})
        if session.client("sts", config=config).get_caller_identity().get("Account") != account:
            raise ValueError("active profile differs from selected function account")
        client = session.client("lambda", config=config)
        function = client.get_function(FunctionName=args.function_arn)["Configuration"]
        if (
            function.get("CodeSha256") != digest
            or function.get("FunctionArn") != args.function_arn
            or function.get("State") != "Active"
            or function.get("LastUpdateStatus") != "Successful"
            or not function.get("RevisionId")
        ):
            raise ValueError("deploy the exact built ZIP to the isolated worker and finish its update before publishing")
        published = client.publish_version(FunctionName=args.function_arn, CodeSha256=digest, RevisionId=function["RevisionId"])
        if not re.fullmatch(r"[1-9][0-9]*", published.get("Version", "")) or published.get("CodeSha256") != digest:
            raise ValueError("published version could not be verified; inspect the exact function before retrying")
        result["function_version_arn"] = published["FunctionArn"]
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
