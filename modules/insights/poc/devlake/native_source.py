"""Independently read the native flow and checkpoint without forging acceptance predicates.

The usage-read routes intentionally withhold execution authority bindings. This
measurement is not the privileged native attestation required by the pilot.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

import resume_campaign
import source_reader
from provider_profile_source import NoRedirect, private_json
from verify_campaign import _pilot_pins


DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_RESPONSE = 1024 * 1024


def gateway_read(url, authorization, allowed_hosts):
    parsed = urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or
            parsed.hostname not in allowed_hosts or parsed.port not in (None, 443) or not parsed.path or
            not isinstance(authorization, str) or not authorization.startswith("Bearer ") or
            not authorization.split(" ", 1)[1]):
        raise ValueError("native read requires an approved HTTPS endpoint and bearer access")
    try:
        with build_opener(NoRedirect()).open(
                Request(url, headers={"Authorization": authorization, "Accept": "application/json"}),
                timeout=30) as response:
            if response.status != 200:
                raise ValueError("native gateway read did not succeed")
            raw = response.read(MAX_RESPONSE + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ValueError("native gateway read is unavailable") from error
    if len(raw) > MAX_RESPONSE:
        raise ValueError("native gateway read is too large")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("native gateway response is invalid") from error
    if not isinstance(document, (list, dict)):
        raise ValueError("native gateway response is invalid")
    return document, hashlib.sha256(raw).hexdigest()


def github_read(path, request=source_reader._github):
    raw = request(path)
    if not isinstance(raw, bytes) or len(raw) > source_reader.MAX_ARCHIVE_BYTES:
        raise ValueError("native workflow source is unavailable")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("native workflow source is invalid") from error
    if not isinstance(document, dict):
        raise ValueError("native workflow source is invalid")
    return document, hashlib.sha256(raw).hexdigest()


def workflow_observation(pins, request=github_read):
    base = f"repos/{source_reader.REPOSITORY}/actions/runs/{pins['run_id']}"
    run, run_digest = request(base)
    jobs, jobs_digest = request(base + f"/attempts/{pins['run_attempt']}/jobs?per_page=100")
    if (run.get("id") != int(pins["run_id"]) or run.get("run_attempt") != int(pins["run_attempt"]) or
            run.get("head_sha") != pins["source_revision"] or run.get("event") != "workflow_dispatch" or
            str(run.get("path", "")).split("@", 1)[0] not in
            (".github/workflows/devlake-poc-qualify.yml",
             f"{source_reader.REPOSITORY}/.github/workflows/devlake-poc-qualify.yml") or
            run.get("conclusion") not in (None, "success") or
            not isinstance(jobs.get("jobs"), list) or jobs.get("total_count") != len(jobs["jobs"])):
        raise ValueError("native workflow run, attempt or job inventory differs")
    names = [job.get("name") for job in jobs["jobs"] if isinstance(job, dict)]
    if (len(set(names)) != len(names) or
            any(not any(job.get("name") == f"window-{window:02} / observe" and
                        job.get("conclusion") == "success" and job.get("run_id") == run["id"]
                        for job in jobs["jobs"] if isinstance(job, dict)) for window in range(12))):
        raise ValueError("native workflow has missing or failed observation windows")
    return {"run_sha256": run_digest, "attempt_jobs_sha256": jobs_digest, "window_count": 12}


def flow_observation(config, read=gateway_read):
    required = ("url", "authorization", "allowed_hosts", "flow_id", "node_id", "node_cycle",
                "node_attempts", "plan_version", "plan_hash", "decision_id")
    if (not isinstance(config, dict) or any(not config.get(field) for field in required) or
            not isinstance(config["url"], str) or
            urlsplit(config["url"]).scheme != "https" or
            config["url"] != "https://" + urlsplit(config["url"]).netloc or
            not isinstance(config["allowed_hosts"], list) or not config["allowed_hosts"] or
            type(config["plan_version"]) is not int or config["plan_version"] < 1 or
            type(config["node_cycle"]) is not int or config["node_cycle"] < 1 or
            type(config["node_attempts"]) is not int or config["node_attempts"] < 1 or
            not isinstance(config["plan_hash"], str) or not DIGEST.fullmatch(config["plan_hash"]) or
            not isinstance(config["decision_id"], str) or len(config["decision_id"]) > 128 or
            any(not isinstance(config[field], str) or not re.fullmatch(r"[0-9a-f-]{36}", config[field])
                for field in ("flow_id", "node_id"))):
        raise ValueError("selected native flow and accepted plan pins are required")
    base = config["url"].rstrip("/") + "/api/orchestration/flows/" + quote(config["flow_id"], safe="")
    def fetch(path):
        return read(base + path, config["authorization"], config["allowed_hosts"])
    plans, plans_digest = fetch("/plans")
    execution, execution_digest = fetch("/execution?limit=200&offset=0")
    recovery, recovery_digest = read(
        config["url"].rstrip("/") + "/api/orchestration/nodes/" + quote(config["node_id"], safe="") +
        "/recovery?flow_id=" + quote(config["flow_id"], safe=""),
        config["authorization"], config["allowed_hosts"])
    if not isinstance(plans, list) or len(plans) != len({plan.get("version") for plan in plans if isinstance(plan, dict)}):
        raise ValueError("native accepted plan inventory is missing or ambiguous")
    selected = [plan for plan in plans if isinstance(plan, dict) and
                plan.get("version") == config["plan_version"] and plan.get("plan_hash") == config["plan_hash"] and
                plan.get("accepted_by_decision_id") == config["decision_id"] and
                plan.get("superseded_at") is None]
    if len(selected) != 1:
        raise ValueError("native accepted plan version or authority reference differs")
    rows = execution.get("executions") if isinstance(execution, dict) else None
    if (not isinstance(rows, list) or execution.get("legacy") is not False or
            execution.get("flow_id") != config["flow_id"] or execution.get("offset") != 0 or
            execution.get("total") != len(rows) or not rows):
        raise ValueError("native execution ledger is missing or truncated")
    relevant = [row for row in rows if isinstance(row, dict) and row.get("node_id") == config["node_id"] and row.get("cycle") == config["node_cycle"]]
    if (len(relevant) != 1 or relevant[0].get("action_overflow") is not False or
            not isinstance(relevant[0].get("actions"), list) or
            not isinstance(recovery, dict) or recovery.get("contract") != "node-recovery-v1" or
            recovery.get("flow_id") != config["flow_id"] or recovery.get("node_id") != config["node_id"] or
            recovery.get("attempts") != config["node_attempts"] or
            not isinstance(recovery.get("revision"), str) or not DIGEST.fullmatch(recovery["revision"])):
        raise ValueError("native recovery snapshot differs from complete execution")
    unresolved = [action for action in relevant[0]["actions"] if not isinstance(action, dict) or
                  action.get("resolved") is not True or not action.get("receipt_ref")]
    return {"plans_sha256": plans_digest, "execution_sha256": execution_digest,
            "recovery_sha256": recovery_digest, "plan_hash": config["plan_hash"],
            "decision_id_sha256": hashlib.sha256(config["decision_id"].encode()).hexdigest(),
            "recovery_revision_sha256": hashlib.sha256(recovery["revision"].encode()).hexdigest(),
            "observed_action_count": len(relevant[0]["actions"]), "unresolved_action_count": len(unresolved)}


def checkpoint_observation(root, manifest_path, revision):
    campaign_id = resume_campaign.campaign_from_manifest(root, manifest_path, revision)
    result = resume_campaign.qualify_unattended(root, campaign_id, revision)
    if result.get("status") != "pass":
        raise ValueError("native checkpoint continuation is incomplete")
    directory = Path(manifest_path).parent
    checkpoint = resume_campaign._read_private(directory / "checkpoint.json", "checkpoint")
    report = resume_campaign._read_private(directory / "report.json", "report")
    return {"scope": "offline-checkpoint-correlated-with-native-readbacks",
            "checkpoint_sha256": hashlib.sha256(json.dumps(checkpoint, sort_keys=True).encode()).hexdigest(),
            "report_sha256": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
            "window_count": len(checkpoint["batches"])}


def collect(manifest, config, root, manifest_path, workflow=workflow_observation,
            flow=flow_observation, checkpoint=checkpoint_observation):
    pins = _pilot_pins(manifest)
    if (not isinstance(config, dict) or config.get("source_revision") != pins["source_revision"] or
            config.get("definition_revision") != pins["definition_revision"]):
        raise ValueError("native source revision pins differ")
    observed_workflow = workflow(pins)
    observed_flow = flow(config.get("gateway"))
    observed_checkpoint = checkpoint(root, manifest_path, pins["source_revision"])
    if observed_checkpoint.get("window_count") != observed_workflow.get("window_count"):
        raise ValueError("native checkpoint windows differ from independent workflow")
    return {"scope": "live-source-measurement", "source_revision": pins["source_revision"],
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "observations": {"workflow": observed_workflow, "gateway": observed_flow,
                             "checkpoint": observed_checkpoint}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Measure native plan and recovery readbacks")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = private_json(args.manifest)
        if manifest.get("source_revision") != args.revision:
            raise ValueError("native manifest differs from selected revision")
        result = collect(manifest, private_json(args.config), args.root, args.manifest)
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(result, output, sort_keys=True)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"FAIL native source: {error if isinstance(error, ValueError) else 'source unavailable or invalid'}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
