"""Audit the documented gateway rollout choice against pinned local repository source."""

import json
import subprocess
from pathlib import Path

from check_catalog_contract import HOME, require


ROOT = HOME.parent.parent
EVIDENCE = json.loads((HOME / "catalog-rollout-evidence.json").read_text())
DECISION = (HOME / "catalog-rollout.md").read_text()


def main():
    inspected = EVIDENCE["inspected_source_revision"]
    require(len(inspected) == len(EVIDENCE["requirements_revision"]) == 40, "Unpinned source or design revision")
    for assertion in EVIDENCE["source_assertions"]:
        path = assertion["path"]
        require(path in DECISION, f"Source is not traced in decision: {path}")
        result = subprocess.run(
            ["git", "show", f"{inspected}:{path}"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        require(result.returncode == 0, f"Pinned local source unavailable: {path}")
        require((ROOT / path).read_text() == result.stdout, f"Workflow/source changed after audited revision: {path}")
        for needle in assertion["contains"]:
            require(needle in result.stdout, f"Unsubstantiated source claim: {path} / {needle}")
    for required in (
        "Gateway catalog unit", "Profile registry remains owner", "pre-serving migration job",
        "No new deployable", "then-current", "disable", "migration head",
        "two run-owned tenant scopes", "independent readback", "durable manifest", "V12 (#8273)",
    ):
        require(required in DECISION, f"Rollout decision lost contract element: {required}")
    require("/api/v1/catalog" in (HOME / "catalog-api.md").read_text(), "Catalog API no longer matches chosen gateway route")
    print(f"PASS: AC-03 pinned gateway source/workflow audit ({len(EVIDENCE['source_assertions'])} inspected paths); no new runtime")


if __name__ == "__main__":
    main()
