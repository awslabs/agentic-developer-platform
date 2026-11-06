"""Publish small deployment JSON to private S3, signed for this exact GitHub run."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Workflows stage this module beside this script from the workflow revision,
# independently of the application source selected for deployment.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deployment_evidence.store import MAX_PAYLOAD, MAX_SCAN_ARCHIVE, REPOSITORY_ARTIFACT_WORKFLOWS, audience, bucket_name, envelope, object_key


def publish_archive(env, path, name, *, aws, request_token, kind="repository-artifact"):
    """Keep the original archive layout, signing a bounded immutable descriptor."""
    if path.is_symlink() or not path.exists() or any(p.is_symlink() for p in path.parents):
        raise ValueError("regular archive source required")
    files = sorted(path.rglob("*")) if path.is_dir() else [path]
    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in files):
        raise ValueError("archive links and special files refused")
    files = [p for p in files if p.is_file()]
    if (
        not 1 <= len(files) <= 256
        or any(not 0 < p.stat().st_size <= 8 * 1024 * 1024 for p in files)
        or sum(p.stat().st_size for p in files) > 32 * 1024 * 1024
    ):
        raise ValueError("archive contents exceed bounds")
    if kind == "workflow-artifact":
        prefix = env["GITHUB_REPOSITORY"] + "/"
        reference = env["GITHUB_WORKFLOW_REF"]
        workflow = reference.removeprefix(prefix).removesuffix("@refs/heads/main")
        if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}\.ya?ml", workflow):
            raise ValueError("workflow evidence requires its maintained main workflow")
    elif kind == "repository-artifact":
        workflow = REPOSITORY_ARTIFACT_WORKFLOWS.get(name)
    else:
        raise ValueError("unsupported archive kind")
    if workflow is None or env["GITHUB_WORKFLOW_REF"] != env["GITHUB_REPOSITORY"] + "/" + workflow + "@refs/heads/main":
        raise ValueError("repository evidence requires its maintained main workflow")
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    source = env["EXPECTED_SOURCE_REVISION"]
    if kind == "workflow-artifact":
        if not re.fullmatch(r"[0-9a-f]{40}", source) or checkout != source:
            raise ValueError("workflow evidence checkout mismatch")
    elif name == "identity-proof":
        if (
            re.fullmatch(r"[0-9a-f]{40}", source) is None
            or checkout != env["ADP_WORKFLOW_REVISION"]
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("PROOF_PHASE") != "stage.identity-proof"
            or env.get("ENVIRONMENT") != "dev"
            or env.get("GATEWAY_REVISION") != source
            or not path.is_file()
            or path.name != "atlassian-stage-summary.json"
        ):
            raise ValueError("Identity proof publication context mismatch")
        subprocess.check_call(["git", "merge-base", "--is-ancestor", source, checkout])
    elif name in {"atlassian-foundation", "atlassian-identity"}:
        if (
            re.fullmatch(r"[0-9a-f]{40}", source) is None
            or checkout != source
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or env.get("GITHUB_RUN_ATTEMPT") != "1"
            or env.get("ENVIRONMENT") != "dev"
            or env.get("ADP_FOUNDATION_EVIDENCE_STORE") != "s3-oidc-v1"
            or not path.is_file()
            or path.name != "atlassian-stage-summary.json"
        ):
            raise ValueError("Stage publication context mismatch")
        subprocess.check_call(["git", "merge-base", "--is-ancestor", source, env["ADP_WORKFLOW_REVISION"]])
        report = json.loads(path.read_text())
        if (
            report.get("stage_id") != ("identity" if name == "atlassian-identity" else "foundation")
            or report.get("source_revision") != source
            or report.get("workflow_revision") != env["ADP_WORKFLOW_REVISION"]
            or report.get("run_id") != env["GITHUB_RUN_ID"]
            or report.get("run_attempt") != "1"
            or (name == "atlassian-identity" and (
                report.get("correlation") != env.get("ADP_CORRELATION")
                or report.get("criteria") != [
                    {"criterion_id": f"8448.AC-{index:02}", "phase_status": "pass", "evidence_verified": True}
                    for index in range(1, 5)
                ]
                or report.get("cleanup_status") != "confirmed"
                or report.get("runtime_verified") is not True
                or report.get("independent_verification") != "confirmed"
                or report.get("unresolved_operations") != []
                or json.loads(env.get("ADP_TOOLS_ADAPTER_WORKFLOW_INPUTS_JSON", "{}" )).get("mode") != "identity"
            ))
        ):
            raise ValueError("Stage summary provenance mismatch")
    elif checkout != source:
        raise ValueError("repository evidence checkout mismatch")
    bucket = bucket_name(env["ACCOUNT_ID"], env["ENVIRONMENT"])
    base = object_key(int(env["GITHUB_REPOSITORY_ID"]), int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"]), kind, name)
    with tempfile.TemporaryDirectory() as temp:
        packed = Path(temp) / "archive.zip"
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, file.relative_to(path).as_posix() if path.is_dir() else file.name)
        if not 0 < packed.stat().st_size <= MAX_SCAN_ARCHIVE:
            raise ValueError("archive too large")
        digest = hashlib.sha256(packed.read_bytes()).hexdigest()
        key = base.removesuffix(".json") + "/" + digest + ".zip"
        if aws(["sts", "get-caller-identity"])["Account"] != env["ACCOUNT_ID"]:
            raise ValueError("archive account mismatch")
        result = aws(
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                key,
                "--body",
                str(packed),
                "--expected-bucket-owner",
                env["ACCOUNT_ID"],
                "--server-side-encryption",
                "AES256",
                "--if-none-match",
                "*",
            ]
        )
        if not result.get("VersionId") or result["VersionId"] == "null":
            raise ValueError("archive must be versioned")
        descriptor = dict(
            schema_version=1,
            repository_id=int(env["GITHUB_REPOSITORY_ID"]),
            run_id=int(env["GITHUB_RUN_ID"]),
            run_attempt=int(env["GITHUB_RUN_ATTEMPT"]),
            account_id=env["ACCOUNT_ID"],
            workflow_path=workflow,
            workflow_revision=env["ADP_WORKFLOW_REVISION"],
            source_revision=source,
            component=name,
            bucket=bucket,
            key=key,
            version_id=result["VersionId"],
            sha256=digest,
            size=packed.stat().st_size,
        )
        target = Path(temp) / "descriptor.json"
        target.write_text(json.dumps(descriptor))
        return publish(env, target, kind, name, aws=aws, request_token=request_token)


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
    if kind == "delivery":
        from src.orchestration.delivery_contract import WorkflowDeliveryEvidence

        WorkflowDeliveryEvidence.model_validate(doc)
    elif expected_name != name:
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
    parser.add_argument("kind", choices=["context", "release", "delivery", "repository-artifact", "workflow-artifact"])
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

    if args.kind in {"repository-artifact", "workflow-artifact"}:
        receipt = publish_archive(os.environ, args.path, args.name, aws=aws, request_token=request_token, kind=args.kind)
    else:
        receipt = publish(os.environ, args.path, args.kind, args.name, aws=aws, request_token=request_token)
    print(json.dumps(receipt))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(
                f"\nPrivate signed evidence: `s3://{receipt['bucket']}/{receipt['key']}`\n\n"
                f"Version: `{receipt['version_id']}` · SHA-256: `{receipt['sha256']}`\n"
            )


if __name__ == "__main__":
    main()
