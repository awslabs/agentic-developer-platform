"""Record keys, lifecycle and digests against the frozen T0 contract (#5794).

The point of the contract-agreement tests here is that the *contract file* is the
authority, not this test's own literals. A namespace typo or a changed fixed width
in ``records.py`` fails against
``docs/task-api/contracts/v1/identity-and-lifecycle.json`` rather than against a
hand-copied expectation that could drift with the code it checks.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.tasks import records
from src.tasks.records import (
    OMITTED_LEGACY_GSI_ATTRIBUTES,
    PERMITTED_TRANSITIONS,
    TASK_NAMESPACES,
    TaskRecordError,
    TaskState,
    TaskTransitionError,
    assert_legacy_invisible,
    base_item,
    canonical_json,
    command_sort_key,
    component_digest,
    dispatch_sort_key,
    event_sort_key,
    idempotency_partition,
    is_terminal,
    model_operation_sort_key,
    payload_digest,
    report_sort_key,
    run_sort_key,
    task_partition,
    turn_sort_key,
    validate_transition,
    work_due_key,
    work_shard,
)

CONTRACT_PATH = Path(__file__).resolve().parents[4] / "docs/task-api/contracts/v1/identity-and-lifecycle.json"

TASK_ID = "tsk_2f1c9d7a-3b4e-4c5d-8e9f-0a1b2c3d4e5f"
INVOCATION_ID = "9c8b7a6d-5e4f-4a3b-9c8d-7e6f5a4b3c2d"
COMMAND_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
TURN_ID = "0f1e2d3c-4b5a-4968-8778-6a5b4c3d2e1f"
DISPATCH_ID = "abcdef01-2345-4678-89ab-cdef01234567"
ARTIFACT_ID = "art_11112222-3333-4444-8555-666677778888"

SCOPE = {"tenant": "tenant-a", "canonical_principal": "svc-principal-1"}


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text())


# ---------------------------------------------------------------------------
# Contract agreement: the frozen file drives these, not local literals
# ---------------------------------------------------------------------------


def test_record_namespaces_match_the_frozen_contract(contract):
    """Every design record type is implemented, and nothing extra is invented."""
    expected = {entry["record_type"] for entry in contract["storage_records"]["records"]}
    assert set(TASK_NAMESPACES) == expected


def test_worker_denied_prefixes_cover_every_namespace(contract):
    """The IAM deny list and the code's namespace list cannot drift apart.

    If a later story adds a namespace without adding its deny, that namespace is
    writable by the worker role — the exact integrity hole T1-AC04 is about.
    """
    denied = set(contract["storage_records"]["worker_direct_write_denied_prefixes"])
    assert {f"{namespace}#" for namespace in TASK_NAMESPACES} == denied


def test_key_forms_match_the_contract_table(contract):
    """Each built key matches the design's documented form for that record."""
    forms = {entry["record_type"]: (entry["event_id"], entry["arrived_at"]) for entry in contract["storage_records"]["records"]}

    built = {
        "TASK": (task_partition(TASK_ID), records.META_SORT_KEY),
        "TASK_RUN": (records.task_run_partition(TASK_ID), run_sort_key(invocation_id=INVOCATION_ID, generation=1)),
        "TASK_EVENTS": (records.task_events_partition(TASK_ID), event_sort_key(1)),
        "TASK_COMMANDS": (records.task_commands_partition(TASK_ID), command_sort_key(COMMAND_ID)),
        "TASK_TURNS": (records.task_turns_partition(TASK_ID), turn_sort_key(1)),
        "TASK_OPS": (records.task_ops_partition(TASK_ID), model_operation_sort_key(TURN_ID)),
        "TASK_WORK": (records.task_work_partition(TASK_ID), dispatch_sort_key(DISPATCH_ID)),
        "TASK_REPORT": (records.task_report_partition(TASK_ID), report_sort_key(generation=1, report_id=DISPATCH_ID)),
        "TASK_ARTIFACT": (records.task_artifact_partition(ARTIFACT_ID), records.META_SORT_KEY),
    }

    for record_type, (partition, sort_key) in built.items():
        expected_partition, expected_sort = forms[record_type]
        # Translate the documented form into a regex: <...> placeholders become
        # permissive groups, everything else must match literally.
        assert re.fullmatch(_form_to_regex(expected_partition), partition), f"{record_type} partition {partition}"
        assert re.fullmatch(_form_to_regex(expected_sort.split(" or ")[0]), sort_key), f"{record_type} sort {sort_key}"


def _form_to_regex(form: str) -> str:
    return "".join(r"[^#]+" if part.startswith("<") else re.escape(part) for part in re.split(r"(<[^>]+>)", form))


def test_idempotency_partition_matches_contract_form(contract):
    partition = idempotency_partition(tenant="tenant-a", canonical_principal="svc-1", idempotency_key="key-1")
    assert re.fullmatch(r"TASK_IDEMP#[0-9a-f]{64}", partition)


def test_lifecycle_transitions_match_the_frozen_contract(contract):
    """The permitted-transition table is exactly the design's, state for state."""
    expected = {state: set(body["permitted"]) for state, body in contract["lifecycle"]["transitions"].items()}
    actual = {state.value: {target.value for target in targets} for state, targets in PERMITTED_TRANSITIONS.items()}
    assert actual == expected


def test_terminal_states_match_the_contract(contract):
    expected = set(contract["lifecycle"]["terminal_states"])
    assert {state.value for state in TaskState if is_terminal(state)} == expected


def test_omitted_legacy_attributes_match_the_contract(contract):
    assert OMITTED_LEGACY_GSI_ATTRIBUTES == set(contract["storage_records"]["omitted_legacy_gsi_attributes"])


def test_work_index_attributes_match_the_contract(contract):
    index = contract["storage_records"]["work_index"]
    assert records.WORK_INDEX_NAME == index["name"]
    assert records.WORK_SHARD_ATTRIBUTE == index["partition_attribute"]
    assert records.WORK_DUE_ATTRIBUTE == index["sort_attribute"]


# ---------------------------------------------------------------------------
# Fixed-width ordering: the property that makes replay correct
# ---------------------------------------------------------------------------


def test_event_sort_keys_order_lexicographically_across_digit_boundaries():
    """String sort order must equal numeric order, or replay emits out of order."""
    sequences = [1, 2, 9, 10, 11, 99, 100, 1000, 999999]
    keys = [event_sort_key(n) for n in sequences]
    assert keys == sorted(keys)


def test_turn_and_generation_keys_are_fixed_width():
    assert turn_sort_key(7).endswith("0" * 19 + "7")
    assert run_sort_key(invocation_id=INVOCATION_ID, generation=3).endswith("0000000003")


def test_work_due_keys_order_chronologically_as_strings():
    early = work_due_key(due_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC), work_id="a")
    later = work_due_key(due_at=datetime(2026, 9, 24, 10, 0, 1, tzinfo=UTC), work_id="a")
    assert early < later


def test_work_due_key_requires_timezone_aware_input():
    """A naive datetime would be interpreted in local time and mis-schedule work."""
    with pytest.raises(TaskRecordError, match="timezone-aware"):
        work_due_key(due_at=datetime(2026, 9, 24, 10, 0), work_id="a")  # noqa: DTZ001 — the condition under test


@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.0])
def test_ordering_numbers_reject_non_positive_integers(bad):
    with pytest.raises(TaskRecordError):
        event_sort_key(bad)


def test_sequence_exceeding_fixed_width_is_refused():
    """Silently truncating or overflowing the width would corrupt ordering."""
    with pytest.raises(TaskRecordError, match="fixed width"):
        event_sort_key(10**21)


def test_work_shard_is_stable_and_within_range():
    shard = work_shard(TASK_ID)
    assert shard == work_shard(TASK_ID)
    assert re.fullmatch(r"v1#(0\d|1[0-5])", shard)


def test_work_shards_distribute_across_partitions():
    """A single hot shard would defeat the purpose of sharding recovery work."""
    ids = [f"tsk_{i:08x}-0000-4000-8000-000000000000" for i in range(256)]
    assert len({work_shard(task_id) for task_id in ids}) > 1


# ---------------------------------------------------------------------------
# Identifier validation: a key must not be forgeable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "tsk_not-a-uuid",
        "2f1c9d7a-3b4e-4c5d-8e9f-0a1b2c3d4e5f",  # missing tsk_ prefix
        "tsk_2f1c9d7a-3b4e-1c5d-8e9f-0a1b2c3d4e5f",  # not version 4
        "tsk_2f1c9d7a-3b4e-4c5d-8e9f-0a1b2c3d4e5f#META",  # delimiter injection
        "",
        None,
    ],
)
def test_task_partition_rejects_malformed_task_ids(bad):
    """An unvalidated ID containing '#' could forge another record's key."""
    with pytest.raises(TaskRecordError):
        task_partition(bad)


def test_command_key_rejects_non_uuid():
    with pytest.raises(TaskRecordError):
        command_sort_key("../../etc/passwd")


# ---------------------------------------------------------------------------
# Lifecycle enforcement
# ---------------------------------------------------------------------------


def test_every_state_pair_matches_the_permitted_table():
    """Exhaustive: exactly the permitted pairs are accepted, all others refused."""
    for current in TaskState:
        for target in TaskState:
            if target in PERMITTED_TRANSITIONS[current]:
                validate_transition(current, target)
            else:
                with pytest.raises(TaskTransitionError):
                    validate_transition(current, target)


def test_cancel_requested_can_never_become_completed():
    """The design's explicit honesty rule: a cancelled task is never a success."""
    with pytest.raises(TaskTransitionError):
        validate_transition(TaskState.CANCEL_REQUESTED, TaskState.COMPLETED)


@pytest.mark.parametrize("terminal", [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED])
def test_terminal_outcomes_are_immutable(terminal):
    for target in TaskState:
        with pytest.raises(TaskTransitionError, match="terminal outcome"):
            validate_transition(terminal, target)


# ---------------------------------------------------------------------------
# Digests: stability for equal payloads, separation for different ones
# ---------------------------------------------------------------------------


def test_digest_is_stable_across_key_order_and_formatting():
    """Idempotent replay depends on this: same request, same digest."""
    a = {"persona": "agent-task-investigator", "instructions": "look", "inputs": {"b": 1, "a": 2}}
    b = {"inputs": {"a": 2, "b": 1}, "instructions": "look", "persona": "agent-task-investigator"}
    assert payload_digest(a) == payload_digest(b)


def test_digest_differs_when_a_value_changes():
    base = {"instructions": "look"}
    assert payload_digest(base) != payload_digest({"instructions": "look "})


def test_canonical_json_sorts_keys_and_omits_whitespace():
    assert canonical_json({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'


def test_canonical_json_preserves_unicode_unescaped():
    """RFC 8785 does not \\u-escape beyond JSON's minimum."""
    assert canonical_json({"k": "café"}) == '{"k":"café"}'.encode()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_numbers(bad):
    """The API rejects these; emitting NaN would not even be valid JSON."""
    with pytest.raises(TaskRecordError, match="nonfinite"):
        canonical_json({"n": bad})


def test_canonical_json_rejects_non_integral_floats():
    """Refuse rather than emit a digest another implementation would not reproduce."""
    with pytest.raises(TaskRecordError, match="non-integral"):
        canonical_json({"n": 1.5})


def test_nonfinite_numbers_are_rejected_when_nested():
    with pytest.raises(TaskRecordError, match="nonfinite"):
        canonical_json({"outer": [{"inner": float("inf")}]})


def test_component_digest_is_length_delimited_not_concatenated():
    """The collision that plain concatenation would allow must not occur.

    ('ab','c') and ('a','bc') concatenate to the same bytes. If the idempotency
    scope hashed that way, one tenant's key could collide with another tenant's.
    """
    assert component_digest("v", "ab", "c") != component_digest("v", "a", "bc")


def test_component_digest_separates_by_version_label():
    assert component_digest("v1", "x") != component_digest("v2", "x")


def test_idempotency_scope_separates_tenant_principal_and_key():
    """Each of the three scope components independently changes the partition."""
    base = idempotency_partition(tenant="t1", canonical_principal="p1", idempotency_key="k1")
    assert base != idempotency_partition(tenant="t2", canonical_principal="p1", idempotency_key="k1")
    assert base != idempotency_partition(tenant="t1", canonical_principal="p2", idempotency_key="k1")
    assert base != idempotency_partition(tenant="t1", canonical_principal="p1", idempotency_key="k2")


def test_idempotency_partition_cannot_be_collided_by_a_delimiter_in_a_component():
    """A '#' inside a tenant must not let it impersonate a tenant/principal split."""
    a = idempotency_partition(tenant="t#p", canonical_principal="x", idempotency_key="k")
    b = idempotency_partition(tenant="t", canonical_principal="p#x", idempotency_key="k")
    assert a != b


def test_idempotency_partition_requires_all_scope_components():
    with pytest.raises(TaskRecordError):
        idempotency_partition(tenant="", canonical_principal="p", idempotency_key="k")


# ---------------------------------------------------------------------------
# Legacy invisibility at the item level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attribute", sorted(OMITTED_LEGACY_GSI_ATTRIBUTES))
def test_items_carrying_a_legacy_gsi_attribute_are_refused(attribute):
    """Each legacy index hash key, individually, must block the write."""
    with pytest.raises(TaskRecordError, match="legacy GSI"):
        assert_legacy_invisible({"event_id": "TASK#x", attribute: "value"})


def test_base_item_carries_required_attributes_and_nested_scope():
    item = base_item(partition=task_partition(TASK_ID), sort_key="META", record_type="TASK", scope=SCOPE)
    assert item["record_type"] == "TASK"
    assert item["schema_version"] == "1.0"
    # Tenant travels nested, where no GSI on this table can reach it.
    assert item["scope"]["tenant"] == "tenant-a"
    assert not OMITTED_LEGACY_GSI_ATTRIBUTES.intersection(item)


def test_base_item_requires_tenant_and_principal_scope():
    with pytest.raises(TaskRecordError, match="scope"):
        base_item(partition=task_partition(TASK_ID), sort_key="META", record_type="TASK", scope={"tenant": "t"})


def test_base_item_rejects_an_unknown_record_type():
    with pytest.raises(TaskRecordError, match="unknown task record_type"):
        base_item(partition="OTHER#1", sort_key="META", record_type="WEBHOOK", scope=SCOPE)
