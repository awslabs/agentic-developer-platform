"""Reading the abort sentinel the Node worker writes (Issue #3963, S4).

An abort is decided in the agent process and *finalized* in the supervising one:
status ``aborted``, check conclusion ``cancelled``, controls revoked, SQS message
deleted so the run never restarts. The sentinel is how the decision crosses that
process boundary, and this suite pins the one property that makes the crossing
safe to act on.

That property is an asymmetry, not a symmetry. Losing an abort signal costs a
mislabelled outcome an operator can see and re-issue. Fabricating one deletes a
live run's queue message and reports a crash as a deliberate stop. So every case
below that feeds the reader something imperfect — absent, truncated, oversized,
stale, wrongly-typed, from another run — asserts ``None``, meaning "no abort
happened". Only a well-formed document bound to *this* run and *this* control
generation is allowed to produce an abort.

The rules are duplicated by design in ``control-abort-sentinel.ts``. The case
table here deliberately mirrors that suite's, because a rule enforced on one side
of the bridge is not enforced.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.abort_sentinel import (
    ABORT_SENTINEL_VERSION,
    MAX_SENTINEL_BYTES,
    MAX_SENTINEL_REASON_LENGTH,
    bound_sentinel_reason,
    read_abort_sentinel,
    validate_abort_sentinel,
)

RUN_ID = "run-abc"
GENERATION = 4


def _document(**overrides) -> dict:
    """A valid sentinel for this run, with targeted fields replaced."""
    document = {
        "version": ABORT_SENTINEL_VERSION,
        "run_id": RUN_ID,
        "generation": GENERATION,
        "command_id": "cmd-1",
        "requested_at": "2026-09-23T00:00:00Z",
        "reason": None,
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not _ABSENT}


_ABSENT = object()


@pytest.fixture()
def sentinel_path(tmp_path) -> str:
    """An isolated path, so a developer's real /tmp sentinel cannot affect a run."""
    return str(tmp_path / "adp-abort-sentinel.json")


def _write(path: str, payload: object) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)


class TestAcceptsAGenuineAbort:
    """The one case that is allowed to say "this run was aborted"."""

    def test_reads_a_sentinel_bound_to_this_run_and_generation(self, sentinel_path):
        _write(sentinel_path, _document(reason="wrong branch"))

        sentinel = read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path)

        assert sentinel is not None
        assert sentinel["run_id"] == RUN_ID
        assert sentinel["generation"] == GENERATION
        assert sentinel["command_id"] == "cmd-1"
        assert sentinel["reason"] == "wrong branch"

    def test_accepts_the_generation_as_the_string_the_child_env_carries(self, sentinel_path):
        # ADP_CONTROL_GENERATION reaches the child process as a string, while
        # register_control_endpoint returns an int. Both are real call shapes.
        _write(sentinel_path, _document())

        assert read_abort_sentinel(RUN_ID, "4", path=sentinel_path) is not None

    def test_tolerates_unknown_extra_fields_within_the_same_version(self, sentinel_path):
        # Forward tolerance inside a version: an unfamiliar field is not grounds
        # to discard a correctly bound abort.
        _write(sentinel_path, _document(future_field="ignored"))

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is not None

    def test_reports_no_reason_as_none_rather_than_empty_string(self, sentinel_path):
        _write(sentinel_path, _document())

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path)["reason"] is None


class TestRefusesWhatItCannotValidate:
    """Every one of these means "no abort happened"."""

    def test_absent_sentinel_is_not_an_abort(self, sentinel_path):
        # The overwhelmingly common case: no operator asked for an abort.
        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    def test_truncated_document_is_not_an_abort_and_does_not_raise(self, sentinel_path):
        # What a torn read looks like. A raise here happens during teardown and
        # could cost the SQS acknowledgement entirely.
        _write(sentinel_path, '{"version": 1, "run_id": "run-a')

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    def test_empty_file_is_not_an_abort(self, sentinel_path):
        _write(sentinel_path, "")

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    def test_a_directory_at_the_path_is_not_an_abort(self, tmp_path):
        directory = tmp_path / "adp-abort-sentinel.json"
        directory.mkdir()

        assert read_abort_sentinel(RUN_ID, GENERATION, path=str(directory)) is None

    def test_oversized_file_is_refused_without_being_parsed(self, sentinel_path):
        # Guards against loading an arbitrary large /tmp file into memory during
        # teardown just because it occupies the sentinel's name.
        padded = _document()
        padded["reason"] = "x" * (MAX_SENTINEL_BYTES + 100)
        _write(sentinel_path, padded)

        assert os.path.getsize(sentinel_path) > MAX_SENTINEL_BYTES
        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    @pytest.mark.parametrize(
        "payload",
        ["[]", '"aborted"', "1", "null", "true"],
        ids=["array", "string", "number", "null", "bool"],
    )
    def test_non_object_json_is_not_an_abort(self, sentinel_path, payload):
        # json.load returns these happily; each would raise on .get().
        _write(sentinel_path, payload)

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"run_id": "run-other"},
            {"generation": GENERATION - 1},
            {"generation": GENERATION + 1},
        ],
        ids=["different_run", "superseded_generation", "future_generation"],
    )
    def test_a_sentinel_from_another_run_or_attempt_is_not_an_abort(self, sentinel_path, overrides):
        # The whole point of the binding. A leftover file must never let an
        # unrelated live run finalize itself as deliberately stopped.
        _write(sentinel_path, _document(**overrides))

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"version": ABORT_SENTINEL_VERSION + 1},
            {"version": ABORT_SENTINEL_VERSION - 1},
            {"version": "1"},
            {"run_id": _ABSENT},
            {"run_id": ""},
            {"run_id": 123},
            {"generation": _ABSENT},
            {"generation": "4"},
            {"generation": 4.5},
            {"generation": True},
            {"command_id": _ABSENT},
            {"command_id": ""},
            {"command_id": 7},
            {"requested_at": _ABSENT},
            {"requested_at": ""},
        ],
    )
    def test_a_malformed_field_is_not_an_abort(self, sentinel_path, overrides):
        _write(sentinel_path, _document(**overrides))

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None

    def test_a_json_true_generation_does_not_read_as_generation_one(self, sentinel_path):
        # isinstance(True, int) is True in Python. Without the explicit bool
        # check this document would validate against generation 1 — a real
        # cross-language divergence, since TypeScript's typeof rejects it.
        _write(sentinel_path, _document(generation=True))

        assert read_abort_sentinel(RUN_ID, True, path=sentinel_path) is None


class TestRefusesAnUnusableBinding:
    """No run identity means nothing to validate against, so nothing is honoured."""

    @pytest.mark.parametrize("run_id", ["", None], ids=["blank", "none"])
    def test_missing_run_id_refuses_even_a_well_formed_sentinel(self, sentinel_path, run_id):
        _write(sentinel_path, _document())

        assert read_abort_sentinel(run_id, GENERATION, path=sentinel_path) is None

    @pytest.mark.parametrize(
        "generation",
        [None, "", "abc", 0, -1, "0", 4.5],
        ids=["none", "blank", "words", "zero", "negative", "zero_string", "fractional"],
    )
    def test_unusable_generation_refuses_even_a_well_formed_sentinel(
        self, sentinel_path, generation
    ):
        # A run whose control channel never registered has no generation, and a
        # sentinel it cannot be compared against must not be trusted.
        _write(sentinel_path, _document())

        assert read_abort_sentinel(RUN_ID, generation, path=sentinel_path) is None


class TestReasonBounding:
    """The reason is operator input on its way into a GitHub comment."""

    def test_truncates_an_over_long_reason(self):
        bounded = bound_sentinel_reason("x" * (MAX_SENTINEL_REASON_LENGTH + 50))

        assert len(bounded) == MAX_SENTINEL_REASON_LENGTH

    def test_collapses_newlines_so_it_cannot_break_comment_layout(self):
        assert bound_sentinel_reason("  wrong\n\nbranch  ") == "wrong branch"

    @pytest.mark.parametrize(
        "reason",
        [None, "", "   ", 42, {"injected": True}, ["a"]],
        ids=["none", "empty", "whitespace", "number", "dict", "list"],
    )
    def test_absent_or_non_string_reasons_become_none(self, reason):
        # str({...}) would put "{'injected': True}" in front of an operator.
        assert bound_sentinel_reason(reason) is None

    def test_re_bounds_an_over_long_reason_found_inside_a_stored_sentinel(self):
        validated = validate_abort_sentinel(
            _document(reason="y" * (MAX_SENTINEL_REASON_LENGTH + 100)), RUN_ID, GENERATION
        )

        assert len(validated["reason"]) == MAX_SENTINEL_REASON_LENGTH


class TestCrossLanguageContract:
    """The two readers are one contract; drift between them is the failure mode."""

    def test_python_and_typescript_agree_on_version_path_and_reason_cap(self):
        source = (
            Path(__file__).resolve().parents[2] / "agent" / "src" / "control-abort-sentinel.ts"
        ).read_text(encoding="utf-8")

        assert f"ABORT_SENTINEL_VERSION = {ABORT_SENTINEL_VERSION}" in source
        assert "ABORT_SENTINEL_PATH = '/tmp/adp-abort-sentinel.json'" in source
        assert f"MAX_SENTINEL_REASON_LENGTH = {MAX_SENTINEL_REASON_LENGTH}" in source
