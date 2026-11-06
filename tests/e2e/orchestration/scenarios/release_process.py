"""Run pinned fixture tests inside the observed source or gateway image."""

import io
import json
from pathlib import Path
import re
import sys
import unittest


def run(request):
    if set(request) not in (
        {"qualification_id", "tests"},
        {"qualification_id", "tests", "product"},
    ) or not re.fullmatch(r"q-[a-z0-9-]{8,50}", request["qualification_id"]):
        raise ValueError("invalid fixture scope")
    if "product" in request and request["product"] is not True:
        raise ValueError("invalid Product fixture selector")
    root = (
        Path(
            "/app/qualification-api/qualification"
            if request.get("product")
            else "/app/src/qualification"
        )
        / request["qualification_id"]
    )
    if not root.is_dir() or root.is_symlink() or not (root / "pricing.py").is_file():
        raise ValueError("fixture code is absent from the deployed image")
    namespace = {
        "__name__": "q2_deployed_fixture",
        "__file__": str(Path("/app/tests") / "test_qualification_api.py"),
    }
    # This source comes only from the pinned harness's fixed definition; the
    # evaluation context supplies no executable text or arbitrary command.
    exec(compile(request["tests"], "pinned-qualification-tests", "exec"), namespace)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["PricingTest"])
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    return {
        "live": True,
        "successful": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "skipped": len(result.skipped),
        "failures": len(result.failures),
        "errors": len(result.errors),
    }


if __name__ == "__main__":
    try:
        value = run(json.loads(sys.argv[1]))
    except Exception as exc:
        value = {"live": True, "status": "NOT_RUN", "reason": type(exc).__name__}
    print("ADP_Q2_RESULT:" + json.dumps(value), flush=True)
