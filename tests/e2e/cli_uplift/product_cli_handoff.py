"""Move installed Product CLI proof between protected jobs via encrypted run state."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from . import ports, statestore


def retain(store, run_id, report_path, source_revision, workflow_revision):
    report = json.loads(Path(report_path).read_text())
    if (
        not isinstance(report, dict)
        or report.get("workflow_run_id") != run_id
        or report.get("status") != "passed"
        or report.get("cleanup") != "complete"
        or report.get("cleanup_outstanding") != []
        or report.get("source_revision") != source_revision
        or report.get("workflow_revision") != workflow_revision
    ):
        raise statestore.StateStoreError(
            "Installed Product CLI proof is incomplete or mismatched"
        )
    return store.retain_product_cli(run_id, report)


def load(store, dispatch_path, output_path):
    dispatch = json.loads(Path(dispatch_path).read_text())
    if not isinstance(dispatch, dict):
        raise statestore.StateStoreError("Selected Product dispatch is missing")
    report = store.product_cli(dispatch.get("run_id"))
    if (
        report.get("source_revision") != dispatch.get("expected_revision")
        or report.get("workflow_revision") != dispatch.get("workflow_revision")
        or report.get("status") != "passed"
        or report.get("cleanup") != "complete"
        or report.get("cleanup_outstanding") != []
    ):
        raise statestore.StateStoreError(
            "Installed Product CLI proof differs from dispatch"
        )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(report, stream)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("retain", "load"))
    parser.add_argument("--run-id")
    parser.add_argument("--report")
    parser.add_argument("--source-revision")
    parser.add_argument("--workflow-revision")
    parser.add_argument("--dispatch")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    bucket = os.environ.get("CLI_UPLIFT_EVAL_STATE_BUCKET")
    if not bucket:
        parser.error("Protected evaluator state bucket is unavailable")
    store = statestore.S3StateStore(
        ports.AwsPort(os.environ.get("AWS_REGION", "us-east-1")),
        bucket,
        kms_key_id=os.environ.get("CLI_UPLIFT_EVAL_STATE_KMS_KEY_ID") or None,
    )
    try:
        if args.mode == "retain":
            if not all(
                (args.run_id, args.report, args.source_revision, args.workflow_revision)
            ):
                parser.error(
                    "Retaining CLI proof needs a run, report and both revisions"
                )
            retain(
                store,
                args.run_id,
                args.report,
                args.source_revision,
                args.workflow_revision,
            )
        else:
            if not all((args.dispatch, args.output)):
                parser.error("Loading CLI proof needs a dispatch and private output")
            load(store, args.dispatch, args.output)
    except (OSError, ValueError, statestore.StateStoreError, ports.PortError):
        parser.error(
            "Protected installed Product CLI evidence is unavailable or mismatched"
        )


if __name__ == "__main__":
    main()
