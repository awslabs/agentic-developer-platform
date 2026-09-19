"""Sanitized evidence: report.json, JUnit XML and the Actions summary.

Everything an operator or a reviewer sees comes through here, so this module is
the last line of defence against publishing a credential. `redact()` runs over
every case detail and every correlation value on the way out — a case that
accidentally collects a token still cannot leak it into an artifact.

What is deliberately kept: account IDs, role and session names, request IDs,
token COUNTS, ADP org/team/user IDs, timing and revisions. Those are the
evidence. What is dropped: passwords, tokens, ExternalIds, keys, cookies and
anything that looks like a bearer credential.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import cases

# Key names whose VALUES must never be published. `external.?id` matters
# specifically: an ExternalId is the second half of an assume-role credential.
SECRET_KEYS = re.compile(
    r"password|token|secret|external.?id|authorization|access.?key|private.?key|cookie|bearer|launch_url",
    re.I,
)

# Token counters are evidence, not credentials, and their names collide with the
# pattern above. Whitelist them explicitly.
COUNTER_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "total_tokens",
        "call_count",
        "token_count",
        "inputTokenCount",
        "outputTokenCount",
    }
)

# Any JWT-shaped run of text, wherever it appears in a free-text string.
JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?")

# Flags whose values are sensitive or environment-specific. The transcript keeps
# the flag (so a reader knows the shape of the command) and drops the value.
_REDACT_VALUE = "<redacted>"


def redact(value):
    """Recursively strip credential-shaped keys and scrub JWTs from strings."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key in COUNTER_KEYS:
                clean[key] = redact(item)
                continue
            if SECRET_KEYS.search(str(key)):
                continue
            clean[key] = redact(item)
        return clean
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return JWT.sub("[REDACTED]", value)
    return value


def sanitize_command(argv):
    """Render a command for the transcript without publishing its values.

    Keeps the subcommand path and the flag names, replaces every flag value and
    positional with a placeholder. A reader can see that
    `adp aws connect --account ... --yes` ran, and can reproduce it from the
    runbook, without the artifact carrying an account-specific secret.
    """
    if not isinstance(argv, (list, tuple)):
        raise TypeError("sanitize_command expects the argv list, not a string")
    parts = []
    for item in argv:
        text = str(item)
        if text.startswith("-"):
            # Split --flag=value so the flag name survives and the value does not.
            parts.append(text.split("=", 1)[0] if "=" in text else text)
        elif parts and parts[-1].startswith("-"):
            parts.append(_REDACT_VALUE)
        elif not parts or not parts[-1].startswith("-"):
            # Leading words are the command path (adp aws connect); keep them
            # until the first flag appears, then treat the rest as values.
            parts.append(
                text if all(not p.startswith("-") for p in parts) else _REDACT_VALUE
            )
    return " ".join(parts)


def build(
    *,
    matrix,
    suites,
    config,
    evaluation_id,
    attempt_id,
    cleanup_ok,
    timing,
    correlation,
    transcript=(),
    preflight=None,
    stages=None,
):
    """Assemble report.json. The status here is the authoritative verdict.

    `stages` participates in the verdict, so a run whose preflight or any other
    stage did not complete cannot publish acceptance even when every case in the
    matrix reads as passed. That combination is reachable on resume, where passed
    results are preserved across attempts by design.
    """
    stages = stages if stages is not None else (timing or {}).get("stages")
    status, reasons = cases.accept(matrix, suites, cleanup_ok=cleanup_ok, stages=stages)
    rows = []
    for case_id, entry in matrix.items():
        case = cases.BY_ID[case_id]
        rows.append(
            {
                "id": case_id,
                "owner": case.owner,
                "suite": case.suite,
                "summary": case.summary,
                "status": entry["status"],
                "detail": redact(entry.get("detail") or {}),
            }
        )
    return {
        "evaluation_id": evaluation_id,
        "attempt_id": attempt_id,
        "status": status,
        "reasons": reasons,
        "full_acceptance": status == cases.PASSED and cases.is_full(suites),
        "suites": list(suites),
        "partial": not cases.is_full(suites),
        "counts": cases.tally(matrix),
        "cases": rows,
        "cleanup": "complete" if cleanup_ok else "failed",
        "expected_revision": config.get("expected_revision"),
        "harness_commit": config.get("harness_commit"),
        "environment": {
            "gateway_url": config.get("gateway_url"),
            "region": config.get("region"),
            "platform_account": config.get("platform_account"),
            "destination_account": config.get("destination_account"),
            "second_destination_account": config.get("second_destination_account"),
        },
        "tool_versions": {
            "claude": config.get("claude_version"),
            "codex": config.get("codex_version"),
        },
        "timing": redact(timing or {}),
        "correlation": redact(correlation or {}),
        "preflight": redact(preflight or {}),
        "transcript": [redact(line) for line in transcript],
    }


def junit(matrix, evaluation_id):
    """JUnit XML for the Actions test view.

    Blocked and not-run become `<skipped>` WITH a reason rather than passes, so
    the distinction the issue requires survives into the report. The suite still
    counts them, and `report.json`/`accept()` remain the acceptance authority —
    JUnit's own pass/fail totals never widen what counts as success.
    """
    failures = sum(1 for entry in matrix.values() if entry["status"] == cases.FAILED)
    skipped = sum(
        1
        for entry in matrix.values()
        if entry["status"] in (cases.BLOCKED, cases.NOT_RUN)
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuite name="cli-uplift-eval" tests="{len(matrix)}" failures="{failures}" skipped="{skipped}" id={quoteattr(evaluation_id)}>',
    ]
    for case_id, entry in matrix.items():
        case = cases.BY_ID[case_id]
        name = quoteattr(f"{case_id} ({case.owner}) {case.summary}")
        detail = json.dumps(redact(entry.get("detail") or {}), sort_keys=True)
        lines.append(f'  <testcase classname="cli-uplift.{case.suite}" name={name}>')
        if entry["status"] == cases.FAILED:
            lines.append(
                f'    <failure message="case failed">{escape(detail)}</failure>'
            )
        elif entry["status"] in (cases.BLOCKED, cases.NOT_RUN):
            lines.append(
                f"    <skipped message={quoteattr(entry['status'])}>{escape(detail)}</skipped>"
            )
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


ICONS = {
    cases.PASSED: "✅",
    cases.FAILED: "❌",
    cases.BLOCKED: "🚧",
    cases.NOT_RUN: "⬜",
}


def summary(
    matrix,
    suites,
    evaluation_id,
    *,
    cleanup_ok,
    revision=None,
    run_url=None,
    stages=None,
):
    """Markdown for $GITHUB_STEP_SUMMARY.

    Grades with the same inputs as `build()`, including stages: an Actions summary
    that says `passed` above a report.json saying `failed` is its own incident.
    """
    status, reasons = cases.accept(matrix, suites, cleanup_ok=cleanup_ok, stages=stages)
    counts = cases.tally(matrix)
    lines = [
        f"## CLI uplift evaluation — `{status}`",
        "",
        f"- **Evaluation ID:** `{evaluation_id}`",
        f"- **Suites:** `{', '.join(suites)}`"
        + (
            ""
            if cases.is_full(suites)
            else "  — **partial scope; cannot satisfy full acceptance**"
        ),
        f"- **Cases:** {counts[cases.PASSED]} passed, {counts[cases.FAILED]} failed, "
        f"{counts[cases.BLOCKED]} blocked, {counts[cases.NOT_RUN]} not run",
        f"- **Cleanup:** {'complete' if cleanup_ok else '**failed**'}",
    ]
    if revision:
        lines.append(f"- **Deployed revision under test:** `{revision}`")
    if run_url:
        lines.append(f"- **Run:** {run_url}")
    if reasons:
        lines += ["", "### Why this is not full acceptance", ""] + [
            f"- {reason}" for reason in reasons
        ]
    lines += [
        "",
        "| Case | Owner | Suite | Status | Expected result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case_id, entry in matrix.items():
        case = cases.BY_ID[case_id]
        note = ""
        missing = (entry.get("detail") or {}).get("missing_fixtures")
        if missing:
            note = " — missing: " + ", ".join(missing)
        icon = ICONS.get(entry["status"], entry["status"])
        lines.append(
            f"| `{case_id}` | {case.owner} | {case.suite} | {icon} {entry['status']}{note} | {case.summary} |"
        )
    return "\n".join(lines) + "\n"


SCHEMA_PATH = Path(__file__).with_name("report.schema.json")


def load_schema():
    return json.loads(SCHEMA_PATH.read_text())


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "boolean": bool,
}


def _type_ok(value, expected):
    names = [expected] if isinstance(expected, str) else list(expected)
    for name in names:
        if name == "null":
            if value is None:
                return True
            continue
        if name == "integer" and isinstance(value, bool):
            continue  # bool is an int subclass; a flag is not a count
        if isinstance(value, _JSON_TYPES[name]):
            return True
    return False


def _check(node, schema, path, errors):
    """Validate the subset of JSON Schema this schema actually uses.

    Deliberately dependency-free: `jsonschema` is not a pinned dependency of this
    repo, and adding one to make a published contract enforceable would mean the
    offline guards could not run without a new install. The subset covered is
    type/enum/pattern/required/additionalProperties/properties/items/minItems/
    minimum, which is exactly what report.schema.json expresses. An unsupported
    keyword is ignored rather than silently treated as satisfied by nothing --
    see test_report_schema_uses_only_supported_keywords, which fails if the
    schema grows a construct this validator cannot check.
    """
    expected = schema.get("type")
    if expected is not None and not _type_ok(node, expected):
        errors.append(f"{path}: expected type {expected}, got {type(node).__name__}")
        return
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: {node!r} is not one of {schema['enum']}")
    if "pattern" in schema and isinstance(node, str):
        if not re.search(schema["pattern"], node):
            # The value may itself be sensitive; report the constraint, not the value.
            errors.append(f"{path}: does not match {schema['pattern']}")
    if "minimum" in schema and isinstance(node, (int, float)):
        if node < schema["minimum"]:
            errors.append(f"{path}: {node} is below minimum {schema['minimum']}")
    if isinstance(node, dict):
        for key in schema.get("required", ()):
            if key not in node:
                errors.append(f"{path}: missing required key {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in node:
                if key not in properties:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, subschema in properties.items():
            if key in node:
                _check(node[key], subschema, f"{path}.{key}", errors)
    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(node):
                _check(item, item_schema, f"{path}[{index}]", errors)


def validate(document, schema=None):
    """Return the list of schema violations in an assembled report.

    Empty means valid. Callers get the list rather than an exception so a
    consumer can log every problem at once; `write()` raises on any.
    """
    errors = []
    _check(document, schema or load_schema(), "$", errors)
    return errors


def write(directory, document, matrix, evaluation_id):
    """Write report.json and results.xml with private permissions.

    The document is validated against report.schema.json before it lands.
    The schema is a published contract, so a drift between it and `build()` must
    break the run that produced the drift -- a report that no longer matches its
    own schema is worse than no report, because downstream consumers trust it.
    `additionalProperties: false` also means a new field carrying a credential
    fails here instead of shipping quietly in an artifact.
    """
    errors = validate(document)
    if errors:
        raise ValueError(
            "report does not satisfy report.schema.json: " + "; ".join(errors)
        )
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.json"
    report_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    report_path.chmod(0o600)
    junit_path = root / "results.xml"
    junit_path.write_text(junit(matrix, evaluation_id))
    junit_path.chmod(0o600)
    return {"report": str(report_path), "junit": str(junit_path)}
