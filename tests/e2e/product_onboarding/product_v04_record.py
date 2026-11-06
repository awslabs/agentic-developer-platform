"""Private B03 browser/API observations from executed selected tests."""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path

from tests.e2e.product_onboarding.product_v02_record import CaseEvidence, private_json
from tests.e2e.product_onboarding.product_v04_evidence import SURFACE_SELECTORS


class ScopeEvidence:
    def __init__(self, root, environment):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ValueError("B03 evidence directory must be private")
        self.run, self.attempt = (
            int(environment["GITHUB_RUN_ID"]),
            int(environment["GITHUB_RUN_ATTEMPT"]),
        )
        if self.run <= 0 or self.attempt <= 0:
            raise ValueError("B03 workflow identity is missing")
        self.run_id = f"product-{self.run}-{self.attempt}"
        self.source = environment["PRODUCT_EXPECTED_REVISION"]
        self.workflow = environment["PRODUCT_BROWSER_WORKFLOW_REVISION"]
        self.checkout = environment["GITHUB_SHA"]
        self.records = {
            selector: CaseEvidence()
            for selectors in SURFACE_SELECTORS.values()
            for surface, selector in selectors.items()
            if surface in ("api", "browser")
        }
        self.context = None
        self.cleanup = None

    def case(self, nodeid):
        return self.records.get(nodeid)

    def bind(self, fixture):
        if (
            fixture["run_id"] != self.run_id
            or fixture["source_revision"] != self.source
        ):
            raise ValueError("B03 scope fixture is from another workflow attempt")
        context = deepcopy(fixture.get("evidence_context"))
        if (
            not isinstance(context, dict)
            or not context.get("deployment")
            or not context.get("identities")
        ):
            raise ValueError("B03 requires independent signed release observations")
        if self.context is not None and self.context != context:
            raise ValueError("B03 deployment changed between cases")
        self.context = context

    def record_cleanup(self, provider, receipt):
        state = provider.state.document
        scope = state.get("scope", {})
        owned = receipt.get("cases", {}).get("scope", {})
        if self.context is None or self.cleanup is not None:
            raise ValueError("B03 cleanup is missing its selected fixture")
        history = scope.get("cli_history", {})
        preserved = (
            history.get("status") == "restored"
            and any(
                item.get("result") == "pass"
                for item in getattr(self, "api_attempts", {}).get("AC-03", [])
            )
            and any(
                item.get("result") == "pass"
                for item in getattr(self, "cli_attempts", {}).get("AC-03", [])
            )
        )
        self.cleanup = {
            "run_id": receipt.get("run_id"),
            "original_projects_preserved": preserved
            and owned.get("fixture_removed") is True,
            "reused_assets_preserved": preserved
            and owned.get("shared_unchanged") is True,
            "receipt": owned,
            "verified": receipt.get("run_id") == self.run_id
            and scope.get("cleanup_pending") is False
            and scope.get("cleanup_receipt") == owned
            and owned.get("owned_absent") is True
            and owned.get("shared_unchanged") is True
            and owned.get("fixture_removed") is True
            and not any(
                state.get(key)
                for key in (
                    "restore_required",
                    "scope_editor_restore_required",
                    "scope_owner_role_restore_required",
                )
            ),
        }

    def write(self):
        cases = {}
        for criterion, selectors in SURFACE_SELECTORS.items():
            cases[criterion] = {}
            for surface, selector in selectors.items():
                if surface == "cli":
                    continue
                record = self.records[selector]
                name = criterion.lower() + "-" + surface
                artifact = private_json(
                    self.root / (name + "-result.json"),
                    {
                        "phases": record.phases,
                        "viewports": record.viewports,
                    },
                )
                readback = private_json(self.root / (name + "-api.json"), record.reads)
                cases[criterion][surface] = {
                    "selector": selector,
                    "status": record.status,
                    "artifact": artifact,
                    "readback": readback,
                    "reads": len(record.reads),
                    "viewports": record.viewports,
                }
        cleanup = private_json(self.root / "product-v04-cleanup.json", self.cleanup)
        return private_json(
            self.root / "product-v04-browser-results.json",
            {
                "run_id": self.run_id,
                "source_revision": self.source,
                "workflow_run_id": self.run,
                "workflow_attempt": self.attempt,
                "workflow_revision": self.workflow,
                "checkout_revision": self.checkout,
                "context": self.context,
                "cases": cases,
                "cleanup_ref": cleanup,
            },
        )
