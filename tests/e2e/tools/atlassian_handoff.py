"""Validate controlled handoff metadata without manufacturing live evidence."""

import json
import re
from pathlib import Path

from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked


MATRIX_PATH = Path(__file__).resolve().parents[3] / "docs/tools/atlassian-mcp/readiness-8446.json"
DESIGN_REVISION = "3454ce6b73d7a263e4e90979d6eaa49f5896d098"


def review_handoff(report: dict, matrix_path=MATRIX_PATH) -> dict:
    matrix = json.loads(Path(matrix_path).read_text())
    if matrix.get("issue") != 8446 or matrix.get("design_revision") != DESIGN_REVISION:
        raise ProbeBlocked("Handoff source or requirement revision does not match the pinned design")
    criteria = matrix.get("criteria", {})
    if set(criteria) != {"AC-01", "AC-02", "AC-03"} or {item["scenario"] for item in criteria.values()} != {"SCN-01", "SCN-02", "SCN-03"}:
        raise ProbeBlocked("Handoff must map each criterion to its distinct proof scenario")
    if any(item.get("live") != "unproven" or not item.get("activation_blocker") for item in criteria.values()):
        raise ProbeBlocked("Generated handoff cannot assert authenticated acceptance")
    fixtures = matrix.get("fixtures", {})
    if len(fixtures) != 4 or any(not item.get("owner") or not item.get("source") for item in fixtures.values()):
        raise ProbeBlocked("Handoff is missing named fixture owners")
    if fixtures["generated_identity_and_provider_cases"].get("human_preparation") is not False:
        raise ProbeBlocked("Controlled fixture generation must not require a prepared human dataset")
    dependencies = matrix.get("forward_dependencies", [])
    by_issue = {entry["issue"]: entry for entry in dependencies}
    if len(by_issue) != len(dependencies) or by_issue.get(8446, {}).get("after") != []:
        raise ProbeBlocked("Standalone proof cannot depend on successor code")
    if by_issue.get(8447, {}).get("after") != [8446] or by_issue.get(8448, {}).get("after") != [8447]:
        raise ProbeBlocked("Setup recovery must precede user lifecycle")
    if "recovery consumer" not in by_issue[8447]["deliverable"] or "recovery consumer" not in by_issue[8448]["deliverable"]:
        raise ProbeBlocked("A planned recovery consumer is not proof it has been delivered")
    if any(dependency not in by_issue or dependency >= issue for issue, entry in by_issue.items() for dependency in entry["after"]):
        raise ProbeBlocked("Dependencies must be forward-only and present in the handoff")
    if not matrix.get("client_qualification", {}).get("installed_client_owner"):
        raise ProbeBlocked("Client qualification needs a separate owner")
    if report.get("design_revision") != DESIGN_REVISION or not re.fullmatch(r"[0-9a-f]{40}", report.get("source_revision", "")):
        raise ProbeBlocked("The controlled report lacks an immutable source and design revision")
    if report.get("phase") != matrix["phase"] or report.get("live_status") != "unproven":
        raise ProbeBlocked("Generated cases cannot claim live provider acceptance")
    cases = report.get("scenarios", {})
    if set(cases) != {item["scenario"] for item in criteria.values()} or any(status not in {"pass", "fail", "blocked"} for status in cases.values()):
        raise ProbeBlocked("All three independently named cases need explicit controlled results")
    return {
        "source_revision": report["source_revision"],
        "design_revision": DESIGN_REVISION,
        "controlled_status": "candidate" if all(status == "pass" for status in cases.values()) else "blocked",
        "live_status": "unproven",
        "activation_blockers": {criterion: item["activation_blocker"] for criterion, item in criteria.items()},
    }
