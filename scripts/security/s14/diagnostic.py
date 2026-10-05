"""Bounded SAME-IRSA acceptance. Prepared only; each live stage requires --execute.
No role assumptions, deployments, autonomous agents, secret output or self-boundary mutations.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import time
import urllib.request
import urllib.error
import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError
from journal import Journal, ReconciliationRequired
from recovery_contract import ORIGIN_RUN, ORIGIN_WORKFLOW_SHA, validate_continuation

APPROVED_CONFIG_SHA256 = (
    "c0c5980303b49d0256694e439ac4e1710dc7430388e8a2c3add73a7d2b83899f"
)


def verified_ecr_manifest(batch, account, repository, digest):
    """Accept duplicate tag records only when every requested manifest is exact."""
    if not (batch.get("images") and not batch.get("failures")):
        raise AssertionError("ECR query failed")
    manifests = set()
    for image in batch["images"]:
        if not (image.get("registryId") == account):
            raise AssertionError("Unexpected ECR registry")
        if not (image.get("repositoryName") == repository):
            raise AssertionError("Unexpected ECR repository")
        if not (image.get("imageId", {}).get("imageDigest") == digest):
            raise AssertionError("Unexpected ECR digest")
        raw = image["imageManifest"]
        if not ("sha256:" + hashlib.sha256(raw.encode()).hexdigest() == digest):
            raise AssertionError("ECR manifest hash mismatch")
        manifests.add(raw)
    if not (len(manifests) == 1):
        raise AssertionError("Conflicting ECR manifests")
    return json.loads(next(iter(manifests)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--receipt", required=True)
    ap.add_argument(
        "--stage", choices=["runtime", "start-build", "poll-build"], required=True
    )
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--source-root")
    ap.add_argument("--source-sha")
    args = ap.parse_args()
    config_bytes = pathlib.Path(args.config).read_bytes()
    if not (hashlib.sha256(config_bytes).hexdigest() == APPROVED_CONFIG_SHA256):
        raise AssertionError("Unreviewed configuration")
    cfg = json.loads(config_bytes)
    receipt = pathlib.Path(args.receipt)
    if not (
        cfg["account"] == "879318057152"
        and cfg["role_name"] == "adp-dev-agent-runner-role"
    ):
        raise AssertionError()
    if not args.execute:
        print(json.dumps({"validated": True, "executed": False, "stage": args.stage}))
        return
    receipt.parent.mkdir(exist_ok=True, parents=True, mode=0o700)
    lockfd = os.open(str(receipt) + ".lock", os.O_CREAT | os.O_WRONLY, 384)
    fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2})
    session = boto3.Session(region_name=cfg["region"])
    if not (session.get_credentials().method == "assume-role-with-web-identity"):
        raise AssertionError("Actual IRSA credential provider required")

    def client(service):
        return session.client(service, config=config)

    identity = client("sts").get_caller_identity()
    if not (
        identity["Account"] == cfg["account"]
        and identity["Arn"].startswith(
            "arn:aws:sts::" + cfg["account"] + ":assumed-role/" + cfg["role_name"] + "/"
        )
    ):
        raise AssertionError("WRONG_RUNTIME_IDENTITY")
    workflow_run_id = os.environ["GITHUB_RUN_ID"]
    workflow_sha = os.environ["GITHUB_SHA"]
    if receipt.exists():
        checkpoint = json.loads(receipt.read_text())
        if "continuation" in checkpoint:
            if not (os.environ["GITHUB_REF"] == "refs/heads/main"):
                raise AssertionError()
            if not (os.environ["GITHUB_RUN_ATTEMPT"] == "1"):
                raise AssertionError()
            validate_continuation(checkpoint, workflow_run_id, workflow_sha)
            # Original evidence remains top-level; the new context is appended.
            workflow_run_id, workflow_sha = ORIGIN_RUN, ORIGIN_WORKFLOW_SHA
    journal = Journal(
        receipt,
        session.client(
            "s3",
            config=Config(
                connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
            ),
        ),
        cfg["source_bucket"],
        cfg["receipt_key"],
        {
            "checks": {},
            "config_sha256": APPROVED_CONFIG_SHA256,
            "role_arn": cfg["role_arn"],
            "workflow_run_id": workflow_run_id,
            "workflow_sha": workflow_sha,
        },
    )
    result = journal.result
    save = journal.save
    if "continuation" in result:
        if identity["Arn"] not in result["continuation"]["caller_arns"]:
            result["continuation"]["caller_arns"].append(identity["Arn"])
    else:
        result["caller_arn"] = identity["Arn"]
    save()
    current = "identity"
    try:
        if args.stage == "runtime":
            if result.get("runtime_complete"):
                print(json.dumps({"runtime_complete": True, "already_complete": True}))
                return
            if not result["checks"].get("ssm"):
                current = "ssm"
                url = client("ssm").get_parameter(
                    Name=cfg["ssm_parameter"], WithDecryption=False
                )["Parameter"]["Value"]
                if not (cfg["gateway_url"].startswith(url.rstrip("/") + "/internal/")):
                    raise AssertionError()
                result["checks"]["ssm"] = True
                save()
            if not result["checks"].get("secret_kms"):
                current = "secret_kms"
                secret_client = client("secretsmanager")
                for arn in cfg["secrets"]:
                    value = secret_client.get_secret_value(SecretId=arn)
                    if not (value.get("SecretString") or value.get("SecretBinary")):
                        raise AssertionError()
                    del value
                result["checks"]["secret_kms"] = {
                    "passed": True,
                    "count": len(cfg["secrets"]),
                }
                save()
            if not result["checks"].get("negative_resources"):
                current = "negative_resources"
                negative = result.setdefault("negative_resource_results", {})
                for name, probe in cfg["negative_resources"].items():
                    if negative.get(name, {}).get("passed"):
                        continue
                    try:
                        response = getattr(
                            client(probe["service"]), probe["operation"]
                        )(**probe["arguments"])
                        if hasattr(response.get("Body"), "close"):
                            response["Body"].close()
                        negative[name] = {
                            "passed": False,
                            "actual": "unexpected_success",
                        }
                        save()
                        raise RuntimeError("Unexpected foreign-resource access")
                    except ClientError as exc:
                        code = exc.response.get("Error", {}).get("Code")
                        negative[name] = {
                            "passed": code in probe["expected_error_codes"],
                            "actual": code,
                        }
                        save()
                        if not negative[name]["passed"]:
                            raise RuntimeError("Not an authorization refusal") from None
                result["checks"]["negative_resources"] = all(
                    (x["passed"] for x in negative.values())
                )
                save()
            if not result["checks"].get("ecr_pull"):
                current = "ecr"
                ecr = client("ecr")
                authorization = ecr.get_authorization_token()["authorizationData"][0]
                if not (
                    base64.b64decode(authorization["authorizationToken"]).startswith(
                        b"AWS:"
                    )
                ):
                    raise AssertionError()
                del authorization
                digest = cfg["ecr_image_digest"]
                image = None
                for _ in range(3):
                    batch = ecr.batch_get_image(
                        repositoryName=cfg["ecr_repository"],
                        imageIds=[{"imageDigest": digest}],
                    )
                    image = verified_ecr_manifest(
                        batch, cfg["account"], cfg["ecr_repository"], digest
                    )
                    if image.get("layers"):
                        break
                    candidates = [
                        x
                        for x in image.get("manifests", [])
                        if x.get("platform", {}).get("architecture") == "amd64"
                        and x.get("platform", {}).get("os") == "linux"
                    ]
                    if not (len(candidates) == 1):
                        raise AssertionError()
                    digest = candidates[0]["digest"]
                layer = image["layers"][0]["digest"]
                download = ecr.get_download_url_for_layer(
                    repositoryName=cfg["ecr_repository"], layerDigest=layer
                )["downloadUrl"]
                with urllib.request.urlopen(
                    urllib.request.Request(download, headers={"Range": "bytes=0-1023"}),
                    timeout=20,
                ) as response:
                    if not (response.read(1024)):
                        raise AssertionError()
                del download
                result["checks"]["ecr_pull"] = True
                save()
            if not result["checks"].get("own_log"):
                current = "own_log"
                stream = result.get(
                    "log_stream"
                ) or "security-s14-" + secrets.token_hex(8)
                result["log_stream"] = stream
                save()
                logs = session.client(
                    "logs",
                    config=Config(
                        connect_timeout=10,
                        read_timeout=30,
                        retries={"total_max_attempts": 1},
                    ),
                )
                try:
                    logs.create_log_stream(
                        logGroupName=cfg["log_group"], logStreamName=stream
                    )
                except logs.exceptions.ResourceAlreadyExistsException:
                    pass
                if not (not result.get("log_event_started")):
                    raise AssertionError(
                        "Prior log write ambiguous; reconcile before retry"
                    )
                result["log_event_started"] = True
                save()
                logs.put_log_events(
                    logGroupName=cfg["log_group"],
                    logStreamName=stream,
                    logEvents=[
                        {
                            "timestamp": int(time.time() * 1000),
                            "message": "S14 bounded shared-runner capability acceptance",
                        }
                    ],
                )
                result["log_stream"] = stream
                result["checks"]["own_log"] = True
                save()
            if not result["checks"].get("gateway_route"):
                current = "gateway"
                request = AWSRequest(method="GET", url=cfg["gateway_url"])
                SigV4Auth(
                    session.get_credentials().get_frozen_credentials(),
                    "execute-api",
                    cfg["region"],
                ).add_auth(request)
                with urllib.request.urlopen(
                    urllib.request.Request(
                        cfg["gateway_url"], headers=dict(request.headers)
                    ),
                    timeout=30,
                ) as response:
                    if not (response.status == cfg["gateway_expected_status"]):
                        raise AssertionError()
                    body = json.loads(response.read(4096))
                    if not (body.get("tenant") == cfg["gateway_tenant"]):
                        raise AssertionError()
                    if not (
                        all(
                            (
                                isinstance(body.get(k), bool)
                                for k in [
                                    "enable_user_credentials",
                                    "enforce_credential_binding",
                                ]
                            )
                        )
                    ):
                        raise AssertionError()
                result["checks"]["gateway_route"] = True
                save()
            if not result["checks"].get("bedrock"):
                current = "bedrock"
                if not (not result.get("model_invocation_started")):
                    raise AssertionError(
                        "Prior model response ambiguous; reconcile before retry"
                    )
                result["model_invocation_started"] = True
                save()
                body = {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                }
                response = session.client(
                    "bedrock-runtime",
                    config=Config(
                        connect_timeout=10,
                        read_timeout=30,
                        retries={"total_max_attempts": 1},
                    ),
                ).invoke_model(
                    modelId=cfg["model_id"],
                    contentType="application/json",
                    accept="application/json",
                    body=json.dumps(body),
                )
                answer = json.loads(response["body"].read())
                if not (answer.get("type") == "message"):
                    raise AssertionError()
                result["checks"]["bedrock"] = True
                save()
            result["runtime_complete"] = all(
                (
                    result["checks"].get(k)
                    for k in [
                        "ssm",
                        "secret_kms",
                        "negative_resources",
                        "ecr_pull",
                        "own_log",
                        "gateway_route",
                        "bedrock",
                    ]
                )
            )
            save()
        elif args.stage == "start-build":
            if not (result.get("runtime_complete")):
                raise AssertionError("Runtime acceptance must finish first")
            if not (not result.get("build_invocation_started")):
                raise AssertionError(
                    "Prior StartBuild intent requires operator reconciliation; no automatic replay"
                )
            if not (not result.get("build_id")):
                raise AssertionError("Existing build handle: use poll-build")
            if not result.get("build_request"):
                if not (
                    args.source_root
                    and args.source_sha == cfg["reviewed_source_sha"]
                    and re.fullmatch("[a-f0-9]{40}", args.source_sha)
                ):
                    raise AssertionError()
                root = pathlib.Path(args.source_root).resolve()
                actual = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=root, text=True
                ).strip()
                if not (actual == args.source_sha):
                    raise AssertionError("Source SHA mismatch")
                if not (
                    not subprocess.check_output(
                        ["git", "status", "--porcelain", "--untracked-files=no"],
                        cwd=root,
                        text=True,
                    ).strip()
                ):
                    raise AssertionError("Tracked source dirty")
                current = "package_source"
                if not result.get("source_upload"):
                    archive = receipt.parent / (
                        "gateway-pr-source-" + secrets.token_hex(8) + ".zip"
                    )
                    subprocess.run(
                        [
                            "git",
                            "archive",
                            "--format=zip",
                            "--output=" + str(archive),
                            args.source_sha,
                        ],
                        cwd=root,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    source_key = (
                        cfg["source_prefix"]
                        + args.source_sha
                        + "-s14-"
                        + secrets.token_hex(8)
                        + ".zip"
                    )
                    result["source_upload"] = {
                        "key": source_key,
                        "archive_path": str(archive),
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "source_sha": actual,
                        "uploaded": False,
                    }
                    save()
                upload = result["source_upload"]
                if not (upload["source_sha"] == actual):
                    raise AssertionError()
                archive = pathlib.Path(upload["archive_path"])
                if not (
                    hashlib.sha256(archive.read_bytes()).hexdigest() == upload["sha256"]
                ):
                    raise AssertionError()
                source_key = upload["key"]
                if not upload["uploaded"]:
                    uploaded = client("s3").put_object(
                        Bucket=cfg["source_bucket"],
                        Key=source_key,
                        Body=archive.read_bytes(),
                        Metadata={
                            "source-sha": actual,
                            "archive-sha256": upload["sha256"],
                        },
                    )
                    if not (uploaded.get("VersionId") not in (None, "", "null")):
                        raise AssertionError(
                            "Source bucket must provide an immutable object version"
                        )
                    upload["version_id"] = uploaded["VersionId"]
                    upload["uploaded"] = True
                    save()
                result["source_key"] = source_key
                result["source_sha"] = actual
                result["source_archive_sha256"] = upload["sha256"]
                result["build_request"] = {
                    "projectName": cfg["codebuild_project"],
                    "serviceRoleOverride": cfg["codebuild_pr_role"],
                    "sourceLocationOverride": cfg["source_bucket"] + "/" + source_key,
                    "buildspecOverride": cfg["buildspec"],
                    "idempotencyToken": secrets.token_hex(24),
                    "sourceVersion": upload["version_id"],
                    "environmentVariablesOverride": [
                        {
                            "name": "S14_REVIEWED_SOURCE_SHA",
                            "value": actual,
                            "type": "PLAINTEXT",
                        },
                        {
                            "name": "PUBLISH_LATEST",
                            "value": "false",
                            "type": "PLAINTEXT",
                        },
                    ],
                }
                result["build_request_saved_at"] = time.time()
                save()
            if not (time.time() - result["build_request_saved_at"] < 240):
                raise AssertionError(
                    "StartBuild response ambiguity exceeded conservative idempotency window; supervisor reconciliation required, no retry"
                )
            current = "start_build"
            result["build_invocation_started"] = True
            save()
            response = session.client(
                "codebuild",
                config=Config(
                    connect_timeout=10,
                    read_timeout=30,
                    retries={"total_max_attempts": 1},
                ),
            ).start_build(**result["build_request"])
            result["build_id"] = response["build"]["id"]
            result["build_status"] = response["build"]["buildStatus"]
            save()
        else:
            if not (result.get("build_id")):
                raise AssertionError("No existing build handle")
            current = "poll_build"
            result["checks"]["codebuild_pr"] = False
            build = client("codebuild").batch_get_builds(ids=[result["build_id"]])[
                "builds"
            ][0]
            result["build_status"] = build["buildStatus"]
            request = result["build_request"]
            if not (build["id"] == result["build_id"]):
                raise AssertionError()
            if not (
                build["serviceRole"]
                == request["serviceRoleOverride"]
                == cfg["codebuild_pr_role"]
            ):
                raise AssertionError()
            if not (
                build["projectName"]
                == request["projectName"]
                == cfg["codebuild_project"]
            ):
                raise AssertionError()
            if not (build["source"]["type"] == "S3"):
                raise AssertionError()
            if not (build["source"]["location"] == request["sourceLocationOverride"]):
                raise AssertionError()
            if not (
                build["source"]["buildspec"]
                == request["buildspecOverride"]
                == cfg["buildspec"]
            ):
                raise AssertionError()
            if not (
                build["sourceVersion"]
                == request["sourceVersion"]
                == result["source_upload"]["version_id"]
            ):
                raise AssertionError()
            environment = {
                v["name"]: v for v in build["environment"]["environmentVariables"]
            }
            for expected in request["environmentVariablesOverride"]:
                if not (environment.get(expected["name"]) == expected):
                    raise AssertionError()
            if not (result["source_sha"] == cfg["reviewed_source_sha"]):
                raise AssertionError()
            result["build_contract_verified"] = True
            result["checks"]["codebuild_pr"] = build["buildStatus"] == "SUCCEEDED"
            if build["buildStatus"] in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT"):
                raise RuntimeError("Build did not succeed")
            save()
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "checks": result["checks"],
                    "build_status": result.get("build_status"),
                    "runtime_complete": result.get("runtime_complete", False),
                }
            )
        )
    except Exception as exc:
        failure = {"stage": current, "exception": type(exc).__name__}
        if isinstance(exc, urllib.error.HTTPError):
            failure.update(
                {
                    "http_status": exc.code,
                    "route_refused": current == "gateway" and exc.code in [401, 403],
                }
            )
        if "continuation" in result:
            result["continuation"]["failure"] = failure
        else:
            result["failure"] = failure
        try:
            save()
        except ReconciliationRequired:
            pass  # Never replay an uncertain journal write from the error handler.
        print(
            json.dumps(
                {"passed": False, "stage": current, "exception": type(exc).__name__}
            )
        )
        sys.exit(1)
    finally:
        summary = receipt.with_name("summary.json")
        summary.write_text(json.dumps(journal.summary(), indent=2) + "\n")
        os.close(lockfd)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "passed": False,
                    "exception": type(exc).__name__,
                    "reconciliation_required": True,
                }
            )
        )
        sys.exit(1)
