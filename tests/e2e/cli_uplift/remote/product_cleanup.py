"""Retire an owned Product from its private on-instance manifest."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import common


def execute(config, evidence):
    work_dir = Path(config.get("work_dir", ""))
    path = work_dir / "product-ownership.json"
    common.require(
        work_dir.is_absolute()
        and work_dir.is_dir()
        and work_dir.stat().st_uid == os.getuid()
        and stat.S_IMODE(work_dir.stat().st_mode) == 0o700,
        "Product work directory is not a private evaluator directory",
    )
    metadata = path.lstat()
    common.require(
        stat.S_ISREG(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.getuid(),
        "Product ownership manifest is not a private evaluator file",
    )
    manifest = json.loads(path.read_text())
    common.require(
        isinstance(manifest, dict)
        and manifest.get("run_id") == config.get("product_run_id")
        and manifest.get("run_id") == config.get("workflow_run_id")
        and isinstance(manifest.get("run_id"), str)
        and re.fullmatch(
            r"product-[1-9][0-9]{0,19}-[1-9][0-9]{0,5}", manifest["run_id"]
        )
        and isinstance(config.get("evaluation_id"), str)
        and re.fullmatch(
            r"adp-e2e-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}", config["evaluation_id"]
        )
        and all(
            isinstance(manifest.get(key), str) and manifest[key]
            for key in ("tenant_id", "actor_id", "source_revision", "idempotency_key")
        ),
        "Product ownership manifest is not bound to the evaluation",
    )
    recovery_intent = config.get("product_recovery_intent")
    if recovery_intent is not None:
        common.require(
            isinstance(recovery_intent, dict)
            and set(recovery_intent)
            == {
                "run_id",
                "tenant_id",
                "actor_id",
                "source_revision",
                "idempotency_key",
            }
            and all(
                manifest.get(key) == value for key, value in recovery_intent.items()
            )
            and manifest["source_revision"] == config.get("expected_revision"),
            "Product on-instance ownership differs from its durable recovery intent",
        )
    region = config["region"]
    origin = config.get("internal_invoke_url", "")
    common.require(
        re.fullmatch(
            rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+",
            origin,
        ),
        "Protected Product cleanup needs a reviewed regional internal Gateway invoke URL",
    )
    env = common.clean_env(config)
    code, output, _ = common.bounded(
        ["aws", "configure", "export-credentials", "--format", "process"],
        env=env,
        timeout=20,
    )
    common.require(
        code == 0, "Instance credentials are unavailable for protected Product cleanup"
    )
    credentials = json.loads(output)
    common.require(
        all(
            isinstance(credentials.get(key), str)
            and re.fullmatch(r"[A-Za-z0-9/+=_-]+", credentials[key])
            for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")
        ),
        "Protected Product cleanup requires temporary instance credentials",
    )
    sigv4 = (
        f'aws-sigv4 = "aws:amz:{region}:execute-api"\n'
        f'user = "{credentials["AccessKeyId"]}:{credentials["SecretAccessKey"]}"\n'
        f'header = "X-Amz-Security-Token: {credentials["SessionToken"]}"\n'
    )
    code, output, _ = common.bounded(
        [
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--config",
            "-",
            "--request",
            "POST",
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            json.dumps(
                {
                    key: manifest[key]
                    for key in (
                        "run_id",
                        "tenant_id",
                        "actor_id",
                        "source_revision",
                        "idempotency_key",
                        "product_id",
                        "project_id",
                    )
                    if key in manifest
                }
            ),
            f"{origin}/internal/v1/product-evaluation/retire",
        ],
        env=env,
        timeout=45,
        stdin=sigv4,
    )
    common.require(code == 0, "Protected Product cleanup was denied or unavailable")
    receipt = json.loads(output)
    common.require(
        isinstance(receipt, dict)
        and receipt.get("fixture_removed") is True
        and receipt.get("run_id") == manifest["run_id"]
        and all(
            isinstance(receipt.get(key), str) and receipt[key]
            for key in ("product_id", "project_id")
        )
        and type(receipt.get("cleanup_revision")) is int
        and receipt["cleanup_revision"] >= 1
        and all(
            receipt.get(key) == manifest[key]
            for key in ("product_id", "project_id")
            if manifest.get(key)
        ),
        "Protected Product cleanup did not verify the owned fixture",
    )
    evidence.update(
        success=True,
        stage="complete",
        detail={
            "run_id": receipt["run_id"],
            "product_id": receipt["product_id"],
            "project_id": receipt["project_id"],
            "cleanup_revision": receipt["cleanup_revision"],
            "fixture_removed": True,
        },
    )


if __name__ == "__main__":
    raise SystemExit(common.run_script(execute))
