#!/usr/bin/env python3
"""Read assignment startup evidence for the three operator-selected stalled flows."""

import argparse
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location("diag", Path(__file__).with_name("diagnose-shared-runtime.py"))
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

PROBE = '''
import asyncio, json, logging
logging.disable(logging.CRITICAL)
async def main():
    from sqlalchemy import text
    from src.shared.database import get_session_factory
    async with get_session_factory()() as s:
        await s.execute(text("SET TRANSACTION READ ONLY"))
        await s.execute(text("SET LOCAL statement_timeout = '10s'"))
        rows = (await s.execute(text("""
          SELECT r.run_id, r.flow_id, r.node_id, n.issue_ref, n.state,
                 r.persona, r.attempt, r.created_at,
                 r.dispatch_metadata -> 'correlation' ->> 'correlation_id' AS correlation_id,
                 r.worker_receipt IS NOT NULL AND r.worker_receipt != 'null'::jsonb AS started,
                 r.terminal_receipt IS NOT NULL AND r.terminal_receipt != 'null'::jsonb AS terminal,
                 r.block_code
          FROM orchestration_run_reports r JOIN orchestration_nodes n ON n.id=r.node_id AND n.org_id=r.org_id
          WHERE r.org_id='aws-e' AND r.flow_id IN
          ('0737183c-99c4-4e1f-bdb7-e4432b46ca20','a555da26-2724-4f38-b960-8738e10fa88c','0022abf5-83a3-4ee4-8af6-0611bda45ea9')
          AND r.attempt=n.attempts ORDER BY r.created_at LIMIT 100
        """))).mappings().all()
        print(json.dumps([dict(r) for r in rows], default=str))
asyncio.run(main())
'''


def main(account, directory):
    result = {"read_only": True, "observed_at": datetime.now(UTC).isoformat()}
    with tempfile.TemporaryDirectory(prefix="adp-flow-diagnose-") as scratch:
        result["identity"] = diag.identity(account, Path(scratch))
        read = subprocess.run([
            "kubectl", "--request-timeout=30s", "exec", "-i", "deployment/bedrockgateway", "-n", "adp-gateway",
            "-c", "bedrockgateway", "--", "python", "-",
        ], input=PROBE, capture_output=True, text=True, timeout=60)
        if read.returncode:
            raise diag.DiagnosticError("assignment_probe_failed")
        result["assignments"] = json.loads(read.stdout)
        result["bootstrap"] = []
        for row in result["assignments"]:
            if row["terminal"] or (row["started"] and row["state"] == "running"):
                continue
            correlation = row["correlation_id"]
            if not isinstance(correlation, str) or not re.fullmatch(r"[a-zA-Z0-9:_-]{1,160}", correlation):
                continue
            stream = correlation.replace(":", "-")
            command = [
                "aws", "logs", "filter-log-events", "--log-group-name", "/adp/dev/agent-factory/bootstrap",
                "--log-stream-names", stream, "--start-time", str(int(datetime.fromisoformat(row["created_at"]).timestamp() * 1000)),
                "--limit", "200", "--no-paginate", "--region", diag.REGION, "--output", "json",
            ]
            raw = {"events": []}
            token = None
            for _ in range(8):
                page = diag.decode(diag.run(command + (["--next-token", token] if token else [])))
                raw["events"].extend(page.get("events", []))
                next_token = page.get("nextToken")
                if not next_token or next_token == token:
                    token = None
                    break
                token = next_token
            raw["nextToken"] = token
            events = []
            for event in raw.get("events", []):
                message = event.get("message", "")
                events.append({
                    "timestamp": event.get("timestamp"),
                    "steps": re.findall(r"\[bootstrap step=(\d+) name=([a-z0-9_]+)\] (ENTER|OK|FAILED)", message),
                    "exceptions": re.findall(r"exception=([A-Za-z0-9_]+)", message),
                    "known_errors": [v for v in (
                        "Invalid protected review-cycle input", "work ownership startup deadline exceeded",
                        "Work ownership requires authenticated worker reporting", "Review-cycle checkout requires scoped GitHub credentials",
                        "Review-cycle PR head changed before worker startup", "run_report_http_403", "run_report_http_409",
                        "work claim", "CalledProcessError", "exit status 1", "exit status 128", "checkout", "report_superseded",
                    ) if v in message],
                })
            result["bootstrap"].append({"run_id": row["run_id"], "stream": stream, "events": events, "truncated": bool(raw.get("nextToken"))})
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "flow-recovery.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--evidence-directory", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    main(args.account_id, args.evidence_directory)
