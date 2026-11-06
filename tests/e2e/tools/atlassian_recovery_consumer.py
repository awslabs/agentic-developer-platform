"""Consume original native work after runner loss without authorizing new calls."""

import argparse
import fcntl
import json
import os
import stat
from tempfile import mkstemp

from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    OPERATION_ORDER,
    WRITES,
    digest,
    load_isolated_fixtures,
)
from tests.e2e.tools.atlassian_native_matrix import NativeMatrix, load_native_plans
from tests.e2e.tools.atlassian_setup_live import private_file


JOURNAL = "cleanup-consumer.json"
MAX_ATTEMPTS = 16


def read_json(path, maximum=262144):
    from tests.e2e.tools.atlassian_flow_summary import _decode

    return _decode(private_file(path, max_bytes=maximum))


def save(path, value):
    descriptor, temporary = mkstemp(prefix=".cleanup-consumer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                value, stream, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def original_calls(root):
    transport = read_json(root / "native-transport.json", 65536)
    run_id = transport["reference"]["run_id"]
    from uuid import UUID

    if str(UUID(run_id)) != run_id:
        raise ValueError("Original native run is unavailable")
    journal = read_json(root / run_id / "attempts.json")
    if journal.get("binding_sha256") != digest(transport):
        raise ValueError("Original native journal binding differs")
    calls = {}
    for entry in journal["attempts"]:
        if entry["kind"] == "call":
            operation = entry["operation_id"]
            if operation in calls:
                raise ValueError("Duplicate original native call")
            calls[operation] = {
                key: entry[key]
                for key in ("slot", "request_id", "request_value_sha256")
            }
    if not calls:
        raise ValueError("Recovery requires previously attempted native work")
    return digest(transport), calls


def binding(matrix, client):
    fixture = matrix.fixtures[client]
    root = fixture["ledger"].path.parent
    transport, calls = original_calls(root)
    return {
        "client": client,
        "run_id": fixture["ledger"].run_id,
        "owner_id": fixture["owner_id"],
        "gateway_revision": matrix.gateway_revision,
        "gateway_sha256": digest(matrix.gateway_url),
        "transport_sha256": transport,
        "plan_sha256": digest(matrix.plans[client]),
        "original_calls": calls,
    }


def protected_bindings(manifest_file, *, gateway_url, gateway_revision):
    fixtures = load_isolated_fixtures(manifest_file)
    result = {}
    for client, fixture in fixtures.items():
        root = fixture["ledger"].path.parent
        transport, calls = original_calls(root)
        metadata = read_json(root / "native-runtime.json", 65536)
        reference = read_json(root / "native-transport.json", 65536)["reference"]
        if (
            metadata["owner_id"] != fixture["owner_id"]
            or metadata["run_id"] != fixture["ledger"].run_id
            or metadata["gateway_revision"] != gateway_revision
            or metadata["transport_sha256"] != transport
            or reference["gateway_origin"] != gateway_url
            or reference["source_revision"] != gateway_revision
        ):
            raise ValueError(
                "Protected recovery target differs from the original native admission"
            )
        result[client] = {
            "client": client,
            "run_id": fixture["ledger"].run_id,
            "owner_id": fixture["owner_id"],
            "gateway_revision": gateway_revision,
            "gateway_sha256": digest(gateway_url),
            "transport_sha256": transport,
            "original_calls": calls,
        }
    return result


def validate_record(record, expected):
    if (
        type(record) is not dict
        or set(record) != {"version", "binding", "attempts"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["binding"] != expected
        or type(record["attempts"]) is not list
        or len(record["attempts"]) > MAX_ATTEMPTS
    ):
        raise ValueError("Original recovery consumer journal differs")
    for number, attempt in enumerate(record["attempts"], 1):
        if (
            type(attempt) is not dict
            or set(attempt) != {"number", "before", "after", "status"}
            or type(attempt["number"]) is not int
            or attempt["number"] != number
            or attempt["status"] not in {"started", "pending", "confirmed"}
            or type(attempt["before"]) is not dict
            or (attempt["after"] is None) != (attempt["status"] == "started")
        ):
            raise ValueError("Recovery consumer attempt history is invalid")
    return record


def snapshot(matrix, client):
    fixture = matrix.fixtures[client]
    operations = fixture["ledger"].state()["operations"]
    return {
        "operations_sha256": digest(operations),
        "pending": sorted(
            operation
            for operation, row in operations.items()
            if row["status"] == "pending"
        ),
        "confirmed_writes": sorted(
            operation
            for operation, row in operations.items()
            if row["status"] == "confirmed" and row["tool"] in WRITES
        ),
        "fixture": matrix.fixture_ledgers[client].state(),
        "original_calls": original_calls(fixture["ledger"].path.parent)[1],
    }


def consume(matrix, client, *, interrupt_after_reservation=False):
    """One bounded repair pass; unattempted cells never enter the execution path."""
    if client not in CLIENTS or client not in matrix.fixture_ledgers:
        raise ValueError(
            "Original owner-bound native fixture and cleanup authority required"
        )
    root = matrix.fixtures[client]["ledger"].path.parent
    descriptor = os.open(
        root / "cleanup-consumer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Private recovery consumer lock required")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        matrix._resume(client)
        expected = binding(matrix, client)
        path = root / JOURNAL
        record = (
            validate_record(read_json(path), expected)
            if path.exists()
            else {"version": 1, "binding": expected, "attempts": []}
        )
        if len(record["attempts"]) >= MAX_ATTEMPTS:
            raise ValueError("Original cleanup consumer attempt limit reached")
        attempt = {
            "number": len(record["attempts"]) + 1,
            "before": snapshot(matrix, client),
            "after": None,
            "status": "started",
        }
        record["attempts"].append(attempt)
        save(path, record)
        if interrupt_after_reservation:
            return {
                "status": "pending",
                "attempt": attempt["number"],
                "journal_sha256": digest(record),
            }
        try:
            for tool in OPERATION_ORDER:
                operation_id = matrix.plans[client]["cells"][tool]["expected"][
                    "operation_id"
                ]
                original = (
                    matrix.fixtures[client]["ledger"]
                    .state()["operations"]
                    .get(operation_id)
                )
                if original is not None and original["status"] == "pending":
                    matrix.execute(client, tool)
            result = matrix.finalize(client)
            attempt["after"] = snapshot(matrix, client)
            if attempt["after"]["original_calls"] != expected["original_calls"]:
                raise ValueError("Cleanup consumer introduced new native work")
            attempt["status"] = (
                "confirmed"
                if result.get("status") == "confirmed"
                and not attempt["after"]["pending"]
                else "pending"
            )
        except (ValueError, OSError, KeyError, TimeoutError):
            attempt["after"] = snapshot(matrix, client)
            attempt["status"] = "pending"
        save(path, record)
        return {
            "status": attempt["status"],
            "attempt": attempt["number"],
            "journal_sha256": digest(record),
        }
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--plans")
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--client", choices=sorted(CLIENTS), required=True)
    args = parser.parse_args(argv)
    from tests.e2e.tools.runner import MCPClient

    matrix = None
    try:
        from pathlib import Path
        from tests.e2e.tools.atlassian_client_matrix import recovery_roots

        fixtures = load_isolated_fixtures(args.manifest)
        restored = recovery_roots(Path(args.manifest).parent)
        if args.client in restored:
            plans = {
                args.client: read_json(restored[args.client] / "recovery-plan.json")
            }
            journal = read_json(restored[args.client] / JOURNAL)
            origin = read_json(restored[args.client] / "recovery-origin.json", 8192)
            if (
                digest(plans[args.client]) != journal["binding"]["plan_sha256"]
                or digest(journal["binding"]) != origin["binding_sha256"]
            ):
                raise ValueError("Restored plan is not the original plan")
            fixtures = {args.client: fixtures[args.client]}
        else:
            if not args.plans:
                raise ValueError(
                    "Original native plans are required before local recovery"
                )
            plans = load_native_plans(args.plans)
        matrix = NativeMatrix(
            plans,
            fixtures,
            gateway_url=args.gateway,
            gateway_revision=args.revision,
            client_factory=MCPClient,
            recovery_client=args.client if args.client in restored else None,
        )
        result = consume(matrix, args.client)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "confirmed" else 3
    except (OSError, ValueError, KeyError, TimeoutError):
        print(
            json.dumps(
                {
                    "status": "pending",
                    "reason": "original recovery or cleanup is unconfirmed",
                }
            )
        )
        return 3
    finally:
        if matrix is not None:
            matrix.close()


if __name__ == "__main__":
    raise SystemExit(main())
