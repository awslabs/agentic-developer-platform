"""Run-owned, bounded lifecycle fault recipes; no shared-service fault injection."""

import re
from dataclasses import dataclass, replace
from time import monotonic
from uuid import UUID, uuid4


@dataclass(frozen=True)
class FaultFixture:
    identifier: str
    stories: tuple[str, ...]
    actions: tuple[str, ...]
    max_provider_calls: int
    max_seconds: int = 900


LIFECYCLE_FIXTURES = (
    FaultFixture("SCN-TLS-0050", ("TLS-C06", "TLS-S04", "TLS-S05"),
                 ("search", "interpreter", "browser", "confirm_stop"), 3),
    FaultFixture("SCN-TLS-0051", ("TLS-C06", "TLS-S10", "TLS-S05"),
                 ("search", "duplicate", "change_payload", "recover", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0052", ("TLS-C06", "TLS-S10", "TLS-S04"),
                 ("search", "drop_response", "disconnect", "recover", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0053", ("TLS-C06", "TLS-S10", "TLS-S04"),
                 ("search", "foreign_read", "refresh_owner", "recover", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0054", ("TLS-C06", "TLS-S10", "TLS-S05"),
                 ("interpreter", "revoke_owner", "restart_reconciler", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0055", ("TLS-C06", "TLS-S10", "TLS-S05"),
                 ("browser", "race_close", "repeat_close", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0056", ("TLS-C06", "TLS-S10", "TLS-S05"),
                 ("browser", "drop_handle", "recover", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0057", ("TLS-C06", "TLS-S10"),
                 ("search", "disable_admission", "recover", "confirm_stop"), 1),
    FaultFixture("SCN-TLS-0058", ("TLS-C06", "TLS-S10", "TLS-S11", "TLS-S12"),
                 ("workload_start", "interrupt_worker", "recover", "foreign_read", "confirm_stop"), 2),
    FaultFixture("SCN-TLS-0059", ("TLS-C06", "TLS-S10", "TLS-S11", "TLS-S12"),
                 ("workload_start", "end_lease", "restart_reconciler", "confirm_stop"), 2),
)


BASE_STORIES = {
    "0001": ("TLS-S04", "TLS-S10", "TLS-S02"),
    "0002": ("TLS-S04", "TLS-S01"),
    "0003": ("TLS-S03",),
    "0004": ("TLS-S03",),
    "0005": ("TLS-C02", "TLS-C03", "TLS-C04"),
    "0006": ("TLS-C01",),
    "0010": ("TLS-S05", "TLS-S09"),
    "0011": ("TLS-S05",),
    "0012": ("TLS-S05",),
    "0013": ("TLS-S06",),
    "0014": ("TLS-S07", "TLS-S08"),
    "0015": ("TLS-S09",),
    "0016": ("TLS-C05", "TLS-C04"),
    "0017": ("TLS-C06",),
    "0018": ("TLS-D02",),
    "0020": ("TLS-S10", "TLS-S11"),
    "0021": ("TLS-S10",),
    "0022": ("TLS-S10",),
    "0023": ("TLS-S12",),
    "0030": ("TLS-S13", "TLS-S14", "TLS-S15"),
    "0031": ("TLS-S13", "TLS-S14"),
    "0032": ("TLS-S15",),
    "0033": ("TLS-S16",),
    "0040": ("TLS-S17",),
    "0041": ("TLS-S18",),
    "0042": ("TLS-S19",),
    "0043": ("TLS-S19",),
}

CLIENTS = ("cli", "claude-code", "codex")
TOOLS = ("search", "interpreter", "browser")
WORKLOADS = ("crew-pod", "engine-worker")


def matrix_cells(identifier):
    if identifier == "SCN-TLS-0050":
        return tuple((client, tool) for client in CLIENTS for tool in TOOLS)
    if identifier == "SCN-TLS-0054":
        return tuple((client, tool) for client in CLIENTS for tool in TOOLS[1:])
    if identifier in {"SCN-TLS-0055", "SCN-TLS-0056"}:
        return tuple((client, "browser") for client in CLIENTS)
    if identifier in {"SCN-TLS-0051", "SCN-TLS-0052", "SCN-TLS-0053", "SCN-TLS-0057"}:
        return tuple((client, "search") for client in CLIENTS)
    if identifier in {"SCN-TLS-0058", "SCN-TLS-0059"}:
        return tuple((workload, tool) for workload in WORKLOADS for tool in TOOLS[1:])
    if identifier in {"SCN-TLS-0010", "SCN-TLS-0011", "SCN-TLS-0012"}:
        return (("claude-code", {"SCN-TLS-0010": "search", "SCN-TLS-0011": "interpreter",
                                "SCN-TLS-0012": "browser"}[identifier]),)
    if identifier == "SCN-TLS-0001":
        return (("claude-code", "gateway"),)
    if identifier == "SCN-TLS-0002":
        return (("codex", "gateway"),)
    if identifier in {"SCN-TLS-0020", "SCN-TLS-0021", "SCN-TLS-0022"}:
        return (("crew-pod", "gateway"),)
    if identifier == "SCN-TLS-0023":
        return (("engine-worker", "gateway"),)
    return (("cli", "gateway"),)


class FixtureUnavailable(Exception):
    pass


def fixture_for_cell(fixture, tool):
    if fixture.identifier == "SCN-TLS-0050":
        if tool not in TOOLS:
            raise ValueError("unknown lifecycle tool cell")
        return replace(fixture, actions=(tool, "confirm_stop"), max_provider_calls=1)
    if fixture.identifier == "SCN-TLS-0054" and tool in TOOLS[1:]:
        return replace(fixture, actions=(tool,) + fixture.actions[1:])
    return fixture


def fixture_summary(fixture):
    return {"fault_actions": list(fixture.actions), "max_provider_calls": fixture.max_provider_calls,
            "max_seconds": fixture.max_seconds, "scope": "run-owned-isolated"}


def canonical_uuid(value):
    identifier = str(UUID(str(value)))
    if identifier != value:
        raise ValueError("invalid run-owned operation identifier")
    return identifier


def observed_step(observation, request_id, max_provider_calls):
    operation_ids = [canonical_uuid(value) for value in observation.get("operation_ids", [])]
    artifact_hashes = observation.get("artifact_sha256", [])
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in artifact_hashes):
        raise ValueError("invalid artifact hash")
    provider_requests = []
    for item in observation.get("provider_request_observations", []):
        if item.get("request_id") != request_id or type(item.get("count")) is not int or not 0 <= item["count"] <= max_provider_calls:
            raise ValueError("unverifiable provider request observation")
        provider_requests.append({"request_id": request_id, "count": item["count"]})
    return operation_ids, artifact_hashes, provider_requests


def run_fixture(fixture, driver, *, clock=monotonic):
    """Execute only through an authorized isolated driver; always request owned cleanup."""
    if driver is None or not driver.isolated or not driver.authorized:
        raise FixtureUnavailable("isolated authorized provider fixture is unavailable")
    if not 0 < fixture.max_provider_calls <= 3 or not 0 < fixture.max_seconds <= 900:
        raise ValueError("unbounded lifecycle fixture")
    started = clock()
    request_id = str(uuid4())
    evidence = {"request_ids": [request_id], "execution_id": None, "operation_ids": [],
                "provider_request_observations": [], "artifact_sha256": [],
                "cleanup": {"status": "pending", "provider_termination": None}, "assertions": []}
    try:
        owned = driver.start_owned(fixture.identifier, max_seconds=fixture.max_seconds,
                                   max_provider_calls=fixture.max_provider_calls)
        evidence["execution_id"] = canonical_uuid(owned["execution_id"])
        evidence["run_id"] = canonical_uuid(owned["run_id"])
        for action in fixture.actions:
            if clock() - started > fixture.max_seconds:
                raise TimeoutError("lifecycle fixture deadline exceeded")
            observation = driver.step(action, request_id=request_id, execution_id=owned["execution_id"])
            operation_ids, artifact_hashes, provider_requests = observed_step(
                observation, request_id, fixture.max_provider_calls
            )
            evidence["operation_ids"].extend(operation_ids)
            evidence["provider_request_observations"].extend(provider_requests)
            evidence["artifact_sha256"].extend(artifact_hashes)
            for item in observation.get("assertions", []):
                if item.get("action") != action or type(item.get("passed")) is not bool:
                    raise ValueError("invalid fault assertion")
                evidence["assertions"].append({"action": action, "passed": item["passed"]})
        evidence["status"] = "pending-verification"
    except (FixtureUnavailable, ValueError, TimeoutError, OSError) as exc:
        evidence["status"] = "blocked"
        evidence["reason"] = type(exc).__name__
    finally:
        try:
            cleanup = driver.stop_owned(evidence.get("execution_id"),
                                        timeout=min(60, fixture.max_seconds))
            evidence["cleanup"] = {"status": cleanup.get("status", "unknown"),
                                   "provider_termination": cleanup.get("provider_termination")}
        except (FixtureUnavailable, ValueError, TimeoutError, OSError) as exc:
            evidence["cleanup"] = {"status": "unknown", "provider_termination": None,
                                   "reason": type(exc).__name__}
    return evidence


def lifecycle_verdict(fixture, evidence, *, observer=None, live=False):
    """Only independent provider observations can complete an executed fault."""
    cleanup = evidence.get("cleanup", {})
    if cleanup.get("status") == "failed":
        return "fail", "provider cleanup failed"
    if evidence.get("status") != "pending-verification":
        return "blocked", "fixture outcome cannot be verified"
    if cleanup.get("status") != "confirmed" or not cleanup.get("provider_termination"):
        return "blocked", "owned provider stop or expiry remains unconfirmed"
    if not live or observer is None:
        return "blocked", "independent provider observation unavailable"
    try:
        result = observer.observe(execution_id=canonical_uuid(evidence["execution_id"]),
                                  run_id=canonical_uuid(evidence["run_id"]),
                                  request_ids=tuple(evidence["request_ids"]),
                                  timeout=min(60, fixture.max_seconds))
    except (FixtureUnavailable, ValueError, TimeoutError, OSError, KeyError):
        return "blocked", "independent provider observation unavailable"
    if result.get("source") != "provider-control-plane" or result.get("execution_id") != evidence["execution_id"] or (
        result.get("run_id") != evidence["run_id"]
    ):
        return "blocked", "provider evidence is not bound to the owned execution"
    if result.get("status") == "failed" or result.get("termination") == "failed":
        return "fail", "provider reported a confirmed failure"
    count = result.get("provider_request_count")
    if type(count) is not int or count < 0:
        return "blocked", "provider request count is missing"
    if count > fixture.max_provider_calls:
        return "fail", "provider exceeded the authorized call bound"
    if set(result.get("request_ids", [])) != set(evidence["request_ids"]):
        return "blocked", "provider requests do not match the owned operation"
    if result.get("termination") not in ("stopped", "expired") or not result.get("termination_observed_at"):
        return "blocked", "provider stop or expiry is not independently confirmed"
    if not evidence.get("operation_ids") or not evidence.get("provider_request_observations"):
        return "blocked", "operation or provider request evidence missing"
    assertions = evidence.get("assertions", [])
    if {item.get("action") for item in assertions if item.get("passed") is True} != set(fixture.actions):
        return "blocked", "required fault assertions are incomplete"
    if not set(evidence.get("artifact_sha256", [])) <= set(result.get("artifact_sha256", [])):
        return "blocked", "artifact hashes lack independent corroboration"
    return "pass", "provider and cleanup independently confirmed"
