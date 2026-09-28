"""Host-only bridge from the retained Node controller to report-authenticated CI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from lib import run_report


def main():
    head = sys.argv[1]
    # The worker owns these values; no credential appears in argv or model env.
    run_report._assignment = {
        "credential": Path(os.environ["ADP_RUN_REPORT_CREDENTIAL_FILE"]).read_text(),
        "run_id": os.environ["ADP_MESSAGE_ID"],
        "attempt": int(os.environ["ADP_ORCHESTRATION_ATTEMPT"]),
    }
    try:
        result = run_report.request("/review-checks", {"head_sha": head, "for_merge": "--for-merge" in sys.argv}, timeout=60)
    except run_report.RunReportError as error:
        print(json.dumps({"error": error.code, "retryable": error.retryable}))
        return
    print(json.dumps(result))


if __name__ == "__main__":
    main()
