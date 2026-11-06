"""Set up and submit selected Product work after the shared workflow resumes it."""

from copy import deepcopy
import re
import time
from urllib.parse import quote

from l5_services.adapters.well_architected.qualification import ProductReader


def validate_case(case):
    setup = case["bootstrap"]
    scope = case["scope"]
    if (not isinstance(setup, dict) or set(setup) != {
            "binding_operation", "setup_idempotency_key", "run_idempotency_key", "timeout_seconds", "poll_seconds"}
            or case.get("project_kind") != "non_adp"
            or any(not isinstance(scope.get(field), str) or not scope[field]
                   for field in ("tenant_id", "product_id", "project_id"))
            or type(case.get("configuration_revision")) is not int or case["configuration_revision"] < 1
            or not isinstance(case.get("configuration_hash"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", case["configuration_hash"])
            or not isinstance(case.get("sources"), list) or not case["sources"]
            or any(field in case for field in ("run_id", "run_revision", "result_id", "result_revision"))
            or any(not isinstance(setup.get(field), str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", setup[field])
                   for field in ("setup_idempotency_key", "run_idempotency_key"))
            or setup["setup_idempotency_key"] == setup["run_idempotency_key"]
            or type(setup["timeout_seconds"]) is not int or not 30 <= setup["timeout_seconds"] <= 3600
            or type(setup["poll_seconds"]) is not int or not 1 <= setup["poll_seconds"] <= 60):
        raise ValueError("invalid explicit Product bootstrap selection")
    request = setup["binding_operation"]
    fields = {"action", "configuration_revision"}
    if isinstance(request, dict) and request.get("action") == "link":
        fields.add("native_resource_ref")
    if (not isinstance(request, dict) or set(request) != fields
            or request.get("action") not in {"create", "link"}
            or request.get("configuration_revision") != case["configuration_revision"]):
        raise ValueError("bootstrap permits only selected profile create or link")


def preflight(cases, product_factory):
    scopes = set()
    for case in cases:
        validate_case(case)
        scope = tuple(case["scope"][field] for field in ("tenant_id", "product_id", "project_id"))
        if scope in scopes:
            raise ValueError("bootstrap requires distinct selected Products")
        scopes.add(scope)
        with product_factory(case) as client:
            observed = ProductReader(client, case["scope"]).get("qualification-prerequisites")
        config = observed.get("configuration") or {}
        if (observed.get("schema_version") != 1 or observed.get("status") != "ready_for_setup"
                or observed.get("provider_identity") != case["provider_identity"]
                or config.get("revision") != case["configuration_revision"]
                or config.get("hash") != case["configuration_hash"]
                or any(config.get("scope", {}).get(field) != case["scope"].get(field)
                       for field in ("tenant_id", "product_id", "project_id"))
                or config.get("handoff_state") != "linked" or config.get("settings", {}).get("enabled") is not True
                or config["settings"].get("source_refs") != case["sources"]
                or config["settings"].get("well_architected", {}).get("review_mode") != "source_only"
                or not config["settings"]["well_architected"].get("goal_refs")
                or any(goal not in case["sources"] for goal in config["settings"]["well_architected"]["goal_refs"])):
            raise ValueError("Product bootstrap prerequisites differ from selection")
    if len(scopes) != 2:
        raise ValueError("bootstrap requires two distinct selected Products")


def wait_for(reader, path, initial, deadline, interval, *, identifier, expected, clock, sleep):
    current = initial
    while True:
        if current.get(identifier) != expected:
            raise ValueError("Product work identity changed")
        state = current.get("state")
        if state == "succeeded":
            return current
        if state in {"failed", "cancelled", "expired", "rejected", "superseded"} or clock() >= deadline:
            raise ValueError("Product work incomplete; retain and reconcile its original identity")
        sleep(interval)
        current = reader.get(path)


def run_results(reader, run):
    results = []
    cursor = None
    seen = set()
    for _ in range(100):
        page = reader.get("results?limit=100" + ("&cursor=" + quote(cursor, safe="") if cursor else ""))
        results.extend(item for item in page["items"] if item.get("run_id") == run["run_id"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
        if cursor in seen:
            raise ValueError("Product result pagination repeated")
        seen.add(cursor)
    else:
        raise ValueError("Product result pagination exceeded bound")
    return results


def completed_selection(case, reader, run, binding, deadline, interval, clock, sleep):
    while True:
        results = run_results(reader, run)
        if any(type(item.get("revision")) is not int or item["revision"] < 1 for item in results):
            raise ValueError("Product result revision unavailable")
        latest = max((item["revision"] for item in results), default=0)
        matching = [item for item in results if item["revision"] == latest]
        if len(matching) > 1:
            raise ValueError("completed Product run has ambiguous latest results")
        result = matching[0] if matching else {}
        if result.get("recommendations") and all(isinstance(item.get("comparison"), dict) for item in result["recommendations"]):
            break
        if clock() >= deadline:
            raise ValueError("completed Product run is awaiting its independent comparison")
        sleep(interval)
    selected = {key: deepcopy(value) for key, value in case.items() if key != "bootstrap"}
    selected.update(binding_id=binding["binding_id"], binding_revision=binding["revision"],
                    run_id=run["run_id"], run_revision=run["revision"],
                    result_id=result["result_id"], result_revision=result["revision"])
    return selected


def run(cases, product_factory, *, journal, clock=None, sleep=None, after_case=None):
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    preflight(cases, product_factory)
    selections = []
    for case in cases:
        setup = case["bootstrap"]
        deadline = clock() + setup["timeout_seconds"]
        with product_factory(case) as client:
            reader = ProductReader(client, case["scope"])
            operation = journal.post(reader, case, "binding-operations", setup["binding_operation"], setup["setup_idempotency_key"])
            operation_id = operation["operation_id"]
            operation = wait_for(reader, "binding-operations/" + quote(operation_id, safe=""), operation,
                                 deadline, setup["poll_seconds"], identifier="operation_id", expected=operation_id,
                                 clock=clock, sleep=sleep)
            binding = reader.get("bindings/" + quote(operation["binding_id"], safe=""))
            if binding.get("state") != "ready" or binding.get("binding_id") != operation["binding_id"]:
                raise ValueError("Product setup did not produce its ready binding")
            work = journal.post(reader, case, "runs", {
                "service_id": "well_architected", "configuration_revision": case["configuration_revision"],
                "binding_id": binding["binding_id"], "capability": "architecture_review", "sources": case["sources"],
            }, setup["run_idempotency_key"])
            run_id = work["run_id"]
            work = wait_for(reader, "runs/" + quote(run_id, safe=""), work,
                            deadline, setup["poll_seconds"], identifier="run_id", expected=run_id, clock=clock, sleep=sleep)
            selections.append(completed_selection(case, reader, work, binding, deadline, setup["poll_seconds"], clock, sleep))
        if after_case is not None:
            after_case(len(selections), selections[-1])
    return selections


def validate_resolved(cases, resolved):
    if not isinstance(resolved, list) or len(resolved) != len(cases):
        raise ValueError("bootstrap readback selection incomplete")
    for case, selected in zip(cases, resolved, strict=True):
        if any(selected.get(key) != value for key, value in case.items() if key != "bootstrap"):
            raise ValueError("bootstrap readback selection changed")
    return resolved
