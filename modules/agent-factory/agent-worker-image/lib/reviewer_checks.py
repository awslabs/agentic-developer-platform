"""Host-only bridge from the retained Node controller to report-authenticated CI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from lib import run_report, status_gateway_client


def main():
    head = sys.argv[1]
    # The worker owns these values; no credential appears in argv or model env.
    try:
        body = {"head_sha": head, "for_merge": "--for-merge" in sys.argv}
        if os.environ.get("ADP_RUN_REPORT_CREDENTIAL_FILE"):
            run_report._assignment = {
                "credential": Path(os.environ["ADP_RUN_REPORT_CREDENTIAL_FILE"]).read_text(),
                "run_id": os.environ["ADP_MESSAGE_ID"],
                "attempt": int(os.environ["ADP_ORCHESTRATION_ATTEMPT"]),
            }
            result = run_report.request("/review-checks", body, timeout=60)
        else:
            result = status_gateway_client._post_bytes(
                "/review-checks", json.dumps(body).encode(), content_type="application/json", timeout_seconds=60, max_response_bytes=128 * 1024)
    except run_report.RunReportError as error:
        print(json.dumps({"error": error.code, "retryable": error.retryable}))
        return
    except status_gateway_client.StatusGatewayError as error:
        print(json.dumps({"error": "protected_review_checks_unavailable", "retryable": error.retryable}))
        return
    print(json.dumps(result))


if __name__ == "__main__":
    main()
