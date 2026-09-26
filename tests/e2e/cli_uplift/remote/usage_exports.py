"""E21 bounded installed NDJSON/CSV exports; metadata only, no inference."""

import csv
import io
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime

import common

COLUMNS = "id,timestamp,request_id,org_id,user_id,model,input_tokens,output_tokens,status_code,invocation_id,chain_id,root_human_id,cost_status,cost_amount,currency,settlement".split(
    ","
)
PRIVATE_FIELDS = {
    "prompt",
    "response",
    "client_secret",
    "access_token",
    "refresh_token",
    "api_key",
    "credentials",
}


def metadata_only(value):
    if isinstance(value, dict):
        common.require(
            not (set(value) & PRIVATE_FIELDS), "Usage export contains private fields"
        )
        for child in value.values():
            metadata_only(child)
    elif isinstance(value, list):
        for child in value:
            metadata_only(child)


def parse(mode, code, out, err):
    try:
        if mode == "ndjson":
            common.require(not err.strip(), "NDJSON export mixed stderr output")
            frames = [json.loads(line) for line in out.splitlines() if line.strip()]
            common.require(
                frames and all(isinstance(frame, dict) for frame in frames),
                "NDJSON export missing typed frames",
            )
            continuation = frames[-1]
            common.require(
                all(frame.get("type") == "record" for frame in frames[:-1]),
                "NDJSON continuation must occur exactly once at end",
            )
            rows = [frame.get("record") for frame in frames[:-1]]
        else:
            reader = csv.DictReader(io.StringIO(out))
            common.require(reader.fieldnames == COLUMNS, "CSV export columns changed")
            rows = list(reader)
            common.require(
                all(
                    set(row) == set(COLUMNS)
                    and all(isinstance(cell, str) for cell in row.values())
                    for row in rows
                ),
                "CSV row width changed",
            )
            for row in rows:
                common.require(
                    all(
                        not (
                            cell.lstrip().startswith(("=", "+", "-", "@"))
                            or cell.startswith(("\t", "\r", "\n"))
                        )
                        for cell in row.values()
                    ),
                    "CSV contains unescaped spreadsheet formula",
                )
            continuation = json.loads(err)
        common.require(
            isinstance(continuation, dict)
            and continuation.get("type") == "continuation",
            "Export continuation missing",
        )
        meta = continuation.get("detail")
        common.require(isinstance(meta, dict), "Export continuation metadata missing")
        complete = meta.get("complete")
        common.require(
            type(complete) is bool
            and continuation.get("status") == ("ok" if complete else "pending")
            and code == (0 if complete else 4),
            "Export completion and exit disagree",
        )
        common.require(
            meta.get("scope", {}).get("kind") == "own", "Export lost own scope"
        )
        common.require(
            (complete and meta.get("next_cursor") is None)
            or (
                not complete
                and isinstance(meta.get("next_cursor"), str)
                and meta["next_cursor"]
            ),
            "Export cursor inconsistent",
        )
        common.require(
            len(rows) <= 1 and (complete or rows),
            "One-record export page bound or continuation violated",
        )
        if mode == "ndjson":
            common.require(
                all(frame.get("scope") == meta["scope"] for frame in frames[:-1]),
                "NDJSON record scope differs from continuation",
            )
        metadata_only([rows, meta])
        for row in rows:
            common.require(
                isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"],
                "Export lacks stable record ID",
            )
            cost = row if mode == "csv" else row.get("cost", {})
            status = cost.get("cost_status" if mode == "csv" else "status")
            amount = cost.get("cost_amount" if mode == "csv" else "amount")
            common.require(
                status in {"unknown", "estimated", "lower_bound"}
                and cost.get("currency") == "USD",
                "Export cost semantics missing",
            )
            common.require(
                amount in (None, "")
                if status == "unknown"
                else isinstance(amount, str) and amount != "",
                "Export unknown cost became known or amount disappeared",
            )
            if amount not in (None, ""):
                common.require(
                    isinstance(amount, str)
                    and Decimal(amount).is_finite()
                    and Decimal(amount) >= 0,
                    "Export cost is not a finite nonnegative decimal string",
                )
        return rows, meta
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        InvalidOperation,
        csv.Error,
    ):
        raise common.RemoteError(
            "Usage export format is malformed; raw output suppressed"
        ) from None


def exercise(cli, flags, evidence):
    formats = {}
    for mode in ("ndjson", "csv"):
        cursor, scope, seen = None, None, set()
        pages = 0
        complete = False
        for _ in range(2):
            argv = [
                cli.binary,
                "logs",
                "export",
                *flags,
                "--format",
                mode,
                "--page-size",
                "1",
                "--max-pages",
                "1",
            ]
            if cursor:
                argv.extend(["--cursor", cursor])
            cli.transcript.append(common.sanitize(argv))
            code, out, err = common.bounded(argv, env=cli.env, timeout=cli.timeout)
            rows, meta = parse(mode, code, out, err)
            try:
                same_window = all(
                    datetime.fromisoformat(meta[key].replace("Z", "+00:00"))
                    == datetime.fromisoformat(
                        flags[flags.index("--" + key) + 1].replace("Z", "+00:00")
                    )
                    for key in ("start", "end")
                )
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                same_window = False
            common.require(same_window, "Export changed selected time window")
            common.require(
                scope is None or meta["scope"] == scope,
                "Export continuation changed scope",
            )
            expected = evidence.get("usage_owner")
            if expected:
                common.require(
                    all(
                        meta["scope"].get(key) == value
                        for key, value in expected.items()
                    ),
                    "Export changed verified owner or tenant",
                )
            scope = meta["scope"]
            ids = {row["id"] for row in rows}
            common.require(not (seen & ids), "Export continuation duplicated a record")
            seen.update(ids)
            pages += 1
            complete = meta["complete"]
            if complete:
                break
            next_cursor = meta["next_cursor"]
            common.require(next_cursor != cursor, "Export continuation did not advance")
            cursor = next_cursor
        formats[mode] = {
            "pages": pages,
            "records_observed": len(seen),
            "complete": complete,
            "continuation_exercised": pages > 1,
        }
    evidence["export_formats"] = formats
    evidence["export_qualification"] = (
        "Bounded own metadata serialization and observed continuation only; empty records, unexercised continuation, late settlement, marked inference and multi-entity accounting remain explicit acceptance gaps."
    )
