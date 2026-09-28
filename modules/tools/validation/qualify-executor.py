#!/usr/bin/env python3
"""Operator qualification of real EKS execution, not Task/provider admission."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import uuid

import boto3

from lib.codex_validation import ValidationCheck
from validation_tools.executor import service_executor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    cluster = boto3.client("eks", region_name="us-east-1").describe_cluster(name="adp-dev-eks-cluster")["cluster"]
    os.environ.update({
        "AWS_REGION": "us-east-1", "ADP_VALIDATION_CLUSTER_NAME": cluster["name"],
        "ADP_VALIDATION_CLUSTER_ENDPOINT": cluster["endpoint"],
        "ADP_VALIDATION_CLUSTER_CA": cluster["certificateAuthority"]["data"],
        "ADP_VALIDATION_NAMESPACE": "adp-codex-validation",
    })
    evidence = {"kind": "operator-executor-qualification", "taskAuthorityQualified": False, "image": args.image, "cases": [], "passed": False}
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*arguments):
                return subprocess.check_output([
                    "git", "-c", "user.name=Qualification", "-c", "user.email=qualification@localhost",
                    "-c", "core.hooksPath=/dev/null", *arguments,
                ], cwd=root, stderr=subprocess.DEVNULL).decode().strip()

            git("init", "-q")
            (root / "source.txt").write_text("verified-source\n")
            git("add", "source.txt")
            git("commit", "-qm", "Operator validation source")
            commit = git("rev-parse", "HEAD")
            tree = git("rev-parse", "HEAD^{tree}")
            cases = [
                ("source", "assert open('source.txt').read()=='verified-source\\n'; print('source verified')", 60, "passed", "completed"),
                ("failure", "raise SystemExit(3)", 60, "failed", "process_failed"),
                ("timeout", "import time; time.sleep(60)", 5, "failed", "timeout"),
                ("cancel", "import time; time.sleep(60)", 60, "failed", "cancelled"),
            ]
            for name, program, timeout, status, reason in cases:
                with service_executor("tsk_" + str(uuid.uuid4())) as executor:
                    observed = {}
                    request = executor.api.request

                    def record(method, path, **kwargs):
                        response = request(method, path, **kwargs)
                        if method == "GET" and "/pods/" in path and isinstance(response, dict):
                            for container in response.get("status", {}).get("containerStatuses", []):
                                terminated = container.get("state", {}).get("terminated")
                                if terminated:
                                    observed[response["metadata"]["uid"]] = terminated
                        return response

                    executor.api.request = record
                    cancelled = threading.Event()
                    timer = threading.Timer(4, cancelled.set) if name == "cancel" else None
                    if timer:
                        timer.start()
                    try:
                        result = executor.run_repository(
                            check=ValidationCheck(name, args.image, ("/var/lang/bin/python3.12", "-c", program), timeout_seconds=timeout),
                            repository=root, expected_head=commit, cancelled=cancelled,
                        )
                    finally:
                        if timer:
                            timer.cancel()
                    assert result["status"] == status and result["reason"] == reason, result
                    assert result["commit"] == commit and result["tree"] == tree, result
                    uid = result["executionIdentity"]["podUid"]
                    assert uid in observed, "container termination was not observed"
                    assert executor.recover()
                    inventories = {
                        kind: executor.api.request("GET", executor.base + "/" + kind, params={"labelSelector": "adp.dev/validation-task=" + executor.scope})["items"]
                        for kind in ["pods", "configmaps"]
                    }
                    assert all(not rows for rows in inventories.values()), "execution artifacts remain"
                    evidence["cases"].append({"name": name, "result": result, "observedTermination": observed[uid], "cleanupConfirmed": True})
                    print(json.dumps({"case": name, "status": status, "reason": reason, "cleanupConfirmed": True}), flush=True)
            evidence["passed"] = True
    except Exception as exc:
        evidence["error"] = str(exc)
    finally:
        Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps({"passed": evidence["passed"], "error": evidence.get("error")}), flush=True)
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
