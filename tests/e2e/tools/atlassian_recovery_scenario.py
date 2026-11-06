"""Run and independently inspect original-operation and fixture cleanup recovery."""

from copy import deepcopy
import multiprocessing
import os

from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    OPERATION_ORDER,
    WRITES,
    digest,
    valid_native_receipt,
    valid_snapshot,
)
from tests.e2e.tools.atlassian_recovery_consumer import (
    JOURNAL,
    binding,
    consume,
    read_json,
    snapshot,
    validate_record,
)


def unrelated_snapshot(matrix, client):
    fixture = matrix.fixtures[client]
    expected = matrix.plans[client]["cells"]["get_confluence_page"]["expected"]
    observed = fixture["reader"].readback(
        **{
            key: expected[key]
            for key in (
                "run_id",
                "client",
                "tool",
                "operation_id",
                "marker",
                "site_id",
                "provider_id",
            )
        }
    )
    if observed != expected:
        raise ValueError("The original unrelated page sentinel changed")
    return observed


def observe_recovery(matrix, client):
    """Reread the consumer journal, original receipts and provider absence independently."""
    expected = binding(matrix, client)
    fixture = matrix.fixtures[client]
    root = fixture["ledger"].path.parent
    record = validate_record(read_json(root / JOURNAL), expected)
    proof, originals, _ = matrix._cleanup_proof(client)
    sealed = read_json(root / "native-finalized.json", 4096)
    if sealed != {"version": 1, "proof_sha256": proof}:
        raise ValueError("Original finalization proof differs")
    current = snapshot(matrix, client)
    owned = current["fixture"]
    provider = fixture["cleanup_provider"]
    search = provider.read_issues(
        site_id=owned["site_id"],
        project_key=owned["project_key"],
        marker=owned["marker"],
    )
    direct = {
        identifier: provider.read_issue(identifier, site_id=owned["site_id"])
        for identifier in owned["issue_ids"]
    }
    transport = read_json(root / "native-transport.json", 65536)
    journal = read_json(root / transport["reference"]["run_id"] / "attempts.json")
    return {
        "schema": "atlassian-recovery/v1",
        "handoff": read_json(root / "recovery-origin.json", 8192)
        if (root / "recovery-origin.json").exists()
        else None,
        "consumer": record,
        "current": current,
        "seal": sealed,
        "native_journal": journal,
        "operations": fixture["ledger"].state()["operations"],
        "originals": originals,
        "provider": {
            "source": "direct-atlassian-provider",
            "search": search,
            "direct": direct,
        },
        "unrelated": unrelated_snapshot(matrix, client),
        "unrelated_expected": matrix.plans[client]["cells"]["get_confluence_page"][
            "expected"
        ],
    }


def qualify_recovery(evidence, *, client, revision):
    """Pure reader: retain unknowns and reject a producer's status-only assertions."""
    if (
        client not in CLIENTS
        or type(evidence) is not dict
        or set(evidence)
        != {
            "schema",
            "handoff",
            "consumer",
            "current",
            "seal",
            "native_journal",
            "operations",
            "originals",
            "provider",
            "unrelated",
            "unrelated_expected",
        }
        or evidence["schema"] != "atlassian-recovery/v1"
    ):
        raise ValueError("Complete native recovery observations required")
    record, current = evidence["consumer"], evidence["current"]
    bound = record["binding"]
    validate_record(record, bound)
    attempts = record["attempts"]
    if (
        bound["client"] != client
        or bound["gateway_revision"] != revision
        or len(attempts) < 3
        or attempts[0]["status"] not in {"pending", "started"}
        or not attempts[0]["before"]["pending"]
        or any(attempt["status"] != "confirmed" for attempt in attempts[-2:])
        or current["pending"]
        or current["original_calls"] != bound["original_calls"]
        or any(
            attempt["before"]["original_calls"] != bound["original_calls"]
            or attempt["after"] is not None
            and attempt["after"]["original_calls"] != bound["original_calls"]
            for attempt in attempts
        )
    ):
        raise ValueError(
            "Original unknown work, failed first repair and two confirmed passes are required"
        )
    for after in [attempts[-2]["after"], attempts[-1]["after"], current]:
        if (
            after["fixture"]["cleanup_status"] != "confirmed"
            or after["pending"]
            or after["confirmed_writes"] != current["confirmed_writes"]
            or after["fixture"] != current["fixture"]
        ):
            raise ValueError(
                "Repeated cleanup did not preserve confirmed effects and ownership"
            )
    provider = evidence["provider"]
    owned = current["fixture"]
    if (
        provider
        != {
            "source": "direct-atlassian-provider",
            "search": {"complete": True, "issues": []},
            "direct": {identifier: None for identifier in owned["issue_ids"]},
        }
        or not owned["issue_ids"]
        or owned["create_status"] != "confirmed"
        or any(owned[key] != bound[key] for key in ("owner_id", "run_id"))
    ):
        raise ValueError("Owned fixture absence lacks independent direct confirmation")
    operations = evidence["operations"]
    if digest(operations) != current["operations_sha256"]:
        raise ValueError("Original operation journal changed")
    journal = evidence["native_journal"]
    if journal["binding_sha256"] != bound["transport_sha256"]:
        raise ValueError("Recovery used another native session")
    calls = {
        entry["operation_id"]: entry
        for entry in journal["attempts"]
        if entry["kind"] == "call"
    }
    if len(calls) != sum(
        entry["kind"] == "call" for entry in journal["attempts"]
    ) or set(calls) != set(bound["original_calls"]):
        raise ValueError("Recovery changed the native operation inventory")
    confirmed_writes = []
    for operation, row in operations.items():
        receipt = row.get("native_receipt")
        if (
            row["status"] != "confirmed"
            or not valid_native_receipt(receipt, row["native_binding"])
            or row["provider_observation"]
            != {**row["expected"], "provider_id": row["provider_id"]}
            or not valid_snapshot(
                row["provider_observation"],
                bound["run_id"],
                operation,
                row,
                baseline=False,
            )
        ):
            raise ValueError(
                "Original effect lacks a bound receipt and provider observation"
            )
        original = evidence["originals"][row["tool"]]
        call = calls[receipt["transport_operation_id"]]
        raw = original["original"]["private_receipt"]
        if (
            receipt != original["receipt"]
            or digest(call) != receipt["journal_entry_sha256"]
            or call["response_value_sha256"]
            != digest(
                {key: value for key, value in raw.items() if key != "operation_ref"}
            )
            or {
                key: call[key] for key in ("slot", "request_id", "request_value_sha256")
            }
            != bound["original_calls"][receipt["transport_operation_id"]]
            or raw["request_id"] != receipt["request_id"]
            or raw["root_human_id"] != bound["owner_id"]
        ):
            raise ValueError(
                "Recovered receipt differs from the original native transport"
            )
        if row["tool"] in WRITES:
            confirmed_writes.append(operation)
    if sorted(confirmed_writes) != current["confirmed_writes"] or not confirmed_writes:
        raise ValueError("Original provider mutation must remain uniquely confirmed")
    unrelated = evidence["unrelated"]
    if (
        unrelated != evidence["unrelated_expected"]
        or unrelated["tool"] != "get_confluence_page"
        or not valid_snapshot(
            unrelated,
            bound["run_id"],
            unrelated["operation_id"],
            unrelated,
            baseline=False,
        )
    ):
        raise ValueError("Unrelated sentinel observation required")
    handoff = evidence["handoff"]
    if handoff is not None:
        from tests.e2e.tools.atlassian_recovery_handoff import SCHEMA, publication_name

        if (
            type(handoff) is not dict
            or set(handoff)
            != {
                "schema",
                "publication",
                "binding_sha256",
                "archive_sha256",
                "restored_without_source",
            }
            or handoff["schema"] != SCHEMA
            or handoff["restored_without_source"] is not True
            or handoff["binding_sha256"] != digest(bound)
            or type(handoff["archive_sha256"]) is not str
            or len(handoff["archive_sha256"]) != 64
            or type(handoff["publication"]) is not dict
            or set(handoff["publication"])
            != {"uri", "version", "run_id", "attempt", "revision"}
        ):
            raise ValueError("Independent original recovery publication is incomplete")
        publication_name(
            client, handoff["publication"]["run_id"], handoff["publication"]["attempt"]
        )
    return {
        "client": client,
        "recovery": "confirmed",
        "cleanup": "confirmed",
        "runner_loss": "persisted-runner-loss"
        if handoff is not None
        else "same-filesystem-only",
        "original_operation_count": len(calls),
        "confirmed_write_count": len(confirmed_writes),
        "consumer_attempts": len(attempts),
        "evidence_sha256": digest(evidence),
    }


def interrupt_consumer(matrix, client):
    consume(matrix, client, interrupt_after_reservation=True)
    os._exit(75)


def exercise_consumer_loss(matrix, client):
    matrix.close()
    worker = multiprocessing.get_context("fork").Process(
        target=interrupt_consumer, args=(matrix, client)
    )
    worker.start()
    worker.join(timeout=30)
    if worker.is_alive():
        worker.terminate()
        worker.join(timeout=5)
        raise ValueError(
            "Owned recovery worker did not reach its durable interruption point"
        )
    if worker.exitcode != 75:
        raise ValueError("Owned recovery worker did not persist its original attempt")


def run_recovery(matrix, client, *, produce=True, handoff=None):
    """Exercise an interrupted repair and twice recover only an existing unknown run."""
    result = {
        "status": "blocked",
        "reason": "Original interrupted run and complete recovery evidence required",
        "cleanup": {"status": "pending"},
    }
    try:
        before = unrelated_snapshot(matrix, client)
        root = matrix.fixtures[client]["ledger"].path.parent
        if produce:
            if not matrix.lose_last_write_receipt:
                raise ValueError(
                    "The original matrix must enable its bounded receipt-loss fixture"
                )
            fault = root / "native-receipt-loss.json"
            if not fault.exists():
                for tool in OPERATION_ORDER:
                    cell_result = matrix.execute(client, tool)
                    if cell_result["status"] != "pass":
                        if (
                            tool == "transition_jira_issue"
                            and cell_result["status"] == "unknown"
                            and fault.exists()
                        ):
                            break
                        raise ValueError(
                            "The planned matrix did not reach its owned final-write fault"
                        )
            lost = read_json(fault, 65536)
            original = matrix.plans[client]["cells"]["transition_jira_issue"][
                "expected"
            ]["operation_id"]
            if (
                lost["operation_id"] != original
                or lost["receipt"]["tool"] != "atlassian___transition_jira_issue"
            ):
                raise ValueError(
                    "The loss fixture belongs to another original operation"
                )
        if not (root / JOURNAL).exists():
            exercise_consumer_loss(matrix, client)
        if handoff is not None:
            handoff(matrix, client)
            root = matrix.fixtures[client]["ledger"].path.parent
        attempts = read_json(root / JOURNAL)["attempts"]
        if len(attempts) < 2 or any(
            attempt["status"] != "confirmed" for attempt in attempts[-2:]
        ):
            for _ in range(2):
                consume(matrix, client)
                matrix.close()
        evidence = observe_recovery(matrix, client)
        if before != evidence["unrelated"]:
            raise ValueError("Recovery changed unrelated provider content")
        verdict = qualify_recovery(
            evidence, client=client, revision=matrix.gateway_revision
        )
        result.update(
            status="pass",
            reason="Original operation and fixture cleanup independently recovered twice",
            observations=deepcopy(evidence),
            recovery=verdict,
            cleanup={
                "status": "confirmed",
                "provider_fixtures": "confirmed",
                "execution": "confirmed",
            },
        )
    except (ValueError, OSError, KeyError, TypeError, TimeoutError):
        pass
    return result
