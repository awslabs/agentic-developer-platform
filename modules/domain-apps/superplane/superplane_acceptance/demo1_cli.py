"""Demo 1 fixture driver and guarded live-selection preflight."""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .demo1_c1 import C1_CHECKPOINT, retirement_preview
from .demo1_evidence import DemoInput, EvidenceError, link_phases
from .demo1_report import assemble_report

MAX_PRIVATE_BYTES = 262_144


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("input: duplicate JSON member")
        result[key] = value
    return result


def _read_private(filename: str) -> object:
    path = Path(filename)
    if not path.is_absolute():
        raise EvidenceError("input: absolute private file required")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
                or not 0 < metadata.st_size <= MAX_PRIVATE_BYTES
            ):
                raise EvidenceError(
                    "input: private regular file with mode 0600 required"
                )
            raw = stream.read(MAX_PRIVATE_BYTES + 1)
        if len(raw) > MAX_PRIVATE_BYTES:
            raise EvidenceError("input: private file exceeds size limit")
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (OSError, ValueError, UnicodeError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("input: private file unreadable or invalid JSON") from None


def _fixture(value: object) -> tuple[list[object], tuple[str, ...], object, object]:
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {
                "version",
                "phases",
                "owned_resources",
                "inventory",
            },
            {"version", "phases", "owned_resources", "inventory", "retirement"},
        )
        or value["version"] != "demo1-fixture-v1"
    ):
        raise EvidenceError("fixture: unsupported version or fields")
    records = value["phases"]
    resources = value["owned_resources"]
    if not isinstance(records, list) or not isinstance(resources, list):
        raise EvidenceError("fixture: invalid records or resource baseline")
    if (
        len(records) > 128
        or len(resources) > 256
        or any(not isinstance(resource, str) for resource in resources)
    ):
        raise EvidenceError("fixture: invalid resource baseline")
    inventory = value["inventory"]
    if inventory is not None and not isinstance(inventory, dict):
        raise EvidenceError("fixture: invalid provider observation")
    retirement = value.get("retirement")
    if retirement is not None and (
        not isinstance(retirement, dict)
        or set(retirement)
        != {
            "status_code",
            "operation_id",
            "body",
        }
        or type(retirement["status_code"]) is not int
    ):
        raise EvidenceError("fixture: invalid retirement review observation")
    return records, tuple(resources), inventory, retirement


def _criteria(scenario: dict) -> dict:
    status = scenario["overall"]
    return {
        "AC-01": {
            "status": "FAIL"
            if status == "FAIL"
            else "NOT RUN"
            if status == "NOT RUN"
            else "BLOCKED",
            "reason": "offline diagnostics cannot satisfy live authority",
        },
        "AC-02": {
            "status": "BLOCKED",
            "reason": "browser and removal integration not verified",
        },
        "AC-03": {
            "status": "NOT RUN",
            "reason": "run final-head domain and applicable shared CI separately",
        },
        "AC-04": {
            "status": "NOT RUN",
            "reason": "foreground review/merge and live evaluation remain separate",
        },
    }


def _publish(document: dict, filename: str) -> None:
    path = Path(filename)
    if not path.is_absolute() or not path.parent.is_dir() or path.parent.is_symlink():
        raise EvidenceError(
            "report: absolute new file in an existing directory required"
        )
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".demo1-report-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    except OSError:
        raise EvidenceError("report: could not publish to a new private file") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline dedicated-workspace acceptance diagnostics"
    )
    parser.add_argument("--mode", choices=("fixture", "live"), required=True)
    parser.add_argument("--private-input")
    parser.add_argument("--fixture")
    parser.add_argument("--report")
    parser.add_argument("--authority")
    parser.add_argument("--browser-state")
    parser.add_argument("--checkpoint")
    arguments = parser.parse_args(argv)
    if arguments.mode == "live":
        try:
            if arguments.fixture or not all(
                (
                    arguments.private_input,
                    arguments.authority,
                    arguments.browser_state,
                    arguments.checkpoint,
                    arguments.report,
                )
            ):
                raise EvidenceError(
                    "live: private selection, authority, browser state, checkpoint and report required"
                )
            from .demo1_live import LiveEnvelope, PrivateCheckpoint, preflight_report
            from .demo1_report import reference

            selected = DemoInput.parse(_read_private(arguments.private_input))
            envelope = LiveEnvelope.parse(_read_private(arguments.authority), selected)
            session = _read_private(arguments.browser_state)
            report = preflight_report(selected, envelope, session)
            with PrivateCheckpoint(
                arguments.checkpoint, selected, envelope.origin
            ) as store:
                saved = store.load()
                if saved is not None:
                    report["checkpoint"] = {
                        "request_ref": reference(saved.request_id),
                        "workspace_ref": reference(saved.workspace_id),
                        "approval_ref": reference(saved.approval_id),
                        "submitted": saved.submitted,
                    }
                _publish(report, arguments.report)
        except EvidenceError as error:
            print(f"BLOCKED: {error}")
            return 2
        print(
            "Live selection preflight: BLOCKED (no runtime admission or release verification)"
        )
        return 2
    try:
        if not all((arguments.private_input, arguments.fixture, arguments.report)):
            raise EvidenceError(
                "fixture: private input, fixture and new report path required"
            )
        selected = DemoInput.parse(_read_private(arguments.private_input))
        records, expected_owned, inventory, retirement = _fixture(
            _read_private(arguments.fixture)
        )
        reader = (
            None
            if inventory is None
            else SimpleNamespace(
                read_inventory=lambda query: inventory,
            )
        )
        scenario = assemble_report(
            selected,
            records,
            expected_owned=expected_owned,
            reader=reader,
        )
        if retirement is not None and scenario["workspace_ref"] is not None:
            try:
                phases = link_phases(selected, records)
                review = retirement_preview(
                    selected,
                    phases[0].workspace_id,
                    phases[0].operation_id,
                    retirement["operation_id"],
                    retirement["status_code"],
                    retirement["body"],
                )
            except EvidenceError as error:
                scenario["checks"]["removal"] = {"status": "FAIL", "detail": str(error)}
                scenario["overall"] = "FAIL"
            else:
                scenario["checks"]["removal"] = {
                    "status": "BLOCKED",
                    "detail": review["reason"],
                }
                scenario["retirement_preview"] = review
        document = {
            "version": "demo1-cli-v1",
            "contract_checkpoint": C1_CHECKPOINT,
            "scenario": scenario,
            "criteria": _criteria(scenario),
        }
        _publish(document, arguments.report)
    except EvidenceError as error:
        print(f"BLOCKED: {error}")
        return 2
    print(f"Fixture diagnostics: {scenario['overall']} (not live acceptance)")
    return 1 if scenario["overall"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
