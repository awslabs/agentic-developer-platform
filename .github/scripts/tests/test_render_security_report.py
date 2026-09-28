"""Tests for render_security_report.py (issue #4441, unit U2).

Gate coverage: NFR-6 purity (byte-identical HTML, including across a
re-import), NT-11 allow-list, FR-C33 no agent write path, FR-C34 content,
FR-C35/FR-C36 living status and withheld `final`.
"""

import hashlib
import json
import subprocess  # nosec B404 - fixed argv, no shell, test-only
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from render_security_report import (
    STATUS_FINAL,
    STATUS_IN_PROGRESS,
    build_view,
    main,
    render,
    render_html,
)
from security_agent_ledger import (
    build_shard,
    load_and_merge,
    load_schema,
    merge_shards,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = Path(__file__).parent.parent / "render_security_report.py"
SCHEMA = load_schema()
TS = "2026-08-30T02:41:00Z"

COMPLETE = FIXTURES / "ledger-complete"
IN_PROGRESS = FIXTURES / "ledger-in-progress"
ZERO = FIXTURES / "ledger-zero-findings"


def _merged(fixture):
    return load_and_merge(fixture, SCHEMA)


def _ops_shard(story, status, failed_runs=0, reason=None):
    record = {"status": status, "failed_runs": failed_runs, "last_transition_at": TS}
    if reason is not None:
        record["reason"] = reason
    return build_shard(
        "2026-08-30", f"ops.{story}", TS, {"story_status": {str(story): record}}, SCHEMA
    )


# ---------------------------------------------------------------- purity


def test_nfr6_same_ledger_renders_byte_identical_html():
    """The core purity claim: same ledger in => byte-identical HTML out."""
    merged = _merged(COMPLETE)
    first = render(merged, SCHEMA)
    second = render(merged, SCHEMA)
    assert first == second
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(
        second.encode()
    ).hexdigest()


def test_nfr6_purity_survives_a_fresh_interpreter():
    """Renders the same fixture in two separate processes and compares hashes.

    This is the assertion a same-process double-render cannot make: it catches
    import-time clock reads or random seeding, the failure mode where the
    purity test passes locally and fails in CI a second later.
    """
    code = (
        "import sys, hashlib;"
        f"sys.path.insert(0, {str(SCRIPT.parent)!r});"
        "from render_security_report import render;"
        "from security_agent_ledger import load_and_merge;"
        f"m = load_and_merge({str(COMPLETE)!r});"
        "print(hashlib.sha256(render(m).encode()).hexdigest())"
    )
    hashes = {
        subprocess.run(  # nosec B603 - fixed argv, no shell
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(hashes) == 1
    assert hashes.pop() == hashlib.sha256(render(_merged(COMPLETE), SCHEMA).encode()).hexdigest()


def test_nfr6_renderer_source_reads_no_clock_env_or_randomness():
    """Source-level guard on the determinism rule.

    A behavioural test can miss an impurity that happens to agree twice in a
    row, so assert structurally that the calls cannot be there. AST-based
    rather than substring-based: prose in a docstring naming `uuid` is not an
    impurity, and a check that cannot tell the difference would force the
    documentation to avoid the words it needs.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text())
    # `main` is the sanctioned I/O boundary; everything else must be pure.
    pure_nodes = [
        n for n in tree.body
        if not (isinstance(n, ast.FunctionDef) and n.name == "main")
    ]

    referenced = set()
    for node in pure_nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                referenced.add(ast.unparse(sub))
            elif isinstance(sub, ast.Name):
                referenced.add(sub.id)
            elif isinstance(sub, ast.Import):
                referenced.update(a.name.split(".")[0] for a in sub.names)
            elif isinstance(sub, ast.ImportFrom) and sub.module:
                referenced.add(sub.module.split(".")[0])

    forbidden = {
        "datetime.now", "datetime.utcnow", "time.time", "time.monotonic",
        "uuid", "random", "os.environ", "os.getenv", "id",
    }
    assert not (referenced & forbidden), (
        f"impure references in the renderer: {sorted(referenced & forbidden)}"
    )


def test_nfr6_render_does_not_mutate_the_ledger():
    """A renderer that edits its input is not a pure function of it."""
    merged = _merged(COMPLETE)
    before = json.dumps(merged, sort_keys=True)
    render(merged, SCHEMA)
    assert json.dumps(merged, sort_keys=True) == before


def test_nfr6_output_is_independent_of_field_insertion_order():
    """Same data, differently-ordered dicts => same HTML. Guards against a
    dict-iteration dependence hiding inside the view builder."""
    fields_a = {"identified_raw": 7, "identified_new_after_dedup": 2}
    fields_b = {"identified_new_after_dedup": 2, "identified_raw": 7}
    a = merge_shards([build_shard("2026-08-30", "workflow", TS, fields_a, SCHEMA)], SCHEMA)
    b = merge_shards([build_shard("2026-08-30", "workflow", TS, fields_b, SCHEMA)], SCHEMA)
    assert render(a, SCHEMA) == render(b, SCHEMA)


def test_nfr6_story_rows_are_independent_of_shard_order():
    shards = [_ops_shard(5004, "fixed"), _ops_shard(5002, "fixed"), _ops_shard(5003, "fixed")]
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 3, "story_ids": [5002, 5003, 5004]}, SCHEMA)
    forward = render(merge_shards([triage] + shards, SCHEMA), SCHEMA)
    backward = render(merge_shards(list(reversed(shards)) + [triage], SCHEMA), SCHEMA)
    assert forward == backward


def test_generated_at_comes_from_the_ledger_not_a_clock():
    """FR-C33's mechanism: the timestamp is data, carried in from the ledger."""
    html_out = render(_merged(COMPLETE), SCHEMA)
    assert "2026-08-30T11:05:00Z" in html_out


# ---------------------------------------------------------------- allow-list


def test_nt11_view_contains_only_allowlisted_keys():
    view = build_view(_merged(COMPLETE), SCHEMA)
    assert set(view) <= set(SCHEMA["x-report-allowlist"])
    assert set(view["stories"][0]) <= set(SCHEMA["x-report-story-allowlist"])
    assert set(view["reconciliation"]) <= set(SCHEMA["x-report-reconciliation-allowlist"])


def test_nt11_unexpected_ledger_field_cannot_reach_the_view_or_html():
    """The leak path this closes: a field that somehow reached the merged
    ledger must not reach the document. Injected post-merge, since write-side
    validation would reject it earlier — that is the point, this is defence in
    depth behind that check.
    """
    merged = _merged(COMPLETE)
    merged["fields"]["exploit_detail"] = "POST /admin with X-Forwarded-For bypass"
    merged["leaked_envelope_key"] = "s3://private/exploit.txt"

    view = build_view(merged, SCHEMA)
    assert "exploit_detail" not in view
    assert "leaked_envelope_key" not in view

    html_out = render(merged, SCHEMA)
    assert "exploit_detail" not in html_out
    assert "X-Forwarded-For" not in html_out
    assert "s3://private/exploit.txt" not in html_out


def test_nt11_unexpected_story_record_field_is_dropped():
    merged = _merged(COMPLETE)
    merged["fields"]["story_status"]["5002"]["notes"] = "curl -X POST /admin"
    html_out = render(merged, SCHEMA)
    assert "curl -X POST" not in html_out
    assert "notes" not in build_view(merged, SCHEMA)["stories"][0]


def test_html_escapes_interpolated_values():
    """Ledger values are escaped, so a value cannot become markup."""
    merged = _merged(COMPLETE)
    merged["run_date"] = "2026-08-30<script>alert(1)</script>"
    html_out = render(merged, SCHEMA)
    assert "<script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_report_allowlist_covers_every_field_the_view_builds():
    """Guards the schema against drift: a key the view builds but the
    allow-list omits would be silently dropped from the report."""
    view = build_view(_merged(COMPLETE), SCHEMA)
    expected = {
        "run_date", "generated_at", "status", "identified_raw",
        "identified_new_after_dedup", "stories_created", "fixed", "stuck",
        "in_progress", "run_duration_seconds", "pentest_cost_usd",
        "daily_epic", "planned_sequence", "stories", "reconciliation",
    }
    assert set(view) == expected


# ---------------------------------------------------------------- content


def test_fr_c34_report_shows_every_required_metric():
    html_out = render(_merged(COMPLETE), SCHEMA)
    for label in (
        "Vulnerabilities identified (raw)",
        "Vulnerabilities new after dedup",
        "Stories created",
        "Fixed autonomously",
        "Halted / stuck",
        "In progress",
        "Run duration",
        "Pentest cost",
    ):
        assert label in html_out


def test_fr_c34_counts_reflect_the_merged_shards():
    view = build_view(_merged(COMPLETE), SCHEMA)
    assert view["identified_raw"] == 12  # 7 + 5, both concurrent halves
    assert view["identified_new_after_dedup"] == 3
    assert view["stories_created"] == 3
    assert (view["fixed"], view["stuck"], view["in_progress"]) == (2, 1, 0)
    assert view["run_duration_seconds"] == 2700  # max, not sum
    assert view["pentest_cost_usd"] == 12.5


def test_stuck_reason_is_rendered_as_prose():
    html_out = render(_merged(COMPLETE), SCHEMA)
    assert "3 failed developer runs" in html_out


def test_no_transition_reason_is_rendered():
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5004]}, SCHEMA)
    stuck = _ops_shard(5004, "stuck", reason="no_transition_timeout")
    html_out = render(merge_shards([triage, stuck], SCHEMA), SCHEMA)
    assert "24h with no state transition" in html_out


def test_planned_sequence_is_rendered_in_dependency_order():
    html_out = render(_merged(COMPLETE), SCHEMA)
    sequence_section = html_out.split("Planned sequence", 1)[1]
    assert sequence_section.index("#5003") < sequence_section.index("#5002")


def test_story_rows_follow_the_planned_sequence():
    view = build_view(_merged(COMPLETE), SCHEMA)
    assert [s["story_id"] for s in view["stories"]] == [5003, 5002, 5004]


def test_unsequenced_stories_render_after_sequenced_ones_by_number():
    """A story filed but not sequenced must still appear, deterministically."""
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 3, "story_ids": [5002, 5003, 5009]}, SCHEMA)
    orchestration = build_shard("2026-08-30", "orchestration", TS,
                                {"planned_sequence": [5003, 5002]}, SCHEMA)
    shards = [triage, orchestration, _ops_shard(5009, "fixed"),
              _ops_shard(5003, "fixed"), _ops_shard(5002, "fixed")]
    view = build_view(merge_shards(shards, SCHEMA), SCHEMA)
    assert [s["story_id"] for s in view["stories"]] == [5003, 5002, 5009]


def test_zero_story_night_renders_without_a_story_table():
    """FR-C2's common case must render cleanly rather than an empty table."""
    html_out = render(_merged(ZERO), SCHEMA)
    assert "No stories were created for this run." in html_out
    assert "Planned sequence" not in html_out


def test_missing_optional_metrics_render_as_not_recorded():
    """A metric no stage wrote must read as unrecorded, never as zero — a
    silent 0 for pentest cost would be a wrong number, not a missing one."""
    merged = merge_shards(
        [build_shard("2026-08-30", "workflow", TS, {"identified_raw": 3}, SCHEMA)], SCHEMA
    )
    view = build_view(merged, SCHEMA)
    assert view["pentest_cost_usd"] is None
    assert "not recorded" in render_html(view)


@pytest.mark.parametrize(
    "seconds,expected",
    [(45, "45s"), (600, "10m 0s"), (3600, "1h 0m"), (5430, "1h 30m")],
)
def test_duration_formatting(seconds, expected):
    merged = merge_shards(
        [build_shard("2026-08-30", "workflow", TS,
                     {"run_duration_seconds": seconds}, SCHEMA)], SCHEMA
    )
    assert expected in render(merged, SCHEMA)


def test_cost_is_rendered_to_cents():
    merged = merge_shards(
        [build_shard("2026-08-30", "workflow", TS,
                     {"pentest_cost_usd": 12.5}, SCHEMA)], SCHEMA
    )
    assert "$12.50" in render(merged, SCHEMA)


def test_daily_epic_is_rendered_when_present():
    assert "5001" in render(_merged(COMPLETE), SCHEMA)


# ---------------------------------------------------------------- status


def test_fr_c36_status_is_in_progress_while_a_story_is_non_terminal():
    view = build_view(_merged(IN_PROGRESS), SCHEMA)
    assert view["status"] == STATUS_IN_PROGRESS
    assert STATUS_FINAL not in render_html(view).split("Status:", 1)[1].split("</p>", 1)[0]


def test_fr_c36_status_is_final_when_every_story_is_terminal():
    view = build_view(_merged(COMPLETE), SCHEMA)
    assert view["status"] == STATUS_FINAL


def test_fr_c35_report_is_living_across_a_state_transition():
    """The same run re-renders from in-progress to final as its last story
    resolves — with only ops shards added, never edited."""
    shards = [
        build_shard("2026-08-30", "triage", TS,
                    {"stories_created": 2, "story_ids": [5002, 5003]}, SCHEMA),
        _ops_shard(5002, "fixed"),
        _ops_shard(5003, "in_progress"),
    ]
    before = build_view(merge_shards(shards, SCHEMA), SCHEMA)
    assert before["status"] == STATUS_IN_PROGRESS

    shards[2] = _ops_shard(5003, "stuck", failed_runs=3, reason="failed_run_limit")
    after = build_view(merge_shards(shards, SCHEMA), SCHEMA)
    assert after["status"] == STATUS_FINAL
    assert after["stuck"] == 1


def test_status_is_final_on_a_zero_story_night():
    assert build_view(_merged(ZERO), SCHEMA)["status"] == STATUS_FINAL


# ---------------------------------------------------------------- reconcile


def test_reconciling_run_reports_totals_that_add_up():
    html_out = render(_merged(COMPLETE), SCHEMA)
    assert "Totals reconcile" in html_out
    assert "do not reconcile" not in html_out


def test_non_reconciling_run_is_flagged_in_the_report():
    """The report must say so rather than look authoritative and be wrong."""
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 2, "story_ids": [5002, 5003]}, SCHEMA)
    merged = merge_shards([triage, _ops_shard(5002, "fixed")], SCHEMA)
    html_out = render(merged, SCHEMA)
    assert "do not reconcile" in html_out
    assert "no status recorded for #5003" in html_out
    assert build_view(merged, SCHEMA)["status"] == STATUS_IN_PROGRESS


def test_uncovered_findings_are_flagged():
    workflow = build_shard("2026-08-30", "workflow", TS,
                           {"identified_new_after_dedup": 2,
                            "new_finding_ids": ["f-a1c2", "f-b3d4"]}, SCHEMA)
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5002],
                          "findings_covered": ["f-a1c2"]}, SCHEMA)
    merged = merge_shards([workflow, triage, _ops_shard(5002, "fixed")], SCHEMA)
    html_out = render(merged, SCHEMA)
    assert "1 finding(s) not covered by any story" in html_out


def test_unexpected_story_status_is_flagged():
    triage = build_shard("2026-08-30", "triage", TS,
                         {"stories_created": 1, "story_ids": [5002]}, SCHEMA)
    merged = merge_shards(
        [triage, _ops_shard(5002, "fixed"), _ops_shard(5099, "fixed")], SCHEMA
    )
    assert "status recorded for unfiled #5099" in render(merged, SCHEMA)


def test_finding_ids_never_appear_in_the_report():
    """Finding ids are ledger-internal; the report carries counts and stories."""
    html_out = render(_merged(COMPLETE), SCHEMA)
    for finding_id in ("f-a1c2", "f-b3d4", "f-c5e6"):
        assert finding_id not in html_out


# ---------------------------------------------------------------- CLI / smoke


def test_html_is_well_formed_enough_to_parse():
    from html.parser import HTMLParser

    class Parser(HTMLParser):
        def error(self, message):  # pragma: no cover
            raise AssertionError(message)

    html_out = render(_merged(COMPLETE), SCHEMA)
    assert html_out.startswith("<!DOCTYPE html>")
    assert html_out.rstrip().endswith("</html>")
    Parser().feed(html_out)


def test_cli_renders_from_a_ledger_dir(tmp_path):
    out = tmp_path / "report.html"
    assert main(["--ledger-dir", str(COMPLETE), "--out", str(out)]) == 0
    assert STATUS_FINAL in out.read_text()


def test_cli_renders_from_a_premerged_ledger(tmp_path):
    merged_path = tmp_path / "merged.json"
    merged_path.write_text(json.dumps(_merged(IN_PROGRESS)))
    out = tmp_path / "report.html"
    assert main(["--merged", str(merged_path), "--out", str(out)]) == 0
    assert STATUS_IN_PROGRESS in out.read_text()


def test_cli_requires_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit):
        main(["--out", str(tmp_path / "r.html")])
    with pytest.raises(SystemExit):
        main(["--ledger-dir", str(COMPLETE), "--merged", "x",
              "--out", str(tmp_path / "r.html")])


def test_cli_output_matches_the_pure_function(tmp_path):
    """No formatting drift between the CLI path and the tested function."""
    out = tmp_path / "report.html"
    main(["--ledger-dir", str(COMPLETE), "--out", str(out)])
    assert out.read_text() == render(_merged(COMPLETE), SCHEMA)


@pytest.mark.parametrize("fixture", [COMPLETE, IN_PROGRESS, ZERO])
def test_smoke_command_shape_for_every_fixture(tmp_path, fixture):
    """The issue's smoke assertion, run as a subprocess over each fixture."""
    out = tmp_path / "r.html"
    subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, str(SCRIPT), "--ledger-dir", str(fixture), "--out", str(out)],
        check=True, capture_output=True,
    )
    text = out.read_text()
    assert STATUS_IN_PROGRESS in text or STATUS_FINAL in text


def test_fr_c33_renderer_is_the_only_html_write_path():
    """FR-C33: no agent authors the HTML. The only writer is main's write_text,
    fed by render() — assert there is exactly one write site."""
    source = SCRIPT.read_text()
    assert source.count("write_text") == 1
    assert "render(merged)" in source
