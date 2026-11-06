"""Produce private B02 inputs from executed browser cases, never from pass defaults."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

from tests.e2e.product_onboarding.product_v02_evidence import CASES


def private_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    return str(path)


class CaseEvidence:
    def __init__(self):
        self.phases = {}
        self.reads = []
        self.viewports = []
        self.cleanup = []

    @property
    def status(self):
        if "failed" in self.phases.values():
            return "fail"
        if "skipped" in self.phases.values():
            return "skip"
        if all(self.phases.get(phase) == "passed" for phase in ("setup", "call", "teardown")):
            return "pass"
        return "interrupted" if self.phases else "not_run"

    def request(self, request):
        record = self

        class ReadbackRequest:
            def get(self, url, **kwargs):
                response = request.get(url, **kwargs)
                if not urlsplit(url).path.startswith("/api/v1/products"):
                    return response
                body = response.body()
                if len(record.reads) >= 50 or len(body) > 2 * 1024 * 1024:
                    raise ValueError("Product API evidence exceeds its bound")
                record.reads.append({"status": response.status, "body": json.loads(body)})
                return response

            def __getattr__(self, name):
                return getattr(request, name)

        return ReadbackRequest()


class BrowserEvidence:
    def __init__(self, root, environment):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ValueError("Product evidence directory must be private")
        self.run = int(environment["GITHUB_RUN_ID"])
        self.attempt = int(environment["GITHUB_RUN_ATTEMPT"])
        if self.run <= 0 or self.attempt <= 0:
            raise ValueError("Product evidence requires a workflow run and attempt")
        self.run_id = f"product-{self.run}-{self.attempt}"
        self.identities = {
            "source_revision": environment["PRODUCT_EXPECTED_REVISION"],
            "browser_code_revision": environment["GITHUB_SHA"],
            "browser_workflow_revision": environment["PRODUCT_BROWSER_WORKFLOW_REVISION"],
        }
        self.records = {name: CaseEvidence() for name in CASES}
        self.context = {}

    def case(self, nodeid):
        return next((self.records[name] for name, spec in CASES.items() if spec[2] == nodeid), None)

    def bind(self, fixture):
        if fixture["run_id"] != self.run_id or fixture["source_revision"] != self.identities["source_revision"]:
            raise ValueError("Product evidence fixture differs from the workflow run")
        context = deepcopy(fixture.get("evidence_context", {}))
        if self.context and context != self.context:
            raise ValueError("Product deployment evidence changed between scenarios")
        self.context = context

    def write(self):
        def save(name, value):
            return private_json(self.root / name, value)

        cases, readbacks, cleanup_records, viewports = {}, {}, [], {}
        for name, spec in CASES.items():
            record = self.records[name]
            actual = f"Browser case {record.status}; {len(record.reads)} independent API reads"
            evidence_ref = save(name + "-browser.json", {"phases": record.phases, "viewports": record.viewports})
            readbacks[name] = save(name + "-api.json", record.reads)
            for entry in record.viewports:
                if entry.get("trace"):
                    viewports[str(entry["width"])] = {
                        "screenshot_ref": entry["screenshot"], "trace_ref": entry["trace"],
                        "overflow": entry["overflow"], "keyboard": entry["keyboard"],
                    }
            cleanup_records.extend(record.cleanup)
            attempt = {
                "attempt_id": f"{self.run}/{self.attempt}/{name}",
                "workflow_run_id": self.run, "workflow_attempt": self.attempt,
                "result": record.status, "expected": spec[0] + " browser and API assertions",
                "actual": actual, "evidence_ref": evidence_ref,
            }
            if record.status in ("skip", "not_run"):
                attempt["reason"] = "Browser case did not execute to completion"
            cases[name] = {
                "ac_id": spec[0], "scenario_id": spec[1], "test_ref": spec[2],
                "run_id": self.run_id, "status": record.status, "expected": attempt["expected"],
                "actual": actual, "evidence_ref": evidence_ref, "readback_ref": readbacks[name],
                "attempts": [attempt],
            }
        cleanup_ref = save("product-v02-cleanup.json", cleanup_records)
        cleaned = all(record.cleanup for record in self.records.values()) and all(
            entry.get("run_id") == self.run_id and all(entry.get(key) is True for key in (
                "fixture_removed", "owned_absent", "shared_unchanged",
            )) for entry in cleanup_records
        )
        for case in cases.values():
            case["cleanup_ref"] = cleanup_ref
        passing = all(record.status == "pass" for record in self.records.values())
        observed = passing and all(record.reads for record in self.records.values())
        ui_ref = save("product-v02-ui.json", cases)
        api_ref = save("product-v02-api.json", {name: record.reads for name, record in self.records.items()})
        common = {
            "run_id": self.run_id, "workflow_run_id": self.run, "workflow_attempt": self.attempt,
            "source_revision": self.identities["source_revision"],
            "browser_workflow_revision": self.identities["browser_workflow_revision"],
            "expected": "All Product browser and independent API assertions pass",
            "actual": "See per-case reports; missing and interrupted cases are retained",
        }
        identities = {**self.context.get("identities", {}), **self.identities}
        inputs = {
            "run_id": self.run_id, "plan_revision": self.context.get("plan_revision"),
            "report_artifact": str(self.root / "product-v02-packet.json"),
            "identities": identities, "deployment": self.context.get("deployment"), "cases": cases,
            "surfaces": {
                "ui": {**common, "status": "pass" if passing else "incomplete", "artifact_ref": ui_ref,
                       "report_ref": ui_ref, "viewports": viewports,
                       "frontend_release_ref": identities.get("frontend_release_ref")},
                "api": {**common, "status": "pass" if observed else "incomplete", "artifact_ref": api_ref,
                        "report_ref": api_ref, "readback_ref": api_ref, "case_readback_refs": readbacks},
                "cli": self.context.get("cli"),
            },
            "cleanup": {
                "run_id": self.run_id, "status": "verified" if cleaned else "incomplete",
                "receipt_ref": cleanup_ref, "fixture_removed": bool(cleaned),
                "owned_absent": bool(cleaned), "shared_unchanged": bool(cleaned),
                "product_id": cleanup_records[-1].get("product_id") if cleaned else None,
                "project_id": cleanup_records[-1].get("project_id") if cleaned else None,
            },
        }
        save("product-v02-inputs.json", inputs)
        return inputs
