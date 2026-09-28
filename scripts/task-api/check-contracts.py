#!/usr/bin/env python3
"""Conformance baseline for the ADP Task API v1 contract.

Runs with no installation step on a stock Python 3.11+ interpreter, because an
evaluator should be able to clone the repository at a revision and get a verdict
from one command.

What it checks, in order:

  1. Every schema document loads, every $ref resolves, and no schema uses a
     keyword this validator does not implement.
  2. Every fixture marked expect=valid validates against its named schema.
  3. Every fixture marked expect=invalid is *rejected* by its named schema, with
     the reason recorded. A fixture that quietly starts passing is a failure --
     the rejections are how the contract binds implementations, so a rejection
     that stops rejecting is a silent loss of a guarantee.
  4. Cross-component identity and state rules that no single schema can express:
     cursor/sequence agreement, generation agreement between an envelope field
     and its authority key, message_id equalling invocation_id, result outcome
     agreeing with task status, and lifecycle transition legality.
  5. The traces: submit-to-result, reconnect/replay, cancellation and the
     nine-row crash matrix, checking that every fixture and invariant they
     reference actually exists and that their ordering is a legal walk of the
     lifecycle.

Exit status is 0 only if every check passes.

Design revision b5761a4a2502aceaa9133afef552b567a19cb46e.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _schema import Registry, SchemaError, validate

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = REPO_ROOT / "docs" / "task-api" / "contracts" / "v1"
SCHEMA_DIR = CONTRACT_DIR / "schemas"
FIXTURE_DIR = CONTRACT_DIR / "fixtures"
TRACE_DIR = CONTRACT_DIR / "traces"

# Derived from identity-and-lifecycle.json rather than hardcoded, so the
# checker cannot drift from the contract it is checking.
TERMINAL_STATUSES: set[str] = set()


class Results:
    """Accumulates check outcomes and the criteria each one exercises."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def record(
        self,
        name: str,
        passed: bool,
        detail: str = "",
        criteria: list[str] | None = None,
        group: str = "",
    ) -> None:
        self.checks.append(
            {
                "check": name,
                "group": group,
                "passed": passed,
                "detail": detail,
                "criteria": criteria or [],
            }
        )

    @property
    def failures(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["passed"]]

    def criteria_covered(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for check in self.checks:
            for criterion in check["criteria"]:
                counts[criterion] = counts.get(criterion, 0) + 1
        return dict(sorted(counts.items()))


def load_fixtures() -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for path in sorted(FIXTURE_DIR.rglob("*.json")):
        out.append((path, json.loads(path.read_text())))
    return out


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def split_pointer(pointer: str) -> tuple[str, str]:
    doc, _, frag = pointer.partition("#")
    return doc, frag


def decode_request_json(payload: bytes) -> Any:
    """Decode request JSON with the design's parser-level rejection rules."""

    def reject_constant(value: str) -> None:
        raise ValueError(f"nonfinite number {value!r} is not valid request JSON")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object field {key!r}")
            result[key] = value
        return result

    text = payload.decode("utf-8", errors="strict")
    return json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=reject_duplicate_keys,
    )


def materialize_fixture(instance: dict[str, Any], specifications: list[Any]) -> None:
    """Expand deterministic large values without storing megabyte-scale fixtures."""
    for specification in specifications:
        pointer = specification.get("json_pointer", "")
        value = specification.get("value")
        count = specification.get("count")
        if (
            not pointer.startswith("/")
            or not isinstance(value, str)
            or not isinstance(count, int)
            or count < 1
        ):
            raise ValueError(f"invalid materialization specification {specification!r}")
        tokens = [
            token.replace("~1", "/").replace("~0", "~")
            for token in pointer[1:].split("/")
        ]
        target: Any = instance
        for token in tokens[:-1]:
            if not isinstance(target, dict) or token not in target:
                raise ValueError(
                    f"materialization pointer {pointer!r} does not resolve"
                )
            target = target[token]
        if not isinstance(target, dict) or tokens[-1] not in target:
            raise ValueError(f"materialization pointer {pointer!r} does not resolve")
        target[tokens[-1]] = value * count


def check_schemas(registry: Registry, results: Results) -> None:
    unsupported = registry.check_keywords()
    results.record(
        "schemas use only implemented keywords",
        not unsupported,
        "; ".join(unsupported[:5]),
        ["T0-AC02"],
        group="schemas",
    )

    broken: list[str] = []
    for name, doc in registry.docs.items():
        stack: list[Any] = [doc]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "$ref" in node and isinstance(node["$ref"], str):
                    try:
                        registry.resolve(node["$ref"], name)
                    except SchemaError as exc:
                        broken.append(f"{name}: {exc}")
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    results.record(
        "every $ref resolves",
        not broken,
        "; ".join(broken[:5]),
        ["T0-AC02"],
        group="schemas",
    )


def check_fixtures(registry: Registry, fixtures, results: Results) -> None:
    for path, fixture in fixtures:
        meta = fixture.get("$fixture")
        if meta is None:
            results.record(
                f"{rel(path)}: has a $fixture block",
                False,
                "every fixture must declare its schema, expectation and owner",
                ["T0-AC02"],
                group="fixtures",
            )
            continue

        missing = [
            k
            for k in ("description", "schema", "expect", "owner", "criteria")
            if k not in meta
        ]
        if missing:
            results.record(
                f"{rel(path)}: $fixture block is complete",
                False,
                f"missing {missing}",
                ["T0-AC02"],
                group="fixtures",
            )
            continue

        expect = meta["expect"]
        criteria = list(meta["criteria"])
        instance = copy.deepcopy({k: v for k, v in fixture.items() if k != "$fixture"})
        doc, frag = split_pointer(meta["schema"])
        validation_stage = meta.get("validation_stage", "schema")

        if expect == "invalid" and "reason" not in meta:
            results.record(
                f"{rel(path)}: rejection states a reason",
                False,
                "an invalid fixture without a stated reason cannot be reviewed",
                criteria,
                group="fixtures",
            )
            continue

        try:
            schema, schema_doc = registry.resolve(f"{doc}#{frag}", doc)
        except SchemaError as exc:
            results.record(
                f"{rel(path)}: schema pointer resolves",
                False,
                str(exc),
                criteria,
                group="fixtures",
            )
            continue

        if validation_stage == "schema_mutations":
            cases = fixture.get("cases")
            if isinstance(cases, list) and cases:
                for case in cases:
                    if not isinstance(case, dict):
                        results.record(
                            f"{rel(path)}: malformed mutation case",
                            False,
                            f"expected object, got {type(case).__name__}",
                            criteria,
                            group="fixtures",
                        )
                        continue
                    case_name = case.get("name", "unnamed mutation")
                    base_fixture = case.get("base_fixture")
                    pointer = case.get("json_pointer")
                    operation = case.get("operation")
                    problems: list[str] = []
                    base: dict[str, Any] = {}
                    if not isinstance(base_fixture, str):
                        problems.append("base_fixture is missing")
                    else:
                        base_path = CONTRACT_DIR / base_fixture
                        if not base_path.is_file():
                            problems.append(
                                f"base fixture does not exist: {base_fixture}"
                            )
                        else:
                            base = json.loads(base_path.read_text())
                    base_meta = base.get("$fixture", {})
                    schema_pointer = case.get("schema", base_meta.get("schema"))
                    mutated = copy.deepcopy(
                        {key: value for key, value in base.items() if key != "$fixture"}
                    )
                    if not isinstance(pointer, str) or not pointer.startswith("/"):
                        problems.append("json_pointer must be an absolute pointer")
                    else:
                        tokens = [
                            token.replace("~1", "/").replace("~0", "~")
                            for token in pointer[1:].split("/")
                        ]
                        target: Any = mutated
                        for token in tokens[:-1]:
                            if not isinstance(target, dict) or token not in target:
                                problems.append(f"pointer does not resolve: {pointer}")
                                break
                            target = target[token]
                        field = tokens[-1] if tokens else ""
                        if not problems and (
                            not isinstance(target, dict) or field not in target
                        ):
                            problems.append(f"pointer does not resolve: {pointer}")
                        elif not problems and operation == "remove":
                            del target[field]
                        elif not problems and operation == "replace":
                            target[field] = case.get("value")
                        elif not problems:
                            problems.append(f"unsupported operation: {operation!r}")
                    baseline_errors: list[str] = []
                    mutation_errors: list[str] = []
                    if not isinstance(schema_pointer, str):
                        problems.append("schema pointer is missing")
                    elif not problems:
                        try:
                            case_schema, case_doc = registry.resolve(
                                schema_pointer, split_pointer(schema_pointer)[0]
                            )
                            baseline = {
                                key: value
                                for key, value in base.items()
                                if key != "$fixture"
                            }
                            baseline_errors = validate(
                                baseline, case_schema, registry, case_doc
                            )
                            mutation_errors = validate(
                                mutated, case_schema, registry, case_doc
                            )
                        except SchemaError as exc:
                            problems.append(str(exc))
                    passed = (
                        expect == "invalid"
                        and not problems
                        and not baseline_errors
                        and bool(mutation_errors)
                    )
                    detail = "; ".join(problems)
                    if baseline_errors:
                        detail = f"base fixture is invalid: {baseline_errors[:2]}"
                    elif not mutation_errors and not problems:
                        detail = "mutated fixture unexpectedly validated"
                    results.record(
                        f"{rel(path)}: {case_name}",
                        passed,
                        detail,
                        criteria,
                        group="fixtures",
                    )
                continue

            base_fixture = meta.get("base_fixture")
            mutations = fixture.get("mutations")
            well_formed = (
                expect == "invalid"
                and isinstance(base_fixture, str)
                and (CONTRACT_DIR / base_fixture).is_file()
                and isinstance(mutations, list)
                and bool(mutations)
            )
            results.record(
                f"{rel(path)}: declares a reusable schema mutation plan",
                well_formed,
                "executed by the internal-adapter checks"
                if well_formed
                else "requires an existing base_fixture and a non-empty mutations list",
                criteria,
                group="fixtures",
            )
            continue

        if validation_stage == "json_decode":
            raw_instance = fixture.get("raw_instance")
            raw_instance_base64 = fixture.get("raw_instance_base64")
            if (raw_instance is None) == (raw_instance_base64 is None):
                results.record(
                    f"{rel(path)}: declares one raw request representation",
                    False,
                    "json_decode fixtures require exactly one of raw_instance or raw_instance_base64",
                    criteria,
                    group="fixtures",
                )
                continue
            try:
                payload = (
                    raw_instance.encode("utf-8")
                    if raw_instance is not None
                    else base64.b64decode(raw_instance_base64, validate=True)
                )
                decoded = decode_request_json(payload)
                decode_error = ""
            except (UnicodeDecodeError, ValueError) as exc:
                decoded = None
                decode_error = str(exc)
            passed = expect == "invalid" and bool(decode_error)
            results.record(
                f"{rel(path)}: is rejected during strict JSON decoding ({meta['reason']})",
                passed,
                decode_error
                if decode_error
                else f"STRICT JSON DECODER ACCEPTED {decoded!r}",
                criteria,
                group="fixtures",
            )
            continue

        try:
            errors = validate(instance, schema, registry, schema_doc)
        except SchemaError as exc:
            results.record(
                f"{rel(path)}: validation ran",
                False,
                f"schema error: {exc}",
                criteria,
                group="fixtures",
            )
            continue

        if validation_stage == "encoded_size":
            try:
                materialize_fixture(instance, meta.get("materialize", []))
            except ValueError as exc:
                results.record(
                    f"{rel(path)}: materializes its encoded request",
                    False,
                    str(exc),
                    criteria,
                    group="fixtures",
                )
                continue
            errors = validate(instance, schema, registry, schema_doc)
            max_body_bytes = json.loads((CONTRACT_DIR / "limits.json").read_text())[
                "submit_body"
            ]["max_body_bytes"]
            encoded_bytes = len(
                json.dumps(
                    instance,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            passed = (
                expect == "invalid" and not errors and encoded_bytes > max_body_bytes
            )
            results.record(
                f"{rel(path)}: exceeds the encoded request limit without another schema violation",
                passed,
                f"encoded body is {encoded_bytes} bytes; limit is {max_body_bytes}; schema errors: {errors[:2]}",
                criteria,
                group="fixtures",
            )
            continue

        if validation_stage != "schema":
            results.record(
                f"{rel(path)}: uses a supported validation stage",
                False,
                f"unknown validation_stage {validation_stage!r}",
                criteria,
                group="fixtures",
            )
            continue

        if expect == "valid":
            results.record(
                f"{rel(path)}: validates",
                not errors,
                "; ".join(errors[:4]),
                criteria,
                group="fixtures",
            )
        elif expect == "invalid":
            if meta.get("checked_by") == "cross_component":
                # Rejected by a cross-component rule below, not by the schema.
                results.record(
                    f"{rel(path)}: deferred to cross-component checks",
                    not errors,
                    meta["reason"]
                    if not errors
                    else f"schema unexpectedly rejected cross-component fixture: {errors[0]}",
                    criteria,
                    group="fixtures",
                )
            else:
                reason = meta["reason"]
                unrelated_structure_errors: list[str] = []
                for error in errors:
                    match = re.search(
                        r"(?:missing required|additional) property '([^']+)'", error
                    )
                    if match and match.group(1) not in reason:
                        unrelated_structure_errors.append(error)
                results.record(
                    f"{rel(path)}: is rejected ({reason})",
                    bool(errors) and not unrelated_structure_errors,
                    "SCHEMA ACCEPTED A FIXTURE THAT MUST BE REJECTED"
                    if not errors
                    else "unrelated structural rejection: "
                    + "; ".join(unrelated_structure_errors[:3])
                    if unrelated_structure_errors
                    else f"rejected: {errors[0]}",
                    criteria,
                    group="fixtures",
                )
        else:
            results.record(
                f"{rel(path)}: expect is valid or invalid",
                False,
                f"unknown expectation {expect!r}",
                criteria,
                group="fixtures",
            )


def cursor_parts(cursor: str) -> tuple[str, str]:
    task, _, seq = cursor.rpartition(":")
    return task, seq


def transition_pairs(lifecycle: dict[str, Any]) -> set[tuple[str, str]]:
    """Flatten the from -> permitted mapping into explicit (from, to) pairs."""
    pairs = set()
    for source, spec in lifecycle["lifecycle"]["transitions"].items():
        for target in spec["permitted"]:
            pairs.add((source, target))
    return pairs


def check_cross_component(
    fixtures, lifecycle: dict[str, Any], results: Results
) -> None:
    """Rules that span components, which no single schema can express."""
    for path, fixture in fixtures:
        meta = fixture.get("$fixture", {})
        expect = meta.get("expect")
        criteria = list(meta.get("criteria", []))
        name = rel(path)
        violations: list[str] = []

        # LC-09: an event cursor must name its own task and its own sequence.
        event_cursor = fixture.get("event_id", fixture.get("event_cursor"))
        if event_cursor is not None and "sequence" in fixture:
            task, seq = cursor_parts(event_cursor)
            if task != fixture.get("task_id"):
                violations.append("event ID task prefix does not equal task_id")
            if seq != str(fixture["sequence"]):
                violations.append("event ID sequence does not equal sequence")

        # The envelope's generation must agree with its authority key.
        ref = fixture.get("assignment_ref")
        if isinstance(ref, dict) and "grant_sk" in ref and "generation" in ref:
            tail = ref["grant_sk"].rsplit("#GEN#", 1)
            if (
                len(tail) == 2
                and tail[1].isdigit()
                and int(tail[1]) != ref["generation"]
            ):
                violations.append(
                    "assignment_ref.generation disagrees with grant_sk GEN segment"
                )
            # Design section 6: the grant key is TASK_RUN#<invocation_id>#GEN#<n>.
            # It is keyed on the invocation, not the task, because one task may
            # have several bounded run attempts and each needs its own grant.
            run = ref["grant_sk"].split("#")
            if (
                len(run) > 1
                and fixture.get("invocation_id")
                and run[1] != fixture["invocation_id"]
            ):
                violations.append("grant_sk run segment does not equal invocation_id")

        # message_id is the invocation, never a transport identifier.
        if (
            "message_id" in fixture
            and "invocation_id" in fixture
            and fixture["message_id"] != fixture["invocation_id"]
        ):
            violations.append("message_id does not equal invocation_id")

        # FIFO deduplication keys on the dispatch, not the invocation.
        if "MessageDeduplicationId" in fixture:
            envelope = next(
                (f for p, f in fixtures if p.name == "envelope-dispatch.json"), {}
            )
            dedup = fixture["MessageDeduplicationId"]
            if expect == "valid" and dedup != envelope.get("dispatch_id"):
                violations.append(
                    "MessageDeduplicationId does not equal the paired envelope dispatch_id"
                )
            if dedup == envelope.get("invocation_id"):
                violations.append(
                    "MessageDeduplicationId is the invocation_id; recovery republish would be swallowed"
                )

        # A result may only appear on a terminal status, and must agree with it.
        if "status" in fixture and isinstance(fixture.get("result"), dict):
            status = fixture["status"]
            outcome = fixture["result"].get("outcome")
            if status not in TERMINAL_STATUSES:
                violations.append(
                    f"non-terminal status {status!r} carries a committed result"
                )
            elif outcome is not None and outcome != status:
                violations.append(
                    f"status {status!r} disagrees with result outcome {outcome!r}"
                )

        # LC-04: a lost heartbeat means health is unknown, not healthy.
        context = meta.get("cross_component_context", {})
        if (
            fixture.get("heartbeat_lost") is True
            or context.get("heartbeat_lost") is True
        ) and fixture.get("execution_health") not in (
            None,
            "unknown",
        ):
            violations.append(
                "heartbeat_lost with a known execution_health; LC-04 requires unknown"
            )

        # LC-08: unknown usage is never a number.
        if (
            fixture.get("handoff") == "unknown"
            or fixture.get("provider_outcome") == "unknown"
        ):
            if fixture.get("usage") not in (None,):
                violations.append("unknown handoff reports usage")
            if fixture.get("total_usd") is not None and "total_usd" in fixture:
                violations.append("unknown provider outcome reports a known total_usd")

        if expect == "valid":
            results.record(
                f"{name}: cross-component identity and state rules hold",
                not violations,
                "; ".join(violations),
                criteria,
                group="cross_component",
            )
        elif expect == "invalid" and meta.get("checked_by") == "cross_component":
            results.record(
                f"{name}: rejected by cross-component rule ({meta.get('reason', '')})",
                bool(violations),
                "NO CROSS-COMPONENT RULE CAUGHT THIS FIXTURE"
                if not violations
                else "; ".join(violations),
                criteria,
                group="cross_component",
            )

    # Every declared transition must use declared states.
    states = set(lifecycle["lifecycle"]["states"])
    bad: list[str] = []
    for source, target in sorted(transition_pairs(lifecycle)):
        if source not in states:
            bad.append(f"unknown from-state {source!r}")
        if target not in states:
            bad.append(f"unknown to-state {target!r}")
    results.record(
        "lifecycle transitions reference declared states only",
        not bad,
        "; ".join(bad[:5]),
        ["T0-AC02", "V0-04"],
        group="cross_component",
    )

    # No transition may leave a terminal state.
    leaks = [
        f"{source} -> {target}"
        for source, target in sorted(transition_pairs(lifecycle))
        if source in TERMINAL_STATUSES
    ]
    results.record(
        "terminal states have no outgoing transitions",
        not leaks,
        "; ".join(leaks),
        ["T0-AC03", "V0-04"],
        group="cross_component",
    )


MISSING = object()


def json_pointer_value(doc: Any, pointer: str) -> Any:
    node = doc
    for token in [t for t in pointer.split("/") if t]:
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or token not in node:
            return MISSING
        node = node[token]
    return node


def resolve_json_pointer(doc: Any, pointer: str) -> bool:
    return json_pointer_value(doc, pointer) is not MISSING


def check_citations(fixtures, results: Results) -> None:
    """Every contract pointer a fixture cites must actually resolve.

    A fixture citing a limit that does not exist looks authoritative and proves
    nothing. This check found three such citations when it was first written.
    """
    docs = {
        name: json.loads((CONTRACT_DIR / name).read_text())
        for name in ("limits.json", "decisions.json", "identity-and-lifecycle.json")
    }
    decision_ids = {d["id"] for d in docs["decisions.json"]["decisions"]}
    invariant_ids = {
        i["id"] for i in docs["identity-and-lifecycle.json"]["lifecycle"]["invariants"]
    }

    for path, fixture in fixtures:
        meta = fixture.get("$fixture", {})
        cited = list(meta.get("violates", []))
        limit_name = (
            fixture.get("details", {}).get("limit_name")
            if isinstance(fixture.get("details"), dict)
            else None
        )
        if limit_name:
            cited.append(f"limits.json#/{limit_name.replace('.', '/')}")
        if not cited:
            continue
        dangling: list[str] = []
        for ref in cited:
            doc_name, _, frag = ref.partition("#")
            if doc_name.endswith(".schema.json"):
                continue  # covered by the schema $ref check
            if doc_name == "decisions.json":
                key = frag.strip("/")
                if key not in decision_ids:
                    dangling.append(ref)
                continue
            if doc_name not in docs:
                dangling.append(ref)
                continue
            if frag.startswith("/lifecycle/invariants/"):
                if frag.rsplit("/", 1)[-1] not in invariant_ids:
                    dangling.append(ref)
                continue
            if not resolve_json_pointer(docs[doc_name], frag):
                dangling.append(ref)
        results.record(
            f"{rel(path)}: cited contract pointers resolve",
            not dangling,
            f"dangling: {dangling}",
            list(meta.get("criteria", [])),
            group="citations",
        )


def check_traces(fixtures, lifecycle: dict[str, Any], results: Results) -> None:
    fixture_paths = {rel(p) for p, _ in fixtures}
    fixture_by_path = {
        rel(p).removeprefix("docs/task-api/contracts/v1/"): f for p, f in fixtures
    }
    allowed = transition_pairs(lifecycle)
    allowed_actions = {entry["route"] for entry in lifecycle["public_routes"]}
    allowed_actions.update(
        f"POST {entry['route']}" for entry in lifecycle["internal_routes"]
    )
    event_kinds = set(lifecycle["event_kinds"])
    required = {
        "submit-to-result.json",
        "reconnect-and-replay.json",
        "cancellation.json",
        "crash-matrix.json",
    }
    present = {p.name for p in TRACE_DIR.glob("*.json")}
    results.record(
        "all required traces are present",
        required <= present,
        f"missing {sorted(required - present)}",
        ["T0-AC02"],
        group="traces",
    )

    for path in sorted(TRACE_DIR.glob("*.json")):
        trace = json.loads(path.read_text())
        meta = trace.get("$trace", {})
        criteria = list(meta.get("criteria", []))
        name = rel(path)

        # Every fixture-bearing field must resolve, including newly added trace fields.
        missing: list[str] = []
        for entry in trace.get("steps", []) + trace.get("rows", []):
            for key, value in entry.items():
                if not (key.endswith("fixture") or key == "emits_event"):
                    continue
                for candidate in str(value).split(", "):
                    if not candidate:
                        continue
                    target = f"docs/task-api/contracts/v1/{candidate}"
                    if target not in fixture_paths:
                        missing.append(
                            f"step {entry.get('step', entry.get('id'))}: {candidate}"
                        )
            for candidate in str(entry.get("evidence", "")).split(", "):
                candidate = candidate.strip()
                if candidate.startswith("fixtures/"):
                    target = f"docs/task-api/contracts/v1/{candidate}"
                    if target not in fixture_paths:
                        missing.append(f"{entry.get('id')}: {candidate}")
        results.record(
            f"{name}: every referenced fixture exists",
            not missing,
            "; ".join(missing[:5]),
            criteria,
            group="traces",
        )

        # Step status progression must be a legal lifecycle walk.
        if "steps" in trace:
            illegal: list[str] = []
            previous: str | None = None
            for entry in trace["steps"]:
                status = entry.get("task_status_after")
                if status is None:
                    continue
                if (
                    previous is not None
                    and status != previous
                    and (previous, status) not in allowed
                ):
                    illegal.append(
                        f"{previous} -> {status} at step {entry.get('step')}"
                    )
                if entry.get("branch") is None:
                    previous = status
            results.record(
                f"{name}: status progression is a legal lifecycle walk",
                not illegal,
                "; ".join(illegal),
                criteria,
                group="traces",
            )

            ordered = [
                e.get("step") for e in trace["steps"] if isinstance(e.get("step"), int)
            ]
            results.record(
                f"{name}: steps are ordered",
                ordered == sorted(ordered),
                f"step numbers out of order: {ordered}",
                criteria,
                group="traces",
            )

            route_errors: list[str] = []
            response_state_errors: list[str] = []
            event_errors: list[str] = []
            trace_task_ids: set[str] = set()
            trace_invocation_ids: set[str] = set()
            for entry in trace["steps"]:
                action = entry.get("action", "")
                for key, fixture_name in entry.items():
                    if not (key.endswith("fixture") or key == "emits_event"):
                        continue
                    fixture = fixture_by_path.get(fixture_name, {})
                    if isinstance(fixture.get("task_id"), str):
                        trace_task_ids.add(fixture["task_id"])
                    if isinstance(fixture.get("invocation_id"), str):
                        trace_invocation_ids.add(fixture["invocation_id"])
                route_match = re.search(r"\b(GET|POST) (/[^ ]+)", action)
                if route_match:
                    routed_action = f"{route_match.group(1)} {route_match.group(2)}"
                    if routed_action not in allowed_actions:
                        route_errors.append(
                            f"step {entry.get('step')}: unknown route {routed_action}"
                        )

                status_after = entry.get("task_status_after")
                for key in ("response_fixture", "snapshot_fixture"):
                    fixture_name = entry.get(key)
                    response = fixture_by_path.get(fixture_name, {})
                    response_status = response.get("status")
                    if (
                        status_after is not None
                        and response_status is not None
                        and response_status != status_after
                    ):
                        response_state_errors.append(
                            f"step {entry.get('step')}: {key} status {response_status} != {status_after}"
                        )

                expected_event_type = entry.get("expected_event_type")
                if expected_event_type and expected_event_type not in event_kinds:
                    event_errors.append(
                        f"step {entry.get('step')}: unknown event type {expected_event_type}"
                    )
                emitted = fixture_by_path.get(entry.get("emits_event"), {})
                if expected_event_type and emitted.get("type") != expected_event_type:
                    event_errors.append(
                        f"step {entry.get('step')}: emitted {emitted.get('type')!r}, expected {expected_event_type!r}"
                    )
                expected_sequence = entry.get("event_sequence")
                if (
                    expected_sequence is not None
                    and emitted.get("sequence") != expected_sequence
                ):
                    event_errors.append(
                        f"step {entry.get('step')}: emitted sequence {emitted.get('sequence')!r}, expected {expected_sequence!r}"
                    )

            results.record(
                f"{name}: action routes are declared contract routes",
                not route_errors,
                "; ".join(route_errors),
                criteria,
                group="traces",
            )
            results.record(
                f"{name}: response fixtures agree with trace task states",
                not response_state_errors,
                "; ".join(response_state_errors),
                criteria,
                group="traces",
            )
            results.record(
                f"{name}: event types and sequences agree with event fixtures",
                not event_errors,
                "; ".join(event_errors),
                criteria,
                group="traces",
            )
            if path.name == "submit-to-result.json":
                results.record(
                    f"{name}: submit retry preserves task and invocation identity",
                    len(trace_task_ids) == 1 and len(trace_invocation_ids) == 1,
                    f"task_ids {sorted(trace_task_ids)}; invocation_ids {sorted(trace_invocation_ids)}",
                    criteria,
                    group="traces",
                )

    # The crash matrix must match the design's own row count.
    matrix = json.loads((TRACE_DIR / "crash-matrix.json").read_text())
    declared = matrix["$trace"]["row_count"]
    actual = len(matrix["rows"])
    results.record(
        "crash matrix row count matches the design table",
        declared == actual == 9,
        f"declared {declared}, found {actual}, design section 7 has 9 rows",
        ["T0-AC02", "T0-AC03", "V0-05"],
        group="traces",
    )
    ids = [r["id"] for r in matrix["rows"]]
    results.record(
        "crash matrix rows are uniquely identified",
        len(set(ids)) == len(ids),
        f"duplicate ids in {ids}",
        ["T0-AC02"],
        group="traces",
    )
    incomplete = [
        r["id"]
        for r in matrix["rows"]
        if not r.get("required_recovery")
        or not r.get("observable")
        or not r.get("owner")
    ]
    results.record(
        "every crash matrix row names its recovery, observable and owner",
        not incomplete,
        f"incomplete rows: {incomplete}",
        ["T0-AC03"],
        group="traces",
    )


def check_design_surface(lifecycle: dict[str, Any], results: Results) -> None:
    """The contract must name every internal route the design enumerates.

    A route the design specifies but the contract omits is a silent scope
    reduction: nine implementation slices would each decide independently
    whether it exists.
    """
    design = (REPO_ROOT / "docs" / "task-api" / "implementation-design.md").read_text()
    section = design[design.find("## 12.") : design.find("## 13.")]
    expected = set(re.findall(r"`(/internal/[^`]+)`", section))
    present = {r["route"] for r in lifecycle["internal_routes"]}
    results.record(
        "contract names every internal route in design section 12",
        expected <= present,
        f"missing {sorted(expected - present)}",
        ["T0-AC02", "V0-01"],
        group="design_surface",
    )

    invariants = lifecycle["lifecycle"]["invariants"]
    unowned = [i["id"] for i in invariants if not i.get("owner") or not i.get("rule")]
    results.record(
        "every lifecycle invariant states a rule and names an owner",
        not unowned,
        f"incomplete: {unowned}",
        ["T0-AC01", "T0-AC02"],
        group="design_surface",
    )

    ids = [i["id"] for i in invariants]
    results.record(
        "lifecycle invariants are uniquely identified",
        len(set(ids)) == len(ids),
        f"duplicates in {ids}",
        ["T0-AC02"],
        group="design_surface",
    )

    common = json.loads((SCHEMA_DIR / "common.schema.json").read_text())["$defs"]
    identifier_drift: list[str] = []
    for name, definition in lifecycle["identifiers"].items():
        schema = common.get(name)
        if schema is None:
            continue
        while "$ref" in schema:
            reference = schema["$ref"]
            prefix = "common.schema.json#/$defs/"
            if not reference.startswith(prefix):
                break
            schema = common[reference.removeprefix(prefix)]
        for field in ("type", "pattern", "minimum"):
            if field in definition and definition[field] != schema.get(field):
                identifier_drift.append(
                    f"{name}.{field}: baseline={definition[field]!r}, schema={schema.get(field)!r}"
                )
    results.record(
        "identity baseline and shared schemas use the same identifier constraints",
        not identifier_drift,
        "; ".join(identifier_drift),
        ["T0-AC02", "V0-03", "V0-04"],
        group="design_surface",
    )


def check_internal_adapters(
    registry: Registry,
    fixtures: list[tuple[Path, dict[str, Any]]],
    lifecycle: dict[str, Any],
    results: Results,
) -> None:
    """Every fixed adapter has one strict, runnable request/response contract."""
    expected_routes = {
        "/internal/v1/tasks/admit": "admit",
        "/internal/v1/tasks/dispatch/claim": "dispatch_claim",
        "/internal/v1/tasks/dispatch/settle": "dispatch_settle",
        "/internal/v1/tasks/recovery/claim": "recovery_claim",
        "/internal/v1/tasks/recovery/settle": "recovery_settle",
        "/internal/v1/agent/task/bootstrap": "bootstrap",
        "/internal/v1/agent/task/attempt": "attempt",
        "/internal/v1/agent/task/report": "report",
        "/internal/v1/agent/task/turn": "turn",
        "/internal/v1/agent/task/model": "model",
        "/internal/v1/agent/task/control": "control",
        "/internal/v1/agent/task/artifact": "artifact",
        "/internal/v1/agent/task/finalize": "finalize",
        "/internal/v1/agent/task/settlement": "settlement",
        "/internal/v1/agent/task/acquire": "acquire",
        "/internal/v1/agent/task/heartbeat": "heartbeat",
        "/internal/v1/agent/task/ack": "ack",
    }
    route_entries = lifecycle["internal_routes"]
    by_route = {entry.get("route"): entry for entry in route_entries}
    present_routes = set(by_route)
    results.record(
        "internal adapter inventory exactly matches section 12",
        present_routes == set(expected_routes),
        f"missing {sorted(set(expected_routes) - present_routes)}; unexpected {sorted(present_routes - set(expected_routes))}",
        ["T0-AC02", "V0-01", "V0-03"],
        group="internal_adapters",
    )

    inventory_errors: list[str] = []
    seen_keys: list[str] = []
    for route, expected_key in expected_routes.items():
        entry = by_route.get(route, {})
        contract_key = entry.get("contract_key")
        if contract_key != expected_key:
            inventory_errors.append(
                f"{route}: contract_key {contract_key!r}, expected {expected_key!r}"
            )
        if isinstance(contract_key, str):
            seen_keys.append(contract_key)
        for direction in ("request", "response"):
            pointer = entry.get(f"{direction}_schema")
            if not isinstance(pointer, str):
                inventory_errors.append(f"{route}: missing {direction}_schema")
                continue
            try:
                registry.resolve(pointer, "internal-adapters.schema.json")
            except SchemaError as exc:
                inventory_errors.append(f"{route}: {direction}_schema: {exc}")
    if len(seen_keys) != len(set(seen_keys)):
        inventory_errors.append(f"duplicate contract keys: {seen_keys}")
    results.record(
        "every internal route maps to resolvable request and response schemas",
        not inventory_errors,
        "; ".join(inventory_errors[:8]),
        ["T0-AC02", "T0-AC03", "V0-03", "V0-04"],
        group="internal_adapters",
    )

    fixtures_by_name = {rel(path): fixture for path, fixture in fixtures}
    catalog_name = (
        "docs/task-api/contracts/v1/fixtures/valid/internal-adapter-catalog.json"
    )
    mutation_name = (
        "docs/task-api/contracts/v1/fixtures/invalid/"
        "internal-adapter-unknown-fields.json"
    )
    catalog_fixture = fixtures_by_name.get(catalog_name, {})
    catalog = {
        key: value for key, value in catalog_fixture.items() if key != "$fixture"
    }
    catalog_errors: list[str] = []
    if set(catalog) != set(expected_routes.values()):
        catalog_errors.append(
            f"catalog keys differ: missing {sorted(set(expected_routes.values()) - set(catalog))}; "
            f"unexpected {sorted(set(catalog) - set(expected_routes.values()))}"
        )
    for route, contract_key in expected_routes.items():
        pair = catalog.get(contract_key, {})
        if pair.get("route") != route:
            catalog_errors.append(
                f"{contract_key}: route {pair.get('route')!r}, expected {route!r}"
            )
        entry = by_route.get(route, {})
        for direction in ("request", "response"):
            pointer = entry.get(f"{direction}_schema")
            if not isinstance(pointer, str) or direction not in pair:
                catalog_errors.append(f"{contract_key}: missing {direction} contract")
                continue
            try:
                schema, schema_doc = registry.resolve(
                    pointer, "internal-adapters.schema.json"
                )
                errors = validate(pair[direction], schema, registry, schema_doc)
            except SchemaError as exc:
                errors = [str(exc)]
            if errors:
                catalog_errors.append(
                    f"{contract_key}.{direction}: {'; '.join(errors[:2])}"
                )
    results.record(
        "adapter catalog covers and validates every fixed request and response",
        not catalog_errors,
        "; ".join(catalog_errors[:8]),
        ["T0-AC02", "T0-AC03", "V0-03", "V0-04"],
        group="internal_adapters",
    )

    mutation_fixture = fixtures_by_name.get(mutation_name, {})
    mutations = mutation_fixture.get("mutations", [])
    mutation_map = {
        item.get("contract_key"): item.get("targets")
        for item in mutations
        if isinstance(item, dict)
    }
    mutation_plan_errors: list[str] = []
    if set(mutation_map) != set(expected_routes.values()):
        mutation_plan_errors.append("mutation plan does not cover every contract key")
    for contract_key, targets in mutation_map.items():
        if targets != ["request", "response"]:
            mutation_plan_errors.append(
                f"{contract_key}: targets must be request and response"
            )
    results.record(
        "negative adapter fixture covers both bodies of every fixed route",
        not mutation_plan_errors,
        "; ".join(mutation_plan_errors),
        ["T0-AC02", "T0-AC03", "V0-03", "V0-04"],
        group="internal_adapters",
    )

    for route, contract_key in expected_routes.items():
        entry = by_route.get(route, {})
        pair = catalog.get(contract_key, {})
        for direction in ("request", "response"):
            pointer = entry.get(f"{direction}_schema")
            instance = copy.deepcopy(pair.get(direction))
            if not isinstance(pointer, str) or not isinstance(instance, dict):
                errors = []
            else:
                instance["unexpected_contract_field"] = True
                try:
                    schema, schema_doc = registry.resolve(
                        pointer, "internal-adapters.schema.json"
                    )
                    errors = validate(instance, schema, registry, schema_doc)
                except SchemaError as exc:
                    errors = [str(exc)]
            results.record(
                f"{contract_key} {direction} rejects unknown fields",
                bool(errors),
                "" if errors else "mutation unexpectedly validated",
                ["T0-AC02", "V0-03"],
                group="internal_adapters",
            )

    required_mutations = mutation_fixture.get("required_field_mutations", [])
    for mutation in required_mutations:
        contract_key = mutation.get("contract_key")
        target = mutation.get("target")
        field = mutation.get("field")
        route = next(
            (
                candidate
                for candidate, candidate_key in expected_routes.items()
                if candidate_key == contract_key
            ),
            None,
        )
        entry = by_route.get(route, {})
        pointer = entry.get(f"{target}_schema")
        instance = copy.deepcopy(catalog.get(contract_key, {}).get(target))
        if (
            not isinstance(pointer, str)
            or not isinstance(instance, dict)
            or not isinstance(field, str)
            or field not in instance
        ):
            errors = []
        else:
            del instance[field]
            try:
                schema, schema_doc = registry.resolve(
                    pointer, "internal-adapters.schema.json"
                )
                errors = validate(instance, schema, registry, schema_doc)
            except SchemaError as exc:
                errors = [str(exc)]
        results.record(
            f"{contract_key} {target} requires {field}",
            bool(errors),
            "" if errors else "required-field mutation unexpectedly validated",
            ["T0-AC02", "V0-03"],
            group="internal_adapters",
        )

    bootstrap = catalog.get("bootstrap", {}).get("response", {})
    credential = (
        bootstrap.get("run_credential") if isinstance(bootstrap, dict) else None
    )
    results.record(
        "bootstrap returns an explicit task-scoped non-secret fixture credential",
        isinstance(credential, str)
        and credential.startswith("fixture:non-secret:")
        and bool(credential.removeprefix("fixture:non-secret:")),
        f"run_credential is {credential!r}",
        ["T0-AC02", "V0-03"],
        group="internal_adapters",
    )


def check_decisions(results: Results) -> None:
    decisions = json.loads((CONTRACT_DIR / "decisions.json").read_text())
    entries = {entry["id"]: entry for entry in decisions["decisions"]}
    expected = [f"O{i}" for i in range(1, 9)]
    results.record(
        "all eight open questions O1-O8 are answered",
        sorted(entries) == expected,
        f"found {sorted(entries)}",
        ["T0-AC01"],
        group="decisions",
    )

    results.record(
        "the decision register is pinned to the accepted design revision",
        decisions.get("design_revision") == "b5761a4a2502aceaa9133afef552b567a19cb46e",
        f"design_revision is {decisions.get('design_revision')!r}",
        ["T0-AC01", "T0-AC04"],
        group="decisions",
    )

    placeholder_markers = (
        "TBD",
        "TODO",
        "FIXME",
        "???",
        "to be decided",
        "placeholder",
    )
    for key in sorted(entries):
        entry = entries[key]
        problems: list[str] = []
        for field in ("answer", "rationale", "design_reference"):
            value = entry.get(field, "")
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{field} is empty")
            elif any(m.lower() in value.lower() for m in placeholder_markers):
                problems.append(f"{field} contains a placeholder marker")
        if not entry.get("downstream_owners"):
            problems.append("no downstream owner")
        evidence = entry.get("evidence") or []
        if not evidence:
            problems.append("no evidence")
        for ref in evidence:
            target = REPO_ROOT / str(ref).split("#")[0].split(":")[0]
            if not target.exists():
                problems.append(f"evidence path does not exist: {ref}")
        results.record(
            f"{key}: concrete answer, real evidence, named downstream owner",
            not problems,
            "; ".join(problems),
            ["T0-AC01"],
            group="decisions",
        )


def check_manifest(results: Results) -> None:
    manifest_path = REPO_ROOT / "docs" / "task-api" / "evaluation-manifest.json"
    if not manifest_path.exists():
        results.record(
            "evaluation manifest exists",
            False,
            f"{rel(manifest_path)} not found",
            ["T0-AC01", "T0-AC03"],
            group="manifest",
        )
        return
    manifest = json.loads(manifest_path.read_text())
    criteria = manifest["criteria"]

    results.record(
        "evaluation manifest exists",
        True,
        f"{len(criteria)} criteria registered",
        ["T0-AC01", "T0-AC03"],
        group="manifest",
    )

    incomplete = [
        cid
        for cid, entry in criteria.items()
        if not all(
            entry.get(f)
            for f in (
                "owner",
                "wave",
                "evidence_lane",
                "fixture_responsibility",
                "command_status",
            )
        )
    ]
    results.record(
        "every criterion names owner, wave, evidence lane, fixture responsibility and command status",
        not incomplete,
        f"incomplete: {incomplete[:8]}",
        ["T0-AC01", "T0-AC03"],
        group="manifest",
    )

    missing_artifacts = [
        f"{cid}: {artifact}"
        for cid, entry in criteria.items()
        for artifact in entry.get("t0_artifacts", [])
        if not (CONTRACT_DIR / artifact).exists()
    ]
    results.record(
        "every criterion artifact reference exists",
        not missing_artifacts,
        f"missing: {missing_artifacts[:8]}",
        ["T0-AC02", "V0-06"],
        group="manifest",
    )

    actual_counts: dict[str, int] = {}
    for criterion_id in criteria:
        group = criterion_id.split("-", 1)[0]
        actual_counts[group] = actual_counts.get(group, 0) + 1
    declared_counts = manifest.get("counts_by_group")
    results.record(
        "manifest criterion count and group totals agree with its entries",
        manifest.get("criterion_count") == len(criteria)
        and declared_counts == actual_counts,
        f"declared total/groups: {manifest.get('criterion_count')}/{declared_counts}; actual: {len(criteria)}/{actual_counts}",
        ["T0-AC01", "V0-06"],
        group="manifest",
    )

    # Later evaluations become runnable only with their versioned PASS record.
    # A frozen-report verifier is explicitly distinct from live collection.
    runnable = sorted(
        cid for cid, entry in criteria.items() if entry["command_status"] == "runnable"
    )
    baseline_runnable = {cid for cid in criteria if cid.startswith(("V0-", "T0-"))}
    invalid_runnable = []
    for cid in runnable:
        if not cid.startswith("V") or cid in baseline_runnable:
            continue
        path = criteria[cid].get("qualification_report", "")
        try:
            report = json.loads((REPO_ROOT / path).read_text())
            recorded = report["criteria"][cid]
            passed = recorded.get("status", recorded.get("outcome")) == "PASS"
        except (OSError, ValueError, KeyError, TypeError):
            passed = False
        if not passed:
            invalid_runnable.append(cid)
    results.record(
        "runnable evaluations require versioned criterion PASS evidence",
        baseline_runnable.issubset(runnable) and not invalid_runnable,
        f"runnable: {runnable}; invalid evaluation commands: {invalid_runnable}",
        ["T0-AC03"],
        group="manifest",
    )

    # A registered command must name a path that exists. The failure this
    # prevents is a criterion marked runnable against a test file that was
    # renamed or never added: the manifest reads as covered, and nothing runs.
    missing_targets = []
    for cid, entry in criteria.items():
        if entry["command_status"] != "runnable":
            continue
        for token in entry["command"].split():
            candidate = token.split("::", 1)[0]
            # Shell environment assignments may themselves name a real path.
            if "=" in candidate and candidate.split("=", 1)[0].isidentifier():
                candidate = candidate.split("=", 1)[1]
            if "/" not in candidate or candidate.startswith("-"):
                continue
            if not (REPO_ROOT / candidate).exists():
                missing_targets.append(f"{cid}:{candidate}")
    results.record(
        "every runnable command points at a path that exists",
        not missing_targets,
        f"missing targets: {missing_targets[:6]}",
        ["T0-AC03", "V0-08"],
        group="manifest",
    )

    unregistered = [
        cid
        for cid, entry in criteria.items()
        if entry["command_status"] not in ("runnable", "not_implemented")
    ]
    results.record(
        "later commands are registered as unimplemented rather than omitted",
        not unregistered,
        f"unexpected statuses: {unregistered[:8]}",
        ["T0-AC03"],
        group="manifest",
    )

    thresholds = manifest.get("fixed_thresholds", {})
    unfixed = [k for k, v in thresholds.items() if v.get("value") in (None, "", "TBD")]
    results.record(
        "numeric acceptance limits are fixed before implementations are measured",
        bool(thresholds) and not unfixed,
        f"unfixed: {unfixed}"
        if unfixed
        else "no thresholds declared"
        if not thresholds
        else "",
        ["T0-AC03"],
        group="manifest",
    )

    threshold_source_problems: list[str] = []
    for name, threshold in thresholds.items():
        source = threshold.get("source", "")
        source_path, _, pointer = source.partition("#")
        target_path = REPO_ROOT / "docs" / "task-api" / source_path
        if not source_path or not pointer or not target_path.exists():
            threshold_source_problems.append(f"{name}: unresolved source {source!r}")
            continue
        source_document = json.loads(target_path.read_text())
        source_value = json_pointer_value(source_document, pointer)
        if source_value is MISSING:
            threshold_source_problems.append(f"{name}: unresolved pointer {source!r}")
        elif not isinstance(
            source_value, (dict, list)
        ) and source_value != threshold.get("value"):
            threshold_source_problems.append(
                f"{name}: value {threshold.get('value')!r} differs from {source_value!r}"
            )
    results.record(
        "fixed threshold sources resolve and scalar values agree",
        not threshold_source_problems,
        "; ".join(threshold_source_problems[:8]),
        ["T0-AC03", "V0-05"],
        group="manifest",
    )

    amendments = manifest.get("proposed_amendments")
    results.record(
        "departures from D01-D18 are recorded as proposed amendments, not silent changes",
        isinstance(amendments, list),
        "proposed_amendments must be a list, empty if there are none",
        ["T0-AC04"],
        group="manifest",
    )
    if isinstance(amendments, list):
        bad = [
            a.get("id", "?")
            for a in amendments
            if not all(
                a.get(f)
                for f in (
                    "id",
                    "decision",
                    "observation",
                    "proposal",
                    "status",
                    "owner_decision_required",
                )
            )
        ]
        results.record(
            "each proposed amendment names the decision, observation, proposal and awaited owner",
            not bad,
            f"incomplete amendments: {bad}",
            ["T0-AC04"],
            group="manifest",
        )


def check_v0_obligations(results: Results) -> None:
    """The three V0 criteria that are about this deliverable itself.

    V0-06 asks for a manifest covering every criterion with nothing removed,
    weakened or counted as passing from a skipped test. V0-07 asks for a
    recorded cross-component consistency review against the exact source
    revision. V0-08 asks for registered runnable entry points and visible
    unimplemented commands. None of these can be proved by a fixture, so they
    are checked here.
    """
    manifest_path = REPO_ROOT / "docs" / "task-api" / "evaluation-manifest.json"
    review_path = CONTRACT_DIR / "consistency-review.json"

    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        criteria = manifest["criteria"]
        expected = [f"T0-AC{i:02d}" for i in range(1, 5)] + [
            f"T{s}-AC{i:02d}" for s in range(1, 9) for i in range(1, 6)
        ]
        validation = (REPO_ROOT / "docs" / "task-api" / "validation.md").read_text()
        expected += sorted(set(re.findall(r"\bV[0-5]-\d\d\b", validation)))
        missing = [c for c in expected if c not in criteria]
        extra = [c for c in criteria if c not in expected]
        results.record(
            "manifest covers every original criterion with none removed or added",
            not missing and not extra,
            f"missing {missing[:6]}; unexpected {extra[:6]}",
            ["T0-AC01", "V0-06"],
            group="v0_obligations",
        )
        results.record(
            "criterion count is the 84 original plus the 8 V0 criteria",
            len(criteria) == 92,
            f"found {len(criteria)}, expected 92",
            ["T0-AC01", "V0-06"],
            group="v0_obligations",
        )
        skipped_as_pass = [
            cid
            for cid, e in criteria.items()
            if e["command_status"] == "not_implemented"
            and "not yet implemented" not in e["command"]
        ]
        results.record(
            "no unimplemented command is presented as an existing test",
            not skipped_as_pass,
            f"misleading commands: {skipped_as_pass[:6]}",
            ["T0-AC03", "V0-08"],
            group="v0_obligations",
        )
        runnable_commands = []
        for entry in criteria.values():
            if entry["command_status"] == "runnable" and entry["command"] not in runnable_commands:
                runnable_commands.append(entry["command"])
        results.record(
            "every runnable entry point is registered in the manifest",
            set(manifest.get("runnable_now", [])) == set(runnable_commands)
            and (REPO_ROOT / "scripts" / "task-api" / "check-contracts.py").exists(),
            f"runnable_now is {manifest.get('runnable_now')}; criteria commands are {runnable_commands}",
            ["V0-08"],
            group="v0_obligations",
        )

    if not review_path.exists():
        results.record(
            "a cross-component consistency review is recorded",
            False,
            f"{rel(review_path)} not found",
            ["T0-AC04", "V0-07"],
            group="v0_obligations",
        )
        return
    review = json.loads(review_path.read_text())
    results.record(
        "consistency review is pinned to the exact design revision",
        review.get("design_revision") == "b5761a4a2502aceaa9133afef552b567a19cb46e",
        f"design_revision is {review.get('design_revision')!r}",
        ["T0-AC04", "V0-07"],
        group="v0_obligations",
    )
    findings = review.get("findings", [])
    incomplete = [
        f.get("id", "?")
        for f in findings
        if not all(f.get(k) for k in ("id", "observation", "resolution", "status"))
    ]
    results.record(
        "every consistency finding records its observation, resolution and status",
        isinstance(findings, list) and not incomplete,
        f"incomplete findings: {incomplete}",
        ["T0-AC04", "V0-07"],
        group="v0_obligations",
    )
    unresolved = [f["id"] for f in findings if f.get("status") == "unresolved"]
    results.record(
        "no unresolved contract contradiction remains, or it is named as a blocker",
        all(
            f.get("blocks_handoff") is not None
            for f in findings
            if f.get("status") == "unresolved"
        ),
        f"unresolved findings without an explicit handoff decision: {unresolved}",
        ["T0-AC04", "V0-07"],
        group="v0_obligations",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument(
        "--verbose", action="store_true", help="list passing checks too"
    )
    args = parser.parse_args()

    registry = Registry(SCHEMA_DIR)
    fixtures = load_fixtures()
    lifecycle = json.loads((CONTRACT_DIR / "identity-and-lifecycle.json").read_text())
    TERMINAL_STATUSES.update(lifecycle["lifecycle"]["terminal_states"])
    results = Results()

    check_schemas(registry, results)
    check_fixtures(registry, fixtures, results)
    check_cross_component(fixtures, lifecycle, results)
    check_citations(fixtures, results)
    check_traces(fixtures, lifecycle, results)
    check_design_surface(lifecycle, results)
    check_internal_adapters(registry, fixtures, lifecycle, results)
    check_decisions(results)
    check_manifest(results)
    check_v0_obligations(results)

    failures = results.failures
    if args.format == "json":
        print(
            json.dumps(
                {
                    "design_revision": "b5761a4a2502aceaa9133afef552b567a19cb46e",
                    "total_checks": len(results.checks),
                    "failed": len(failures),
                    "criteria_covered": results.criteria_covered(),
                    "checks": results.checks if args.verbose else failures,
                },
                indent=2,
            )
        )
        return 1 if failures else 0

    groups: dict[str, list[dict[str, Any]]] = {}
    for check in results.checks:
        groups.setdefault(check["group"], []).append(check)

    print("Task API v1 contract conformance")
    print("design revision b5761a4a2502aceaa9133afef552b567a19cb46e")
    print()
    for group, checks in groups.items():
        failed = [c for c in checks if not c["passed"]]
        print(
            f"  {group:16} {len(checks) - len(failed):4} passed  {len(failed):4} failed"
        )
    print()
    if args.verbose:
        for check in results.checks:
            mark = "ok  " if check["passed"] else "FAIL"
            print(f"  {mark} {check['check']}")
            if check["detail"] and not check["passed"]:
                print(f"       {check['detail']}")
    else:
        for check in failures:
            print(f"  FAIL {check['check']}")
            if check["detail"]:
                print(f"       {check['detail']}")
    if failures:
        print()
        print(f"{len(failures)} of {len(results.checks)} checks failed.")
        return 1
    covered = results.criteria_covered()
    print(
        f"All {len(results.checks)} checks passed, exercising {len(covered)} criteria."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
