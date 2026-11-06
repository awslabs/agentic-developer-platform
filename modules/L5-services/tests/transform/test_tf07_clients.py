"""Synthetic client and event readbacks; installed routes remain a separate check."""

from copy import deepcopy

import pytest

from l5_services.transform_client_probe import probe_clients


SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example",
         "project_id": "project-example", "relationship_revision": "1"}
ROOT = "/v1/products/product-example/projects/project-example/services/transform"


class FixtureDriver:
    def __init__(self):
        self.traces = {surface: {"surface": surface, "evidence_ref": f"private/{surface}",
                                 "requests": [("GET", f"{ROOT}/runs/run-example"),
                                              ("GET", f"{ROOT}/results/result-example")],
                                 "status": 200,
                                 "run": {"run_id": "run-example", "revision": 2, "scope": SCOPE,
                                         "state": "outcome_unknown"},
                                 "result": {"result_id": "result-example", "run_id": "run-example",
                                            "revision": 3, "scope": SCOPE, "service_id": "transform",
                                            "verification_status": "unknown", "cost_status": "unknown"}}
                       for surface in ("ui", "cli", "mcp")}
        self.receipt = {"tenant_id": "tenant-example", "product_id": "product-example",
                        "project_id": "project-example", "service_id": "transform",
                        "trigger_id": "trigger-example", "event_kind": "schedule",
                        "event_id": "schedule:2026-01-01T00:00:00Z:revision:1",
                        "idempotency_key": "tick-example", "run_id": "scheduled-example",
                        "state": "admitted", "reason": None, "evidence_ref": "private/event"}
        self.runs = [{"run_id": "scheduled-example", "scope": SCOPE,
                      "admission": {"event_id": self.receipt["event_id"]}}]
        self.scm = {"before": {"revision": "base-example", "pull_requests": 0},
                    "after": {"revision": "base-example", "pull_requests": 0},
                    "pr_actions": [], "evidence_ref": "private/scm"}
        self.calls = []

    async def read_client(self, surface, run_id, result_id):
        self.calls.append(surface)
        assert (run_id, result_id) == ("run-example", "result-example")
        return deepcopy(self.traces[surface])

    async def read_schedule_tick(self, trigger_id, *, replay):
        self.calls.append("replay" if replay else "tick")
        assert trigger_id == "trigger-example"
        return deepcopy(self.receipt)

    async def read_event_runs(self, receipt):
        self.calls.append("event-runs")
        assert receipt == self.receipt
        return deepcopy(self.runs)

    async def read_scm(self):
        self.calls.append("scm")
        return deepcopy(self.scm)


async def check(driver):
    return await probe_clients(driver, scope=SCOPE, run_id="run-example",
                               result_id="result-example", trigger_id="trigger-example")


@pytest.mark.asyncio
async def test_three_scoped_readbacks_and_single_replayed_occurrence():
    driver = FixtureDriver()
    report = await check(driver)
    assert report["live_qualified"] is False
    assert report["occurrence_count"] == 1 and report["scheduled_run_id"] != report["run_id"]
    assert set(report["client_refs"]) == {"ui", "cli", "mcp"}
    assert driver.calls == ["ui", "cli", "mcp", "tick", "replay", "event-runs", "scm"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "wrong_route", "foreign_scope", "other_revision", "unknown_as_success", "missing_trace",
    "replayed_tick", "duplicate_run", "foreign_event", "implicit_pr", "changed_scm",
])
async def test_inconsistent_client_event_or_scm_evidence_is_rejected(change):
    driver = FixtureDriver()
    if change == "wrong_route":
        driver.traces["cli"]["requests"][1] = ("GET", "/v1/results/result-example")
    elif change == "foreign_scope":
        driver.traces["mcp"]["result"]["scope"] = {**SCOPE, "tenant_id": "foreign"}
    elif change == "other_revision":
        driver.traces["ui"]["result"]["revision"] = 4
    elif change == "unknown_as_success":
        driver.traces["ui"]["run"]["state"] = "succeeded"
    elif change == "missing_trace":
        driver.traces["ui"]["evidence_ref"] = ""
    elif change == "replayed_tick":
        original = driver.read_schedule_tick

        async def changed(trigger_id, *, replay):
            receipt = await original(trigger_id, replay=replay)
            if replay:
                receipt["run_id"] = "another-run"
            return receipt

        driver.read_schedule_tick = changed
    elif change == "duplicate_run":
        driver.runs.append(deepcopy(driver.runs[0]))
    elif change == "foreign_event":
        driver.receipt["tenant_id"] = "foreign"
    elif change == "implicit_pr":
        driver.scm["pr_actions"] = ["opened"]
    elif change == "changed_scm":
        driver.scm["after"]["revision"] = "other"
    with pytest.raises(ValueError):
        await check(driver)
