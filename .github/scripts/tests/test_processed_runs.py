"""Tests for the processed-scan run log (#4792).

The claim under test is narrow and worth stating: **the same findings file is
never turned into issues twice, and everything else still gets processed.** Both
halves matter. A log that blocks too much silently drops security findings, which
is worse than the duplication it was built to stop.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import processed_runs as pr
from processed_runs import ProcessedRunsError, main

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / ".github/scripts/processed_runs.py"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/script-tests.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/security-agent-nightly.yml"

RUN_DATE = "2026-08-30"
STARTED = "2026-09-07T17:16:04Z"
FINISHED = "2026-09-07T17:34:12Z"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _findings(tmp_path: Path, body: str = '{"findings": []}', name="code-review-findings.json") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _entry(**overrides) -> dict:
    kwargs = {
        "run_id": "34117676808",
        "source": "code-review",
        "run_date": RUN_DATE,
        "findings_key": "security-agent/runs/2026-08-30/code-review-findings.json",
        "version": "sha256:" + "a" * 64,
        "started_at": STARTED,
        "finished_at": FINISHED,
        "status": "success",
        "findings_total": 62,
        "work_items_filed": 31,
        "daily_epic": 4700,
    }
    kwargs.update(overrides)
    return pr.build_entry(**kwargs)


def _log_with(*entries) -> dict:
    log = pr.empty_log()
    for entry in entries:
        log = pr.append(log, entry)
    return log


# --------------------------------------------------------------------------
# 1. the version key is the file's content
# --------------------------------------------------------------------------


def test_the_same_bytes_version_the_same_and_different_bytes_do_not(tmp_path):
    a = _findings(tmp_path / "a", '{"findings": [1]}')
    b = _findings(tmp_path / "b", '{"findings": [1]}')
    c = _findings(tmp_path / "c", '{"findings": [2]}')
    assert pr.findings_version(a) == pr.findings_version(b)
    assert pr.findings_version(a) != pr.findings_version(c)
    assert pr.findings_version(a).startswith("sha256:")


def test_versioning_an_unreadable_file_is_an_error(tmp_path):
    with pytest.raises(ProcessedRunsError, match="cannot read the findings file"):
        pr.findings_version(tmp_path / "missing.json")


# --------------------------------------------------------------------------
# 2. the question: has this file been processed?
# --------------------------------------------------------------------------


def test_a_file_with_a_success_entry_is_already_processed():
    entry = _entry()
    found = pr.already_processed(
        _log_with(entry), version=entry["findings_version"], source="code-review"
    )
    assert found is not None and found["run_id"] == "34117676808"


def test_a_file_with_only_a_failed_entry_is_not_processed():
    """A failed night must stay retryable, or a transient fault would make a scan
    permanently unprocessable -- strictly worse than the duplication this prevents."""
    entry = _entry(status="failed", work_items_filed=None, daily_epic=None)
    assert (
        pr.already_processed(
            _log_with(entry), version=entry["findings_version"], source="code-review"
        )
        is None
    )


def test_a_failed_then_successful_run_is_processed():
    version = "sha256:" + "b" * 64
    log = _log_with(
        _entry(version=version, status="failed", run_id="1", work_items_filed=None, daily_epic=None),
        _entry(version=version, status="success", run_id="2"),
    )
    found = pr.already_processed(log, version=version, source="code-review")
    assert found is not None and found["run_id"] == "2"


def test_the_other_scanner_has_not_processed_it():
    """The two scanners process the same date independently, so the source is part
    of the key: a file consumed by one has not been consumed by the other."""
    entry = _entry(source="code-review")
    assert (
        pr.already_processed(
            _log_with(entry), version=entry["findings_version"], source="pentest"
        )
        is None
    )


def test_a_republished_file_for_the_same_date_is_not_processed():
    """The regression that keying on the DATE would cause. Different bytes for the
    same date is a genuine re-scan and must be processed, or real findings are lost."""
    log = _log_with(_entry(version="sha256:" + "c" * 64))
    assert pr.already_processed(log, version="sha256:" + "d" * 64, source="code-review") is None


def test_an_unknown_source_is_rejected():
    with pytest.raises(ProcessedRunsError, match="is not one of"):
        pr.already_processed(pr.empty_log(), version="sha256:x", source="made-up")


# --------------------------------------------------------------------------
# 3. loading: a missing log is empty, a corrupt one is an error
# --------------------------------------------------------------------------


def test_a_missing_log_reads_as_nothing_processed(tmp_path):
    """The state of every environment before its first run. Treating it as an error
    would make the first run fail for being first."""
    assert pr.load(tmp_path / "absent.json") == pr.empty_log()


def test_a_corrupt_log_is_an_error_not_an_empty_one(tmp_path):
    """Reading a corrupt log as empty would re-process every scan it recorded."""
    path = tmp_path / "log.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProcessedRunsError, match="Refusing to treat a corrupt log"):
        pr.load(path)


@pytest.mark.parametrize(
    "document,match",
    [
        ([], "must be an object"),
        ({"schema_version": "999", "runs": []}, "unsupported run-log schema_version"),
        ({"schema_version": "1", "runs": {}}, "`runs` must be an array"),
        ({"schema_version": "1", "runs": [[]]}, "must be an object"),
    ],
    ids=["array", "wrong-version", "runs-not-array", "entry-not-object"],
)
def test_malformed_logs_are_rejected(document, match):
    with pytest.raises(ProcessedRunsError, match=match):
        pr.validate(document)


def test_an_entry_with_an_undeclared_field_is_rejected():
    """An allow-list, like every artifact in this pipeline: this document decides
    whether work happens, and a field nobody declared is one nobody validated."""
    entry = {**_entry(), "surprise": "x"}
    with pytest.raises(ProcessedRunsError, match="undeclared fields"):
        pr.validate({"schema_version": "1", "runs": [entry]})


@pytest.mark.parametrize("field", pr._REQUIRED_ENTRY_FIELDS)
def test_every_required_entry_field_is_required(field):
    entry = {k: v for k, v in _entry().items() if k != field}
    with pytest.raises(ProcessedRunsError, match="missing or empties"):
        pr.validate({"schema_version": "1", "runs": [entry]})


@pytest.mark.parametrize("status", ["done", "SUCCESS", "", "in_progress"])
def test_an_unknown_status_is_rejected(status):
    """A typo'd status would neither block a re-run nor read as a failure."""
    with pytest.raises(ProcessedRunsError):
        _entry(status=status)


def test_unknown_counts_are_omitted_not_zeroed():
    """A 0 is a value a reader records as true and cannot tell from a real count --
    the same reasoning the ledger applies to its absent daily_epic."""
    entry = _entry(findings_total=None, work_items_filed=None, daily_epic=None)
    for absent in ("findings_total", "work_items_filed", "daily_epic"):
        assert absent not in entry


# --------------------------------------------------------------------------
# 4. appending and writing
# --------------------------------------------------------------------------


def test_appending_preserves_existing_entries_and_does_not_mutate():
    first = _entry(run_id="1", version="sha256:" + "1" * 64)
    log = _log_with(first)
    before = json.dumps(log, sort_keys=True)
    grown = pr.append(log, _entry(run_id="2", version="sha256:" + "2" * 64))
    assert [e["run_id"] for e in grown["runs"]] == ["1", "2"]
    assert json.dumps(log, sort_keys=True) == before


def test_the_log_is_written_deterministically(tmp_path):
    """A re-run of the same night produces byte-identical output, so a diff means
    a real change."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    pr.write(a, _log_with(_entry()))
    pr.write(b, _log_with(_entry()))
    assert a.read_bytes() == b.read_bytes()


def test_the_whole_object_is_rewritten_each_time(tmp_path):
    """Deliberate: the findings bucket has one lifecycle rule with NO prefix filter
    and a 365-day expiration, so an object never rewritten eventually ages out.
    One object per run would age out entry by entry and silently weaken the check."""
    path = tmp_path / "log.json"
    pr.write(path, _log_with(_entry(run_id="1", version="sha256:" + "1" * 64)))
    pr.write(path, _log_with(
        _entry(run_id="1", version="sha256:" + "1" * 64),
        _entry(run_id="2", version="sha256:" + "2" * 64),
    ))
    assert len(pr.load(path)["runs"]) == 2


# --------------------------------------------------------------------------
# 5. the CLI, which is what the workflow actually calls
# --------------------------------------------------------------------------


def test_check_reports_false_on_a_fresh_log(tmp_path, capsys):
    findings = _findings(tmp_path)
    rc = main(["check", "--log", str(tmp_path / "log.json"),
               "--source", "code-review", "--findings", str(findings)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "processed=false" in out
    assert f"findings_version={pr.findings_version(findings)}" in out


def test_check_reports_true_after_a_successful_record(tmp_path, capsys):
    """The round trip that matters: record a run, then a second check on the same
    bytes says already-processed and names the run that did it."""
    findings = _findings(tmp_path)
    log = tmp_path / "log.json"
    argv = ["record", "--log", str(log), "--source", "code-review",
            "--findings", str(findings), "--run-id", "34117676808",
            "--run-date", RUN_DATE, "--started-at", STARTED,
            "--finished-at", FINISHED, "--status", "success",
            "--findings-total", "62", "--work-items-filed", "31", "--daily-epic", "4700"]
    assert main(argv) == 0
    capsys.readouterr()

    rc = main(["check", "--log", str(log), "--source", "code-review",
               "--findings", str(findings)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "processed=true" in out
    assert "34117676808" in out


def test_check_exits_zero_when_already_processed(tmp_path, capsys):
    """A skip is a normal outcome, not a failure: the workflow reads the printed
    value, so a non-zero exit would turn an expected skip into a red run."""
    findings = _findings(tmp_path)
    log = tmp_path / "log.json"
    pr.write(log, _log_with(_entry(version=pr.findings_version(findings))))
    assert main(["check", "--log", str(log), "--source", "code-review",
                 "--findings", str(findings)]) == 0
    assert "processed=true" in capsys.readouterr().out


def test_record_reuses_a_precomputed_version(tmp_path, capsys):
    """`record` takes the version `check` compared, so the two cannot disagree if
    the file changed underneath the run."""
    log = tmp_path / "log.json"
    version = "sha256:" + "e" * 64
    argv = ["record", "--log", str(log), "--source", "code-review",
            "--findings-version", version, "--run-id", "1", "--run-date", RUN_DATE,
            "--started-at", STARTED, "--finished-at", FINISHED, "--status", "success"]
    assert main(argv) == 0
    capsys.readouterr()
    assert pr.load(log)["runs"][0]["findings_version"] == version


def test_record_without_a_file_or_a_version_fails(tmp_path, capsys):
    argv = ["record", "--log", str(tmp_path / "log.json"), "--source", "code-review",
            "--run-id", "1", "--run-date", RUN_DATE, "--started-at", STARTED,
            "--finished-at", FINISHED, "--status", "success"]
    assert main(argv) == 1
    assert "needs --findings or --findings-version" in capsys.readouterr().err


def test_a_corrupt_log_fails_the_cli_loudly(tmp_path, capsys):
    log = tmp_path / "log.json"
    log.write_text("{broken", encoding="utf-8")
    rc = main(["check", "--log", str(log), "--source", "code-review",
               "--findings", str(_findings(tmp_path))])
    assert rc == 1
    assert "::error title=Security run log::" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 6. structural gates
# --------------------------------------------------------------------------


def test_this_step_touches_no_github_state():
    """A file in, a file out -- the same shape as every other script in this
    directory. This decides whether issues get filed; it must not file any."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("_gh", "subprocess", "issue create", "adp-trigger", "boto3"):
        assert forbidden not in source, f"{forbidden!r} appears in a step that files nothing"


def test_no_agent_mention_literal_in_the_source():
    import re

    offenders = [
        line
        for line in SCRIPT_PATH.read_text(encoding="utf-8").splitlines()
        if re.search(r"@agent-[a-z]", line, re.IGNORECASE)
    ]
    assert offenders == []


def test_this_suite_and_its_subject_are_pinned_into_script_tests():
    """An unpinned suite never runs in CI, which makes every gate above decorative."""
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_processed_runs.py" in text
    assert text.count(".github/scripts/processed_runs.py") == 2, (
        "the subject script must be in BOTH the push and pull_request path filters"
    )


def test_the_nightly_checks_before_it_processes_and_records_after():
    """An unreferenced log is dead code: the duplication it prevents happens in the
    nightly, so the nightly has to consult it."""
    text = NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
    assert "processed_runs.py check" in text, "the nightly never checks the log"
    assert "processed_runs.py record" in text, "the nightly never records into the log"
    assert text.index("processed_runs.py check") < text.index("processed_runs.py record"), (
        "the check must come before the record"
    )
