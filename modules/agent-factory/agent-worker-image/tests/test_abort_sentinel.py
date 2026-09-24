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

import base64
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.abort_sentinel import (
    ABORT_SENTINEL_PATH,
    ABORT_SENTINEL_VERSION,
    MAX_SAFE_GENERATION,
    MAX_SENTINEL_BYTES,
    MAX_SENTINEL_ENVELOPE_BYTES,
    MAX_SENTINEL_REASON_LENGTH,
    MAX_SIGNED_BODY_BYTES,
    _coerce_generation,
    authorized_abort_reason,
    bound_sentinel_reason,
    read_abort_sentinel,
    validate_abort_sentinel,
)

RUN_ID = "run-abc"
GENERATION = 4

# The exact request body the gateway signed, base64 — the field the operator's
# reason is derived from. Every valid sentinel carries one: a document without it
# cannot have its reason bound to the signature, so the reader refuses it.
#
# Ordinary JSON, no credential. Nothing in this module signs or verifies anything,
# so no test here shows a reason is *authorized*; that is
# ``test_abort_authorization.py``, which generates keys per run. What this file
# pins is which documents parse and what text is derived from bytes already
# accepted.
SIGNED_BODY = base64.b64encode(
    json.dumps({"command_id": "cmd-1", "reason": "wrong branch"}).encode("utf-8")
).decode("ascii")

# The gateway's acceptance receipt — Issue #3963 review finding 1.
#
# Signed by nothing, deliberately. This module pins the sentinel's SHAPE contract:
# that the acceptance field is required, string-typed and bounded. Whether a receipt
# actually verifies against a gateway key is a different question, answered in
# ``test_abort_authorization.py`` with keys generated per run. The three-part form is
# used only so the value is a plausible token rather than arbitrary text.
ABORT_RECEIPT = "adpe1.eyJhY3Rpb24iOiJhYm9ydF9hY2NlcHRlZCJ9.cmVjZWlwdC1zaWduYXR1cmU"


def _document(**overrides) -> dict:
    """A valid sentinel for this run, with targeted fields replaced."""
    document = {
        "version": ABORT_SENTINEL_VERSION,
        "run_id": RUN_ID,
        "generation": GENERATION,
        "command_id": "cmd-1",
        "requested_at": "2026-09-23T00:00:00Z",
        # Both required. ``abort_receipt`` is the gateway's signed attestation that it
        # ACCEPTED this abort against the live run — not merely that an envelope was
        # once issued; ``signed_body_base64`` is the preimage the reason is derived
        # from. Neither is judged here: this module tests the shape contract, and the
        # receipt's signature is checked in tests/test_abort_authorization.py.
        "abort_receipt": ABORT_RECEIPT,
        "signed_body_base64": SIGNED_BODY,
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
        _write(sentinel_path, _document())

        sentinel = read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path)

        assert sentinel is not None
        assert sentinel["run_id"] == RUN_ID
        assert sentinel["generation"] == GENERATION
        assert sentinel["command_id"] == "cmd-1"
        # Verbatim: these bytes are the preimage of the envelope's signed
        # ``body_digest``, so re-encoding them anywhere between the writer and the
        # digest comparison would break the check they exist to pass.
        assert sentinel["signed_body_base64"] == SIGNED_BODY
        assert sentinel["abort_receipt"] == ABORT_RECEIPT
        # No ``reason`` key, by design. The reason is derived from the signed bytes
        # by ``_resolve_abort_outcome``; a plain field of this name is what let
        # fabricated text be attributed to a human, so its absence is the contract.
        assert "reason" not in sentinel

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

    @pytest.mark.parametrize(
        "overrides",
        [
            {"abort_receipt": _ABSENT},
            {"abort_receipt": ""},
            {"abort_receipt": {"token": "x"}},
            {"abort_receipt": True},
            {"abort_receipt": "z" * (MAX_SENTINEL_ENVELOPE_BYTES + 1)},
            {"signed_body_base64": _ABSENT},
            {"signed_body_base64": ""},
            {"signed_body_base64": 7},
            {"signed_body_base64": "A" * (MAX_SIGNED_BODY_BYTES + 1)},
        ],
        ids=[
            "receipt_missing",
            "receipt_blank",
            "receipt_wrong_type",
            "receipt_boolean",
            "receipt_oversized",
            "signed_body_missing",
            "signed_body_blank",
            "signed_body_wrong_type",
            "signed_body_oversized",
        ],
    )
    def test_a_document_that_cannot_prove_accepted_delivery_is_not_an_abort(
        self, sentinel_path, overrides
    ):
        # Two distinct facts, both required. A valid envelope proves the gateway
        # *issued* an authorization; ``abort_receipt`` is the gateway's own signed
        # statement that it accepted the abort against this live run after recording
        # durable intent. A command the gateway refused at the live recheck leaves its
        # envelope behind, so a reader honouring the envelope alone would finalize an
        # abort the platform had just explicitly denied. And without the signed body
        # the reason cannot be bound to the signature at all.
        #
        # The field this replaced was ``delivery: "accepted"`` — Issue #3963 review
        # finding 1. Requiring a literal the pod itself writes is not a check: the
        # agent has a ``Bash`` tool, so any code in the pod could put those eight
        # characters beside a genuine-but-merely-issued envelope, and the finalizer
        # would delete a live run's queue message on the run's own word. Hence the
        # shape rules pinned here refuse an ABSENT, blank, mistyped or oversized
        # receipt outright rather than normalizing it to ``None`` the way an unusable
        # ``envelope`` is normalized: the envelope is optional to the parse, and the
        # acceptance proof is the parse's whole purpose.
        _write(sentinel_path, _document(**overrides))

        assert read_abort_sentinel(RUN_ID, GENERATION, path=sentinel_path) is None


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
        padded["pad"] = "x" * (MAX_SENTINEL_BYTES + 100)
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

    def test_a_reason_field_on_the_document_is_ignored_entirely(self):
        # The regression that motivated removing the field. An unsigned ``reason``
        # sitting beside the envelope was read as the operator's words, so a valid
        # envelope minted for one reason could carry another to the closing comment.
        # It is now neither read nor surfaced: a caller that wants the reason has to
        # go through ``authorized_abort_reason``, which proves the digest first.
        validated = validate_abort_sentinel(
            _document(reason="fabricated by the worker"), RUN_ID, GENERATION
        )

        assert validated is not None
        assert "reason" not in validated

    def test_derives_an_over_long_signed_reason_back_down_to_the_cap(self):
        # The bound applies to signed text too. A signature proves the operator sent
        # the words; it says nothing about their length being sensible for a GitHub
        # comment, and the listener's own cap (1000) is looser than this one.
        body = base64.b64encode(
            json.dumps({"reason": "y" * (MAX_SENTINEL_REASON_LENGTH + 100)}).encode("utf-8")
        ).decode("ascii")

        derived = authorized_abort_reason({"signed_body_base64": body})

        assert len(derived) == MAX_SENTINEL_REASON_LENGTH

    @pytest.mark.parametrize(
        "recorded",
        [_ABSENT, None, "", 7, "not base64 at all!!", "eyJ1bnRlcm1pbmF0ZWQ"],
        ids=["absent", "none", "blank", "wrong_type", "not_base64", "not_json"],
    )
    def test_an_unusable_signed_body_derives_no_reason_rather_than_raising(self, recorded):
        # Runs on the teardown path, so it must never raise: an exception here could
        # cost the SQS acknowledgement and strand the message. ``None`` means "no
        # verified reason", and the caller must then say nothing about a reason
        # rather than fall back to any other field.
        sentinel = {} if recorded is _ABSENT else {"signed_body_base64": recorded}

        assert authorized_abort_reason(sentinel) is None


_VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "agent"
    / "src"
    / "__fixtures__"
    / "abort-sentinel-vectors.json"
)
_VECTORS = json.loads(_VECTORS_PATH.read_text(encoding="utf-8"))


def _vector_ids(vectors):
    return [vector["name"] for vector in vectors]


class TestSharedVectors:
    """Both runtimes evaluate these same documents with their real validators.

    The previous cross-language test compared *source text* for the shared
    constants. That could not have caught the divergence it existed to catch: the
    constants matched exactly while Python honoured ``version: true`` (because
    ``True == 1``) and TypeScript rejected it. Matching declarations do not prove
    matching behaviour, so the contract is pinned by running both validators over
    one fixture instead. ``control-abort-sentinel.test.ts`` reads the same file.
    """

    def test_the_fixture_matches_this_reader_s_constants(self):
        # The fixture carries the shared constants so drift in either direction
        # is a failing test rather than a silently different contract.
        assert _VECTORS["version"] == ABORT_SENTINEL_VERSION
        assert _VECTORS["path"] == ABORT_SENTINEL_PATH
        assert _VECTORS["max_reason_length"] == MAX_SENTINEL_REASON_LENGTH
        assert _VECTORS["max_envelope_length"] == MAX_SENTINEL_ENVELOPE_BYTES
        assert _VECTORS["max_signed_body_length"] == MAX_SIGNED_BODY_BYTES
        assert _VECTORS["max_sentinel_bytes"] == MAX_SENTINEL_BYTES
        assert _VECTORS["max_safe_generation"] == MAX_SAFE_GENERATION
        # Shape only, because the fixture's receipt is unsigned: what is pinned is
        # that the contract's required acceptance field is a non-empty string within
        # the same ceiling as the envelope. Its signature is covered by
        # tests/test_abort_authorization.py against per-run keys.
        assert isinstance(_VECTORS["abort_receipt"], str)
        assert 0 < len(_VECTORS["abort_receipt"].encode("utf-8")) <= MAX_SENTINEL_ENVELOPE_BYTES
        # The file ceiling pinned as a derivation, not a literal: it must exceed the
        # fields the document has to carry together. Restating the number would have
        # been satisfied by the broken value — a flat 8192, below the 21852-byte
        # signed-body bound — under which the TypeScript writer stored documents this
        # reader then refused, reporting an abort to the operator as recorded and then
        # finalizing the run as a crash.
        #
        # TWO envelope bounds, because a genuine document now carries both the
        # issuance envelope and the acceptance receipt (review finding 1). Budgeting
        # for one would reproduce exactly that defect with the receipt as the field
        # that overflows the ceiling.
        assert (
            _VECTORS["max_sentinel_bytes"]
            > _VECTORS["max_signed_body_length"] + 2 * _VECTORS["max_envelope_length"]
        )

    @pytest.mark.parametrize("vector", _VECTORS["vectors"], ids=_vector_ids(_VECTORS["vectors"]))
    def test_document_vector(self, vector):
        binding = _VECTORS["binding"]

        validated = validate_abort_sentinel(
            vector["document"], binding["run_id"], binding["generation"]
        )

        if vector["accept"]:
            assert validated is not None, vector["note"]
            assert validated["run_id"] == binding["run_id"]
            assert validated["generation"] == binding["generation"]
            # No ``reason`` on the validated payload, on either side: it is derived
            # from the signed bytes, and ``reason_vectors`` below pins that
            # derivation. A document field of this name is what allowed fabricated
            # text to be attributed to a human.
            assert "reason" not in validated
            # The normalization both readers have to agree on. Neither judges the
            # signature here, so what is pinned is which values survive as a
            # token and which collapse to exactly ``None`` — the value
            # ``verify_abort_authorization`` treats as "no proof, refuse the
            # abort". A shape surviving on one side only would mean one runtime
            # refusing an authorized abort, or handing a non-token to a verifier.
            assert validated["envelope"] == vector["expect_envelope"]
        else:
            assert validated is None, vector["note"]

    @pytest.mark.parametrize(
        "vector",
        _VECTORS["generation_binding_vectors"],
        ids=_vector_ids(_VECTORS["generation_binding_vectors"]),
    )
    def test_generation_binding_vector(self, vector):
        # The env-supplied generation is parsed on both sides: Python coerces the
        # decimal-string form here, TypeScript in `parseStrictGeneration`. Both
        # must accept and reject exactly the same strings, because the generation
        # is what stops a stale sentinel finalizing a live run.
        assert _coerce_generation(vector["raw"]) == vector["expect"], vector.get("note", "")

    @pytest.mark.parametrize(
        "vector",
        [v for v in _VECTORS["vectors"] if not v["accept"] and isinstance(v["document"], dict)],
        ids=_vector_ids(
            [v for v in _VECTORS["vectors"] if not v["accept"] and isinstance(v["document"], dict)]
        ),
    )
    def test_rejected_vectors_are_also_rejected_through_the_file_reader(
        self, sentinel_path, vector
    ):
        # The validator is the shared rule table, but the file reader is what the
        # finalizer actually calls. A rule enforced only in the validator would
        # not protect the real path.
        binding = _VECTORS["binding"]
        _write(sentinel_path, vector["document"])

        assert (
            read_abort_sentinel(binding["run_id"], binding["generation"], path=sentinel_path)
            is None
        ), vector["note"]

    @pytest.mark.parametrize(
        "vector",
        _VECTORS["reason_vectors"],
        ids=_vector_ids(_VECTORS["reason_vectors"]),
    )
    def test_reason_vector(self, vector):
        """The operator's words, derived from the bytes the gateway signed.

        This is the end-to-end derivation on the half that prints the reason, driven
        by the same table TypeScript evaluates against its bounding rule. The
        divergence that matters would be the two halves disagreeing about what an
        operator's words *are*.
        """
        recorded = base64.b64encode(json.dumps(vector["body"]).encode("utf-8")).decode("ascii")

        derived = authorized_abort_reason({"signed_body_base64": recorded})

        assert derived == vector["expect"], vector.get("note", "")

    @pytest.mark.parametrize(
        "vector",
        _VECTORS["byte_boundary_vectors"],
        ids=_vector_ids(_VECTORS["byte_boundary_vectors"]),
    )
    def test_byte_boundary_vector(self, sentinel_path, vector):
        """The file ceiling is a bound on UTF-8 bytes, not on characters.

        ``multibyte_one_past_ceiling`` is the defect these vectors exist for: this
        reader used a text-mode ``read(n)``, which bounds *characters*, so a document
        of ~16000 two-byte characters sat well under a 32092-character read and was
        parsed and honoured even though it exceeded the declared byte limit — up to
        4x over for 4-byte characters. The read is binary and bounded before decoding
        now, and TypeScript measures ``Buffer.byteLength``.
        """
        binding = _VECTORS["binding"]
        base = {
            "version": _VECTORS["version"],
            "run_id": binding["run_id"],
            "generation": binding["generation"],
            "command_id": "cmd-0001",
            "requested_at": "2026-09-23T00:00:00Z",
            "abort_receipt": _VECTORS["abort_receipt"],
            "signed_body_base64": _VECTORS["signed_body_base64"],
            "pad": "",
        }

        # Padding goes in an unknown extra field, which forward tolerance requires
        # both readers to ignore — so size is the only rule under test.
        #
        # ``ensure_ascii=False`` and the compact separators are both needed to build
        # the same bytes ``JSON.stringify`` produces, and the first one is the whole
        # point of the multibyte vectors: Python escapes a non-ASCII character to a
        # 6-byte ``\u00e9`` by default, where JavaScript emits the 2 raw UTF-8 bytes.
        # Left at the default, this test would have padded with ASCII escapes and
        # measured nothing about multibyte handling while appearing to.
        def serialize(document: dict) -> bytes:
            return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        pad_width = len(vector["pad_char"].encode("utf-8"))
        overhead = len(serialize(base))
        assert (vector["target_bytes"] - overhead) % pad_width == 0
        document = dict(
            base, pad=vector["pad_char"] * ((vector["target_bytes"] - overhead) // pad_width)
        )
        serialized = serialize(document)
        assert len(serialized) == vector["target_bytes"]

        with open(sentinel_path, "wb") as handle:
            handle.write(serialized)

        read = read_abort_sentinel(binding["run_id"], binding["generation"], path=sentinel_path)

        assert (read is not None) == vector["accept"], vector["note"]
