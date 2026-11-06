"""Bind private Security selector executions to one reviewed release attempt."""

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path


SCENARIOS = ("setup", "assessments", "recovery", "context")
SELECTOR_NODES = {
    "setup": "modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_clients_and_setup",
    "assessments": "modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_real_security_assessments",
    "recovery": "modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_isolation_recovery_cleanup",
    "context": "modules/L5-services/tests/e2e/test_l5_security_acceptance.py::test_context_reuse",
}
MAX_SELECTOR_AGE_SECONDS = 120 * 60


def private_bytes(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_size > limit:
            raise ValueError("Security selector receipt is not a bounded private file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(limit + 1)
    finally:
        os.close(descriptor)
    if not content or len(content) > limit:
        raise ValueError("Security selector receipt is empty or oversized")
    return content


def read_selector_receipts(directory, release, now):
    directory = Path(directory).resolve(strict=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("Security selector receipts require a private directory")
    run_id, attempt = release["GITHUB_RUN_ID"], release["GITHUB_RUN_ATTEMPT"]
    if not str(run_id).isdecimal() or not str(attempt).isdecimal():
        raise ValueError("Security selector receipts require a workflow run and attempt")
    digests = {}
    previous_observed = None
    for scenario in SCENARIOS:
        name = f"security-{run_id}-{attempt}-{scenario}"
        if scenario == "setup":
            candidates = [directory / (name + ".json")]
            candidates += list(directory.glob(name + "-reconcile-*.json"))
            candidates = [path for path in candidates if path.exists()]
            if len(candidates) != 1:
                raise ValueError("Security setup requires exactly one passing original-run selector receipt")
            name = candidates[0].stem
        log = private_bytes(directory / (name + ".log"), 262144)
        # Pytest appends H:MM:SS when a selector lasts at least one minute.
        # That display suffix changes neither the result nor receipt freshness.
        if (not re.search(rb"^1 passed(?:, [0-9]+ warnings?)?(?: in [0-9]+(?:\.[0-9]+)?s(?: \([0-9]+:[0-5][0-9]:[0-5][0-9]\))?)?$", log, re.MULTILINE)
                or re.search(rb"\b[0-9]+ (?:failed|errors?|skipped|deselected|xfailed|xpassed)\b", log)):
            raise ValueError("Security selector log does not establish one passing test")
        content = private_bytes(directory / (name + ".json"), 4096)
        receipt = json.loads(content)
        expected = {"scenario": scenario, "run_id": str(run_id), "run_attempt": str(attempt),
                    "selector_node": SELECTOR_NODES[scenario],
                    "source_revision": release["SOURCE_SHA"],
                    "fixture_sha256": release["L5_FIXTURE_DIGEST"],
                    "log_sha256": hashlib.sha256(log).hexdigest(), "result": "passed"}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError("Security selector result belongs to another release or log")
        try:
            observed = datetime.fromisoformat(receipt["observed_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Security selector receipt has no observation time") from error
        if (observed.utcoffset() is None or not 0 <= (now - observed).total_seconds() <= MAX_SELECTOR_AGE_SECONDS
                or previous_observed is not None and observed < previous_observed):
            raise ValueError("Security selector receipt is stale or after native readback")
        previous_observed = observed
        digests[scenario] = hashlib.sha256(content).hexdigest()
    return digests
