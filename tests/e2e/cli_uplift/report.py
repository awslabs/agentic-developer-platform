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
from tests.regression.catalog import cli_tags

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


def qualification_rows(matrix):
    entry = matrix.get("E46") or {}
    limitation = (entry.get("detail") or {}).get("reservation_limitation")
    if not isinstance(limitation, dict):
        return []
    return [
        {
            "id": "QUAL03",
            "source_case": "E46",
            "status": "known_limitation",
            "detail": redact(limitation),
        }
    ]


def assistant_qualification_rows(matrix, config, preflight, cleanup_ok):
    revision = (preflight or {}).get("deployed_revision")
    rows = []
    for case_id in ("E46", "E47"):
        entry = matrix.get(case_id)
        if entry is None:
            continue
        detail = entry.get("detail") or {}
        verified = entry["status"] == cases.PASSED
        rows.append(
            redact(
                {
                    "source_case": case_id,
                    "qualification_status": "pending_operator_review",
                    "case_status": entry["status"],
                    "expected_revision": config.get("expected_revision"),
                    "deployed_revision": revision,
                    "cleanup_status": "complete" if cleanup_ok else "failed",
                    "cleanup_seconds": (
                        detail.get("cleanup_seconds")
                        if verified and case_id == "E46"
                        else None
                    ),
                    "fault": detail.get("fault")
                    if verified and case_id == "E47"
                    else None,
                    "outcome": detail.get("outcome")
                    if verified and case_id == "E47"
                    else None,
                    "accounting_status": (
                        detail.get("accounting_status")
                        if verified and case_id == "E47"
                        else None
                    ),
                    "fault_proof": (
                        detail.get("fault_proof")
                        if verified and case_id == "E47"
                        else None
                    ),
                }
            )
        )
    return rows


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
    product_cli_attempts=(),
    cleanup_outstanding=(),
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
                "tags": cli_tags(case_id),
                "owner": case.owner,
                "suite": case.suite,
                "summary": case.summary,
                "status": entry["status"],
                "detail": redact(entry.get("detail") or {}),
            }
        )
    document = {
        "evaluation_id": evaluation_id,
        "attempt_id": attempt_id,
        "status": status,
        "reasons": reasons,
        "full_acceptance": status == cases.PASSED and cases.is_full(suites),
        "suites": list(suites),
        "partial": not cases.is_full(suites),
        "counts": cases.tally(matrix),
        "cases": rows,
        "known_limitations": qualification_rows(matrix),
        "assistant_qualification": assistant_qualification_rows(
            matrix, config, preflight, cleanup_ok
        ),
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
    cli_document = product_cli_report(
        document, product_cli_attempts, cleanup_outstanding
    )
    if cli_document is not None and cli_document["status"] != "passed":
        document["status"] = cases.FAILED
        document["reasons"] = [
            *reasons,
            "Required installed Product CLI evidence is incomplete",
        ]
        document["full_acceptance"] = False
    return document


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
        lines.append("    <properties>")
        for field, identities in cli_tags(case_id).items():
            key = "adp_" + field.removesuffix("_ids") + "_id"
            for identity in identities:
                lines.append(
                    f"      <property name={quoteattr(key)} value={quoteattr(identity)}/>"
                )
        lines.append("    </properties>")
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
    for row in qualification_rows(matrix):
        lines.append(
            f"| `{row['id']}` | #7174 | assistant | 🚧 known_limitation | "
            "Creation reservation has no observed pod (source E46; proof in report.json) |"
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


PRODUCT_CLI_CASES = {
    "PR03-C01": ("AC-01", "SCN-01"),
    "PR03-C02": ("AC-02", "SCN-02"),
    "PR03-C03": ("AC-03", "SCN-03"),
}


def product_cli_report(document, attempts, cleanup_outstanding=()):
    """Separate, private attempt-aware Product CLI evidence; never a BOLT decision."""
    selected = {
        row["id"]: row for row in document["cases"] if row["id"] in PRODUCT_CLI_CASES
    }
    if not selected:
        return None
    attempts = [
        redact(attempt) for attempt in attempts if attempt.get("case_id") in selected
    ]
    release = (document.get("preflight") or {}).get("product_release_check") or {}
    expected_hashes = (
        (document.get("preflight") or {}).get("expected_release") or {}
    ).get("files") or {}
    failures = []
    rows = []
    for case_id, (criterion, scenario) in PRODUCT_CLI_CASES.items():
        case = selected.get(case_id)
        if case is None:
            failures.append(f"{case_id} was not selected")
            continue
        observed = [item for item in attempts if item.get("case_id") == case_id]
        proof = observed[-1] if observed else {}
        receipt = proof.get("retirement_receipt") or {}
        if any(
            not isinstance(item.get("attempt_id"), str)
            or not item["attempt_id"].startswith(document["evaluation_id"] + "-a")
            or item.get("requirements_revision") != document.get("expected_revision")
            for item in observed
        ):
            failures.append(f"{case_id} has an unbound CLI attempt")
        if case["status"] != cases.PASSED:
            failures.append(f"{case_id} is {case['status']}")
        elif (
            not observed
            or proof.get("status") != "passed"
            or (
                case_id == "PR03-C03"
                and (
                    proof.get("absence_verified") is not True
                    or not isinstance(proof.get("owned_product"), dict)
                    or not proof["owned_product"].get("product_id")
                    or not proof["owned_product"].get("project_id")
                    or not isinstance(proof.get("retirement_receipt"), dict)
                    or receipt.get("run_id") != document["evaluation_id"]
                    or receipt.get("fixture_removed") is not True
                    or type(receipt.get("cleanup_revision")) is not int
                    or receipt["cleanup_revision"] < 1
                    or any(
                        receipt.get(field) != proof["owned_product"].get(field)
                        for field in ("product_id", "project_id")
                    )
                    or not isinstance(proof.get("recovery_attempts"), list)
                    or any(
                        not isinstance(attempt, dict)
                        or attempt.get("state")
                        not in ("reconcile_started", "reconcile_received")
                        for attempt in proof["recovery_attempts"]
                    )
                    or not any(
                        isinstance(result, dict)
                        and result.get("kind") == "product_fixture"
                        and result.get("status") == "deleted"
                        for result in proof.get("cleanup_results") or []
                    )
                )
            )
            or (
                case_id != "PR03-C03"
                and (
                    not proof.get("cli_events")
                    or not proof.get("installed_hashes")
                    or any(
                        not isinstance(event, dict)
                        or not isinstance(event.get("argv"), list)
                        or len(event["argv"]) < 4
                        or event["argv"][:2] != ["adp", "product"]
                        or event["argv"][-1] != "--json"
                        or event.get("actor") not in ("owner", "foreign")
                        or type(event.get("exit_code")) is not int
                        or not isinstance(event.get("output"), dict)
                        or event["output"].get("status") not in ("ok", "failed")
                        for event in proof.get("cli_events") or []
                    )
                )
            )
            or (
                case_id == "PR03-C01"
                and (
                    len(proof.get("cli_events") or []) < 6
                    or len(proof.get("api_readback") or []) < 5
                    or not isinstance(proof.get("owned_product"), dict)
                    or any(
                        not isinstance(readback, dict)
                        or readback.get("status") != 200
                        or any(
                            readback.get(field) != proof["owned_product"].get(field)
                            for field in ("product_id", "project_id")
                        )
                        for readback in proof.get("api_readback") or []
                    )
                )
            )
            or (
                case_id == "PR03-C02"
                and (len(proof.get("denials") or []) < 3 or not proof.get("retry"))
            )
        ):
            failures.append(f"{case_id} lacks verified installed-CLI attempt evidence")
        elif case_id != "PR03-C03" and (
            not all(
                proof["installed_hashes"].get(name)
                for name in ("adp", "adp-product.py")
            )
            or any(
                expected_hashes.get(name) != digest
                for name, digest in proof["installed_hashes"].items()
            )
        ):
            failures.append(f"{case_id} installed hashes differ from pinned release")
        rows.append(
            {
                "case_id": case_id,
                "criterion": criterion,
                "scenario": scenario,
                "status": case["status"],
                "attempt_ids": [item.get("attempt_id") for item in observed],
            }
        )
    if document.get("cleanup") != "complete" or cleanup_outstanding:
        failures.append("Product fixture cleanup is incomplete")
    if (
        not release.get("workflow_revision")
        or not release.get("image_digest")
        or release.get("source_revision") != document.get("expected_revision")
        or document.get("expected_revision")
        != (document.get("preflight") or {}).get("deployed_revision")
    ):
        failures.append(
            "Pinned Product source, workflow or API release proof is unavailable"
        )
    return {
        "schema_version": 1,
        "surface": "installed-cli",
        "evaluation_id": document["evaluation_id"],
        "attempt_id": document["attempt_id"],
        "workflow_run_id": document.get("product_run_id"),
        "requirements_revision": document.get("expected_revision"),
        "source_revision": release.get("source_revision"),
        "workflow_revision": release.get("workflow_revision"),
        "api_image_digest": release.get("image_digest"),
        "cli_manifest_sha256": release.get("cli_manifest_sha256"),
        "status": "passed" if not failures else "incomplete",
        "failures": failures,
        "cases": rows,
        "attempts": attempts,
        "cleanup": document.get("cleanup"),
        "cleanup_outstanding": list(cleanup_outstanding),
    }


def write(
    directory,
    document,
    matrix,
    evaluation_id,
    *,
    product_cli_attempts=(),
    cleanup_outstanding=(),
    product_run_id=None,
):
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
    samples = next(
        (
            row["detail"].get("samples") or []
            for row in document["cases"]
            if row["id"] == "E49"
        ),
        [],
    )
    raw_path = root / "assistant-latency-samples.jsonl"
    with raw_path.open("w") as stream:
        for sample in samples:
            stream.write(
                json.dumps(
                    {
                        **redact(sample),
                        "evaluation_id": evaluation_id,
                        "harness_commit": document.get("harness_commit"),
                        "deployed_revision": (document.get("preflight") or {}).get(
                            "deployed_revision"
                        ),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    raw_path.chmod(0o600)
    paths = {"report": str(report_path), "junit": str(junit_path)}
    cli_document = product_cli_report(
        {**document, "product_run_id": product_run_id},
        product_cli_attempts,
        cleanup_outstanding,
    )
    if cli_document is not None:
        private_path = root / "product-profile-cli.private.json"
        private_path.write_text(
            json.dumps(cli_document, indent=2, sort_keys=True) + "\n"
        )
        private_path.chmod(0o600)
        paths["product_cli_private"] = str(private_path)
    return paths
