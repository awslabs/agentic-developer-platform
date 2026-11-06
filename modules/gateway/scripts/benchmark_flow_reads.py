"""Bounded, non-certifying API sampling for Delivery Flow reads."""

import asyncio
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

import httpx

API_SAMPLES = 100
REQUEST_TIMEOUT_SECONDS = 15
API_P95_TARGET_SECONDS = 2


@dataclass(frozen=True)
class ApiExpectation:
    list_flow_ids: tuple[str, ...]
    graph_flow_id: str
    graph_node_ids: tuple[str, ...]
    graph_edges: tuple[tuple[str, str], ...] = ()
    list_query: str = "fixture-flow"


@dataclass(frozen=True)
class ApiAttempt:
    operation: str
    reader: int
    sample: int
    elapsed_seconds: float | None
    classification: str
    status: int | None
    success: bool
    rows_returned: int | None
    sql_count: int | None = None


def _content_error(operation: str, body: Any, expected: ApiExpectation) -> tuple[str | None, int | None]:
    if not isinstance(body, dict):
        return "missing_content", None
    if operation == "list":
        rows = body.get("flows")
        if not isinstance(rows, list):
            return "missing_content", None
        identities = [row.get("id") for row in rows if isinstance(row, dict)]
        if len(identities) != len(rows) or set(identities) != set(expected.list_flow_ids) or len(rows) != len(expected.list_flow_ids):
            return "content_violation", len(rows)
        return None, len(rows)
    nodes, edges = body.get("nodes"), body.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return "missing_content", None
    node_ids = [node.get("id") for node in nodes if isinstance(node, dict)]
    edge_pairs = [(edge.get("from_node_id"), edge.get("to_node_id")) for edge in edges if isinstance(edge, dict)]
    if (
        body.get("flow_id") != expected.graph_flow_id
        or body.get("activity_included") is not False
        or len(node_ids) != len(nodes)
        or len(edge_pairs) != len(edges)
        or set(node_ids) != set(expected.graph_node_ids)
        or len(nodes) != len(expected.graph_node_ids)
        or set(edge_pairs) != set(expected.graph_edges)
        or len(edges) != len(expected.graph_edges)
    ):
        return "content_violation", len(nodes)
    return None, len(nodes)


async def sample_api(
    client: httpx.AsyncClient, expected: ApiExpectation, *, operation: str, reader: int, sample: int, clock=time.monotonic
) -> ApiAttempt:
    started = clock()
    status = None
    rows = None
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            if operation == "list":
                response = await client.get(
                    "/orchestration/flows",
                    params={"q": expected.list_query, "sort": "created", "limit": 25, "offset": 0},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            else:
                response = await client.get(
                    f"/orchestration/flows/{expected.graph_flow_id}",
                    params={"include_activity": "false"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
        status = response.status_code
        if status in (401, 403):
            classification = "auth_violation"
        elif status != 200:
            classification = "http_failure"
        else:
            try:
                classification, rows = _content_error(operation, response.json(), expected)
            except (ValueError, TypeError):
                classification = "missing_content"
            classification = classification or "ok"
    except (TimeoutError, httpx.TimeoutException):
        classification = "timeout"
    except httpx.RequestError:
        classification = "request_failure"
    return ApiAttempt(operation, reader, sample, clock() - started, classification, status, classification == "ok", rows)


def nearest_rank(values: list[float]) -> float:
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def evaluate_api(attempts: list[ApiAttempt], *, readers: int) -> dict[str, Any]:
    if readers not in (1, 5):
        raise ValueError("reader mode must be one or five")
    operations = {}
    for operation in ("list", "graph"):
        samples = [attempt for attempt in attempts if attempt.operation == operation]
        expected_keys = {(reader, sequence) for reader in range(readers) for sequence in range(reader + 1, API_SAMPLES + 1, readers)}
        observed_keys = {(attempt.reader, attempt.sample) for attempt in samples}
        complete = len(samples) == API_SAMPLES and observed_keys == expected_keys and all(attempt.elapsed_seconds is not None for attempt in samples)
        valid = complete and all(attempt.classification != "aborted" for attempt in samples)
        successful = [attempt.elapsed_seconds for attempt in samples if attempt.success and attempt.elapsed_seconds is not None]
        gate_values = [attempt.elapsed_seconds if attempt.success else math.inf for attempt in samples]
        p95 = nearest_rank(gate_values) if valid else math.inf
        violations = sum(attempt.classification in ("content_violation", "missing_content", "auth_violation") for attempt in samples)
        operations[operation] = {
            "planned": API_SAMPLES,
            "observed": len(samples),
            "complete": complete,
            "successes": len(successful),
            "success_rate": len(successful) / API_SAMPLES,
            "gate_p95_seconds": None if math.isinf(p95) else p95,
            "gate_p95_infinite": math.isinf(p95),
            "successful_only_p95_seconds": nearest_rank(successful) if successful else None,
            "violations": violations,
            "passed": valid and len(successful) >= 99 and violations == 0 and p95 <= API_P95_TARGET_SECONDS,
        }
    return {
        "mode": f"{readers}-reader",
        "phase": "api",
        "operations": operations,
        "passed": all(operation["passed"] for operation in operations.values()),
        "attempts": [asdict(attempt) for attempt in attempts],
    }


def evaluate_browser(attempts: list[dict[str, Any]], *, readers: int) -> dict[str, Any]:
    if readers not in (1, 5):
        raise ValueError("reader mode must be one or five")

    def valid_duration(attempt: dict[str, Any]) -> bool:
        duration = attempt.get("meaningful_seconds")
        return isinstance(duration, int | float) and not isinstance(duration, bool) and math.isfinite(duration) and duration >= 0

    operations = {}
    for operation in ("list", "graph"):
        samples = [attempt for attempt in attempts if attempt.get("operation") == operation]
        expected_keys = {(reader, sample) for reader in range(readers) for sample in range(reader + 1, 21, readers)}
        observed_keys = {(attempt.get("reader"), attempt.get("sample")) for attempt in samples}
        complete = len(samples) == 20 and observed_keys == expected_keys and all(map(valid_duration, samples))
        success = [attempt for attempt in samples if attempt.get("classification") == "ok" and valid_duration(attempt)]
        latencies = [attempt["meaningful_seconds"] if attempt in success else math.inf for attempt in samples]
        p95 = nearest_rank(latencies) if complete else math.inf
        violations = sum(attempt.get("classification") in ("content_violation", "auth_violation", "missing_content") for attempt in samples)
        operations[operation] = {
            "planned": 20,
            "observed": len(samples),
            "complete": complete,
            "successes": len(success),
            "success_rate": len(success) / 20,
            "gate_p95_seconds": None if math.isinf(p95) else p95,
            "gate_p95_infinite": math.isinf(p95),
            "successful_only_p95_seconds": nearest_rank([attempt["meaningful_seconds"] for attempt in success]) if success else None,
            "violations": violations,
            "passed": complete and len(success) == 20 and violations == 0 and p95 <= 3,
        }
    return {
        "mode": f"{readers}-reader",
        "phase": "browser",
        "operations": operations,
        "passed": all(operation["passed"] for operation in operations.values()),
        "attempts": attempts,
    }


async def collect_api(client: httpx.AsyncClient, expected: ApiExpectation, *, readers: int, reserve=None, record=None) -> dict[str, Any]:
    if readers not in (1, 5):
        raise ValueError("reader mode must be one or five")
    started = asyncio.Event()

    async def read(reader: int) -> list[ApiAttempt]:
        await started.wait()
        result = []
        for sample in range(reader + 1, API_SAMPLES + 1, readers):
            for operation in ("list", "graph"):
                if reserve is not None:
                    reserve()
                attempt = await sample_api(client, expected, operation=operation, reader=reader, sample=sample)
                result.append(attempt)
                if record is not None:
                    record(attempt)
        return result

    tasks = [asyncio.create_task(read(reader)) for reader in range(readers)]
    started.set()
    try:
        attempts = [attempt for reader_attempts in await asyncio.gather(*tasks) for attempt in reader_attempts]
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return evaluate_api(attempts, readers=readers)
