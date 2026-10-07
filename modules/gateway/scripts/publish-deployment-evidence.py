"""Publish small deployment JSON to private S3, signed for this exact GitHub run."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Workflows stage this module beside this script from the workflow revision,
# independently of the application source selected for deployment.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deployment_evidence.store import MAX_PAYLOAD, audience, bucket_name, envelope, object_key


def publish(env, path, kind, name, *, aws, request_token):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_PAYLOAD:
        raise ValueError("bounded regular evidence file required")
    payload = path.read_bytes()
    doc = json.loads(payload)
    expected = {"repository_id": int(env["GITHUB_REPOSITORY_ID"]), "run_id": int(env["GITHUB_RUN_ID"]), "run_attempt": int(env["GITHUB_RUN_ATTEMPT"])}
    if any(doc.get(k) != v for k, v in expected.items()) or doc.get("account_id") != env["ACCOUNT_ID"]:
        raise ValueError("evidence does not belong to this deployment")
    if doc.get("workflow_revision") != env["ADP_WORKFLOW_REVISION"]:
        raise ValueError("evidence workflow revision mismatch")
    expected_name = doc["workflow_path"].rsplit("/", 1)[-1] if kind == "context" else doc.get("component")
    if expected_name != name:
        raise ValueError("evidence kind/name mismatch")
    if aws(["sts", "get-caller-identity"])["Account"] != env["ACCOUNT_ID"]:
        raise ValueError("evidence account differs from deployment identity")
    bucket = bucket_name(env["ACCOUNT_ID"], env["ENVIRONMENT"])
    key = object_key(expected["repository_id"], expected["run_id"], expected["run_attempt"], kind, name)
    raw = envelope(payload, request_token(audience(payload)))
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "evidence.json"
        target.write_bytes(raw)
        target.chmod(0o600)
        result = aws(
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--body",
                str(target),
                "--expected-bucket-owner",
                env["ACCOUNT_ID"],
                "--server-side-encryption",
                "AES256",
                "--content-type",
                "application/json",
                "--if-none-match",
                "*",
            ]
        )
    if not result.get("VersionId") or result["VersionId"] == "null":
        raise ValueError("deployment evidence bucket must have versioning enabled")
    return {"bucket": bucket, "key": key, "version_id": result["VersionId"], "sha256": audience(payload).rsplit(":", 1)[1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["context", "release"])
    parser.add_argument("name")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    def aws(parts):
        return json.loads(subprocess.check_output(["aws", *parts, "--region", os.environ["AWS_REGION"], "--output", "json"], timeout=30))

    def request_token(aud):
        url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".actions.githubusercontent.com")
            or parsed.username
            or parsed.password
        ):
            raise ValueError("untrusted GitHub OIDC request endpoint")
        request = Request(
            url + ("&" if "?" in url else "?") + urlencode({"audience": aud}),
            headers={"Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]},
        )

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.loads(response.read(32769))["value"]

    print(json.dumps(publish(os.environ, args.path, args.kind, args.name, aws=aws, request_token=request_token)))


if __name__ == "__main__":
    main()
