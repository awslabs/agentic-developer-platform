"""Example harness adapter: wrap independently produced integration outcomes.

Run in the pinned workflow after its integration test writes a JSON array of
{criterion_id, outcome, evidence_references} objects. The engine authenticates
and verifies the resulting run/attempt artifact; this script grants no authority.
"""

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def emit(results_path, output_path):
    manifest = os.environ["INPUT_SOURCE_MANIFEST"]
    sources = json.loads(manifest)
    criteria = json.loads(os.environ["INPUT_PROBE_CRITERIA"])
    observed = json.loads(Path(results_path).read_text())
    outcomes = observed["criteria"]
    expected = {item["criterion_id"] for item in criteria}
    if (
        not sources["sources"]
        or any(not source.get("revision") or not source.get("components") or not source.get("targets") for source in sources["sources"])
        or sources["target_contract_hash"] != os.environ["INPUT_TARGET_CONTRACT_HASH"]
        or len(expected) != len(criteria)
        or {item["criterion_id"] for item in outcomes} != expected
        or len(outcomes) != len(expected)
        or any(not item.get("evidence_references") for item in outcomes)
        or not observed.get("runtime_readbacks")
    ):
        raise ValueError("probe results or source manifest do not match dispatched inputs")
    result = {
        "evidence_schema": "deployed-probe-result/v1",
        "correlation": os.environ["INPUT_ADP_CORRELATION"],
        "source_snapshot_hash": os.environ["INPUT_SOURCE_SNAPSHOT_HASH"],
        "source_manifest_digest": hashlib.sha256(manifest.encode()).hexdigest(),
        "specification_digest": os.environ["INPUT_SPECIFICATION_DIGEST"],
        "target_contract_hash": os.environ["INPUT_TARGET_CONTRACT_HASH"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "observed_at": datetime.now(UTC).isoformat(),
        "criteria": outcomes,
        "runtime_readbacks": observed["runtime_readbacks"],
    }
    Path(output_path).write_text(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    emit(sys.argv[1], sys.argv[2])
