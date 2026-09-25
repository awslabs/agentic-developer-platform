"""AWS-only acceptance using the SDK and general tools in the hosted agent image.

Uses maintained URL persona/skill guidance and CLI, not Bedrock tool emulation.
Platform ingress, gateway routing and GitHub delivery still require separate tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import boto3
import domain_investigation as cli
from benchmark import get_bytes, put_json, require_aws_runtime, s3_location
from live_evaluation import (
    MODEL,
    analyst_prompt,
    load_case,
    upload_case,
    validate_manifest,
)
from research_case import verify_case


def run_case(directory, row, model, *, run=subprocess.run):
    case = cli.start(
        directory,
        row["url"],
        row["objective"],
        scope=row.get("scope", "observed_external"),
        incident_context=row.get("incident_context", []),
        brand_references=row.get("brand_references", []),
    )
    skill = Path(__file__).parent
    task = {
        "cwd": str(directory.parent),
        "model": model,
        "instructions": analyst_prompt(),
        "transcript": str(directory.parent / "hosted-transcript.jsonl"),
        "prompt": (
            "Complete this existing cyber URL investigation using your Bash/Read/Write tools. "
            f"The case is already started at {directory}. The authoritative installed CLI is {skill / 'domain_investigation.py'}; "
            "use that absolute path instead of /app examples. Do not start a second case. "
            "Inspect status, contract, case.json and screenshots, choose useful next actions and enrichment, "
            "then write assessment/review files outside the case directory and call finish. "
            "Never edit case.json or captured evidence directly. Do not post messages or use GitHub. "
            "This acceptance caller publishes the complete case to S3. Do not upload separately. "
            "Use the maintained browser CLI to preserve session state; native mode connects directly to AgentCore Browser. " + row["objective"]
        ),
    }
    error = None
    try:
        result = run(
            ["node", str(skill / "hosted_session.mjs")],
            input=json.dumps(task),
            text=True,
            capture_output=True,
            timeout=275,
            env={**os.environ, "CLAUDE_CODE_USE_BEDROCK": "1"},
        )
        if result.returncode:
            error = "Hosted SDK process did not complete"
            (directory / "hosted-error.txt").write_text(result.stderr[-16000:])
    except subprocess.TimeoutExpired:
        error = "Hosted SDK exceeded its acceptance deadline"
    except OSError:
        error = "Hosted SDK process could not start"
    finally:
        case = load_case(directory)
        if case["browser_view"].get("session_open") or not case.get("stop_reason"):
            # New SDK artifacts are outside the existing manifest; save_case includes
            # them without modifying the already hashed observation content.
            cli.close(
                directory,
                "Hosted acceptance ended; preserve evidence and unresolved questions",
            )
        case = load_case(directory)
        if case["assessment"]["assessor"] != "collection-system":
            # Runtime configuration owns model provenance, not model-written JSON.
            case["assessment"]["model_version"] = model
        transcript = Path(task["transcript"])
        if transcript.exists():
            shutil.copyfile(transcript, directory / "hosted-transcript.jsonl")
        cli.save_case(directory, case)
        verify_case(directory)
    return {
        "id": row["id"],
        "kind": "hosted-sdk-acceptance",
        "error": error,
        "assessment": case["assessment"],
        "reviews": len(case["reviews"]),
        "observations": len(case["observations"]),
        "sessions": case["sessions"],
        "model_completed": not error
        and case["assessment"]["assessor"] != "collection-system",
        "cleanup_confirmed": not case.get("unconfirmed_browser_start", False)
        and all(s["cleanup_status"] == "stopped" for s in case["sessions"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args(argv)
    require_aws_runtime()
    s3_location(args.manifest)
    s3_location(args.output_prefix)
    s3 = boto3.client("s3")
    manifest = validate_manifest(json.loads(get_bytes(s3, args.manifest)))
    if not 1 <= len(manifest["cases"]) <= args.max_cases <= 10:
        raise ValueError("Hosted acceptance budget exceeded; maximum ten cases")
    prefix = args.output_prefix.rstrip("/")
    results = []
    put_json(
        s3,
        prefix + "/protocol.json",
        {
            "kind": "hosted-sdk-acceptance",
            "model": args.model,
            "limitations": [
                "Uses hosted SDK and general tools; platform worker orchestration, UI/GitHub ingress and gateway policy are not exercised."
            ],
        },
    )
    for row in manifest["cases"]:
        with tempfile.TemporaryDirectory(prefix="cyber-hosted-") as tmp:
            directory = Path(tmp) / "case"
            result = run_case(directory, row, args.model)
            upload_case(s3, prefix + "/cases/" + row["id"], directory)
            results.append(result)
            put_json(s3, prefix + "/summary.json", {"cases": results})
            print(
                json.dumps(
                    {
                        "id": row["id"],
                        "model_completed": result["model_completed"],
                        "observations": result["observations"],
                    }
                ),
                flush=True,
            )
            if not result["cleanup_confirmed"] or not result["model_completed"]:
                raise RuntimeError(
                    "Hosted acceptance did not complete; stop admitting cases"
                )


if __name__ == "__main__":
    main()
