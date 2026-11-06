"""Retain V05 browser, installed CLI and API observations in private files."""

from __future__ import annotations

from tests.e2e.product_onboarding.product_v02_record import BrowserEvidence, CaseEvidence, private_json

CASES = {
    name: "tests/e2e/new_ui/test_product_resume.py::" + name
    for name in ("test_resume_ui_to_cli", "test_resume_cli_to_ui", "test_progress_math", "test_resume_recovery")
}


class ResumeEvidence(BrowserEvidence):
    def __init__(self, root, environment):
        super().__init__(root, environment)
        self.records = {name: CaseEvidence() for name in CASES}
        for record in self.records.values():
            record.cli_events = []

    def case(self, nodeid):
        return next((self.records[name] for name, selector in CASES.items() if selector == nodeid), None)

    def write(self):
        results = {}
        for name, record in self.records.items():
            artifacts = {
                surface: private_json(self.root / f"v05-{name}-{surface}.json", value)
                for surface, value in (
                    ("browser", {"phases": record.phases, "screenshots": record.viewports}),
                    ("cli", record.cli_events),
                    ("api", record.reads),
                    ("cleanup", record.cleanup),
                )
            }
            results[name] = {"test_ref": CASES[name], "status": record.status,
                             "artifacts": artifacts, "read_count": len(record.reads),
                             "cli_count": len(record.cli_events), "cleanup_count": len(record.cleanup)}
        return private_json(self.root / "v05-surface-report.json", {
            "run_id": self.run_id, "workflow_run_id": self.run, "workflow_attempt": self.attempt,
            "identities": self.identities, "deployment": self.context, "cases": results,
        })
