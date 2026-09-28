"""Tests for security_agent_ledger.py (issue #4441, unit U2).

Gate coverage: NT-6 concurrency, FR-C28 path, FR-C29 no shared-object write,
FR-C30 idempotency, FR-C31 shard field ownership, FR-C32 reconciliation,
FR-C36 finalization, FR-C37 both stuck paths, NT-11 write-side allow-list.
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from security_agent_ledger import (
    LedgerError,
    build_shard,
    evaluate_story_status,
    is_final,
    load_and_merge,
    load_schema,
    load_shards,
    merge_shards,
    put_shard,
    reconcile,
    run_prefix,
    serialize_shard,
    shard_key,
    status_counts,
    validate_shard,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = load_schema()
TS = "2026-08-30T02:41:00Z"


class FakeS3:
    """Records put_object calls. Deliberately has NO get_object: a test double
    cannot serve a read the production code is not allowed to perform."""

    def __init__(self):
        self.objects = {}
        self.put_calls = []

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803
        self.put_calls.append(Key)
        self.objects[(Bucket, Key)] = Body


def _workflow_shard(stage="workflow.code-review", raw=7):
    return build_shard(
        "2026-08-30",
        stage,
        TS,
        {"identified_raw": raw, "identified_new_after_dedup": 1},
        SCHEMA,
    )


def _ops_shard(story, status, failed_runs=0, reason=None, ts=TS):
    record = {
        "status": status,
        "failed_runs": failed_runs,
        "last_transition_at": ts,
    }
    if reason is not None:
        record["reason"] = reason
    return build_shard(
        "2026-08-30", f"ops.{story}", ts, {"story_status": {str(story): record}}, SCHEMA
    )


# ---------------------------------------------------------------- schema/paths


def test_schema_declares_merge_rule_and_owner_for_every_field():
    """Every field is merge-able and owned. A field missing either would blow
    up at merge time on a real run instead of here."""
    valid_rules = {"sum", "max", "unique-list", "single", "map-union"}
    stage_types = set(SCHEMA["properties"]["stage_type"]["enum"])
    for name, spec in SCHEMA["x-fields"].items():
        assert spec["merge"] in valid_rules, name
        assert spec["stages"], name
        assert set(spec["stages"]) <= stage_types, name


def test_fr_c31_schema_covers_every_required_shard_field():
    """FR-C31: the required field set is present and owned by the right stage."""
    expected = {
        "identified_raw": "workflow",
        "identified_new_after_dedup": "workflow",
        "run_duration_seconds": "workflow",
        "pentest_cost_usd": "workflow",
        "daily_epic": "triage",
        "stories_created": "triage",
        "story_ids": "triage",
        "planned_sequence": "orchestration",
        "story_status": "ops",
    }
    for field, owner in expected.items():
        assert field in SCHEMA["x-fields"], field
        assert owner in SCHEMA["x-fields"][field]["stages"], field


def test_fr_c28_run_prefix_and_key_layout():
    """FR-C28: state lives under security-agent/runs/<YYYY-MM-DD>/."""
    assert run_prefix("2026-08-30") == "security-agent/runs/2026-08-30"
    assert (
        shard_key("2026-08-30", "workflow.pentest")
        == "security-agent/runs/2026-08-30/shard-workflow.pentest.json"
    )


def test_distinct_stages_produce_distinct_keys():
    """The property the whole sharding design rests on."""
    keys = {
        shard_key("2026-08-30", s)
        for s in ["workflow.code-review", "workflow.pentest", "triage", "ops.5002"]
    }
    assert len(keys) == 4


# ---------------------------------------------------------------- validation


def test_validate_rejects_field_outside_schema():
    """NT-11 write side: an undeclared field cannot enter the ledger."""
    with pytest.raises(LedgerError, match="not in the schema"):
        build_shard("2026-08-30", "workflow", TS, {"exploit_detail": "curl ..."}, SCHEMA)


def test_validate_rejects_field_owned_by_another_stage():
    """FR-C31: ops cannot write triage's counts."""
    with pytest.raises(LedgerError, match="may not write field"):
        build_shard("2026-08-30", "ops.5002", TS, {"stories_created": 3}, SCHEMA)


def test_validate_rejects_unknown_envelope_key():
    shard = _workflow_shard()
    shard["notes"] = "anything"
    with pytest.raises(LedgerError, match="outside the schema"):
        validate_shard(shard, SCHEMA)


def test_validate_rejects_missing_required_key():
    shard = _workflow_shard()
    del shard["generated_at"]
    with pytest.raises(LedgerError, match="missing required fields"):
        validate_shard(shard, SCHEMA)


def test_validate_rejects_stage_type_mismatch():
    shard = _workflow_shard()
    shard["stage_type"] = "triage"
    with pytest.raises(LedgerError, match="does not belong to stage_type"):
        validate_shard(shard, SCHEMA)


@pytest.mark.parametrize(
    "key,value",
    [
        ("run_date", "30-08-2026"),
        ("stage", "Workflow"),
        ("generated_at", "2026-08-30 02:41"),
    ],
)
def test_validate_rejects_malformed_envelope_values(key, value):
    shard = _workflow_shard()
    shard[key] = value
    with pytest.raises(LedgerError):
        validate_shard(shard, SCHEMA)


def test_validate_rejects_bad_schema_version():
    shard = _workflow_shard()
    shard["schema_version"] = "2"
    with pytest.raises(LedgerError, match="unsupported schema_version"):
        validate_shard(shard, SCHEMA)


def test_validate_rejects_bad_stage_type_enum():
    shard = _workflow_shard()
    shard["stage"] = "audit"
    shard["stage_type"] = "audit"
    with pytest.raises(LedgerError):
        validate_shard(shard, SCHEMA)


def test_validate_rejects_non_object_shard_and_fields():
    with pytest.raises(LedgerError, match="must be an object"):
        validate_shard(["not", "a", "shard"], SCHEMA)
    shard = _workflow_shard()
    shard["fields"] = []
    with pytest.raises(LedgerError, match="fields must be an object"):
        validate_shard(shard, SCHEMA)


@pytest.mark.parametrize("bad", [-1, "3", True, 2.5])
def test_validate_rejects_bad_integer_counts(bad):
    """A bool is an int in Python; a bool count is a caller bug, not a count."""
    with pytest.raises(LedgerError):
        build_shard("2026-08-30", "workflow", TS, {"identified_raw": bad}, SCHEMA)


@pytest.mark.parametrize("bad", [-0.5, "12.50", True])
def test_validate_rejects_bad_cost(bad):
    with pytest.raises(LedgerError):
        build_shard("2026-08-30", "workflow", TS, {"pentest_cost_usd": bad}, SCHEMA)


def test_validate_accepts_integer_cost():
    """A whole-dollar cost is a valid number."""
    shard = build_shard("2026-08-30", "workflow", TS, {"pentest_cost_usd": 12}, SCHEMA)
    assert shard["fields"]["pentest_cost_usd"] == 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("new_finding_ids", "f-a1c2"),
        ("new_finding_ids", [123]),
        ("story_ids", ["5002"]),
    ],
)
def test_validate_rejects_bad_arrays(field, value):
    stage = "workflow" if field == "new_finding_ids" else "triage"
    with pytest.raises(LedgerError):
        build_shard("2026-08-30", stage, TS, {field: value}, SCHEMA)


def test_validate_rejects_story_status_not_keyed_by_number():
    with pytest.raises(LedgerError, match="keys must be story numbers"):
        build_shard(
            "2026-08-30",
            "ops.5002",
            TS,
            {"story_status": {"story-5002": {"status": "fixed", "failed_runs": 0,
                                            "last_transition_at": TS}}},
            SCHEMA,
        )


def test_validate_rejects_story_status_extra_field():
    """NT-11: free-form text on a story record is the leak path."""
    with pytest.raises(LedgerError, match="outside the schema"):
        build_shard(
            "2026-08-30",
            "ops.5002",
            TS,
            {"story_status": {"5002": {"status": "fixed", "failed_runs": 0,
                                       "last_transition_at": TS,
                                       "notes": "POST /admin with ..."}}},
            SCHEMA,
        )


def test_validate_rejects_stuck_without_reason():
    """FR-C37: a stuck story with no reason makes the report unactionable."""
    with pytest.raises(LedgerError, match="records no reason"):
        _ops_shard(5004, "stuck", failed_runs=3)


def test_validate_rejects_reason_on_non_stuck_story():
    with pytest.raises(LedgerError, match="stamps reason"):
        _ops_shard(5002, "fixed", reason="failed_run_limit")


def test_validate_rejects_free_text_reason():
    with pytest.raises(LedgerError, match="invalid reason"):
        _ops_shard(5004, "stuck", failed_runs=3, reason="agent gave up on /admin")


def test_validate_rejects_invalid_story_status_value():
    with pytest.raises(LedgerError, match="invalid status"):
        _ops_shard(5002, "merged")


def test_validate_rejects_non_object_story_record_and_missing_keys():
    with pytest.raises(LedgerError, match="must be an object"):
        build_shard("2026-08-30", "ops.5002", TS, {"story_status": {"5002": "fixed"}}, SCHEMA)
    with pytest.raises(LedgerError, match="missing"):
        build_shard(
            "2026-08-30", "ops.5002", TS, {"story_status": {"5002": {"status": "fixed"}}}, SCHEMA
        )
    with pytest.raises(LedgerError, match="must be an object"):
        build_shard("2026-08-30", "ops.5002", TS, {"story_status": []}, SCHEMA)


def test_validate_rejects_bad_story_transition_timestamp():
    """Envelope timestamp valid, record timestamp not — isolates the inner check."""
    with pytest.raises(LedgerError, match="last_transition_at"):
        build_shard(
            "2026-08-30", "ops.5002", TS,
            {"story_status": {"5002": {"status": "in_progress", "failed_runs": 0,
                                       "last_transition_at": "yesterday"}}},
            SCHEMA,
        )


# ---------------------------------------------------------------- idempotency


def test_fr_c30_rewriting_a_stage_is_byte_identical():
    """FR-C30: a retried stage produces the same bytes, so it is a no-op."""
    first = serialize_shard(_workflow_shard())
    second = serialize_shard(_workflow_shard())
    assert first == second


def test_fr_c30_serialization_is_insensitive_to_key_insertion_order():
    """Same data, different dict order → same bytes. Otherwise a retry looks
    like a change purely because a dict was built in another order."""
    a = build_shard(
        "2026-08-30", "workflow", TS,
        {"identified_raw": 7, "identified_new_after_dedup": 1}, SCHEMA,
    )
    b = build_shard(
        "2026-08-30", "workflow", TS,
        {"identified_new_after_dedup": 1, "identified_raw": 7}, SCHEMA,
    )
    assert serialize_shard(a) == serialize_shard(b)


def test_put_shard_rewrite_is_idempotent_at_the_same_key():
    client = FakeS3()
    key1 = put_shard("findings-bucket", _workflow_shard(), client)
    key2 = put_shard("findings-bucket", _workflow_shard(), client)
    assert key1 == key2
    assert len(client.objects) == 1
    assert client.put_calls == [key1, key1]


def test_put_shard_validates_before_writing():
    """A malformed shard must not reach the bucket at all."""
    client = FakeS3()
    bad = _workflow_shard()
    bad["fields"] = {"identified_raw": -5}
    with pytest.raises(LedgerError):
        put_shard("findings-bucket", bad, client)
    assert client.put_calls == []


# ---------------------------------------------------------------- concurrency


def test_nt6_concurrent_stages_both_survive():
    """NT-6: the two architect halves write simultaneously; both survive.

    They are concurrent by design, so a lost update here would be guaranteed
    rather than unlucky.
    """
    client = FakeS3()
    shards = [
        _workflow_shard("workflow.code-review", raw=7),
        _workflow_shard("workflow.pentest", raw=5),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        keys = list(pool.map(lambda s: put_shard("findings-bucket", s, client), shards))

    assert len(set(keys)) == 2
    assert len(client.objects) == 2
    merged = merge_shards([json.loads(b) for b in client.objects.values()], SCHEMA)
    assert merged["fields"]["identified_raw"] == 12  # neither half lost


def test_fr_c29_no_shared_object_read_path_exists():
    """FR-C29: assert the module has no get-then-put path.

    Source-level, because the guarantee is the *absence* of code. A behavioural
    test can only sample the interleavings it happens to hit; if a read path
    were ever added, this fails immediately. AST-based so that prose naming
    these calls does not trip it — only real attribute access counts.
    """
    import ast

    source = (Path(__file__).parent.parent / "security_agent_ledger.py").read_text()
    called = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
    }
    forbidden = {"get_object", "download_file", "download_fileobj", "copy_object"}
    assert not (called & forbidden), (
        f"{sorted(called & forbidden)} reintroduces the lost-update path"
    )


def test_merge_rejects_duplicate_stage_shards():
    """Two shards claiming one stage id means a writer collision upstream."""
    with pytest.raises(LedgerError, match="duplicate stage shards"):
        merge_shards([_workflow_shard("workflow"), _workflow_shard("workflow")], SCHEMA)


def test_merge_rejects_shards_from_different_runs():
    other = build_shard("2026-08-29", "triage", TS, {"stories_created": 1}, SCHEMA)
    with pytest.raises(LedgerError, match="span multiple runs"):
        merge_shards([_workflow_shard(), other], SCHEMA)


def test_merge_rejects_empty_shard_set():
    with pytest.raises(LedgerError, match="empty shard set"):
        merge_shards([], SCHEMA)


# ---------------------------------------------------------------- merge rules


def test_merge_sums_counts_and_maxes_duration():
    """Overlapping stages: counts add, wall-clock does not."""
    a = build_shard("2026-08-30", "workflow.code-review", TS,
                    {"identified_raw": 7, "run_duration_seconds": 1800}, SCHEMA)
    b = build_shard("2026-08-30", "workflow.pentest", TS,
                    {"identified_raw": 5, "run_duration_seconds": 2700}, SCHEMA)
    merged = merge_shards([a, b], SCHEMA)
    assert merged["fields"]["identified_raw"] == 12
    assert merged["fields"]["run_duration_seconds"] == 2700


def test_merge_unions_finding_ids_without_duplicates():
    a = build_shard("2026-08-30", "workflow.code-review", TS,
                    {"new_finding_ids": ["f-b3d4", "f-a1c2"]}, SCHEMA)
    b = build_shard("2026-08-30", "workflow.pentest", TS,
                    {"new_finding_ids": ["f-a1c2", "f-c5e6"]}, SCHEMA)
    merged = merge_shards([a, b], SCHEMA)
    assert merged["fields"]["new_finding_ids"] == ["f-a1c2", "f-b3d4", "f-c5e6"]


def test_merge_unions_story_status_maps():
    merged = merge_shards(
        [_ops_shard(5002, "fixed"), _ops_shard(5003, "in_progress")], SCHEMA
    )
    assert set(merged["fields"]["story_status"]) == {"5002", "5003"}


def test_merge_raises_on_conflicting_story_records():
    """Silently resolving this is how a report becomes authoritative and wrong."""
    a = _ops_shard(5002, "fixed")
    b = build_shard(
        "2026-08-30", "ops.5002-retry", TS,
        {"story_status": {"5002": {"status": "in_progress", "failed_runs": 2,
                                   "last_transition_at": TS}}}, SCHEMA,
    )
    with pytest.raises(LedgerError, match="conflicting records"):
        merge_shards([a, b], SCHEMA)


def test_merge_allows_identical_story_records_from_two_shards():
    """A duplicated-but-identical record is a retry, not a conflict."""
    a = _ops_shard(5002, "fixed")
    b = build_shard(
        "2026-08-30", "ops.5002-retry", TS,
        {"story_status": {"5002": {"status": "fixed", "failed_runs": 0,
                                   "last_transition_at": TS}}}, SCHEMA,
    )
    assert set(merge_shards([a, b], SCHEMA)["fields"]["story_status"]) == {"5002"}


def test_merge_raises_on_conflicting_single_valued_field():
    a = build_shard("2026-08-30", "triage.a", TS, {"daily_epic": 5001}, SCHEMA)
    b = build_shard("2026-08-30", "triage.b", TS, {"daily_epic": 5099}, SCHEMA)
    with pytest.raises(LedgerError, match="single-valued"):
        merge_shards([a, b], SCHEMA)


def test_merge_accepts_agreeing_single_valued_field():
    a = build_shard("2026-08-30", "triage.a", TS, {"daily_epic": 5001}, SCHEMA)
    b = build_shard("2026-08-30", "triage.b", TS, {"daily_epic": 5001}, SCHEMA)
    assert merge_shards([a, b], SCHEMA)["fields"]["daily_epic"] == 5001


def test_merge_preserves_dependency_order_of_planned_sequence():
    """Sequence carries meaning; re-sorting it would invent a third ordering."""
    merged = load_and_merge(FIXTURES / "ledger-complete", SCHEMA)
    assert merged["fields"]["planned_sequence"] == [5003, 5002, 5004]


def test_merge_generated_at_is_the_newest_writer():
    merged = load_and_merge(FIXTURES / "ledger-complete", SCHEMA)
    assert merged["generated_at"] == "2026-08-30T11:05:00Z"


def test_merge_is_independent_of_shard_order():
    """Report content must not depend on the order shards happened to load."""
    shards = load_shards(FIXTURES / "ledger-complete", SCHEMA)
    forward = merge_shards(shards, SCHEMA)
    backward = merge_shards(list(reversed(shards)), SCHEMA)
    assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)


def test_merge_rejects_unknown_merge_rule(monkeypatch):
    """Guards the rule dispatch itself: an unroutable rule must fail loudly."""
    schema = load_schema()
    schema["x-fields"]["identified_raw"]["merge"] = "average"
    a = build_shard("2026-08-30", "workflow.a", TS, {"identified_raw": 1}, schema)
    b = build_shard("2026-08-30", "workflow.b", TS, {"identified_raw": 2}, schema)
    with pytest.raises(LedgerError, match="unknown merge rule"):
        merge_shards([a, b], schema)


# ---------------------------------------------------------------- load


def test_load_shards_is_sorted_by_stage_not_filesystem_order():
    stages = [s["stage"] for s in load_shards(FIXTURES / "ledger-complete", SCHEMA)]
    assert stages == sorted(stages)


def test_load_shards_validates_each_file(tmp_path):
    (tmp_path / "shard-workflow.json").write_text(json.dumps({"stage": "workflow"}))
    with pytest.raises(LedgerError):
        load_shards(tmp_path, SCHEMA)


def test_load_shards_ignores_non_shard_files(tmp_path):
    (tmp_path / "shard-workflow.json").write_bytes(serialize_shard(_workflow_shard("workflow")))
    (tmp_path / "report.html").write_text("<html></html>")
    (tmp_path / "notes.json").write_text("{}")
    assert len(load_shards(tmp_path, SCHEMA)) == 1


# ---------------------------------------------------------------- reconcile


def test_fr_c32_reconciliation_holds_on_complete_run():
    merged = load_and_merge(FIXTURES / "ledger-complete", SCHEMA)
    result = reconcile(merged)
    assert result["ok"] is True
    assert result["accounted"] == merged["fields"]["stories_created"] == 3
    assert status_counts(merged) == {"fixed": 2, "stuck": 1, "in_progress": 0}


def test_fr_c32_identity_fails_when_a_story_has_no_status():
    """The failure this identity exists to catch: a story vanishing from the
    totals. The result must name WHICH one."""
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 2, "story_ids": [5002, 5003]}, SCHEMA)
    merged = merge_shards([triage, _ops_shard(5002, "fixed")], SCHEMA)
    result = reconcile(merged)
    assert result["ok"] is False
    assert result["identity_ok"] is False
    assert result["missing_story_ids"] == [5003]


def test_fr_c32_identity_fails_on_status_for_an_unfiled_story():
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5002]}, SCHEMA)
    merged = merge_shards(
        [triage, _ops_shard(5002, "fixed"), _ops_shard(5099, "fixed")], SCHEMA
    )
    result = reconcile(merged)
    assert result["unexpected_story_ids"] == [5099]
    assert result["ok"] is False


def test_fr_c32_coverage_fails_when_a_finding_has_no_story():
    """The second half of the identity, checked by finding identity."""
    workflow = build_shard("2026-08-30", "workflow", TS,
                           {"identified_new_after_dedup": 2,
                            "new_finding_ids": ["f-a1c2", "f-b3d4"]}, SCHEMA)
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5002],
                          "findings_covered": ["f-a1c2"]}, SCHEMA)
    merged = merge_shards([workflow, triage, _ops_shard(5002, "fixed")], SCHEMA)
    result = reconcile(merged)
    assert result["coverage_ok"] is False
    assert result["uncovered_finding_count"] == 1
    assert result["identity_ok"] is True  # only the coverage half fails


def test_coverage_fails_on_findings_with_zero_stories_without_ids():
    """Without finding ids only the count-level claim is checkable — and it
    still catches findings that produced no story at all."""
    workflow = build_shard("2026-08-30", "workflow", TS,
                           {"identified_new_after_dedup": 3}, SCHEMA)
    merged = merge_shards([workflow], SCHEMA)
    assert reconcile(merged)["coverage_ok"] is False


def test_fr_c2_zero_finding_night_reconciles_and_is_final():
    """The common case: nothing found, nothing filed, run closes clean."""
    merged = load_and_merge(FIXTURES / "ledger-zero-findings", SCHEMA)
    assert merged["fields"]["identified_new_after_dedup"] == 0
    assert reconcile(merged)["ok"] is True
    assert is_final(merged) is True


# ---------------------------------------------------------------- finalization


def test_fr_c36_final_withheld_while_a_story_is_in_progress():
    merged = load_and_merge(FIXTURES / "ledger-in-progress", SCHEMA)
    assert status_counts(merged)["in_progress"] == 1
    assert is_final(merged) is False


def test_fr_c36_final_granted_when_all_stories_terminal():
    merged = load_and_merge(FIXTURES / "ledger-complete", SCHEMA)
    assert is_final(merged) is True


def test_fr_c36_final_withheld_when_totals_do_not_reconcile():
    """A run whose numbers do not add up is not finished being understood,
    even with nothing still moving."""
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 2, "story_ids": [5002, 5003]}, SCHEMA)
    merged = merge_shards([triage, _ops_shard(5002, "fixed")], SCHEMA)
    assert is_final(merged) is False


def test_stuck_story_counts_as_terminal_for_finalization():
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5004]}, SCHEMA)
    stuck = _ops_shard(5004, "stuck", failed_runs=3, reason="failed_run_limit")
    assert is_final(merge_shards([triage, stuck], SCHEMA)) is True


# ---------------------------------------------------------------- stuck rule


def test_fr_c37_stuck_by_failed_run_limit():
    """Path 1 of 2, independently: 3 failed developer runs."""
    record = {"status": "in_progress", "failed_runs": 3,
              "last_transition_at": "2026-08-30T10:00:00Z"}
    result = evaluate_story_status(record, "2026-08-30T10:05:00Z", SCHEMA)
    assert result["status"] == "stuck"
    assert result["reason"] == "failed_run_limit"


def test_fr_c37_stuck_by_no_transition_timeout():
    """Path 2 of 2, independently: 24h with no state transition — reached with
    zero failed runs, so it cannot be passing via path 1."""
    record = {"status": "in_progress", "failed_runs": 0,
              "last_transition_at": "2026-08-29T09:00:00Z"}
    result = evaluate_story_status(record, "2026-08-30T09:00:00Z", SCHEMA)
    assert result["status"] == "stuck"
    assert result["reason"] == "no_transition_timeout"


def test_stuck_rule_thresholds_match_decision_d19():
    assert SCHEMA["x-stuck-rule"]["failed_runs_threshold"] == 3
    assert SCHEMA["x-stuck-rule"]["no_transition_hours"] == 24


@pytest.mark.parametrize(
    "failed_runs,last_transition,now",
    [
        (2, "2026-08-30T09:00:00Z", "2026-08-30T10:00:00Z"),   # under both
        (2, "2026-08-29T10:00:01Z", "2026-08-30T10:00:00Z"),   # just under 24h
    ],
)
def test_story_below_both_thresholds_stays_in_progress(failed_runs, last_transition, now):
    record = {"status": "in_progress", "failed_runs": failed_runs,
              "last_transition_at": last_transition}
    result = evaluate_story_status(record, now, SCHEMA)
    assert result["status"] == "in_progress"
    assert result.get("reason") is None


def test_stuck_evaluation_is_exactly_at_the_boundary():
    """Exactly 24h is stuck — the boundary is closed, not ambiguous."""
    record = {"status": "in_progress", "failed_runs": 0,
              "last_transition_at": "2026-08-29T10:00:00Z"}
    assert evaluate_story_status(record, "2026-08-30T10:00:00Z", SCHEMA)["status"] == "stuck"


def test_evaluate_does_not_revisit_terminal_stories():
    """`fixed` does not become `stuck` because time passed."""
    record = {"status": "fixed", "failed_runs": 0,
              "last_transition_at": "2026-08-01T10:00:00Z"}
    assert evaluate_story_status(record, "2026-08-30T10:00:00Z", SCHEMA) == record


def test_evaluate_does_not_mutate_its_input():
    record = {"status": "in_progress", "failed_runs": 3,
              "last_transition_at": "2026-08-30T10:00:00Z"}
    evaluate_story_status(record, "2026-08-30T10:05:00Z", SCHEMA)
    assert record["status"] == "in_progress"


def test_evaluate_handles_offset_and_naive_timestamps():
    """Writers are not all guaranteed to emit Z-suffixed UTC."""
    record = {"status": "in_progress", "failed_runs": 0,
              "last_transition_at": "2026-08-29T12:00:00+02:00"}
    result = evaluate_story_status(record, "2026-08-30T12:00:00+02:00", SCHEMA)
    assert result["status"] == "stuck"


def test_failed_run_limit_takes_precedence_when_both_paths_apply():
    """Deterministic reason when a story hits both: the run limit is the
    proximate cause and the more actionable one."""
    record = {"status": "in_progress", "failed_runs": 5,
              "last_transition_at": "2026-08-01T10:00:00Z"}
    result = evaluate_story_status(record, "2026-08-30T10:00:00Z", SCHEMA)
    assert result["reason"] == "failed_run_limit"


# ---------------------------------------------------------------- CLI


def test_cli_write_produces_a_validated_shard_locally(tmp_path):
    from security_agent_ledger import main

    rc = main([
        "write", "--run-date", "2026-08-30", "--stage", "triage",
        "--generated-at", TS, "--fields", '{"stories_created": 2}',
        "--out-dir", str(tmp_path),
    ])
    assert rc == 0
    written = json.loads((tmp_path / "shard-triage.json").read_text())
    assert validate_shard(written, SCHEMA)["fields"]["stories_created"] == 2


def test_cli_write_reads_fields_from_a_file(tmp_path):
    from security_agent_ledger import main

    fields = tmp_path / "fields.json"
    fields.write_text('{"identified_raw": 4}')
    rc = main([
        "write", "--run-date", "2026-08-30", "--stage", "workflow",
        "--generated-at", TS, "--fields", str(fields), "--fields-file",
        "--out-dir", str(tmp_path),
    ])
    assert rc == 0
    assert json.loads((tmp_path / "shard-workflow.json").read_text())[
        "fields"]["identified_raw"] == 4


def test_cli_write_defaults_generated_at_to_now(tmp_path):
    """The clock lives in the CLI only — never below it."""
    from security_agent_ledger import main

    main([
        "write", "--run-date", "2026-08-30", "--stage", "workflow",
        "--fields", '{"identified_raw": 1}', "--out-dir", str(tmp_path),
    ])
    written = json.loads((tmp_path / "shard-workflow.json").read_text())
    assert written["generated_at"].endswith("Z")


def test_cli_write_requires_a_destination():
    from security_agent_ledger import main

    with pytest.raises(SystemExit):
        main(["write", "--run-date", "2026-08-30", "--stage", "workflow",
              "--fields", "{}"])


def test_cli_write_reports_validation_errors_as_exit_2(tmp_path, capsys):
    from security_agent_ledger import main

    rc = main([
        "write", "--run-date", "2026-08-30", "--stage", "ops.5002",
        "--generated-at", TS, "--fields", '{"stories_created": 1}',
        "--out-dir", str(tmp_path),
    ])
    assert rc == 2
    assert "ledger error" in capsys.readouterr().err


def test_cli_merge_emits_reconciliation_and_final(tmp_path, capsys):
    from security_agent_ledger import main

    rc = main(["merge", "--ledger-dir", str(FIXTURES / "ledger-complete")])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["final"] is True
    assert payload["reconciliation"]["ok"] is True


def test_cli_merge_writes_to_out_file(tmp_path):
    from security_agent_ledger import main

    out = tmp_path / "merged.json"
    main(["merge", "--ledger-dir", str(FIXTURES / "ledger-in-progress"),
          "--out", str(out)])
    assert json.loads(out.read_text())["final"] is False


def test_cli_merge_strict_exits_nonzero_when_totals_do_not_reconcile(tmp_path):
    from security_agent_ledger import main

    (tmp_path / "shard-triage.json").write_bytes(
        serialize_shard(build_shard("2026-08-30", "triage", TS,
                                    {"stories_created": 2, "story_ids": [1, 2]}, SCHEMA))
    )
    assert main(["merge", "--ledger-dir", str(tmp_path), "--strict"]) == 1
    assert main(["merge", "--ledger-dir", str(tmp_path)]) == 0
