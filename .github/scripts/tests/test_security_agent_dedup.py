"""Tests for the findings normalization + baseline dedup unit (#4447, U8).

Gate coverage, from the issue's `## Validation` and impact table:

* NT-4 -- replaying identical findings twice yields zero new the second time.
* The positional-key property -- a moved file or a shifted line does NOT
  resurface a known finding. This is the assert that decides whether the key
  can be positional at all, and it is the one the impact table calls out as
  arriving "on an ordinary refactor, with no obvious cause".
* NT-5 -- zero-new emits an explicit "nothing to file" signal, not an empty
  result a downstream stage could misread as "not yet run".
* `security-scan.yml` is byte-identical to `main`.
* No public-artifact upload path.
* Dedup must not fail closed silently -- a genuinely new finding reaches
  triage, and suppressions are counted.
"""

import json
import subprocess  # nosec B404 - fixed argv, no shell, test-only
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from dedup_security_findings import (
    BASELINE_PATH,
    DedupError,
    assert_ids_are_unstable,
    baseline_fingerprints,
    build_baseline_entry,
    build_result,
    dedup,
    empty_baseline,
    fingerprint,
    ledger_fields,
    load_baseline,
    main,
    nothing_to_file,
    refresh_baseline,
    serialize_baseline,
    validate_baseline,
    workflow_stage,
)
from normalize_security_findings import (
    SOURCES,
    NormalizationError,
    extract_findings,
    is_actionable,
    keyable_files,
    load_raw_document,
    normalize_document,
    normalize_documents,
    normalize_finding,
    primary_location,
    prose_signature,
    source_of,
)
from security_agent_ledger import (
    build_shard,
    load_schema,
    load_shards,
    merge_shards,
    validate_shard,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
SECURITY_SCAN_WORKFLOW = REPO_ROOT / ".github/workflows/security-scan.yml"
PROFILE_PATH = REPO_ROOT / ".github/security/security-agent-profile.json"


# --------------------------------------------------------------------------
# helpers -- raw findings in the service's real shape
#
# Field names come from `findings.schema_fields` in the profile (the spike's
# recorded interface), not from what this module happens to read. A fixture
# written from the consumer is circular: both can be wrong together while
# every test passes.
# --------------------------------------------------------------------------


def raw_finding(
    finding_id="f-79066a5c-0000-4000-8000-000000000001",
    name="Budget caps never bind on /v1/chat/completions",
    file_path="modules/gateway/src/routes/chat.py",
    line_start=142,
    risk_type="BUSINESS_LOGIC_VULNERABILITIES",
    status="ACTIVE",
    job_field="codeReviewJobId",
    **extra,
):
    finding = {
        "findingId": finding_id,
        "name": name,
        "status": status,
        "riskType": risk_type,
        "riskLevel": "HIGH",
        "confidence": "HIGH",
        "validationStatus": "CONFIRMED",
        "codeLocations": [{"filePath": file_path, "lineStart": line_start}],
        job_field: "job-1",
    }
    finding.update(extra)
    return finding


def write_raw(tmp_path, findings, name="code-review-findings.json", run_date="2026-08-30"):
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "runDate": run_date,
                "title": "adp-dev-nightly-codereview-20260830",
                "findingCount": len(findings),
                "findings": findings,
            }
        ),
        encoding="utf-8",
    )
    return path


def normalized_one(**kwargs):
    return normalize_finding(raw_finding(**kwargs))


# --------------------------------------------------------------------------
# NT-4 -- replaying identical findings yields zero new
# --------------------------------------------------------------------------


def test_replaying_identical_findings_yields_zero_new_the_second_time():
    """NT-4, the headline requirement: a known finding does not re-file.

    Night one files it; it is accepted into the baseline; night two reports the
    very same finding and nothing is new.
    """
    findings = [normalized_one()]
    first = dedup(findings, empty_baseline())
    assert first["new_count"] == 1, "a first-ever finding must be new"

    baseline = refresh_baseline(
        empty_baseline(), findings, "2026-08-30", "accepted_risk"
    )
    second = dedup(findings, baseline)
    assert second["new_count"] == 0, (
        "dedup failed OPEN: a finding already in the baseline re-filed, which is "
        "the nightly issue flood this unit exists to prevent"
    )
    assert second["suppressed_count"] == 1


def test_the_same_night_reported_by_both_halves_is_one_piece_of_work():
    """The two halves review overlapping source. One defect reported twice is
    one work item, not two -- otherwise every shared finding double-files."""
    result = dedup(
        [
            normalized_one(finding_id="f-aaaa1111-0000-4000-8000-000000000001"),
            normalized_one(
                finding_id="f-bbbb2222-0000-4000-8000-000000000002",
                job_field="pentestJobId",
            ),
        ],
        empty_baseline(),
    )
    assert result["new_count"] == 1


def test_differing_finding_ids_do_not_make_the_same_defect_new():
    """The spike's core evidence: ids are per-job UUIDs, so two runs share
    NONE. A key that included the id would match nothing and re-file
    everything."""
    a = normalized_one(finding_id="f-79066a5c-0000-4000-8000-000000000001")
    b = normalized_one(finding_id="f-8ac415ea-0000-4000-8000-000000000002")
    assert fingerprint(a) == fingerprint(b)


def test_riskType_churn_does_not_resurface_a_known_finding():
    """Recorded in the spike: the SAME install-callback defect appeared as
    INSECURE_DIRECT_OBJECT_REFERENCE and later as PRIVILEGE_ESCALATION."""
    a = normalized_one(risk_type="INSECURE_DIRECT_OBJECT_REFERENCE")
    b = normalized_one(risk_type="PRIVILEGE_ESCALATION")
    assert fingerprint(a) == fingerprint(b)


def test_rescoring_severity_does_not_resurface_a_known_finding():
    """riskLevel/confidence/validationStatus are re-rated between runs."""
    base = raw_finding()
    rescored = raw_finding(riskLevel="CRITICAL", confidence="MEDIUM")
    rescored["validationStatus"] = "NOT_VALIDATED"
    assert fingerprint(normalize_finding(base)) == fingerprint(
        normalize_finding(rescored)
    )


# --------------------------------------------------------------------------
# the positional-key property -- the assert the issue hangs the design on
# --------------------------------------------------------------------------


def test_a_shifted_line_does_not_resurface_a_known_finding():
    """An unrelated edit above the finding shifts every line below it.

    With a `file:line` key -- the shape `diff_security_findings.py` uses for
    SAST -- this refactor resurfaces every known finding in the file as new.
    """
    baseline = refresh_baseline(
        empty_baseline(), [normalized_one(line_start=142)], "2026-08-30", "accepted_risk"
    )
    result = dedup([normalized_one(line_start=907)], baseline)
    assert result["new_count"] == 0, (
        "a shifted line resurfaced a known finding -- the key is positional, "
        "which floods the tracker on an ordinary refactor"
    )


def test_a_moved_file_does_not_resurface_a_known_finding():
    """The file is moved to a new directory; the defect is unchanged."""
    baseline = refresh_baseline(
        empty_baseline(),
        [normalized_one(file_path="modules/gateway/src/routes/chat.py")],
        "2026-08-30",
        "accepted_risk",
    )
    result = dedup(
        [normalized_one(file_path="modules/gateway/src/api/v2/routes/chat.py")],
        baseline,
    )
    assert result["new_count"] == 0, (
        "a moved file resurfaced a known finding -- the key includes the "
        "directory, so any reorganisation floods the tracker"
    )


def test_the_fingerprint_excludes_every_positional_and_unstable_signal():
    """Stated as a property over the key itself, so a future edit that
    reintroduces a line number or an id fails here and not in production."""
    finding = normalized_one(line_start=142, file_path="a/b/c/chat.py")
    fp = fingerprint(finding)
    for unstable in (
        "142",
        "a/b/c",
        "f-79066a5c-0000-4000-8000-000000000001",
        "BUSINESS_LOGIC_VULNERABILITIES",
        "HIGH",
    ):
        assert unstable not in fp
    # It is a bare sha256 digest: fixed width, no prose.
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)


def test_the_key_still_separates_genuinely_different_defects():
    """The other side of the property. A key loose enough to survive a move
    must not be so loose it collapses unrelated findings -- that is the
    'fails closed too aggressively' row of the impact table."""
    a = normalized_one(name="Budget caps never bind on /v1/chat/completions")
    b = normalized_one(name="Tenant id is read from an unvalidated header")
    assert fingerprint(a) != fingerprint(b)

    c = normalized_one(file_path="modules/gateway/src/routes/chat.py")
    d = normalized_one(file_path="modules/gateway/src/routes/embeddings.py")
    assert fingerprint(c) != fingerprint(d)


def test_codeLocations_order_does_not_change_the_key():
    """The service does not promise ordering within codeLocations."""
    forward = normalize_finding(
        raw_finding(codeLocations=[{"filePath": "a/x.py"}, {"filePath": "b/y.py"}])
    )
    reverse = normalize_finding(
        raw_finding(codeLocations=[{"filePath": "b/y.py"}, {"filePath": "a/x.py"}])
    )
    assert fingerprint(forward) == fingerprint(reverse)


def test_title_punctuation_and_casing_churn_does_not_resurface_a_finding():
    """Titles are LLM-authored prose; casing and punctuation drift between
    runs even when the defect described is identical."""
    a = normalized_one(name="Budget caps never bind on /v1/chat/completions")
    b = normalized_one(name="budget caps never bind on `/v1/chat/completions`!")
    assert fingerprint(a) == fingerprint(b)


# --------------------------------------------------------------------------
# NT-5 -- the zero-new signal
# --------------------------------------------------------------------------


def test_zero_new_emits_an_explicit_nothing_to_file_signal():
    """NT-5. The signal is a BOOLEAN, not an empty list: a consumer cannot
    tell an empty `new_findings` apart from a stage that never ran."""
    normalized = {"identified_raw": 4, "dropped_by_status": 0, "findings": [], "run_date": "2026-08-30"}
    result = build_result(normalized, empty_baseline())
    assert result["nothing_to_file"] is True
    assert result["identified_new_after_dedup"] == 0
    assert result["new_findings"] == []
    # The raw count survives: "4 found, all known" is a different night from
    # "nothing scanned", and the report must be able to say which.
    assert result["identified_raw"] == 4


def test_a_night_with_new_findings_does_not_signal_nothing_to_file():
    normalized = normalize_document({"runDate": "2026-08-30", "findings": [raw_finding()]})
    result = build_result(normalized, empty_baseline())
    assert result["nothing_to_file"] is False
    assert result["identified_new_after_dedup"] == 1


def test_everything_deduped_away_is_the_common_night():
    """FR-C2: findings reported, all already known, nothing to file."""
    findings = [normalized_one(), normalized_one(name="Tenant id unvalidated")]
    baseline = refresh_baseline(empty_baseline(), findings, "2026-08-30", "accepted_risk")
    normalized = {
        "identified_raw": 2,
        "dropped_by_status": 0,
        "findings": findings,
        "run_date": "2026-08-30",
    }
    result = build_result(normalized, baseline)
    assert result["nothing_to_file"] is True
    assert result["suppressed_by_baseline"] == 2


def test_nothing_to_file_helper_tracks_the_new_count():
    assert nothing_to_file({"new_count": 0}) is True
    assert nothing_to_file({"new_count": 1}) is False


# --------------------------------------------------------------------------
# not failing closed silently
# --------------------------------------------------------------------------


def test_a_genuinely_new_finding_reaches_triage_past_a_populated_baseline():
    """The suppression must be selective, not blanket."""
    known = [normalized_one()]
    baseline = refresh_baseline(empty_baseline(), known, "2026-08-30", "accepted_risk")
    fresh = normalized_one(
        name="Webhook signature is never verified",
        file_path="modules/agent-factory/webhook-ingress/handler.py",
    )
    result = dedup(known + [fresh], baseline)
    assert result["new_count"] == 1
    assert result["new"][0]["title"] == "Webhook signature is never verified"


def test_suppressions_are_counted_so_a_silent_suppression_is_impossible():
    """The impact table's 'fails closed' row is invisible by nature. Counting
    every suppression is what makes it visible at all."""
    findings = [normalized_one()]
    baseline = refresh_baseline(empty_baseline(), findings, "2026-08-30", "accepted_risk")
    result = dedup(findings, baseline)
    assert result["suppressed_count"] == 1
    assert result["suppressed_fingerprints"] == [fingerprint(findings[0])]


def test_a_baseline_entry_nothing_matched_is_reported_not_dropped():
    """A baseline fingerprint with no match tonight is reported. It could be
    fixed OR reworded, and this module cannot tell -- so it says so instead of
    quietly pruning."""
    baseline = refresh_baseline(
        empty_baseline(), [normalized_one()], "2026-08-30", "accepted_risk"
    )
    result = dedup([], baseline)
    assert result["unmatched_baseline_fingerprints"] == sorted(
        baseline_fingerprints(baseline)
    )


def test_a_finding_with_no_keyable_content_stays_visible():
    """A finding carrying neither a location nor a title must not collide with
    every other empty finding and vanish as 'already known'."""
    a = normalize_finding({"findingId": "f-1111", "codeLocations": []})
    b = normalize_finding({"findingId": "f-2222", "codeLocations": []})
    assert fingerprint(a) != fingerprint(b)
    assert dedup([a, b], empty_baseline())["new_count"] == 2


def test_an_unparseable_baseline_is_an_error_not_an_empty_baseline():
    """Degrading a corrupt baseline to empty makes every known finding new."""
    with pytest.raises(DedupError, match="refusing to treat it as empty"):
        load_baseline(_write(Path(_tmp()), "baseline.json", "{not json"))


def test_a_missing_baseline_is_an_empty_baseline():
    """The first run legitimately has nothing accepted."""
    assert load_baseline(Path(_tmp()) / "absent.json") == empty_baseline()


def test_an_empty_baseline_file_is_an_empty_baseline():
    assert load_baseline(_write(Path(_tmp()), "baseline.json", "   ")) == empty_baseline()


# --------------------------------------------------------------------------
# the baseline refresh path is bounded
# --------------------------------------------------------------------------


def test_the_refresh_path_rejects_a_reason_outside_the_closed_enum():
    """Free-text reasons are how prose -- and eventually exploit detail --
    reaches a committed file."""
    with pytest.raises(DedupError, match="is not one of"):
        build_baseline_entry(normalized_one(), "2026-08-30", "because I said so")


def test_a_baseline_entry_carries_no_prose_or_reproduction_detail():
    """The baseline is committed to the repo, which is not the private
    rendezvous (NEV-2)."""
    entry = build_baseline_entry(normalized_one(), "2026-08-30", "accepted_risk")
    assert set(entry) == {"fingerprint", "accepted_on", "reason", "key_files"}
    blob = json.dumps(entry)
    for leak in ("Budget caps", "modules/gateway/src", "142", "BUSINESS_LOGIC"):
        assert leak not in blob


def test_a_baseline_with_unknown_fields_is_rejected():
    """Bounding the entry shape is what stops the refresh path becoming a way
    to silence findings without review."""
    with pytest.raises(DedupError, match="outside the schema"):
        validate_baseline(
            {
                "schema_version": "1",
                "entries": [
                    {
                        "fingerprint": "a" * 64,
                        "accepted_on": "2026-08-30",
                        "reason": "accepted_risk",
                        "suppress_everything": True,
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "baseline,match",
    [
        ([], "must be an object"),
        ({"schema_version": "9", "entries": []}, "unsupported baseline schema_version"),
        ({"schema_version": "1", "entries": {}}, "must be an array"),
        ({"schema_version": "1", "entries": ["x"]}, "must be an object"),
        (
            {"schema_version": "1", "entries": [{"reason": "accepted_risk"}]},
            "has no fingerprint",
        ),
        (
            {
                "schema_version": "1",
                "entries": [{"fingerprint": "a", "reason": "nope"}],
            },
            "not one of",
        ),
    ],
)
def test_malformed_baselines_are_rejected(baseline, match):
    with pytest.raises(DedupError, match=match):
        validate_baseline(baseline)


def test_a_duplicate_fingerprint_in_the_baseline_is_rejected():
    entry = {"fingerprint": "a" * 64, "accepted_on": "2026-08-30", "reason": "accepted_risk"}
    with pytest.raises(DedupError, match="duplicate fingerprint"):
        validate_baseline({"schema_version": "1", "entries": [entry, dict(entry)]})


def test_refreshing_twice_does_not_grow_the_baseline():
    """Idempotent, so a re-run is a no-op rather than a spurious diff."""
    findings = [normalized_one()]
    once = refresh_baseline(empty_baseline(), findings, "2026-08-30", "accepted_risk")
    twice = refresh_baseline(once, findings, "2026-08-31", "accepted_risk")
    assert len(twice["entries"]) == len(once["entries"]) == 1
    assert twice["entries"][0]["accepted_on"] == "2026-08-30"


def test_refresh_preserves_existing_entries():
    first = refresh_baseline(empty_baseline(), [normalized_one()], "2026-08-30", "accepted_risk")
    second = refresh_baseline(
        first, [normalized_one(name="Something else")], "2026-08-31", "false_positive"
    )
    assert len(second["entries"]) == 2
    assert baseline_fingerprints(first) <= baseline_fingerprints(second)


def test_the_serialized_baseline_is_byte_stable():
    """An unchanged baseline must not appear in a diff."""
    baseline = refresh_baseline(
        empty_baseline(),
        [normalized_one(), normalized_one(name="Another")],
        "2026-08-30",
        "accepted_risk",
    )
    assert serialize_baseline(baseline) == serialize_baseline(baseline)
    assert serialize_baseline(baseline).endswith("\n")


def test_the_committed_baseline_is_valid():
    """The shipped artifact must load -- otherwise the first real run fails."""
    assert validate_baseline(json.loads(BASELINE_PATH.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# normalization
# --------------------------------------------------------------------------


def test_both_sources_normalize_to_one_shape():
    """The stated purpose of the normalizer."""
    review = normalize_finding(raw_finding(job_field="codeReviewJobId"))
    pentest = normalize_finding(raw_finding(job_field="pentestJobId"))
    assert set(review) == set(pentest)
    assert review["source"] == "code-review"
    assert pentest["source"] == "pentest"


def test_source_falls_back_to_unknown_rather_than_guessing():
    assert source_of({}) == "unknown"


def test_the_source_does_not_affect_the_key():
    """One defect found by both halves must not split into two work items."""
    assert fingerprint(normalized_one(job_field="codeReviewJobId")) == fingerprint(
        normalized_one(job_field="pentestJobId")
    )


def test_normalization_drops_exploit_bearing_fields():
    """NEV-2. These are absent because the output is built from an allow-list,
    so a field the service adds later cannot leak either."""
    normalized = normalize_finding(
        raw_finding(
            attackScript="curl -X POST --data 'evil' https://target/admin",
            verificationScript="python3 exploit.py",
            reasoning="chain the IDOR with the missing check to escalate",
            codeRemediationTask="apply this patch",
            alignmentRationale="matches the threat model",
            description="step 1: forge the installation_id",
        )
    )
    blob = json.dumps(normalized)
    for leak in ("curl", "exploit.py", "chain the IDOR", "forge the installation_id"):
        assert leak not in blob
    for field in (
        "attackScript",
        "verificationScript",
        "reasoning",
        "codeRemediationTask",
        "alignmentRationale",
        "description",
    ):
        assert field not in normalized


def test_an_undeclared_service_field_does_not_leak_through():
    """The schema is an open set; a deny-list would pass a new field."""
    normalized = normalize_finding(raw_finding(someFutureExploitField="secret detail"))
    assert "secret detail" not in json.dumps(normalized)


def test_resolved_and_false_positive_findings_are_dropped_and_counted():
    """Dropped because the service says they are not live work -- counted so
    the drop is visible rather than silent."""
    result = normalize_document(
        {
            "runDate": "2026-08-30",
            "findings": [
                raw_finding(status="ACTIVE"),
                raw_finding(name="a", status="RESOLVED"),
                raw_finding(name="b", status="FALSE_POSITIVE"),
                raw_finding(name="c", status="ACCEPTED"),
            ],
        }
    )
    assert result["identified_raw"] == 4, "raw count must be what the scanner reported"
    assert result["dropped_by_status"] == 2
    assert len(result["findings"]) == 2


def test_is_actionable_handles_a_missing_or_odd_status():
    assert is_actionable({}) is True
    assert is_actionable({"status": None}) is True
    assert is_actionable({"status": "resolved"}) is False


def test_primary_location_survives_a_malformed_codeLocations():
    for value in (None, "nope", [], [None], [{}], [{"filePath": ""}]):
        assert primary_location({"codeLocations": value}) == {
            "file_path": "",
            "line_start": None,
        }


def test_primary_location_skips_entries_without_a_path():
    assert primary_location(
        {"codeLocations": [{"lineStart": 1}, {"filePath": "a/b.py", "lineStart": 7}]}
    ) == {"file_path": "a/b.py", "line_start": 7}


def test_a_boolean_lineStart_is_not_treated_as_a_line_number():
    assert primary_location({"codeLocations": [{"filePath": "a.py", "lineStart": True}]})[
        "line_start"
    ] is None


def test_the_display_location_keeps_the_full_path_and_line():
    """Humans need the real location; the KEY must not use it. Keeping them as
    separate fields is what makes that separation checkable."""
    normalized = normalized_one(file_path="modules/gateway/src/routes/chat.py", line_start=142)
    assert normalized["file_path"] == "modules/gateway/src/routes/chat.py"
    assert normalized["line_start"] == 142
    assert normalized["key_files"] == ["chat.py"]


def test_keyable_files_dedupes_and_sorts_basenames():
    assert keyable_files(
        {
            "codeLocations": [
                {"filePath": "b/z.py"},
                {"filePath": "a/z.py"},
                {"filePath": "c/a.py"},
            ]
        }
    ) == ["a.py", "z.py"]


def test_keyable_files_tolerates_malformed_input():
    for value in (None, "nope", [None], [{"filePath": None}], [{"filePath": "  "}]):
        assert keyable_files({"codeLocations": value}) == []


def test_keyable_files_handles_windows_separators_and_trailing_slashes():
    assert keyable_files({"codeLocations": [{"filePath": "a\\b\\c.py"}]}) == ["c.py"]
    assert keyable_files({"codeLocations": [{"filePath": "a/b/"}]}) == ["b"]


def test_prose_signature_normalizes_churn():
    assert prose_signature("Budget Caps -- NEVER bind!") == "budget caps never bind"
    assert prose_signature(None) == ""
    assert prose_signature(42) == ""
    assert prose_signature("!!!") == ""


def test_extract_findings_tolerates_an_absent_array():
    assert extract_findings({}) == []
    assert extract_findings({"findings": None}) == []


def test_extract_findings_skips_non_object_entries():
    assert extract_findings({"findings": [raw_finding(), "junk", None]}) == [raw_finding()]


def test_a_non_array_findings_field_is_an_error():
    with pytest.raises(NormalizationError, match="must be an array"):
        extract_findings({"findings": {"a": 1}})


def test_an_unreadable_findings_document_is_an_error_not_zero_findings():
    """Zero findings reads exactly like a clean repo, and the nightly has
    already paid for a metered review by this point."""
    with pytest.raises(NormalizationError, match="cannot read"):
        load_raw_document(Path(_tmp()) / "absent.json")


def test_an_unparseable_findings_document_is_an_error():
    with pytest.raises(NormalizationError, match="not valid JSON"):
        load_raw_document(_write(Path(_tmp()), "raw.json", "{nope"))


def test_a_scalar_findings_document_is_an_error():
    with pytest.raises(NormalizationError, match="must hold an object or an array"):
        load_raw_document(_write(Path(_tmp()), "raw.json", "42"))


def test_a_bare_array_document_is_accepted():
    path = _write(Path(_tmp()), "raw.json", json.dumps([raw_finding()]))
    assert len(load_raw_document(path)["findings"]) == 1


def test_normalize_documents_sums_the_raw_counts_across_both_halves():
    """`identified_raw` merges with `sum` in the ledger schema, because the two
    concurrent halves each report their own."""
    tmp = Path(_tmp())
    review = write_raw(tmp, [raw_finding()], name="code-review-findings.json")
    pentest = write_raw(
        tmp, [raw_finding(name="Another", job_field="pentestJobId")], name="pentest-findings.json"
    )
    merged = normalize_documents([review, pentest])
    assert merged["identified_raw"] == 2
    assert len(merged["findings"]) == 2
    assert merged["run_date"] == "2026-08-30"


def test_normalize_documents_requires_at_least_one_document():
    with pytest.raises(NormalizationError, match="no findings documents"):
        normalize_documents([])


# --------------------------------------------------------------------------
# the ledger shard -- U2's schema, not a reshaped one
# --------------------------------------------------------------------------


def test_ledger_fields_are_accepted_by_the_U2_schema():
    """The fields this unit reports must validate as a `workflow` shard. Built
    through `build_shard` so the schema -- not this test -- is the authority."""
    normalized = normalize_document({"runDate": "2026-08-30", "findings": [raw_finding()]})
    fields = ledger_fields(build_result(normalized, empty_baseline()))
    shard = build_shard(
        "2026-08-30", "workflow.code-review", "2026-08-30T02:38:00Z", fields, load_schema()
    )
    assert shard["fields"]["identified_raw"] == 1
    assert shard["fields"]["identified_new_after_dedup"] == 1


def test_the_zero_new_night_writes_a_valid_shard_too():
    """The common night must still be recordable."""
    fields = ledger_fields(
        build_result(
            {"identified_raw": 4, "dropped_by_status": 0, "findings": [], "run_date": "2026-08-28"},
            empty_baseline(),
        )
    )
    shard = build_shard(
        "2026-08-28", "workflow.code-review", "2026-08-28T02:38:00Z", fields, load_schema()
    )
    assert shard["fields"]["identified_new_after_dedup"] == 0
    assert shard["fields"]["new_finding_ids"] == []


def test_the_ledger_carries_service_ids_not_fingerprints():
    """`new_finding_ids` is constrained to `^f-[0-9a-f]+$`; a bare sha256
    fingerprint would be rejected by the schema."""
    normalized = normalize_document({"runDate": "2026-08-30", "findings": [raw_finding()]})
    result = build_result(normalized, empty_baseline())
    ids = ledger_fields(result)["new_finding_ids"]
    assert ids == ["f-79066a5c-0000-4000-8000-000000000001"]
    assert result["new_findings"][0]["fingerprint"] not in ids


def test_ledger_fields_skip_ids_that_are_not_service_ids():
    result = build_result(
        {
            "identified_raw": 2,
            "dropped_by_status": 0,
            "run_date": "2026-08-30",
            "findings": [
                normalize_finding({"findingId": None, "name": "a", "codeLocations": []}),
                normalize_finding({"findingId": "xyz", "name": "b", "codeLocations": []}),
            ],
        },
        empty_baseline(),
    )
    assert ledger_fields(result)["new_finding_ids"] == []


def test_this_unit_writes_only_fields_it_owns():
    """Do not touch or reshape another stage's shard: every field reported here
    must be owned by `workflow` in `x-fields`."""
    schema = load_schema()
    normalized = normalize_document({"runDate": "2026-08-30", "findings": [raw_finding()]})
    for name in ledger_fields(build_result(normalized, empty_baseline())):
        assert "workflow" in schema["x-fields"][name]["stages"], (
            f"{name} is not a workflow-stage field"
        )


# --------------------------------------------------------------------------
# the runtime re-check of the spike's answer
# --------------------------------------------------------------------------


def test_the_profile_still_records_unstable_ids():
    """If this flips, the fingerprint is no longer the right key."""
    assert assert_ids_are_unstable(PROFILE_PATH) is None


def test_a_profile_claiming_stable_ids_is_refused():
    path = _write(
        Path(_tmp()),
        "profile.json",
        json.dumps({"findings": {"finding_id_stable": True}}),
    )
    with pytest.raises(DedupError, match="no longer records finding_id_stable=false"):
        assert_ids_are_unstable(path)


def test_a_missing_profile_is_refused():
    with pytest.raises(DedupError, match="cannot read the security agent profile"):
        assert_ids_are_unstable(Path(_tmp()) / "absent.json")


# --------------------------------------------------------------------------
# CLI -- the smoke test from the issue
# --------------------------------------------------------------------------


def test_smoke_running_twice_over_the_same_fixture_yields_zero_new(tmp_path):
    """The issue's smoke test: run twice over the same fixture and the second
    run reports `identified_new_after_dedup == 0`.

    Between the runs the baseline is refreshed, which is how a finding becomes
    known -- exactly the operator flow the refresh path exists for.
    """
    raw = write_raw(tmp_path, [raw_finding(), raw_finding(name="Tenant id unvalidated")])
    baseline = tmp_path / "baseline.json"
    baseline.write_text(serialize_baseline(empty_baseline()), encoding="utf-8")
    first_out = tmp_path / "first.json"

    assert main([
        "diff", "--findings", str(raw), "--baseline", str(baseline),
        "--output", str(first_out), "--profile", str(PROFILE_PATH),
    ]) == 0
    first = json.loads(first_out.read_text(encoding="utf-8"))
    assert first["identified_new_after_dedup"] == 2
    assert first["nothing_to_file"] is False

    assert main([
        "refresh-baseline", "--findings", str(raw), "--baseline", str(baseline),
        "--accepted-on", "2026-08-30", "--reason", "accepted_risk",
    ]) == 0

    second_out = tmp_path / "second.json"
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(baseline),
        "--output", str(second_out), "--profile", str(PROFILE_PATH),
    ]) == 0
    second = json.loads(second_out.read_text(encoding="utf-8"))
    assert second["identified_new_after_dedup"] == 0, (
        "the smoke test from the issue failed: a replayed fixture re-filed"
    )
    assert second["nothing_to_file"] is True


def test_the_cli_writes_a_ledger_shard_when_asked(tmp_path):
    """The shard is written wrapped, under a name derived from its stage id.

    Read back off disk and revalidated: `build_shard` validates on the way out,
    but what every downstream reader consumes is these BYTES, and the defect this
    replaced was precisely a file whose contents no reader accepted.
    """
    raw = write_raw(tmp_path, [raw_finding()])
    ledger = tmp_path / "ledger"
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--ledger-dir", str(ledger),
        "--source", "code-review", "--generated-at", "2026-08-30T02:38:00Z",
        "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
    ]) == 0

    shard = json.loads(
        (ledger / "shard-workflow.code-review.json").read_text(encoding="utf-8")
    )
    validate_shard(shard, load_schema())
    assert shard["stage"] == "workflow.code-review"
    assert shard["stage_type"] == "workflow"
    assert shard["run_date"] == "2026-08-30"
    assert shard["generated_at"] == "2026-08-30T02:38:00Z"
    assert set(shard["fields"]) == {
        "identified_raw", "identified_new_after_dedup", "new_finding_ids",
    }


def test_the_written_shard_is_accepted_by_the_ledger_reader(tmp_path):
    """The acceptance test: the shard goes into U2's real directory reader, which
    validates every shard's envelope and raises on the first missing field."""
    raw = write_raw(tmp_path, [raw_finding()])
    ledger = tmp_path / "ledger"
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--ledger-dir", str(ledger),
        "--source", "pentest", "--generated-at", "2026-08-30T02:38:00Z",
        "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
    ]) == 0
    assert [s["stage"] for s in load_shards(ledger)] == ["workflow.pentest"]


def test_the_two_halves_write_distinct_shards_into_one_directory(tmp_path):
    """`workflow.<source>`, per the ledger schema's own description: the halves
    run CONCURRENTLY into one prefix, and the schema merges `identified_raw` with
    `sum` precisely because each reports its own. A shared bare `workflow` id
    would have the second half to finish overwrite the first's counts."""
    raw = write_raw(tmp_path, [raw_finding()])
    ledger = tmp_path / "ledger"
    for source in SOURCES:
        assert main([
            "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
            "--output", str(tmp_path / f"out.{source}.json"), "--ledger-dir", str(ledger),
            "--source", source, "--generated-at", "2026-08-30T02:38:00Z",
            "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
        ]) == 0

    assert sorted(p.name for p in ledger.iterdir()) == [
        "shard-workflow.code-review.json",
        "shard-workflow.pentest.json",
    ], "one half overwrote the other's counts"
    # Both merge cleanly, which is what `sum` over two distinct stage ids means.
    assert merge_shards(load_shards(ledger))["fields"]["identified_raw"] == 2


def test_a_shard_cannot_be_written_under_an_undeclared_source():
    with pytest.raises(DedupError, match="is not one of"):
        workflow_stage("nmap")


def test_the_shard_stage_is_never_a_bare_workflow():
    for source in SOURCES:
        assert workflow_stage(source) != "workflow"
        assert workflow_stage(source).startswith("workflow.")


def test_writing_a_shard_needs_a_source_and_a_timestamp(tmp_path, capsys):
    """Both are required with `--ledger-dir`: without the source the two halves
    cannot be told apart, and without the timestamp a re-run is not reproducible."""
    raw = write_raw(tmp_path, [raw_finding()])
    ledger = tmp_path / "ledger"
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--ledger-dir", str(ledger),
        "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
    ]) == 1
    assert "--ledger-dir needs --source and --generated-at" in capsys.readouterr().err
    assert not ledger.exists()


def test_a_shard_cannot_be_written_without_a_run_date(tmp_path, capsys):
    """`run_date` is what places the shard under tonight's prefix. A document with
    no `runDate` and no `--run-date` leaves it None, and a shard under the wrong
    night's prefix is a night whose counts silently belong to another run."""
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"findings": [raw_finding()]}), encoding="utf-8")
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--ledger-dir", str(tmp_path / "ledger"),
        "--source", "code-review", "--generated-at", "2026-08-30T02:38:00Z",
        "--profile", str(PROFILE_PATH),
    ]) == 1
    assert "without a run date" in capsys.readouterr().err


def test_the_quiet_night_still_writes_a_valid_shard(tmp_path):
    """The common night must be recordable, and recorded as zeros rather than as
    an absent shard a reader cannot distinguish from a stage that never ran."""
    raw = write_raw(tmp_path, [])
    ledger = tmp_path / "ledger"
    assert main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--ledger-dir", str(ledger),
        "--source", "code-review", "--generated-at", "2026-08-30T02:38:00Z",
        "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
    ]) == 0
    shard = load_shards(ledger)[0]
    assert shard["fields"]["identified_new_after_dedup"] == 0
    assert shard["fields"]["new_finding_ids"] == []


def test_the_shard_is_byte_reproducible_across_a_rerun(tmp_path):
    """FR-C30: same inputs and same caller-supplied timestamp, same bytes."""
    raw = write_raw(tmp_path, [raw_finding()])
    for name in ("a", "b"):
        assert main([
            "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
            "--output", str(tmp_path / f"out.{name}.json"),
            "--ledger-dir", str(tmp_path / name),
            "--source", "code-review", "--generated-at", "2026-08-30T02:38:00Z",
            "--run-date", "2026-08-30", "--profile", str(PROFILE_PATH),
        ]) == 0
    shard = "shard-workflow.code-review.json"
    assert (tmp_path / "a" / shard).read_bytes() == (tmp_path / "b" / shard).read_bytes()


def test_the_cli_log_carries_counts_but_no_prose(tmp_path, capsys):
    """A CI log is readable by anyone who can see the run (NEV-2)."""
    raw = write_raw(tmp_path, [raw_finding()])
    main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--profile", str(PROFILE_PATH),
    ])
    out = capsys.readouterr().out
    assert "identified_new_after_dedup=1" in out
    for leak in ("Budget caps", "modules/gateway/src/routes/chat.py"):
        assert leak not in out


def test_the_cli_reports_the_quiet_night_in_words(tmp_path, capsys):
    raw = write_raw(tmp_path, [])
    main([
        "diff", "--findings", str(raw), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--profile", str(PROFILE_PATH),
    ])
    assert "nothing to file" in capsys.readouterr().out.lower()


def test_the_cli_turns_a_bad_document_into_an_exit_code(tmp_path, capsys):
    bad = _write(tmp_path, "raw.json", "{nope")
    rc = main([
        "diff", "--findings", str(bad), "--baseline", str(tmp_path / "absent.json"),
        "--output", str(tmp_path / "out.json"), "--profile", str(PROFILE_PATH),
    ])
    assert rc == 1
    assert "::error title=Security findings dedup::" in capsys.readouterr().err


def test_the_refresh_cli_rejects_a_reason_outside_the_enum(tmp_path):
    """argparse `choices` closes the enum at the CLI boundary too."""
    raw = write_raw(tmp_path, [raw_finding()])
    with pytest.raises(SystemExit):
        main([
            "refresh-baseline", "--findings", str(raw),
            "--baseline", str(tmp_path / "b.json"),
            "--accepted-on", "2026-08-30", "--reason", "whatever",
        ])


def test_the_refresh_cli_writes_a_committable_baseline(tmp_path, capsys):
    raw = write_raw(tmp_path, [raw_finding()])
    baseline = tmp_path / "baseline.json"
    assert main([
        "refresh-baseline", "--findings", str(raw), "--baseline", str(baseline),
        "--accepted-on", "2026-08-30", "--reason", "false_positive",
    ]) == 0
    assert validate_baseline(json.loads(baseline.read_text(encoding="utf-8")))
    assert "pull request" in capsys.readouterr().out


# --------------------------------------------------------------------------
# regression checks -- the existing pipeline is untouched
# --------------------------------------------------------------------------


def _git(*argv):
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", *argv], cwd=REPO_ROOT, capture_output=True, text=True
    )


def _main_ref():
    """A ref for `main` that exists HERE, fetching it if necessary.

    `actions/checkout@v7` clones shallow and single-branch, so `origin/main`
    does NOT exist on the runner. Every "unchanged vs main" gate that simply
    skips when the ref is missing therefore skips in CI -- passing locally and
    asserting nothing where it matters. That is the silent-coverage-loss the
    Script Tests workflow exists to prevent, so these gates fetch the ref
    instead of shrugging. Only a genuinely offline checkout skips, and it says
    so with a reason.
    """
    for ref in ("origin/main", "main"):
        if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
            return ref
    # Not present: ask the remote for it. --depth=1 keeps this cheap.
    if _git("fetch", "--depth=1", "origin", "main").returncode == 0:
        for ref in ("FETCH_HEAD", "origin/main"):
            if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
                return ref
    return None


def _assert_unchanged_vs_main(path: str):
    """Assert one path is byte-identical to `main`."""
    ref = _main_ref()
    if ref is None:
        pytest.skip("no network and no local main ref; cannot compare against main")
    completed = _git("diff", "--exit-code", ref, "--", path)
    assert completed.returncode == 0, (
        f"{path} differs from main:\n{completed.stdout}"
    )


def test_security_scan_workflow_is_byte_identical_to_main():
    """The issue's explicit gate. The existing scan pipeline must not change
    behaviour as a side effect of this EPIC."""
    _assert_unchanged_vs_main(".github/workflows/security-scan.yml")


def _assert_dedup_workflow_scope(changed: set[str]):
    """The unit's scope restriction applies when its implementation changes.

    Unrelated PRs also run Script Tests. They must be able to maintain other
    workflows (for example AIDLC reminders) without changing this unit's policy.
    """
    unit_paths = {
        ".github/scripts/dedup_security_findings.py",
        ".github/scripts/normalize_security_findings.py",
    }
    if changed.isdisjoint(unit_paths):
        return
    workflows = {p for p in changed if p.startswith(".github/workflows/")}
    allowed = {
        ".github/workflows/security-agent-nightly.yml",
        ".github/workflows/script-tests.yml",
    }
    assert workflows <= allowed, (
        f"unexpected workflow changes alongside the dedup unit: {sorted(workflows - allowed)}"
    )


def test_this_unit_touches_no_workflow_but_the_test_binding():
    """Dedup implementation changes must stay within the unit's pipeline scope.

    The separate security-scan byte-identity and private-findings checks still
    run for every PR; unrelated workflow edits do not bypass those protections.
    """
    ref = _main_ref()
    if ref is None:
        pytest.skip("no network and no local main ref; cannot compare against main")
    completed = _git("diff", "--name-only", ref, "--")
    assert completed.returncode == 0, f"git diff failed: {completed.stderr}"
    _assert_dedup_workflow_scope(set(completed.stdout.splitlines()))


def test_unrelated_workflow_maintenance_is_outside_the_dedup_units_scope():
    _assert_dedup_workflow_scope({".github/workflows/aidlc-gate-nudge.yml"})


@pytest.mark.parametrize("unit", ["dedup_security_findings.py", "normalize_security_findings.py"])
@pytest.mark.parametrize("workflow", ["security-scan.yml", "aidlc-gate-nudge.yml"])
def test_dedup_changes_still_reject_out_of_scope_workflow_edits(unit, workflow):
    with pytest.raises(AssertionError, match="unexpected workflow changes"):
        _assert_dedup_workflow_scope({f".github/scripts/{unit}", f".github/workflows/{workflow}"})


def test_dedup_changes_can_update_their_own_workflow_bindings():
    _assert_dedup_workflow_scope({
        ".github/scripts/dedup_security_findings.py",
        ".github/workflows/security-agent-nightly.yml",
        ".github/workflows/script-tests.yml",
    })


def test_no_public_artifact_upload_path_exists_for_findings():
    """Findings go to the private rendezvous. `upload-artifact` on a findings
    path would make the whole repo's findings world-readable to anyone who can
    see the run."""
    text = SECURITY_SCAN_WORKFLOW.read_text(encoding="utf-8")
    assert "actions/upload-artifact" not in text, (
        "an artifact upload in the scan workflow is a public findings path"
    )
    for path in (
        REPO_ROOT / ".github/scripts/dedup_security_findings.py",
        REPO_ROOT / ".github/scripts/normalize_security_findings.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "upload-artifact" not in source
        assert "public-read" not in source


def test_the_existing_findings_differ_is_untouched():
    """This unit mirrors `diff_security_findings.py`'s model; it does not edit
    it. Its callers must keep passing."""
    _assert_unchanged_vs_main(".github/scripts/diff_security_findings.py")


def test_the_ledger_schema_contract_this_unit_relies_on_is_untouched():
    """The fields this unit writes already exist in U2's schema, and it reshapes
    no stage's contract.

    Asserted as "nothing this unit relies on CHANGED", not as whole-file byte
    identity. The original byte-identity form over-reached: it also forbade a
    later stage from DECLARING ITS OWN new field, which U2's validator requires
    before that stage can write one (an undeclared field is rejected at write
    time -- NT-11). U10's join barrier hit exactly that, needing to record which
    scanner never signalled. Additive declarations by the owning stage are the
    intended way to extend this schema; what must not change is the shape
    anything already depends on, which is what this now checks.
    """
    ref = _main_ref()
    if ref is None:
        pytest.skip("no network and no local main ref; cannot compare against main")
    path = ".github/security/ledger-schema.json"
    completed = _git("show", f"{ref}:{path}")
    assert completed.returncode == 0, f"cannot read {path} from {ref}"
    before = json.loads(completed.stdout)
    after = json.loads((REPO_ROOT / path).read_text(encoding="utf-8"))

    # The envelope, the stuck rule and every previously-declared field must be
    # unchanged: those are what other stages read.
    for key in ("schema_version", "properties", "required", "x-story-status", "x-stuck-rule"):
        assert after.get(key) == before.get(key), f"{key} changed in {path}"
    for name, spec in before["x-fields"].items():
        assert after["x-fields"].get(name) == spec, f"field {name!r} was reshaped"
    for name in before["x-report-allowlist"]:
        assert name in after["x-report-allowlist"], f"{name!r} dropped from the report"

    # This unit in particular adds nothing at all.
    assert set(after["x-fields"]) - set(before["x-fields"]) <= {"unsignalled_sources"}, (
        "an unexpected field was added to the ledger schema"
    )


def test_this_suite_is_pinned_into_script_tests():
    """An unpinned suite never runs, which makes the gate citing it vacuous."""
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_security_agent_dedup.py" in text, (
        "the new suite is not pinned into Script Tests, so it never runs in CI"
    )
    for path in (
        ".github/scripts/dedup_security_findings.py",
        ".github/scripts/normalize_security_findings.py",
        ".github/security/security-agent-baseline.json",
    ):
        assert path in text, f"{path} is not in the Script Tests paths filter"


# --------------------------------------------------------------------------
# tiny local helpers (kept at the bottom: they are plumbing, not gates)
# --------------------------------------------------------------------------


def _tmp():
    import tempfile

    return tempfile.mkdtemp()


def _write(directory, name, text):
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path
