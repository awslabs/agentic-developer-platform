"""Guard tests for the live-control evaluation harness (Issue #3960).

The harness itself never runs in CI — it needs an operator-created isolated
fixture and a real credential (evaluation #3967). What CI can and must prove is
that its **guards** work, because those are precisely the properties that are
unverifiable at the moment they matter: by the time the isolation check is load
bearing, someone is already pointing the harness at a live AWS account.

So every test here answers one question: *does the harness refuse?*

  * a fixture that is not isolated                → nonzero (DP-INV-1)
  * a credential resolving to the wrong account   → nonzero
  * an invocation table with the wrong key schema → nonzero
  * a check missing from the W1 manifest          → nonzero
  * a cleanup failure                             → not reported as success

Plus the redaction contract, tested against credentials hidden in the awkward
places: nested dicts, bland key names, free text, and inside lists.

No AWS, no network: the STS and DynamoDB clients are injected, and the two that
``main`` constructs are patched at the boto3 session boundary.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Loaded by path because the script is a hyphenated executable, not an importable
# module — the same approach as test_assume_customer_creds.py in this directory.
#
# Registered in sys.modules BEFORE exec_module, which that sibling test does not
# need to do: `@dataclass` resolves its own module out of sys.modules to evaluate
# annotations, so a dataclass in a path-loaded module raises AttributeError on
# import if the module is not registered first.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "agent-control-eval.py"
_spec = importlib.util.spec_from_file_location("agent_control_eval", _SCRIPT_PATH)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["agent_control_eval"] = _mod
_spec.loader.exec_module(_mod)


REPO_ROOT = Path(__file__).resolve().parents[3]

ACCOUNT = "879318057152"


def valid_config() -> dict:
    """A fixture description that passes every config guard.

    Each test mutates exactly one field, so a failure names the guard that fired
    rather than leaving it ambiguous which of several problems was caught.
    """
    return {
        "account_id": ACCOUNT,
        "environment": "dev-control-fixture",
        "fixture_isolated": True,
        "gateway_url": "https://gateway.example.internal",
        "invocation_table": "adp-dev-webhook-events",
        "live_run_id": "msg-live-001",
        "terminal_run_id": "msg-terminal-001",
        "tenant_id": "org-fixture-001",
    }


def write_config(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def sts_for(account: str) -> MagicMock:
    client = MagicMock()
    client.get_caller_identity.return_value = {
        "Account": account,
        "Arn": f"arn:aws:iam::{account}:role/eval",
    }
    return client


def dynamodb_with_schema(schema: list[dict]) -> MagicMock:
    client = MagicMock()
    client.describe_table.return_value = {"Table": {"KeySchema": schema}}
    return client


CORRECT_SCHEMA = [
    {"AttributeName": "event_id", "KeyType": "HASH"},
    {"AttributeName": "arrived_at", "KeyType": "RANGE"},
]


class TestFixtureIsolationIsMandatory:
    """DP-INV-1: the flag may be enabled ONLY in an isolated fixture."""

    def test_missing_isolation_field_is_refused(self, tmp_path: Path):
        """Absent means no, not "assume the operator knows what they're doing"."""
        config = valid_config()
        del config["fixture_isolated"]

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(write_config(tmp_path, config))

        assert "fixture_isolated" in str(exc.value)

    def test_isolation_false_is_refused(self, tmp_path: Path):
        config = valid_config()
        config["fixture_isolated"] = False

        with pytest.raises(_mod.EvalConfigError):
            _mod.load_config(write_config(tmp_path, config))

    @pytest.mark.parametrize(
        "value", ["true", "True", "yes", 1, "1", [True], {"isolated": True}]
    )
    def test_only_the_json_boolean_true_counts(self, tmp_path: Path, value):
        """The string "false" is truthy — this is the bug this test exists for.

        A ``bool(value)`` check would accept ``"false"`` and enable a control
        listener on whatever environment the operator was actually pointing at.
        The identity check against ``True`` is what makes that impossible, and
        ``1`` is rejected for the same reason even though it is harmless in
        isolation: accepting near-misses is how the check erodes.
        """
        config = valid_config()
        config["fixture_isolated"] = value

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(write_config(tmp_path, config))

        assert "exactly true" in str(exc.value)

    def test_an_isolated_fixture_is_accepted(self, tmp_path: Path):
        """The guards must not be so strict that a valid fixture cannot run."""
        loaded = _mod.load_config(write_config(tmp_path, valid_config()))

        assert loaded["fixture_isolated"] is True
        assert loaded["account_id"] == ACCOUNT

    def test_the_refusal_explains_the_invariant(self, tmp_path: Path):
        """The operator hitting this is mid-evaluation with a broken fixture.

        That is exactly the moment someone reaches for "just enable the flag on
        dev", so the message has to say why that is not the workaround.
        """
        config = valid_config()
        config["fixture_isolated"] = False

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(write_config(tmp_path, config))

        message = str(exc.value)
        assert "isolated" in message
        assert "shared" in message


class TestConfigValidation:
    """Nothing is contacted until the fixture description is complete."""

    @pytest.mark.parametrize("field", sorted(_mod.REQUIRED_CONFIG_FIELDS))
    def test_every_required_field_is_enforced(self, tmp_path: Path, field: str):
        """Parametrized over the constant so a new required field is covered free."""
        config = valid_config()
        del config[field]

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(write_config(tmp_path, config))

        assert field in str(exc.value)

    @pytest.mark.parametrize("field", sorted(_mod.REQUIRED_CONFIG_FIELDS))
    def test_an_empty_value_is_as_bad_as_a_missing_key(
        self, tmp_path: Path, field: str
    ):
        """An empty string is the shape a half-filled template file has."""
        config = valid_config()
        config[field] = ""

        with pytest.raises(_mod.EvalConfigError):
            _mod.load_config(write_config(tmp_path, config))

    @pytest.mark.parametrize(
        "account", ["12345", "not-an-account", "8793180571520", "879318057 52", ""]
    )
    def test_a_malformed_account_id_is_refused(self, tmp_path: Path, account: str):
        config = valid_config()
        config["account_id"] = account

        with pytest.raises(_mod.EvalConfigError):
            _mod.load_config(write_config(tmp_path, config))

    def test_a_missing_config_file_is_refused(self, tmp_path: Path):
        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(tmp_path / "nope.json")

        assert "not found" in str(exc.value)

    def test_malformed_json_is_refused_as_config_not_a_crash(self, tmp_path: Path):
        path = tmp_path / "fixture.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(path)

        assert "valid JSON" in str(exc.value)

    def test_a_json_array_is_refused(self, tmp_path: Path):
        """Valid JSON, wrong shape — would otherwise fail later as a TypeError."""
        path = tmp_path / "fixture.json"
        path.write_text("[]", encoding="utf-8")

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(path)

        assert "JSON object" in str(exc.value)


class TestAccountVerification:
    """No ambient account: the credential must match what the fixture names."""

    def test_a_matching_account_passes(self):
        assert _mod.verify_account(valid_config(), sts_for(ACCOUNT)) == ACCOUNT

    def test_a_mismatched_account_is_refused(self):
        """The dangerous direction: fixture says test, credential says production."""
        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.verify_account(valid_config(), sts_for("999988887777"))

        message = str(exc.value)
        assert "mismatch" in message
        assert ACCOUNT in message and "999988887777" in message

    def test_the_refusal_names_both_accounts(self):
        """An operator with several profiles cannot act on "wrong account"."""
        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.verify_account(valid_config(), sts_for("111122223333"))

        assert "AWS_PROFILE" in str(exc.value)

    def test_an_absent_account_in_the_sts_response_is_refused(self):
        """Fails closed on a malformed identity rather than comparing None loosely."""
        client = MagicMock()
        client.get_caller_identity.return_value = {}

        with pytest.raises(_mod.EvalPreconditionError):
            _mod.verify_account(valid_config(), client)


class TestTableKeySchemaVerification:
    """Verified before any write, not after a confusing failure."""

    def test_the_expected_schema_passes(self):
        _mod.verify_table_key_schema(
            valid_config(), dynamodb_with_schema(CORRECT_SCHEMA)
        )

    def test_a_missing_range_key_is_refused(self):
        """`event_id` alone: the update would target the wrong item or no item."""
        schema = [{"AttributeName": "event_id", "KeyType": "HASH"}]

        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.verify_table_key_schema(valid_config(), dynamodb_with_schema(schema))

        assert "key schema" in str(exc.value)

    def test_a_reordered_schema_is_refused(self):
        """Hash and range swapped is a different table, not a formatting variant."""
        schema = [
            {"AttributeName": "arrived_at", "KeyType": "HASH"},
            {"AttributeName": "event_id", "KeyType": "RANGE"},
        ]

        with pytest.raises(_mod.EvalPreconditionError):
            _mod.verify_table_key_schema(valid_config(), dynamodb_with_schema(schema))

    def test_a_renamed_key_is_refused(self):
        schema = [
            {"AttributeName": "invocation_id", "KeyType": "HASH"},
            {"AttributeName": "arrived_at", "KeyType": "RANGE"},
        ]

        with pytest.raises(_mod.EvalPreconditionError):
            _mod.verify_table_key_schema(valid_config(), dynamodb_with_schema(schema))

    def test_an_undescribable_table_is_refused(self):
        """Cannot prove the schema ⇒ refuse. Never "proceed and hope"."""
        client = MagicMock()
        client.describe_table.side_effect = RuntimeError("ResourceNotFoundException")

        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.verify_table_key_schema(valid_config(), client)

        assert "cannot describe" in str(exc.value)

    def test_the_refusal_names_the_table(self):
        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.verify_table_key_schema(valid_config(), dynamodb_with_schema([]))

        assert "adp-dev-webhook-events" in str(exc.value)


class TestCheckManifest:
    """Exactly W1-01..W1-10 — asserted for equality, not membership."""

    @staticmethod
    def _results(check_ids) -> list:
        return [
            _mod.CheckResult(check_id=cid, status=_mod.STATUS_PASSED)
            for cid in check_ids
        ]

    def test_the_full_manifest_passes(self):
        _mod.assert_check_manifest(self._results(_mod.EXPECTED_CHECK_IDS))

    def test_the_manifest_is_exactly_ten_ids(self):
        """Pinned so the set cannot quietly shrink to match a shorter harness."""
        assert _mod.EXPECTED_CHECK_IDS == (
            "W1-01",
            "W1-02",
            "W1-03",
            "W1-04",
            "W1-05",
            "W1-06",
            "W1-07",
            "W1-08",
            "W1-09",
            "W1-10",
        )

    @pytest.mark.parametrize("dropped", _mod.EXPECTED_CHECK_IDS)
    def test_dropping_any_single_check_fails_the_run(self, dropped: str):
        """The core guard, parametrized over all ten.

        Nine passing checks read exactly like ten in a report unless something
        compares against the manifest — and the check most likely to be dropped is
        the awkward one (W1-09 needs a second in-cluster pod).
        """
        remaining = [cid for cid in _mod.EXPECTED_CHECK_IDS if cid != dropped]

        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.assert_check_manifest(self._results(remaining))

        assert dropped in str(exc.value)
        assert "missing" in str(exc.value)

    def test_an_unexpected_check_id_fails_the_run(self):
        """Divergence in either direction makes the evidence unreadable."""
        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.assert_check_manifest(
                self._results((*_mod.EXPECTED_CHECK_IDS, "W1-99"))
            )

        assert "W1-99" in str(exc.value)
        assert "unexpected" in str(exc.value)

    def test_a_duplicated_check_id_fails_the_run(self):
        """Two rows for one ID lets a pass and a fail coexist in one report."""
        results = self._results(_mod.EXPECTED_CHECK_IDS)
        results.append(
            _mod.CheckResult(check_id="W1-06", status=_mod.STATUS_FAILED)
        )

        with pytest.raises(_mod.EvalPreconditionError) as exc:
            _mod.assert_check_manifest(results)

        assert "duplicate" in str(exc.value)

    def test_an_empty_result_set_fails(self):
        """A harness that ran nothing must not produce a passing report."""
        with pytest.raises(_mod.EvalPreconditionError):
            _mod.assert_check_manifest([])

    def test_every_check_id_has_a_description(self):
        """An evidence row without a description is not evidence to a reviewer.

        Compared against every registered spec rather than against wave 1 alone:
        the description map is the per-ID fallback used when a report is built, so
        a wave-2 ID missing from it produces an evidence row with an empty
        subject.
        """
        registered = {spec.check_id for spec in _mod.ALL_CHECK_SPECS}

        assert set(_mod.CHECK_DESCRIPTIONS) == registered
        assert all(_mod.CHECK_DESCRIPTIONS[cid].strip() for cid in registered)
        assert set(_mod.EXPECTED_CHECK_IDS) <= registered


class TestEvidenceRedaction:
    """Evidence lands on disk and gets pasted into issues."""

    def test_token_keys_are_redacted(self):
        result = _mod.redact({"control_token": "pod-minted-secret-value"})

        assert result["control_token"] == _mod.REDACTED
        assert "pod-minted-secret-value" not in json.dumps(result)

    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "control_token",
            "Authorization",
            "aws_secret_access_key",
            "password",
            "session_token",
            "github_credential",
            "api_key",
            "private_key",
            "X-Amz-Signature",
            "cookie",
        ],
    )
    def test_all_secret_key_shapes_are_redacted(self, key: str):
        assert _mod.redact({key: "sensitive"})[key] == _mod.REDACTED

    def test_redaction_is_case_insensitive_on_keys(self):
        result = _mod.redact({"CONTROL_TOKEN": "x", "Session_Token": "y"})

        assert result["CONTROL_TOKEN"] == _mod.REDACTED
        assert result["Session_Token"] == _mod.REDACTED

    def test_nested_structures_are_redacted(self):
        """Depth is where redaction usually fails — a single-level scrub misses this."""
        payload = {
            "observed": {
                "headers": {"authorization": "Bearer abc123"},
                "runs": [{"token": "t1"}],
            }
        }

        result = _mod.redact(payload)

        assert result["observed"]["headers"]["authorization"] == _mod.REDACTED
        assert result["observed"]["runs"][0]["token"] == _mod.REDACTED

    def test_a_bearer_header_in_free_text_is_redacted(self):
        """The key is bland (`detail`); only value-shape matching catches this."""
        result = _mod.redact(
            {"detail": "upstream said: Authorization: Bearer sk-live-abc.def-123"}
        )

        assert "sk-live-abc.def-123" not in result["detail"]
        assert _mod.REDACTED in result["detail"]

    @pytest.mark.parametrize(
        "secret",
        [
            "ASIAIOSFODNN7EXAMPLE",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_16CharactersMinimumHere00",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ],
    )
    def test_credential_shaped_values_are_redacted_under_bland_keys(self, secret: str):
        """Key-name matching alone would let every one of these through."""
        result = _mod.redact({"message": f"failed for {secret} at 12:00"})

        assert secret not in result["message"]

    def test_redaction_replaces_rather_than_truncates(self):
        """A prefix of a secret is still a secret; a stable hash still identifies it."""
        result = _mod.redact({"token": "abcdefghijklmnop"})

        assert result["token"] == _mod.REDACTED
        assert "abcd" not in result["token"]

    def test_non_secret_values_survive(self):
        """Redaction that eats the evidence is useless — status codes must remain."""
        payload = {
            "status_code": 404,
            "run_id": "msg-run-001",
            "passed": True,
            "phase": "executing",
        }

        assert _mod.redact(payload) == payload

    def test_lists_of_scalars_are_walked(self):
        result = _mod.redact({"messages": ["Bearer topsecret1", "fine"]})

        assert "topsecret1" not in json.dumps(result)
        assert result["messages"][1] == "fine"

    def test_check_result_evidence_is_redacted(self):
        """The per-check path, not just the top-level report."""
        result = _mod.CheckResult(
            check_id="W1-10",
            status=_mod.STATUS_PASSED,
            observations=[
                _mod.Observation(
                    command="curl -sS 'https://gw/x'",
                    status=200,
                    body={"control_token": "leaked"},
                )
            ],
            message="Bearer leaked-too",
        )

        evidence = result.to_evidence()

        assert evidence["evidence"][0]["body"]["control_token"] == _mod.REDACTED
        assert evidence["evidence"][0]["status"] == 200
        assert "leaked-too" not in evidence["message"]

    def test_the_whole_report_is_redacted_defensively(self):
        """A field added later must not leak by forgetting to redact at its site."""
        config = valid_config()
        config["gateway_url"] = "https://gw.internal"
        results = [
            _mod.CheckResult(
                check_id=cid,
                status=_mod.STATUS_PASSED,
                observations=[
                    _mod.Observation(command="curl", body={"token": f"secret-{cid}"})
                ],
            )
            for cid in _mod.EXPECTED_CHECK_IDS
        ]

        report = _mod.build_report(config, results, cleanup_ok=True)

        serialised = json.dumps(report)
        assert "secret-W1-01" not in serialised
        assert _mod.REDACTED in serialised


class TestReportOutcome:
    """What "passed" means, and what it must never mean."""

    @staticmethod
    def _all_passing() -> list:
        return [
            _mod.CheckResult(check_id=cid, status=_mod.STATUS_PASSED)
            for cid in _mod.EXPECTED_CHECK_IDS
        ]

    def test_all_checks_and_clean_cleanup_is_a_pass(self):
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        # §7's aggregates: `passed` is a COUNT compared against `required`, not a
        # boolean. The operator's jq does `.passed == .required`, which a boolean
        # true would silently fail against an integer 10.
        assert report["passed"] == report["required"] == 10
        assert report["failed"] == 0
        assert report["skipped"] == 0
        assert report["not_run"] == 0
        assert report["cleanup_ok"] is True
        assert _mod.report_is_passing(report) is True

    def test_cleanup_failure_is_never_reported_as_success(self):
        """A fixture left with a live control listener is the DP-INV-1 violation.

        The tempting reading — "all ten checks passed, so the evaluation passed" —
        is what leaves an enabled control channel behind on an environment nobody
        is watching any more.
        """
        report = _mod.build_report(
            valid_config(), self._all_passing(), cleanup_ok=False
        )

        assert report["passed"] == report["required"]
        assert report["cleanup_ok"] is False
        assert _mod.report_is_passing(report) is False

    def test_one_failing_check_fails_the_report(self):
        results = self._all_passing()
        results[3] = _mod.CheckResult(
            check_id="W1-04",
            status=_mod.STATUS_FAILED,
            message="got 501 before authorization",
        )

        report = _mod.build_report(valid_config(), results, cleanup_ok=True)

        assert report["failed"] == 1
        assert report["passed"] == 9
        assert _mod.report_is_passing(report) is False

    def test_a_not_run_check_cannot_satisfy_the_gate(self):
        """The design decision this harness turns on.

        A prerequisite the harness could not reach is `not_run`, individually, and
        must not be readable as a pass. §7's gate reads `.not_run == 0`, so the
        count has to be visible in the aggregates rather than folded into a
        boolean — nine passes and one unreachable check is an incomplete
        evaluation, not a successful one.
        """
        results = self._all_passing()
        results[8] = _mod.CheckResult(
            check_id="W1-09",
            status=_mod.STATUS_NOT_RUN,
            message="prerequisite missing: journal_tests artifact absent",
        )

        report = _mod.build_report(valid_config(), results, cleanup_ok=True)

        assert report["not_run"] == 1
        assert report["failed"] == 0
        assert report["passed"] == 9
        assert _mod.report_is_passing(report) is False

    def test_the_report_records_that_no_verb_is_supported(self):
        """The story's claim, stated in the evidence rather than inferred from it."""
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        assert report["supported_verbs"] == []

    def test_the_report_carries_the_manifest_and_provenance(self):
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        assert report["expected_check_ids"] == list(_mod.EXPECTED_CHECK_IDS)
        assert report["revision"] == "revival-2026-09-12"
        assert report["issue"] == "3960"
        assert report["fixture_isolated"] is True

    def test_the_report_is_written_and_reloadable(self, tmp_path: Path):
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        path = _mod.write_report(report, tmp_path / "out")

        assert json.loads(path.read_text(encoding="utf-8")) == report

    def test_every_check_appears_in_the_written_evidence(self, tmp_path: Path):
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)
        path = _mod.write_report(report, tmp_path / "out")

        written = json.loads(path.read_text(encoding="utf-8"))

        # `checks` is an OBJECT keyed by ID: the operator's check() does
        # `.checks[$id]`, which cannot index a list.
        assert isinstance(written["checks"], dict)
        assert set(written["checks"]) == set(_mod.EXPECTED_CHECK_IDS)

    def test_the_report_is_written_as_result_json(self, tmp_path: Path):
        """The exact filename §7's gate reads.

        The gate is a copy-pasted `jq ... "$CONTROL_EVIDENCE_DIR/result.json"`, so
        any other name makes the published command fail on a file-not-found no
        matter how good the report inside is.
        """
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        path = _mod.write_report(report, tmp_path / "out")

        assert path.name == "result.json"

    def test_each_check_entry_has_the_three_fields_the_gate_reads(self):
        """§7: each entry has `status`, `acceptance_ids` and a NONEMPTY evidence list."""
        report = _mod.build_report(valid_config(), self._all_passing(), cleanup_ok=True)

        for check_id, entry in report["checks"].items():
            assert entry["status"] == _mod.STATUS_PASSED, check_id
            assert entry["acceptance_ids"], check_id
            assert entry["evidence"], f"{check_id} has an empty evidence list"


class TestEntryPointFailsClosed:
    """Invoked bare or misconfigured, it must exit nonzero."""

    def test_no_config_argument_exits_nonzero(self):
        """The property the CI smoke step asserts: no inferred target."""
        assert _mod.main([]) == _mod.EXIT_CONFIG

    def test_a_missing_config_file_exits_nonzero(self, tmp_path: Path):
        assert (
            _mod.main(["--config", str(tmp_path / "absent.json")]) == _mod.EXIT_CONFIG
        )

    def test_an_unisolated_fixture_exits_nonzero(self, tmp_path: Path):
        config = valid_config()
        config["fixture_isolated"] = False

        assert (
            _mod.main(["--config", str(write_config(tmp_path, config))])
            == _mod.EXIT_CONFIG
        )

    def test_a_wrong_account_exits_nonzero_before_any_check(self, tmp_path: Path):
        """And the DynamoDB client is never even reached."""
        path = write_config(tmp_path, valid_config())
        dynamodb = dynamodb_with_schema(CORRECT_SCHEMA)
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for("999988887777"),
            "dynamodb": dynamodb,
        }[name]

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(["--config", str(path), "--dry-run"])

        assert code == _mod.EXIT_PRECONDITION
        dynamodb.describe_table.assert_not_called()

    def test_a_wrong_key_schema_exits_nonzero(self, tmp_path: Path):
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb_with_schema(
                [{"AttributeName": "wrong", "KeyType": "HASH"}]
            ),
        }[name]

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(["--config", str(path), "--dry-run"])

        assert code == _mod.EXIT_PRECONDITION

    def test_dry_run_succeeds_on_a_valid_fixture(self, tmp_path: Path):
        """The guards must leave a legitimate fixture usable."""
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb_with_schema(CORRECT_SCHEMA),
        }[name]

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(["--config", str(path), "--dry-run"])

        assert code == _mod.EXIT_OK

    def test_a_full_run_without_the_live_fixture_exits_nonzero(self, tmp_path: Path):
        """Refuse, never report a vacuous pass.

        This is the difference between "ten checks passed" and "zero checks ran and
        nothing objected". The driver now exists, so the checks genuinely execute
        and each records `not_run` naming the prerequisite it wanted — which is a
        nonzero exit, and is distinguishable from a broken fixture.
        """
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb_with_schema(CORRECT_SCHEMA),
        }[name]

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(
                ["--config", str(path), "--evidence-dir", str(tmp_path / "ev")]
            )

        assert code == _mod.EXIT_CHECKS_FAILED
        report = json.loads((tmp_path / "ev" / "result.json").read_text(encoding="utf-8"))
        # Every check names its own missing prerequisite rather than the whole run
        # collapsing into one blanket "precondition failed".
        assert report["not_run"] == report["required"] == 10
        assert report["passed"] == 0
        assert _mod.report_is_passing(report) is False
        for check_id, entry in report["checks"].items():
            assert entry["status"] == _mod.STATUS_NOT_RUN, check_id
            assert "prerequisite missing" in entry["message"], check_id

    def test_an_unsupported_wave_is_refused_rather_than_passing_empty(
        self, tmp_path: Path
    ):
        """Wave 4 belongs to S7 (§7).

        Asking for it must not emit a report with zero required checks, which
        would satisfy `.passed == .required` at 0 == 0 and read as a clean pass.

        The subject moved from wave 3 to wave 4 when S6 #3965 transcribed #3969's
        table. That is the second time this test's wave has been consumed by the
        story that implemented it, so the number is read from the module rather
        than typed: whatever the first unregistered wave is, that is the one whose
        refusal this test is about, and it cannot be invalidated again by the next
        wave landing.
        """
        path = write_config(tmp_path, valid_config())
        unregistered = max(_mod.SUPPORTED_WAVES) + 1

        code = _mod.main(["--wave", str(unregistered), "--config", str(path)])

        assert code == _mod.EXIT_CONFIG

    def test_a_credential_failure_is_a_precondition_error_not_a_traceback(
        self, tmp_path: Path
    ):
        """An operator with an expired credential should get a diagnosis."""
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = RuntimeError("ExpiredToken")

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(["--config", str(path), "--dry-run"])

        assert code == _mod.EXIT_PRECONDITION

    def test_exit_codes_are_distinct(self):
        """Distinguishable so a wrapper can tell config error from check failure."""
        codes = [
            _mod.EXIT_OK,
            _mod.EXIT_CONFIG,
            _mod.EXIT_PRECONDITION,
            _mod.EXIT_CHECKS_FAILED,
            _mod.EXIT_CLEANUP,
        ]

        assert len(set(codes)) == len(codes)
        assert _mod.EXIT_OK == 0
        assert all(code != 0 for code in codes[1:])


    def test_a_wave_two_run_without_its_evidence_cannot_exit_zero(self, tmp_path: Path):
        """The honesty guarantee that replaces wave 2's blanket refusal (#3964).

        S5 registers wave 2 so its four checks can actually run, which removes the
        `EXIT_CONFIG` that previously made `--wave 2` safe by making it impossible.
        What must survive that change is the property the refusal was protecting:
        wave 2 cannot report success unless all ten of its checks were answered.

        #5825 completed the predicate table, so the thing that keeps this run
        nonzero is no longer a missing implementation but missing *evidence* — a
        wave-1 config carries none of wave 2's artifacts, and a run that could not
        look is NOT RUN rather than a pass. That is the substitution this test
        guards: the gate must hold on evidence, not on the absence of code.

        Asserted through `main`, not through `report_is_passing` alone, so it covers
        the exit code an operator's shell actually branches on.
        """
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb_with_schema(CORRECT_SCHEMA),
        }[name]

        with patch("boto3.session.Session", return_value=session):
            code = _mod.main(
                ["--wave", "2", "--config", str(path), "--evidence-dir", str(tmp_path / "ev")]
            )

        assert code != _mod.EXIT_OK
        report = json.loads((tmp_path / "ev" / "result.json").read_text(encoding="utf-8"))
        # Ten, not four: the required bar is the evaluation file's whole table.
        assert report["required"] == 10
        assert report["wave"] == 2
        assert _mod.report_is_passing(report) is False
        # Neither check is passed and neither is skipped, which some gates tolerate.
        # The two statuses are different on purpose, and the difference is the
        # four-status model doing its job: W2-01 could not look, because this wave-1
        # config declares no `wave2_preflight` artifact, so it is NOT RUN. W2-10 did
        # look — cleanup ran and recorded that it had no rows to remove — and a
        # wave-2 run whose fixture seeded nothing contradicts the wave, so it is
        # FAILED. Both are nonzero; neither is a pass.
        assert report["checks"]["W2-01"]["status"] == _mod.STATUS_NOT_RUN
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        for check_id in ("W2-01", "W2-10"):
            entry = report["checks"][check_id]
            # §7: every evidence list is nonempty, including for a check that
            # could not run — its evidence is the reason.
            assert entry["evidence"], check_id
        # And the reason is the missing observation, not a missing predicate: every
        # wave-2 ID now resolves to a real method.
        assert set(_mod.WAVE2_PREDICATES) == {
            spec.check_id for spec in _mod.WAVE2_CHECKS
        }


    def test_wave_two_reports_carry_every_wave_two_id_and_no_wave_one_id(
        self, tmp_path: Path
    ):
        """A wave-2 run must not inherit wave 1's manifest.

        `EXPECTED_CHECK_IDS` is still the wave-1 tuple and is the default for both
        `build_report` and `assert_check_manifest`, so a wave-2 run that failed to
        thread its own IDs through would emit a report labelled wave 2 while
        claiming wave 1's requirements.
        """
        path = write_config(tmp_path, valid_config())
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb_with_schema(CORRECT_SCHEMA),
        }[name]

        with patch("boto3.session.Session", return_value=session):
            _mod.main(
                ["--wave", "2", "--config", str(path), "--evidence-dir", str(tmp_path / "ev")]
            )

        report = json.loads((tmp_path / "ev" / "result.json").read_text(encoding="utf-8"))
        assert set(report["checks"]) == {spec.check_id for spec in _mod.WAVE2_CHECKS}
        assert not any(cid.startswith("W1-") for cid in report["checks"])
        assert report["expected_check_ids"] == [spec.check_id for spec in _mod.WAVE2_CHECKS]


# ---------------------------------------------------------------------------
# Below: the checks that the harness's *contract* matches the authoritative
# sources — the evaluation file's ID table and the operator's published gate.
# These are the tests that would have caught the ID-meaning drift.
# ---------------------------------------------------------------------------


def artifact_payloads() -> dict:
    """A complete, passing set of operator-recorded artifacts.

    Every required key present and every value the passing one, so a test that
    wants to exercise a single failure mutates exactly one field and the failure
    names that field rather than being ambiguous between several.
    """
    return {
        "provenance": {
            "source_digest": "sha256:abc",
            "deployed_digest": "sha256:abc",
            "ci_jobs": {"Agent control tests": "passed"},
            "isolation_before_listener": True,
            "ordinary_flags_off": True,
        },
        "listener_auth": {
            "missing_token_status": 401,
            "wrong_token_status": 401,
            "rejected_before_verb_parse": True,
        },
        "token_lifecycle": {
            "before_expiry_status": 200,
            "after_expiry_status": 401,
            "stale_generation_status": 401,
            "ordinary_clock_unchanged": True,
        },
        "peer_probe": {
            "probe_pod": "control-probe-1",
            "gateway_ping_status": 200,
            "probe_connect_result": "connection refused",
            "policy_selectors": {"app": "bedrockgateway"},
            "timeout_seconds": 5,
        },
        "fixture_task": {"completed": True, "normalized_output_digest": "sha256:out"},
        "worker_unavailable": {"state": "unavailable", "command_acknowledged": False},
        "transport_guard": {
            "blocked_targets": {
                family: True
                for family in (
                    "unregistered_ip",
                    "wrong_port",
                    "metadata",
                    "link_local",
                    "loopback",
                    "public",
                )
            },
            "redirect_blocked": True,
            "blocked_before_transport": True,
        },
        "flag_parity": {
            "flag_off_events_digest": "sha256:e",
            "flag_on_events_digest": "sha256:e",
            "differing_fields": [],
            "ordinary_flags_off": True,
        },
        "journal_tests": {
            "replay_same_id": True,
            "content_conflict": True,
            "bounds_enforced": True,
            "expiry_is_unknown": True,
            "assistant_turns": 0,
        },
        "negative_tests": {
            key: True
            for key in (
                "wrong_account",
                "missing_isolation",
                "wrong_key",
                "absent_required_check",
                "unknown_check_id",
                "failed_cleanup",
            )
        },
        "neutral_contract": neutral_contract_payload(),
        "browser_control_run": browser_control_run_payload(),
    }


def browser_control_run_payload(**overrides) -> dict:
    """A complete, passing wave-4 browser capture.

    Every value is the one a correct deployed dashboard would produce, so a test
    that wants one failure mutates exactly one key and the resulting message names
    that key rather than being ambiguous between several.
    """
    payload = {
        "bundle_revision": "a" * 40,
        "gateway_url": "https://gw.internal",
        "captured_at": "2026-09-24T10:00:00Z",
        "spec_digest": "sha256:spec",
        "flag_off": {"control_nodes": 0, "command_requests": 0},
        "flag_loading": {"control_nodes": 0, "command_requests": 0},
        "flag_error": {"control_nodes": 0, "command_requests": 0},
        "advertised_capabilities": {
            "pause": True,
            "resume": True,
            "steer": False,
            "abort": True,
        },
        "rendered_controls": ["pause", "resume", "abort"],
        "nonowner_submit_blocked": True,
        "terminal_submit_blocked": True,
        "phase_sequence": ["running", "pause_requested", "paused", "running"],
        "pause_copy_mentions_spend": True,
        "active_tool_reason": "Tools in progress: unknown",
        "steer_request": {"path": "/activity/invocations/msg-live/agent/steer", "status": 202},
        "steer_status_sequence": ["pending", "delivered"],
        "poll_intervals_ms": [2010, 1990, 2005],
        "polled_while_hidden": False,
        "polled_after_close": False,
        "polled_after_terminal": False,
        "backoff_intervals_ms": [2000, 4000, 8000],
        "detail_refreshed_after_command": True,
        "request_destinations": ["https://gw.internal/activity/invocations/msg-live/agent/state"],
        "request_bodies_contain_pod_address": False,
        "request_bodies_contain_token": False,
        "spoofed_identity_rejected": True,
    }
    payload.update(overrides)
    return payload


def neutral_contract_payload(**overrides) -> dict:
    """A complete, passing W2-02 artifact.

    Built from the harness's own key list rather than a literal, so a property
    added to `REQUIRED_ARTIFACT_KEYS["neutral_contract"]` without a passing value
    here fails loudly instead of this fixture quietly going out of date. The
    non-boolean keys are supplied explicitly; everything else defaults to True.
    """
    payload: dict = {
        "protocol_version": _mod.CONTROL_PROTOCOL_VERSION,
        "adapter_id": _mod.CLAUDE_ADAPTER_ID,
        "sdk_version": _mod.EXPECTED_CLAUDE_SDK_VERSION,
        "adapters": {
            "claude": {"passed": True, "test_count": 61},
            "echo": {"passed": True, "test_count": 61},
        },
        "second_adapter": {
            "name": "echo",
            "declares_missing_capability": True,
            "imports_provider_sdk": False,
        },
    }
    for key in _mod.REQUIRED_ARTIFACT_KEYS["neutral_contract"]:
        payload.setdefault(key, True)
    payload.update(overrides)
    return payload


def live_config(tmp_path: Path, **overrides) -> dict:
    """A fixture config wired to on-disk artifacts, ready for the driver."""
    payloads = overrides.pop("artifact_payloads", None) or artifact_payloads()
    for name, payload in payloads.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    config = valid_config()
    config.update(
        {
            "gateway_url": "https://gw.internal",
            "flag_off_gateway_url": "https://gw-off.internal",
            "unknown_run_id": "msg-unknown",
            "arrived_at": "2026-09-12T00:00:00Z",
            "terminal_arrived_at": "2026-09-12T00:00:00Z",
            "generation": 2,
            "command_id": "11111111-2222-3333-4444-555555555555",
            "expected_output_digest": "sha256:out",
            "identity_env": {
                "owner": "EVAL_OWNER_TOKEN_VAR",
                "nonowner": "EVAL_NONOWNER_TOKEN_VAR",
                "other_tenant": "EVAL_OTHER_TOKEN_VAR",
            },
            "artifacts": {name: f"{name}.json" for name in payloads},
            "cleanup_items": [
                {"event_id": "msg-live", "arrived_at": "2026-09-12T00:00:00Z"}
            ],
        }
    )
    config.update(overrides)
    return config


OWNER_TOKEN = "tok-owner-secret"
IDENTITY_ENV = {
    "EVAL_OWNER_TOKEN_VAR": OWNER_TOKEN,
    "EVAL_NONOWNER_TOKEN_VAR": "tok-nonowner-secret",
    "EVAL_OTHER_TOKEN_VAR": "tok-other-secret",
}


def body_schema_error(url: str, body: object) -> str | None:
    """Mirror of the gateway's per-verb request models, or None if the body is valid.

    One predicate, used by both the stub gateway below and the assertions that the
    harness sends a schema-valid body. Two copies would be free to drift, which is
    the shape of the defect this exists to pin: the stub's idea of a valid body was
    looser than the product's, so nothing could see that the harness was sending
    `steer` something the real gateway rejects.

    Mirrors `control_schemas.py`: both models are `extra="forbid"`, so `reason` on
    a steer and `instruction` on a pause are each an unknown field, and steer's
    `instruction` has `min_length=1`.
    """
    is_steer = url.endswith("/steer")
    allowed = {"command_id", "instruction"} if is_steer else {"command_id", "reason"}
    fields = body if isinstance(body, dict) else {}
    if not fields.get("command_id"):
        return "command_id is required"
    unknown = sorted(set(fields) - allowed)
    if unknown:
        return f"fields this verb forbids: {unknown}"
    if is_steer and not fields.get("instruction"):
        return "instruction is required"
    return None


def gateway_stub(**overrides):
    """A fake gateway answering the way a correct S1 deployment does.

    Overrides let one test bend a single response — which is how a "the harness
    would notice" assertion is written without hand-building a whole client.

    The stub answers on request *shape*, so it must hold the product's per-verb
    body schema (ControlCommandRequest vs ControlSteerRequest) and the product's
    ordering (validate the body, then authorize). An earlier version accepted a
    key-only body for every verb, which made it strictly more permissive than the
    gateway — so the harness could send `steer` a body the real deployment rejects
    with 400 and every test still passed. That divergence, not the harness body
    itself, is why the defect reached a live evaluation.
    """
    caps = overrides.get("capabilities", {v: False for v in _mod.CONTROL_VERBS})
    state_body = {
        "run_id": "msg-live",
        "generation": 2,
        "available": True,
        "reason": None,
        "capabilities": caps,
        "state": "running",
        "active_tool_count": 0,
        "updated_at": "2026-09-12T00:00:00Z",
        "commands": [],
    }
    state_body.update(overrides.get("state_extra", {}))
    # `state_extra` can add or change a key but not remove one, and "the key is
    # absent" is a distinct failure from "the key is wrong" for any check that
    # requires a field to be present (W2-02's capability map, W2-03's
    # `active_tool_count`). Hence an explicit drop list.
    for key in overrides.get("state_omit", ()):
        state_body.pop(key, None)

    # Every (url, status) the stub answered, in call order. Lets a test assert the
    # ladder a specific verb actually observed, which is the difference between
    # "the check passed" and "the check reached the question it exists to ask".
    answered: list[tuple[str, int]] = []

    def handler(method, url, headers=None, content=None, json=None, timeout=None):
        response = MagicMock()
        auth = (headers or {}).get("Authorization")
        flag_off = "gw-off" in url

        def reply(status, body):
            response.status_code = status
            response.json = lambda: body
            answered.append((url, status))
            return response

        if url.endswith("/state"):
            # Defaults to 200 so no existing test shifts; the override exists for
            # checks that compare a live contract and must refuse to compare it
            # against a response the gateway did not serve.
            return reply(overrides.get("state_status", 200), dict(state_body))
        if url.endswith("/ping"):
            return reply(200, {"run_id": "msg-live", "available": True})
        if not auth:
            return reply(overrides.get("anonymous_status", 401), {"detail": "unauthenticated"})
        if content is not None and len(content) > 16 * 1024:
            return reply(overrides.get("oversize_status", 413), {"detail": "too large"})
        if content is not None:
            return reply(overrides.get("malformed_status", 400), {"detail": "invalid json"})
        if json and any(key in json for key in ("actor", "target", "token")):
            return reply(overrides.get("overreach_status", 400), {"detail": "forbidden field"})
        # Per-verb schema, in the product's order: this precedes every
        # authorization answer below, so a body the verb's model rejects is a 400
        # even where a 404 or a 501 would otherwise be due. `steer` requires a
        # non-empty `instruction`; the other three forbid the field outright
        # (`extra="forbid"`), so sending it everywhere would not be a fix either.
        schema_error = body_schema_error(url, json)
        if schema_error:
            return reply(overrides.get("schema_status", 400), {"detail": schema_error})
        if "msg-unknown" in url:
            # `unknown_status` is overridable so a test can put a *body-validation*
            # rejection where the indistinguishable 404 belongs — the real fault
            # the steer false negative was masquerading as (#5015).
            return reply(
                overrides.get("unknown_status", 404),
                dict(overrides.get("unknown_body", {"detail": "not found"})),
            )
        if auth != f"Bearer {OWNER_TOKEN}":
            # `nonowner_body_by_verb` bends the refusal for ONE verb and leaves the
            # rest correct, so a test can make the enumeration oracle appear on the
            # verb it names. A global `nonowner_body` would fail on `pause` first —
            # W1-02 visits it before `steer` — and satisfy a steer-specific
            # assertion without ever reaching steer.
            per_verb = overrides.get("nonowner_body_by_verb", {})
            body = per_verb.get(url.rsplit("/", 1)[-1]) or overrides.get(
                "nonowner_body", {"detail": "not found"}
            )
            return reply(404, dict(body))
        if flag_off:
            return reply(overrides.get("flag_off_status", 503), {"detail": "disabled"})
        if "msg-term" in url:
            return reply(overrides.get("terminal_status", 410), {"detail": "gone"})
        return reply(overrides.get("authorized_status", 501), {"detail": "not implemented"})

    client = MagicMock()
    client.request.side_effect = handler
    client.answered = answered
    return client


def ddb_stub(item: dict | None = None, *, existed: bool = True) -> MagicMock:
    """A DynamoDB stub for the cleanup path.

    `existed` drives DeleteItem's ALL_OLD response, which is how the harness tells
    "removed the fixture row" from "deleted nothing". It is set explicitly rather
    than left to MagicMock's default, because an auto-created attribute is truthy
    and would make every test read as a successful removal for no stated reason.
    """
    client = dynamodb_with_schema(CORRECT_SCHEMA)
    client.get_item.return_value = {"Item": item} if item else {}
    client.delete_item.return_value = (
        {"Attributes": {"event_id": {"S": "fixture-row"}}} if existed else {}
    )
    return client


def run_driver(tmp_path: Path, *, config: dict, client, dynamodb=None, specs=None) -> dict:
    """Drive a wave's checks and return ``{check_id: CheckResult}``.

    Defaults to wave 1 so every existing caller keeps its meaning; `specs` selects
    another wave.
    """
    probe = _mod.Probe(config["gateway_url"], client)
    artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
    driver = _mod.Driver(config, probe, artifacts, dynamodb=dynamodb or ddb_stub())
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        results = _mod.run_checks(
            driver, _mod.WAVE1_CHECKS if specs is None else specs
        )
    return {result.check_id: result for result in results}


def run_w2_02(tmp_path: Path, *, contract=None, client=None, config=None) -> object:
    """Drive W2-02 alone and return its CheckResult.

    `contract` replaces the neutral_contract artifact payload; pass `False` to
    omit the artifact entirely (the not_run path).
    """
    payloads = artifact_payloads()
    if contract is False:
        payloads.pop("neutral_contract")
    elif contract is not None:
        payloads["neutral_contract"] = contract
    cfg = config or live_config(tmp_path, artifact_payloads=payloads)
    # Looked up by ID rather than indexed: a reordered manifest would silently
    # make this drive a different check.
    spec = next(s for s in _mod.WAVE2_CHECKS if s.check_id == "W2-02")
    results = run_driver(
        tmp_path, config=cfg, client=client or gateway_stub(), specs=(spec,)
    )
    return results["W2-02"]


class TestCheckIdsMatchTheEvaluationFile:
    """The drift that would have read OK: same IDs, different meanings.

    revival-design §7 makes the evaluation file's table authoritative. A harness
    keyed by these IDs with a different partition of the space would satisfy the
    operator's `jq` gate and every `check()` call while proving something other
    than what the evaluation requires — and nothing downstream would flag it,
    which is worse than a check that is plainly missing. These tests pin each
    ID's meaning so a future edit has to change a test to change the contract.
    """

    # Transcribed from evaluation #3967's acceptance table.
    EXPECTED_ACCEPTANCE_IDS = {
        "W1-01": ("Gate/regression",),
        "W1-02": ("AC-S1", "AC-S2"),
        "W1-03": ("AC-S3",),
        "W1-04": ("AC-S4",),
        "W1-05": ("AC-S5",),
        "W1-06": ("AC-S6",),
        "W1-07": ("AC-S7",),
        "W1-08": ("AC-F1", "AC-F2"),
        "W1-09": ("Gate/regression",),
        "W1-10": ("Gate/regression",),
    }

    def test_every_check_carries_the_acceptance_ids_the_table_assigns(self):
        actual = {spec.check_id: spec.acceptance_ids for spec in _mod.WAVE1_CHECKS}

        assert actual == self.EXPECTED_ACCEPTANCE_IDS

    def test_the_story_owns_exactly_the_acceptance_ids_it_claims(self):
        """#3960's "Owned acceptance IDs" line, checked against the manifest."""
        owned = {
            item
            for spec in _mod.WAVE1_CHECKS
            for item in spec.acceptance_ids
            if item.startswith("AC-")
        }

        assert owned == {
            "AC-S1",
            "AC-S2",
            "AC-S3",
            "AC-S4",
            "AC-S5",
            "AC-S6",
            "AC-S7",
            "AC-F1",
            "AC-F2",
        }

    @pytest.mark.parametrize(
        ("check_id", "phrase"),
        [
            ("W1-01", "preflight"),
            ("W1-02", "BOTH adapters"),
            ("W1-03", "expiry"),
            ("W1-04", "non-gateway probe pod"),
            ("W1-05", "413"),
            ("W1-06", "410"),
            ("W1-07", "before transport"),
            ("W1-08", "flag-off"),
            ("W1-09", "control_schemas.py"),
            ("W1-10", "negative tests"),
        ],
    )
    def test_each_id_keeps_its_subject(self, check_id: str, phrase: str):
        """The specific confusions this pins.

        W1-04 is the peer probe and W1-09 is schema/journal conformance — an
        earlier revision of this harness had those two swapped, and no test
        objected because both IDs existed and both "passed".
        """
        assert phrase in _mod.CHECK_DESCRIPTIONS[check_id]

    def test_the_peer_probe_is_not_filed_under_w1_09(self):
        """Stated as its own assertion because it is the exact historical drift."""
        assert "probe pod" not in _mod.CHECK_DESCRIPTIONS["W1-09"]
        assert "probe pod" in _mod.CHECK_DESCRIPTIONS["W1-04"]

    def test_every_id_has_a_predicate(self):
        """A manifest entry with no predicate would be a check that never runs."""
        assert set(_mod.WAVE1_PREDICATES) == set(_mod.EXPECTED_CHECK_IDS)
        for check_id, method_name in _mod.WAVE1_PREDICATES.items():
            assert hasattr(_mod.Driver, method_name), check_id

    def test_every_supported_wave_is_carried_by_a_real_manifest(self):
        """Each registered wave has its full manifest; unsupported waves stay absent."""
        assert _mod.SUPPORTED_WAVES == (1, 2, 3, 4)
        assert _mod.SUPPORTED_WAVES == tuple(sorted(_mod.WAVE_CHECKS))
        assert all(_mod.WAVE_CHECKS.values())
        assert 5 not in _mod.WAVE_CHECKS

    def test_wave_two_carries_the_whole_evaluation_manifest(self):
        """All ten of #3968's IDs, not just the one this story implements.

        The manifest is what `report_is_passing` divides by. Registering wave 2
        with only its finished check would make it a 1/1 wave that exits 0 — a
        green report for a wave whose pause proof does not exist. Pinned as an
        exact tuple so it cannot quietly shrink to match whatever is implemented.
        """
        assert tuple(spec.check_id for spec in _mod.WAVE2_CHECKS) == (
            "W2-01",
            "W2-02",
            "W2-03",
            "W2-04",
            "W2-05",
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
            "W2-10",
        )

    def test_s3_implements_exactly_w2_02(self):
        """AC-T7 is this story's owned acceptance ID; the rest are other stories'.

        Asserted in both directions. An extra predicate here would mean S3 is
        claiming evidence for a property it did not build.

        Updated when S2 (#3961) landed W2-03..W2-05, and again when #3968's defect
        (#5825) landed W2-01 and W2-10: the point of this assertion is that W2-02
        maps to AC-T7 and that every registered predicate resolves to a real method,
        not that S3 is the only story to have delivered one.
        """
        assert set(_mod.WAVE2_PREDICATES) == {
            "W2-01",  # #3968 defect #5825 — consolidated wave-2 preflight
            "W2-02",  # S3 #3962 — AC-T7, this story's own
            "W2-03",  # S2 #3961
            "W2-04",  # S2 #3961
            "W2-05",  # S2 #3961
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
            "W2-10",  # #3968 defect #5825 — cleanup and security recheck
        }
        assert _mod.CHECK_ACCEPTANCE_IDS["W2-02"] == ("AC-T7",)
        for method_name in _mod.WAVE2_PREDICATES.values():
            assert hasattr(_mod.Driver, method_name)

    def test_every_unimplemented_manifest_entry_names_an_owner(self):
        """A not_run must say who delivers it.

        Both directions. An unowned entry becomes nobody's job; an owner recorded
        for a check that IS implemented is a stale note that will read as
        outstanding work after the story lands.
        """
        registered = {spec.check_id for spec in _mod.ALL_CHECK_SPECS}
        implemented = set(_mod.CHECK_PREDICATES)

        assert set(_mod.PENDING_CHECK_OWNERS) == registered - implemented
        for check_id, owner in _mod.PENDING_CHECK_OWNERS.items():
            assert owner.strip(), check_id

    def test_check_ids_are_unique_across_waves(self):
        """CHECK_DESCRIPTIONS is keyed by ID alone and spans every wave.

        Two waves sharing an ID would make one check's evidence silently describe
        the other's — and the report is keyed by ID too, so nothing downstream
        could tell.
        """
        ids = [spec.check_id for spec in _mod.ALL_CHECK_SPECS]

        assert len(ids) == len(set(ids))

    def test_the_wave_one_manifest_default_did_not_grow(self):
        """`EXPECTED_CHECK_IDS` is `assert_check_manifest`'s default.

        If it had grown to span every wave, a wave-1 report would satisfy the
        manifest guard while missing nine checks — the guard would still run, and
        would still pass.
        """
        assert _mod.EXPECTED_CHECK_IDS == tuple(
            spec.check_id for spec in _mod.WAVE1_CHECKS
        )
        assert not any(cid.startswith("W2-") for cid in _mod.EXPECTED_CHECK_IDS)

    def test_each_wave_reports_its_own_evaluation_issue(self):
        """#3967 accepted wave 1 and is closed; #3968 owns wave 2, #3969 wave 3.

        A wave-2 report labelled 3967 would attach evidence to a finished
        evaluation.
        """
        assert _mod.WAVE_EVALUATIONS[1] == "3967"
        assert _mod.WAVE_EVALUATIONS[2] == "3968"
        assert _mod.WAVE_EVALUATIONS[3] == "3969"
        assert set(_mod.WAVE_EVALUATIONS) == set(_mod.WAVE_CHECKS)
        assert set(_mod.WAVE_REVISIONS) == set(_mod.WAVE_CHECKS)

    def test_wave_four_carries_the_full_ten_check_manifest(self):
        """#3970's whole table, not only the four checks S7 implements.

        Same load-bearing property as wave 2: registering only S7's four checks
        would make `required` 4, all four could pass, `passed == required` would
        hold and `--wave 4` would exit 0 — a report indistinguishable from a
        complete wave-4 pass, on a wave whose entire purpose is consolidating all
        37 criteria. The count stays at ten so the six it does not implement show
        up as NOT RUN against a real bar.
        """
        assert tuple(spec.check_id for spec in _mod.WAVE4_CHECKS) == (
            "W4-01",
            "W4-02",
            "W4-03",
            "W4-04",
            "W4-05",
            "W4-06",
            "W4-07",
            "W4-08",
            "W4-09",
            "W4-10",
        )

    def test_wave_four_now_implements_every_check_in_its_manifest(self):
        """All ten, with nothing left owed — and nothing gained by it.

        This assertion replaces an earlier one pinning wave 4 at S7's four checks.
        That was the right bar while six predicates did not exist: a partial W4-03
        that checked the browser steer and not S6's retry/FIFO/cap proof would have
        reported a green consolidation over a proof nobody made.

        #3970 implemented the six, so the correct invariant flips. The load-bearing
        part is the SECOND assertion: an empty PENDING_CHECK_OWNERS for this wave must
        not be read as the wave being closer to passing. It means the only remaining
        cause of a wave-4 not_run is a missing INPUT, which is a different and honest
        answer — `TestWaveFourRefusesWithoutItsInputs` is where that is pinned.
        """
        assert set(_mod.WAVE4_PREDICATES) == {
            "W4-01",  # #3970 — the wave's own preflight
            "W4-02",  # S7 #3966 — AC-F3, browser
            "W4-03",  # #3970 — steering consolidation
            "W4-04",  # S7 #3966 — the pause family, browser
            "W4-05",  # #3970 — abort consolidation
            "W4-06",  # #3970 — security matrix consolidation
            "W4-07",  # S7 #3966 — live schema parity
            "W4-08",  # S7 #3966 — polling lifecycle, browser
            "W4-09",  # #3970 — runtime comparison consolidation
            "W4-10",  # #3970 — the 37-criterion evidence index
        }
        owed = {spec.check_id for spec in _mod.WAVE4_CHECKS} - set(_mod.WAVE4_PREDICATES)
        assert owed == set()
        # No stale owner entry survives: an owner recorded against a delivered check
        # reads as outstanding work forever.
        assert not (set(_mod.PENDING_CHECK_OWNERS) & {
            spec.check_id for spec in _mod.WAVE4_CHECKS
        })
        for method_name in _mod.WAVE4_PREDICATES.values():
            assert hasattr(_mod.Driver, method_name), method_name

    def test_the_wave_four_gate_names_are_the_names_gateway_ci_defines(self):
        """An invented gate name is a requirement no operator could ever satisfy.

        This is the mirror of the false pass and just as harmful: W4-01 requires each
        named gate to have passed, so a name CI does not define makes wave 4
        permanently unsatisfiable, and the only way around it is to hand-write a
        record for a job that never ran.

        The first draft of W4-01 named three gates — "Frontend unit tests",
        "Frontend typecheck", "Frontend build" — and NONE of them existed:
        `gateway-ci.yml` defines `Frontend Unit Tests` (which runs Vitest and then
        `tsc --noEmit` in the same job, so typecheck has no separate gate) and
        `Build Container`. Asserted against the workflow text so the next rename is a
        failing test here rather than a stuck evaluation.
        """
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "gateway-ci.yml"
        ).read_text(encoding="utf-8")

        for gate in _mod.WAVE4_REQUIRED_CI_GATES:
            assert f"name: {gate}" in workflow, gate

        # And the coarseness is deliberate, not an omission: typecheck runs inside the
        # Vitest job, so there is no third gate to require.
        assert "npx tsc --noEmit" in workflow
        assert "Frontend typecheck" not in workflow

    def test_wave_four_does_not_reimplement_the_control_gate_checks(self):
        """W2-01 validates the control gates; W4-01 must not validate them again.

        Two implementations of one claim in one report are free to disagree, and the
        second one would necessarily be the weaker: it would be written to the schema
        the FRONTEND gates can satisfy, and `gateway-ci.yml` publishes no
        `checked-out-revision-*` artifact, so it could not bind a checkout to a run at
        all. The weaker copy is then the one an operator satisfies.

        Prior-wave acceptance is the stronger link — W4-01 requires wave 2 accepted,
        which means W2-01 passed with the full archived-run-plus-checkout binding on a
        revision contained in what is deployed.
        """
        source = (
            REPO_ROOT / "platform" / "scripts" / "agent-control-eval.py"
        ).read_text(encoding="utf-8")
        start = source.index("    def check_w4_01(")
        end = source.index("    # ---- wave 4 (#3966)", start)
        body = source[start:end]

        assert "WAVE4_REQUIRED_CI_GATES" in body
        assert "WAVE2_REQUIRED_CI_GATES" not in body
        # The prerequisite that actually carries them.
        assert "_assert_prior_wave_accepted" in body

    def test_wave_four_records_its_evaluation_and_revision(self):
        """#3970 reads wave 4, under revision revival-2026-09-12.

        The revision matters: the wave-4 body self-identifies as
        revival-2026-09-12, and harness-neutral-2026-09-15 is only a
        compatibility amendment that explicitly does not authorize this wave.
        Recording the amendment here would overstate what has been approved.
        """
        assert _mod.WAVE_EVALUATIONS[4] == "3970"
        assert _mod.WAVE_REVISIONS[4] == "revival-2026-09-12"


    def test_wave_two_carries_the_full_ten_check_manifest(self):
        """The manifest is #3968's whole table, not just the checks S5 implements.

        This is the load-bearing assertion of the partial-wave design. Registering
        only S5's four checks would make `required` 4, all four would pass,
        `passed == required` would hold and `--wave 2` would exit 0 — a report
        indistinguishable from a complete wave-2 pass. The count stays at ten so
        the six unimplemented checks show up as NOT RUN against a real bar.
        """
        assert tuple(spec.check_id for spec in _mod.WAVE2_CHECKS) == (
            "W2-01",
            "W2-02",
            "W2-03",
            "W2-04",
            "W2-05",
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
            "W2-10",
        )


    def test_wave_two_assigns_the_acceptance_ids_the_table_assigns(self):
        """Transcribed from evaluation #3968's acceptance table."""
        actual = {spec.check_id: spec.acceptance_ids for spec in _mod.WAVE2_CHECKS}

        assert actual == {
            "W2-01": ("Gate/regression",),
            "W2-02": ("AC-T7",),
            "W2-03": ("AC-P1",),
            "W2-04": ("AC-P2",),
            "W2-05": ("AC-P3", "AC-P5", "AC-P6"),
            "W2-06": ("AC-A3", "AC-A9"),
            "W2-07": ("AC-A10",),
            "W2-08": ("AC-A11", "AC-A12"),
            "W2-09": ("AC-A10",),
            "W2-10": ("Gate/regression",),
        }


    def test_s5_implements_exactly_the_checks_its_acceptance_ids_cover(self):
        """#3964 owns AC-A3/A9/A10/A11/A12 — and therefore W2-06..W2-09.

        Both directions matter. A predicate for a check S5 does not own would be
        this story asserting another story's work; a missing one would be a check
        reported NOT RUN when it could actually have been answered.

        S2 (#3961) has since added W2-03..W2-05 for AC-P1/P2/P3/P5/P6 and #3968's
        defect (#5825) added W2-01 and W2-10, so the implemented set is asserted as
        S3's + S2's + S5's + the defect's rather than S5's alone. Every wave-2 ID now
        resolves to a predicate; what still makes a wave-2 run fail is absent or
        contradicted evidence, which is a different and correct reason.
        """
        assert set(_mod.WAVE2_PREDICATES) == {
            "W2-01",  # #3968 / #5825 — consolidated preflight
            "W2-02",  # S3 #3962 — AC-T7
            "W2-03",  # S2 #3961 — AC-P1
            "W2-04",  # S2 #3961 — AC-P2
            "W2-05",  # S2 #3961 — AC-P3/P5/P6
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
            "W2-10",  # #3968 / #5825 — cleanup and security recheck
        }
        for check_id, method_name in _mod.WAVE2_PREDICATES.items():
            assert hasattr(_mod.Driver, method_name), check_id


    def test_every_unimplemented_wave_two_check_names_its_owner(self):
        """A NOT RUN with no owner is a dead end for the operator reading it.

        Wave 2 is now fully implemented, so the correct assertion is that the
        mapping carries no stale wave-2 entry — an owner recorded against a
        delivered check reads as outstanding work forever. The both-directions
        invariant across every wave lives in
        `test_every_unimplemented_manifest_entry_names_an_owner`.
        """
        implemented = set(_mod.WAVE2_PREDICATES)
        all_ids = {spec.check_id for spec in _mod.WAVE2_CHECKS}

        assert all_ids - implemented == set()
        assert not (set(_mod.PENDING_CHECK_OWNERS) & all_ids)
        assert all(owner.strip() for owner in _mod.PENDING_CHECK_OWNERS.values())


class TestTheMirroredControlRuntimeConstants:
    """The harness mirrors four values from TypeScript. This is the seam.

    `CONTROL_PROTOCOL_VERSION`, `CLAUDE_ADAPTER_ID`,
    `EXPECTED_CLAUDE_SDK_VERSION` and `STEER_QUEUE_CAP` are copied rather than
    imported, deliberately:
    this script runs standalone against a URL and must not acquire the agent
    module's dependency tree. The cost of copying is drift, and drift here is
    silent in the worst way — the harness would keep accepting evidence recorded
    against a contract version the deployment no longer speaks, and report it as a
    pass. So the copies are pinned against the sources in CI, where a bump becomes
    a failing test to update rather than a stale live evaluation.
    """

    AGENT_SRC = REPO_ROOT / "modules" / "agent-factory" / "agent" / "src"
    RUNTIME = AGENT_SRC / "control-runtime.ts"
    ADAPTER = AGENT_SRC / "harnesses" / "claude-control.ts"
    CONTROL_STATE = AGENT_SRC / "control-state.ts"
    PACKAGE_JSON = REPO_ROOT / "modules" / "agent-factory" / "agent" / "package.json"

    def test_the_typescript_sources_exist(self):
        """Named deliverables of #3962. A rename must fail here, not silently pass."""
        assert self.RUNTIME.is_file()
        assert self.ADAPTER.is_file()

    def test_the_steering_queue_cap_matches_the_journal(self):
        """W3-07 asserts an exact accepted count, so a bump must fail here first.

        The cap is the journal's `DEFAULT_MAX_PENDING`. If the worker's default moved
        to 20 and this copy stayed at 10, W3-07 would fail a correct deployment for
        accepting twenty — reported as a steering defect, in the one check whose whole
        subject is the bound. The story requires the cap be configurable, which is
        exactly what makes the default worth pinning rather than assuming.
        """
        assert self.CONTROL_STATE.is_file()
        match = re.search(
            r"export const DEFAULT_MAX_PENDING\s*=\s*(\d+)",
            self.CONTROL_STATE.read_text(encoding="utf-8"),
        )

        assert match, "DEFAULT_MAX_PENDING not found in control-state.ts"
        assert int(match.group(1)) == _mod.STEER_QUEUE_CAP

    def test_the_protocol_version_matches_the_neutral_contract(self):
        source = self.RUNTIME.read_text(encoding="utf-8")
        match = re.search(
            r"export const CONTROL_PROTOCOL_VERSION\s*=\s*(\d+)", source
        )

        assert match, "CONTROL_PROTOCOL_VERSION not found in control-runtime.ts"
        assert int(match.group(1)) == _mod.CONTROL_PROTOCOL_VERSION

    def test_the_adapter_id_matches_the_claude_adapter(self):
        source = self.ADAPTER.read_text(encoding="utf-8")
        match = re.search(
            r"export const CLAUDE_ADAPTER_ID\s*=\s*'([^']+)'", source
        )

        assert match, "CLAUDE_ADAPTER_ID not found in claude-control.ts"
        assert match.group(1) == _mod.CLAUDE_ADAPTER_ID

    def test_the_sdk_pin_matches_the_adapter_and_the_dependency(self):
        """Three places, and all three must agree.

        The adapter records the version it was proven against; package.json is what
        a bump actually edits. If those two disagree, the adapter's evidence is
        stale — and this harness would accept it either way without this test.
        """
        adapter_match = re.search(
            r"export const CLAUDE_SDK_VERSION\s*=\s*'([^']+)'",
            self.ADAPTER.read_text(encoding="utf-8"),
        )
        assert adapter_match, "CLAUDE_SDK_VERSION not found in claude-control.ts"

        declared = json.loads(self.PACKAGE_JSON.read_text(encoding="utf-8"))[
            "dependencies"
        ]["@anthropic-ai/claude-agent-sdk"]

        assert adapter_match.group(1) == _mod.EXPECTED_CLAUDE_SDK_VERSION
        assert declared.lstrip("^~") == _mod.EXPECTED_CLAUDE_SDK_VERSION

    def test_the_four_verbs_match_the_neutral_contract(self):
        """The verb universe, on both sides of the language boundary."""
        source = self.RUNTIME.read_text(encoding="utf-8")
        match = re.search(
            r"const verbs: ControlAction\[\] = \[([^\]]+)\]", source
        )

        assert match, "the verb list was not found in intersectCapabilities"
        verbs = tuple(re.findall(r"'([a-z]+)'", match.group(1)))
        assert verbs == _mod.CONTROL_VERBS


class TestWave2NeutralContract:
    """W2-02 / AC-T7 — the check S3 #3962 delivers.

    Two halves, tested separately because they fail for different reasons: the
    operator-recorded contract artifact (validated, not trusted) and the deployed
    capability surface the harness reads itself. The second exists because the
    artifact describes a source tree while the evaluation is about a deployment,
    and the failure worth catching is a green suite paired with a build that
    advertises a verb.
    """

    def test_a_complete_artifact_and_correct_deployment_passes(self, tmp_path: Path):
        result = run_w2_02(tmp_path)

        assert result.status == _mod.STATUS_PASSED, result.message

    def test_the_check_records_evidence(self, tmp_path: Path):
        """§7's `check()` requires nonempty evidence; a status alone is not enough."""
        result = run_w2_02(tmp_path)
        evidence = result.to_evidence()

        assert evidence["acceptance_ids"] == ["AC-T7"]
        assert evidence["evidence"]
        # Both kinds: the artifact path and the state reads it made itself.
        assert any("artifact" in item for item in evidence["evidence"])
        assert any("command" in item for item in evidence["evidence"])

    def test_a_missing_artifact_is_not_run_not_a_pass(self, tmp_path: Path):
        result = run_w2_02(tmp_path, contract=False)

        assert result.status == _mod.STATUS_NOT_RUN
        assert "neutral_contract" in result.message

    @pytest.mark.parametrize(
        "dropped_key", _mod.REQUIRED_ARTIFACT_KEYS["neutral_contract"]
    )
    def test_an_incomplete_artifact_fails_rather_than_skipping(
        self, tmp_path: Path, dropped_key: str
    ):
        """Parametrized over every key: a claim without its evidence is a failure.

        `not_run` for a half-filled artifact would let an operator satisfy this
        check by leaving out whichever property is awkward to prove.
        """
        payload = neutral_contract_payload()
        del payload[dropped_key]

        result = run_w2_02(tmp_path, contract=payload)

        assert result.status == _mod.STATUS_FAILED
        assert dropped_key in result.message

    @pytest.mark.parametrize(
        "prop",
        [
            key
            for key in _mod.REQUIRED_ARTIFACT_KEYS["neutral_contract"]
            if key not in _mod._NEUTRAL_CONTRACT_NON_BOOLEAN_KEYS
        ],
    )
    def test_each_named_property_fails_on_its_own(self, tmp_path: Path, prop: str):
        """Thirteen properties, thirteen distinguishable failures.

        The point of one key per property is that the report says WHICH one is
        unproven. An `all(...)` over the group would collapse them into a single
        boolean and this test would be impossible to write.
        """
        result = run_w2_02(tmp_path, contract=neutral_contract_payload(**{prop: False}))

        assert result.status == _mod.STATUS_FAILED
        assert prop in result.message
        # The message must also say why anyone cared — this evidence is read by
        # people who did not write the story.
        assert len(result.message) > len(prop) + 40

    def test_a_property_recorded_as_a_truthy_non_true_fails(self, tmp_path: Path):
        """`is not True`, not falsiness. "probably" is not a proof."""
        result = run_w2_02(
            tmp_path, contract=neutral_contract_payload(disposed_once="yes")
        )

        assert result.status == _mod.STATUS_FAILED
        assert "disposed_once" in result.message

    def test_a_protocol_version_mismatch_fails(self, tmp_path: Path):
        result = run_w2_02(
            tmp_path,
            contract=neutral_contract_payload(
                protocol_version=_mod.CONTROL_PROTOCOL_VERSION + 1
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "protocol version" in result.message

    def test_a_non_claude_production_adapter_fails(self, tmp_path: Path):
        """Claude is the first production adapter; the second is test-only.

        Substitutability in a test suite is not accepted live second-harness
        support, and this is the assertion that keeps those apart.
        """
        result = run_w2_02(tmp_path, contract=neutral_contract_payload(adapter_id="echo"))

        assert result.status == _mod.STATUS_FAILED
        assert "second-harness" in result.message

    def test_an_sdk_version_other_than_the_pin_fails(self, tmp_path: Path):
        """Streaming input and shouldQuery are observed behaviour, not a guarantee."""
        result = run_w2_02(
            tmp_path, contract=neutral_contract_payload(sdk_version="0.3.999")
        )

        assert result.status == _mod.STATUS_FAILED
        assert "lockfile" in result.message

    def test_only_the_claude_adapter_fails(self, tmp_path: Path):
        """One adapter passing a neutral suite proves the suite runs, not neutrality."""
        result = run_w2_02(
            tmp_path,
            contract=neutral_contract_payload(
                adapters={"claude": {"passed": True, "test_count": 61}}
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "BOTH" in result.message or "independently shaped" in result.message

    def test_a_second_adapter_that_did_not_pass_fails(self, tmp_path: Path):
        result = run_w2_02(
            tmp_path,
            contract=neutral_contract_payload(
                adapters={
                    "claude": {"passed": True, "test_count": 61},
                    "echo": {"passed": False, "test_count": 61},
                }
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "echo" in result.message

    def test_an_adapter_that_ran_zero_tests_fails(self, tmp_path: Path):
        """The specific way "passed" lies.

        A jest run over a deleted file exits 0. Without a positive test count,
        `passed: true` is satisfied by a suite that asserted nothing — which is the
        same hazard the CI job avoids by pinning test files by name.
        """
        result = run_w2_02(
            tmp_path,
            contract=neutral_contract_payload(
                adapters={
                    "claude": {"passed": True, "test_count": 61},
                    "echo": {"passed": True, "test_count": 0},
                }
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "ran nothing" in result.message

    def test_a_second_adapter_without_a_missing_capability_fails(self, tmp_path: Path):
        """Without a capability gap the intersection is never observed working."""
        second = {
            "name": "echo",
            "declares_missing_capability": False,
            "imports_provider_sdk": False,
        }

        result = run_w2_02(tmp_path, contract=neutral_contract_payload(second_adapter=second))

        assert result.status == _mod.STATUS_FAILED
        assert "missing capability" in result.message

    def test_a_second_adapter_importing_the_provider_sdk_fails(self, tmp_path: Path):
        """A look-alike proves the contract accepts Claude's shape — the opposite."""
        second = {
            "name": "echo",
            "declares_missing_capability": True,
            "imports_provider_sdk": True,
        }

        result = run_w2_02(tmp_path, contract=neutral_contract_payload(second_adapter=second))

        assert result.status == _mod.STATUS_FAILED
        assert "provider SDK" in result.message

    def test_a_second_adapter_naming_an_unreported_adapter_fails(self, tmp_path: Path):
        """The named second adapter must be one that actually ran."""
        second = {
            "name": "codex",
            "declares_missing_capability": True,
            "imports_provider_sdk": False,
        }

        result = run_w2_02(tmp_path, contract=neutral_contract_payload(second_adapter=second))

        assert result.status == _mod.STATUS_FAILED
        assert "codex" in result.message

    @pytest.mark.parametrize("verb", _mod.CONTROL_VERBS)
    def test_a_deployed_build_advertising_any_verb_fails(self, tmp_path: Path, verb: str):
        """The half the harness observes itself, per verb.

        S3 keeps all four unsupported. A true capability puts a button on the
        dashboard whose handler returns 501 — and a perfect artifact cannot see
        this, because it describes the source tree rather than the deployment.
        """
        caps = {v: v == verb for v in _mod.CONTROL_VERBS}

        result = run_w2_02(tmp_path, client=gateway_stub(capabilities=caps))

        assert result.status == _mod.STATUS_FAILED
        assert verb in result.message

    def test_a_deployed_build_omitting_a_verb_key_fails(self, tmp_path: Path):
        """Absent and false are indistinguishable to the dashboard, not to the contract."""
        caps = {v: False for v in _mod.CONTROL_VERBS if v != "abort"}

        result = run_w2_02(tmp_path, client=gateway_stub(capabilities=caps))

        assert result.status == _mod.STATUS_FAILED
        assert "abort" in result.message

    def test_both_adapters_state_endpoints_are_read(self, tmp_path: Path):
        """Not just one edge. Two HTTP surfaces share one control service.

        Reading only the activity adapter would leave the orchestration edge's
        capability rendering unobserved, which is the drift W1-02 exists to catch
        on the authorization side.
        """
        client = gateway_stub()
        run_w2_02(tmp_path, client=client)

        urls = [url for url, _ in client.answered]
        assert any("/activity/invocations/" in url and url.endswith("/state") for url in urls)
        assert any("/orchestration/runs/" in url and url.endswith("/state") for url in urls)

    def test_the_check_sends_no_command(self, tmp_path: Path):
        """AC-T7 is a contract check, not a control probe.

        W2-02 must not POST a verb: this wave's live steer completion is W3-11's,
        and a command sent here would be a side effect on a fixture in the name of
        reading a capability map.
        """
        client = gateway_stub()
        run_w2_02(tmp_path, client=client)

        assert client.answered, "the check made no request at all"
        assert all(url.endswith("/state") for url, _ in client.answered), client.answered

    def test_a_missing_live_run_id_is_not_run(self, tmp_path: Path):
        """The deployed half needs a fixture run; absent is "could not look"."""
        payloads = artifact_payloads()
        config = live_config(tmp_path, artifact_payloads=payloads)
        del config["live_run_id"]

        result = run_w2_02(tmp_path, config=config)

        assert result.status == _mod.STATUS_NOT_RUN
        assert "live_run_id" in result.message


class TestWave2Composition:
    """What `--wave 2` reports today, and why it must not exit 0."""

    def test_the_delivered_check_passes_and_the_rest_are_not_run(self, tmp_path: Path):
        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(),
            specs=_mod.WAVE2_CHECKS,
        )

        assert results["W2-02"].status == _mod.STATUS_PASSED, results["W2-02"].message
        outstanding = {
            cid: r.status for cid, r in results.items() if cid != "W2-02"
        }
        assert set(outstanding.values()) == {_mod.STATUS_NOT_RUN}, outstanding

    def test_every_not_run_names_its_owning_story(self, tmp_path: Path):
        """"Not implemented" without an owner is how a check stops being a job."""
        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(),
            specs=_mod.WAVE2_CHECKS,
        )

        for check_id, result in results.items():
            if check_id == "W2-02":
                continue
            assert result.message, check_id
            if check_id in _mod.PENDING_CHECK_OWNERS:
                assert "#" in result.message, (check_id, result.message)
            else:
                assert "prerequisite missing" in result.message

    def test_an_incomplete_wave_cannot_report_passing(self, tmp_path: Path):
        """The reason the whole manifest is registered rather than just W2-02.

        `report_is_passing` asks `passed == required`. A wave holding only its one
        finished check would be 1/1 and exit 0 — a green report for a wave whose
        pause proof does not exist yet.
        """
        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(),
            specs=_mod.WAVE2_CHECKS,
        )
        report = _mod.build_report(
            live_config(tmp_path),
            list(results.values()),
            cleanup_ok=True,
            wave=2,
            expected_ids=tuple(spec.check_id for spec in _mod.WAVE2_CHECKS),
        )

        assert report["required"] == 10
        assert report["passed"] == 1
        assert report["not_run"] == 9
        assert _mod.report_is_passing(report) is False

    def test_the_wave_two_report_names_its_own_evaluation_and_revision(
        self, tmp_path: Path
    ):
        report = _mod.build_report(
            live_config(tmp_path), [], cleanup_ok=True, wave=2, expected_ids=()
        )

        assert report["evaluation"] == "3968"
        assert report["revision"] == "harness-neutral-2026-09-15"

    def test_a_manifest_entry_with_neither_predicate_nor_owner_fails(
        self, tmp_path: Path
    ):
        """Unowned and unimplemented is a harness bug, not a wave in progress.

        `not_run` for this would be indistinguishable from a check someone is
        working on, so it is a FAILURE — that is the difference between "not yet"
        and "nobody's job".
        """
        orphan = _mod.CheckSpec("W2-99", ("AC-X",), "an unowned check")

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(),
            specs=(orphan,),
        )

        assert results["W2-99"].status == _mod.STATUS_FAILED
        assert "PENDING_CHECK_OWNERS" in results["W2-99"].message

    def test_wave_one_is_unchanged_by_the_wave_two_addition(self, tmp_path: Path):
        """The regression that matters: #3967 is accepted, so wave 1 must still be 10/10."""
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        assert list(results) == list(_mod.EXPECTED_CHECK_IDS)
        assert [r.status for r in results.values()] == [_mod.STATUS_PASSED] * 10


class TestDriverOutcomes:
    """pass / fail / not_run, and the boundary between them."""

    def test_a_correct_deployment_passes_every_check(self, tmp_path: Path):
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        assert [r.status for r in results.values()] == [_mod.STATUS_PASSED] * 10

    def test_a_missing_artifact_is_not_run_naming_the_prerequisite(
        self, tmp_path: Path
    ):
        """Not a pass, and not a blanket failure for the whole run."""
        config = live_config(tmp_path)
        del config["artifacts"]["peer_probe"]

        results = run_driver(tmp_path, config=config, client=gateway_stub())

        assert results["W1-04"].status == _mod.STATUS_NOT_RUN
        assert "peer_probe" in results["W1-04"].message
        # The other nine still ran: one unreachable observation must not blind
        # the operator to the rest.
        assert results["W1-02"].status == _mod.STATUS_PASSED

    def test_an_incomplete_artifact_fails_rather_than_skipping(self, tmp_path: Path):
        """A half-filled artifact is a claim without its evidence.

        Treating it as not_run would let an operator satisfy a check by recording
        an artifact with the awkward field left out.
        """
        payloads = artifact_payloads()
        del payloads["peer_probe"]["timeout_seconds"]

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-04"].status == _mod.STATUS_FAILED
        assert "timeout_seconds" in results["W1-04"].message

    def test_a_missing_identity_env_var_is_not_run(self, tmp_path: Path):
        """The token itself is never in config, so its absence is a prerequisite."""
        config = live_config(tmp_path)
        probe = _mod.Probe(config["gateway_url"], gateway_stub())
        driver = _mod.Driver(
            config,
            probe,
            _mod.ArtifactStore(tmp_path, config["artifacts"]),
            dynamodb=ddb_stub(),
        )

        with patch.dict("os.environ", {}, clear=True):
            results = {r.check_id: r for r in _mod.run_checks(driver)}

        assert results["W1-02"].status == _mod.STATUS_NOT_RUN
        assert "EVAL_OWNER_TOKEN_VAR" in results["W1-02"].message

    @pytest.mark.parametrize(
        ("override", "check_id"),
        [
            ({"anonymous_status": 200}, "W1-02"),
            ({"authorized_status": 200}, "W1-02"),
            ({"malformed_status": 501}, "W1-05"),
            ({"oversize_status": 400}, "W1-05"),
            ({"overreach_status": 200}, "W1-05"),
            ({"terminal_status": 501}, "W1-06"),
            ({"flag_off_status": 501}, "W1-08"),
        ],
    )
    def test_a_wrong_status_fails_its_check(
        self, tmp_path: Path, override: dict, check_id: str
    ):
        """Each mapping the story requires, broken one at a time."""
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub(**override)
        )

        assert results[check_id].status == _mod.STATUS_FAILED

    def test_a_true_capability_fails_w1_02(self, tmp_path: Path):
        """S1 implements no verb; a true capability puts a dead button on the UI."""
        client = gateway_stub(capabilities={"pause": True, "resume": False, "steer": False, "abort": False})

        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-02"].status == _mod.STATUS_FAILED
        assert "pause" in results["W1-02"].message

    def test_distinguishable_404_bodies_fail_w1_02(self, tmp_path: Path):
        """A 404 that says "not yours" is an enumeration oracle in a 404's clothes.

        Status codes alone cannot catch this, which is why the check compares the
        bodies of the three refusals rather than only their codes.
        """
        client = gateway_stub(nonowner_body={"detail": "you do not own this run"})

        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-02"].status == _mod.STATUS_FAILED
        assert "enumerate" in results["W1-02"].message

    def test_a_leaked_token_in_the_state_body_fails_w1_03(self, tmp_path: Path):
        client = gateway_stub(state_extra={"token": "leaked"})

        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-03"].status == _mod.STATUS_FAILED

    def test_a_missing_state_field_fails_w1_09(self, tmp_path: Path):
        """S7 renders this response and nothing else."""
        client = gateway_stub()
        original = client.request.side_effect

        def drop_generation(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if url.endswith("/state"):
                body = response.json()
                body.pop("generation", None)
                response.json = lambda: body
            return response

        client.request.side_effect = drop_generation

        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-09"].status == _mod.STATUS_FAILED
        assert "generation" in results["W1-09"].message

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("control_token", {"S": "x"}),
            ("control_address", {"S": "10.0.0.1"}),
            ("control_port", {"N": "8770"}),
            ("control_token_expires_at", {"N": "2000000000"}),
        ],
    )
    def test_leftover_private_fields_fail_w1_06(
        self, tmp_path: Path, field: str, value: dict
    ):
        """Each persisted private field must fail acceptance on its own."""
        dynamodb = ddb_stub(item={field: value})

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(),
            dynamodb=dynamodb,
        )

        assert results["W1-06"].status == _mod.STATUS_FAILED
        assert field in results["W1-06"].message

    def test_cleared_terminal_row_passes_w1_06(self, tmp_path: Path):
        config = live_config(tmp_path)
        dynamodb = ddb_stub(item={
            "event_id": {"S": config["terminal_run_id"]},
            "arrived_at": {"S": config["terminal_arrived_at"]},
            "status": {"S": "complete"},
        })

        results = run_driver(
            tmp_path, config=config, client=gateway_stub(), dynamodb=dynamodb
        )

        assert results["W1-06"].status == _mod.STATUS_PASSED

    def test_a_stale_deployed_digest_fails_w1_01(self, tmp_path: Path):
        """§7 rejects a stale deployment digest: the evidence would describe
        a different build than the one under review."""
        payloads = artifact_payloads()
        payloads["provenance"]["deployed_digest"] = "sha256:stale"

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-01"].status == _mod.STATUS_FAILED

    def test_a_policy_only_peer_result_fails_w1_04(self, tmp_path: Path):
        """§7: record actual connection results, "not policy YAML alone"."""
        payloads = artifact_payloads()
        payloads["peer_probe"]["probe_connect_result"] = "policy denies port 8770"

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-04"].status == _mod.STATUS_FAILED

    def test_an_unbounded_probe_timeout_fails_w1_04(self, tmp_path: Path):
        """An unbounded wait cannot distinguish "blocked" from "still trying"."""
        payloads = artifact_payloads()
        payloads["peer_probe"]["timeout_seconds"] = 0

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-04"].status == _mod.STATUS_FAILED

    def test_a_nonzero_assistant_turn_count_fails_w1_09(self, tmp_path: Path):
        """Polling a read contract must not cost model tokens or perturb the run."""
        payloads = artifact_payloads()
        payloads["journal_tests"]["assistant_turns"] = 1

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-09"].status == _mod.STATUS_FAILED

    def test_an_acknowledging_dead_worker_fails_w1_06(self, tmp_path: Path):
        payloads = artifact_payloads()
        payloads["worker_unavailable"]["command_acknowledged"] = True

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-06"].status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("negative", list(_mod.REQUIRED_ARTIFACT_KEYS["negative_tests"]))
    def test_each_unproven_negative_test_fails_w1_10(
        self, tmp_path: Path, negative: str
    ):
        """W1-10 is the harness auditing its own guards, one at a time."""
        payloads = artifact_payloads()
        payloads["negative_tests"][negative] = False

        results = run_driver(
            tmp_path,
            config=live_config(tmp_path, artifact_payloads=payloads),
            client=gateway_stub(),
        )

        assert results["W1-10"].status == _mod.STATUS_FAILED
        assert negative in results["W1-10"].message

    def test_a_failing_check_does_not_stop_the_others(self, tmp_path: Path):
        """Nine real answers plus one named failure beats aborting at the first."""
        results = run_driver(
            tmp_path,
            config=live_config(tmp_path),
            client=gateway_stub(terminal_status=501),
        )

        assert len(results) == 10
        assert results["W1-06"].status == _mod.STATUS_FAILED

    def test_every_check_records_evidence(self, tmp_path: Path):
        """§7 requires a nonempty evidence list; the gate reads it."""
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        for check_id, result in results.items():
            assert result.to_evidence()["evidence"], check_id


class TestEveryVerbGetsTheBodyItsSchemaRequires:
    """The per-verb request body, and the ladder it makes observable (#5015).

    `steer` is the only verb whose model requires its free text
    (`ControlSteerRequest.instruction`, `min_length=1`); the other three take the
    idempotency key plus an optional `reason`. The harness used to send one
    key-only body to all four, and because body validation deliberately precedes
    the authorization gate, every rung of steer's ladder — 401, three
    indistinguishable 404s, 501 — collapsed into a single 400. The evaluation
    reported `W1-02 failed: activity/steer: unknown_run returned 400, expected
    404` against a correct deployment, and the authorization proof for the one
    verb that carries free-form text to an agent was never actually observed.

    Two things have to hold at once here, and they pull in opposite directions:
    the *valid* bodies must now be valid per verb, and the *deliberately invalid*
    bodies must stay invalid. Loosening the second to fix the first would retire
    W1-05's ordering guarantee, which is the failure mode this class watches for.
    """

    # ---- the helper's own contract ------------------------------------

    def test_steer_carries_an_instruction(self):
        body = _mod.valid_command_body("steer", "id-1")

        assert body["instruction"]
        assert body["command_id"] == "id-1"

    @pytest.mark.parametrize("verb", ["pause", "resume", "abort"])
    def test_the_reason_taking_verbs_keep_the_key_only_body(self, verb: str):
        """`extra="forbid"` cuts both ways: an `instruction` here is a 400 too.

        This is the "per-verb body applied to the wrong verbs" blast radius — the
        three reason-taking verbs would start failing on a valid-body path and
        hide a real refusal-ordering regression behind the noise.
        """
        assert _mod.valid_command_body(verb, "id-1") == {"command_id": "id-1"}

    def test_the_instruction_is_bounded_well_under_the_schema_cap(self):
        """MAX_INSTRUCTION_CHARS is 4000; the probe text must not approach it.

        A long instruction would blur into W1-05's oversize leg, which is a
        different observation that must keep answering 413.
        """
        assert 0 < len(_mod.STEER_INSTRUCTION) <= 200

    def test_every_verb_is_covered_by_the_helper(self):
        """No verb may fall through to a body its own model rejects."""
        for verb in _mod.CONTROL_VERBS:
            assert body_schema_error(f"/x/{verb}", _mod.valid_command_body(verb, "id-1")) is None

    # ---- what the harness actually sends -------------------------------

    def test_the_harness_sends_a_valid_body_to_every_verb_on_both_adapters(
        self, tmp_path: Path
    ):
        """Observed off the wire, not asserted about the helper in isolation.

        The defect was not in a body-shaping function — there wasn't one. It was
        in what the checks passed to the probe, so this reads the recorded calls.
        """
        client = gateway_stub()
        run_driver(tmp_path, config=live_config(tmp_path), client=client)

        posts = [
            call
            for call in client.request.call_args_list
            if call.args[0] == "POST" and call.kwargs.get("json") is not None
        ]
        # Only the legs that are *supposed* to be valid: W1-05 sends over-reaching
        # bodies on purpose and they must keep being rejected.
        ladder = [
            call
            for call in posts
            if not any(k in call.kwargs["json"] for k in ("actor", "target", "token"))
        ]
        assert ladder, "no ladder probes were recorded at all"
        for call in ladder:
            url = call.args[1]
            error = body_schema_error(url, call.kwargs["json"])
            assert error is None, f"{url} was sent a body the gateway rejects: {error}"

        # And specifically: steer was reached on both adapters, with an
        # instruction. Without this, a future edit that stopped probing steer
        # entirely would satisfy the loop above vacuously.
        steered = [c for c in ladder if c.args[1].endswith("/steer")]
        assert {"/activity/" in c.args[1] for c in steered} == {True, False}
        for call in steered:
            assert call.kwargs["json"]["instruction"]

    def test_the_authorization_ladder_is_observed_for_steer(self, tmp_path: Path):
        """The whole point: steer must reach 401 / 404×3 / 501, not one 400.

        Asserted on the statuses the gateway actually answered for the steer
        route, because "W1-02 passed" alone would also be true of a harness that
        stopped probing steer. The smoke test in the issue asks for exactly this
        shape — 401, three identical 404s, and 501 — recorded for the free-text
        verb.
        """
        client = gateway_stub()
        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-02"].status == _mod.STATUS_PASSED

        for adapter_marker in ("/activity/", "/orchestration/"):
            ladder = [
                status
                for url, status in client.answered
                if url.endswith("/steer") and adapter_marker in url
            ]
            # W1-02's five rungs, in the order the check drives them. Slicing
            # rather than comparing the whole list: W1-05 also posts to /steer,
            # and its rejections are a different check's business.
            assert ladder[:5] == [401, 404, 404, 404, 501], adapter_marker
            assert 400 not in ladder[:5], (
                f"{adapter_marker}steer answered a body-validation rejection inside the "
                "authorization ladder — the #5015 false negative"
            )

    # ---- the fix must not have made the harness blind ------------------

    def test_a_body_rejection_where_the_unknown_run_refusal_belongs_still_fails(
        self, tmp_path: Path
    ):
        """The real fault this false negative was masquerading as.

        If a deployment ever answers 400 where the indistinguishable 404 is due,
        W1-02 must still fail. Making the harness send a valid body must not be
        the same thing as teaching it to accept 400 as a pass — that would retire
        the steer authorization ladder permanently and let a regression where one
        tenant can steer another tenant's agent through the gate.
        """
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub(unknown_status=400)
        )

        assert results["W1-02"].status == _mod.STATUS_FAILED
        assert "unknown_run returned 400, expected 404" in results["W1-02"].message

    def test_the_body_schema_predicate_mirrors_the_two_request_models(self):
        """The predicate's own contract: what each model accepts and refuses.

        This is a unit test of the mirror, not of the stub. The stub integration
        is exercised separately below, because a stub that stopped consulting
        this predicate would still satisfy these three lines.
        """
        assert body_schema_error("/a/steer", {"command_id": "id-1"}) is not None
        assert body_schema_error("/a/steer", {"command_id": "id-1", "instruction": ""}) is not None
        assert body_schema_error("/a/steer", {"command_id": "id-1", "instruction": "go"}) is None
        # `extra="forbid"`: the fix must be per-verb, not "add it everywhere".
        assert body_schema_error("/a/pause", {"command_id": "i", "instruction": "x"}) is not None

    @pytest.mark.parametrize(
        ("adapter", "path"),
        [
            ("activity", "/activity/invocations/msg-live/agent/{verb}"),
            ("orchestration", "/orchestration/runs/msg-live/{verb}"),
        ],
    )
    def test_the_stub_gateway_rejects_a_steer_missing_its_instruction(
        self, adapter: str, path: str
    ):
        """Drives the stub's request path, not the predicate it calls.

        The stub accepting a key-only body for every verb is the actual escape
        mechanism: it was more permissive than the gateway, so the harness could
        send `steer` a body the real deployment rejects and every test still
        passed. Pinning that requires putting a request through the stub — if the
        stub stops consulting the schema predicate, this authenticated owner steer
        falls through to the unsupported-verb 501 and these assertions fail.

        Both adapters, because a one-sided fix is how the two edges drift.
        """
        client = gateway_stub()

        def owner_post(body: dict) -> _mod.Observation:
            return _mod.Probe("https://gw", client).request(
                "POST", path.format(verb="steer"), role="owner", token=OWNER_TOKEN, json_body=body
            )

        assert owner_post({"command_id": "id-1"}).status == 400, adapter
        assert owner_post({"command_id": "id-1", "instruction": ""}).status == 400, adapter
        # The 400s above are the missing instruction and nothing else: the same
        # request carrying one reaches the authorization answer behind it.
        assert owner_post({"command_id": "id-1", "instruction": "go"}).status == 501, adapter

    @pytest.mark.parametrize(
        ("adapter", "path"),
        [
            ("activity", "/activity/invocations/msg-live/agent/{verb}"),
            ("orchestration", "/orchestration/runs/msg-live/{verb}"),
        ],
    )
    def test_the_stub_gateway_rejects_an_instruction_on_a_reason_taking_verb(
        self, adapter: str, path: str
    ):
        """The other half of `extra="forbid"`, also through the stub's path.

        Without this the stub would accept "add `instruction` everywhere", which
        is the wrong fix: on pause/resume/abort that field is an unknown one and
        the product answers 400.
        """
        client = gateway_stub()
        probe = _mod.Probe("https://gw", client)

        observation = probe.request(
            "POST",
            path.format(verb="pause"),
            role="owner",
            token=OWNER_TOKEN,
            json_body={"command_id": "id-1", "instruction": "go"},
        )

        assert observation.status == 400, adapter

    @pytest.mark.parametrize(
        ("override", "check_id"),
        [
            ({"malformed_status": 501}, "W1-05"),
            ({"oversize_status": 400}, "W1-05"),
            ({"overreach_status": 200}, "W1-05"),
        ],
    )
    def test_the_invalid_body_legs_still_expect_their_rejections(
        self, tmp_path: Path, override: dict, check_id: str
    ):
        """W1-05 covers all four verbs, and the fix must not have weakened it.

        Re-asserted here rather than left to the shared status table above so the
        relationship is explicit: these three legs are the ones a careless fix
        would have routed through the valid-body helper.
        """
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub(**override)
        )

        assert results[check_id].status == _mod.STATUS_FAILED

    def test_malformed_bodies_still_outrank_the_unsupported_verb_answer(
        self, tmp_path: Path
    ):
        """The reviewed validate-before-authorize decision, still pinned.

        A malformed body must answer 400 even for a verb that would otherwise
        answer 501. W1-05 asserts it; this confirms it still holds on a stub whose
        schema check now sits ahead of the authorization answers, i.e. that the
        stub models the ordering rather than accidentally inverting it.
        """
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        assert results["W1-05"].status == _mod.STATUS_PASSED

    def test_the_refusal_bodies_are_still_compared_for_steer(self, tmp_path: Path):
        """The enumeration oracle now applies to the verb that never reached it.

        Before the fix, steer's three refusals were all the same 400, so a
        distinguishable 404 on steer specifically could not have been caught.

        The divergence is planted on `steer` ALONE and the earlier verbs keep
        answering correctly, so the check has to walk past pause and resume to
        fail — and the failure message has to name steer. A global override would
        fail on `pause` (W1-02's first verb) and satisfy a steer-specific
        assertion without steer ever being probed.
        """
        client = gateway_stub(
            nonowner_body_by_verb={"steer": {"detail": "you do not own this run"}}
        )

        results = run_driver(tmp_path, config=live_config(tmp_path), client=client)

        assert results["W1-02"].status == _mod.STATUS_FAILED
        assert "enumerate" in results["W1-02"].message
        assert "/steer" in results["W1-02"].message, results["W1-02"].message
        # Reached by walking the ladder, not by tripping on the first verb: pause
        # and resume were probed and answered their indistinguishable 404s.
        answered = [(url, status) for url, status in client.answered if status == 404]
        assert any(url.endswith("/pause") for url, _ in answered)
        assert any(url.endswith("/steer") for url, _ in answered)
        # And the failing observation is recorded as evidence for the operator.
        evidence = json.dumps(results["W1-02"].to_evidence())
        assert "you do not own this run" in evidence

    def test_the_terminal_and_flag_off_legs_still_hold_for_their_verb(
        self, tmp_path: Path
    ):
        """W1-06's 410/404 and W1-08's 503 also send a command body."""
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        assert results["W1-06"].status == _mod.STATUS_PASSED
        assert results["W1-08"].status == _mod.STATUS_PASSED


class TestNoCredentialReachesEvidence:
    """Two independent mechanisms, tested separately."""

    def test_a_bearer_value_never_appears_in_the_report(self, tmp_path: Path):
        config = live_config(tmp_path)
        results = run_driver(tmp_path, config=config, client=gateway_stub())
        report = _mod.build_report(config, list(results.values()), cleanup_ok=True)

        serialised = json.dumps(report)

        for token in IDENTITY_ENV.values():
            assert token not in serialised

    def test_the_recorded_command_names_the_role_not_the_token(self, tmp_path: Path):
        """Redaction is the second line of defence, not the mechanism.

        The command is assembled with a placeholder, so the token is never in the
        structure at all — it cannot leak through a field added later that
        forgets to redact.
        """
        results = run_driver(
            tmp_path, config=live_config(tmp_path), client=gateway_stub()
        )

        commands = [obs.command for obs in results["W1-02"].observations]

        assert any("$<owner>" in command for command in commands)
        assert not any(OWNER_TOKEN in command for command in commands)

    def test_a_credential_in_the_config_file_is_a_config_error(self, tmp_path: Path):
        """The committed file itself would be the leak; redaction cannot fix that."""
        config = valid_config()
        config["owner_token"] = "should-not-be-here"

        with pytest.raises(_mod.EvalConfigError) as exc:
            _mod.load_config(write_config(tmp_path, config))

        assert "identity_env" in str(exc.value)


class TestCleanupIsBoundedAndAlwaysRuns:
    """§7: always run bounded cleanup; never purge a shared queue."""

    def test_it_deletes_only_the_named_pairs_and_confirms_absence(self):
        dynamodb = ddb_stub()
        config = {
            "invocation_table": "t",
            "cleanup_items": [{"event_id": "e1", "arrived_at": "a1"}],
        }

        outcome = _mod.run_cleanup(config, dynamodb)
        ok, notes = outcome.ok, outcome.notes

        assert ok is True
        dynamodb.delete_item.assert_called_once_with(
            TableName="t",
            Key={"event_id": {"S": "e1"}, "arrived_at": {"S": "a1"}},
            # ALL_OLD is part of the contract: without it a delete against a key
            # that never existed is indistinguishable from a real teardown.
            ReturnValues="ALL_OLD",
        )
        # A consistent read, because an eventually-consistent one can report an
        # item gone before it is.
        assert dynamodb.get_item.call_args.kwargs["ConsistentRead"] is True
        assert "confirms absence" in " ".join(notes)

    def test_a_delete_that_removed_nothing_is_not_reported_as_a_removal(self):
        """DeleteItem succeeds identically on a key that never existed.

        This is the failure a wrong `invocation_table`, a wrong `environment` or a
        stale key format produces, and every other field on the record — deleted,
        confirmed_absent, error — reads exactly as it does for a real teardown. I hit
        it for real: running the runbook's example config against the live dev
        account reported three rows "removed ... consistent read confirms absence"
        while deleting nothing, because its placeholder `msg-0000...` keys match no
        row in a table of 413k UUID-keyed items. Had those placeholders been real
        IDs, the same green output would have covered destroying production rows in
        a table with no point-in-time recovery.
        """
        dynamodb = ddb_stub(existed=False)
        config = {
            "invocation_table": "t",
            "cleanup_items": [{"event_id": "e1", "arrived_at": "a1"}],
        }

        outcome = _mod.run_cleanup(config, dynamodb)

        # Still ok: the row may legitimately have expired by TTL or been removed by
        # an earlier run. The evidence just must not claim this harness removed it.
        assert outcome.ok is True
        assert outcome.deletions[0].existed is False
        assert "was already absent" in " ".join(outcome.notes)
        assert "confirms absence" not in " ".join(outcome.notes)
        assert outcome.deletions[0].to_evidence()["existed"] is False

    def test_a_delete_that_removed_a_row_records_that_it_existed(self):
        outcome = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e1", "arrived_at": "a1"}]},
            ddb_stub(existed=True),
        )
        assert outcome.deletions[0].existed is True
        assert "confirms absence" in " ".join(outcome.notes)

    def test_a_refused_partial_key_records_no_existence_claim(self):
        """`existed` must be None, not False: nothing was asked of the table."""
        outcome = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e1"}]},
            ddb_stub(),
        )
        assert outcome.ok is False
        assert outcome.deletions[0].existed is None

    def test_it_never_scans_or_queries(self):
        """No scan, no prefix, no wildcard: an item it was not told about is
        unreachable by construction, not by care."""
        dynamodb = ddb_stub()

        _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e", "arrived_at": "a"}]},
            dynamodb,
        )

        dynamodb.scan.assert_not_called()
        dynamodb.query.assert_not_called()

    def test_a_partial_key_is_refused(self):
        """A delete keyed on event_id alone could match an unrelated item."""
        dynamodb = ddb_stub()

        outcome = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e"}]}, dynamodb
        )
        ok, notes = outcome.ok, outcome.notes

        assert ok is False
        dynamodb.delete_item.assert_not_called()
        assert "partial-key" in " ".join(notes)

    def test_a_surviving_item_is_a_cleanup_failure(self):
        dynamodb = ddb_stub(item={"event_id": {"S": "e"}})

        outcome = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e", "arrived_at": "a"}]},
            dynamodb,
        )
        ok, notes = outcome.ok, outcome.notes

        assert ok is False
        assert "still present" in " ".join(notes)

    def test_a_delete_error_is_a_cleanup_failure(self):
        dynamodb = ddb_stub()
        dynamodb.delete_item.side_effect = RuntimeError("AccessDenied")

        outcome = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e", "arrived_at": "a"}]},
            dynamodb,
        )
        ok, notes = outcome.ok, outcome.notes

        assert ok is False
        assert "AccessDenied" in " ".join(notes)

    def test_cleanup_runs_even_when_checks_fail(self, tmp_path: Path):
        """The failure path is exactly when a fixture is most likely to be left
        with a live listener — the state DP-INV-1 forbids."""
        config = live_config(tmp_path)
        path = write_config(tmp_path, config)
        dynamodb = ddb_stub()
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb,
        }[name]

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=gateway_stub(terminal_status=501)),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
        ):
            code = _mod.main(
                ["--wave", "1", "--config", str(path), "--evidence-dir", str(tmp_path / "ev")]
            )

        assert code == _mod.EXIT_CHECKS_FAILED
        dynamodb.delete_item.assert_called_once()

    def test_cleanup_failure_outranks_ten_passing_checks(self, tmp_path: Path):
        config = live_config(tmp_path)
        path = write_config(tmp_path, config)
        dynamodb = ddb_stub()
        dynamodb.delete_item.side_effect = RuntimeError("AccessDenied")
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb,
        }[name]

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=gateway_stub()),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
        ):
            code = _mod.main(
                ["--wave", "1", "--config", str(path), "--evidence-dir", str(tmp_path / "ev")]
            )

        assert code == _mod.EXIT_CLEANUP
        report = json.loads((tmp_path / "ev" / "result.json").read_text(encoding="utf-8"))
        assert report["passed"] == report["required"]
        assert report["cleanup_ok"] is False
        assert _mod.report_is_passing(report) is False


class TestThePublishedCommandAndGate:
    """The end-to-end contract: the issue's own smoke command and §7's `jq`.

    This class is the one that matters. The failure mode being avoided is a
    harness that satisfies its unit tests but not the operator's copy-pasted
    command — which is exactly the state this file was in when it had 101
    passing tests and an entry point that exited 2 on `--wave`.
    """

    @staticmethod
    def _run(tmp_path: Path, client=None, wave: int = 1, **config_overrides) -> tuple[int, dict]:
        """Drive the real CLI. `wave` defaults to 1 so existing callers keep meaning."""
        config = live_config(tmp_path, **config_overrides)
        path = write_config(tmp_path, config)
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": ddb_stub(),
        }[name]
        evidence = tmp_path / "evidence"

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=client or gateway_stub()),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
        ):
            code = _mod.main(
                [
                    "--wave",
                    str(wave),
                    "--config",
                    str(path),
                    "--evidence-dir",
                    str(evidence),
                ]
            )

        report = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        return code, report

    def test_the_documented_invocation_is_accepted(self, tmp_path: Path):
        """`--wave` and `--evidence-dir`: the names the issue and §7 actually pass."""
        args = _mod.parse_args(
            ["--wave", "1", "--config", "c.json", "--evidence-dir", "ev"]
        )

        assert args.wave == 1
        assert args.evidence_dir == Path("ev")

    def test_the_deprecated_output_dir_still_resolves(self):
        """An operator following an older note is redirected, not error'd."""
        args = _mod.parse_args(["--config", "c.json", "--output-dir", "old"])

        assert args.evidence_dir == Path("old")

    def test_the_published_command_exits_zero_on_a_good_fixture(self, tmp_path: Path):
        code, _ = self._run(tmp_path)

        assert code == _mod.EXIT_OK

    def test_the_section_7_jq_gate_holds(self, tmp_path: Path):
        """Every conjunct of the operator's gate, evaluated in Python.

        `report_is_passing` is the same predicate, so exit code and gate cannot
        disagree — an exit 0 the operator's `jq` then rejects is the worst of
        both worlds.
        """
        _, report = self._run(tmp_path)

        assert report["failed"] == 0
        assert report["skipped"] == 0
        assert report["not_run"] == 0
        assert report["passed"] == report["required"]
        assert report["cleanup_ok"] is True
        assert _mod.report_is_passing(report) is True

    def test_the_check_function_holds_for_every_id(self, tmp_path: Path):
        """#3967's `check()`: status == "passed" and (.evidence | length > 0)."""
        _, report = self._run(tmp_path)

        for check_id in _mod.EXPECTED_CHECK_IDS:
            entry = report["checks"][check_id]
            assert entry["status"] == "passed", check_id
            assert len(entry["evidence"]) > 0, check_id

    def test_the_evidence_file_is_named_result_json(self, tmp_path: Path):
        self._run(tmp_path)

        assert (tmp_path / "evidence" / "result.json").is_file()

    def test_a_single_broken_behaviour_makes_the_gate_reject(self, tmp_path: Path):
        """The gate must be sensitive, not just satisfiable."""
        code, report = self._run(tmp_path, client=gateway_stub(authorized_status=200))

        assert code == _mod.EXIT_CHECKS_FAILED
        assert _mod.report_is_passing(report) is False

    def test_a_not_run_check_cannot_pass_the_gate_end_to_end(self, tmp_path: Path):
        """The mutation that matters: unreachable must not read as verified."""
        config = live_config(tmp_path)
        artifacts = dict(config["artifacts"])
        del artifacts["journal_tests"]

        code, report = self._run(tmp_path, artifacts=artifacts)

        assert code == _mod.EXIT_CHECKS_FAILED
        assert report["not_run"] == 1
        assert report["checks"]["W1-09"]["status"] == _mod.STATUS_NOT_RUN
        assert _mod.report_is_passing(report) is False

    def test_the_report_carries_wave_and_acceptance_ids(self, tmp_path: Path):
        _, report = self._run(tmp_path)

        assert report["wave"] == 1
        assert report["evaluation"] == "3967"
        assert report["checks"]["W1-02"]["acceptance_ids"] == ["AC-S1", "AC-S2"]
        assert report["checks"]["W1-08"]["acceptance_ids"] == ["AC-F1", "AC-F2"]

    def test_the_report_states_that_no_verb_is_supported(self, tmp_path: Path):
        """S1's central claim, stated in the evidence rather than inferred."""
        _, report = self._run(tmp_path)

        assert report["supported_verbs"] == []


class TestThePublishedWaveTwoCommand:
    """`--wave 2` as the issue publishes it, driven through the real `main`.

    This is the loop-closure the unit tests cannot make. Before this revision
    `SUPPORTED_WAVES` was `(1,)`, so the smoke command this story ships —

        python3 platform/scripts/agent-control-eval.py --wave 2 \\
            --config "$CONTROL_EVAL_CONFIG" --evidence-dir "$CONTROL_EVIDENCE_DIR"

    — reached the wave guard and returned EXIT_CONFIG *before loading a config*,
    writing no evidence at all. An operator running the documented command got a
    refusal that named the wave, which is honest but proves nothing about W2-02.

    What must be true now is narrower than "wave 2 works": the refusal has to be
    replaced by a *run* that reports the delivered check as passed, the nine
    outstanding ones as not_run, and still exits nonzero. All three at once. Any
    two of them is a familiar failure — exit 0 on an incomplete wave, or a
    nonzero with no evidence to read.
    """

    _run = staticmethod(TestThePublishedCommandAndGate._run)

    def test_the_wave_two_command_runs_instead_of_refusing(self, tmp_path: Path):
        """It reaches the checks: evidence exists, and the code is not EXIT_CONFIG."""
        code, report = self._run(tmp_path, wave=2)

        assert code != _mod.EXIT_CONFIG
        assert (tmp_path / "evidence" / "result.json").is_file()
        assert report["wave"] == 2

    def test_the_wave_two_command_exits_four_not_zero(self, tmp_path: Path):
        """Nonzero because nine checks are outstanding — not because it broke.

        `failed == 0` is the half that distinguishes "this wave is unfinished"
        from "this wave regressed", and it is why the exit code alone is not
        enough of an assertion here.

        The row cleanup is what is asserted clean, not the whole fixture. This
        fixture declares no `resource_teardown` and W2-10 is outstanding, so the
        aggregate `cleanup_ok` is correctly false: nothing in this run established
        that the fixture's resources are gone. That is a reporting fact about an
        unfinished wave and not a cleanup failure, which is why the exit code stays
        EXIT_CHECKS_FAILED rather than becoming EXIT_CLEANUP.
        """
        code, report = self._run(tmp_path, wave=2)

        assert code == _mod.EXIT_CHECKS_FAILED
        assert report["cleanup"]["ok"] is True
        assert report["fixture_cleanup"]["rows_ok"] is True
        assert report["cleanup_ok"] is False
        assert report["failed"] == 0
        assert report["passed"] == 1
        assert report["not_run"] == 9
        assert report["required"] == 10
        assert _mod.report_is_passing(report) is False

    def test_the_delivered_check_is_readable_as_passed_with_evidence(
        self, tmp_path: Path
    ):
        """What the operator actually looks at after the nonzero exit."""
        _, report = self._run(tmp_path, wave=2)
        entry = report["checks"]["W2-02"]

        assert entry["status"] == _mod.STATUS_PASSED, entry
        assert entry["acceptance_ids"] == ["AC-T7"]
        assert len(entry["evidence"]) > 0

    def test_every_outstanding_check_names_its_owning_story_in_the_report(
        self, tmp_path: Path
    ):
        """The report has to be actionable, not just correct.

        Redaction runs over the messages on the way out, so "the owner survives
        into the written evidence" is a separate claim from the in-process one.
        """
        _, report = self._run(tmp_path, wave=2)

        outstanding = {
            cid: entry
            for cid, entry in report["checks"].items()
            if cid != "W2-02"
        }
        assert len(outstanding) == 9
        for check_id, entry in outstanding.items():
            assert entry["status"] == _mod.STATUS_NOT_RUN, check_id
            if check_id in _mod.PENDING_CHECK_OWNERS:
                assert "#" in entry["message"], (check_id, entry["message"])
            else:
                assert "prerequisite missing" in entry["message"]

    def test_a_broken_contract_artifact_fails_the_delivered_check_end_to_end(
        self, tmp_path: Path
    ):
        """Sensitivity: W2-02 must be capable of failing through the CLI.

        Both an unfinished wave and a broken one exit 4, so the exit code cannot
        tell them apart — the report must, and this is the test that proves the
        distinction is real rather than a story about the code.
        """
        payloads = artifact_payloads()
        payloads["neutral_contract"] = neutral_contract_payload(
            stale_events_rejected=False
        )

        code, report = self._run(tmp_path, wave=2, artifact_payloads=payloads)

        assert code == _mod.EXIT_CHECKS_FAILED
        assert report["checks"]["W2-02"]["status"] == _mod.STATUS_FAILED
        assert report["failed"] == 1
        assert report["passed"] == 0
        assert "stale_events_rejected" in report["checks"]["W2-02"]["message"]

    def test_wave_one_is_unchanged_by_wave_two_existing(self, tmp_path: Path):
        """The regression this revision could plausibly cause, asserted directly."""
        code, report = self._run(tmp_path, wave=1)

        assert code == _mod.EXIT_OK
        assert report["wave"] == 1
        assert report["evaluation"] == "3967"
        assert report["required"] == report["passed"]

    def test_an_undelivered_wave_is_still_refused(self, tmp_path: Path):
        """The guard was narrowed, not removed.

        The first wave with no manifest must still refuse *before* loading a config
        and must write no evidence — an empty report for an unwritten wave would
        read as "nothing to prove here".

        Derived from `SUPPORTED_WAVES` rather than named, for the reason the
        matching guard test in `TestEntryPointFailsClosed` gives: this was wave 3
        until #3965 registered it, and hardcoding the next number only postpones
        the same repair.
        """
        config = write_config(tmp_path, live_config(tmp_path))
        evidence = tmp_path / "evidence"
        undelivered = max(_mod.SUPPORTED_WAVES) + 1

        code = _mod.main(
            [
                "--wave",
                str(undelivered),
                "--config",
                str(config),
                "--evidence-dir",
                str(evidence),
            ]
        )

        assert code == _mod.EXIT_CONFIG
        assert not (evidence / "result.json").exists()


class TestTheHarnessesRuntimeDependencies:
    """The dependencies `main` needs, asserted by name.

    Learned from CI: the harness job installed no `httpx`, so the eleven tests
    that drive the published end-to-end command could not run. `main` catches the
    import failure and returns EXIT_PRECONDITION — correct behaviour, and exactly
    why it was hard to read: the visible symptom was a coverage drop to 83% and
    one assertion reporting `3 == 4`, neither of which says "a dependency is
    missing". These tests say it.
    """

    def test_httpx_is_available(self):
        """Needed to build the probe client, so it is a runtime dependency."""
        import httpx  # noqa: F401

    def test_boto3_is_available(self):
        """Needed for the STS account check and the DynamoDB key-schema check."""
        import boto3  # noqa: F401

    def test_the_runbook_lists_the_dependencies_an_operator_must_install(self):
        """The operator installs these by hand; the doc is where they read them."""
        text = TestTheDocumentedFixtureConfig.DOC.read_text(encoding="utf-8")

        assert "httpx" in text
        assert "boto3" in text


class TestTheDocumentedFixtureConfig:
    """The example in docs/runbooks/agent-control-evaluation.md must be real.

    revival-design §7 names "documented fixture config" as an S1 deliverable, and
    the harness cannot run in CI — so the doc is the only thing an operator has
    before their first run. A stale example is worse than none: it produces a
    config that validates, runs, and reports `not_run` on checks the operator
    believes they configured.

    So the doc's example is parsed out of the markdown and checked against the
    harness's own constants. Adding a config key without documenting it, or
    documenting one the harness does not read, fails here.
    """

    DOC = (
        REPO_ROOT
        / "docs"
        / "runbooks"
        / "agent-control-evaluation.md"
    )

    # Read by the driver but intentionally absent from the example: `artifacts`
    # paths are per-fixture, and these have documented defaults.
    _OPTIONAL_IN_EXAMPLE: frozenset[str] = frozenset()

    @classmethod
    def example(cls) -> dict:
        text = cls.DOC.read_text(encoding="utf-8")
        start = text.index("<!-- EXAMPLE-CONFIG-BEGIN -->")
        end = text.index("<!-- EXAMPLE-CONFIG-END -->")
        block = text[start:end]
        body = block.split("```json", 1)[1].rsplit("```", 1)[0]
        return json.loads(body)

    def test_the_runbook_exists(self):
        assert self.DOC.is_file()

    def test_the_runbook_documents_the_neutral_contract_artifact(self):
        """W2-02's artifact is the one whose evidence comes from a test run.

        Its shape is not guessable, so an operator who cannot see it documented
        will omit it and get `not_run` on the only wave-2 check that works.
        """
        text = self.DOC.read_text(encoding="utf-8")

        assert "neutral_contract" in text
        for key in _mod.REQUIRED_ARTIFACT_KEYS["neutral_contract"]:
            assert key in text, key

    def test_the_runbook_explains_why_a_wave_two_run_can_exit_nonzero(self):
        """An operator must not read a nonzero `--wave 2` as the harness being broken.

        This test used to pin the heading "Wave 2 is incomplete on purpose", which
        was true while W2-01 and W2-10 had no predicate. #5825 implemented them, so
        the prose had to change — but the operator's need did not: a nonzero run
        still has to be explained, and now the explanation is different. It is one
        of missing evidence, a contradicted deployment, or incomplete cleanup, all
        three of which are the operator's to act on rather than a gap in the harness.

        Pinned on the three causes rather than on a heading string, because the
        heading is prose and the causes are the contract.
        """
        text = self.DOC.read_text(encoding="utf-8")

        assert "Wave 2 is fully implemented" in text
        # The three reasons a complete wave-2 run can still exit nonzero.
        assert "required artifact is missing" in text
        assert "disagrees with the contract" in text
        assert "cleanup did not complete" in text
        # And that reaching ten of ten is now possible at all, which is the state
        # #5825 made reachable and the previous prose denied.
        assert "complete wave-2 report is therefore now reachable" in text

    def test_the_runbook_documents_capturing_the_wave_two_only_artifacts(self):
        """An undocumented artifact is one the operator omits.

        The consequence is a `not_run` they cannot explain — which looks exactly
        like the harness being broken. Each new artifact needs its own capture
        instructions, and every required key has to appear, or a half-filled file
        produces a `failed` whose missing key the operator has no way to source.

        There are three, not two: W2-10's single `cleanup_security_recheck` became
        `security_capture` (read before teardown) and `teardown_verification` (read
        after), because one artifact cannot hold both a live observation and the
        absence of the thing observed. The split is the operator-visible half of the
        ordering fix, so the doc has to carry both halves separately — an operator
        who writes one file for both would be back to claiming the live capability
        surface of a torn-down fixture.
        """
        text = self.DOC.read_text(encoding="utf-8")

        for artifact in ("wave2_preflight", "security_capture", "teardown_verification"):
            assert f"### `{artifact}`" in text, artifact
            for key in _mod.REQUIRED_ARTIFACT_KEYS[artifact]:
                assert key in text, (artifact, key)
        # The superseded single artifact must not linger as a fourth capture
        # section: a doc offering both shapes lets the operator pick the broken one.
        assert "### `cleanup_security_recheck`" not in text

    @pytest.mark.parametrize(
        "artifact",
        [
            "steering_delivery",
            "steering_queue",
            "steering_trust_boundary",
            "steering_input_stream",
            "steering_retry",
        ],
    )
    def test_the_runbook_documents_capturing_the_wave_three_artifacts(self, artifact: str):
        """Same requirement as wave 2's, and for the same reason.

        None of these five shapes is guessable, and two of them describe an
        experiment the operator has to run by hand (`steering_input_stream` needs
        the real SDK; `steering_queue` needs delivery held while eleven commands are
        submitted). An undocumented artifact is one they omit, and the resulting
        `not_run` looks exactly like the harness being broken.

        Parametrized per artifact rather than looped, so a missing section names
        which one instead of failing on whichever came first.
        """
        text = self.DOC.read_text(encoding="utf-8")

        assert f"### `{artifact}`" in text
        for key in _mod.REQUIRED_ARTIFACT_KEYS[artifact]:
            assert key in text, (artifact, key)

    def test_the_runbook_states_the_marker_bound_is_measured_from_handoff(self):
        """The one instruction an operator can follow correctly and still be wrong.

        `handoff_at` and `accepted_at` are both in the artifact, both plausible
        readings of "within 35 seconds", and only one is the rule. An operator who
        measures from submission records a correct run as a late one — so the doc has
        to say which, not merely list both fields.
        """
        text = " ".join(self.DOC.read_text(encoding="utf-8").split())

        assert "measured from **`handoff_at`**, never from `accepted_at`" in text
        assert str(_mod.STEER_MARKER_MAX_LATENCY_SECONDS) in text

    def test_the_runbook_says_a_wave_three_run_cannot_yet_exit_zero(self):
        """Otherwise the nonzero reads as a fixture problem the operator must fix.

        Seven of twelve checks have no predicate, so `--wave 3` is nonzero on a
        perfect fixture. That is the design, and an operator who does not know it
        will go looking for the defect in their own environment.
        """
        text = " ".join(self.DOC.read_text(encoding="utf-8").split())

        assert "cannot exit 0 yet, and that is deliberate" in text
        assert "S4 #3963" in text

    def test_the_documented_cleanup_record_matches_what_the_harness_writes(self):
        """The `cleanup` block is the evidence a reviewer reads instead of a verdict.

        Its field names are documented, so they can drift from the dataclass that
        produces them — and a reviewer following a stale doc would look for a key
        that is not there and conclude the record was incomplete. Pinned against
        `RowDeletion.to_evidence()` itself rather than a literal list.
        """
        text = self.DOC.read_text(encoding="utf-8")
        record = _mod.CleanupOutcome(
            ok=True,
            notes=[],
            deletions=[
                _mod.RowDeletion(
                    event_id="msg-0000000000000001",
                    arrived_at="2026-09-12T10:00:00Z",
                    both_keys_present=True,
                    deleted=True,
                    confirmed_absent=True,
                )
            ],
            declared_items=3,
        ).to_evidence()

        for key in record:
            assert key in text, key
        for key in record["deletions"][0]:
            assert key in text, key

    def test_the_runbook_explains_the_capture_teardown_verify_ordering(self):
        """The ordering is the defect #5825 fixed, and it is operator-visible.

        This test previously required the doc to say "keep the `live_run_id` row
        readable" past teardown. That instruction was the defect in prose form: it
        asked the operator to leave a control-enabled fixture row alive so a check
        could read it, which is the opposite of what W2-10 exists to establish, and
        an operator who did the right thing instead got `not_run`. The requirement
        is inverted here deliberately — the doc must NOT ask for a surviving row.

        What the operator does need told: reads happen first, teardown second,
        absence-verification third; and the substitution they would otherwise reach
        for — answering a post-teardown question with an asserted boolean, or with a
        removal the creation ledger never mentioned — does not work.
        """
        # Whitespace-normalized: these are sentences, and markdown rewraps them at
        # the column limit, so a line break landing mid-phrase is not a contract
        # change and must not read as one.
        text = " ".join(self.DOC.read_text(encoding="utf-8").split())

        assert "W2-10 runs after cleanup" in text
        assert "captures, then tears down, then verifies" in text
        # The retired instruction must be gone, not merely supplemented: a doc that
        # still tells the operator to preserve the row teaches the old workaround.
        assert "keep the `live_run_id` row readable" not in text.lower()
        # A removed resource is not an answer, and a not-found is not the pass.
        assert "cannot answer a post-teardown request" in text
        assert "never accepted" in text
        # The substitution the issue forbids, stated where the operator would
        # otherwise reach for it.
        assert "would simply be ignored" in text

    def test_the_example_is_valid_json_and_passes_every_config_guard(
        self, tmp_path: Path
    ):
        """The operator's first action is to copy this block. It has to work."""
        loaded = _mod.load_config(write_config(tmp_path, self.example()))

        assert loaded["fixture_isolated"] is True
        assert loaded["account_id"] == ACCOUNT

    def test_the_example_names_the_live_wave_1_target(self):
        """#3967's prerequisites: 879318057152 / dev / embark1."""
        example = self.example()

        assert example["account_id"] == ACCOUNT
        assert "embark1" in example["environment"]

    def test_the_example_carries_no_credential(self):
        """Illustrative or not, a doc is where a real token gets pasted from."""
        example = self.example()

        for key in example:
            assert not any(
                pattern in key.lower()
                for pattern in ("token", "secret", "password", "credential")
            ), key

    def test_identity_env_names_variables_not_tokens(self):
        """The indirection IS the mechanism, so the example must model it."""
        identity_env = self.example()["identity_env"]

        assert set(identity_env) == set(_mod.IDENTITY_ROLES)
        for role, var in identity_env.items():
            assert var == var.upper(), role
            assert " " not in var, role

    def test_the_example_declares_every_artifact_the_checks_read(self):
        """A missing entry is a check that reports not_run on a real fixture."""
        assert set(self.example()["artifacts"]) == set(_mod.REQUIRED_ARTIFACT_KEYS)

    def test_the_example_covers_every_config_key_the_harness_reads(self):
        """Both directions.

        Undocumented key → an operator cannot know to set it, and the check
        silently reports not_run. Documented key the harness ignores → the
        operator sets it and believes something is covered that is not.
        """
        source = _SCRIPT_PATH.read_text(encoding="utf-8")
        read_keys = set(re.findall(r'config\.get\("([^"]+)"', source))
        read_keys |= set(re.findall(r'config\["([^"]+)"\]', source))
        read_keys |= set(re.findall(r'_require\("([^"]+)"\)', source))
        read_keys |= set(_mod.REQUIRED_CONFIG_FIELDS)
        # W1-01 requires its four fields in a loop rather than one call each, so
        # the literal-argument patterns above do not see them.
        for group in re.findall(r'for key in \(([^)]*)\):\n\s+self\._require\(key\)', source):
            read_keys |= set(re.findall(r'"([^"]+)"', group))
        # Read off artifact payloads and cleanup items, not the top-level config.
        read_keys -= {"probe_connect_result", "event_id"}

        documented = set(self.example())

        assert read_keys - documented == set(), "undocumented config keys"
        assert documented - read_keys == set(), "documented keys the harness ignores"

    def test_cleanup_items_in_the_example_carry_both_key_halves(self):
        """A partial key is refused by the harness; the example must not teach it."""
        for item in self.example()["cleanup_items"]:
            assert set(item) == {"event_id", "arrived_at"}

    def test_the_examples_cleanup_covers_the_rows_it_creates(self):
        """A fixture row left behind is a fixture left in the DP-INV-1 state."""
        example = self.example()
        cleaned = {item["event_id"] for item in example["cleanup_items"]}

        assert example["live_run_id"] in cleaned
        assert example["terminal_run_id"] in cleaned
        # ...but NOT the unknown ID: it names a row that must not exist, so
        # "cleaning" it would be deleting something the harness never created.
        assert example["unknown_run_id"] not in cleaned

    def test_the_documented_check_table_matches_the_manifest(self):
        """The runbook's ID table is the manifest restated for a human reader."""
        text = self.DOC.read_text(encoding="utf-8")

        for spec in _mod.WAVE1_CHECKS:
            row = f"| {spec.check_id} | {', '.join(spec.acceptance_ids)} |"
            assert row in text, spec.check_id

    def test_the_documented_exit_codes_are_the_real_ones(self):
        text = self.DOC.read_text(encoding="utf-8")

        for code in (
            _mod.EXIT_OK,
            _mod.EXIT_CONFIG,
            _mod.EXIT_PRECONDITION,
            _mod.EXIT_CHECKS_FAILED,
            _mod.EXIT_CLEANUP,
        ):
            assert f"| {code} |" in text

    def test_the_documented_default_evidence_dir_is_the_real_one(self):
        """It is documented as gitignored, so the two must be the same path."""
        default = _mod.parse_args(["--config", "c.json"]).evidence_dir
        text = self.DOC.read_text(encoding="utf-8")

        assert str(default) in text
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "test-results/" in gitignore

    def test_the_runbook_is_in_the_index(self):
        """An unindexed runbook is one nobody finds during an evaluation."""
        index = (self.DOC.parent / "README.md").read_text(encoding="utf-8")

        assert "agent-control-evaluation.md" in index


@pytest.mark.parametrize("implemented", [[], ["pause", "resume"], ["abort", "pause", "resume"], ["abort", "pause", "resume", "steer"]])
def test_w2_02_accepts_each_implemented_stage(tmp_path, implemented):
    caps = {verb: verb in implemented for verb in _mod.CONTROL_VERBS}
    result = run_w2_02(
        tmp_path,
        contract=neutral_contract_payload(implemented_verbs=implemented),
        client=gateway_stub(capabilities=caps),
    )
    assert result.status == _mod.STATUS_PASSED


@pytest.mark.parametrize("implemented", [["abort"], ["steer"], ["pause", "resume", "steer"], ["pause", "resume", "unknown"], ["pause", "resume", "resume"], [[]], "pause", None])
def test_w2_02_rejects_invalid_stage_declaration(tmp_path, implemented):
    result = run_w2_02(tmp_path, contract=neutral_contract_payload(implemented_verbs=implemented))
    assert result.status == _mod.STATUS_FAILED


@pytest.mark.parametrize("implemented", [["abort", "pause", "resume"], ["abort", "pause", "resume", "steer"]])
def test_w2_02_later_stage_still_requires_matching_live_capabilities(tmp_path, implemented):
    result = run_w2_02(
        tmp_path, contract=neutral_contract_payload(implemented_verbs=implemented),
        client=gateway_stub(capabilities={verb: verb in {"pause", "resume"} for verb in _mod.CONTROL_VERBS}),
    )
    assert result.status == _mod.STATUS_FAILED
    assert "disagree" in result.message


@pytest.mark.parametrize("second", [None, [], "echo"])
def test_w2_02_rejects_non_object_second_adapter(tmp_path, second):
    result = run_w2_02(tmp_path, contract=neutral_contract_payload(second_adapter=second))
    assert result.status == _mod.STATUS_FAILED


ABORTED_RUN_ID = "msg-aborted-001"

# The run the native-interruption experiment was performed on. A separate row from
# the aborted one on purpose: the experiment's whole point is that this run was cut
# off by the provider and did NOT become an aborted run.
NATIVE_INTERRUPT_RUN_ID = "msg-native-interrupt-001"

def wave2_artifact_payloads() -> dict:
    """A complete, passing artifact set for the wave-2 checks S5 owns (#3964).

    Kept separate from `artifact_payloads()` so a wave-1 test cannot be perturbed
    by a wave-2 field, and so the wave-1 driver tests keep asserting exactly ten
    results with exactly the wave-1 artifacts they always did.
    """
    return {
        "harness_neutrality": {
            # Two differently named adapters whose normalized accounting is
            # identical — the point of the harness-neutral contract.
            "adapter_a": {"aborted": 1, "completed": 2, "failed": 0},
            "adapter_b": {"aborted": 1, "completed": 2, "failed": 0},
            # A native interrupt that never got a confirmed ADP abort finalization.
            # It must NOT have become an aborted run — and the outcome has to be one
            # the writer could actually have produced, recorded with the run it was
            # observed on and how it was read back. A bare status (or an empty field)
            # used to pass here, which is the false green #5825 fixes.
            "native_interrupt_status": {
                "status": "failed",
                "run_id": NATIVE_INTERRUPT_RUN_ID,
                "observed_by": "GET /me/agent-invocations/{run_id} after provider interrupt",
            },
            "shared_code_imports_sdk": False,
        },
        "aborted_counters": {
            "today_before": {"total": 10, "completed": 6, "failed": 2, "active": 2, "aborted": 0},
            "today_after": {"total": 12, "completed": 6, "failed": 2, "active": 2, "aborted": 2},
            "seeded_aborted": 2,
            # Exactly four categories present, so the equality holds.
            "four_category_dataset": {
                "total": 10,
                "completed": 5,
                "failed": 2,
                "active": 1,
                "aborted": 2,
            },
            # Contains blocked/skipped/budget_stopped rows too, so `total` exceeds
            # the four buckets — and that inequality is the assertion.
            "mixed_dataset": {
                "total": 12,
                "completed": 4,
                "failed": 2,
                "active": 1,
                "aborted": 2,
            },
            "mixed_expected": {"completed": 4, "failed": 2, "active": 1},
            "daily_deltas": {"aborted": 2, "completed": 0, "failed": 0},
            "persona_deltas": {"aborted": 2, "completed": 0, "failed": 0},
        },
        "vocabulary_parity": {
            "writer_digest_deployed": True,
            "gateway_digest_deployed": True,
            "writer_allowed_statuses": [
                "in_progress",
                "complete",
                "failed",
                "skipped",
                "budget_stopped",
                "aborted",
            ],
            "gateway_terminal_statuses": [
                "complete",
                "failed",
                "rejected",
                "rate_limited",
                "no_op",
                "blocked",
                "skipped",
                "budget_stopped",
                "aborted",
            ],
            "unknown_status_rejected": True,
            "unknown_status_reached_table": False,
            "suites": {
                "tests/activity/test_status_aborted.py": "passed",
                "tests/test_status_vocabulary.py": "passed",
                "src/__tests__/utils/status.test.ts": "passed",
                "src/__tests__/components/InvocationChain.test.tsx": "passed",
            },
        },
        "stats_schema_keys": {
            # Exported from the backend Pydantic models, per the issue's "export
            # fixture JSON before comparing keys, never jq a TypeScript source file".
            "levels": {
                "response": ["window_days", "active_runs", "today", "daily", "by_persona", "recent_failures", "top_repos", "spend"],
                "active_runs": ["invocation_id", "invoked_at", "persona", "repo", "topic"],
                "recent_failures": ["invocation_id", "invoked_at", "persona", "repo", "topic", "error_message"],
                "top_repos": ["repo", "total"],
                "spend": ["total_cost_usd", "total_tokens", "total_calls"],
                "today": ["total", "completed", "failed", "active", "aborted"],
                "daily": ["date", "total", "completed", "failed", "aborted"],
                "by_persona": ["persona", "total", "completed", "failed", "aborted"],
            }
        },
    }


def pause_artifact_payloads() -> dict:
    """A complete, passing artifact set for the three checks S2 (#3961) owns.

    Kept in its own helper for the same reason `wave2_artifact_payloads` is kept
    apart from `artifact_payloads`: a pause field must not be able to perturb S5's
    aborted-run tests, and each `test_a_*_fails` below bends exactly one key of
    this baseline so a failure names one defect.

    These describe a *correct deployment*, not a transcript of the recorded
    experiment. `data/experiments/3961-pause-live-sdk-run{1,2}.json` is narrower
    than the predicate contract — it carries no `spill_hooks_composed`,
    `tool_coverage` or `degraded`, records `task_output_bytes: 811` where AC-P1
    requires 0, and leaves `held_tools_admitted_after_resume` null. That gap is
    real and belongs to the live evaluation (#3968), which is what has to produce
    an artifact meeting every key; it is not something this fixture can close.

    Also overrides `neutral_contract` so the wave-2 fixture is self-consistent.
    W2-02 cross-checks the tested build's `implemented_verbs` against the deployed
    capability map and rejects a disagreement — it already allows `[]` (S3) or
    `['pause', 'resume']` (S2). Since this fixture advertises pause on `/state` for
    W2-03, the contract artifact has to say so too; leaving it at S3's `[]` makes
    W2-02 fail with "deployed capabilities disagree with the tested build's
    implemented_verbs", which is the cross-check working, not a fixture nuisance.
    """
    return {
        "neutral_contract": neutral_contract_payload(
            implemented_verbs=["pause", "resume"]
        ),
        "pause_boundary": {
            "adapter_id": _mod.CLAUDE_ADAPTER_ID,
            "sdk_version": _mod.EXPECTED_CLAUDE_SDK_VERSION,
            "permission_mode": "bypassPermissions",
            "spill_hooks_composed": True,
            "requested": {"admission_closed": True},
            # The four zero-counters are the whole of AC-P1, measured from outside
            # the agent over a nonzero interval.
            "held_interval": {
                "duration_ms": 5000,
                "new_admissions": 0,
                "fixture_writes": 0,
                "fixture_service_calls": 0,
                "task_output_bytes": 0,
                "observed_by": "fixture",
            },
            "tool_coverage": {
                "long_running_bash": True,
                "delegated_task": True,
                "background_task": True,
            },
            "confirmed": {"state": "paused", "active_tool_count": 0},
            # Both degradation paths exercised, neither reporting `paused`.
            "degraded": {
                "untracked_activity": {
                    "state": "pause_requested",
                    "reason": "background task still tracked as in flight",
                },
                "hook_timeout": {
                    "state": "running",
                    "reason": "pre-tool barrier timed out; admission reopened",
                },
            },
        },
        "pause_resume": {
            "released_count": 1,
            "session_id_before": "2e9595a7-5a81-46aa-9f3a-3fac9d88e93e",
            "session_id_after": "2e9595a7-5a81-46aa-9f3a-3fac9d88e93e",
            "attempt_id_before": "attempt-1",
            "attempt_id_after": "attempt-1",
            "interrupt_called": False,
            "initial_prompt_replayed": False,
            "prior_history_preserved": True,
            "task_completed": True,
            "held_tools_admitted_after_resume": 1,
            "races": {
                "resume_before_pause": {"serialized": True, "errored": False},
                "repeated_resume": {"serialized": True, "errored": False},
            },
        },
        "pause_expiry": {
            "auto_resumed": True,
            "annotation_count": 1,
            "extra_assistant_turn": False,
            "neutral_annotation": True,
            # The defect found in review of this story: released without ever
            # reporting a confirmation or a failure.
            "resolved_before_release": True,
            "pod_killed": False,
            "idle_retry_fired": False,
            "exit_watchdog_fired": False,
            "heartbeats_during_pause": 3,
            "paused_distinguishable_from_stalled": True,
            "spill_output_preserved": True,
            "held_hook_timeout": {
                "exercised": True,
                "state": "running",
                "reason": "hook bound lapsed before resume; admission reopened",
                # The bound must exceed the budget it is holding, taken from the
                # shipped adapter's own `preToolUseTimeoutSeconds`.
                "hook_timeout_seconds": 1860,
                "pause_budget_seconds": 1800,
            },
            "deadline_clamp": {
                "granted_ms": 1_800_000,
                "remaining_ms": 2_100_000,
                "finalization_margin_ms": 120_000,
                "nonpositive_budget_rejected": True,
            },
            "cancellation": {
                "held_work_admitted": False,
                "held_work_denied": True,
                "annotation_emitted": False,
            },
        },
    }


# The three synthetic rows a wave-2 fixture seeds, and the revisions involved.
# Full 40-character SHAs because that is what W2-01 requires: a short SHA or a
# branch name names whatever a ref happened to point at, and the point of the field
# is to remove that ambiguity.
#
# Four DISTINCT revisions, and the distinctness is the fixture's whole shape. It
# models the normal, correct topology root's review named: the deployed build is
# NEWER than the merge commits it contains. An earlier fixture used one revision
# for everything, which made equality and containment indistinguishable — so a check
# that demanded equality passed the test suite and then failed every correct
# deployment, because a real deployment is never equal to one story's merge commit.
WAVE1_ACCEPTED_REVISION = "b" * 40  # the oldest: wave 1's accepted build
WAVE2_REVISION = "a" * 40  # the wave-2 stories' merge commit
DEPLOYED_WORKER_REVISION = "c" * 40  # what is RUNNING, newer than both
DEPLOYED_GATEWAY_REVISION = "d" * 40  # ships from its own workflow, so its own SHA
WORKER_DIGEST = "sha256:" + "1" * 64
GATEWAY_DIGEST = "sha256:" + "2" * 64
# The verbs this build does not implement. Must agree with `wave2_gateway_stub`'s
# default capability map, because W2-10 compares the recorded claim against the
# harness's own pre-teardown live reads, and a stub that disagreed with the artifact
# would make the passing fixture fail for a reason no test intended.
UNSUPPORTED_VERBS = ("steer", "abort")


class CommitGraph:
    """A real commit graph the harness's git queries are answered from.

    The cluster-free analogue of `SharedRowStore` and `FixtureResources`, and here for
    the same reason: W2-01 no longer reads `is_ancestor` out of the artifact, it asks
    git. A fake that returned "yes" to every query would put the tests back where the
    review found them — asserting against an answer the test itself supplied. So this
    models the ONE fact that matters: which commits exist, and which reach which.

    The shape is the normal correct topology. Wave 1's accepted build is the oldest;
    the wave-2 stories merge on top of it; the two deployed components build from two
    later commits on separate branches. Every invented SHA is simply absent, which is
    what makes "internally consistent but invented" fail here rather than pass.
    """

    def __init__(
        self,
        parents: dict[str, tuple[str, ...]] | None = None,
        *,
        fail_with: int | None = None,
        raises: Exception | None = None,
    ):
        # The two ways git can decline to answer, rather than answer: an unexpected
        # exit status (a broken or shallow checkout) and not being runnable at all.
        # Held here rather than passed to `runner()` so a test can hand a broken graph
        # anywhere a working one goes.
        self._fail_with = fail_with
        self._raises = raises
        self.parents = dict(
            parents
            if parents is not None
            else {
                WAVE1_ACCEPTED_REVISION: (),
                WAVE2_REVISION: (WAVE1_ACCEPTED_REVISION,),
                DEPLOYED_WORKER_REVISION: (WAVE2_REVISION,),
                DEPLOYED_GATEWAY_REVISION: (WAVE2_REVISION,),
            }
        )
        self.queries: list[list[str]] = []

    def reaches(self, ancestor: str, descendant: str) -> bool:
        """Whether `descendant` reaches `ancestor` by walking parents.

        Reflexive, as `git merge-base --is-ancestor` is: a commit contains itself.
        """
        seen, stack = set(), [descendant]
        while stack:
            current = stack.pop()
            if current == ancestor:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.parents.get(current, ()))
        return False

    def runner(self):
        """A `git_runner` answering `cat-file -e` and `merge-base --is-ancestor`."""

        def run(argv):
            self.queries.append(list(argv))
            if self._raises is not None:
                raise self._raises
            # `fail_with` breaks the ancestry query only, leaving `cat-file` to succeed.
            # That is the case worth covering: the commits exist, so the harness gets
            # past the existence check and then git fails to answer — which must not be
            # read as "not an ancestor". Breaking cat-file too would land on the
            # absent-commit path instead and cover it twice.
            if self._fail_with is not None and "merge-base" in argv:
                return SimpleNamespace(
                    returncode=self._fail_with, stdout="", stderr="broken checkout"
                )
            if "cat-file" in argv:
                revision = argv[-1].split("^")[0]
                return SimpleNamespace(
                    returncode=0 if revision in self.parents else 128,
                    stdout="",
                    stderr="" if revision in self.parents else "Not a valid object name",
                )
            ancestor, descendant = argv[-2], argv[-1]
            return SimpleNamespace(
                returncode=0 if self.reaches(ancestor, descendant) else 1, stdout="", stderr=""
            )

        return run


# A well-formed SHA that is not a commit anywhere. The "internally consistent but
# invented" case root's review asked the tests to reject: it passes every syntax check
# and every self-comparison, and git has never heard of it.
INVENTED_REVISION = "f" * 40


def contained_in_claim(**overrides) -> dict:
    """The ancestry map the artifact used to be believed about, kept as a NEGATIVE.

    It exists only so tests can assert that writing it down changes nothing. That is
    the shape of root's finding: `is_ancestor: true` is the conclusion the check
    reaches, so an artifact stating it was restating the question. No passing fixture
    includes this; every test that does is proving it is inert.
    """
    claim = {
        "worker": {"deployed_revision": DEPLOYED_WORKER_REVISION, "is_ancestor": True},
        "gateway": {"deployed_revision": DEPLOYED_GATEWAY_REVISION, "is_ancestor": True},
    }
    claim.update(overrides)
    return claim


def graph_without(component: str, revision: str) -> CommitGraph:
    """A graph where one deployed component does NOT contain `revision`.

    The honest shape of a stale deployment: the component's build branched from
    somewhere that never included the commit. Both commits still exist — so git can
    answer, and the answer is "no", which is a failed evaluation rather than an
    unrun one.
    """
    graph = CommitGraph()
    deployed = {
        "worker": DEPLOYED_WORKER_REVISION,
        "gateway": DEPLOYED_GATEWAY_REVISION,
    }[component]
    # Re-root that component on a commit of its own, which descends from nothing the
    # stories or wave 1 merged into.
    graph.parents[revision] = graph.parents.get(revision, ())
    graph.parents[deployed] = (STALE_BRANCH_POINT,)
    graph.parents[STALE_BRANCH_POINT] = ()
    return graph


# The commit a stale component branched from: real, and an ancestor of nothing under
# review. Distinct from `INVENTED_REVISION`, and the distinction is the one the harness
# has to keep — this one makes git say "no", that one makes git say "I cannot tell you".
STALE_BRANCH_POINT = "e" * 40


def raw_metadata(command: str, body, **overrides) -> dict:
    """One archived tool response: how it was obtained, when, and what came back."""
    document = {
        "command": command,
        "retrieved_at": relative_time(-30),
        "body": body,
    }
    document.update(overrides)
    return document


def codebuild_body(
    build_id: str, revision: str, tag: str, *, status: str = "SUCCEEDED", **build_overrides
) -> dict:
    """A real-shaped `aws codebuild batch-get-builds` response for one build.

    The field locations are the ones the API actually uses, because that is what the
    harness parses: `buildStatus` for the outcome, `environment.environmentVariables`
    for the `ADP_SOURCE_SHA` and `IMAGE_TAG` overrides `codebuild-run.sh` passes, and
    `source.location` for the `codebuild/src/<sha>-<unique>.zip` archive that script
    uploads. A GitHub Actions run document would not model our build path at all.
    """
    build = {
        "id": build_id,
        "projectName": build_id.split(":")[0],
        "buildStatus": status,
        "source": {
            "type": "S3",
            "location": f"adp-terraform-state-879318057152/codebuild/src/{revision}-a1b2c3.zip",
        },
        "environment": {
            "environmentVariables": [
                {"name": "ADP_SOURCE_SHA", "value": revision, "type": "PLAINTEXT"},
                {"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"},
                {"name": "PUBLISH_LATEST", "value": "true", "type": "PLAINTEXT"},
            ]
        },
    }
    build.update(build_overrides)
    return {"builds": [build]}


def push_log_body(tag: str, digest: str) -> dict:
    """A real-shaped `aws logs get-log-events` response containing a push digest line.

    `docker push` ends each tag with `<tag>: digest: sha256:... size: <bytes>`, and
    that line is the build's own statement of what it published — the corroboration
    `built_digest` previously had none of.
    """
    return {
        "events": [
            {"message": "Build started"},
            {"message": "The push refers to repository [123.dkr.ecr.us-east-1.amazonaws.com/x]"},
            {"message": f"{tag}: digest: {digest} size: 4703"},
            {"message": "Phase complete: BUILD State: SUCCEEDED"},
        ]
    }


def build_record(component: str, revision: str, digest: str, **overrides) -> dict:
    """The archived provenance tying one running digest to the build that made it.

    Modelled on what the real deployed build path emits, because that is the point of
    the schema: `aws codebuild batch-get-builds` reports the build, its outcome and the
    source archive it consumed; the build log reports the digest `docker push`
    published for the tag; `aws ecr describe-images` reports the digest being served.
    The summary fields are restatements of those three bodies, and W2-01 PARSES the
    bodies at their real field locations — so a test cannot fabricate a link by
    asserting it, and a document that merely mentions the right strings does not pass.
    """
    build_id = f"adp-{component}-build:{'0' * 8}-1111-2222-3333-{'4' * 12}"
    tag = revision[:12]
    record = {
        "project": f"adp-{component}-build",
        "build_id": build_id,
        "build_url": (
            "https://console.aws.amazon.com/codesuite/codebuild/projects/"
            f"adp-{component}-build/build/{build_id}"
        ),
        "built_revision": revision,
        "image_tag": tag,
        "built_digest": digest,
        "repository": f"adp-{component}",
        "registry_digest": digest,
        "raw": {
            "build": raw_metadata(
                f"aws codebuild batch-get-builds --ids {build_id}",
                codebuild_body(build_id, revision, tag),
            ),
            "build_log": raw_metadata(
                f"aws logs get-log-events --log-group-name /aws/codebuild/adp-{component}-build "
                f"--log-stream-name {build_id.split(':')[1]}",
                push_log_body(tag, digest),
            ),
            "registry": raw_metadata(
                f"aws ecr describe-images --repository-name adp-{component} "
                f"--image-ids imageTag={tag}",
                {
                    "imageDetails": [
                        {
                            "repositoryName": f"adp-{component}",
                            "imageDigest": digest,
                            "imageTags": [tag],
                        }
                    ]
                },
            ),
        },
    }
    record.update(overrides)
    return record


def deployed_components(**overrides) -> dict:
    """What is RUNNING, per component: revision, image digest, source revision, build.

    Two components with distinct revisions AND distinct digests, because they ship
    from separate workflows and W2-01 rejects an equal digest pair — that pair is
    what a value copied over its neighbour looks like, and it would let a stale
    half-deployment satisfy every per-component comparison.

    `source_revision == revision` per component is the source-to-image link, and
    `build_record` is what makes that pair mean something: the archived output of the
    run that performed the build and of the registry serving the result. Without it
    the pair only established that the operator wrote one SHA twice.
    """
    components = {
        "worker": {
            "revision": DEPLOYED_WORKER_REVISION,
            "image_digest": WORKER_DIGEST,
            "source_revision": DEPLOYED_WORKER_REVISION,
            "build_record": build_record("worker", DEPLOYED_WORKER_REVISION, WORKER_DIGEST),
        },
        "gateway": {
            "revision": DEPLOYED_GATEWAY_REVISION,
            "image_digest": GATEWAY_DIGEST,
            "source_revision": DEPLOYED_GATEWAY_REVISION,
            "build_record": build_record("gateway", DEPLOYED_GATEWAY_REVISION, GATEWAY_DIGEST),
        },
    }
    components.update(overrides)
    return components


def required_ci_gates(*, tested_revision: str | None = None, **overrides) -> dict:
    """The gates CI actually defines, each on a revision that is actually deployed.

    Keyed by the required names rather than by whatever the operator happened to
    record: an arbitrary nonempty map of "passed" values demonstrates the operator's
    spelling, not the build's gates. `tested_revision` is what makes a green run
    evidence about THIS build instead of a green run of unknown subject, and `raw` is
    the run document those fields are read out of — the correction for a gate whose
    `run_id` could be any truthy value at all.

    `tested_revision` is a parameter rather than a constant because the harness
    requires the gates to have run on a revision that is actually deployed, so a test
    that changes what is deployed has to be able to move the gates with it.
    """
    tested = tested_revision or DEPLOYED_WORKER_REVISION
    gates = {}
    for index, name in enumerate(_mod.WAVE2_REQUIRED_CI_GATES):
        run_id = f"ci-run-{index}"
        job_id = _mod.CI_GATE_JOB_IDS[name]
        gates[name] = {
            "status": "passed",
            "run_id": run_id,
            "run_url": f"https://github.com/aws-e/adp/actions/runs/990{index}",
            "tested_revision": tested,
            "raw": {
                "run": raw_metadata(
                    f"gh run view {run_id} --json databaseId,headSha,attempt,event,jobs",
                    {
                        "databaseId": run_id,
                        "headSha": tested,
                        "attempt": 1,
                        "event": "pull_request",
                        "jobs": [{"name": name, "conclusion": "success"}],
                    },
                ),
                "checkout": raw_metadata(
                    f"gh run download {run_id} -n checked-out-revision-{job_id}",
                    checkout_artifact(job_id, run_id, tested),
                ),
            },
        }
    gates.update(overrides)
    return gates


def checkout_artifact(job_id: str, run_id: str, revision: str, **overrides) -> dict:
    """The JSON `agent-control-ci.yml` uploads as `checked-out-revision-<job>`.

    Exactly the six keys the workflow's `Verify and record the checked-out revision`
    step writes — the test fixture models the emitted schema rather than a convenient
    one, because root's finding was that the harness must read this artifact instead of
    a SHA the operator typed into the run response.
    """
    artifact = {
        "job": job_id,
        "checked_out_revision": revision,
        "run_id": run_id,
        "run_attempt": "1",
        "event_name": "pull_request",
        "workflow_ref_sha": revision,
    }
    artifact.update(overrides)
    return artifact


def fixture_identity(**overrides) -> dict:
    """Which fixture, in which account and environment, these observations describe.

    Without it a complete, internally consistent artifact from a previous run against
    a different fixture is indistinguishable from this run's evidence.
    """
    config = valid_config()
    identity = {
        "account_id": config["account_id"],
        "environment": config["environment"],
        "run_id": config["live_run_id"],
    }
    identity.update(overrides)
    return identity


# The resources a wave-2 fixture creates, with the identity each was observed to
# have AT CREATION. Identities rather than names because `kubectl apply` can adopt a
# pre-existing same-name object and `create-queue` can return an existing queue — so
# a name establishes neither ownership nor, at teardown, that the thing removed was
# the thing created.
def creation_ledger() -> list[dict]:
    return [
        {
            "kind": "Deployment",
            "name": "agent-worker-fixture-1",
            "identity": "uid:11111111-1111-1111-1111-111111111111",
            "created": True,
        },
        {
            "kind": "Pod",
            "name": "control-probe-1",
            "identity": "uid:22222222-2222-2222-2222-222222222222",
            "created": True,
        },
        {
            "kind": "NetworkPolicy",
            "name": "control-fixture-isolation",
            "identity": "uid:33333333-3333-3333-3333-333333333333",
            "created": True,
        },
    ]


def relative_time(offset_seconds: int) -> str:
    """An ISO-8601 instant `offset_seconds` from now.

    Anchored to real time rather than a fixed date because the harness stamps its own
    teardown window with `datetime.now()`, and W2-10 compares the artifact's
    `captured_at` against that window. A fixed 2026-09-12 literal would sit years
    before every real run, so the honest fixture would fail the ordering check for a
    reason that has nothing to do with what the test is about. Offsets keep the
    RELATIVE order — which is the property being tested — while staying comparable to
    the harness's own clock.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


#: How long after the teardown begins each fixture resource is removed. The
#: control-enabled workloads go FIRST and the NetworkPolicy last, which is the order
#: DP-INV-1 requires: deleting the policy while its workload still runs leaves a
#: control-enabled pod reachable with its ingress restriction already gone.
LEDGER_REMOVAL_OFFSETS = {
    "Deployment": 10,
    "Pod": 15,
    "NetworkPolicy": 40,
}


def ledger_removals(**overrides) -> list[dict]:
    """One absence observation per created resource, keyed by the SAME identity.

    `observed_by` records HOW absence was established. Without it the entry is a
    claim rather than an observation, which is the substitution root's fourth finding
    named: `{name: True}` maps let omitting a leaked resource pass.

    `removed_at` is what makes the ORDER checkable rather than only the end state:
    both the workload and the policy must be gone, but the policy must go last.
    """
    by_identity = {
        entry["identity"]: {
            "identity": entry["identity"],
            "absent": True,
            "observed_by": f"kubectl get {entry['kind'].lower()} {entry['name']} --ignore-not-found",
            "removed_at": relative_time(LEDGER_REMOVAL_OFFSETS[entry["kind"]]),
        }
        for entry in creation_ledger()
    }
    by_identity.update(overrides)
    return list(by_identity.values())


def bent_removal(identity: str, **fields) -> dict:
    """A complete absence observation for `identity`, with `fields` replaced.

    Exists so a test that bends ONE field of one observation does not also silently
    drop the others. Hand-writing the whole dict was how these negatives were built
    before `removed_at` was required, and an incomplete literal now fails on the
    missing key instead of on the defect the test is named for — which would leave the
    real behaviour unasserted while the suite still went green.
    """
    complete = next(
        removal for removal in ledger_removals() if removal["identity"] == identity
    )
    return {**complete, **fields}


def wave2_preflight_payload(**overrides) -> dict:
    """A complete, passing W2-01 artifact.

    Every required key present with the passing value, so a test exercising one
    failure mode bends exactly one field.
    """
    payload: dict = {
        "wave1_evidence": {
            "accepted": True,
            "evaluation": _mod.WAVE_EVALUATIONS[1],
            "passed": len(_mod.WAVE1_CHECKS),
            "required": len(_mod.WAVE1_CHECKS),
            "cleanup_ok": True,
            "revision": WAVE1_ACCEPTED_REVISION,
            # Identity, so a reviewer can retrieve wave 1's report rather than take
            # this summary of it on trust.
            "run_id": "eval-3967-run-1",
        },
        # No `contained_in`: containment is not something the artifact gets to claim
        # any more. The harness computes it from the commit graph, so the fixture's
        # contribution is the revisions themselves and `CommitGraph` supplies the
        # ancestry — a fixture that could assert containment would be asserting the
        # answer the check exists to reach.
        "merged_revisions": {
            story: {"merged": True, "revision": WAVE2_REVISION}
            for story in _mod.WAVE2_REQUIRED_STORIES
        },
        "protocol_version": _mod.CONTROL_PROTOCOL_VERSION,
        "adapter_id": _mod.CLAUDE_ADAPTER_ID,
        "sdk_version": _mod.EXPECTED_CLAUDE_SDK_VERSION,
        "package_versions": {
            name: "0.3.220" for name in _mod.WAVE2_REQUIRED_PACKAGES
        },
        "deployed_components": deployed_components(),
        "ci_gates": required_ci_gates(),
        "isolation_before_listener": True,
        "ordinary_flags_off": True,
        "fixture_only_flag_scope": {
            "enabled_in_fixture": True,
            # An enumerated empty list, not `false`: the claim is "nowhere else",
            # and a list is the only form of it an auditor can check.
            "enabled_elsewhere": [],
            "fixture_environment": valid_config()["environment"],
        },
        "fixture_identity": fixture_identity(),
        "creation_ledger": creation_ledger(),
    }
    payload.update(overrides)
    return payload


def security_capture_payload(**overrides) -> dict:
    """A complete, passing W2-10 artifact for the observations taken BEFORE teardown.

    Half of what was one `cleanup_security_recheck` artifact. The split is the
    ordering correction: these are observations of a RUNNING deployment, so they have
    to be recorded while it exists. Asking for them after teardown is what made a
    correct teardown report NOT RUN.

    `observed_revisions` binds them to the DEPLOYED build — not to a story's
    historical merge commit, which a correct (newer) deployment does not equal.
    """
    payload: dict = {
        "captured_before_teardown": True,
        "observed_revisions": {
            component: {
                "revision": entry["revision"],
                "image_digest": entry["image_digest"],
            }
            for component, entry in deployed_components().items()
        },
        "fixture_identity": fixture_identity(),
        "isolation_present": True,
        "wave1_security": {
            "unauthenticated_rejected": True,
            "cross_tenant_indistinguishable": True,
            "nonowner_indistinguishable": True,
            "transport_targets_blocked": True,
            "no_token_in_public_state": True,
            "admission_authorization_preserved": True,
            "delivery_authorization_preserved": True,
        },
        "unsupported_verbs": {verb: 501 for verb in UNSUPPORTED_VERBS},
        "unsupported_adapter_capabilities": {verb: False for verb in UNSUPPORTED_VERBS},
        "general_flag_enablement": False,
        "ordinary_flags_off": True,
    }
    payload.update(overrides)
    return payload


#: How long after the teardown begins the absence observations were recorded: AFTER
#: the last removal in `LEDGER_REMOVAL_OFFSETS`, because the honest producer of this
#: artifact is the teardown command itself — it removes the resources, reads their
#: absence, and writes this. A value predating the teardown is the prefilled artifact
#: W2-10 refuses.
TEARDOWN_VERIFIED_OFFSET = 60


def teardown_verification_payload(**overrides) -> dict:
    """A complete, passing W2-10 artifact for what teardown ACHIEVED.

    Only absence, because absence is the only thing teardown produces. Nothing here
    requires a removed resource to respond — that requirement is the defect this
    split fixes.

    Deliberately carries no row-deletion claim: the deletion half of W2-10 is
    verified against the harness's own `CleanupOutcome`, and a field here saying
    "cleanup succeeded" is exactly the substitute #5825 exists to remove.
    """
    payload: dict = {
        "verified_after_teardown": True,
        # Dated inside the teardown window the harness records, because that is what
        # distinguishes a fresh observation from a file written earlier in the run.
        "captured_at": relative_time(TEARDOWN_VERIFIED_OFFSET),
        "fixture_identity": fixture_identity(),
        "removals": ledger_removals(),
        # The environment's PERSISTENT baseline isolation, not the fixture's own
        # policies: those are ledger resources and must be gone. Conflating the two
        # made this artifact unsatisfiable alongside ledger reconciliation.
        "baseline_isolation_present": True,
        "general_flag_enablement": False,
        "ordinary_flags_off": True,
    }
    payload.update(overrides)
    return payload


def wave2_cleanup_items() -> list[dict]:
    """Exact-key teardown for every synthetic row a wave-2 fixture seeds.

    Both key halves on every row, and the `unknown_run_id` deliberately absent:
    it names a row that must NOT exist, so "cleaning" it would mean deleting an
    object the harness never created.
    """
    config = valid_config()
    return [
        {"event_id": config["live_run_id"], "arrived_at": "2026-09-12T00:00:00Z"},
        {"event_id": config["terminal_run_id"], "arrived_at": "2026-09-12T00:00:00Z"},
        {"event_id": ABORTED_RUN_ID, "arrived_at": "2026-09-12T00:00:00Z"},
    ]


def wave2_config(tmp_path: Path, **overrides) -> dict:
    """`live_config` plus the wave-2 artifacts and the seeded aborted run."""
    payloads = overrides.pop("artifact_payloads", None) or {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
        **wave2_only_artifact_payloads(),
    }
    config = live_config(tmp_path, artifact_payloads=payloads)
    config["aborted_run_id"] = ABORTED_RUN_ID
    # Wave 1 declares only its own live row; wave 2 seeds three, and W2-01 fails a
    # fixture whose teardown does not cover all of them.
    config["cleanup_items"] = wave2_cleanup_items()
    config.update(overrides)
    return config


def wave2_only_artifact_payloads() -> dict:
    """The three artifacts #5825 added: W2-01's preflight and W2-10's two halves.

    Two for W2-10, not one, because they are recorded at opposite sides of the
    teardown boundary: the security observations while the fixture runs, the absence
    observations after it is gone. One combined artifact could not express that
    ordering, and the version that tried made a post-teardown check depend on a
    deleted resource.
    """
    return {
        "wave2_preflight": wave2_preflight_payload(),
        "security_capture": security_capture_payload(),
        "teardown_verification": teardown_verification_payload(),
    }


def _artifacts_read_by(method_names) -> tuple[str, ...]:
    """Every `_artifact("name")` a set of `Driver` methods reads, from the source.

    Derived by scanning the harness rather than listed by hand. The reason is the
    same one that motivates this whole issue: a hand-maintained inventory stops
    being exhaustive silently, and a test parametrized over a stale list passes by
    covering less than it claims. Scanning means a check that starts reading a new
    artifact is covered the moment the call appears.

    Bounded per method by the next `def` at the same indentation, so one check's
    reads are not attributed to its neighbour.
    """
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    found: list[str] = []
    for name in method_names:
        start = source.find(f"    def {name}(")
        if start == -1:
            continue
        end = source.find("\n    def ", start + 1)
        body = source[start : end if end != -1 else len(source)]
        for artifact in re.findall(r'_artifact\("([^"]+)"\)', body):
            if artifact not in found:
                found.append(artifact)
    return tuple(found)


# Every artifact a wave-2 check reads. Used to parametrize the "no single missing
# artifact can exit zero" test over the real input space of omissions, and guarded
# by a test that fails if the derivation ever yields a short list.
WAVE2_READ_ARTIFACTS: tuple[str, ...] = _artifacts_read_by(
    _mod.WAVE2_PREDICATES[spec.check_id]
    for spec in _mod.WAVE2_CHECKS
    if spec.check_id in _mod.WAVE2_PREDICATES
)


def stats_body(**overrides) -> dict:
    """A stats response shaped like the real `StatsResponse`, with aborted present."""
    body = {
        "window_days": 7,
        "active_runs": [
            {
                "invocation_id": "msg-active-1",
                "invoked_at": "2026-09-15T09:00:00Z",
                "persona": "developer",
                "repo": "aws-e/adp",
                "topic": "a run in flight",
            }
        ],
        "today": {"total": 12, "completed": 6, "failed": 2, "active": 2, "aborted": 2},
        "daily": [
            {"date": "2026-09-15", "total": 12, "completed": 6, "failed": 2, "aborted": 2}
        ],
        "by_persona": [
            {"persona": "developer", "total": 12, "completed": 6, "failed": 2, "aborted": 2}
        ],
        "recent_failures": [
            {
                "invocation_id": "msg-failed-1",
                "invoked_at": "2026-09-15T08:00:00Z",
                "persona": "developer",
                "repo": "aws-e/adp",
                "topic": "a run that broke",
                "error_message": "boom",
            }
        ],
        "top_repos": [{"repo": "aws-e/adp", "total": 12}],
        "spend": {"total_cost_usd": 1.25, "total_tokens": 4096, "total_calls": 12},
    }
    body.update(overrides)
    return body


def aborted_detail_body(**overrides) -> dict:
    """The seeded aborted invocation as the read API returns it."""
    body = {
        "invocation_id": ABORTED_RUN_ID,
        "status": "aborted",
        "completed_at": "2026-09-15T10:15:00Z",
        "liveness": "exited",
        "persona": "developer",
    }
    body.update(overrides)
    return body


def wave2_gateway_stub(**overrides):
    """A fake gateway answering the way a correct S5 deployment does.

    Only the three endpoints the wave-2 checks read. Overrides bend one response so
    a test can prove the harness notices, in the same style as `gateway_stub`.

    `state_capabilities` / `state_extra` are forwarded to the inner `gateway_stub`
    for W2-03, which does not stop at the artifact: it also reads `/state` to
    confirm the deployment advertises the pause capability and reports
    `active_tool_count`. The default here advertises pause, because this stub
    models a *correct* deployment — note that this is deliberately NOT the state of
    the tree, where the verb is disabled pending the authorization intersection
    (`docs/design-notes/3961-control-authorization-intersection.md`). Proving the
    check notices that mismatch is what
    `test_a_deployment_that_disables_pause_fails_w2_03` is for.
    """
    detail = overrides.get("detail_body", aborted_detail_body())
    stats = overrides.get("stats", stats_body())
    listed = overrides.get(
        "list_items", [{"invocation_id": ABORTED_RUN_ID, "status": "aborted"}]
    )
    inner_kwargs = {
        "capabilities": overrides.get(
            "state_capabilities",
            {verb: verb in {"pause", "resume"} for verb in _mod.CONTROL_VERBS},
        )
    }
    for passthrough in ("state_extra", "state_omit"):
        if passthrough in overrides:
            inner_kwargs[passthrough] = overrides[passthrough]
    inner = gateway_stub(**inner_kwargs)

    def handler(method, url, headers=None, content=None, json=None, timeout=None):
        response = MagicMock()

        def reply(status, body):
            response.status_code = status
            response.json = lambda: body
            return response

        if "/activity/invocations/" in url or "/orchestration/runs/" in url:
            return inner.request(method, url, headers=headers, content=content, json=json, timeout=timeout)
        if "agent-run-stats" in url:
            return reply(overrides.get("stats_status", 200), stats)
        # The filtered list. Asserted on the query string because "does the filter
        # actually select" is the question W2-06 asks.
        if "agent-invocations?" in url:
            if "status=aborted" not in url:
                return reply(400, {"detail": "unexpected query"})
            return reply(overrides.get("list_status", 200), {"items": listed, "last_key": None})
        if "agent-invocations/" in url:
            return reply(overrides.get("detail_status", 200), detail)
        return reply(404, {"detail": "not found"})

    client = MagicMock()
    client.request.side_effect = handler
    return client


class SharedRowStore:
    """One row store behind BOTH the fake DynamoDB and the fake gateway.

    This class is the reason the rest of the wave-2 regression is trustworthy, and it
    exists because of a specific defect root's review reproduced. The previous fixture
    used two independent fakes: `ddb_stub` answered `get_item` from a canned value and
    `wave2_gateway_stub` answered every `/state` with a static 200. They described
    different worlds. Cleanup could delete a row in one world while the gateway in the
    other went on serving it as live — so a post-teardown check that (wrongly) needed
    the deleted run to answer still saw a 200, and the positive test passed. Against a
    real gateway, which stops serving a deleted row, the same harness reported
    `W2-10 not_run`: a CORRECT teardown could not produce a passing wave.

    A stub can only be evidence about a real deployment where it is CONSTRAINED like
    one. So here there is exactly one dict of rows: `delete_item` removes from it,
    `get_item` reads from it, and the gateway's `/state` and command routes 404 for a
    run whose row is gone. Any check that depends on a removed resource answering now
    fails the test suite instead of passing it.
    """

    def __init__(self, run_ids):
        pairs = [(str(run_id), str(arrived_at)) for run_id, arrived_at in run_ids]
        # Present rows, keyed the way the real table is keyed: an existing row is one
        # this fixture seeded and teardown has not yet removed.
        self.rows: dict[tuple[str, str], dict] = {
            pair: {"event_id": {"S": pair[0]}} for pair in pairs
        }
        # Every run this store ever held. Retained after removal because the gateway
        # half needs to distinguish "this fixture's row, now deleted" (404) from a run
        # this store never described at all (left to the inner stub).
        self.seeded: frozenset[str] = frozenset(event_id for event_id, _ in pairs)
        self.deletes: list[dict] = []

    # ---- the DynamoDB half ------------------------------------------------

    def exists(self, run_id: str) -> bool:
        return any(event_id == str(run_id) for event_id, _ in self.rows)

    def dynamodb(self, *, delete_raises=None, refuse_delete_for=()) -> MagicMock:
        """A DynamoDB client whose deletes and reads act on this store.

        `delete_raises` and `refuse_delete_for` model the two failure shapes that
        matter — an API error, and a delete that reports success while the row stays
        present — without letting either drift away from what the gateway then sees.
        """
        client = dynamodb_with_schema(CORRECT_SCHEMA)
        calls = {"n": 0}

        def delete_item(**kwargs):
            calls["n"] += 1
            key = kwargs["Key"]
            pair = (key["event_id"]["S"], key["arrived_at"]["S"])
            self.deletes.append(dict(kwargs))
            if delete_raises is not None and calls["n"] in delete_raises:
                raise RuntimeError(delete_raises[calls["n"]])
            if pair[0] in refuse_delete_for:
                # The silent-failure case: the API accepts the delete and the row is
                # still there. Only a read can tell.
                return {}
            self.rows.pop(pair, None)
            return {}

        def get_item(**kwargs):
            key = kwargs["Key"]
            pair = (key["event_id"]["S"], key["arrived_at"]["S"])
            row = self.rows.get(pair)
            return {"Item": row} if row else {}

        client.delete_item.side_effect = delete_item
        client.get_item.side_effect = get_item
        return client

    # ---- the gateway half -------------------------------------------------

    def gateway(self, **overrides):
        """A gateway that stops serving a run once its row is gone."""
        return self.wrap(wave2_gateway_stub(**overrides))

    def wrap(self, inner):
        """Constrain an EXISTING fake gateway by this store's rows.

        Takes a client rather than building one so a test that bends some other
        response — a 500 on the detail route, a missing capability map — still gets the
        one constraint a real deployment cannot be without. Otherwise every such test
        would quietly revert to the two-worlds fixture this class exists to eliminate.

        Every other response stays exactly what the rest of the wave-2 suite relies on.
        The 404 is returned rather than a 200 with `available: false` because a deleted
        row is a run the gateway cannot find, not a run it knows to be unavailable.
        """

        def handler(method, url, headers=None, content=None, json=None, timeout=None):
            for run_id in self.seeded:
                if f"/{run_id}" in url and not self.exists(run_id):
                    response = MagicMock()
                    response.status_code = 404
                    response.json = lambda: {"detail": "not found"}
                    return response
            return inner.request(
                method, url, headers=headers, content=content, json=json, timeout=timeout
            )

        client = MagicMock()
        client.request.side_effect = handler
        return client


class FixtureResources:
    """The fixture's CLUSTER resources, modelled the way `SharedRowStore` models rows.

    The point of this class is the same as that one's, applied to root's second
    finding. W2-10's absence half was previously judged against a hand-written
    artifact that said the pods and policies were gone; nothing in the test had any
    notion of a pod, so the artifact could say anything and the fixture could not
    disagree with it. Here the resources really exist, the teardown command really
    removes them, and the absence artifact is written FROM this state rather than
    asserted alongside it.

    That means a test can no longer accidentally describe an impossible run. A
    prefilled artifact and a teardown that removes nothing now produce different
    bytes on disk, which is exactly the distinction the harness's digest comparison
    is looking for.

    `removal_order` is what makes finding 4 testable: the teardown removes kinds in
    the order given, stamping each with an increasing timestamp, so a test can ask
    for the unsafe order (policy before workload) and get a genuinely unsafe removal
    rather than a doctored timestamp.
    """

    #: Kinds removed in the DP-INV-1-safe order: control-enabled workloads first, the
    #: NetworkPolicy restricting reach to them last.
    SAFE_ORDER = ("Deployment", "Pod", "NetworkPolicy")

    def __init__(self, ledger=None, *, removal_order=None, clock=None):
        self.ledger = list(ledger if ledger is not None else creation_ledger())
        self.present = {entry["identity"]: dict(entry) for entry in self.ledger}
        self.removed_at: dict[str, str] = {}
        self.removal_order = tuple(removal_order or self.SAFE_ORDER)
        # Monotonic offsets from real time. The offsets — not wall-clock resolution —
        # carry the ordering, so the assertions do not depend on how fast the test
        # machine is; anchoring to now keeps them comparable to the window the harness
        # stamps with its own clock.
        self._tick = 0
        self._clock = clock or relative_time

    def remove_all(self) -> None:
        """Remove every fixture resource, in `removal_order`, stamping each removal."""
        by_kind: dict[str, list[dict]] = {}
        for entry in self.ledger:
            by_kind.setdefault(str(entry.get("kind")), []).append(entry)
        ordered = [kind for kind in self.removal_order if kind in by_kind]
        ordered += [kind for kind in by_kind if kind not in ordered]
        for kind in ordered:
            for entry in by_kind[kind]:
                self._tick += 5
                self.removed_at[entry["identity"]] = self._clock(self._tick)
                self.present.pop(entry["identity"], None)

    def absence_observations(self) -> list[dict]:
        """What a real post-teardown read of these resources would report.

        `absent` is computed from `present`, not asserted: a resource this teardown
        failed to remove reports `absent: False` here, and the check fails on it. A
        resource never removed carries no `removed_at`, for the same reason.
        """
        return [
            {
                "identity": entry["identity"],
                "absent": entry["identity"] not in self.present,
                "observed_by": (
                    f"kubectl get {str(entry['kind']).lower()} {entry['name']} --ignore-not-found"
                ),
                "removed_at": self.removed_at.get(entry["identity"]),
            }
            for entry in self.ledger
        ]

    def latest_removal(self) -> str:
        """The last removal stamp, for dating an artifact written after teardown."""
        return max(self.removed_at.values(), default=self._clock(0))


def teardown_runner_for(
    tmp_path: Path,
    config: dict,
    resources: FixtureResources,
    *,
    exit_code: int = 0,
    remove: bool = True,
    write_verification: bool = True,
    verification=None,
    raises: BaseException | None = None,
):
    """A stand-in for the operator's `resource_teardown` command.

    Models the honest lifecycle: remove the resources, then record their absence,
    then exit. That ORDER is the thing being tested — the artifact is written from
    post-removal state, so it cannot claim an absence that did not happen.

    Each keyword turns off one part of that lifecycle, which is how the negatives are
    built: `remove=False` is a command that reports success while removing nothing,
    `write_verification=False` is one that removes without recording, `exit_code`
    makes it fail, and `raises` makes it unrunnable. None of them let a test fake the
    *result* — they change what the command does and let the harness notice.
    """
    # Resolved the same way `ArtifactStore` resolves it, so the command writes the file
    # the harness reads. A test whose runner wrote somewhere else would leave the
    # original prefilled artifact in place and look like the defect it is testing for.
    #
    # `None` when the config declares no such artifact. A fixture that never told the
    # harness where to read absence from has nowhere for this command to write it
    # either, and inventing a path would hand the harness a file it was not pointed at.
    raw = (config.get("artifacts") or {}).get("teardown_verification")
    path = None
    if raw:
        declared = Path(raw)
        path = declared if declared.is_absolute() else tmp_path / declared

    def runner(argv, timeout):  # noqa: ANN001, ARG001 - matches the real runner's shape
        if raises is not None:
            raise raises
        if remove:
            resources.remove_all()
        if write_verification and path is not None:
            payload = (
                verification
                if verification is not None
                else teardown_verification_payload(
                    removals=resources.absence_observations(),
                    captured_at=resources.latest_removal(),
                )
            )
            path.write_text(json.dumps(payload), encoding="utf-8")
        completed = MagicMock()
        completed.returncode = exit_code
        completed.stdout = f"removed {len(resources.removed_at)} fixture resources\n"
        completed.stderr = ""
        return completed

    return runner


def shared_store_for(config: dict) -> SharedRowStore:
    """A `SharedRowStore` seeded with exactly the rows this config declares.

    Built from `cleanup_items` rather than a literal so a test that changes the
    declared rows cannot leave the store describing different ones.
    """
    items = config.get("cleanup_items") or []
    return SharedRowStore(
        (item.get("event_id"), item.get("arrived_at"))
        for item in items
        if isinstance(item, dict) and item.get("event_id") and item.get("arrived_at")
    )


def cleanup_outcome_for(config: dict, **overrides) -> object:
    """The record a successful `run_cleanup` produces for this config's rows.

    Built from the config's own `cleanup_items` rather than a literal, so a test
    that changes the declared rows does not silently leave the deletion record
    describing different ones — which is the mismatch W2-10 is built to notice.
    """
    items = config.get("cleanup_items") or []
    deletions = []
    ok = True
    for item in items:
        pair = item if isinstance(item, dict) else {}
        event_id, arrived_at = pair.get("event_id"), pair.get("arrived_at")
        # Mirrors `run_cleanup`'s own branching rather than assuming success: a row
        # declared without both key halves is REFUSED there, and a helper that
        # reported it deleted anyway would hand W2-10 a record the real teardown
        # never produces.
        both = bool(event_id) and bool(arrived_at)
        ok = ok and both
        deletions.append(
            _mod.RowDeletion(
                event_id=str(event_id or ""),
                arrived_at=str(arrived_at or ""),
                both_keys_present=both,
                deleted=both,
                confirmed_absent=both,
                error=None if both else "partial key; refused",
            )
        )
    fields = {
        "ok": ok,
        "notes": [f"removed {d.event_id}/{d.arrived_at}" for d in deletions if d.deleted],
        "deletions": deletions,
        "declared_items": len(items),
    }
    fields.update(overrides)
    return _mod.CleanupOutcome(**fields)


_UNSET = object()


def capture_for(config: dict, client=None, **overrides) -> object:
    """The pre-teardown capture the harness's OWN reads produce for this fixture.

    Produced by calling `capture_security_observations` against the stub gateway
    rather than hand-building a `SecurityCapture`, so a helper cannot hand W2-10
    observations the real capture would never make. `overrides` replaces a field
    afterwards, which is how a test models a capture that half-completed.

    Called while the store's rows are still present, which is the ordering the real
    `main` uses — a capture taken after teardown is precisely the defect.
    """
    probe = _mod.Probe(config["gateway_url"], client or wave2_gateway_stub())
    artifacts = _mod.ArtifactStore(Path("/nonexistent"), {})
    driver = _mod.Driver(config, probe, artifacts, dynamodb=None)
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        capture = _mod.capture_security_observations(driver, config)
    if not overrides:
        return capture
    fields = {
        "ok": capture.ok,
        "run_id": capture.run_id,
        "adapters": capture.adapters,
        "notes": capture.notes,
    }
    fields.update(overrides)
    return _mod.SecurityCapture(**fields)


def run_wave2(  # noqa: PLR0913 - one parameter per substitutable collaborator
    tmp_path: Path,
    *,
    config=None,
    client=None,
    cleanup=_UNSET,
    capture=_UNSET,
    teardown=_UNSET,
    resources=None,
    runner=None,
    graph=None,
) -> dict:
    """Drive all ten wave-2 checks and return ``{check_id: CheckResult}``.

    `cleanup` defaults to the successful record the config's declared rows would
    produce, because this helper models a *correct* run; pass `None` to model a run
    where cleanup never happened, or a bent record to prove W2-10 notices.

    `capture` likewise defaults to the observations the harness's own pre-teardown
    reads would produce against this client; pass `None` to model a run where the
    capture never happened.

    `teardown` defaults to running the real `run_resource_teardown` against `runner`
    — by default a `teardown_runner_for` command that genuinely removes `resources`
    and records their absence. Pass `None` to model a run where the seam never
    executed.

    **The ordering is the real one.** Both fakes are wired to a single
    `SharedRowStore`, the capture is taken while its rows are still present, the
    resource teardown runs between the capture and the verification, and the default
    `cleanup` record is the one that store's own deletes produce. A helper that
    captured afterwards — or that let the gateway keep serving a row the deletion
    record calls gone, or that read an absence artifact no teardown had written —
    would hand W2-10 a combination no real run can produce, which is exactly how the
    previous fixture passed while the deployed harness failed.
    """
    config = config if config is not None else wave2_config(tmp_path)
    store = shared_store_for(config)
    client = store.wrap(client or wave2_gateway_stub())
    probe = _mod.Probe(config["gateway_url"], client)
    artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
    dynamodb = store.dynamodb()
    # A real commit graph behind W2-01's containment queries. The default is the
    # correct topology — deployed builds newer than the merge commits they contain —
    # and a test models a stale deployment by giving a graph where they are not.
    graph = graph if graph is not None else CommitGraph()
    driver = _mod.Driver(
        config, probe, artifacts, dynamodb=dynamodb, git_runner=graph.runner()
    )
    resources = resources if resources is not None else FixtureResources()
    config.setdefault("resource_teardown", ["/fixture/teardown.sh", "--wave", "2"])
    # The same split `main` performs, for the same reason: the nine checks read live
    # rows, so running them after teardown would make them fail for the one reason
    # that is not a defect.
    pre = [s for s in _mod.WAVE2_CHECKS if s.check_id not in _mod.POST_CLEANUP_CHECK_IDS]
    post = [s for s in _mod.WAVE2_CHECKS if s.check_id in _mod.POST_CLEANUP_CHECK_IDS]
    expected = tuple(spec.check_id for spec in _mod.WAVE2_CHECKS)
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        results = _mod.run_checks(driver, pre, manifest_ids=expected)
        # Pre-teardown: the rows still exist, so these reads can be made at all.
        capture = (
            _mod.capture_security_observations(driver, config)
            if capture is _UNSET
            else capture
        )
        # Resource teardown, through the real `run_resource_teardown`, so the freshness
        # snapshot it takes of the absence artifact is a real one. The command really
        # removes `resources` and writes the artifact from what remains.
        if teardown is _UNSET:
            teardown = _mod.run_resource_teardown(
                config,
                artifacts,
                runner=runner or teardown_runner_for(tmp_path, config, resources),
            )
        # Then rows, through the real `run_cleanup` against the shared store, so the
        # deletion record and what the gateway then serves cannot disagree.
        cleanup = _mod.run_cleanup(config, dynamodb) if cleanup is _UNSET else cleanup
        results.extend(
            _mod.run_checks(
                driver,
                post,
                manifest_ids=expected,
                cleanup=cleanup,
                capture=capture,
                teardown=teardown,
            )
        )
    return {result.check_id: result for result in results}


NATIVE_INTERRUPT_OBSERVATION = {
    "status": "failed",
    "run_id": NATIVE_INTERRUPT_RUN_ID,
    "observed_by": "GET /me/agent-invocations/{run_id} after provider interrupt",
}


def run_wave2_native_interrupt(tmp_path: Path, recorded) -> dict:
    """Drive wave 2 with exactly one thing bent: the native-interruption outcome."""
    payloads = {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
        **wave2_only_artifact_payloads(),
    }
    payloads["harness_neutrality"] = {
        **payloads["harness_neutrality"],
        "native_interrupt_status": recorded,
    }
    return run_wave2(
        tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
    )


class TestNativeInterruptMeasurementCannotBeAbsent:
    """W2-06 must not pass on an unmeasured native-interruption outcome.

    Root reproduced four false passes through `run_wave2`: `None`, `""`, `{}` and
    `"invented"` all reported PASSED, exactly as a valid `"failed"` did, because the
    predicate tested only `!= "aborted"`. The claim is a NEGATIVE — a provider's
    interrupted turn did not by itself become an ADP abort — and a negative cannot be
    established by a field nobody filled in.

    The distinction these tests pin: absent measurement is NOT RUN (nothing was
    observed, so there is nothing to judge), an unrecognised value FAILS (something
    was recorded and the deployment could not have produced it), and the valid
    non-aborted control still passes.
    """

    def test_the_valid_non_aborted_control_still_passes(self, tmp_path: Path):
        """The positive control, asserted FIRST so the negatives below are not vacuous.

        If this regressed to a fail, every other test in this class would pass for
        the wrong reason — a check that can never pass rejects bad input too.
        """
        results = run_wave2_native_interrupt(tmp_path, NATIVE_INTERRUPT_OBSERVATION)

        assert results["W2-06"].status == _mod.STATUS_PASSED, results["W2-06"].message

    @pytest.mark.parametrize("absent", [None, "", {}, []])
    def test_an_unmeasured_outcome_is_not_run_rather_than_a_pass(
        self, tmp_path: Path, absent
    ):
        """`None`/`""`/`{}` from root's reproduction: three of the four false passes."""
        results = run_wave2_native_interrupt(tmp_path, absent)

        assert results["W2-06"].status == _mod.STATUS_NOT_RUN, results["W2-06"].message
        assert "native-interruption" in results["W2-06"].message

    @pytest.mark.parametrize("invalid", ["invented", "in_progress", "active", 0, 1, True])
    def test_a_status_outside_the_writers_vocabulary_fails(
        self, tmp_path: Path, invalid
    ):
        """`"invented"` — the fourth false pass — and the still-running statuses.

        `active`/`in_progress` are rejected for a specific reason rather than for
        tidiness: the experiment interrupts a turn, so a row still reported as
        running means the experiment never reached the state it claims to describe.
        """
        results = run_wave2_native_interrupt(
            tmp_path, {**NATIVE_INTERRUPT_OBSERVATION, "status": invalid}
        )

        assert results["W2-06"].status == _mod.STATUS_FAILED, results["W2-06"].message

    def test_a_bare_status_string_carries_no_provenance_and_fails(self, tmp_path: Path):
        """Even a VALID status fails without the experiment behind it.

        This is the discriminating case: `"failed"` is a legitimate outcome, so a
        check that only validated the vocabulary would accept it. What makes it
        unusable is that it names neither the run interrupted nor how the outcome was
        read back — so it cannot be distinguished from an expectation somebody typed.
        """
        results = run_wave2_native_interrupt(tmp_path, "failed")

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "provenance" in results["W2-06"].message

    @pytest.mark.parametrize("key", ["run_id", "observed_by"])
    def test_an_experiment_missing_its_provenance_fails(self, tmp_path: Path, key):
        recorded = {k: v for k, v in NATIVE_INTERRUPT_OBSERVATION.items() if k != key}

        results = run_wave2_native_interrupt(tmp_path, recorded)

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert key in results["W2-06"].message

    def test_no_recorded_value_reaches_a_pass_without_a_real_outcome(
        self, tmp_path: Path
    ):
        """The property, stated once over the whole space root probed.

        A per-value test can be satisfied by special-casing that value. This asserts
        the general shape: of everything root tried, only a recognised status with
        provenance passes, and every other value is not_run or failed — never passed.
        """
        for recorded in (
            None,
            "",
            {},
            [],
            "invented",
            "failed",
            "aborted",
            {**NATIVE_INTERRUPT_OBSERVATION, "status": "aborted"},
            {**NATIVE_INTERRUPT_OBSERVATION, "status": "invented"},
            {**NATIVE_INTERRUPT_OBSERVATION, "status": None},
        ):
            results = run_wave2_native_interrupt(tmp_path, recorded)
            assert results["W2-06"].status != _mod.STATUS_PASSED, (
                f"{recorded!r} reached a PASS without an observed non-aborted outcome"
            )


class TestWave2AbortedChecks:
    """The four wave-2 checks S5 (#3964) owns: W2-06..W2-09.

    Same posture as the wave-1 driver tests: the harness never runs in CI, so what
    CI proves is that each check would NOTICE the deployment being wrong. Every
    test below starts from a passing fixture and bends exactly one thing, so a
    failure names the specific defect rather than being ambiguous.
    """

    def test_a_correct_deployment_passes_all_four(self, tmp_path: Path):
        results = run_wave2(tmp_path)

        for check_id in ("W2-06", "W2-07", "W2-08", "W2-09"):
            assert results[check_id].status == _mod.STATUS_PASSED, (
                check_id,
                results[check_id].message,
            )

    def test_no_check_reports_not_run_on_a_complete_fixture(self, tmp_path: Path):
        """The honesty property, at the driver level.

        Narrowed from five IDs to two when S2 (#3961) implemented W2-03..W2-05, and
        inverted by #5825, which implemented the last two. The property worth
        asserting is now the strong direction: given complete evidence, no check
        reports `not_run`. A `not_run` here would mean an observation the harness
        claims to make that it silently cannot, which is the failure the old
        ID-by-ID list was a stand-in for.
        """
        results = run_wave2(tmp_path)

        unanswered = {
            check_id: result.message
            for check_id, result in results.items()
            if result.status == _mod.STATUS_NOT_RUN
        }
        assert unanswered == {}

    # ---- W2-06: terminality and neutrality -----------------------------

    def test_a_null_completed_at_on_an_aborted_row_fails(self, tmp_path: Path):
        """AC-A3 exactly: the defect is a terminal row with no completion time."""
        client = wave2_gateway_stub(detail_body=aborted_detail_body(completed_at=None))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "completed_at" in results["W2-06"].message

    def test_an_aborted_row_reported_as_live_fails(self, tmp_path: Path):
        client = wave2_gateway_stub(detail_body=aborted_detail_body(liveness="live"))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "exited" in results["W2-06"].message

    def test_a_filter_that_omits_the_seeded_row_fails(self, tmp_path: Path):
        """AC-A9: the option existing is not the same as the option working."""
        client = wave2_gateway_stub(list_items=[])

        results = run_wave2(tmp_path, client=client)

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "not returned by status=aborted" in results["W2-06"].message

    def test_a_filter_that_ignores_its_argument_fails(self, tmp_path: Path):
        """The failure a presence-only assertion cannot see.

        A filter returning the whole table contains the seeded row, so "is it
        there?" passes. Only checking that nothing ELSE came back catches it.
        """
        client = wave2_gateway_stub(
            list_items=[
                {"invocation_id": ABORTED_RUN_ID, "status": "aborted"},
                {"invocation_id": "msg-complete-1", "status": "complete"},
            ]
        )

        results = run_wave2(tmp_path, client=client)

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "also returned" in results["W2-06"].message

    def test_a_native_interrupt_recorded_as_aborted_fails(self, tmp_path: Path):
        """The harness-neutral contract's central prohibition."""
        results = run_wave2_native_interrupt(
            tmp_path, {**NATIVE_INTERRUPT_OBSERVATION, "status": "aborted"}
        )

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "confirmed abort finalization" in results["W2-06"].message

    def test_two_adapters_disagreeing_on_accounting_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["harness_neutrality"]["adapter_b"] = {
            "aborted": 0,
            "completed": 3,
            "failed": 0,
        }

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "differs" in results["W2-06"].message

    def test_an_sdk_import_in_shared_code_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["harness_neutrality"]["shared_code_imports_sdk"] = True

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-06"].status == _mod.STATUS_FAILED
        assert "provider SDK" in results["W2-06"].message

    def test_a_missing_aborted_run_id_is_not_run(self, tmp_path: Path):
        """No seeded row means the check cannot be answered — not that it passed."""
        config = wave2_config(tmp_path)
        del config["aborted_run_id"]

        results = run_wave2(tmp_path, config=config)

        assert results["W2-06"].status == _mod.STATUS_NOT_RUN
        assert "aborted_run_id" in results["W2-06"].message

    # ---- W2-07: counted exactly once -----------------------------------

    def test_an_aborted_row_counted_twice_fails(self, tmp_path: Path):
        """`total` moving by more than the seed count."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["aborted_counters"]["today_after"]["total"] = 14

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert "exactly once" in results["W2-07"].message

    @pytest.mark.parametrize("bucket", ["completed", "failed", "active"])
    def test_an_aborted_row_also_counted_elsewhere_fails(
        self, tmp_path: Path, bucket: str
    ):
        """The half a total-only assertion misses.

        A row counted into both `aborted` and `failed` leaves `total` correct while
        doubling the failure rate — which is the number an operator is judged on,
        and the reason aborted got its own counter in the first place.
        """
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        # `total` deliberately left alone: double-counting a row into two buckets
        # does not change how many rows there are, which is the whole reason the
        # total-delta assertion above cannot see this defect.
        payloads["aborted_counters"]["today_after"][bucket] += 2

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert bucket in results["W2-07"].message

    def test_a_four_category_dataset_that_does_not_balance_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["aborted_counters"]["four_category_dataset"]["aborted"] = 1

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert "buckets sum to" in results["W2-07"].message

    def test_a_mixed_dataset_that_balances_fails_as_not_mixed(self, tmp_path: Path):
        """The inequality is the assertion, and this is why.

        If the "mixed" dataset's total equals its four buckets, it contains no
        blocked/skipped/budget_stopped rows — so it is not testing preservation of
        the outcomes it exists to protect, and the four-way equality would have
        been asserted somewhere it does not generally hold.
        """
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["aborted_counters"]["mixed_dataset"]["total"] = 9

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert "not testing preservation" in results["W2-07"].message

    def test_a_reclassified_pre_existing_outcome_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["aborted_counters"]["mixed_dataset"]["failed"] = 4

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert "reclassified" in results["W2-07"].message

    @pytest.mark.parametrize("scope", ["daily", "persona"])
    def test_a_breakdown_counter_that_did_not_move_fails(self, tmp_path: Path, scope: str):
        """Separate accumulators in `_aggregate`, so they can drift independently."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["aborted_counters"][f"{scope}_deltas"]["aborted"] = 0

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-07"].status == _mod.STATUS_FAILED
        assert scope in results["W2-07"].message

    # ---- W2-08: writer/reader parity across two images ------------------

    @pytest.mark.parametrize(
        "field", ["writer_digest_deployed", "gateway_digest_deployed"]
    )
    def test_a_half_deployment_fails(self, tmp_path: Path, field: str):
        """The specific risk of a story that spans two images and two workflows."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"][field] = False

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert field in results["W2-08"].message

    def test_a_writer_allowlist_without_aborted_fails(self, tmp_path: Path):
        """AC-A12: the abort's own terminal write would be refused."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"]["writer_allowed_statuses"] = [
            "in_progress",
            "complete",
            "failed",
        ]

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert "read as live forever" in results["W2-08"].message

    def test_a_gateway_terminal_set_without_aborted_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"]["gateway_terminal_statuses"] = ["complete", "failed"]

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert "AC-A11" in results["W2-08"].message

    def test_an_allowlist_that_never_rejects_fails(self, tmp_path: Path):
        """An allowlist whose reject path never fires is not a validation."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"]["unknown_status_rejected"] = False

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert "not a validation" in results["W2-08"].message

    def test_validation_after_the_write_fails(self, tmp_path: Path):
        """Rejected but persisted is not rejected. AC-A12 is about ORDER."""
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"]["unknown_status_reached_table"] = True

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert "BEFORE the write" in results["W2-08"].message

    def test_a_failing_parity_suite_fails(self, tmp_path: Path):
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["vocabulary_parity"]["suites"]["tests/test_status_vocabulary.py"] = "failed"

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-08"].status == _mod.STATUS_FAILED
        assert "test_status_vocabulary.py" in results["W2-08"].message

    # ---- W2-09: the live stats contract --------------------------------

    def test_a_stats_response_without_the_aborted_counter_fails(self, tmp_path: Path):
        """The field this story adds, absent from the live deployment."""
        today = {"total": 12, "completed": 6, "failed": 2, "active": 2}
        client = wave2_gateway_stub(stats=stats_body(today=today))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_FAILED
        assert "aborted" in results["W2-09"].message

    @pytest.mark.parametrize("level", ["daily", "by_persona"])
    def test_a_breakdown_row_without_aborted_fails(self, tmp_path: Path, level: str):
        rows = {
            "daily": [{"date": "2026-09-15", "total": 12, "completed": 6, "failed": 2}],
            "by_persona": [
                {"persona": "developer", "total": 12, "completed": 6, "failed": 2}
            ],
        }
        client = wave2_gateway_stub(stats=stats_body(**{level: rows[level]}))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_FAILED
        assert "aborted" in results["W2-09"].message

    def test_a_missing_top_level_key_fails(self, tmp_path: Path):
        body = stats_body()
        del body["spend"]
        client = wave2_gateway_stub(stats=body)

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_FAILED
        assert "spend" in results["W2-09"].message

    @pytest.mark.parametrize(
        "level", ["daily", "by_persona", "active_runs", "recent_failures", "top_repos"]
    )
    def test_an_empty_array_is_not_run_rather_than_a_vacuous_pass(
        self, tmp_path: Path, level: str
    ):
        """§7 requires seeded nonempty arrays.

        An empty list satisfies "every element has the required keys" vacuously, so
        treating it as a pass would let the whole check succeed against a fixture
        that produced no data at all.
        """
        client = wave2_gateway_stub(stats=stats_body(**{level: []}))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_NOT_RUN
        assert level in results["W2-09"].message

    def test_a_null_spend_is_not_run(self, tmp_path: Path):
        client = wave2_gateway_stub(stats=stats_body(spend=None))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_NOT_RUN
        assert "spend" in results["W2-09"].message

    def test_a_schema_field_the_deployment_predates_fails(self, tmp_path: Path):
        """The check the hardcoded list above cannot make.

        The presence lists in the harness are literals in that file, so they cannot
        notice a field ADDED to the backend schema and missing from the deployed
        response. The exported fixture is what catches it.
        """
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["stats_schema_keys"]["levels"]["today"].append("budget_stopped")

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
        )

        assert results["W2-09"].status == _mod.STATUS_FAILED
        assert "budget_stopped" in results["W2-09"].message

    def test_a_non_200_stats_response_fails(self, tmp_path: Path):
        client = wave2_gateway_stub(stats_status=500)

        results = run_wave2(tmp_path, client=client)

        assert results["W2-09"].status == _mod.STATUS_FAILED
        assert "500" in results["W2-09"].message

    def test_no_wave_two_check_leaks_a_token_into_its_evidence(self, tmp_path: Path):
        """Evidence is written to disk and pasted into issues."""
        results = run_wave2(tmp_path)

        rendered = json.dumps(
            {cid: result.to_evidence() for cid, result in results.items()}
        )
        assert OWNER_TOKEN not in rendered
        for token in IDENTITY_ENV.values():
            assert token not in rendered


def run_wave2_with_pause(tmp_path: Path, artifact: str, patch_: dict, **kwargs) -> dict:
    """Drive wave 2 with exactly one pause artifact field bent.

    Shallow-merges into the named artifact so a test names only the field under
    test, in the same style as `wave2_gateway_stub`'s overrides. `None` as a value
    deletes the key, which is how the "a missing field must not read as a pass"
    cases are written.
    """
    payloads = {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
    }
    for key, value in patch_.items():
        if value is None:
            payloads[artifact].pop(key, None)
        else:
            payloads[artifact][key] = value
    config = wave2_config(tmp_path, artifact_payloads=payloads)
    return run_wave2(tmp_path, config=config, **kwargs)


class TestWave2PauseChecks:
    """The three wave-2 checks S2 (#3961) owns: W2-03, W2-04, W2-05.

    Same posture as `TestWave2AbortedChecks`: the harness never runs in CI, so what
    CI proves is that each check would NOTICE a deployment that pauses badly. Every
    test starts from the passing fixture and bends exactly one thing.

    The bar these enforce is the one the story states first — a pause that reports
    `paused` while the run is still acting is worse than no pause at all — so most
    of these are written as "this defect must FAIL the check", not as happy paths.
    """

    def test_a_correct_deployment_passes_all_three(self, tmp_path: Path):
        results = run_wave2(tmp_path)

        for check_id in ("W2-03", "W2-04", "W2-05"):
            assert results[check_id].status == _mod.STATUS_PASSED, (
                check_id,
                results[check_id].message,
            )

    # ---- W2-03: the tool boundary (AC-P1) -------------------------------

    @pytest.mark.parametrize(
        "counter",
        ["new_admissions", "fixture_writes", "fixture_service_calls", "task_output_bytes"],
    )
    def test_any_nonzero_side_effect_during_the_hold_fails(
        self, tmp_path: Path, counter: str
    ):
        """The whole claim of AC-P1, one counter at a time.

        A single nonzero counter means `paused` was displayed over a run that was
        still writing files, calling services or producing output.
        """
        held = {**pause_artifact_payloads()["pause_boundary"]["held_interval"], counter: 1}

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"held_interval": held})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert counter in results["W2-03"].message

    def test_a_zero_length_hold_fails(self, tmp_path: Path):
        """A pause held for no measurable time cannot show side effects ceased."""
        held = {**pause_artifact_payloads()["pause_boundary"]["held_interval"], "duration_ms": 0}

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"held_interval": held})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "duration_ms" in results["W2-03"].message

    def test_counters_reported_by_the_agent_itself_fail(self, tmp_path: Path):
        """A paused agent reporting its own inactivity is the claim, not evidence."""
        held = {
            **pause_artifact_payloads()["pause_boundary"]["held_interval"],
            "observed_by": "agent",
        }

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"held_interval": held})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "observed_by" in results["W2-03"].message

    def test_confirming_paused_with_unsettled_work_fails(self, tmp_path: Path):
        """`paused` with a nonzero active tool count is the forbidden false claim."""
        results = run_wave2_with_pause(
            tmp_path,
            "pause_boundary",
            {"confirmed": {"state": "paused", "active_tool_count": 2}},
        )

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "active_tool_count" in results["W2-03"].message

    def test_admission_left_open_at_pause_requested_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(
            tmp_path, "pause_boundary", {"requested": {"admission_closed": False}}
        )

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "admission" in results["W2-03"].message

    @pytest.mark.parametrize("kind", ["long_running_bash", "delegated_task", "background_task"])
    def test_a_barrier_never_tested_on_the_hard_tools_fails(self, tmp_path: Path, kind: str):
        """The hard cases are the tool that outlives the settle wait and the work
        that continues behind a completed parent."""
        coverage = {**pause_artifact_payloads()["pause_boundary"]["tool_coverage"], kind: False}

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"tool_coverage": coverage})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert kind in results["W2-03"].message

    @pytest.mark.parametrize("case", ["untracked_activity", "hook_timeout"])
    def test_a_degradation_that_reported_paused_fails(self, tmp_path: Path, case: str):
        """Untracked activity and a timed-out hook must never yield `paused`."""
        degraded = dict(pause_artifact_payloads()["pause_boundary"]["degraded"])
        degraded[case] = {"state": "paused", "reason": "looked quiet"}

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"degraded": degraded})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert case in results["W2-03"].message

    @pytest.mark.parametrize("case", ["untracked_activity", "hook_timeout"])
    def test_a_degradation_without_a_reason_fails(self, tmp_path: Path, case: str):
        """An operator told only that the pause did not take cannot act."""
        degraded = dict(pause_artifact_payloads()["pause_boundary"]["degraded"])
        degraded[case] = {"state": "running", "reason": "   "}

        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"degraded": degraded})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "reason" in results["W2-03"].message

    def test_evidence_from_another_adapter_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"adapter_id": "echo"})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "echo" in results["W2-03"].message

    def test_evidence_from_another_sdk_version_fails(self, tmp_path: Path):
        """The barrier rests on observed SDK behaviour, so a version is not fungible."""
        results = run_wave2_with_pause(tmp_path, "pause_boundary", {"sdk_version": "0.3.219"})

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "0.3.219" in results["W2-03"].message

    def test_a_run_that_asked_permission_per_tool_fails(self, tmp_path: Path):
        """Such a run appears contained whether or not the barrier works."""
        results = run_wave2_with_pause(
            tmp_path, "pause_boundary", {"permission_mode": "default"}
        )

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "permission_mode" in results["W2-03"].message

    def test_a_barrier_proven_without_the_spill_hooks_fails(self, tmp_path: Path):
        """Composition is where a PreToolUse addition could displace another hook."""
        results = run_wave2_with_pause(
            tmp_path, "pause_boundary", {"spill_hooks_composed": False}
        )

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "spill_hooks_composed" in results["W2-03"].message

    def test_a_deployment_that_disables_pause_fails_w2_03(self, tmp_path: Path):
        """A green experiment beside a build that disables the verb is a mismatch.

        This is the state of the tree today, and the check must not call it a pass:
        the barrier is proven while the capability stays off pending the
        authorization intersection. W2-02's own cross-check is bent in step, so this
        test isolates W2-03's live-surface read rather than tripping both.
        """
        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
        }
        payloads["neutral_contract"] = neutral_contract_payload(implemented_verbs=[])
        client = wave2_gateway_stub(
            state_capabilities={verb: False for verb in _mod.CONTROL_VERBS}
        )

        results = run_wave2(
            tmp_path,
            config=wave2_config(tmp_path, artifact_payloads=payloads),
            client=client,
        )

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "pause capability" in results["W2-03"].message

    def test_a_state_read_omitting_active_tool_count_fails(self, tmp_path: Path):
        """The gateway must report runtime truth from the barrier.

        Without this field a reader infers containment from invocation status, which
        is the inference AC-P1 exists to replace.
        """
        client = wave2_gateway_stub(state_omit=("active_tool_count",))

        results = run_wave2(tmp_path, client=client)

        assert results["W2-03"].status == _mod.STATUS_FAILED
        assert "active_tool_count" in results["W2-03"].message

    # ---- W2-04: same-execution resume (AC-P2) ---------------------------

    def test_a_changed_session_fails(self, tmp_path: Path):
        """A new session is a restart, not a resume."""
        results = run_wave2_with_pause(
            tmp_path, "pause_resume", {"session_id_after": "a-different-session"}
        )

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "session" in results["W2-04"].message

    def test_a_changed_attempt_fails(self, tmp_path: Path):
        """Interrupt-and-new-turn is explicitly not a successful pause/resume."""
        results = run_wave2_with_pause(
            tmp_path, "pause_resume", {"attempt_id_after": "attempt-2"}
        )

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "attempt" in results["W2-04"].message

    @pytest.mark.parametrize("field", ["session_id", "attempt_id"])
    @pytest.mark.parametrize("value", [None, "", "   ", 17, []])
    def test_missing_or_malformed_resume_identity_fails(
        self, tmp_path: Path, field, value
    ):
        results = run_wave2_with_pause(
            tmp_path,
            "pause_resume",
            {f"{field}_before": value, f"{field}_after": value},
        )
        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert field.split("_")[0] in results["W2-04"].message

    def test_an_interrupt_call_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(tmp_path, "pause_resume", {"interrupt_called": True})

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "interrupt" in results["W2-04"].message

    def test_a_replayed_prompt_fails(self, tmp_path: Path):
        """A replayed prompt duplicates every side effect already performed."""
        results = run_wave2_with_pause(
            tmp_path, "pause_resume", {"initial_prompt_replayed": True}
        )

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "replayed" in results["W2-04"].message

    def test_a_double_release_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(tmp_path, "pause_resume", {"released_count": 2})

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "released" in results["W2-04"].message

    def test_dropping_the_parked_tools_fails(self, tmp_path: Path):
        """A pause that discards the model's held work is not a pause.

        Zero admitted-after-resume is the failure the first live experiment's own
        assertion got wrong: the barrier parks tools, it does not deny them.
        """
        results = run_wave2_with_pause(
            tmp_path, "pause_resume", {"held_tools_admitted_after_resume": 0}
        )

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert "held_tools_admitted_after_resume" in results["W2-04"].message

    def test_a_run_that_cannot_finish_after_resume_fails(self, tmp_path: Path):
        """A pause that leaves the run unable to finish is an abort."""
        results = run_wave2_with_pause(tmp_path, "pause_resume", {"task_completed": False})

        assert results["W2-04"].status == _mod.STATUS_FAILED

    def test_lost_history_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(
            tmp_path, "pause_resume", {"prior_history_preserved": False}
        )

        assert results["W2-04"].status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("case", ["resume_before_pause", "repeated_resume"])
    def test_an_unserialized_race_fails(self, tmp_path: Path, case: str):
        """Two transitions each observing the pre-state is how a pause is released
        twice or confirmed after cancellation."""
        races = dict(pause_artifact_payloads()["pause_resume"]["races"])
        races[case] = {"serialized": False, "errored": False}

        results = run_wave2_with_pause(tmp_path, "pause_resume", {"races": races})

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert case in results["W2-04"].message

    @pytest.mark.parametrize("case", ["resume_before_pause", "repeated_resume"])
    def test_a_race_that_errored_fails(self, tmp_path: Path, case: str):
        """An operator double-clicking resume is ordinary and must not fail the run."""
        races = dict(pause_artifact_payloads()["pause_resume"]["races"])
        races[case] = {"serialized": True, "errored": True}

        results = run_wave2_with_pause(tmp_path, "pause_resume", {"races": races})

        assert results["W2-04"].status == _mod.STATUS_FAILED
        assert case in results["W2-04"].message

    # ---- W2-05: expiry, visibility, clamp (AC-P3/P5/P6) -----------------

    def test_a_pause_released_without_ever_resolving_fails(self, tmp_path: Path):
        """The B4 defect found in review of this story.

        "pausing…" for the whole budget, then a silent resume, with the operator
        never told the pause did not take.
        """
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"resolved_before_release": False}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "without first reporting" in results["W2-05"].message

    def test_an_unbounded_pause_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"auto_resumed": False})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("count", [0, 2])
    def test_the_wrong_number_of_expiry_annotations_fails(self, tmp_path: Path, count: int):
        """Zero leaves the model on a stale belief; more than one is transcript noise."""
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"annotation_count": count}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "annotation" in results["W2-05"].message

    def test_an_extra_assistant_turn_on_expiry_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"extra_assistant_turn": True}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_a_provider_shaped_expiry_annotation_fails(self, tmp_path: Path):
        """Only the Claude adapter may translate it into `shouldQuery:false`."""
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"neutral_annotation": False}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED

    @pytest.mark.parametrize(
        "watchdog", ["pod_killed", "idle_retry_fired", "exit_watchdog_fired"]
    )
    def test_a_watchdog_firing_during_a_valid_pause_fails(
        self, tmp_path: Path, watchdog: str
    ):
        """Pausing a run must not become a way to lose it."""
        results = run_wave2_with_pause(tmp_path, "pause_expiry", {watchdog: True})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_a_silent_pause_fails(self, tmp_path: Path):
        """Going silent is the one thing a pause must not do — silence is what a
        hung run looks like."""
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"heartbeats_during_pause": 0}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "heartbeats_during_pause" in results["W2-05"].message

    def test_a_pause_indistinguishable_from_a_stall_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"paused_distinguishable_from_stalled": False}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_lost_spill_output_fails(self, tmp_path: Path):
        results = run_wave2_with_pause(
            tmp_path, "pause_expiry", {"spill_output_preserved": False}
        )

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_a_pause_that_consumes_the_finalization_margin_fails(self, tmp_path: Path):
        """A pause leaving no room to write a terminal state ends the run
        indistinguishably from a pod that vanished."""
        clamp = {
            **pause_artifact_payloads()["pause_expiry"]["deadline_clamp"],
            "granted_ms": 2_050_000,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"deadline_clamp": clamp})

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "finalization margin" in results["W2-05"].message

    def test_accepting_a_nonpositive_budget_fails(self, tmp_path: Path):
        """A pause that expires the instant it begins looks like no pause at all."""
        clamp = {
            **pause_artifact_payloads()["pause_expiry"]["deadline_clamp"],
            "nonpositive_budget_rejected": False,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"deadline_clamp": clamp})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_an_unexercised_hook_bound_fails(self, tmp_path: Path):
        """It is the one bound the adapter does not enforce itself."""
        hook = {
            **pause_artifact_payloads()["pause_expiry"]["held_hook_timeout"],
            "exercised": False,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"held_hook_timeout": hook})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_a_hook_bound_shorter_than_the_pause_budget_fails(self, tmp_path: Path):
        """Otherwise the budget is decorative and every long pause ends as an
        aborted tool."""
        hook = {
            **pause_artifact_payloads()["pause_expiry"]["held_hook_timeout"],
            "hook_timeout_seconds": 60,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"held_hook_timeout": hook})

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "does not exceed" in results["W2-05"].message

    def test_a_timed_out_hook_still_reporting_paused_fails(self, tmp_path: Path):
        """The parked tool was released by the CLI, so containment has lapsed."""
        hook = {
            **pause_artifact_payloads()["pause_expiry"]["held_hook_timeout"],
            "state": "paused",
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"held_hook_timeout": hook})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_an_abort_that_flushes_held_work_fails(self, tmp_path: Path):
        """An abort that admits its parked tools on the way out runs exactly the
        side effects the operator aborted to prevent."""
        cancel = {
            **pause_artifact_payloads()["pause_expiry"]["cancellation"],
            "held_work_admitted": True,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"cancellation": cancel})

        assert results["W2-05"].status == _mod.STATUS_FAILED
        assert "admitted work" in results["W2-05"].message

    def test_cancellation_emitting_a_resume_annotation_fails(self, tmp_path: Path):
        """An aborted run is not a resumed one."""
        cancel = {
            **pause_artifact_payloads()["pause_expiry"]["cancellation"],
            "annotation_emitted": True,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"cancellation": cancel})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    def test_held_work_left_unresolved_on_cancellation_fails(self, tmp_path: Path):
        """Neither admitted nor denied leaves those calls hanging."""
        cancel = {
            **pause_artifact_payloads()["pause_expiry"]["cancellation"],
            "held_work_denied": False,
        }

        results = run_wave2_with_pause(tmp_path, "pause_expiry", {"cancellation": cancel})

        assert results["W2-05"].status == _mod.STATUS_FAILED

    # ---- shape and absence ---------------------------------------------

    @pytest.mark.parametrize(
        "artifact,field,check",
        [
            ("pause_boundary", "held_interval", "W2-03"),
            ("pause_boundary", "requested", "W2-03"),
            ("pause_boundary", "tool_coverage", "W2-03"),
            ("pause_boundary", "confirmed", "W2-03"),
            ("pause_boundary", "degraded", "W2-03"),
            ("pause_resume", "races", "W2-04"),
            ("pause_expiry", "deadline_clamp", "W2-05"),
            ("pause_expiry", "held_hook_timeout", "W2-05"),
            ("pause_expiry", "cancellation", "W2-05"),
        ],
    )
    def test_a_scalar_where_an_object_belongs_fails(
        self, tmp_path: Path, artifact: str, field: str, check: str
    ):
        """`REQUIRED_ARTIFACT_KEYS` guarantees presence, not shape.

        A predicate that let a string through here would raise AttributeError from
        deep inside itself, which tells an operator far less than a named field.
        """
        results = run_wave2_with_pause(tmp_path, artifact, {field: "true"})

        assert results[check].status == _mod.STATUS_FAILED
        assert field in results[check].message

    @pytest.mark.parametrize(
        "artifact,field,check",
        [
            ("pause_boundary", "tool_coverage", "W2-03"),
            ("pause_boundary", "degraded", "W2-03"),
            ("pause_expiry", "deadline_clamp", "W2-05"),
            ("pause_expiry", "held_hook_timeout", "W2-05"),
            ("pause_expiry", "cancellation", "W2-05"),
        ],
    )
    def test_an_absent_optional_object_fails_rather_than_passing(
        self, tmp_path: Path, artifact: str, field: str, check: str
    ):
        """These are read with `.get()`, so absence must not read as satisfied."""
        results = run_wave2_with_pause(tmp_path, artifact, {field: None})

        assert results[check].status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("artifact,check", [
        ("pause_boundary", "W2-03"),
        ("pause_resume", "W2-04"),
        ("pause_expiry", "W2-05"),
    ])
    def test_an_undeclared_pause_artifact_is_not_run_not_passed(
        self, tmp_path: Path, artifact: str, check: str
    ):
        """The honesty property for the operator who has not recorded the evidence.

        This is the state the harness was in before the runbook declared these
        three: a missing artifact must say so, never pass.
        """
        config = wave2_config(tmp_path)
        del config["artifacts"][artifact]

        results = run_wave2(tmp_path, config=config)

        assert results[check].status == _mod.STATUS_NOT_RUN
        assert artifact in results[check].message

    def test_no_pause_check_leaks_a_token_into_its_evidence(self, tmp_path: Path):
        results = run_wave2(tmp_path)

        rendered = json.dumps(
            {cid: results[cid].to_evidence() for cid in ("W2-03", "W2-04", "W2-05")}
        )
        assert OWNER_TOKEN not in rendered
        for token in IDENTITY_ENV.values():
            assert token not in rendered


@pytest.mark.parametrize("artifact,field,value,check", [
    ("harness_neutrality", "adapter_a", {}, "W2-06"),
    ("aborted_counters", "seeded_aborted", 0, "W2-07"),
    ("aborted_counters", "seeded_aborted", True, "W2-07"),
    ("vocabulary_parity", "suites", {}, "W2-08"),
    ("vocabulary_parity", "suites", {"arbitrary": "passed"}, "W2-08"),
    ("stats_schema_keys", "levels", {}, "W2-09"),
    ("stats_schema_keys", "levels", {"today": []}, "W2-09"),
])
def test_wave2_rejects_empty_or_vacuous_evidence(tmp_path, artifact, field, value, check):
    payloads = {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
    }
    payloads[artifact][field] = value
    results = run_wave2(tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads))
    assert results[check].status == _mod.STATUS_FAILED



def test_combined_wave2_runs_every_story_and_can_now_reach_a_complete_report(tmp_path):
    """The state #5825 exists to make reachable: ten of ten, on complete evidence.

    Before this defect was fixed the arithmetic could not add up — W2-01 and W2-10
    had no predicate, so the wave was permanently 8/10 and evaluation #3968 could
    never close. This asserts the counting end to end: every ID answered, every one
    passed, and `report_is_passing` agreeing.

    Note what is being demonstrated and what is not. This is a complete *fixture*,
    which proves the harness would accept a correct deployment. It is not live
    evidence, and merging it does not accept wave 2 — the real run belongs to the
    maintainer. The companion property, that nothing short of this reaches a pass,
    is `TestNoPassingWave2WithoutTenChecksAndVerifiedCleanup`.
    """
    config = wave2_config(tmp_path)
    results = run_wave2(tmp_path, config=config)
    passed = {key for key, value in results.items() if value.status == _mod.STATUS_PASSED}
    # All ten: S3's W2-02, S2's W2-03..W2-05, S5's W2-06..W2-09 and #5825's
    # W2-01 preflight plus W2-10 verified cleanup.
    assert passed == {spec.check_id for spec in _mod.WAVE2_CHECKS}
    assert not [key for key, value in results.items() if value.status == _mod.STATUS_NOT_RUN]
    report = _mod.build_report(config, list(results.values()), cleanup_ok=True, wave=2,
                              expected_ids=tuple(spec.check_id for spec in _mod.WAVE2_CHECKS))
    assert report["required"] == 10
    assert report["passed"] == 10
    assert report["not_run"] == 0
    assert report["failed"] == 0
    assert _mod.report_is_passing(report)


# ===========================================================================
# W2-01 (consolidated preflight) and W2-10 (verified cleanup and security
# recheck), delivered by #5825 to close evaluation #3968's last gap.
#
# The posture is the same as every other driver test here and worth restating,
# because it is what makes these tests worth their length: the harness never
# runs in CI, so nothing below observes a deployment. What CI proves is that
# each check would NOTICE a deployment being wrong. Every test starts from the
# complete passing fixture above and bends exactly one observation, so a failure
# names one defect rather than being ambiguous between several.
#
# A check that cannot fail is worse than a missing one, because it looks like
# evidence. These are the tests that establish these two can.
# ===========================================================================


def run_w2_01(tmp_path: Path, *, preflight=None, config=None, client=None, graph=None):
    """Drive W2-01 alone and return its CheckResult.

    `preflight` replaces the W2-01 artifact payload; pass `False` to omit the
    artifact from the config entirely, which is the distinct "could not look" case.
    `graph` replaces the commit graph W2-01's containment queries are answered from,
    which is how a test models a deployment that does not contain a story.
    """
    payloads = {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
        **wave2_only_artifact_payloads(),
    }
    if preflight is False:
        payloads.pop("wave2_preflight")
    elif preflight is not None:
        payloads["wave2_preflight"] = preflight
    config = config or wave2_config(tmp_path, artifact_payloads=payloads)
    results = run_wave2(tmp_path, config=config, client=client, graph=graph)
    return results["W2-01"]


def run_w2_10(
    tmp_path: Path,
    *,
    capture_artifact=None,
    verification=None,
    cleanup=_UNSET,
    capture=_UNSET,
    teardown=_UNSET,
    resources=None,
    runner=None,
    config=None,
    client=None,
):
    """Drive W2-10 alone and return its CheckResult.

    `capture_artifact` replaces the pre-teardown `security_capture` artifact and
    `verification` the post-teardown `teardown_verification` one (`False` omits
    either). `cleanup` replaces the harness's own deletion record, `capture` its own
    pre-teardown observations, and `teardown` its own record of invoking the fixture's
    resource teardown (`None` for each models that half never having happened).
    `resources`/`runner` change what the teardown command actually DOES. Defaults
    model a correct teardown of a correct fixture.

    A bent `verification` is modelled as what the teardown command WROTE, not as a
    file sitting on disk beforehand. That distinction matters: the seam refuses an
    artifact whose bytes did not change across the teardown, so pre-seeding a bent
    payload would trip the prefilled-artifact rule and every test below would fail for
    that one reason instead of the defect it names. Writing it from the runner keeps
    each test bending exactly one observation of an otherwise honest run. The
    prefilled case gets its own explicit test rather than being an artefact of how
    this helper stages files.

    The two artifacts are separate parameters rather than one because they are the
    two sides of the teardown boundary, and a test that bends the security half must
    be unable to accidentally perturb the absence half.
    """
    payloads = {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
        **wave2_only_artifact_payloads(),
    }
    if capture_artifact is False:
        payloads.pop("security_capture")
    elif capture_artifact is not None:
        payloads["security_capture"] = capture_artifact
    if verification is False:
        # An absent artifact: the teardown must not write one either, or the file
        # would exist after all and the "could not look" case would not be modelled.
        payloads.pop("teardown_verification")
    config = config or wave2_config(tmp_path, artifact_payloads=payloads)
    if runner is None and (verification is not None or "teardown_verification" not in payloads):
        resources = resources if resources is not None else FixtureResources()
        runner = teardown_runner_for(
            tmp_path,
            config,
            resources,
            write_verification=verification is not False,
            verification=verification if verification is not False else None,
        )
    # `cleanup`, `capture` and `teardown` are forwarded UNRESOLVED so the default path
    # runs the real `run_cleanup`, the real capture and the real teardown seam against
    # the shared store and the fixture resources, in order. Materializing a cleanup
    # record here instead would leave the store's rows in place while the record called
    # them deleted — the two-worlds fixture again.
    results = run_wave2(
        tmp_path,
        config=config,
        client=client,
        cleanup=cleanup,
        capture=capture,
        teardown=teardown,
        resources=resources,
        runner=runner,
    )
    return results["W2-10"]


class TestWave2PreflightIsDiscriminating:
    """W2-01: does this evidence describe the build under review?

    The reason this check is `Gate/regression` rather than an acceptance ID: if it
    is wrong, the other nine checks are correct observations of the WRONG THING,
    which reads exactly like a passing wave. So the failure mode it guards against
    is not "a check went red" but "nine checks went green about nothing".
    """

    def test_a_complete_preflight_passes(self, tmp_path: Path):
        result = run_w2_01(tmp_path)

        assert result.status == _mod.STATUS_PASSED, result.message

    def test_an_absent_artifact_is_not_run_not_failed_and_not_passed(self, tmp_path: Path):
        """"Could not look" is a third thing, and the distinction is the contract.

        `not_run` is nonzero, so this is never a false green — but it must not be
        `failed` either, because a missing recording is the operator's omission
        rather than evidence the deployment is wrong, and telling them apart is
        what makes the report actionable.
        """
        result = run_w2_01(tmp_path, preflight=False)

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "wave2_preflight" in result.message

    def test_a_present_but_incomplete_artifact_fails_rather_than_not_running(
        self, tmp_path: Path
    ):
        """Half-filled is a different thing from absent, and it is a failure.

        The operator looked and recorded a partial answer. Reporting that as
        `not_run` would invite them to "fix the omission" by supplying the file
        they already supplied.
        """
        payload = wave2_preflight_payload()
        del payload["deployed_components"]

        result = run_w2_01(tmp_path, preflight=payload)

        assert result.status == _mod.STATUS_FAILED
        assert "deployed_components" in result.message

    # ---- (1) prior-wave compatibility ----------------------------------

    @pytest.mark.parametrize(
        "field,value,expected_in_message",
        [
            # Not accepted at all.
            ("accepted", False, "not recorded as accepted"),
            ("accepted", None, "not recorded as accepted"),
            # Accepted, but the evidence belongs to another evaluation.
            ("evaluation", "3968", "not wave 1's acceptance"),
            # Accepted "as a whole" while short of its own bar. `accepted: true`
            # alone cannot distinguish a 10/10 run from a 9/10 one, which is why
            # the counts are asserted separately.
            ("passed", 9, "not an accepted baseline"),
            ("required", 9, "not an accepted baseline"),
            # Accepted with its fixture left enabled — the DP-INV-1 state.
            ("cleanup_ok", False, "did not complete cleanup"),
            # Provenance that does not identify a build.
            ("revision", "main", "not a full 40-character git SHA"),
            ("revision", "abc1234", "not a full 40-character git SHA"),
            ("revision", None, "not a full 40-character git SHA"),
            # Accepted, pinned, and anonymous. A revision says which build was
            # evaluated; the run identity is what lets a reviewer retrieve the report
            # instead of taking this summary of it on trust.
            ("run_id", None, "records no 'run_id'"),
            ("run_id", "", "records no 'run_id'"),
        ],
    )
    def test_incompatible_or_absent_wave_one_evidence_fails(
        self, tmp_path: Path, field, value, expected_in_message
    ):
        wave1 = wave2_preflight_payload()["wave1_evidence"]
        wave1[field] = value

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(wave1_evidence=wave1))

        assert result.status == _mod.STATUS_FAILED
        assert expected_in_message in result.message, result.message

    def test_wave_one_evidence_that_is_not_an_object_fails(self, tmp_path: Path):
        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(wave1_evidence="accepted")
        )

        assert result.status == _mod.STATUS_FAILED
        assert "must be an object" in result.message

    @pytest.mark.parametrize(
        "asserted",
        [
            {"compatible_with_current_revision": True},
            {"compatible_with_current_revision": "yes"},
            {"contained_in": contained_in_claim()},
        ],
    )
    def test_an_asserted_wave_one_compatibility_claim_is_not_evidence(
        self, tmp_path: Path, asserted
    ):
        """Root's third finding, on the prior-wave half.

        Both spellings of the same circularity. `compatible_with_current_revision:
        true` is the CONCLUSION this section is supposed to reach, and so is a
        `contained_in` map carrying `is_ancestor: true` — renaming the assertion did
        not turn it into evidence, which is precisely what the review said about my
        previous attempt. The positive-looking value is the one under test, because it
        is the one that used to pass.

        Neither can help now: the harness asks git, and wave 1's revision is absent
        from the graph this parametrization runs against. So the claim in the artifact
        is inert — which is the property, not an incidental detail.
        """
        wave1 = wave2_preflight_payload()["wave1_evidence"]
        wave1.update(asserted)
        wave1["revision"] = INVENTED_REVISION

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(wave1_evidence=wave1))

        assert result.status == _mod.STATUS_NOT_RUN
        assert "is not a commit in this checkout" in result.message, result.message
        assert INVENTED_REVISION in result.message

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_wave_one_evidence_not_contained_in_a_deployed_component_fails(
        self, tmp_path: Path, component
    ):
        """The subtle case: a TRUE observation of a build this one has since changed.

        Stale rather than wrong — wave 1 really was accepted, on a revision this
        deployment no longer contains. Expressed as a GRAPH in which the component
        branched before wave 1's accepted commit, because that is what the situation
        actually is; the artifact has no field left that could express it, which is the
        point of computing containment instead of reading it.

        Parametrized over both components because containment in one and absence from
        the other is the half-deployment shape, and a check reading only the worker
        would pass a gateway that dropped it.
        """
        result = run_w2_01(tmp_path, graph=graph_without(component, WAVE1_ACCEPTED_REVISION))

        assert result.status == _mod.STATUS_FAILED
        assert "is not contained in the deployed" in result.message
        assert component in result.message

    # ---- (2) merged revisions, versions and CI -------------------------

    @pytest.mark.parametrize("story", sorted(_mod.WAVE2_REQUIRED_STORIES))
    def test_an_unmerged_contributing_story_fails(self, tmp_path: Path, story):
        """Wave 2's checks span three stories; evidence gathered while one is
        unmerged describes a build that does not implement the wave.

        Parametrized over the stories rather than testing one, because "S2 is
        checked and the others are assumed" is exactly the gap that lets a story
        land unverified.
        """
        merged = wave2_preflight_payload()["merged_revisions"]
        merged[story]["merged"] = False

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(merged_revisions=merged)
        )

        assert result.status == _mod.STATUS_FAILED
        assert story in result.message
        assert "not recorded as merged" in result.message

    @pytest.mark.parametrize("story", sorted(_mod.WAVE2_REQUIRED_STORIES))
    def test_a_missing_story_revision_fails(self, tmp_path: Path, story):
        merged = wave2_preflight_payload()["merged_revisions"]
        del merged[story]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(merged_revisions=merged)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "no merged revision recorded" in result.message

    @pytest.mark.parametrize("bad_revision", ["main", "a" * 39, "A" * 40, "", None, 40])
    def test_provenance_that_does_not_pin_a_commit_fails(self, tmp_path: Path, bad_revision):
        """Malformed provenance, enumerated.

        A branch name, a short SHA, an uppercase SHA, empty, absent and a non-string
        each have to fail: every one of them names something other than exactly one
        commit, and the whole purpose of the field is to name exactly one commit.
        """
        merged = wave2_preflight_payload()["merged_revisions"]
        merged["S2"]["revision"] = bad_revision

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(merged_revisions=merged)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a full 40-character git SHA" in result.message

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_a_merged_story_absent_from_the_running_build_fails(
        self, tmp_path: Path, component
    ):
        """Merged is necessary and not sufficient: merged-but-not-deployed.

        The stories landed on `main` and the running build branched before them, which
        is what a deployment nobody refreshed looks like. Both components, because
        containment in one and absence from the other is the half-deployment shape and
        a check reading only the worker would pass a gateway that lacks the wave.
        """
        result = run_w2_01(tmp_path, graph=graph_without(component, WAVE2_REVISION))

        assert result.status == _mod.STATUS_FAILED
        assert "is not contained in the deployed" in result.message
        assert component in result.message

    @pytest.mark.parametrize(
        "asserted",
        [{"ci_passed": True}, {"contained_in": contained_in_claim()}],
    )
    def test_asserted_story_claims_are_not_containment_evidence(
        self, tmp_path: Path, asserted
    ):
        """Neither a green merge nor a written-down ancestry says the story is running.

        `ci_passed: true` says the merge was gated — a genuinely different question
        from whether it is deployed, and an earlier revision took it as sufficient. A
        `contained_in` map asserting `is_ancestor: true` is worse: it states the
        conclusion. Both are in the artifact here, and the check reaches its answer
        without consulting either, so the story still fails for the real reason.
        """
        merged = wave2_preflight_payload()["merged_revisions"]
        merged["S5"].update(asserted)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(merged_revisions=merged),
            graph=graph_without("worker", WAVE2_REVISION),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "is not contained in the deployed" in result.message

    def test_every_deployed_component_is_queried_for_every_story(self, tmp_path: Path):
        """Both directions of reconciliation, now that the deployed set is authoritative.

        The artifact no longer supplies a containment map, so it can neither omit a
        deployed component nor invent an undeployed one — the harness iterates what is
        deployed and asks git about each. This pins that: one query pair per
        (story or prior wave) × component, plus the per-gate head-containment query the
        pull_request path adds, and nothing else.
        """
        graph = CommitGraph()

        result = run_w2_01(tmp_path, graph=graph)

        assert result.status == _mod.STATUS_PASSED
        asked = {
            (argv[-2], argv[-1]) for argv in graph.queries if "merge-base" in argv
        }
        subjects = {WAVE1_ACCEPTED_REVISION, WAVE2_REVISION}
        deployed = {DEPLOYED_WORKER_REVISION, DEPLOYED_GATEWAY_REVISION}
        # The gates in the default fixture are `pull_request` runs, where the job builds
        # a merge of the run's head — so each gate also asks whether its head is
        # contained in what it checked out. Here both are the tested revision, which
        # `--is-ancestor` answers reflexively.
        tested = required_ci_gates()[_mod.WAVE2_REQUIRED_CI_GATES[0]]["tested_revision"]
        assert asked == {(s, d) for s in subjects for d in deployed} | {(tested, tested)}

    def test_an_invented_story_revision_cannot_be_confirmed(self, tmp_path: Path):
        """The internally-consistent-but-invented case, on the story half.

        A well-formed SHA nobody ever committed. Every syntax check passes and every
        self-comparison agrees; git has simply never heard of it. That is NOT RUN
        rather than FAILED — the harness could not look, and reporting a deployment
        defect it did not observe would be the mirror of the false pass.
        """
        merged = wave2_preflight_payload()["merged_revisions"]
        merged["S2"]["revision"] = INVENTED_REVISION

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(merged_revisions=merged)
        )

        assert result.status == _mod.STATUS_NOT_RUN
        assert "is not a commit in this checkout" in result.message
        assert INVENTED_REVISION in result.message

    @pytest.mark.parametrize(
        ("broken", "expected_in_message"),
        [
            ({"fail_with": 128}, "exited 128"),
            ({"raises": FileNotFoundError("git")}, "git could not be run"),
        ],
    )
    def test_a_checkout_that_cannot_answer_is_not_run_rather_than_failed(
        self, tmp_path: Path, broken, expected_in_message
    ):
        """git failing to answer is a gap in the evidence, not a defect in the build.

        A broken checkout and an absent git both have to come back NOT RUN. The
        alternative is worse than a false pass in one specific way: it would report a
        stale deployment that is not stale, and an operator who trusted it would
        redeploy a correct environment to chase an evaluator's error.
        """
        result = run_w2_01(tmp_path, graph=CommitGraph(**broken))

        assert result.status == _mod.STATUS_NOT_RUN
        assert expected_in_message in result.message

    def test_a_deployment_newer_than_the_merge_commits_passes(self, tmp_path: Path):
        """Root's second finding, stated as the property rather than as a negative.

        The deployed revisions in this fixture are NOT equal to any story's merge
        commit — they are newer, which is what a correct deployment is. An earlier
        revision required `recheck_revision == merged_revisions.S2.revision`, so this
        entirely correct topology failed, and the only way to satisfy it would have
        been to redeploy an old merge commit to please the evaluator.
        """
        preflight = wave2_preflight_payload()
        deployed = {
            entry["revision"] for entry in preflight["deployed_components"].values()
        }
        merge_commits = {
            entry["revision"] for entry in preflight["merged_revisions"].values()
        }
        # The premise: this fixture really is the newer-deployment case.
        assert not (deployed & merge_commits)
        assert WAVE1_ACCEPTED_REVISION not in deployed

        result = run_w2_01(tmp_path, preflight=preflight)

        assert result.status == _mod.STATUS_PASSED, result.message

    @pytest.mark.parametrize(
        "field,value,expected_in_message",
        [
            ("protocol_version", 2, "control protocol version"),
            ("protocol_version", "1", "control protocol version"),
            ("adapter_id", "echo", "records adapter"),
            ("sdk_version", "0.3.219", "the lockfile pins"),
        ],
    )
    def test_a_contract_version_mismatch_fails(
        self, tmp_path: Path, field, value, expected_in_message
    ):
        """The gateway peer, the adapter and this harness must move together.

        `protocol_version: "1"` is in the table deliberately: the string and the
        integer are equal to a human reader and unequal to the contract, and a
        recording that stringified the version is the realistic form of this defect.
        """
        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(**{field: value}))

        assert result.status == _mod.STATUS_FAILED
        assert expected_in_message in result.message

    @pytest.mark.parametrize("package", _mod.WAVE2_REQUIRED_PACKAGES)
    def test_an_unpinned_package_version_fails(self, tmp_path: Path, package):
        """The control path spans two runtimes; an unrecorded version on either
        side is a contract nobody pinned."""
        packages = dict(wave2_preflight_payload()["package_versions"])
        del packages[package]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(package_versions=packages)
        )

        assert result.status == _mod.STATUS_FAILED
        assert package in result.message

    @pytest.mark.parametrize("empty_version", ["", "   "])
    def test_a_blank_package_version_fails(self, tmp_path: Path, empty_version):
        """Present-but-blank is the form a templated recording takes when the
        substitution did not happen, and it must not read as pinned."""
        packages = dict(wave2_preflight_payload()["package_versions"])
        packages[_mod.WAVE2_REQUIRED_PACKAGES[0]] = empty_version

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(package_versions=packages)
        )

        assert result.status == _mod.STATUS_FAILED

    # ---- (3) deployed identity, per component -------------------------

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_an_image_not_built_from_the_deployed_revision_fails(
        self, tmp_path: Path, component
    ):
        """Root's third finding: the source-to-image link, without which a digest
        establishes nothing.

        The component records a running revision and a well-formed digest, but the
        image was BUILT FROM some other commit — so what is running traces to source
        nobody reviewed. Both sides are parametrized because one component checked and
        the other assumed is the half-deployment shape, and that asymmetry is exactly
        what makes a stale image survive review.

        The archived run document agrees with the wrong revision, which is the honest
        shape of this defect: the build really did consume that commit. So there is
        nothing internally inconsistent to catch, and the check has to notice the thing
        that is actually wrong — a running image tracing to unreviewed source.
        """
        other = STALE_BRANCH_POINT
        components = deployed_components()
        components[component]["source_revision"] = other
        components[component]["build_record"] = build_record(
            component, other, components[component]["image_digest"]
        )

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert component in result.message
        assert "was built from source revision" in result.message

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    @pytest.mark.parametrize("key", _mod.DEPLOYED_COMPONENT_KEYS)
    def test_an_incomplete_deployed_identity_fails(self, tmp_path: Path, component, key):
        """Every one of the three facts, on every component.

        Parametrized over the full product rather than one example: a missing
        `source_revision` on the gateway is a different blind spot from a missing
        `image_digest` on the worker, and a check covering some of them would leave
        the rest unexamined.
        """
        components = deployed_components()
        del components[component][key]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    # ---- (1b) the build record: provenance rather than self-agreement ----
    #
    # Root's finding 3, stated as the discriminating question: does this pass because
    # something was RETRIEVED, or because two fields the operator wrote agree? Every
    # test below keeps the record internally consistent and removes only the retrieval,
    # because "well-formed, self-agreeing, and not linked to anything retrievable" is
    # the case the review asked for — not a mismatched string.

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_a_self_agreeing_component_with_no_build_record_fails(
        self, tmp_path: Path, component
    ):
        """The exact shape that used to pass: `revision == source_revision`, valid digest.

        Nothing here is malformed and nothing disagrees. It is the pre-#5825 record in
        full, and the only thing missing is any evidence that a build ever happened —
        which is precisely why it must not pass. Two equal recorded strings establish
        that the operator wrote one SHA twice.
        """
        components = deployed_components()
        del components[component]["build_record"]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "build_record" in result.message

    @pytest.mark.parametrize("key", _mod.BUILD_RECORD_KEYS)
    def test_a_build_record_missing_any_link_fails(self, tmp_path: Path, key):
        """Each link in the chain, separately.

        Parametrized over the whole key set rather than one example: a record without
        `registry_digest` cannot say what is being served, one without `run_url` cannot
        be opened, and one without `raw` is a summary of a document nobody archived.
        Those are different blind spots, and covering one would leave the others.
        """
        components = deployed_components()
        del components["worker"]["build_record"][key]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize("truthy", [True, 1, ["run"], {"id": 1}])
    def test_a_build_run_identity_that_is_not_a_string_fails(self, tmp_path: Path, truthy):
        """An arbitrary truthy value satisfies a presence check and identifies nothing.

        The literal defect root named on the CI half, tested here on the build half
        too: a bare `if not record.get(key)` accepts `True`, and `True` is not a run
        anybody can retrieve.
        """
        components = deployed_components()
        components["worker"]["build_record"]["build_id"] = truthy

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "must be a string identifying the build run" in result.message

    @pytest.mark.parametrize(
        "not_a_location", ["the CI run", "adp/actions/runs/1", "http://example.invalid/1"]
    )
    def test_a_build_run_url_that_cannot_be_opened_fails(
        self, tmp_path: Path, not_a_location
    ):
        """The field is there so a reviewer can go and read the run.

        A prose description, a bare path and a plaintext URL all fail: the first two
        are not locations at all, and the point of requiring https is that the value
        has to be a fetchable reference rather than a plausible-looking string.
        """
        components = deployed_components()
        components["worker"]["build_record"]["build_url"] = not_a_location

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not an https URL" in result.message

    @pytest.mark.parametrize("document", _mod.BUILD_RECORD_RAW_DOCUMENTS)
    @pytest.mark.parametrize("key", _mod.RAW_METADATA_KEYS)
    def test_an_archived_document_missing_its_own_provenance_fails(
        self, tmp_path: Path, document, key
    ):
        """An archive has to say how it was obtained, when, and what came back.

        Without `command` a reviewer cannot re-retrieve it; without `retrieved_at` a
        document carried over from a previous evaluation is invisible; without `body`
        the archive is a claim that an archive exists. All three documents, because the
        build, its log and the registry read are independently forgeable.
        """
        components = deployed_components()
        del components["worker"]["build_record"]["raw"][document][key]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize("document", _mod.BUILD_RECORD_RAW_DOCUMENTS)
    def test_an_empty_archived_body_is_not_an_archive(self, tmp_path: Path, document):
        """The shape a placeholder takes, and it must not satisfy a presence check.

        `body: {}` is what a collection script writes when the retrieval failed and
        nobody looked. It is present, correctly typed, and says nothing.
        """
        components = deployed_components()
        components["worker"]["build_record"]["raw"][document]["body"] = {}

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "body" in result.message

    def test_an_undatable_archive_retrieval_fails(self, tmp_path: Path):
        """A retrieval nobody can date cannot be told from one carried forward.

        The same reasoning as the teardown artifact's `captured_at`: a document from an
        earlier evaluation may describe a build this one has since replaced, and an
        unparseable timestamp makes that indistinguishable from a fresh read.
        """
        components = deployed_components()
        components["worker"]["build_record"]["raw"]["build"]["retrieved_at"] = "recently"

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a parseable ISO-8601 instant" in result.message

    @pytest.mark.parametrize("uncorroborated", ["built_revision", "build_id", "image_tag"])
    def test_a_build_summary_the_archive_does_not_corroborate_fails(
        self, tmp_path: Path, uncorroborated
    ):
        """The cross-check that makes the archive load-bearing rather than decorative.

        One field at a time: the archived build document is a real, well-formed
        SUCCEEDED response that corroborates everything EXCEPT the value under test.
        The record stays internally consistent throughout, so what fails is
        specifically the absence of retrieval behind one claim — which is what an
        invented summary attached to a genuine document looks like, and what a
        shape-only check would accept.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        other_revision = "0" * 40
        # A complete, plausible SUCCEEDED build, with the one value under test replaced
        # by something the build genuinely reported instead.
        substitutions = {
            "build_id": ("adp-worker-build:some-other-build", record["built_revision"], record["image_tag"]),
            "built_revision": (record["build_id"], other_revision, record["image_tag"]),
            "image_tag": (record["build_id"], record["built_revision"], "some-other-tag"),
        }
        build_id, revision, tag = substitutions[uncorroborated]
        body = codebuild_body(build_id, revision, tag)
        if uncorroborated == "built_revision":
            # Keep the source archive consistent with the revision the build reports,
            # so the ONLY thing wrong is that the summary claims a different one.
            body["builds"][0]["source"]["location"] = f"bucket/codebuild/src/{revision}-a1b2c3.zip"
        record["raw"]["build"]["body"] = body

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "but the archived build document reports" in result.message
        assert uncorroborated in result.message

    def test_a_failed_build_cannot_establish_what_is_running(self, tmp_path: Path):
        """Root's reproduction (b): a build whose own outcome is a failure.

        Reported as still passing at `9b797746`, and this is why. The check dumped the
        archived body to JSON and searched it for the expected revision and build id.
        Both appear in a FAILED build's response exactly as they appear in a successful
        one — the response is about that build either way — so the outcome field was
        never consulted. A build that failed published no image, so its output cannot
        be the provenance of a running one.

        Every CodeBuild non-success state, because "not SUCCEEDED" is the condition,
        not a list of the ones somebody remembered.
        """
        for status in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT", "IN_PROGRESS"):
            components = deployed_components()
            record = components["worker"]["build_record"]
            record["raw"]["build"]["body"] = codebuild_body(
                record["build_id"],
                record["built_revision"],
                record["image_tag"],
                status=status,
            )

            result = run_w2_01(
                tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
            )

            assert result.status == _mod.STATUS_FAILED, status
            assert f"buildStatus {status!r}" in result.message
            assert "did not publish an image" in result.message

    def test_an_unrelated_document_that_merely_mentions_the_right_strings_fails(
        self, tmp_path: Path
    ):
        """Root's reproduction (c), and the one that shows presence bought nothing.

        The entire archived build document is replaced by a single prose field that
        happens to contain the build id and the revision. Under the substring matcher
        this passed — the strings were there — which means requiring the archive gave
        no more assurance than requiring the summary alone, since the operator writes
        both. A document that is not a `batch-get-builds` response cannot corroborate
        a build, however many of the right words it contains.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        record["raw"]["build"]["body"] = {
            "unrelated_notes": (
                f"build {record['build_id']} was started for revision "
                f"{record['built_revision']} and tagged {record['image_tag']}"
            )
        }

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a usable `aws codebuild batch-get-builds` response" in result.message
        assert "'builds' in the CodeBuild response must be a nonempty list" in result.message

    def test_a_build_whose_source_archive_is_another_revision_fails(self, tmp_path: Path):
        """The override says one commit; the archive the build consumed is another.

        `codebuild-run.sh` uploads `git archive <sha>` to
        `codebuild/src/<sha>-<unique>.zip` and separately passes `ADP_SOURCE_SHA`. The
        environment override is just a string the caller set, so on its own it is the
        same class of evidence as the summary field. What the build actually compiled
        is the archive, and when the two disagree the override is wrong about the
        build.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        body = codebuild_body(
            record["build_id"], record["built_revision"], record["image_tag"]
        )
        body["builds"][0]["source"]["location"] = (
            "bucket/codebuild/src/" + "9" * 40 + "-a1b2c3.zip"
        )
        record["raw"]["build"]["body"] = body

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "which does not name revision" in result.message

    def test_a_built_digest_the_build_log_does_not_report_fails(self, tmp_path: Path):
        """The link root's review said was never corroborated by build output at all.

        `built_digest` was compared against the running image and against the registry
        — both of which the same operator recorded — so a digest nobody published
        satisfied the chain as long as it was written consistently in three places.
        The build states what it pushed, on the `<tag>: digest: sha256:... size: ...`
        line, and that line is the only first-hand source for the value.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        other = "sha256:" + "7" * 64
        record["raw"]["build_log"]["body"] = push_log_body(record["image_tag"], other)

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "reports the build pushed tag" in result.message
        assert other in result.message

    def test_a_build_log_with_no_push_line_cannot_corroborate_a_digest(
        self, tmp_path: Path
    ):
        """A log that never says it pushed anything is not evidence that it did.

        The shape a truncated or wrong-stream log takes. It is present, correctly
        typed, and contains no statement about a published digest — so the recorded
        one is still a value the operator typed.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        record["raw"]["build_log"]["body"] = {
            "events": [
                {"message": "Phase complete: BUILD State: SUCCEEDED"},
                {"message": f"pushed {record['built_digest']} eventually"},
            ]
        }

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "no `docker push` digest line" in result.message

    def test_a_registry_serving_a_different_digest_fails(self, tmp_path: Path):
        """The build succeeded; something else is running.

        The one link the operator cannot retype their way around, because the registry
        reports what is actually being served. A build record that agrees with itself
        while the registry serves another image is a deployment that did not take.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        other = "sha256:" + "7" * 64
        record["registry_digest"] = other
        record["raw"]["registry"]["body"] = {
            "imageDetails": [
                {
                    "repositoryName": record["repository"],
                    "imageDigest": other,
                    "imageTags": [record["image_tag"]],
                }
            ]
        }

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "the registry is the side that cannot be retyped" in result.message.lower()

    def test_a_registry_entry_without_the_pushed_tag_fails(self, tmp_path: Path):
        """The digest is right; it is not the image this build's tag resolves to.

        The tag is the only thing binding a build to a registry entry, so a response
        describing an image that does not carry it is a response about a different
        image — the "unrelated record containing the desired strings" case, on the
        registry side.
        """
        components = deployed_components()
        record = components["worker"]["build_record"]
        record["raw"]["registry"]["body"] = {
            "imageDetails": [
                {
                    "repositoryName": record["repository"],
                    "imageDigest": record["registry_digest"],
                    "imageTags": ["some-other-tag"],
                }
            ]
        }

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "reports the served image carrying tags" in result.message

    def test_the_real_codebuild_source_and_ecr_path_satisfies_the_schema(
        self, tmp_path: Path
    ):
        """Root's actual build path, not an attestation format nobody can produce.

        Root reported what the live builds emit: `codebuild-run.sh` with
        `ADP_RELEASE_BUILD=true` uploads a `git archive` of the exact commit to a
        per-build S3 key, passes the commit as the `ADP_SOURCE_SHA` environment
        override, tags the image with the full SHA, and the evidence available for
        acceptance is `aws codebuild batch-get-builds` plus the S3 source object and
        the ECR digest — with `PUBLISH_LATEST=false` so no moving tag is involved.
        They asked, reasonably, that the harness support that rather than require a
        GitHub attestation format they cannot obtain.

        This test is how that request is answered as a fact rather than a promise. It
        builds the record entirely out of what those commands really return —
        `source.location` naming the S3 archive, `ADP_SOURCE_SHA` and `IMAGE_TAG` in
        `environment.environmentVariables`, `buildStatus`, the `docker push` digest
        line in the build log, the ECR image detail, and the CodeBuild console link —
        and asserts W2-01 accepts it.

        The negatives around it are what make this meaningful: the same document with
        `buildStatus` anything but SUCCEEDED fails, a build whose source archive names
        another revision fails, a log that does not report the push digest fails, and a
        registry entry not carrying the pushed tag fails. A positive control alone would
        only establish that some document passes.

        The `raw` bodies are the redacted shapes, not real output — a test cannot
        produce real output, and pretending otherwise would be the same substitution
        this whole review is about. What it establishes is that the schema admits
        this path; whether the archived documents are genuine is root's to see when
        they run it against the live account.
        """
        revision = "405d1e6eb531105239432b2719844e1e51e60a93"
        digest = "sha256:" + "c" * 64
        tag = revision
        codebuild_id = "adp-dev-agent-runtime:9f8e7d6c-1234-4abc-8def-0123456789ab"
        source_object = (
            "adp-terraform-state-879318057152/codebuild/src/" + revision + "-1758672000-4242.zip"
        )

        record = build_record(
            "worker",
            revision,
            digest,
            project="adp-dev-agent-runtime",
            build_id=codebuild_id,
            build_url=(
                "https://us-east-1.console.aws.amazon.com/codesuite/codebuild/projects/"
                "adp-dev-agent-runtime/build/" + codebuild_id.replace(":", "%3A")
            ),
            image_tag=tag,
            repository="adp-dev-agent-runtime",
            raw={
                "build": raw_metadata(
                    f"aws codebuild batch-get-builds --ids {codebuild_id} --region us-east-1",
                    {
                        "builds": [
                            {
                                "id": codebuild_id,
                                "projectName": "adp-dev-agent-runtime",
                                "buildStatus": "SUCCEEDED",
                                # The S3 source object: the release build's source is a
                                # `git archive` of one commit, so this key IS the source
                                # revision's contents and names it.
                                "source": {"type": "S3", "location": source_object},
                                "resolvedSourceVersion": "3HL4kqtJlcpXroDTDmjVBH40Nrjfkd",
                                "environment": {
                                    "environmentVariables": [
                                        {"name": "ADP_SOURCE_SHA", "value": revision},
                                        {"name": "IMAGE_TAG", "value": tag},
                                        {"name": "PUBLISH_LATEST", "value": "false"},
                                    ]
                                },
                            }
                        ]
                    },
                ),
                "build_log": raw_metadata(
                    "aws logs get-log-events "
                    "--log-group-name /aws/codebuild/adp-dev-agent-runtime "
                    "--log-stream-name 9f8e7d6c-1234-4abc-8def-0123456789ab --region us-east-1",
                    {
                        "events": [
                            {
                                "message": "The push refers to repository "
                                "[879318057152.dkr.ecr.us-east-1.amazonaws.com/"
                                "adp-dev-agent-runtime]"
                            },
                            {"message": f"{tag}: digest: {digest} size: 4703"},
                            {"message": "Phase complete: BUILD State: SUCCEEDED"},
                        ]
                    },
                ),
                "registry": raw_metadata(
                    "aws ecr describe-images --repository-name adp-dev-agent-runtime "
                    f"--image-ids imageTag={tag} --region us-east-1",
                    {
                        "imageDetails": [
                            {
                                "repositoryName": "adp-dev-agent-runtime",
                                "imageDigest": digest,
                                "imageTags": [tag],
                                "registryId": "879318057152",
                            }
                        ]
                    },
                ),
            },
        )
        components = deployed_components()
        components["worker"] = {
            "revision": revision,
            "image_digest": digest,
            "source_revision": revision,
            "build_record": record,
        }
        # The commit graph has to contain the revision, because containment is
        # computed rather than recorded — the real run answers from the operator's
        # clone, which is where this commit actually lives.
        graph = CommitGraph()
        graph.parents[revision] = (WAVE2_REVISION,)
        gates = required_ci_gates(tested_revision=revision)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                deployed_components=components, ci_gates=gates
            ),
            graph=graph,
        )

        assert result.status == _mod.STATUS_PASSED, result.message

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_a_missing_deployed_component_fails(self, tmp_path: Path, component):
        """A gateway speaking this contract in front of a worker that does not is the
        normal half-deployment, and it is invisible to any check reading one side."""
        components = deployed_components()
        del components[component]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "no deployed identity recorded" in result.message
        assert component in result.message

    @pytest.mark.parametrize("bad", ["main", "a" * 39, "A" * 40, "", None, 40])
    @pytest.mark.parametrize("key", ["revision", "source_revision"])
    def test_a_deployed_revision_that_does_not_pin_a_commit_fails(
        self, tmp_path: Path, key, bad
    ):
        """A branch name, a short SHA, an uppercase SHA, empty, absent, a non-string.

        Each names something other than exactly one commit, and pinning exactly one
        commit is the entire purpose of the field. `main` matters most: it is what an
        operator writes when the deployment came from a moving ref, which is precisely
        when "what is running" is unknowable.
        """
        components = deployed_components()
        components["worker"][key] = bad

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a full 40-character git SHA" in result.message

    @pytest.mark.parametrize("bad", ["pending", "not-a-digest", "sha256:abc", "", None])
    def test_a_malformed_image_digest_fails_rather_than_comparing_equal(
        self, tmp_path: Path, bad
    ):
        """Two equally-malformed values compare equal.

        The mutation that separates a real digest check from a string comparison: an
        unparseable value cannot identify a build, and a comparison between two of them
        succeeds whenever they are equally malformed. `pending` is the realistic form —
        what a templated recording holds when the substitution never happened.
        """
        components = deployed_components()
        components["worker"]["image_digest"] = bad

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a sha256 digest" in result.message

    def test_identical_worker_and_gateway_digests_fail(self, tmp_path: Path):
        """A copied value would make a stale half-deployment pass both pairings.

        Worker and gateway are separate images from separate Dockerfiles, so an equal
        pair cannot be a true recording — and the specific way it lies is that it
        satisfies every per-component comparison above while describing one image
        twice.

        The copy is thorough on purpose: the gateway's build record is rewritten around
        the worker's digest too, so every per-component link still holds and the ONLY
        thing wrong is that two components claim one image. A test that copied the
        digest alone would be caught by the build-record cross-check instead, and would
        stop covering the defect it names.
        """
        components = deployed_components()
        components["gateway"]["image_digest"] = WORKER_DIGEST
        components["gateway"]["build_record"] = build_record(
            "gateway", DEPLOYED_GATEWAY_REVISION, WORKER_DIGEST
        )

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(deployed_components=components)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "same image digest" in result.message

    def test_a_flat_deployed_record_fails(self, tmp_path: Path):
        """The shape an earlier revision used: four flat digest fields.

        It cannot say which of two independently-shipped components is stale, and it
        carried no source revision at all — so the only thing its matching pair
        established was that the operator wrote the same value twice.
        """
        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                deployed_components="sha256:" + "1" * 64
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "must be an object keyed by component" in result.message

    # ---- (3b) required CI gates, by name, on the tested revision -------

    @pytest.mark.parametrize("gate", _mod.WAVE2_REQUIRED_CI_GATES)
    def test_a_missing_required_gate_fails(self, tmp_path: Path, gate):
        """Root's third finding: gates BY NAME.

        Every required gate parametrized, because a check verifying one of the three
        would let the other two go unrun. The names are the ones
        `.github/workflows/agent-control-ci.yml` actually defines — renaming a job
        breaks this deliberately.
        """
        gates = required_ci_gates()
        del gates[gate]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert gate in result.message

    def test_green_gates_with_names_nobody_required_fail(self, tmp_path: Path):
        """The exact input root's review named as passing when it must not.

        `{"anything": "passed"}` — and here a more plausible version: three real-looking
        green gates whose names are not the required ones. It demonstrates the
        operator's spelling, not the build's gates, and a check that only scanned for
        non-passing values would accept it.
        """
        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates={
                    "anything": {
                        "status": "passed",
                        "run_id": "ci-1",
                        "tested_revision": DEPLOYED_WORKER_REVISION,
                    },
                    "Unit tests": {
                        "status": "passed",
                        "run_id": "ci-2",
                        "tested_revision": DEPLOYED_WORKER_REVISION,
                    },
                }
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "no result recorded for the required CI gate" in result.message

    def test_a_gate_tested_on_a_revision_that_is_not_deployed_fails(self, tmp_path: Path):
        """A genuinely green run, of a build that is not the one running.

        This is the mutation that separates "the gate passed" from "the gate passed on
        what is deployed". The gate name is right, the status is right, the run exists —
        and its subject is some other commit, so it is evidence about a build nobody is
        evaluating.
        """
        gates = required_ci_gates()
        gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["tested_revision"] = "9" * 40

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "tested revision" in result.message

    @pytest.mark.parametrize("bad", ["main", "abc1234", "", None])
    def test_a_gate_whose_tested_revision_is_not_a_commit_fails(self, tmp_path: Path, bad):
        gates = required_ci_gates()
        gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["tested_revision"] = bad

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("key", _mod.CI_GATE_KEYS)
    def test_a_gate_missing_its_identity_or_subject_fails(self, tmp_path: Path, key):
        """A gate without a run ID is unretrievable; without a tested revision it
        names a green run of unknown subject. Both are claims rather than evidence."""
        gates = required_ci_gates()
        del gates[_mod.WAVE2_REQUIRED_CI_GATES[0]][key]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize("truthy", [True, 1, ["ci-1"], {"id": "ci-1"}])
    def test_a_gate_run_identity_that_is_not_a_string_fails(self, tmp_path: Path, truthy):
        """The literal input root's review named: an arbitrary truthy `run_id`.

        `if not entry.get("run_id")` accepts `True`, and `True` identifies no run. Every
        other field here is correct, so this isolates the defect: the check was testing
        truthiness where it needed an identity.
        """
        gates = required_ci_gates()
        gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["run_id"] = truthy

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "must be a string identifying the run" in result.message

    @pytest.mark.parametrize("not_a_location", ["the run", "actions/runs/1", "ftp://x/1"])
    def test_a_gate_run_url_that_cannot_be_opened_fails(self, tmp_path: Path, not_a_location):
        """A gate is evidence only if the run behind it can be opened and read."""
        gates = required_ci_gates()
        gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["run_url"] = not_a_location

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "not an https URL" in result.message

    @pytest.mark.parametrize(
        "uncorroborated", ["run_id", "tested_revision", "job name"]
    )
    def test_a_gate_summary_the_run_document_does_not_corroborate_fails(
        self, tmp_path: Path, uncorroborated
    ):
        """An invented gate, in the only form left: summary fields with no run behind them.

        The archived document is real and well-formed and corroborates everything except
        the one value under test. The `job name` case is the sharpest of the three: a
        genuine run of a DIFFERENT workflow, whose green conclusion says nothing about
        the gate being claimed — which is the "green run of unknown subject" the field
        exists to rule out.
        """
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates()
        entry = gates[gate]
        entry["raw"]["run"]["body"] = {
            "databaseId": "some-other-run" if uncorroborated == "run_id" else entry["run_id"],
            # A head that EXISTS and is a real commit, but one the checked-out revision
            # does not contain: WAVE1_ACCEPTED_REVISION is an ancestor of the tested
            # revision, so its descendant direction is the failing one. An absent commit
            # would land on the not_run path instead — git could not look — and would
            # cover the unanswerable case rather than the uncorroborated one.
            "headSha": DEPLOYED_GATEWAY_REVISION
            if uncorroborated == "tested_revision"
            else entry["tested_revision"],
            "attempt": 1,
            "event": "pull_request",
            "jobs": [
                {
                    "name": "Some other job" if uncorroborated == "job name" else gate,
                    "conclusion": "success",
                }
            ],
        }

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        expected = {
            "run_id": "reports databaseId",
            # On a pull_request run the job builds a merge OF the head, so the head must
            # be contained in what was checked out. A run document naming an unrelated
            # real commit as its head is not a document about this run.
            "tested_revision": "does not contain that head",
            "job name": "contains no job named",
        }[uncorroborated]
        assert expected in result.message

    def test_a_run_whose_jobs_all_failed_is_not_a_passing_gate(self, tmp_path: Path):
        """Root's reproduction (a): every job in the archived run concluded `failure`.

        Reported as still passing at `9b797746`. The substring matcher looked for the
        run id, the tested revision and the job NAME in the dumped body — all three of
        which a red run's document contains, because it is a document about that run
        and that job. The conclusion was never read, so a wholly failed run satisfied a
        gate whose entire content is "this job passed".

        Both halves are set red, the run and the job, because the gate is the job's
        outcome and either alone would leave the other unchecked.
        """
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates()
        entry = gates[gate]
        entry["raw"]["run"]["body"] = {
            "databaseId": entry["run_id"],
            "headSha": entry["tested_revision"],
            "conclusion": "failure",
            "jobs": [
                {"name": name, "conclusion": "failure"}
                for name in _mod.WAVE2_REQUIRED_CI_GATES
            ],
        }

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "not a passing gate" in result.message or "conclusion 'failure'" in result.message

    @pytest.mark.parametrize("conclusion", ["failure", "cancelled", "skipped", None])
    def test_a_named_job_that_did_not_succeed_is_not_a_passing_gate(
        self, tmp_path: Path, conclusion
    ):
        """The run is green overall; the required job is not.

        A run can conclude `success` while a specific job was skipped — a `paths` filter
        or an `if` condition does exactly that — so the run's own status cannot stand in
        for the job's. `None` is the in-progress case: a job with no conclusion yet has
        not passed, and treating a null as "not failure" is how an unfinished run
        becomes a gate.
        """
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates()
        entry = gates[gate]
        entry["raw"]["run"]["body"] = {
            "databaseId": entry["run_id"],
            "headSha": entry["tested_revision"],
            "conclusion": "success",
            "jobs": [{"name": gate, "conclusion": conclusion}],
        }

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert f"reports job {gate!r} as {conclusion!r}" in result.message

    def test_an_unrelated_run_document_mentioning_the_gate_is_not_a_gate(
        self, tmp_path: Path
    ):
        """Reproduction (c) on the CI half: prose containing all the right strings.

        The gate's whole archive is replaced by a note that mentions the run id, the
        revision and the job name. Under presence-matching this passed. It is not a
        `gh run view` response, so it establishes nothing about a run.
        """
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates()
        entry = gates[gate]
        entry["raw"]["run"]["body"] = {
            "unrelated_notes": (
                f"run {entry['run_id']} ran {gate} on {entry['tested_revision']}"
            )
        }

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "does not establish a passing run of that job" in result.message
        assert "databaseId" in result.message

    # ---- the manual path: the checkout artifact, not an appended SHA ----
    #
    # Root's second finding on `ae57ee24`: the runbook had the operator paste
    # `checked_out_revision` into the archived `gh run view` body, and the parser
    # trusted it — so the one fact the manual path exists to establish was back to
    # being an assertion, inside a document otherwise written by GitHub. The workflow
    # already uploads the real value per job. These cover reading THAT, and refusing
    # the three ways a wrong artifact could be substituted for the right one.

    def _dispatch_gate(self, **artifact_overrides) -> tuple[str, dict]:
        """One required gate, as a manual dispatch: head is the workflow ref, not the subject."""
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates()
        entry = gates[gate]
        entry["raw"]["run"]["body"] = {
            "databaseId": entry["run_id"],
            # Deliberately NOT the subject: on a manual run this names the ref the
            # workflow file was loaded from.
            "headSha": "f" * 40,
            "attempt": 1,
            "event": "workflow_dispatch",
            "conclusion": "success",
            "jobs": [{"name": gate, "conclusion": "success"}],
        }
        artifact = checkout_artifact(
            _mod.CI_GATE_JOB_IDS[gate],
            entry["run_id"],
            entry["tested_revision"],
            event_name="workflow_dispatch",
            workflow_ref_sha="f" * 40,
        )
        # Applied after construction so a case can override `run_id`/`job` too, which
        # are positional above.
        artifact.update(artifact_overrides)
        entry["raw"]["checkout"]["body"] = artifact
        return gate, gates

    def test_a_sha_appended_to_the_run_response_cannot_override_the_checkout(
        self, tmp_path: Path
    ):
        """Root's finding: an operator-added `checked_out_revision` is refused, not trusted.

        `gh run view` does not return that key, so its presence means someone edited the
        authoritative response — and it is edited at exactly the field that decides what
        the gate is evidence about. Refusing it is what makes the artifact the only
        source for that value.
        """
        gate, gates = self._dispatch_gate()
        gates[gate]["raw"]["run"]["body"]["checked_out_revision"] = gates[gate][
            "tested_revision"
        ]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "carries a 'checked_out_revision' key" in result.message

    def test_a_manual_run_without_the_checkout_artifact_fails(self, tmp_path: Path):
        """No artifact, no tested revision — `headSha` may not stand in for it.

        The document is a real, green, well-formed manual run. What it cannot say is
        which revision the jobs checked out, so the gate is unusable rather than
        assumed-good.
        """
        gate, gates = self._dispatch_gate()
        del gates[gate]["raw"]["checkout"]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "archives no ['checkout'] document" in result.message

    def test_a_checkout_artifact_from_another_job_is_not_this_gate(self, tmp_path: Path):
        """A manual run uploads one artifact per test job. They are not interchangeable.

        Each job checks out independently, so the harness-tests job's artifact says
        nothing about whether the agent-control job checked out the right tree — and
        substituting one would make two of the three gates unverified.
        """
        other = _mod.CI_GATE_JOB_IDS[_mod.WAVE2_REQUIRED_CI_GATES[2]]
        gate, gates = self._dispatch_gate(job=other)

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert f"is for job {other!r}" in result.message

    def test_a_checkout_artifact_from_another_run_is_not_this_gate(self, tmp_path: Path):
        """The right job, the right revision — from a different run.

        Without binding the artifact to the archived run, any past green run of the
        same job would serve as every gate's checkout evidence.
        """
        gate, gates = self._dispatch_gate(run_id="ci-run-999")

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "checkout artifact from run 'ci-run-999'" in result.message

    def test_a_checkout_artifact_from_another_attempt_is_not_this_gate(
        self, tmp_path: Path
    ):
        """A re-run checks out afresh, so the attempt that produced the evidence matters.

        Attempt 1 can have checked out one tree and attempt 2 another; reporting
        attempt 2's conclusion against attempt 1's checkout mixes two runs.
        """
        gate, gates = self._dispatch_gate(run_attempt="2")

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "from attempt '2'" in result.message

    def test_a_manual_run_whose_job_checked_out_another_revision_fails(
        self, tmp_path: Path
    ):
        """The dispatch asked for one revision; the job's own artifact reports another.

        This is the case the workflow's verify step is supposed to have failed already.
        The harness checks it anyway: a gate is judged on what was actually built, and
        the two guards fail independently.
        """
        gate, gates = self._dispatch_gate(checked_out_revision="9" * 40)

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "artifact says it checked out" in result.message

    def test_a_manual_run_on_the_dispatched_revision_is_a_passing_gate(
        self, tmp_path: Path
    ):
        """The positive control for the manual path: the artifact's SHA is the subject.

        Without this the tests above would be satisfiable by rejecting every
        `workflow_dispatch` run, which would make the new manual CI entry point useless
        as evidence. Note `headSha` here is `f`*40 — the workflow ref — and the gate
        still passes, because the subject is read from the artifact.
        """
        gates = required_ci_gates()
        for gate, entry in gates.items():
            entry["raw"]["run"]["body"] = {
                "databaseId": entry["run_id"],
                "headSha": "f" * 40,  # the workflow ref, deliberately NOT the subject
                "attempt": 1,
                "event": "workflow_dispatch",
                "conclusion": "success",
                "jobs": [{"name": gate, "conclusion": "success"}],
            }
            entry["raw"]["checkout"]["body"] = checkout_artifact(
                _mod.CI_GATE_JOB_IDS[gate],
                entry["run_id"],
                entry["tested_revision"],
                event_name="workflow_dispatch",
                workflow_ref_sha="f" * 40,
            )

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_PASSED, result.message

    @pytest.mark.parametrize("key", _mod.CHECKOUT_ARTIFACT_KEYS)
    def test_a_checkout_artifact_missing_an_emitted_field_fails(
        self, tmp_path: Path, key
    ):
        """Scoped to the schema the workflow emits — all six keys, no more.

        A document missing any of them is not that artifact, and accepting a partial one
        would let a hand-written stub take its place.
        """
        gate, gates = self._dispatch_gate()
        del gates[gate]["raw"]["checkout"]["body"][key]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert f"missing ['{key}']" in result.message

    def test_the_gate_job_ids_are_the_ones_ci_defines(self):
        """The artifact name and the `job` field both carry the workflow's job id.

        Mapped explicitly rather than slugified from the display name: they are
        independent strings in the workflow, so a job renamed on one side only must
        fail here rather than resolve to a plausible artifact name that does not exist.
        """
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "agent-control-ci.yml"
        ).read_text(encoding="utf-8")

        assert set(_mod.CI_GATE_JOB_IDS) == set(_mod.WAVE2_REQUIRED_CI_GATES)
        for gate, job_id in _mod.CI_GATE_JOB_IDS.items():
            assert f"  {job_id}:\n    # AC-S7 required-check name. Do not rename.\n    name: {gate}\n" in workflow, job_id
            assert f"name: checked-out-revision-{job_id}" in workflow, job_id

    # ---- a pull_request run tests a MERGE commit, not the branch tip ----
    #
    # Root's finding on `1166f1d9`, reproduced with the real artifact of a real run
    # rather than a constructed one. GitHub does not build the branch as pushed on a
    # `pull_request` event: it builds a temporary merge of the branch into its base, and
    # that merge is what `actions/checkout` gives the job and what the tests ran against.
    # The API reports the branch tip as the run's head. Both values are authentic and
    # they are different facts.
    #
    # Run 35956224007 of this very workflow:
    #     API headSha                       1166f1d9aee982c3a22f7bd88e643b120ae8c457
    #     artifact checked_out_revision     92d6cb4ccb4bcf3e39b69d9a3b7666e02088464c
    #     artifact workflow_ref_sha         92d6cb4ccb4bcf3e39b69d9a3b7666e02088464c
    #     run_id / run_attempt / event      35956224007 / 1 / pull_request
    #
    # The harness demanded equality for every non-dispatch trigger, so that honest gate
    # FAILED. These cover accepting it on the relation that actually holds — a merge of
    # the head contains the head — while still refusing an unrelated revision.

    #: The real values above, so the fixtures below model the observed contract.
    REAL_PR_HEAD = "1166f1d9aee982c3a22f7bd88e643b120ae8c457"
    REAL_PR_MERGE = "92d6cb4ccb4bcf3e39b69d9a3b7666e02088464c"
    REAL_PR_RUN_ID = "35956224007"

    def _real_pr_gate(self, *, merge: str | None = None, head: str | None = None, **artifact_overrides):
        """One gate as the real pull_request run 35956224007 actually recorded it."""
        merge = merge or self.REAL_PR_MERGE
        head = head or self.REAL_PR_HEAD
        gate = _mod.WAVE2_REQUIRED_CI_GATES[0]
        gates = required_ci_gates(tested_revision=merge)
        entry = gates[gate]
        entry["run_id"] = self.REAL_PR_RUN_ID
        entry["raw"]["run"]["body"] = {
            "databaseId": self.REAL_PR_RUN_ID,
            "headSha": head,
            "attempt": 1,
            "event": "pull_request",
            "conclusion": "success",
            "jobs": [{"name": gate, "conclusion": "success"}],
        }
        artifact = checkout_artifact(
            _mod.CI_GATE_JOB_IDS[gate],
            self.REAL_PR_RUN_ID,
            merge,
            event_name="pull_request",
            workflow_ref_sha=merge,
        )
        artifact.update(artifact_overrides)
        entry["raw"]["checkout"]["body"] = artifact
        # The other two gates are left on the default revision, so this gate's merge
        # commit has to be deployed for the "tested a deployed revision" check to hold.
        return gate, gates, merge

    def _pr_graph(self, merge: str, head: str) -> CommitGraph:
        """A graph in which `merge` is a merge commit of `head` — and is deployed."""
        graph = CommitGraph()
        graph.parents[head] = (WAVE2_REVISION,)
        graph.parents[merge] = (head, WAVE2_REVISION)
        return graph

    def test_the_real_pull_request_run_is_accepted_with_its_merge_commit(
        self, tmp_path: Path
    ):
        """The honest case root reproduced: unmodified API response, real artifact.

        This is the positive control for the whole change, and it fails against the
        previous revision with "Outside a manual dispatch those are the same revision".
        The tested revision is the merge commit, because that is the tree the tests ran
        in — the branch tip was never built.
        """
        gate, gates, merge = self._real_pr_gate()
        graph = self._pr_graph(merge, self.REAL_PR_HEAD)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates=gates,
                deployed_components=deployed_components(
                    worker={
                        "revision": merge,
                        "image_digest": WORKER_DIGEST,
                        "source_revision": merge,
                        "build_record": build_record("worker", merge, WORKER_DIGEST),
                    }
                ),
            ),
            graph=graph,
        )

        assert result.status == _mod.STATUS_PASSED, result.message

    def test_a_pull_request_checkout_that_does_not_contain_the_head_fails(
        self, tmp_path: Path
    ):
        """The negative that keeps containment from being a licence to differ.

        A real, existing commit that simply is not a merge of this run's head. Accepting
        it would mean any two archived documents could be paired as long as both parsed,
        which is the substitution the run/attempt bindings exist to prevent.
        """
        gate, gates, merge = self._real_pr_gate()
        graph = CommitGraph()
        graph.parents[self.REAL_PR_HEAD] = (WAVE2_REVISION,)
        # `merge` exists and is deployed, but branched BEFORE the head — so it cannot
        # be a merge of it.
        graph.parents[merge] = (WAVE2_REVISION,)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates=gates,
                deployed_components=deployed_components(
                    worker={
                        "revision": merge,
                        "image_digest": WORKER_DIGEST,
                        "source_revision": merge,
                        "build_record": build_record("worker", merge, WORKER_DIGEST),
                    }
                ),
            ),
            graph=graph,
        )

        assert result.status == _mod.STATUS_FAILED
        assert "does not contain that head" in result.message

    def test_a_pull_request_head_git_cannot_resolve_is_not_run(self, tmp_path: Path):
        """Unanswerable is NOT RUN, never a pass.

        The one way a containment check could quietly become permissive is by treating
        "git could not be asked" as "nothing to object to". An unfetched head — a
        shallow clone, or a branch tip deleted after the merge — has to report that the
        harness could not look.
        """
        gate, gates, merge = self._real_pr_gate(head=INVENTED_REVISION)
        graph = CommitGraph()
        graph.parents[merge] = (WAVE2_REVISION,)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates=gates,
                deployed_components=deployed_components(
                    worker={
                        "revision": merge,
                        "image_digest": WORKER_DIGEST,
                        "source_revision": merge,
                        "build_record": build_record("worker", merge, WORKER_DIGEST),
                    }
                ),
            ),
            graph=graph,
        )

        assert result.status == _mod.STATUS_NOT_RUN
        assert "is not a commit in this checkout" in result.message

    def test_a_pull_request_artifact_whose_ref_sha_is_not_its_checkout_fails(
        self, tmp_path: Path
    ):
        """`workflow_ref_sha` was parsed, shape-checked, and then never used.

        On a pull_request run GITHUB_SHA is the merge commit the job built, so the two
        fields the step writes are the same value — as the real artifact above shows.
        Leaving it unchecked meant an artifact could name any workflow ref at all.
        """
        gate, gates, merge = self._real_pr_gate(workflow_ref_sha="e" * 40)
        graph = self._pr_graph(merge, self.REAL_PR_HEAD)

        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates=gates,
                deployed_components=deployed_components(
                    worker={
                        "revision": merge,
                        "image_digest": WORKER_DIGEST,
                        "source_revision": merge,
                        "build_record": build_record("worker", merge, WORKER_DIGEST),
                    }
                ),
            ),
            graph=graph,
        )

        assert result.status == _mod.STATUS_FAILED
        assert "recorded workflow_ref_sha" in result.message

    def test_a_manual_run_whose_ref_sha_is_not_the_api_head_fails(self, tmp_path: Path):
        """The manual half of the same binding.

        On a `workflow_dispatch` run `headSha` names the ref the workflow FILE was
        loaded from, which is exactly what the step records as `workflow_ref_sha`. They
        are therefore checkable against each other, and were not being checked. The
        tested revision still comes only from `checked_out_revision`.
        """
        gate, gates = self._dispatch_gate(workflow_ref_sha="e" * 40)

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert "recorded workflow_ref_sha" in result.message

    @pytest.mark.parametrize("field", ["attempt", "event"])
    def test_a_run_document_omitting_a_binding_field_fails_rather_than_skipping_it(
        self, tmp_path: Path, field
    ):
        """A document that says less must not be judged less strictly.

        Both comparisons used to be conditional on the field being present, so omitting
        `attempt` skipped the attempt binding and omitting `event` skipped the trigger
        binding that decides which head rule applies. The collector requests both, so an
        absent one is an incomplete archive.
        """
        gates = required_ci_gates()
        del gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["raw"]["run"]["body"][field]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert f"records no {field!r}" in result.message

    @pytest.mark.parametrize("key", _mod.RAW_METADATA_KEYS)
    def test_a_gate_archive_missing_its_own_provenance_fails(self, tmp_path: Path, key):
        """The gate's run document needs the same three facts every archive needs.

        Not a duplicate of the build-record case: these are separate code paths reached
        through separate schemas, and a gate whose archive went unvalidated would be the
        one remaining place an unretrievable claim could enter.
        """
        gates = required_ci_gates()
        del gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["raw"]["run"][key]

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize("status", ["failure", "cancelled", "skipped", "", None, True])
    def test_a_gate_that_did_not_pass_fails(self, tmp_path: Path, status):
        """`skipped` and `cancelled` are in the table with `failure` deliberately: a
        gate that did not run is not a gate that passed, and a check testing
        `!= "failure"` would accept both."""
        gates = required_ci_gates()
        gates[_mod.WAVE2_REQUIRED_CI_GATES[0]]["status"] = status

        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED

    @pytest.mark.parametrize("gates", [{}, None, "passed", []])
    def test_a_vacuous_gate_record_fails_rather_than_passing_vacuously(
        self, tmp_path: Path, gates
    ):
        """An empty map makes "no gate failed" trivially true.

        Indistinguishable, to any check that only looks for failures, from a build
        whose gates never ran — the state a recording produced before CI finished
        would be in.
        """
        result = run_w2_01(tmp_path, preflight=wave2_preflight_payload(ci_gates=gates))

        assert result.status == _mod.STATUS_FAILED

    def test_a_flat_passed_gate_string_is_not_evidence(self, tmp_path: Path):
        """The shape an earlier revision accepted: name → "passed".

        No run to retrieve and no revision it tested, so it cannot be distinguished
        from a value someone typed. The failure has to name the shape rather than the
        status, because the status is the only part that looks right.
        """
        result = run_w2_01(
            tmp_path,
            preflight=wave2_preflight_payload(
                ci_gates={name: "passed" for name in _mod.WAVE2_REQUIRED_CI_GATES}
            ),
        )

        assert result.status == _mod.STATUS_FAILED

    def test_the_required_gate_names_are_the_ones_ci_defines(self):
        """The tripwire against the constant drifting away from the workflow.

        `WAVE2_REQUIRED_CI_GATES` is only meaningful if those jobs exist: a required
        gate CI does not define can never be green, and a renamed job would make this
        check unsatisfiable rather than discriminating. Read from the workflow file, so
        renaming a job fails here instead of in a live evaluation.
        """
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "agent-control-ci.yml"
        ).read_text(encoding="utf-8")

        for gate in _mod.WAVE2_REQUIRED_CI_GATES:
            assert f"name: {gate}" in workflow, gate

    def test_the_workflow_can_be_dispatched_against_an_exact_revision(self):
        """The gates must be runnable on a revision the PR `paths` filters never ran.

        Root reported the concrete consequence: the deployed source
        `405d1e6eb531105239432b2719844e1e51e60a93` has only "Script Tests" on GitHub,
        because Agent Control CI triggers on filtered pull requests alone. W2-01
        requires each required gate to have passed ON a deployed revision, so without a
        manual entry point that requirement is unsatisfiable for exactly the revisions
        it exists for — an evaluation would have to be failed for a reason no operator
        could fix.

        Asserted from the workflow file rather than described in a runbook, because the
        evaluation's `checked_out_revision` contract depends on the workflow actually
        publishing that value. Checked here: the dispatch input exists and is required,
        every one of the three required jobs waits on the validation job and checks out
        its ref, and each verifies `git rev-parse HEAD` against the expected SHA rather
        than assuming the checkout obeyed.
        """
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "agent-control-ci.yml"
        ).read_text(encoding="utf-8")

        assert "workflow_dispatch:" in workflow
        assert "source_sha:" in workflow
        assert "required: true" in workflow
        # The PR path must be untouched — the filters and the main-branch restriction
        # are what keep this workflow off PRs that cannot affect the control path.
        assert "pull_request:" in workflow
        assert "- main" in workflow

        assert workflow.count("needs: resolve-source") == len(_mod.WAVE2_REQUIRED_CI_GATES)
        assert workflow.count("ref: ${{ needs.resolve-source.outputs.ref }}") == len(
            _mod.WAVE2_REQUIRED_CI_GATES
        )
        # Each job asks git what it actually has and fails on a mismatch. Trusting the
        # checkout would make the recorded revision an assumption again.
        assert workflow.count('ACTUAL="$(git rev-parse HEAD)"') == len(
            _mod.WAVE2_REQUIRED_CI_GATES
        )
        assert workflow.count('[ "$ACTUAL" != "$EXPECTED" ]') == len(
            _mod.WAVE2_REQUIRED_CI_GATES
        )
        # And archives it machine-readably, under the field name the harness reads.
        assert workflow.count('"checked_out_revision": "${ACTUAL}"') == len(
            _mod.WAVE2_REQUIRED_CI_GATES
        )
        # github.sha is recorded as the workflow ref, never as the tested source.
        assert '"workflow_ref_sha": "${GITHUB_SHA}"' in workflow
        assert '"checked_out_revision": "${GITHUB_SHA}"' not in workflow

    # ---- (4) fixture scope, inventory and teardown readiness ----------

    def test_isolation_applied_after_the_listener_fails(self, tmp_path: Path):
        """Ordering, not presence. DP-INV-1 requires the ingress policy BEFORE any
        enabled listener; the reverse order means the fixture was briefly reachable
        while control-enabled, and by recheck time it looks identical to the correct
        order."""
        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(isolation_before_listener=False)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "before listener start" in result.message

    def test_ordinary_flags_left_on_fails(self, tmp_path: Path):
        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(ordinary_flags_off=False)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "ordinary" in result.message

    def test_a_flag_off_fixture_cannot_produce_this_waves_evidence(self, tmp_path: Path):
        scope = dict(wave2_preflight_payload()["fixture_only_flag_scope"])
        scope["enabled_in_fixture"] = False

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(fixture_only_flag_scope=scope)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not recorded as enabled in the fixture" in result.message

    def test_the_flag_enabled_outside_the_fixture_fails(self, tmp_path: Path):
        """DP-INV-1 itself: the invariant the whole evaluation is conditioned on."""
        scope = dict(wave2_preflight_payload()["fixture_only_flag_scope"])
        scope["enabled_elsewhere"] = ["dev", "embark1"]

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(fixture_only_flag_scope=scope)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "enabled outside the fixture" in result.message
        assert "dev" in result.message and "embark1" in result.message

    @pytest.mark.parametrize("claim", [False, True, "no", None])
    def test_a_boolean_enabled_elsewhere_claim_is_not_evidence(self, tmp_path: Path, claim):
        """The form of the answer is the point.

        `enabled_elsewhere: false` is the CLAIM; an enumerated empty list is the
        EVIDENCE for it. `False` is in this table alongside `True` deliberately:
        the "correct-looking" boolean must fail too, because a boolean cannot be
        audited against the environments it implicitly ranges over.
        """
        scope = dict(wave2_preflight_payload()["fixture_only_flag_scope"])
        scope["enabled_elsewhere"] = claim

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(fixture_only_flag_scope=scope)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "must be a list" in result.message

    def test_a_preflight_describing_another_fixture_fails(self, tmp_path: Path):
        """The flag was enabled somewhere — just not here. A true recording of the
        wrong environment, which is the stale-evidence shape again."""
        scope = dict(wave2_preflight_payload()["fixture_only_flag_scope"])
        scope["fixture_environment"] = "dev-control-fixture-embark2"

        result = run_w2_01(
            tmp_path, preflight=wave2_preflight_payload(fixture_only_flag_scope=scope)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "different fixture" in result.message

    @pytest.mark.parametrize("dropped", ["W2-03", "W2-07", "W2-10"])
    def test_a_short_wave_fails_the_inventory(self, tmp_path: Path, dropped):
        """The self-referential assertion, and the reason it is not circular.

        A future revision that quietly drops a check would produce a wave whose
        every PRESENT check passes — a green report about nine tenths of the
        evaluation. The inventory is compared against #3968's table, so the missing
        ID is named rather than being absent from both sides of the comparison.
        """
        config = wave2_config(tmp_path)
        probe = _mod.Probe(config["gateway_url"], wave2_gateway_stub())
        artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
        driver = _mod.Driver(
            config, probe, artifacts, dynamodb=ddb_stub(), git_runner=CommitGraph().runner()
        )
        short = tuple(
            spec.check_id for spec in _mod.WAVE2_CHECKS if spec.check_id != dropped
        )

        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            results = _mod.run_checks(
                driver,
                [s for s in _mod.WAVE2_CHECKS if s.check_id == "W2-01"],
                manifest_ids=short,
            )

        assert results[0].status == _mod.STATUS_FAILED
        assert dropped in results[0].message

    @pytest.mark.parametrize("extra", ["W2-11", "W3-01"])
    def test_an_unknown_check_id_fails_the_inventory(self, tmp_path: Path, extra):
        """Both directions. An extra ID means the report claims evidence for a
        check #3968's table does not define, which no reviewer can interpret."""
        config = wave2_config(tmp_path)
        probe = _mod.Probe(config["gateway_url"], wave2_gateway_stub())
        artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
        driver = _mod.Driver(
            config, probe, artifacts, dynamodb=ddb_stub(), git_runner=CommitGraph().runner()
        )
        inflated = tuple(spec.check_id for spec in _mod.WAVE2_CHECKS) + (extra,)

        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            results = _mod.run_checks(
                driver,
                [s for s in _mod.WAVE2_CHECKS if s.check_id == "W2-01"],
                manifest_ids=inflated,
            )

        assert results[0].status == _mod.STATUS_FAILED
        assert extra in results[0].message

    def test_a_fixture_with_no_declared_teardown_fails_at_preflight(self, tmp_path: Path):
        """Cleanup is bounded to declared pairs by design — there is no scan to fall
        back on — so an undeclared row is one that survives the evaluation. Catching
        that BEFORE anything is seeded is the difference between a preflight failure
        and a fixture nobody can take apart."""
        config = wave2_config(tmp_path, cleanup_items=[])

        result = run_w2_01(tmp_path, config=config)

        assert result.status == _mod.STATUS_FAILED
        assert "no 'cleanup_items' are declared" in result.message

    @pytest.mark.parametrize(
        "item",
        [
            {"event_id": "msg-live-001"},
            {"arrived_at": "2026-09-12T00:00:00Z"},
            {"event_id": "msg-live-001", "arrived_at": ""},
            {"event_id": "", "arrived_at": "2026-09-12T00:00:00Z"},
            "msg-live-001",
        ],
    )
    def test_a_partial_key_declaration_fails_at_preflight(self, tmp_path: Path, item):
        """Wrong row keys, caught early.

        A delete keyed on the partition key alone could match an unrelated item, so
        the harness refuses it at teardown. Declaring both halves here is what turns
        that refusal into a preflight failure instead of a surprise after the
        fixture exists.
        """
        config = wave2_config(tmp_path, cleanup_items=[item])

        result = run_w2_01(tmp_path, config=config)

        assert result.status == _mod.STATUS_FAILED
        assert "BOTH event_id and arrived_at" in result.message

    @pytest.mark.parametrize(
        "uncovered_key", ["live_run_id", "terminal_run_id", "aborted_run_id"]
    )
    def test_a_seeded_row_missing_from_the_teardown_fails(self, tmp_path: Path, uncovered_key):
        """Every synthetic row this wave seeds must be covered, and each one is
        parametrized: a check that only verified the live row would leave the
        terminal and aborted rows behind, which is the fixture left in the state
        DP-INV-1 forbids."""
        config = wave2_config(tmp_path)
        target = str(config[uncovered_key])
        config["cleanup_items"] = [
            item for item in config["cleanup_items"] if str(item["event_id"]) != target
        ]

        result = run_w2_01(tmp_path, config=config)

        assert result.status == _mod.STATUS_FAILED
        assert uncovered_key in result.message

    def test_the_unknown_run_id_is_not_required_to_be_cleaned_up(self, tmp_path: Path):
        """The deliberate asymmetry, asserted so a later "consistency" change does
        not quietly introduce a delete of something the harness never created.

        `unknown_run_id` names a row that must NOT exist. Requiring it in
        `cleanup_items` would mean declaring a deletion of an object outside the
        fixture's ownership — which §7 forbids.
        """
        config = wave2_config(tmp_path)
        assert config["unknown_run_id"] not in {
            item["event_id"] for item in config["cleanup_items"]
        }

        result = run_w2_01(tmp_path, config=config)

        assert result.status == _mod.STATUS_PASSED, result.message


class TestWave2VerifiedCleanupIsDiscriminating:
    """W2-10: verified teardown, and the security posture that survived it.

    This is the check the defect was really about. The tests are organized around
    the one property that matters most: there is NO input to this check that
    reports a successful teardown without one having happened.
    """

    def test_a_correct_teardown_and_recheck_passes(self, tmp_path: Path):
        result = run_w2_10(tmp_path)

        assert result.status == _mod.STATUS_PASSED, result.message

    # ---- the harness's own deletion record ----------------------------

    def test_an_absent_cleanup_record_is_not_run_never_a_pass(self, tmp_path: Path):
        """The single most important assertion in this class.

        `cleanup=None` is what a run where cleanup never happened looks like from
        inside this check. It must be `not_run` — nonzero — and specifically not
        `passed`: a check that treats "I have no record" as "it must have worked"
        is the false green #5825 exists to remove.
        """
        result = run_w2_10(tmp_path, cleanup=None)

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "cleanup record is absent" in result.message

    def test_a_failed_cleanup_fails_the_check(self, tmp_path: Path):
        """Never let a cleanup check pass before cleanup actually succeeds."""
        config = wave2_config(tmp_path)
        broken = cleanup_outcome_for(
            config, ok=False, notes=["cleanup failed for msg-live-001: ThrottlingException"]
        )

        result = run_w2_10(tmp_path, config=config, cleanup=broken)

        assert result.status == _mod.STATUS_FAILED
        assert "cleanup did not complete" in result.message
        assert "ThrottlingException" in result.message

    def test_an_empty_deletion_record_fails_rather_than_passing_vacuously(
        self, tmp_path: Path
    ):
        """`ok=True` with nothing removed.

        This is the shape a "cleanup succeeded" boolean has, and it is exactly what
        the issue forbids as a substitute for observations: every per-row assertion
        below iterates the deletions, so an empty list satisfies all of them.
        """
        config = wave2_config(tmp_path)
        empty = cleanup_outcome_for(config, deletions=[], declared_items=0)

        result = run_w2_10(tmp_path, config=config, cleanup=empty)

        assert result.status == _mod.STATUS_FAILED
        assert "vacuously true" in result.message

    def test_a_declared_row_with_no_deletion_record_fails(self, tmp_path: Path):
        """Partial failure: three rows declared, two accounted for."""
        config = wave2_config(tmp_path)
        full = cleanup_outcome_for(config)
        partial = _mod.CleanupOutcome(
            ok=True,
            notes=full.notes,
            deletions=full.deletions[:-1],
            declared_items=full.declared_items,
        )

        result = run_w2_10(tmp_path, config=config, cleanup=partial)

        assert result.status == _mod.STATUS_FAILED
        assert "nobody can account for" in result.message

    def test_a_row_deleted_with_only_half_its_key_fails(self, tmp_path: Path):
        """Wrong row keys. A delete keyed on the partition key alone could match an
        unrelated item — deleting an ordinary row, which is explicitly forbidden."""
        config = wave2_config(tmp_path)
        full = cleanup_outcome_for(config)
        bent = list(full.deletions)
        bent[0] = _mod.RowDeletion(
            event_id=bent[0].event_id,
            arrived_at="",
            both_keys_present=False,
            deleted=False,
            confirmed_absent=False,
            error="partial key; refused",
        )

        result = run_w2_10(
            tmp_path,
            config=config,
            cleanup=_mod.CleanupOutcome(
                ok=True, notes=full.notes, deletions=bent, declared_items=full.declared_items
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "BOTH" in result.message

    @pytest.mark.parametrize(
        "deleted,confirmed_absent",
        [(True, False), (False, True), (False, False)],
    )
    def test_a_row_not_confirmed_absent_fails(self, tmp_path: Path, deleted, confirmed_absent):
        """Both halves are required, and each is a real failure mode.

        `deleted=True, confirmed_absent=False` is a DeleteItem that returned success
        while the row is still readable. `deleted=False, confirmed_absent=True` is
        an absence claimed without a delete having been issued — the shape a
        hand-written record takes. Absence is established by a CONSISTENT read
        because an eventually-consistent one can report an item gone before it is.
        """
        config = wave2_config(tmp_path)
        full = cleanup_outcome_for(config)
        bent = list(full.deletions)
        bent[1] = _mod.RowDeletion(
            event_id=bent[1].event_id,
            arrived_at=bent[1].arrived_at,
            both_keys_present=True,
            deleted=deleted,
            confirmed_absent=confirmed_absent,
        )

        result = run_w2_10(
            tmp_path,
            config=config,
            cleanup=_mod.CleanupOutcome(
                ok=True, notes=full.notes, deletions=bent, declared_items=full.declared_items
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not confirmed absent" in result.message

    def test_a_deletion_the_config_never_declared_fails(self, tmp_path: Path):
        """What a scan-and-delete would produce.

        Cleanup must be bounded to declared pairs — no scan, no prefix, no wildcard
        — so an undeclared deletion means an ordinary row was reachable. Asserted
        here as well as in `run_cleanup` because this is the record the evaluation
        actually reads.
        """
        config = wave2_config(tmp_path)
        full = cleanup_outcome_for(config)
        extra = _mod.RowDeletion(
            event_id="msg-someone-elses-real-row",
            arrived_at="2026-09-12T00:00:00Z",
            both_keys_present=True,
            deleted=True,
            confirmed_absent=True,
        )

        result = run_w2_10(
            tmp_path,
            config=config,
            cleanup=_mod.CleanupOutcome(
                ok=True,
                notes=full.notes,
                deletions=[*full.deletions, extra],
                declared_items=full.declared_items + 1,
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "never declared" in result.message
        assert "msg-someone-elses-real-row" in result.message

    def test_deleting_the_unknown_run_id_fails(self, tmp_path: Path):
        """The unknown run ID names a row that must NOT exist, so deleting it means
        the harness removed an object it did not create."""
        config = wave2_config(tmp_path)
        unknown = str(config["unknown_run_id"])
        config["cleanup_items"] = [
            *config["cleanup_items"],
            {"event_id": unknown, "arrived_at": "2026-09-12T00:00:00Z"},
        ]

        result = run_w2_10(tmp_path, config=config, cleanup=cleanup_outcome_for(config))

        assert result.status == _mod.STATUS_FAILED
        assert unknown in result.message

    # ---- the pre-teardown capture: it must have HAPPENED ---------------

    def test_an_absent_capture_is_not_run_never_a_pass(self, tmp_path: Path):
        """The ordering defect's other half, stated as a status.

        `capture=None` is what a run looks like from inside this check when nobody
        observed the live capability surface before teardown. These observations are
        unrecoverable — after teardown there is no deployment to read — so the honest
        answer is `not_run`, nonzero, and specifically not a pass. The tempting wrong
        answer is to treat the now-absent fixture as confirmation.
        """
        result = run_w2_10(tmp_path, capture=None)

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "never observed while the fixture existed" in result.message

    def test_a_capture_that_did_not_complete_fails(self, tmp_path: Path):
        """`ok=False` carries the reason the capture could not be made.

        `capture_security_observations` records its failures rather than raising,
        precisely so a failed capture cannot abort the run before teardown — the
        fixture has to come down either way. That design only works if the failure is
        still fatal to W2-10's verdict, which is what this asserts.
        """
        config = wave2_config(tmp_path)
        broken = capture_for(
            config, ok=False, notes=["no owner token in the environment; cannot capture"]
        )

        result = run_w2_10(tmp_path, config=config, capture=broken)

        assert result.status == _mod.STATUS_FAILED
        assert "did not complete" in result.message
        assert "owner token" in result.message

    def test_an_absent_capture_artifact_is_not_run(self, tmp_path: Path):
        result = run_w2_10(tmp_path, capture_artifact=False)

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "security_capture" in result.message

    def test_an_absent_verification_artifact_is_not_run(self, tmp_path: Path):
        result = run_w2_10(tmp_path, verification=False)

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "teardown_verification" in result.message

    # ---- the executable teardown seam ----------------------------------
    #
    # Root's second finding: `main` captured live state, deleted ROWS only, and then
    # immediately read an artifact already claiming the pods and queues were gone.
    # Nothing in between removed a resource. These tests are about the lifecycle
    # rather than the artifact's contents — whether the removal actually happened,
    # whether the harness caused it, and whether the observations postdate it.

    def test_an_undeclared_teardown_command_is_not_run(self, tmp_path: Path):
        """No seam configured means the lifecycle was never executed.

        `not_run`, not a pass, and this is the distinction the whole finding rests on:
        a fixture that never tore its resources down has not demonstrated verified
        cleanup, and the absence artifact it offers would necessarily predate the
        removal it describes. Nonzero either way.
        """
        config = wave2_config(tmp_path)
        config.pop("resource_teardown", None)
        # Passed explicitly so `run_wave2`'s default cannot put the key back.
        result = run_w2_10(
            tmp_path,
            config=config,
            teardown=_mod.run_resource_teardown(config),
        )

        assert result.status == _mod.STATUS_NOT_RUN
        assert result.status != _mod.STATUS_PASSED
        assert "resource_teardown" in result.message

    def test_a_teardown_the_harness_never_invoked_fails(self, tmp_path: Path):
        """A declared-but-unrun seam is worse than an undeclared one.

        Undeclared is an incomplete fixture. Declared and not invoked means the
        lifecycle was described and then skipped, so the absence artifact is again
        older than the removal it reports. That is a failure, not a missing input.
        """
        result = run_w2_10(
            tmp_path,
            teardown=_mod.ResourceTeardown(
                configured=True,
                invoked=False,
                ok=False,
                exit_code=None,
                started_at=None,
                finished_at=None,
                stdout_digest=None,
                verification_present_before=False,
                verification_digest_before=None,
                notes=["declared but never run"],
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "declared but not invoked" in result.message

    def test_a_failing_teardown_command_fails_the_check(self, tmp_path: Path):
        """A nonzero teardown means the resources are not established as removed.

        The command really runs and really reports failure, so this is the honest
        model of a teardown script that could not finish — and W2-10 must not pass
        before teardown has actually succeeded, whatever the artifact says.
        """
        resources = FixtureResources()
        result = run_w2_10(
            tmp_path,
            resources=resources,
            runner=teardown_runner_for(
                tmp_path,
                wave2_config(tmp_path),
                resources,
                exit_code=1,
                remove=False,
                write_verification=False,
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "resource teardown failed" in result.message

    def test_an_unrunnable_teardown_command_fails_rather_than_crashing(
        self, tmp_path: Path
    ):
        """A missing script is a failure of the evaluation, not of the harness.

        `run_resource_teardown` records the exception instead of raising, because
        `main` calls it on the path to row cleanup and an exception here would abandon
        deletions that still have to happen — leaving the fixture in the DP-INV-1
        state this whole evaluation exists to prevent.
        """
        resources = FixtureResources()
        result = run_w2_10(
            tmp_path,
            resources=resources,
            runner=teardown_runner_for(
                tmp_path,
                wave2_config(tmp_path),
                resources,
                raises=FileNotFoundError("teardown-control-fixture.sh"),
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "could not be run" in result.message

    def test_a_prefilled_absence_artifact_cannot_pass(self, tmp_path: Path):
        """The defect itself, reproduced end to end.

        The artifact is complete, internally consistent, correctly dated relative to
        nothing, and says every resource is gone — exactly the file the previous
        revision accepted. What is wrong is that the teardown command did not write
        it: the bytes on disk are identical before and after the removal, so the
        observations in it were made while the fixture still existed.

        This is why freshness is established by the harness's own digest rather than
        by a field in the artifact. Every claim inside the file, `captured_at`
        included, is written by the same hand as the absence claims.
        """
        resources = FixtureResources()
        result = run_w2_10(
            tmp_path,
            resources=resources,
            # Removes the resources, but records nothing: the pre-seeded artifact from
            # `wave2_only_artifact_payloads()` is left exactly as it was.
            runner=teardown_runner_for(
                tmp_path,
                wave2_config(tmp_path),
                resources,
                write_verification=False,
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "unchanged across the resource teardown" in result.message

    def test_a_rewritten_but_backdated_artifact_cannot_pass(self, tmp_path: Path):
        """The gap the digest comparison alone would leave.

        A teardown script that rewrites the file — so the bytes do change — but stamps
        it with observations from before the removal. The digest check is satisfied and
        the dating check is what catches it. Both are needed: the digest catches an
        untouched file with a plausible timestamp, and the timestamp catches a touched
        file describing an earlier moment.
        """
        result = run_w2_10(
            tmp_path,
            verification=teardown_verification_payload(
                captured_at=relative_time(-3600)
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "before the fixture's resource teardown even began" in result.message

    @pytest.mark.parametrize("captured_at", [None, "", "shortly after teardown", 0])
    def test_an_undatable_absence_artifact_fails(self, tmp_path: Path, captured_at):
        """An artifact that cannot be dated cannot be shown to postdate anything.

        `"shortly after teardown"` is the entry that matters: it is a truthy string, so
        a presence check would accept it, and it is precisely the kind of value someone
        writes when the real timestamp was not recorded.
        """
        result = run_w2_10(
            tmp_path,
            verification=teardown_verification_payload(captured_at=captured_at),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "captured_at" in result.message

    def test_a_teardown_that_removes_nothing_cannot_report_absence(
        self, tmp_path: Path
    ):
        """The seam and the resource state cannot disagree.

        The command exits 0 and writes the artifact, but removes nothing — so the
        absence observations it reads back say `absent: false`, because they are
        computed from the resources rather than asserted alongside them. A fixture
        whose resources model nothing at all could not express this case, which is why
        `FixtureResources` exists.
        """
        resources = FixtureResources()
        result = run_w2_10(
            tmp_path,
            resources=resources,
            runner=teardown_runner_for(
                tmp_path, wave2_config(tmp_path), resources, remove=False
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "still present after teardown" in result.message
        assert resources.present, "the resources must really still be there"

    def test_the_capture_happens_before_the_teardown_removes_anything(
        self, tmp_path: Path
    ):
        """Ordering, asserted against the resources rather than against a flag.

        The live capability reads need the fixture to exist; the absence observations
        need it gone. If the seam ran first, the capture would be reading a torn-down
        deployment and a CORRECT teardown would produce `not_run` — the inverted
        ordering that made this check unpassable before.
        """
        resources = FixtureResources()
        observed_during_capture = {}
        # Bound before patching, or the wrapper would resolve to itself.
        real_capture = _mod.capture_security_observations

        def recording_capture(driver, config):
            observed_during_capture["present"] = len(resources.present)
            return real_capture(driver, config)

        with patch.object(_mod, "capture_security_observations", recording_capture):
            result = run_w2_10(tmp_path, resources=resources)

        assert result.status == _mod.STATUS_PASSED, result.message
        # All three resources were still up when the capture was taken...
        assert observed_during_capture["present"] == len(creation_ledger())
        # ...and all three are gone by the end.
        assert resources.present == {}

    @pytest.mark.parametrize("value", [False, None, "yes", "true"])
    def test_a_capture_not_recorded_as_pre_teardown_fails(self, tmp_path: Path, value):
        """The flag that makes the ordering auditable.

        `"yes"` and `"true"` are in the table because a truthiness test would accept
        both, and the thing being asserted is an operator's positive statement that
        these reads happened before teardown — recorded after it, they describe an
        environment that no longer existed.
        """
        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(captured_before_teardown=value)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "captured_before_teardown" in result.message

    @pytest.mark.parametrize("value", [False, None, "yes"])
    def test_a_verification_not_recorded_as_post_teardown_fails(self, tmp_path: Path, value):
        """The mirror of the above, and the reason both flags exist.

        Absence observations recorded BEFORE teardown would describe the fixture while
        it still existed — they would be observations of presence relabelled as
        absence.
        """
        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(verified_after_teardown=value)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "verified_after_teardown" in result.message

    def test_an_incomplete_capture_artifact_fails(self, tmp_path: Path):
        payload = security_capture_payload()
        del payload["isolation_present"]

        result = run_w2_10(tmp_path, capture_artifact=payload)

        assert result.status == _mod.STATUS_FAILED
        assert "isolation_present" in result.message

    # ---- the capture must be bound to the DEPLOYED build ---------------

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    @pytest.mark.parametrize("key", ["revision", "image_digest"])
    def test_a_capture_bound_to_a_different_build_fails(
        self, tmp_path: Path, component, key
    ):
        """Root's second finding, in the check that motivated it.

        The observation was TRUE — just not of this deployment. What it is compared
        against is the preflight's `deployed_components`: what is actually RUNNING,
        per component, revision and digest. An earlier revision compared it against
        story S2's historical merge commit instead, which a correct deployment does
        not equal, so a newer compatible build failed.
        """
        observed = security_capture_payload()["observed_revisions"]
        observed[component] = {**observed[component], key: "9" * (40 if key == "revision" else 64)}

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(observed_revisions=observed)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "stale-evidence" in result.message
        assert component in result.message

    def test_a_capture_bound_to_a_story_merge_commit_fails(self, tmp_path: Path):
        """The specific wrong subject root's review named, asserted directly.

        S2's merge commit is a real revision in the fixture's history and it is NOT
        what is deployed — the deployment is newer. An implementation comparing
        against it would pass this input and fail every correct deployment, so the
        test asserting it FAILS is what keeps that implementation from returning.
        """
        observed = {
            component: {"revision": WAVE2_REVISION, "image_digest": entry["image_digest"]}
            for component, entry in deployed_components().items()
        }

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(observed_revisions=observed)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "stale-evidence" in result.message

    @pytest.mark.parametrize("component", ["worker", "gateway"])
    def test_a_capture_that_names_no_build_for_a_component_fails(
        self, tmp_path: Path, component
    ):
        """An unbound observation is a true statement about an unknown subject."""
        observed = security_capture_payload()["observed_revisions"]
        del observed[component]

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(observed_revisions=observed)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "records no observed revision/digest" in result.message
        assert component in result.message

    @pytest.mark.parametrize("bad", [{}, None, "c" * 40, []])
    def test_a_capture_with_no_build_binding_at_all_fails(self, tmp_path: Path, bad):
        """`"c"*40` is the shape an earlier revision used: one flat revision string.

        It cannot distinguish the two independently-shipped components, so it cannot
        say which of them the observation actually describes.
        """
        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(observed_revisions=bad)
        )

        assert result.status == _mod.STATUS_FAILED

    # ---- both artifacts must describe THIS fixture run -----------------

    @pytest.mark.parametrize(
        "artifact", ["security_capture", "teardown_verification"]
    )
    @pytest.mark.parametrize("key", ["account_id", "environment"])
    def test_an_artifact_describing_another_fixture_fails(
        self, tmp_path: Path, artifact, key
    ):
        """A complete, internally consistent artifact from a previous run.

        Parametrized over both artifacts because either one carried over would make
        half the verdict evidence about a fixture nobody is evaluating, and over both
        identity fields because the same account in a different environment is a
        different fixture.
        """
        wrong = {"account_id": "000000000000", "environment": "staging"}[key]
        payload = (
            security_capture_payload
            if artifact == "security_capture"
            else teardown_verification_payload
        )(fixture_identity=fixture_identity(**{key: wrong}))

        kwargs = (
            {"capture_artifact": payload}
            if artifact == "security_capture"
            else {"verification": payload}
        )
        result = run_w2_10(tmp_path, **kwargs)

        assert result.status == _mod.STATUS_FAILED
        assert "describes a different fixture" in result.message

    def test_a_capture_artifact_describing_another_run_fails(self, tmp_path: Path):
        """The artifact's run and the harness's own capture must be the same run.

        This is the cross-check that cannot be satisfied by a consistent forgery: the
        harness knows which run it read, so an artifact naming a different one is
        evidence from another evaluation regardless of how complete it is.
        """
        result = run_w2_10(
            tmp_path,
            capture_artifact=security_capture_payload(
                fixture_identity=fixture_identity(run_id="msg-some-other-run")
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "observations from another run" in result.message

    def test_the_two_halves_must_describe_the_same_run(self, tmp_path: Path):
        """Capture of fixture A, teardown of fixture B.

        Each artifact is individually complete and consistent; together they describe
        a security posture that was never torn down and a teardown whose posture was
        never observed. Only comparing them catches it.
        """
        result = run_w2_10(
            tmp_path,
            verification=teardown_verification_payload(
                fixture_identity=fixture_identity(run_id="msg-live-002")
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "must be about the same fixture run" in result.message

    def test_a_missing_preflight_does_not_double_count_as_a_w2_10_failure(
        self, tmp_path: Path
    ):
        """One missing artifact must not read as two independent defects.

        W2-01 owns the absent-preflight failure. If W2-10 re-raised it, a single
        operator omission would read as two failures — which inflates the apparent
        damage and sends the reader looking for a second cause. So W2-10's build
        cross-check is skipped rather than failed.

        What it must NOT do is pass: the creation ledger lives in that preflight, and
        without it teardown completeness cannot be measured against anything. So the
        honest answer is `not_run` — "could not measure" — which is still nonzero.
        This is the distinction the whole check rests on: skipping a comparison
        because another check owns it is fine; skipping the MEASUREMENT is not.
        """
        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
            **wave2_only_artifact_payloads(),
        }
        payloads.pop("wave2_preflight")
        config = wave2_config(tmp_path, artifact_payloads=payloads)
        results = run_wave2(tmp_path, config=config)

        assert results["W2-01"].status == _mod.STATUS_NOT_RUN
        assert results["W2-10"].status == _mod.STATUS_NOT_RUN
        assert results["W2-10"].status != _mod.STATUS_PASSED
        assert "creation ledger is unavailable" in results["W2-10"].message
        # And the absent preflight is not reported as a W2-10 FAILURE.
        assert results["W2-10"].status != _mod.STATUS_FAILED

    def test_a_malformed_ledger_does_not_become_an_empty_one(self, tmp_path: Path):
        """The subtle version of the above, and the one an implementation gets wrong.

        W2-01 owns a malformed ledger, so W2-10 swallows the validation error to avoid
        double-counting — and the trap is that swallowing it leaves an EMPTY ledger,
        against which "every created resource was removed" is vacuously true. The
        emptiness check is what stops a malformed ledger from becoming a pass.
        """
        preflight = wave2_preflight_payload(creation_ledger=[{"kind": "Pod"}])
        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
            **wave2_only_artifact_payloads(),
            "wave2_preflight": preflight,
        }
        config = wave2_config(tmp_path, artifact_payloads=payloads)
        results = run_wave2(tmp_path, config=config)

        assert results["W2-01"].status == _mod.STATUS_FAILED
        assert results["W2-10"].status == _mod.STATUS_NOT_RUN
        assert results["W2-10"].status != _mod.STATUS_PASSED

    # ---- teardown completeness, against the creation ledger ------------

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_created_resource_with_no_absence_observation_fails(
        self, tmp_path: Path, index
    ):
        """Root's fourth finding, and the exact way the previous check was fooled.

        The removal record was a caller-chosen map of names to booleans, so OMITTING a
        leaked resource passed — nothing compared the list against what the fixture
        created. Here each ledger entry is dropped from the removals in turn, because
        a check that reconciled only the first would leave the rest omittable. Every
        one of them must be named as unaccounted for.
        """
        ledger = creation_ledger()
        dropped = ledger[index]
        removals = [
            removal
            for removal in ledger_removals()
            if removal["identity"] != dropped["identity"]
        ]

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "no post-teardown absence observation" in result.message
        assert dropped["name"] in result.message
        assert dropped["identity"] in result.message

    @pytest.mark.parametrize("index", [0, 1, 2])
    def test_a_resource_still_present_after_teardown_fails(self, tmp_path: Path, index):
        """A fixture workload left running is a control-enabled pod outliving its
        evaluation — the concrete harm DP-INV-1 exists to prevent."""
        ledger = creation_ledger()
        identity = ledger[index]["identity"]
        removals = ledger_removals(
            **{
                identity: bent_removal(
                    identity,
                    absent=False,
                    observed_by="kubectl get -o name returned the object",
                )
            }
        )

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "still present after teardown" in result.message
        assert ledger[index]["name"] in result.message

    @pytest.mark.parametrize("absent", [None, "true", "yes", 1, "absent"])
    def test_an_absence_not_recorded_true_fails(self, tmp_path: Path, absent):
        """`"true"`, `"yes"` and `1` are the table's whole point: a truthiness test
        would accept all three, and each is a string or number someone typed rather
        than the outcome of a read."""
        identity = creation_ledger()[0]["identity"]
        removals = ledger_removals(**{identity: bent_removal(identity, absent=absent)})

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "still present after teardown" in result.message

    def test_removing_something_the_fixture_never_created_fails(self, tmp_path: Path):
        """The other direction, and it is not a formality.

        A removal of an identity the ledger does not contain means teardown had an
        unrelated resource in reach — someone else's workload, deleted by this
        evaluation. Reconciliation in one direction only would treat that as a bonus.
        """
        removals = [
            *ledger_removals(),
            {
                "identity": "uid:99999999-9999-9999-9999-999999999999",
                "absent": True,
                "observed_by": "kubectl get deployment someone-elses-app",
                # Complete, so it fails for being unrelated to this fixture rather than
                # for being malformed. An incomplete literal here would pass the test
                # while leaving the reconciliation direction it names unasserted.
                "removed_at": relative_time(LEDGER_REMOVAL_OFFSETS["Deployment"]),
            },
        ]

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "which the creation ledger does not contain" in result.message

    def test_a_removal_keyed_by_name_instead_of_identity_fails(self, tmp_path: Path):
        """Names are not identities, and this is why the ledger records both.

        `kubectl apply` can adopt a pre-existing same-name object, so "some object
        called control-probe-1 is gone" is also satisfied by a resource that was
        recreated under the same name. Only the observed UID distinguishes "this exact
        object is gone".
        """
        # Complete observations in every respect EXCEPT that they are keyed by name, so
        # the only thing the check can object to is the identity.
        removals = [
            {
                **bent_removal(entry["identity"]),
                "identity": entry["name"],
            }
            for entry in creation_ledger()
        ]

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "the creation ledger does not contain" in result.message

    @pytest.mark.parametrize("key", _mod.LEDGER_REMOVAL_KEYS)
    def test_a_removal_missing_a_required_field_fails(self, tmp_path: Path, key):
        """`observed_by` is the one that matters most: it records HOW absence was
        established, and without it the entry is the asserted boolean root's review
        told us to replace."""
        removals = ledger_removals()
        del removals[0][key]

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize("blank", ["", None, False])
    def test_an_unattributed_absence_claim_fails(self, tmp_path: Path, blank):
        """Present but empty, which a key-presence check alone would accept."""
        identity = creation_ledger()[0]["identity"]
        removals = ledger_removals(
            **{identity: bent_removal(identity, observed_by=blank)}
        )

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "records no 'observed_by'" in result.message

    @pytest.mark.parametrize("vacuous", [[], {}, True, "all gone", None])
    def test_a_vacuous_removal_record_fails(self, tmp_path: Path, vacuous):
        """"All gone: true" cannot say which resource was checked.

        `[]` and `{}` are the important entries: an empty record makes every
        per-resource assertion above iterate nothing, so all of them pass — which is
        indistinguishable from a fixture nobody looked for. It is only caught because
        the ledger says three resources exist.
        """
        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=vacuous)
        )

        assert result.status == _mod.STATUS_FAILED
        assert result.status != _mod.STATUS_PASSED

    def test_a_name_keyed_boolean_map_is_not_accepted_at_all(self, tmp_path: Path):
        """The exact shape root's fourth finding rejected, asserted as a shape.

        `{"agent-worker-fixture-1": true, "control-probe-1": true}` — the caller-chosen
        map of names to booleans. Every value is `true` and every name is real, and it
        still must not pass, because it carries no identity and no observation and its
        completeness is whatever the caller chose to type.
        """
        result = run_w2_10(
            tmp_path,
            verification=teardown_verification_payload(
                removals={entry["name"]: True for entry in creation_ledger()}
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert result.status != _mod.STATUS_PASSED

    # ---- posture that must SURVIVE teardown ----------------------------

    def test_baseline_isolation_removed_along_with_the_fixture_fails(
        self, tmp_path: Path
    ):
        """Teardown must not take the environment's own isolation with it.

        The distinction this asserts is root's fourth finding. The previous revision
        required a single `isolation_present: true` after teardown while ALSO requiring
        every created resource to be absent — and the fixture's NetworkPolicies are
        created resources. So the two requirements could not both be met: either a
        policy was left behind (a leak, the thing DP-INV-1 forbids) or it was removed
        and "isolation present" was false. The check was unsatisfiable, which in
        practice means it was going to be satisfied by whichever answer someone wrote.

        They are two different objects with opposite lifecycles. The fixture's own
        policies must be GONE — asserted by the ledger reconciliation above. The
        environment's persistent baseline isolation must REMAIN, which is this.
        """
        result = run_w2_10(
            tmp_path,
            verification=teardown_verification_payload(
                baseline_isolation_present=False
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "baseline isolation is not recorded as still present" in result.message

    def test_removing_the_fixture_policy_before_its_listener_fails(
        self, tmp_path: Path
    ):
        """The window the end state cannot show, and the other half of finding 4.

        Both the control-enabled workload and its NetworkPolicy are gone afterwards,
        so every absence assertion above is satisfied. What is wrong is the ORDER:
        the policy went first, leaving an interval in which a control-enabled pod was
        running with its ingress restriction already deleted. That interval is strictly
        worse than either end state, and DP-INV-1 is about the interval.

        The unsafe order is produced by making the teardown command genuinely remove
        things in the wrong sequence, not by editing a timestamp — so what the test
        exercises is a real unsafe teardown rather than a doctored record of a safe one.
        """
        resources = FixtureResources(
            removal_order=("NetworkPolicy", "Deployment", "Pod")
        )

        result = run_w2_10(tmp_path, resources=resources)

        assert result.status == _mod.STATUS_FAILED
        assert "BEFORE the control-enabled workload" in result.message

    def test_removing_the_fixture_policy_after_its_listener_passes(
        self, tmp_path: Path
    ):
        """The positive control for the ordering, which the test above needs.

        Without this, an implementation that rejected every removal order would
        satisfy the negative while making the check impossible to pass — the failure
        mode that turns a safety requirement into an unsatisfiable one, which is what
        finding 4 was about in the first place.
        """
        resources = FixtureResources(
            removal_order=("Deployment", "Pod", "NetworkPolicy")
        )

        result = run_w2_10(tmp_path, resources=resources)

        assert result.status == _mod.STATUS_PASSED, result.message

    @pytest.mark.parametrize("kind", ["Pod", "NetworkPolicy"])
    def test_an_undated_removal_leaves_the_order_unestablished(
        self, tmp_path: Path, kind
    ):
        """Both sides of the pair need a time, or the order is not checkable.

        Parametrized over the workload and the policy because an implementation that
        required the stamp on only one of them would let the other be omitted — and
        then the comparison silently does not happen, which reads as a pass.
        """
        identity = next(
            entry["identity"] for entry in creation_ledger() if entry["kind"] == kind
        )
        removals = ledger_removals(
            **{identity: bent_removal(identity, removed_at=None)}
        )

        result = run_w2_10(
            tmp_path, verification=teardown_verification_payload(removals=removals)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not a parseable instant" in result.message

    def test_isolation_absent_during_the_capture_fails(self, tmp_path: Path):
        """The same invariant at the other end of the run.

        Isolation missing while the fixture was RUNNING is the more serious of the two:
        a control-enabled listener was reachable. Both ends are asserted because a
        check reading only one would leave the other window unobserved.
        """
        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(isolation_present=False)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "isolation was not recorded as present while the fixture was running" in (
            result.message
        )

    @pytest.mark.parametrize("stage", ["capture", "verification"])
    def test_ordinary_flags_on_fails_at_either_end(self, tmp_path: Path, stage):
        payload = (
            security_capture_payload if stage == "capture" else teardown_verification_payload
        )(ordinary_flags_off=False)
        kwargs = (
            {"capture_artifact": payload} if stage == "capture" else {"verification": payload}
        )

        result = run_w2_10(tmp_path, **kwargs)

        assert result.status == _mod.STATUS_FAILED
        assert "ordinary" in result.message

    @pytest.mark.parametrize("value", [True, "false", None])
    @pytest.mark.parametrize("stage", ["capture", "verification"])
    def test_general_flag_enablement_fails_at_either_end(
        self, tmp_path: Path, stage, value
    ):
        """Widening the flag to make a check pass is the specific shortcut this
        assertion forbids. `"false"` and `None` are in the table because a
        truthiness test would accept both as "not enabled"; both ends are covered
        because enabling it during the run and leaving it enabled after are different
        failures with the same fix.
        """
        payload = (
            security_capture_payload if stage == "capture" else teardown_verification_payload
        )(general_flag_enablement=value)
        kwargs = (
            {"capture_artifact": payload} if stage == "capture" else {"verification": payload}
        )

        result = run_w2_10(tmp_path, **kwargs)

        assert result.status == _mod.STATUS_FAILED
        assert "general_flag_enablement" in result.message

    @pytest.mark.parametrize(
        "prop",
        [
            "unauthenticated_rejected",
            "cross_tenant_indistinguishable",
            "nonowner_indistinguishable",
            "transport_targets_blocked",
            "no_token_in_public_state",
            "admission_authorization_preserved",
            "delivery_authorization_preserved",
        ],
    )
    def test_a_failed_wave_one_security_probe_fails(self, tmp_path: Path, prop):
        """Each wave-1 guarantee is a separate property with a separate failure
        mode, so each is parametrized rather than rolled into one probe result.

        `admission_authorization_preserved` and `delivery_authorization_preserved`
        are the two worth naming: #5029 requires authorization to be revalidated
        immediately before physical handoff, and pause/resume is precisely the code
        path that could have moved that revalidation earlier.
        """
        security = dict(security_capture_payload()["wave1_security"])
        security[prop] = False

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(wave1_security=security)
        )

        assert result.status == _mod.STATUS_FAILED
        assert prop in result.message

    @pytest.mark.parametrize(
        "prop",
        ["admission_authorization_preserved", "delivery_authorization_preserved"],
    )
    def test_an_unrecorded_wave_one_security_property_fails(self, tmp_path: Path, prop):
        """Absent is not the same as false, and neither is a pass. An unrecorded
        property is one nobody re-observed."""
        security = dict(security_capture_payload()["wave1_security"])
        del security[prop]

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(wave1_security=security)
        )

        assert result.status == _mod.STATUS_FAILED
        assert prop in result.message
        assert "nobody re-observed" in result.message

    # ---- unsupported verbs: recorded, advertised AND attempted ---------

    @pytest.mark.parametrize("status", [200, 202, 403, 404, 500, "501"])
    def test_an_unsupported_verb_not_answering_501_fails(self, tmp_path: Path, status):
        """An authorized request for a verb this build does not implement must be
        refused as unimplemented. `200` is the alarming one — the verb was enabled
        — and `"501"` is in the table because a stringified status would pass a
        loose comparison."""
        verbs = dict(security_capture_payload()["unsupported_verbs"])
        verbs["abort"] = status

        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(unsupported_verbs=verbs)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "did not answer 501" in result.message

    @pytest.mark.parametrize("vacuous", [{}, None, True])
    def test_a_vacuous_unsupported_verb_claim_fails(self, tmp_path: Path, vacuous):
        result = run_w2_10(
            tmp_path, capture_artifact=security_capture_payload(unsupported_verbs=vacuous)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "nonempty object" in result.message

    @pytest.mark.parametrize("claim", [True, "false", None])
    def test_an_adapter_capability_not_recorded_false_fails(self, tmp_path: Path, claim):
        caps = dict(security_capture_payload()["unsupported_adapter_capabilities"])
        caps["abort"] = claim

        result = run_w2_10(
            tmp_path,
            capture_artifact=security_capture_payload(
                unsupported_adapter_capabilities=caps
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "not recorded as false" in result.message

    @pytest.mark.parametrize("vacuous", [{}, None])
    def test_a_vacuous_adapter_capability_claim_fails(self, tmp_path: Path, vacuous):
        result = run_w2_10(
            tmp_path,
            capture_artifact=security_capture_payload(
                unsupported_adapter_capabilities=vacuous
            ),
        )

        assert result.status == _mod.STATUS_FAILED
        assert "nonempty object" in result.message

    def test_a_deployment_that_contradicts_the_recording_fails(self, tmp_path: Path):
        """The half the harness observes itself, and why the artifact is not enough.

        The recording describes what the operator probed; the capture is what the
        deployment told the HARNESS while it was still running. A deployment
        advertising a verb the artifact calls unsupported means the recording and the
        deployment disagree — and the deployment is what the next operator inherits.

        Note the client is passed to `run_w2_10`, so the same stub produces both the
        capture and the check's own reads: the disagreement is between the artifact and
        a real observation, not between two hand-written values.
        """
        client = wave2_gateway_stub(
            state_capabilities={verb: True for verb in _mod.CONTROL_VERBS}
        )

        result = run_w2_10(tmp_path, client=client)

        assert result.status == _mod.STATUS_FAILED
        assert "recording and the deployment disagree" in result.message

    def test_a_state_response_with_no_capability_map_fails(self, tmp_path: Path):
        client = wave2_gateway_stub(state_omit=("capabilities",))

        result = run_w2_10(tmp_path, client=client)

        assert result.status == _mod.STATUS_FAILED
        assert result.status != _mod.STATUS_PASSED

    # ---- the harness's own authorized attempt at each refused verb -----

    @pytest.mark.parametrize("verb", UNSUPPORTED_VERBS)
    def test_a_verb_the_capture_never_attempted_fails(self, tmp_path: Path, verb):
        """The advertised map alone is the deployment's claim about itself.

        A capability map saying `abort: false` is what the build SAYS. Until someone
        POSTs `abort` as an authorized owner, nothing has observed what it does — and
        an enabled-but-still-advertised-false verb is exactly the quiet relaxation that
        gap would hide. So an unattempted verb is a failure, not a pass.

        The capture is bent by dropping one verb from `refusals`, which is what a
        capture that could not complete its attempts would produce.
        """
        config = wave2_config(tmp_path)
        real = capture_for(config)
        bent = [
            _mod.LiveCapabilityCapture(
                adapter=entry.adapter,
                run_id=entry.run_id,
                status=entry.status,
                capabilities=entry.capabilities,
                refusals={k: v for k, v in entry.refusals.items() if k != verb},
                error=entry.error,
            )
            for entry in real.adapters
        ]

        result = run_w2_10(
            tmp_path, config=config, capture=capture_for(config, adapters=bent)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "made no authorized attempt" in result.message
        assert verb in result.message

    @pytest.mark.parametrize("observed", [200, 202, 403, 500, None])
    def test_an_attempt_that_contradicts_the_recorded_status_fails(
        self, tmp_path: Path, observed
    ):
        """The comparison root's review asked for: observation against recording.

        `200` is the one that matters — the verb actually worked when POSTed, while
        both the artifact and the capability map called it unimplemented. A check
        reading only the map would report that deployment as correct. `None` is a
        transport failure: an attempt that produced no status observed nothing.
        """
        config = wave2_config(tmp_path)
        real = capture_for(config)
        bent = [
            _mod.LiveCapabilityCapture(
                adapter=entry.adapter,
                run_id=entry.run_id,
                status=entry.status,
                capabilities=entry.capabilities,
                refusals={verb: observed for verb in entry.refusals},
                error=entry.error,
            )
            for entry in real.adapters
        ]

        result = run_w2_10(
            tmp_path, config=config, capture=capture_for(config, adapters=bent)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "describe different deployments" in result.message

    def test_the_capture_really_did_attempt_the_unsupported_verbs(self, tmp_path: Path):
        """The premise behind every test above, asserted against the real capture.

        If `capture_security_observations` did not actually POST the refused verbs, the
        comparisons in W2-10 would be trivially satisfiable and this whole section
        would be testing nothing. Asserted on the stub's recorded calls, so it is the
        requests that were really issued.

        Equally important: it attempted ONLY those verbs. `pause` and `resume` are
        implemented in this wave, so POSTing them would act on the run the other nine
        checks are still describing — which is why the capture is confined to verbs the
        deployment reports unavailable.
        """
        config = wave2_config(tmp_path)
        client = wave2_gateway_stub()
        capture = capture_for(config, client)

        posted = [
            call.args[1] if len(call.args) > 1 else call.kwargs.get("url")
            for call in client.request.call_args_list
            if (call.args[0] if call.args else call.kwargs.get("method")) == "POST"
        ]

        assert capture.ok, capture.notes
        for entry in capture.adapters:
            assert set(entry.refusals) == set(UNSUPPORTED_VERBS), entry.adapter
        for verb in UNSUPPORTED_VERBS:
            assert any(f"/{verb}" in url for url in posted), verb
        for implemented in ("pause", "resume"):
            assert not any(f"/{implemented}" in url for url in posted), implemented

    @pytest.mark.parametrize("adapter", sorted(_mod.ADAPTERS))
    def test_a_capture_that_missed_an_adapter_edge_fails(self, tmp_path: Path, adapter):
        """Two edges, one control service, and drift between them is invisible to a
        capture of only one. Both are parametrized because whichever edge a check read
        would be the one that could not be wrong."""
        config = wave2_config(tmp_path)
        real = capture_for(config)
        kept = [entry for entry in real.adapters if entry.adapter != adapter]

        result = run_w2_10(
            tmp_path, config=config, capture=capture_for(config, adapters=kept)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "did not observe" in result.message
        assert adapter in result.message

    @pytest.mark.parametrize("status", [404, 500, 503, None])
    def test_a_capture_read_that_did_not_answer_fails(self, tmp_path: Path, status):
        """A non-200 here is the deployment failing to answer, not a torn-down fixture.

        That is the whole reason this read moved BEFORE teardown: afterwards a 404 is
        expected and says nothing, so it cannot be distinguished from a broken gateway.
        Beforehand the fixture is running, so anything other than 200 is a real defect.
        """
        config = wave2_config(tmp_path)
        real = capture_for(config)
        bent = [
            _mod.LiveCapabilityCapture(
                adapter=entry.adapter,
                run_id=entry.run_id,
                status=status,
                capabilities=entry.capabilities,
                refusals=entry.refusals,
                error=None if status else "connection reset by peer",
            )
            for entry in real.adapters
        ]

        result = run_w2_10(
            tmp_path, config=config, capture=capture_for(config, adapters=bent)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "pre-teardown capability read returned status" in result.message

    def test_a_capture_with_no_capability_map_fails(self, tmp_path: Path):
        config = wave2_config(tmp_path)
        real = capture_for(config)
        bent = [
            _mod.LiveCapabilityCapture(
                adapter=entry.adapter,
                run_id=entry.run_id,
                status=200,
                capabilities={},
                refusals=entry.refusals,
                error=None,
            )
            for entry in real.adapters
        ]

        result = run_w2_10(
            tmp_path, config=config, capture=capture_for(config, adapters=bent)
        )

        assert result.status == _mod.STATUS_FAILED
        assert "carried no capabilities object" in result.message

    def _drive_w2_10(
        self, tmp_path: Path, config: dict, *, client=None, cleanup=None, capture=None
    ):
        """Drive W2-10 alone, bypassing `run_wave2`'s defaults.

        For the two cases that need a deliberately INCONSISTENT world — a forged
        deletion record `run_cleanup` would never emit, and a gateway that raises on
        every request — so neither can be produced by the ordered shared-store path.
        Everything else goes through `run_w2_10`, which does use it.
        """
        probe = _mod.Probe(config["gateway_url"], client or wave2_gateway_stub())
        artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
        driver = _mod.Driver(
            config, probe, artifacts, dynamodb=ddb_stub(), git_runner=CommitGraph().runner()
        )
        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            return _mod.run_checks(
                driver,
                [s for s in _mod.WAVE2_CHECKS if s.check_id == "W2-10"],
                cleanup=cleanup,
                capture=capture,
            )[0]

    def test_an_unexpected_exception_is_failed_not_passed(self, tmp_path: Path):
        """The catch-all path, asserted rather than assumed.

        An unexpected exception must not escape `run_checks` — which would abandon
        the remaining checks and write no report — and must not be swallowed into a
        pass. `AssertionError` is `failed`, `PrerequisiteMissingError` is `not_run`,
        and anything else is `failed`, because an unexplained error is not evidence
        that a property holds.

        Provoked by a config whose `cleanup_items` is a list of bare strings, with a
        deletion record that nonetheless reports success. That combination cannot
        arise from `run_cleanup` (it refuses a partial key), which is the point: it
        forces the check past its own assertions into code that raises
        `AttributeError`, exercising the path an unrelated future bug would take.
        """
        config = wave2_config(tmp_path, cleanup_items=["msg-live-001"])
        forged = _mod.CleanupOutcome(
            ok=True,
            notes=["forged"],
            deletions=[
                _mod.RowDeletion(
                    event_id="msg-live-001",
                    arrived_at="2026-09-12T00:00:00Z",
                    both_keys_present=True,
                    deleted=True,
                    confirmed_absent=True,
                )
            ],
            declared_items=1,
        )

        result = self._drive_w2_10(tmp_path, config, cleanup=forged)

        assert result.status == _mod.STATUS_FAILED
        assert result.status != _mod.STATUS_PASSED
        assert "unexpected AttributeError" in result.message

    def test_a_transport_failure_during_the_capture_is_nonzero_never_a_pass(
        self, tmp_path: Path
    ):
        """The gateway being unreachable is "could not look", not "it was fine".

        `Probe` records a transport failure as an observation with no status rather
        than propagating it, so this does not reach the catch-all above. It reaches the
        non-200 branch, which refuses to treat an unanswered read as a satisfied one.

        This case moved with the ordering fix and is worth being precise about. The
        capture happens while the fixture is RUNNING, so an unreachable gateway there is
        a real defect rather than the expected consequence of teardown — which is
        exactly why the read was moved before it. Afterwards the same failure would be
        indistinguishable from a correctly removed fixture.
        """
        client = wave2_gateway_stub()
        client.request.side_effect = RuntimeError("connection reset by peer")

        result = run_w2_10(tmp_path, client=client)

        assert result.status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}
        assert result.status != _mod.STATUS_PASSED

    def test_the_run_still_tears_down_when_the_capture_cannot_be_made(
        self, tmp_path: Path
    ):
        """The design constraint behind recording capture failures instead of raising.

        A capture that cannot complete must not abort the run before teardown — the
        fixture has to come down either way, or a failed evaluation leaves
        control-enabled workloads running. So `capture_security_observations` returns
        `ok=False` rather than propagating, and cleanup proceeds.

        Asserted on the store's rows, not on a status: the question is whether teardown
        physically happened.
        """
        config = wave2_config(tmp_path)
        store = shared_store_for(config)
        client = store.wrap(wave2_gateway_stub())
        client.request.side_effect = RuntimeError("connection reset by peer")
        probe = _mod.Probe(config["gateway_url"], client)
        artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
        dynamodb = store.dynamodb()
        driver = _mod.Driver(config, probe, artifacts, dynamodb=dynamodb)

        assert store.rows, "premise: the fixture's rows exist before teardown"
        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            capture = _mod.capture_security_observations(driver, config)
            cleanup = _mod.run_cleanup(config, dynamodb)

        assert capture.ok is False
        assert capture.notes
        assert cleanup.ok, cleanup.notes
        assert not store.rows


class TestNoPassingWave2WithoutTenChecksAndVerifiedCleanup:
    """The decisive property, asserted through the real `main`.

    Everything above tests a check in isolation. This class tests the claim the
    issue actually makes: there is NO input that produces a passing wave-2 report
    without all ten checks answered AND cleanup genuinely completed. That is a
    statement about the whole command — the exit code, the written `result.json`,
    and the `jq` gate an operator runs over it — so it cannot be established by
    driving predicates directly.

    `main` is used unmodified. What varies is the fixture on disk and the DynamoDB
    stub's behaviour, because those are the only things a real operator controls.

    **The fakes share one row store.** `_run` wires the DynamoDB client and the
    gateway to the same `SharedRowStore`, so a row teardown deletes is a row the
    gateway stops serving — the constraint a real deployment has. The previous
    fixture used two independent stubs and a gateway that answered every `/state`
    with a static 200; that let a check depending on a deleted run still see a 200,
    so the positive control below passed while the same harness reported
    `W2-10 not_run` against a real gateway. A whole-command regression whose fakes
    are mutually inconsistent cannot discriminate the defect it exists to catch.
    """

    @staticmethod
    def _run(  # noqa: PLR0913 - one parameter per substitutable collaborator
        tmp_path: Path,
        *,
        config=None,
        client=None,
        dynamodb=None,
        store=None,
        resources=None,
        runner=None,
        graph=None,
    ):
        """Drive the real CLI at `--wave 2` and return `(exit_code, report)`.

        With no `client`/`dynamodb` override, both come from one `SharedRowStore`
        seeded with the config's declared rows. Passing either separately is how a
        test models a specific inconsistency on purpose.

        The `resource_teardown` command is patched at the RUNNER, not stubbed out of
        the harness: `main` still calls the real `run_resource_teardown`, which still
        takes its own freshness snapshot of the absence artifact and still records the
        window. Only the subprocess is replaced, by a command that genuinely removes
        `resources` and writes the artifact from what is left.

        The git queries are patched the same way — at `_default_git_runner`, so `main`
        still builds its own `Driver` and still computes containment rather than
        reading it. A `CommitGraph` answers, because the revisions this fixture names
        are not commits in any real checkout; the real environment answers from the
        clone the operator runs in.
        """
        config = config if config is not None else wave2_config(tmp_path)
        store = store if store is not None else shared_store_for(config)
        resources = resources if resources is not None else FixtureResources()
        config.setdefault("resource_teardown", ["/fixture/teardown.sh", "--wave", "2"])
        runner = runner or teardown_runner_for(tmp_path, config, resources)
        graph = graph if graph is not None else CommitGraph()
        path = write_config(tmp_path, config)
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": dynamodb if dynamodb is not None else store.dynamodb(),
        }[name]
        evidence = tmp_path / "evidence"

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=client or store.gateway()),
            patch.object(_mod, "_default_teardown_runner", runner),
            patch.object(_mod, "_default_git_runner", graph.runner()),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
        ):
            code = _mod.main(
                ["--wave", "2", "--config", str(path), "--evidence-dir", str(evidence)]
            )

        report = json.loads((evidence / "result.json").read_text(encoding="utf-8"))
        return code, report

    def test_a_complete_fixture_reaches_a_passing_report(self, tmp_path: Path):
        """The positive control, and it has to come first.

        Every negative below is only meaningful if this passes: a command that can
        never exit 0 would satisfy all of them while proving nothing. So this pins
        that the bar is reachable — ten of ten, cleanup verified, exit 0 — before
        anything argues about what fails to reach it.

        This is a fixture, not a deployment. It shows the harness would accept a
        correct live run; it is not itself evidence about one.
        """
        code, report = self._run(tmp_path)

        assert code == _mod.EXIT_OK, report
        assert report["required"] == 10
        assert report["passed"] == 10
        assert report["failed"] == 0
        assert report["not_run"] == 0
        assert report["skipped"] == 0
        assert report["cleanup_ok"] is True
        assert _mod.report_is_passing(report) is True
        assert set(report["checks"]) == {spec.check_id for spec in _mod.WAVE2_CHECKS}

    def test_the_command_tears_resources_down_between_capture_and_verification(
        self, tmp_path: Path
    ):
        """The lifecycle root's second finding said the command did not have.

        Asserted against the real `main`, because that is where the defect lived: it
        captured live state, deleted ROWS, and then read an artifact already claiming
        the pods and queues were gone. Nothing between those steps removed a resource.

        Three things are checked, and each is a different way the ordering could be
        wrong: the resources are really gone afterwards (the teardown ran at all), the
        harness's own record says it invoked the command (the operator did not simply
        assert it), and the absence artifact was rewritten across that invocation (the
        observations postdate the removal rather than predating it).
        """
        resources = FixtureResources()
        config = wave2_config(tmp_path)
        artifact = tmp_path / config["artifacts"]["teardown_verification"]
        before = artifact.read_bytes()

        code, report = self._run(tmp_path, config=config, resources=resources)

        assert code == _mod.EXIT_OK, report
        assert resources.present == {}, "the fixture's resources must actually be gone"
        record = report["resource_teardown"]
        assert record["configured"] is True
        assert record["invoked"] is True
        assert record["ok"] is True
        assert record["exit_code"] == 0
        assert record["started_at"] and record["finished_at"]
        # The evidence a reviewer needs to see that freshness was established rather
        # than assumed: the digest the harness took going in, matching the file that
        # was actually there, and different from what the teardown left behind.
        assert record["verification_present_before"] is True
        assert record["verification_digest_before"] == (
            "sha256:" + hashlib.sha256(before).hexdigest()
        )
        assert artifact.read_bytes() != before

    def test_the_teardown_output_digest_is_recorded_rather_than_the_output(
        self, tmp_path: Path
    ):
        """A teardown script's stdout is a plausible place for a credential.

        The digest ties the recorded run to the operator's own log without copying
        arbitrary command output into an evidence file that gets attached to issues.
        """
        secret = "ghs_examplelookingtokenvalue0000000000"  # noqa: S105 - a fake, to prove it is not copied
        resources = FixtureResources()

        def leaky_runner(argv, timeout):  # noqa: ANN001, ARG001
            resources.remove_all()
            (tmp_path / "teardown_verification.json").write_text(
                json.dumps(
                    teardown_verification_payload(
                        removals=resources.absence_observations(),
                        captured_at=resources.latest_removal(),
                    )
                ),
                encoding="utf-8",
            )
            completed = MagicMock()
            completed.returncode = 0
            completed.stdout = f"authenticating with {secret}\n"
            completed.stderr = ""
            return completed

        _, report = self._run(tmp_path, resources=resources, runner=leaky_runner)

        record = report["resource_teardown"]
        assert record["stdout_digest"].startswith("sha256:")
        assert secret not in json.dumps(report)

    def test_a_failing_teardown_still_cleans_up_the_rows(self, tmp_path: Path):
        """Adding a step in front of cleanup must not create a way to skip it.

        Row deletion is the one thing that must always happen: a fixture row left
        behind is a fixture left in the state DP-INV-1 forbids. The seam was inserted
        ahead of it, so the failure mode to rule out is a teardown error taking the
        deletions down with it.
        """
        resources = FixtureResources()

        def exploding_runner(argv, timeout):  # noqa: ANN001, ARG001
            raise OSError("the teardown host went away")

        code, report = self._run(
            tmp_path, resources=resources, runner=exploding_runner
        )

        assert code != _mod.EXIT_OK
        # The rows were still deleted, and verifiably so.
        assert report["cleanup"]["ok"] is True
        assert len(report["cleanup"]["deletions"]) == 3
        for deletion in report["cleanup"]["deletions"]:
            assert deletion["confirmed_absent"] is True
        # And the failure is reported rather than swallowed.
        assert report["resource_teardown"]["ok"] is False
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED

    def test_the_report_carries_the_first_hand_deletion_record(self, tmp_path: Path):
        """W2-10's evidence has to be readable by the reviewer, not just by W2-10.

        The per-row record goes into the report: which pairs were deleted, that both
        key halves were used, and that a consistent read confirmed absence. Without
        it a reviewer has only the check's verdict, and the whole point of this
        defect is that a verdict about cleanup is not evidence of cleanup.
        """
        _, report = self._run(tmp_path)
        record = report["cleanup"]

        assert record["ok"] is True
        assert record["declared_items"] == 3
        assert len(record["deletions"]) == 3
        for deletion in record["deletions"]:
            assert deletion["both_keys_present"] is True
            assert deletion["deleted"] is True
            assert deletion["confirmed_absent"] is True
            assert deletion["event_id"] and deletion["arrived_at"]

    def test_a_row_still_present_after_delete_cannot_exit_zero(self, tmp_path: Path):
        """The ordering defect this whole issue is about, end to end.

        The consistent read finds the row still there, so cleanup did not succeed.
        Both consequences are asserted: `cleanup_ok` false AND W2-10 failed. A
        W2-10 that ran before the deletions would report `passed` here while
        `cleanup_ok` was false — a report containing its own contradiction, which is
        precisely what "never let a cleanup check pass before cleanup actually
        succeeds" forbids.
        """
        # `get_item` answers with the row still present, on every call.
        stubborn = ddb_stub(item={"event_id": {"S": "msg-live-001"}})

        code, report = self._run(tmp_path, dynamodb=stubborn)

        assert code != _mod.EXIT_OK
        assert report["cleanup_ok"] is False
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert report["checks"]["W2-10"]["status"] != _mod.STATUS_PASSED
        assert _mod.report_is_passing(report) is False

    def test_a_delete_that_raises_cannot_exit_zero(self, tmp_path: Path):
        """A throttled or denied DeleteItem is a fixture left populated."""
        angry = ddb_stub()
        angry.delete_item.side_effect = RuntimeError("AccessDeniedException")

        code, report = self._run(tmp_path, dynamodb=angry)

        assert code != _mod.EXIT_OK
        assert report["cleanup_ok"] is False
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert _mod.report_is_passing(report) is False

    def test_an_unrunnable_teardown_cannot_report_cleanup_success(
        self, tmp_path: Path
    ):
        """Root's reproduction: rows gone, resources still running, `cleanup_ok=true`.

        The exact scenario from the review, and the reason it is a CLI test rather
        than a predicate test. Every individual verdict was already right — the run
        exited 4 and W2-10 failed — so nothing driving predicates directly could see
        the defect. It lived in the two places an operator actually reads: the
        summary line and `result.json` both said `cleanup_ok: true` while all three
        fixture resources were still present, because that field carried only the
        DynamoDB row record.

        That is the worst direction for this particular field to be wrong in. An
        operator scanning a nonzero run to decide whether the environment is safe to
        reuse reads it as "the fixture is gone, the failure was something else", and
        walks away from a fixture with a control listener still enabled — the state
        DP-INV-1 exists to forbid.

        `resources` is asserted directly, not inferred from the report, because the
        whole point is that the report was disagreeing with the world. And the row
        record keeps its `ok: true`: the rows really did go, and losing that
        distinction would trade this defect for a vaguer one.
        """
        resources = FixtureResources()
        code, report = self._run(
            tmp_path,
            resources=resources,
            runner=teardown_runner_for(
                tmp_path,
                wave2_config(tmp_path),
                resources,
                raises=OSError("teardown-control-fixture.sh: permission denied"),
            ),
        )

        # The fixture is genuinely still standing. Every resource the ledger names.
        assert len(resources.present) == 3
        assert resources.removed_at == {}

        # What root reported as correct, and which must stay correct.
        assert code == _mod.EXIT_CHECKS_FAILED
        assert report["resource_teardown"]["ok"] is False
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED

        # What was wrong: the published cleanup verdict.
        assert report["cleanup_ok"] is False
        assert report["fixture_cleanup"]["resources_ok"] is False
        assert report["fixture_cleanup"]["absence_verified"] is False
        # Preserved as the row-specific record, under its own name.
        assert report["cleanup"]["ok"] is True
        assert report["fixture_cleanup"]["rows_ok"] is True
        # And the notes say which part failed, so the summary is actionable rather
        # than merely not-wrong.
        assert any(
            "resource teardown" in note for note in report["fixture_cleanup"]["notes"]
        )
        assert _mod.report_is_passing(report) is False

    def test_retained_resources_cannot_report_cleanup_success(self, tmp_path: Path):
        """The same reporting rule for a teardown that exits 0 and removes nothing.

        Distinct from the case above, and worth its own test: here the command runs
        fine and reports success, so `resource_teardown.ok` is TRUE. The only thing
        that knows the resources are still there is the absence verification. If the
        aggregate had been built from the teardown's exit status alone it would pass
        here, which is why it reads the verification check's outcome too.
        """
        resources = FixtureResources()
        code, report = self._run(
            tmp_path,
            resources=resources,
            runner=teardown_runner_for(
                tmp_path, wave2_config(tmp_path), resources, remove=False
            ),
        )

        assert len(resources.present) == 3
        assert code != _mod.EXIT_OK
        assert report["resource_teardown"]["ok"] is True
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert report["cleanup_ok"] is False
        assert report["fixture_cleanup"]["resources_ok"] is True
        assert report["fixture_cleanup"]["absence_verified"] is False
        assert _mod.report_is_passing(report) is False

    def test_a_clean_run_still_reports_cleanup_success(self, tmp_path: Path):
        """The positive control for the two above, so the fix is not just strictness.

        Without this, making `cleanup_ok` harder to earn would be indistinguishable
        from making it unearnable — the same class of defect as the W2-10 that could
        not pass after a correct cleanup. An honest fixture reports true on all three
        components, and the resources really are gone.
        """
        resources = FixtureResources()
        code, report = self._run(tmp_path, resources=resources)

        assert code == _mod.EXIT_OK
        assert resources.present == {}
        assert report["cleanup_ok"] is True
        assert report["fixture_cleanup"] == {
            "ok": True,
            "rows_ok": True,
            "resources_ok": True,
            "absence_verified": True,
            "notes": [],
        }
        assert _mod.report_is_passing(report) is True

    def test_a_partial_cleanup_failure_still_verifies_the_rest_and_fails(
        self, tmp_path: Path
    ):
        """Partial failure: one row of three refuses to go.

        Two properties at once. Cleanup must not abandon the remaining rows when one
        fails — stopping early would leave rows behind for no reason — and the check
        must still fail. "Mostly cleaned up" is not cleaned up.
        """
        flaky = ddb_stub()
        calls = {"n": 0}

        def delete(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("ProvisionedThroughputExceededException")
            return {}

        flaky.delete_item.side_effect = delete

        code, report = self._run(tmp_path, dynamodb=flaky)

        assert code != _mod.EXIT_OK
        # All three attempted, not just the two before the failure.
        assert calls["n"] == 3
        assert report["cleanup_ok"] is False
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert len(report["cleanup"]["deletions"]) == 3

    @pytest.mark.parametrize("artifact", WAVE2_READ_ARTIFACTS)
    def test_no_single_missing_artifact_can_exit_zero(self, tmp_path: Path, artifact):
        """Parametrized over every artifact a wave-2 check actually reads.

        The list is DERIVED from the harness's own source (see
        `WAVE2_READ_ARTIFACTS`) rather than written out here, so an artifact a future
        wave-2 check starts reading is covered the moment the `_artifact` call
        appears. A hand-maintained list would silently stop being exhaustive, which
        is the same class of defect as the incomplete predicate table this issue
        fixes.

        One removal, one nonzero run, every time. This is the "missing evidence
        remains NOT RUN/nonzero" requirement stated over the whole input space of
        omissions rather than one example of it.
        """
        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
            **wave2_only_artifact_payloads(),
        }
        payloads.pop(artifact)
        config = wave2_config(tmp_path, artifact_payloads=payloads)

        code, report = self._run(tmp_path, config=config)

        assert code != _mod.EXIT_OK
        assert report["passed"] < report["required"]
        assert _mod.report_is_passing(report) is False

    def test_the_two_new_artifacts_are_among_the_ones_wave_two_reads(self):
        """Guards the derivation above against silently covering nothing.

        `WAVE2_READ_ARTIFACTS` is produced by scanning the harness source. If that
        scan broke — a renamed helper, a reformatted call — it would yield an empty
        or short tuple, and the parametrized test above would pass by testing
        nothing. Naming #5825's three artifacts explicitly is the tripwire.

        Three, not two: W2-10's single `cleanup_security_recheck` became
        `security_capture` and `teardown_verification`, one on each side of the
        teardown boundary. That split is the ordering fix, and asserting both names
        here is what stops them being quietly recombined into an artifact that would
        again have to be recorded at one instant.
        """
        assert "wave2_preflight" in WAVE2_READ_ARTIFACTS
        assert "security_capture" in WAVE2_READ_ARTIFACTS
        assert "teardown_verification" in WAVE2_READ_ARTIFACTS
        # The combined artifact must be gone, not merely unused: an implementation
        # still reading it would have kept the ordering defect available.
        assert "cleanup_security_recheck" not in WAVE2_READ_ARTIFACTS
        assert "cleanup_security_recheck" not in _mod.REQUIRED_ARTIFACT_KEYS
        assert len(WAVE2_READ_ARTIFACTS) >= 10
        assert set(WAVE2_READ_ARTIFACTS) <= set(_mod.REQUIRED_ARTIFACT_KEYS)

    def test_wave_one_only_artifacts_are_genuinely_unread_by_wave_two(self, tmp_path: Path):
        """The other side of the partition, asserted rather than assumed.

        A wave-2 run does not read wave 1's artifacts, so removing one cannot fail a
        wave-2 check — and a test that expected it to would be asserting a coupling
        the harness deliberately does not have. Pinning it here means the exclusion
        is a recorded decision rather than a gap somebody later "fixes" by making
        wave 2 depend on wave 1's files.

        Wave 1's evidence still reaches wave 2, but through W2-01's
        `wave1_evidence` recording — a summarized, revision-pinned claim that the
        preflight validates — not by re-reading wave 1's raw artifacts.
        """
        unread = sorted(set(_mod.REQUIRED_ARTIFACT_KEYS) - set(WAVE2_READ_ARTIFACTS))
        assert unread, "expected wave 1 to own artifacts wave 2 does not read"

        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
            **wave2_only_artifact_payloads(),
        }
        for name in unread:
            payloads.pop(name, None)
        config = wave2_config(tmp_path, artifact_payloads=payloads)

        code, report = self._run(tmp_path, config=config)

        assert code == _mod.EXIT_OK, report
        assert _mod.report_is_passing(report) is True

    def test_an_undeclared_teardown_cannot_exit_zero(self, tmp_path: Path):
        """No `cleanup_items` at all.

        The tempting shortcut: the ROW cleanup record reports `ok=True` when nothing
        is declared, which is correct on its own terms (a wave that created no rows
        has nothing to remove) and is exactly why W2-10 cannot rely on it alone.
        W2-01 fails on the undeclared teardown and W2-10 fails on the empty record,
        so the two together close the hole the row record leaves open.

        The row record's `ok=True` is pinned here deliberately, because it is the
        trap, and because the top-level `cleanup_ok` is now the aggregate that does
        NOT repeat it: a run whose verification check failed does not get to report
        the fixture as cleaned up, however well the (empty) row deletion went.
        """
        config = wave2_config(tmp_path, cleanup_items=[])

        code, report = self._run(tmp_path, config=config)

        assert code != _mod.EXIT_OK
        # The row-specific record is TRUE here, which is the trap...
        assert report["cleanup"]["ok"] is True
        assert report["fixture_cleanup"]["rows_ok"] is True
        # ...and the published verdict does not inherit it.
        assert report["cleanup_ok"] is False
        assert report["fixture_cleanup"]["absence_verified"] is False
        assert report["checks"]["W2-01"]["status"] == _mod.STATUS_FAILED
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert _mod.report_is_passing(report) is False

    def test_a_forged_cleanup_claim_in_the_operator_artifact_changes_nothing(
        self, tmp_path: Path
    ):
        """The substitution the issue forbids, attempted directly.

        An operator (or a well-meaning script) adds every plausible
        "cleanup succeeded" field to the artifacts they control, while the actual
        DynamoDB deletes fail. If any of those fields were load-bearing this would
        exit 0. None are: the deletion half of W2-10 reads only the harness's own
        `CleanupOutcome`, so no artifact can vote on it.

        Both W2-10 artifacts are forged, because the split created a second file the
        operator writes and a claim moved into either one would be just as wrong.
        """
        forged_claims = {
            "cleanup_ok": True,
            "cleanup_succeeded": True,
            "rows_deleted": 3,
            "all_rows_removed": True,
            "teardown_verified": True,
            "consistent_read_confirmed_absent": True,
        }
        payloads = {
            **artifact_payloads(),
            **wave2_artifact_payloads(),
            **pause_artifact_payloads(),
            **wave2_only_artifact_payloads(),
            "security_capture": security_capture_payload(**forged_claims),
            "teardown_verification": teardown_verification_payload(**forged_claims),
        }
        config = wave2_config(tmp_path, artifact_payloads=payloads)
        angry = ddb_stub()
        angry.delete_item.side_effect = RuntimeError("AccessDeniedException")

        code, report = self._run(tmp_path, config=config, dynamodb=angry)

        assert code != _mod.EXIT_OK
        assert report["checks"]["W2-10"]["status"] == _mod.STATUS_FAILED
        assert _mod.report_is_passing(report) is False
        # And it failed on the real deletion record, not on the forged fields.
        assert "AccessDeniedException" in report["checks"]["W2-10"]["message"]

    def test_cleanup_and_its_verification_both_run_when_the_checks_raise(
        self, tmp_path: Path
    ):
        """The exception path. Cleanup is in `finally` for a reason.

        The failure path is exactly when a fixture is most likely to be left with a
        live control listener — the state DP-INV-1 forbids. So an exception in the
        pre-cleanup checks must not skip either the teardown or its verification,
        and the evidence must still be written for the operator to read.

        `assert_check_manifest` is what turns an abandoned run into `EXIT_PRECONDITION`
        rather than a short report, so the exit code is asserted as nonzero rather
        than as a specific value.
        """
        config = wave2_config(tmp_path)
        recorder = ddb_stub()
        boom = wave2_gateway_stub()
        boom.request.side_effect = KeyboardInterrupt("operator interrupted")

        path = write_config(tmp_path, config)
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": recorder,
        }[name]

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=boom),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
            pytest.raises(KeyboardInterrupt),
        ):
            _mod.main(
                [
                    "--wave",
                    "2",
                    "--config",
                    str(path),
                    "--evidence-dir",
                    str(tmp_path / "evidence"),
                ]
            )

        # Teardown still happened, for every declared row, by exact key.
        assert recorder.delete_item.call_count == 3
        for call in recorder.delete_item.call_args_list:
            assert set(call.kwargs["Key"]) == {"event_id", "arrived_at"}

    def test_teardown_survives_an_exception_in_the_capture_itself(self, tmp_path: Path):
        """The failure mode the capture-then-verify reordering introduced.

        Moving the security observations before `run_cleanup` put a new step between
        the checks and teardown, and anything on that path can now be the reason the
        fixture is never removed. `capture_security_observations` records ordinary
        failures rather than raising, but a `BaseException` — an operator's Ctrl-C
        during a slow probe — bypasses that entirely.

        Teardown is the one step that has to survive every failure mode, including a
        failure of the step added in front of it, because a fixture left standing is a
        control-enabled workload outliving its evaluation. So the capture has its own
        `finally`.

        Distinct from the test above, which raises in the CHECKS: that path was always
        covered. This one raises during the capture, which only exists after this fix.
        """
        config = wave2_config(tmp_path)
        recorder = ddb_stub()
        path = write_config(tmp_path, config)
        session = MagicMock()
        session.client.side_effect = lambda name, **_: {
            "sts": sts_for(ACCOUNT),
            "dynamodb": recorder,
        }[name]

        with (
            patch("boto3.session.Session", return_value=session),
            patch("httpx.Client", return_value=wave2_gateway_stub()),
            patch.dict("os.environ", IDENTITY_ENV, clear=False),
            patch.object(
                _mod,
                "capture_security_observations",
                side_effect=KeyboardInterrupt("interrupted mid-capture"),
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _mod.main(
                [
                    "--wave",
                    "2",
                    "--config",
                    str(path),
                    "--evidence-dir",
                    str(tmp_path / "evidence"),
                ]
            )

        assert recorder.delete_item.call_count == 3
        for call in recorder.delete_item.call_args_list:
            assert set(call.kwargs["Key"]) == {"event_id", "arrived_at"}

    def test_no_wave_two_check_id_is_missing_from_the_report(self, tmp_path: Path):
        """The completeness of the report itself, through `main`.

        Asserted separately from the count because `passed == required` is arithmetic
        over whatever the report happens to contain: ten passes over nine checks and
        a duplicate would satisfy it. The IDs are the wave, and `assert_check_manifest`
        plus W2-01's and W2-10's inventory assertions are what tie the two together.
        """
        _, report = self._run(tmp_path)

        assert sorted(report["checks"]) == sorted(
            spec.check_id for spec in _mod.WAVE2_CHECKS
        )
        assert len(report["checks"]) == report["required"] == 10

    def test_the_post_cleanup_check_is_the_only_one_deferred(self):
        """The split is deliberately minimal, and that is worth pinning.

        Deferring a check past cleanup is a real cost: it runs against a fixture
        that has been torn down, so anything needing live fixture state cannot go
        here. W2-10 is the only check whose subject IS the teardown. If this set
        grew silently, checks would start running against a dismantled environment
        and failing for reasons unrelated to what they test.
        """
        assert _mod.POST_CLEANUP_CHECK_IDS == frozenset({"W2-10"})
        before, after = _mod.split_post_cleanup_specs(_mod.WAVE2_CHECKS)
        assert tuple(s.check_id for s in after) == ("W2-10",)
        assert len(before) == 9
        # The partition is exactly the wave: nothing dropped, nothing duplicated.
        assert {s.check_id for s in before} | {s.check_id for s in after} == {
            spec.check_id for spec in _mod.WAVE2_CHECKS
        }
        # And wave 1 is untouched: it has no post-cleanup check, so its behaviour
        # is unchanged by the split.
        w1_before, w1_after = _mod.split_post_cleanup_specs(_mod.WAVE1_CHECKS)
        assert w1_after == ()
        assert w1_before == _mod.WAVE1_CHECKS

class TestTheDispatchInputIsValidatedAsAWholeValue:
    """The `resolve-source` step script, EXECUTED — not described.

    Root's first finding on `ae57ee24` was found by running the block: the check was
    `printf '%s' "$SOURCE_SHA" | grep -Eq '^[0-9a-f]{40}$'`, and `grep` matches a
    LINE. Root passed a 40-hex SHA followed by a newline, `ref=main`, and another
    `expected=` line; the step exited 0 and appended those lines to `$GITHUB_OUTPUT`,
    where the last value of a key wins — so the jobs would have checked out `main`
    while the run reported a different expected SHA.

    A substring assertion on the workflow text cannot detect that, which is why these
    tests extract the real script out of the YAML and run it under bash against a
    temporary `$GITHUB_OUTPUT`. What is asserted is the exit status and the outputs
    actually written, since those are what GitHub acts on.
    """

    @staticmethod
    def _resolve_script() -> str:
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "agent-control-ci.yml").read_text(
                encoding="utf-8"
            )
        )
        (step,) = workflow["jobs"]["resolve-source"]["steps"]
        return step["run"]

    def _run(self, tmp_path: Path, source_sha: str, event: str = "workflow_dispatch"):
        """Execute the step and return (exit status, parsed $GITHUB_OUTPUT)."""
        import subprocess

        script = tmp_path / "resolve.sh"
        script.write_text(self._resolve_script(), encoding="utf-8")
        output = tmp_path / "github_output"
        output.write_text("", encoding="utf-8")
        summary = tmp_path / "github_step_summary"
        summary.write_text("", encoding="utf-8")
        completed = subprocess.run(  # noqa: S603
            ["bash", str(script)],  # noqa: S607
            env={
                "PATH": "/usr/bin:/bin",
                "SOURCE_SHA": source_sha,
                "EVENT_NAME": event,
                "GITHUB_OUTPUT": str(output),
                "GITHUB_STEP_SUMMARY": str(summary),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # Parsed the way GitHub does: later assignments to a key override earlier ones,
        # which is precisely what made the injection dangerous rather than merely untidy.
        written: dict[str, str] = {}
        for line in output.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                written[key] = value
        return completed.returncode, written

    def test_a_full_lowercase_sha_resolves_to_itself(self, tmp_path: Path):
        """The positive control: without it, rejecting everything would pass the rest."""
        sha = "405d1e6eb531105239432b2719844e1e51e60a93"

        status, written = self._run(tmp_path, sha)

        assert status == 0
        assert written == {"ref": sha, "expected": sha}

    def test_root_s_injection_is_rejected(self, tmp_path: Path):
        """The exact input root executed: a valid SHA, a newline, then chosen outputs.

        The consequence if accepted is not a confusing log line — `ref=main` overrides
        the SHA, so the jobs check out a moving branch while `expected` names a
        different commit, and the gate reports a revision nobody tested.
        """
        injected = (
            "a" * 40 + "\nref=main\nexpected=" + "b" * 40
        )

        status, written = self._run(tmp_path, injected)

        assert status == 1
        assert written == {}

    @pytest.mark.parametrize(
        ("label", "value"),
        [
            ("empty", ""),
            ("a branch name", "main"),
            ("an abbreviated sha", "405d1e6"),
            ("uppercase hex", "405D1E6EB531105239432B2719844E1E51E60A93"),
            ("39 characters", "a" * 39),
            ("41 characters", "a" * 41),
            ("non-hex characters", "z" * 40),
            ("a trailing newline", "a" * 40 + "\n"),
            ("a trailing carriage return", "a" * 40 + "\r"),
            ("a leading newline", "\n" + "a" * 40),
            ("surrounding whitespace", "  " + "a" * 40 + "  "),
            ("a second sha on a second line", "a" * 40 + "\n" + "b" * 40),
            ("a shell metacharacter suffix", "a" * 40 + "; rm -rf /"),
            ("a command substitution", "$(git rev-parse HEAD)"),
        ],
    )
    def test_anything_other_than_exactly_forty_lowercase_hex_is_rejected(
        self, tmp_path: Path, label, value
    ):
        """Rejected with a nonzero exit and NO outputs written.

        The two halves matter separately: a nonzero exit stops the dependent jobs, and
        writing no outputs means nothing downstream can consume a partially-validated
        ref even if the failure were somehow ignored.
        """
        status, written = self._run(tmp_path, value)

        assert status == 1, label
        assert written == {}, label

    def test_a_pull_request_run_ignores_the_input_and_checks_out_the_trigger(
        self, tmp_path: Path
    ):
        """The PR path is unchanged: empty outputs are `actions/checkout`'s default.

        Executed with a hostile `source_sha` set, because `inputs.source_sha` is not
        addressable on a `pull_request` event but the step must not depend on that.
        """
        status, written = self._run(
            tmp_path, "main\nref=evil", event="pull_request"
        )

        assert status == 0
        assert written == {"ref": "", "expected": ""}

    def test_the_job_running_these_tests_installs_the_yaml_reader_they_need(self):
        """The dependency this class needs must be declared where this class runs.

        Every test above reaches the step script through `yaml.safe_load`, so without
        that dependency they do not fail on the validator's behaviour — they fail on
        import, all seventeen of them, which is how run 35956224007 went red. The
        coupling is invisible otherwise: the tests pass on any developer machine that
        happens to have PyYAML installed, and fail only in the job that matters.

        Asserted against the workflow's own install step rather than by importing the
        module, because the defect was never that the reader is unavailable in general.
        It was that it is absent from the one environment where these assertions are a
        required check.
        """
        import yaml

        workflow = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "agent-control-ci.yml").read_text(
                encoding="utf-8"
            )
        )
        (install,) = [
            step
            for step in workflow["jobs"]["control-evaluation-harness-tests"]["steps"]
            if step.get("name") == "Install harness test dependencies"
        ]

        assert "pyyaml" in install["run"].lower()


# ---------------------------------------------------------------------------
# Wave 3 — the steering checks S6 #3965 owns: W3-06..W3-09 and W3-11.
# ---------------------------------------------------------------------------

STEER_IDS: tuple[str, ...] = tuple(
    f"3f2b9c14-7d51-4e8a-9b02-5c6d7e8f{index:04x}" for index in range(1, 11)
)


def steering_artifact_payloads() -> dict:
    """A complete, passing artifact set for the five steering checks.

    Kept in its own helper for the reason `pause_artifact_payloads` is: a wave-3
    field must not be able to perturb wave 2's fixtures, and every negative test
    below bends exactly one key of this baseline so a failure names one defect.

    The `steering_delivery` instants are deliberately more than three minutes
    apart at `accepted_at` → `handoff_at` and five seconds apart at `handoff_at` →
    `marker_at`. That is a PASS, and it is the shape that distinguishes the
    implemented bound from the one it would be easy to write instead: measured from
    submission this fixture is 225 seconds late. A baseline with the three instants
    seconds apart would satisfy both readings and could not tell them apart.
    """
    return {
        "steering_delivery": {
            "command_id": STEER_IDS[0],
            "accepted_at": "2026-09-24T14:10:02Z",
            "handoff_at": "2026-09-24T14:13:47Z",
            "marker_at": "2026-09-24T14:13:52Z",
            "state_command_ids": [STEER_IDS[0]],
            "log_command_ids": [STEER_IDS[0]],
            "tool_active_at_submission": True,
            "status_at_submission": "pending",
            "delivered_at_matches_handoff": True,
            "model_comprehension_claimed": False,
        },
        "steering_queue": {
            "submission_order": list(STEER_IDS),
            "handoff_order": list(STEER_IDS),
            "accepted_count": _mod.STEER_QUEUE_CAP,
            "overflow_status": 429,
            "paused_pending_ids": [STEER_IDS[0], STEER_IDS[1]],
            "paused_delivered_after_resume": [STEER_IDS[0], STEER_IDS[1]],
            "abort_cancelled_ids": ["9c1e4a77-0b52-4d13-8f6a-2e7b5c8d1a03"],
            "expiry_outcome": "unknown",
            "replayed_after_unknown": False,
            "authority_revalidated_at_handoff": True,
        },
        "steering_trust_boundary": {
            "delimiters_present": True,
            "instruction_inside_delimiters": True,
            "actor_attribution": "operator octocat via ADP control (trusted caller)",
            "origin_kind": "human",
            "should_query": True,
            "attacker_actor_metadata_rejected": True,
            "raw_instruction_in_system_text": False,
        },
        "steering_input_stream": {
            "initial_task_consumed": True,
            "later_user_messages": 2,
            "generator_disposed": True,
            "query_closed": True,
            "message_count": 3,
            "turn_count": 4,
            "observed_by": (
                "control-runtime.integration.ts experiment 9 against "
                f"@anthropic-ai/claude-agent-sdk {_mod.EXPECTED_CLAUDE_SDK_VERSION}"
            ),
        },
        "steering_retry": {
            "queued_command_id": "9c1e4a77-0b52-4d13-8f6a-2e7b5c8d1a03",
            "deliveries_of_queued_command": 1,
            "confirmed_handoffs_replayed": 0,
            "session_preserved": True,
            "attempt_id_before": "attempt-1",
            "attempt_id_after": "attempt-2",
            "ambiguous_handoff_outcome": "unknown",
            "abort_during_retry_started_next_attempt": False,
        },
    }


WAVE3_IMPLEMENTED: tuple[str, ...] = ("W3-06", "W3-07", "W3-08", "W3-09", "W3-11")


def run_steering(tmp_path: Path, artifact: str | None = None, patch_: dict | None = None) -> dict:
    """Drive the five implemented wave-3 checks, optionally bending one field.

    Only those five specs are run, rather than the whole wave-3 manifest. The other
    seven have no predicate by design, so including them would add seven `not_run`
    results to every assertion here and say nothing about steering — the manifest's
    completeness is asserted in `TestCheckIdsMatchTheEvaluationFile`, which is where
    that claim belongs.

    `None` as a patch value deletes the key, which is how the "a missing field must
    not read as a pass" cases are written.
    """
    payloads = steering_artifact_payloads()
    if artifact is not None:
        for key, value in (patch_ or {}).items():
            if value is None:
                payloads[artifact].pop(key, None)
            else:
                payloads[artifact][key] = value
    config = live_config(tmp_path, artifact_payloads={**artifact_payloads(), **payloads})
    artifacts = _mod.ArtifactStore(tmp_path, config["artifacts"])
    driver = _mod.Driver(config, _mod.Probe(config["gateway_url"], gateway_stub()), artifacts)
    specs = tuple(s for s in _mod.WAVE3_CHECKS if s.check_id in WAVE3_IMPLEMENTED)
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        results = _mod.run_checks(driver, specs, manifest_ids=WAVE3_IMPLEMENTED)
    return {result.check_id: result for result in results}


class TestWave3SteeringChecks:
    """The five wave-3 checks S6 (#3965) owns.

    Same posture as `TestWave2PauseChecks`: the harness never runs in CI, so what
    CI proves is that each check would NOTICE a deployment that steers badly. Every
    test starts from the passing fixture and bends exactly one thing.

    The bar these enforce is the one the story states first — `delivered` must mean
    the SDK accepted the input, and must not be claimed before it did — so most are
    written as "this defect must FAIL the check" rather than as happy paths.
    """

    def test_a_correct_deployment_passes_all_five(self, tmp_path: Path):
        """The positive control. Every negative below is vacuous without it."""
        results = run_steering(tmp_path)

        for check_id in WAVE3_IMPLEMENTED:
            assert results[check_id].status == _mod.STATUS_PASSED, (
                check_id,
                results[check_id].message,
            )

    @pytest.mark.parametrize(
        ("artifact", "check_id", "key"),
        [
            ("steering_delivery", "W3-06", "status_at_submission"),
            ("steering_delivery", "W3-06", "handoff_at"),
            ("steering_queue", "W3-07", "overflow_status"),
            ("steering_queue", "W3-07", "expiry_outcome"),
            ("steering_trust_boundary", "W3-08", "instruction_inside_delimiters"),
            ("steering_input_stream", "W3-09", "later_user_messages"),
            ("steering_retry", "W3-11", "ambiguous_handoff_outcome"),
        ],
    )
    def test_omitting_an_awkward_key_fails_rather_than_skips(
        self, tmp_path: Path, artifact: str, check_id: str, key: str
    ):
        """A half-filled artifact is a claim without its evidence.

        Stated separately from the per-field tests rather than folded into them as a
        `None` case, because a DIFFERENT mechanism answers: the artifact store's
        required-keys guard fires before the predicate reads anything, so the
        message names the missing key and not the property. Folding it in would have
        asserted the predicate's wording against a message the predicate never
        produced.

        The keys chosen are the awkward ones — the status that must be `pending`,
        the handoff instant, the 429, the expiry outcome, the containment
        observation, the message count, the ambiguous outcome. Those are exactly the
        fields an operator under pressure would be tempted to leave out, and
        treating an omission as `not_run` would make leaving them out the easy path.
        """
        results = run_steering(tmp_path, artifact, {key: None})

        assert results[check_id].status == _mod.STATUS_FAILED
        assert key in results[check_id].message
        assert "claim without its evidence" in results[check_id].message

    @pytest.mark.parametrize("artifact", sorted(steering_artifact_payloads()))
    def test_an_absent_artifact_is_not_run_rather_than_passed(
        self, tmp_path: Path, artifact: str
    ):
        """"Could not look" is never a pass, for each of the five separately.

        Parametrized per artifact because the five checks are independent readers:
        one of them silently defaulting would be invisible in an aggregate.
        """
        config = live_config(
            tmp_path,
            artifact_payloads={**artifact_payloads(), **steering_artifact_payloads()},
        )
        config["artifacts"].pop(artifact)
        artifacts = _mod.ArtifactStore(tmp_path, config["artifacts"])
        driver = _mod.Driver(
            config, _mod.Probe(config["gateway_url"], gateway_stub()), artifacts
        )
        specs = tuple(s for s in _mod.WAVE3_CHECKS if s.check_id in WAVE3_IMPLEMENTED)
        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            results = {r.check_id: r for r in _mod.run_checks(driver, specs)}

        owner = {
            "steering_delivery": "W3-06",
            "steering_queue": "W3-07",
            "steering_trust_boundary": "W3-08",
            "steering_input_stream": "W3-09",
            "steering_retry": "W3-11",
        }[artifact]
        assert results[owner].status == _mod.STATUS_NOT_RUN, results[owner].message
        assert artifact in results[owner].message

    # ---- W3-06: honest acknowledgement (AC-T2, AC-T4) -------------------

    def test_a_marker_late_after_handoff_fails(self, tmp_path: Path):
        """The bound itself, over by one second."""
        results = run_steering(
            tmp_path, "steering_delivery", {"marker_at": "2026-09-24T14:14:23Z"}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "after the SDK handoff" in results["W3-06"].message

    def test_the_bound_is_measured_from_handoff_not_submission(self, tmp_path: Path):
        """The substitution that would fail a correct run, asserted as a PASS.

        This is the load-bearing test of W3-06 and the only one that distinguishes
        the implemented rule from the plausible wrong one. The baseline's marker is
        225 seconds after `accepted_at` and 5 seconds after `handoff_at`; a bound
        measured from submission would fail it. Widening the gap further must still
        pass, because a steer waiting out a long tool call is correct behaviour.
        """
        results = run_steering(
            tmp_path,
            "steering_delivery",
            {"handoff_at": "2026-09-24T15:47:00Z", "marker_at": "2026-09-24T15:47:04Z"},
        )

        assert results["W3-06"].status == _mod.STATUS_PASSED, results["W3-06"].message

    def test_a_marker_predating_the_handoff_fails(self, tmp_path: Path):
        """Acknowledging before delivering, which is not read as clock skew."""
        results = run_steering(
            tmp_path, "steering_delivery", {"marker_at": "2026-09-24T14:13:40Z"}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "predates the SDK handoff" in results["W3-06"].message

    def test_a_handoff_before_acceptance_fails(self, tmp_path: Path):
        """A command delivered before it was received: one instant is mislabelled."""
        results = run_steering(
            tmp_path, "steering_delivery", {"handoff_at": "2026-09-24T14:09:00Z"}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "precedes acceptance" in results["W3-06"].message

    @pytest.mark.parametrize("field", ["accepted_at", "handoff_at", "marker_at"])
    @pytest.mark.parametrize("value", [None, "", "   ", "not-a-time", 17])
    def test_an_unusable_instant_fails(self, tmp_path: Path, field: str, value):
        """A missing instant turns the bound into a different bound."""
        results = run_steering(tmp_path, "steering_delivery", {field: value})

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert field in results["W3-06"].message

    def test_a_steer_submitted_with_no_tool_running_fails(self, tmp_path: Path):
        """AC-T2's subject is the mid-tool case; the idle one proves nothing."""
        results = run_steering(
            tmp_path, "steering_delivery", {"tool_active_at_submission": False}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "mid-tool" in results["W3-06"].message

    @pytest.mark.parametrize("status", ["delivered", "applied", ""])
    def test_a_status_claiming_delivery_mid_tool_fails(self, tmp_path: Path, status):
        """Mid-tool there is no parked reader, so no handoff can have happened."""
        results = run_steering(
            tmp_path, "steering_delivery", {"status_at_submission": status}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "pending" in results["W3-06"].message

    def test_a_delivered_at_taken_at_enqueue_fails(self, tmp_path: Path):
        """The dashboard renders that field as the delivery time."""
        results = run_steering(
            tmp_path, "steering_delivery", {"delivered_at_matches_handoff": False}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "delivered_at" in results["W3-06"].message

    @pytest.mark.parametrize("field", ["state_command_ids", "log_command_ids"])
    def test_a_record_naming_a_different_command_fails(self, tmp_path: Path, field: str):
        """An operator correlating an acknowledgement needs something to match on."""
        results = run_steering(tmp_path, "steering_delivery", {field: [STEER_IDS[9]]})

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert field in results["W3-06"].message

    @pytest.mark.parametrize("field", ["state_command_ids", "log_command_ids"])
    @pytest.mark.parametrize("value", [None, [], "", 17, [None], [""]])
    def test_an_unusable_id_sequence_fails(self, tmp_path: Path, field: str, value):
        results = run_steering(tmp_path, "steering_delivery", {field: value})

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert field in results["W3-06"].message

    def test_claiming_the_model_understood_fails(self, tmp_path: Path):
        """The distinction the whole story is built on.

        `delivered` is a statement about the SDK accepting bytes. An artifact
        claiming comprehension makes a declined instruction indistinguishable from
        a delivered one, which is the honesty property AC-T4 protects.
        """
        results = run_steering(
            tmp_path, "steering_delivery", {"model_comprehension_claimed": True}
        )

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "comprehend" in results["W3-06"].message

    @pytest.mark.parametrize("value", [None, "", "   ", 17])
    def test_a_missing_command_id_fails(self, tmp_path: Path, value):
        results = run_steering(tmp_path, "steering_delivery", {"command_id": value})

        assert results["W3-06"].status == _mod.STATUS_FAILED
        assert "command_id" in results["W3-06"].message

    # ---- W3-07: the bound, the order, the terminal outcomes (AC-T5, AC-T8) ----

    @pytest.mark.parametrize("count", [9, 11])
    def test_the_wrong_accepted_count_fails(self, tmp_path: Path, count: int):
        """A cap that admits one more than it declares is not a cap."""
        results = run_steering(tmp_path, "steering_queue", {"accepted_count": count})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "accepted_count" in results["W3-07"].message

    @pytest.mark.parametrize("status", [202, 200, 500])
    def test_an_overflow_that_is_not_429_fails(self, tmp_path: Path, status):
        """Backpressure the caller cannot see is a silently dropped instruction."""
        results = run_steering(tmp_path, "steering_queue", {"overflow_status": status})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "429" in results["W3-07"].message

    def test_a_short_submission_sequence_fails(self, tmp_path: Path):
        """The overflow case is only exercised once the queue is actually full."""
        results = run_steering(
            tmp_path,
            "steering_queue",
            {"submission_order": list(STEER_IDS[:3]), "handoff_order": list(STEER_IDS[:3])},
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "cap" in results["W3-07"].message

    def test_an_inverted_pair_fails_and_names_the_position(self, tmp_path: Path):
        """A FIFO that inverts one pair satisfies every set-level comparison.

        The message has to name the position, which is the reason these are
        recorded as sequences rather than as a `fifo_ok` boolean.
        """
        swapped = list(STEER_IDS)
        swapped[3], swapped[4] = swapped[4], swapped[3]

        results = run_steering(tmp_path, "steering_queue", {"handoff_order": swapped})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "not FIFO" in results["W3-07"].message
        assert "position 3" in results["W3-07"].message

    def test_an_accepted_command_that_never_reached_the_sdk_fails(self, tmp_path: Path):
        """A set difference, distinguished from an ordering defect in the message."""
        dropped = list(STEER_IDS[:9]) + ["7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918"]

        results = run_steering(tmp_path, "steering_queue", {"handoff_order": dropped})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "never-delivered" in results["W3-07"].message

    def test_a_repeated_handoff_id_fails(self, tmp_path: Path):
        """A command ID twice in a handoff order is a replay.

        Deduplicating it before the order comparison would convert this finding
        into a pass, which is why the sequence reader refuses duplicates itself.
        """
        replayed = list(STEER_IDS[:9]) + [STEER_IDS[0]]

        results = run_steering(tmp_path, "steering_queue", {"handoff_order": replayed})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "repeats" in results["W3-07"].message

    def test_a_pause_that_drops_a_pending_command_fails(self, tmp_path: Path):
        """A pause is not a discard."""
        results = run_steering(
            tmp_path,
            "steering_queue",
            {"paused_delivered_after_resume": [STEER_IDS[0]]},
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "after resume" in results["W3-07"].message

    def test_a_pause_that_reorders_on_release_fails(self, tmp_path: Path):
        """Same set, wrong order — 'now do X' after 'stop doing Y' is not reversible."""
        results = run_steering(
            tmp_path,
            "steering_queue",
            {"paused_delivered_after_resume": [STEER_IDS[1], STEER_IDS[0]]},
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "after resume" in results["W3-07"].message

    def test_an_abort_that_flushes_its_queue_fails(self, tmp_path: Path):
        """Delivering on the way out runs what the operator aborted to prevent."""
        results = run_steering(
            tmp_path, "steering_queue", {"abort_cancelled_ids": [STEER_IDS[2]]}
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "cancelled by the abort" in results["W3-07"].message

    @pytest.mark.parametrize("outcome", ["delivered", "pending", "applied"])
    def test_an_expiry_resolved_to_anything_but_unknown_fails(self, tmp_path: Path, outcome):
        """Both alternatives are dishonest in opposite directions."""
        results = run_steering(tmp_path, "steering_queue", {"expiry_outcome": outcome})

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "unknown" in results["W3-07"].message

    def test_a_replay_after_unknown_fails(self, tmp_path: Path):
        """`unknown` exists so an ambiguous handoff is reported, not retried."""
        results = run_steering(
            tmp_path, "steering_queue", {"replayed_after_unknown": True}
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "replayed" in results["W3-07"].message

    def test_authorization_checked_only_at_submission_fails(self, tmp_path: Path):
        """The #5029 bypass a delayed buffer would open."""
        results = run_steering(
            tmp_path, "steering_queue", {"authority_revalidated_at_handoff": False}
        )

        assert results["W3-07"].status == _mod.STATUS_FAILED
        assert "revalidated" in results["W3-07"].message

    # ---- W3-08: the trust boundary (AC-S8) ------------------------------

    def test_unwrapped_steering_text_fails(self, tmp_path: Path):
        results = run_steering(
            tmp_path, "steering_trust_boundary", {"delimiters_present": False}
        )

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "delimiters" in results["W3-08"].message

    def test_delimiters_that_do_not_contain_the_instruction_fail(self, tmp_path: Path):
        """The separate observation, and the one that carries AC-S8.

        A wrapper appended AFTER the raw text satisfies `delimiters_present` while
        containing nothing, so this must fail with the delimiters still reported
        present.
        """
        results = run_steering(
            tmp_path, "steering_trust_boundary", {"instruction_inside_delimiters": False}
        )

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "inside" in results["W3-08"].message

    @pytest.mark.parametrize("value", [None, "", "   ", 17])
    def test_a_missing_actor_attribution_fails(self, tmp_path: Path, value):
        results = run_steering(tmp_path, "steering_trust_boundary", {"actor_attribution": value})

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "actor_attribution" in results["W3-08"].message

    @pytest.mark.parametrize("origin", ["system", "agent", "", None])
    def test_a_non_human_origin_fails(self, tmp_path: Path, origin):
        results = run_steering(tmp_path, "steering_trust_boundary", {"origin_kind": origin})

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "origin_kind" in results["W3-08"].message

    def test_steering_queued_as_a_non_querying_note_fails(self, tmp_path: Path):
        """Note semantics, not steering: filed away until something else provokes a turn."""
        results = run_steering(tmp_path, "steering_trust_boundary", {"should_query": False})

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "should_query" in results["W3-08"].message

    def test_attacker_settable_attribution_fails(self, tmp_path: Path):
        """If text inside the envelope can set it, the envelope's authority is theirs."""
        results = run_steering(
            tmp_path,
            "steering_trust_boundary",
            {"attacker_actor_metadata_rejected": False},
        )

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "attacker" in results["W3-08"].message

    def test_the_raw_instruction_in_system_text_fails(self, tmp_path: Path):
        """Delimiters elsewhere do not matter if the text is also present unwrapped."""
        results = run_steering(
            tmp_path, "steering_trust_boundary", {"raw_instruction_in_system_text": True}
        )

        assert results["W3-08"].status == _mod.STATUS_FAILED
        assert "system text" in results["W3-08"].message

    # ---- W3-09: the real input stream (AC-T6) ---------------------------

    def test_a_stream_that_never_carried_the_task_fails(self, tmp_path: Path):
        """Steering shares the channel the prompt arrives on."""
        results = run_steering(
            tmp_path, "steering_input_stream", {"initial_task_consumed": False}
        )

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "initial task" in results["W3-09"].message

    @pytest.mark.parametrize("count", [0, 1, None, "2"])
    def test_fewer_than_two_later_messages_fails(self, tmp_path: Path, count):
        """One is ambiguous: a replayed prompt on a fresh session looks identical."""
        results = run_steering(
            tmp_path, "steering_input_stream", {"later_user_messages": count}
        )

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "later_user_messages" in results["W3-09"].message

    def test_counts_that_contradict_each_other_fail(self, tmp_path: Path):
        """Two messages after the task cannot fit in a two-message stream."""
        results = run_steering(tmp_path, "steering_input_stream", {"message_count": 2})

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "message_count" in results["W3-09"].message

    @pytest.mark.parametrize("turns", [0, -1, None])
    def test_a_stream_that_provoked_no_turn_fails(self, tmp_path: Path, turns):
        """Indistinguishable from a channel that accepted the messages and dropped them."""
        results = run_steering(tmp_path, "steering_input_stream", {"turn_count": turns})

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "turn_count" in results["W3-09"].message

    @pytest.mark.parametrize("field", ["generator_disposed", "query_closed"])
    def test_an_undisposed_attempt_fails(self, tmp_path: Path, field: str):
        """Each holds an SDK subprocess; the leak only shows up in aggregate."""
        results = run_steering(tmp_path, "steering_input_stream", {field: False})

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert results["W3-09"].message

    @pytest.mark.parametrize(
        "source",
        [
            "grep of agent-worker.ts",
            "a mock input channel in the jest suite",
            "stub adapter",
            "fake SDK transport",
            "source read of control-runtime.ts",
            "code read",
            "static inspection",
        ],
    )
    def test_a_non_observation_named_as_the_source_fails(self, tmp_path: Path, source: str):
        """The claim mocks cannot make, refused by name.

        A mock accepts as many messages as it is handed by construction, and a grep
        establishes that the code intends to push them. Neither is evidence the
        provider accepted them, and #3969 says so explicitly.
        """
        results = run_steering(tmp_path, "steering_input_stream", {"observed_by": source})

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "observed_by" in results["W3-09"].message

    @pytest.mark.parametrize("value", [None, "", "   ", 17])
    def test_an_unattributed_stream_observation_fails(self, tmp_path: Path, value):
        results = run_steering(tmp_path, "steering_input_stream", {"observed_by": value})

        assert results["W3-09"].status == _mod.STATUS_FAILED
        assert "observed_by" in results["W3-09"].message

    # ---- W3-11: the retry (AC-T7) --------------------------------------

    def test_a_stranded_pending_command_fails(self, tmp_path: Path):
        """Zero deliveries: the operator waits on an instruction that cannot arrive."""
        results = run_steering(
            tmp_path, "steering_retry", {"deliveries_of_queued_command": 0}
        )

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "never delivered" in results["W3-11"].message

    def test_a_duplicated_delivery_fails_differently(self, tmp_path: Path):
        """The opposite failure, and it must not share a message with the stranded one.

        This is why the field is an integer: 0 and 2 have opposite causes and
        opposite fixes, and "not 1" would send the reader looking for the wrong one.
        """
        results = run_steering(
            tmp_path, "steering_retry", {"deliveries_of_queued_command": 2}
        )
        message = results["W3-11"].message

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "twice" in message
        assert "never delivered" not in message

    def test_a_replayed_confirmed_handoff_fails(self, tmp_path: Path):
        """Only PENDING commands cross a retry."""
        results = run_steering(
            tmp_path, "steering_retry", {"confirmed_handoffs_replayed": 1}
        )

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "confirmed_handoffs_replayed" in results["W3-11"].message

    def test_a_lost_session_fails(self, tmp_path: Path):
        """A correctly reattached command delivered to a run that forgot the context."""
        results = run_steering(tmp_path, "steering_retry", {"session_preserved": False})

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "session" in results["W3-11"].message

    def test_an_unchanged_attempt_fails(self, tmp_path: Path):
        """Then no attempt was replaced and the reattachment path never ran.

        This is the test that keeps the check from passing on a run where the retry
        did not happen — every other field would be satisfied by a single-attempt
        run that never needed to move anything.
        """
        results = run_steering(tmp_path, "steering_retry", {"attempt_id_after": "attempt-1"})

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "did not change" in results["W3-11"].message

    @pytest.mark.parametrize("field", ["attempt_id_before", "attempt_id_after"])
    @pytest.mark.parametrize("value", [None, "", "   ", 17])
    def test_a_missing_attempt_identity_fails(self, tmp_path: Path, field: str, value):
        results = run_steering(tmp_path, "steering_retry", {field: value})

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert field in results["W3-11"].message

    @pytest.mark.parametrize("outcome", ["delivered", "pending"])
    def test_a_guessed_ambiguous_handoff_fails(self, tmp_path: Path, outcome):
        """Guessing is worse than reporting, in both directions."""
        results = run_steering(
            tmp_path, "steering_retry", {"ambiguous_handoff_outcome": outcome}
        )

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "unknown" in results["W3-11"].message

    def test_an_attempt_started_after_an_abort_during_backoff_fails(self, tmp_path: Path):
        """Backoff is not a window in which cancellation is deferred."""
        results = run_steering(
            tmp_path,
            "steering_retry",
            {"abort_during_retry_started_next_attempt": True},
        )

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "backoff" in results["W3-11"].message

    @pytest.mark.parametrize("value", [None, "", "   ", 17])
    def test_a_missing_queued_command_id_fails(self, tmp_path: Path, value):
        results = run_steering(tmp_path, "steering_retry", {"queued_command_id": value})

        assert results["W3-11"].status == _mod.STATUS_FAILED
        assert "queued_command_id" in results["W3-11"].message


class TestWave3IsHonestlyIncomplete:
    """Wave 3 must report 5/12, not 5/5, and must not exit 0.

    The load-bearing claim of the partial-wave design, at the wave-3 boundary.
    Registering only the implemented checks would make `required` five, five would
    pass, `passed == required` would hold and `--wave 3` would exit 0 — a report
    indistinguishable from a complete wave-3 pass on a build with no abort
    evidence whatsoever.
    """

    def test_the_manifest_is_the_whole_evaluation_not_the_implemented_subset(self):
        assert len(_mod.WAVE3_CHECKS) == 12
        assert set(_mod.WAVE3_PREDICATES) == set(WAVE3_IMPLEMENTED)
        registered = {spec.check_id for spec in _mod.WAVE3_CHECKS}
        assert set(WAVE3_IMPLEMENTED) < registered

    def test_every_unimplemented_wave3_check_names_its_owner(self):
        """A not_run has to say who to go to, or it reads as a harness bug."""
        registered = {spec.check_id for spec in _mod.WAVE3_CHECKS}
        outstanding = registered - set(_mod.WAVE3_PREDICATES)

        assert outstanding == {"W3-01", "W3-02", "W3-03", "W3-04", "W3-05", "W3-10", "W3-12"}
        for check_id in outstanding:
            assert _mod.PENDING_CHECK_OWNERS[check_id].strip()

    def test_the_abort_checks_are_attributed_to_s4(self):
        """Stated directly because it is the attribution a reader will act on."""
        for check_id in ("W3-02", "W3-03", "W3-04"):
            assert "3963" in _mod.PENDING_CHECK_OWNERS[check_id], check_id

    def test_a_full_wave3_run_cannot_pass_on_the_steering_evidence_alone(
        self, tmp_path: Path
    ):
        """Five passed, seven not_run, nonzero — the honest report.

        Driven through `run_checks` over the FULL manifest rather than the
        implemented subset, because the subset is what would produce the false
        green and this is the assertion that it does not.
        """
        config = live_config(
            tmp_path,
            artifact_payloads={**artifact_payloads(), **steering_artifact_payloads()},
        )
        artifacts = _mod.ArtifactStore(tmp_path, config["artifacts"])
        driver = _mod.Driver(
            config, _mod.Probe(config["gateway_url"], gateway_stub()), artifacts
        )
        manifest = tuple(spec.check_id for spec in _mod.WAVE3_CHECKS)
        with patch.dict("os.environ", IDENTITY_ENV, clear=False):
            results = _mod.run_checks(driver, _mod.WAVE3_CHECKS, manifest_ids=manifest)

        by_status: dict[str, list[str]] = {}
        for result in results:
            by_status.setdefault(result.status, []).append(result.check_id)

        assert sorted(by_status[_mod.STATUS_PASSED]) == sorted(WAVE3_IMPLEMENTED)
        assert len(by_status[_mod.STATUS_NOT_RUN]) == 7
        # No unowned check may FAIL: that would be a harness bug reported as a
        # deployment defect, and it is what run_checks does with an unowned gap.
        assert _mod.STATUS_FAILED not in by_status, by_status


# Wave 4 (#3966): the dashboard's own checks
#
# These read a captured browser run, so the thing worth testing is that the
# capture cannot be made to pass by asserting a conclusion in it. Each test below
# mutates one key of an otherwise-passing capture and expects a named failure.
# ---------------------------------------------------------------------------


def run_w4(tmp_path: Path, check_id: str, *, capture=None, client=None, config=None):
    """Drive one wave-4 check alone and return its CheckResult.

    `capture` replaces the browser_control_run payload; pass `False` to omit the
    artifact entirely (the not_run path).
    """
    payloads = artifact_payloads()
    if capture is False:
        payloads.pop("browser_control_run")
    elif capture is not None:
        payloads["browser_control_run"] = capture
    cfg = config or live_config(tmp_path, artifact_payloads=payloads)
    spec = next(s for s in _mod.WAVE4_CHECKS if s.check_id == check_id)
    results = run_driver(
        tmp_path,
        config=cfg,
        client=client or gateway_stub(),
        specs=(spec,),
    )
    return results[check_id]


class TestWaveFourManifestHonesty:
    """The wave cannot report itself complete on the four checks S7 implements."""

    def test_the_unimplemented_checks_report_not_run_naming_their_owner(self, tmp_path):
        """An owned not_run, never a silent pass and never an unowned failure."""
        cfg = live_config(tmp_path)
        results = run_driver(
            tmp_path, config=cfg, client=gateway_stub(), specs=_mod.WAVE4_CHECKS
        )
        for check_id in ("W4-01", "W4-03", "W4-05", "W4-06", "W4-09", "W4-10"):
            result = results[check_id]
            assert result.status == _mod.STATUS_NOT_RUN, (check_id, result.status)
            assert result.message and result.message.strip(), check_id

    def test_a_full_wave_four_run_cannot_report_complete_in_this_revision(self, tmp_path):
        """The honest bar: ten required, six unanswerable, so never `not_run == 0`.

        This is the assertion that stops wave 4 being cited as passed on the
        strength of the dashboard checks alone.
        """
        cfg = live_config(tmp_path)
        results = run_driver(
            tmp_path, config=cfg, client=gateway_stub(), specs=_mod.WAVE4_CHECKS
        )
        assert len(results) == 10
        assert any(r.status == _mod.STATUS_NOT_RUN for r in results.values())


class TestW4_02_FeatureGating:
    """AC-F3: absent with the flag off, while loading, and on backend error."""

    @pytest.mark.parametrize("phase", ["flag_off", "flag_loading", "flag_error"])
    def test_a_rendered_control_node_in_any_negative_phase_fails(self, tmp_path, phase):
        capture = browser_control_run_payload(
            **{phase: {"control_nodes": 1, "command_requests": 0}}
        )
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert phase.split("_")[-1] in result.message or "control node" in result.message

    @pytest.mark.parametrize("phase", ["flag_off", "flag_loading", "flag_error"])
    def test_a_command_request_in_any_negative_phase_fails(self, tmp_path, phase):
        """Zero nodes with a request sent means an invisible control still acted."""
        capture = browser_control_run_payload(
            **{phase: {"control_nodes": 0, "command_requests": 1}}
        )
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "command request" in result.message

    def test_a_boolean_instead_of_a_count_is_rejected(self, tmp_path):
        """`False` cannot distinguish "none observed" from "not measured"."""
        capture = browser_control_run_payload(
            flag_off={"control_nodes": False, "command_requests": 0}
        )
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "counted integer" in result.message

    def test_offering_an_unadvertised_verb_fails(self, tmp_path):
        """The fail-open this check exists for: a button the run cannot honour."""
        capture = browser_control_run_payload(
            advertised_capabilities={
                "pause": True, "resume": False, "steer": False, "abort": False
            },
            rendered_controls=["pause", "steer"],
        )
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "steer" in result.message

    def test_no_rendered_controls_is_not_run_rather_than_a_pass(self, tmp_path):
        """A capture that never exercised the positive case proves nothing."""
        capture = browser_control_run_payload(rendered_controls=[])
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_NOT_RUN

    @pytest.mark.parametrize(
        "key", ["nonowner_submit_blocked", "terminal_submit_blocked"]
    )
    def test_an_unproven_submit_block_fails(self, tmp_path, key):
        result = run_w4(tmp_path, "W4-02", capture=browser_control_run_payload(**{key: False}))
        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    def test_a_passing_capture_passes(self, tmp_path):
        assert run_w4(tmp_path, "W4-02").status == _mod.STATUS_PASSED

    def test_a_missing_capture_is_not_run(self, tmp_path):
        result = run_w4(tmp_path, "W4-02", capture=False)
        assert result.status == _mod.STATUS_NOT_RUN


class TestW4_02_CaptureProvenance:
    """A capture that cannot be tied to a deployment cannot answer for one."""

    @pytest.mark.parametrize("key", ["bundle_revision", "gateway_url"])
    def test_a_capture_without_provenance_is_not_run(self, tmp_path, key):
        result = run_w4(tmp_path, "W4-02", capture=browser_control_run_payload(**{key: ""}))
        assert result.status == _mod.STATUS_NOT_RUN
        assert key in result.message

    def test_a_capture_from_another_deployment_fails(self, tmp_path):
        """Observations from a laptop dev server must not answer for the fixture."""
        capture = browser_control_run_payload(gateway_url="http://localhost:5173")
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "localhost" in result.message

    def test_an_unorderable_capture_time_fails(self, tmp_path):
        capture = browser_control_run_payload(captured_at="last tuesday")
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "captured_at" in result.message

    def test_a_capture_without_a_spec_digest_fails(self, tmp_path):
        """A weakened spec and the real one must not leave identical evidence."""
        capture = browser_control_run_payload(spec_digest="")
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "spec_digest" in result.message


class TestW4_04_PauseHonesty:
    """AC-P4: the phase sequence, the spend warning, and no false quiescence."""

    def test_a_passing_capture_passes(self, tmp_path):
        assert run_w4(tmp_path, "W4-04").status == _mod.STATUS_PASSED

    def test_a_missing_transition_fails(self, tmp_path):
        capture = browser_control_run_payload(phase_sequence=["running", "paused"])
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "pause_requested" in result.message

    def test_paused_without_passing_through_requested_fails(self, tmp_path):
        """The specific lie: the UI deciding a run is paused rather than reporting it."""
        capture = browser_control_run_payload(
            phase_sequence=["running", "paused", "pause_requested", "paused", "running"]
        )
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED

    def test_missing_spend_copy_fails(self, tmp_path):
        capture = browser_control_run_payload(pause_copy_mentions_spend=False)
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "spend" in result.message

    def test_an_unobserved_count_rendered_as_a_bare_zero_fails(self, tmp_path):
        """"0 tools" from an unreported count is the false quiescence claim."""
        capture = browser_control_run_payload(active_tool_reason="Tools in progress: 0")
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED

    def test_asserting_quiescence_outright_fails(self, tmp_path):
        capture = browser_control_run_payload(active_tool_reason="No tools running")
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED

    @pytest.mark.parametrize(
        "reason",
        [
            "Tool count unknown, so no tools running right now",
            "Tools in progress: unknown — nothing is running",
        ],
    )
    def test_hedged_text_that_still_asserts_quiescence_fails(self, tmp_path, reason):
        """The guard must survive text that also says "unknown".

        Copy that says both — "unknown" to satisfy the wording rule and "no tools
        running" to reassure the operator — is the worst version of this bug, since
        it reads as quiescence while passing a naive keyword check. Without this
        case the outright-quiescence branch is never reached: the missing-hedge
        check above it fires first on plainly-worded text.
        """
        capture = browser_control_run_payload(active_tool_reason=reason)
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "quiescence" in result.message

    def test_a_qualified_zero_is_accepted(self, tmp_path):
        """"none reported" attributes the zero to the worker rather than claiming it."""
        capture = browser_control_run_payload(
            active_tool_reason="Tools in progress: none reported"
        )
        assert run_w4(tmp_path, "W4-04", capture=capture).status == _mod.STATUS_PASSED


class TestW4_08_PollingLifecycle:
    """Measured intervals, not a configured constant."""

    def test_a_passing_capture_passes(self, tmp_path):
        assert run_w4(tmp_path, "W4-08").status == _mod.STATUS_PASSED

    def test_a_single_timestamp_is_not_an_interval(self, tmp_path):
        capture = browser_control_run_payload(poll_intervals_ms=[2000])
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "two measured intervals" in result.message

    @pytest.mark.parametrize("interval", [200, 30000])
    def test_an_interval_outside_the_contract_fails(self, tmp_path, interval):
        capture = browser_control_run_payload(poll_intervals_ms=[interval, interval])
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "2-second contract" in result.message

    @pytest.mark.parametrize(
        "key", ["polled_while_hidden", "polled_after_close", "polled_after_terminal"]
    )
    def test_polling_that_did_not_stop_fails(self, tmp_path, key):
        result = run_w4(tmp_path, "W4-08", capture=browser_control_run_payload(**{key: True}))
        assert result.status == _mod.STATUS_FAILED
        assert key in result.message

    @pytest.mark.parametrize(
        "key", ["polled_while_hidden", "polled_after_close", "polled_after_terminal"]
    )
    def test_an_unmeasured_stop_condition_fails(self, tmp_path, key):
        """Must be an observed `false`, not an absent key read as falsy."""
        capture = browser_control_run_payload(**{key: None})
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED

    def test_a_decreasing_retry_interval_fails(self, tmp_path):
        """Retrying a failing endpoint faster is the load-amplifying bug.

        A bounded backoff legitimately plateaus at its cap. Below that cap,
        constant-rate retries and decreases must both fail.
        """
        decreasing = browser_control_run_payload(backoff_intervals_ms=[8000, 2000])
        result = run_w4(tmp_path, "W4-08", capture=decreasing)
        assert result.status == _mod.STATUS_FAILED
        assert "do not increase" in result.message

    @pytest.mark.parametrize("intervals", [[2000, 2000], [True, True], [float("nan"), 8000]])
    def test_flat_below_cap_or_invalid_backoff_fails(self, tmp_path, intervals):
        capture = browser_control_run_payload(backoff_intervals_ms=intervals)
        assert run_w4(tmp_path, "W4-08", capture=capture).status == _mod.STATUS_FAILED

    def test_a_backoff_plateaued_at_its_cap_is_accepted(self, tmp_path):
        capture = browser_control_run_payload(backoff_intervals_ms=[30000, 30000])
        assert run_w4(tmp_path, "W4-08", capture=capture).status == _mod.STATUS_PASSED

    def test_a_detail_that_never_refreshed_fails(self, tmp_path):
        capture = browser_control_run_payload(detail_refreshed_after_command=False)
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "detail" in result.message

    def test_a_delivered_steer_with_no_pending_state_fails(self, tmp_path):
        """Labelling an enqueue as delivered is the comprehension claim AC-T1 forbids."""
        capture = browser_control_run_payload(steer_status_sequence=["delivered"])
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "delivered" in result.message


class TestW4_02_MalformedCaptureShapes:
    """A capture whose observations are the wrong TYPE must fail, not crash.

    These are the paths a hand-edited or partially-written evidence file takes.
    They matter because the alternative to an explicit type check is a
    `TypeError` escaping the predicate, which the driver would report as a
    harness crash rather than as the unusable evidence it is.
    """

    @pytest.mark.parametrize("phase", ["flag_off", "flag_loading", "flag_error"])
    def test_a_negative_phase_that_is_not_an_object_fails(self, tmp_path, phase):
        capture = browser_control_run_payload(**{phase: "no controls"})
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert phase in result.message

    def test_an_absent_count_key_fails(self, tmp_path):
        """A half-written observation is not evidence of zero."""
        capture = browser_control_run_payload(flag_off={"control_nodes": 0})
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "command_requests" in result.message

    def test_non_dict_advertised_capabilities_fails(self, tmp_path):
        capture = browser_control_run_payload(advertised_capabilities=["pause"])
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "advertised_capabilities" in result.message

    def test_non_list_rendered_controls_fails(self, tmp_path):
        capture = browser_control_run_payload(rendered_controls="pause,resume")
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "rendered_controls" in result.message

    def test_a_truthy_non_true_capability_does_not_authorize_a_control(self, tmp_path):
        """`"pause": "yes"` is not an advertisement; only `true` is.

        Guards the `value is True` comparison. A loose truthiness test would let a
        gateway serving a string, or a `1`, license a rendered control.
        """
        capture = browser_control_run_payload(
            advertised_capabilities={
                "pause": "yes", "resume": False, "steer": False, "abort": False
            },
            rendered_controls=["pause"],
        )
        result = run_w4(tmp_path, "W4-02", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "pause" in result.message


class TestW4_04_MalformedPhaseSequence:
    @pytest.mark.parametrize("sequence", [None, [], "running,paused"])
    def test_a_sequence_that_is_not_a_nonempty_list_fails(self, tmp_path, sequence):
        capture = browser_control_run_payload(phase_sequence=sequence)
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "phase_sequence" in result.message

    @pytest.mark.parametrize("reason", [None, "", "   ", 0])
    def test_an_unrecorded_tool_reason_fails(self, tmp_path, reason):
        """Absent text is not the same as text that says "unknown"."""
        capture = browser_control_run_payload(active_tool_reason=reason)
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "active_tool_reason" in result.message

    def test_no_resume_after_the_pause_fails(self, tmp_path):
        """The sequence must close the loop: a run that never resumed is stuck."""
        capture = browser_control_run_payload(
            phase_sequence=["running", "pause_requested", "paused"]
        )
        result = run_w4(tmp_path, "W4-04", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "running" in result.message


class TestW4_07_LiveSchemaParity:
    """The live response must carry every field control_schemas.py declares.

    W4-07 is the only wave-4 check that talks to the gateway rather than reading a
    capture, so its failure modes are served responses — and the one that matters
    most is the pod-address leak, since that reaches the browser regardless of
    what the UI chooses to render.
    """

    def test_a_conforming_response_passes(self, tmp_path):
        assert run_w4(tmp_path, "W4-07").status == _mod.STATUS_PASSED

    def test_a_non_200_state_response_fails(self, tmp_path):
        """A contract cannot be compared against a response that was not served."""
        result = run_w4(tmp_path, "W4-07", client=gateway_stub(state_status=503))
        assert result.status == _mod.STATUS_FAILED
        assert "503" in result.message

    @pytest.mark.parametrize(
        "field",
        [
            "run_id",
            "generation",
            "available",
            "reason",
            "capabilities",
            "state",
            "active_tool_count",
            "updated_at",
            "commands",
        ],
    )
    def test_a_field_the_backend_declares_but_the_response_omits_fails(
        self, tmp_path, field
    ):
        result = run_w4(tmp_path, "W4-07", client=gateway_stub(state_omit=(field,)))
        assert result.status == _mod.STATUS_FAILED
        assert field in result.message

    def test_an_omitted_capability_verb_fails(self, tmp_path):
        """An absent verb key is indistinguishable from `false` to the client."""
        partial = {v: False for v in _mod.CONTROL_VERBS}
        dropped = partial.pop("steer")
        assert dropped is False
        result = run_w4(tmp_path, "W4-07", client=gateway_stub(capabilities=partial))
        assert result.status == _mod.STATUS_FAILED
        assert "steer" in result.message

    def test_non_object_capabilities_fails(self, tmp_path):
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={"capabilities": []})
        )
        assert result.status == _mod.STATUS_FAILED
        assert "capabilities" in result.message

    @pytest.mark.parametrize(
        "banned",
        ["pod_ip", "pod_address", "pod_port", "control_token", "token", "address"],
    )
    def test_a_pod_coordinate_in_the_public_body_fails(self, tmp_path, banned):
        """The response the browser receives: a leak here is a leak to devtools."""
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={banned: "10.0.42.7"})
        )
        assert result.status == _mod.STATUS_FAILED
        assert banned in result.message

    def test_non_list_commands_fails(self, tmp_path):
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={"commands": {}})
        )
        assert result.status == _mod.STATUS_FAILED
        assert "commands" in result.message

    def test_a_non_object_command_entry_fails(self, tmp_path):
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={"commands": ["cmd-1"]})
        )
        assert result.status == _mod.STATUS_FAILED
        assert "commands[0]" in result.message

    def test_a_command_entry_missing_its_acknowledgement_fields_fails(self, tmp_path):
        """The per-command status the dashboard keys on must be fully present."""
        entry = {"command_id": "c-1", "action": "pause", "status": "pending"}
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={"commands": [entry]})
        )
        assert result.status == _mod.STATUS_FAILED
        assert "delivered_at" in result.message

    def test_a_fully_populated_command_entry_passes(self, tmp_path):
        entry = {
            "command_id": "c-1",
            "action": "pause",
            "status": "delivered",
            "accepted_at": "2026-09-12T00:00:00Z",
            "delivered_at": "2026-09-12T00:00:01Z",
            "reason": None,
        }
        result = run_w4(
            tmp_path, "W4-07", client=gateway_stub(state_extra={"commands": [entry]})
        )
        assert result.status == _mod.STATUS_PASSED


class TestW4_08_MalformedPollObservations:
    @pytest.mark.parametrize("intervals", [None, "2000,2000", [2000, "2000"]])
    def test_non_numeric_intervals_fail(self, tmp_path, intervals):
        capture = browser_control_run_payload(poll_intervals_ms=intervals)
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "poll_intervals_ms" in result.message

    @pytest.mark.parametrize("backoff", [None, [2000], "2000,4000"])
    def test_an_unmeasured_backoff_fails(self, tmp_path, backoff):
        capture = browser_control_run_payload(backoff_intervals_ms=backoff)
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED
        assert "backoff_intervals_ms" in result.message

    def test_a_non_numeric_backoff_entry_fails(self, tmp_path):
        capture = browser_control_run_payload(backoff_intervals_ms=[2000, "4000"])
        result = run_w4(tmp_path, "W4-08", capture=capture)
        assert result.status == _mod.STATUS_FAILED

    def test_a_pending_then_delivered_steer_is_accepted(self, tmp_path):
        capture = browser_control_run_payload(
            steer_status_sequence=["pending", "pending", "delivered"]
        )
        assert run_w4(tmp_path, "W4-08", capture=capture).status == _mod.STATUS_PASSED

    def test_a_steer_that_never_reached_delivered_is_accepted(self, tmp_path):
        """A still-pending steer is an honest state, not a failure.

        W4-08 forbids claiming delivery without a preceding pending; it does not
        require delivery to have happened, which is S6's subject and not this
        check's to assert.
        """
        capture = browser_control_run_payload(steer_status_sequence=["pending"])
        assert run_w4(tmp_path, "W4-08", capture=capture).status == _mod.STATUS_PASSED

    @pytest.mark.parametrize("sequence", [None, [], "pending"])
    def test_an_absent_steer_sequence_does_not_fail_this_check(self, tmp_path, sequence):
        """Steer is optional here: a run whose adapter cannot steer still polls.

        W4-08's subject is the polling lifecycle. The steer vocabulary is asserted
        only when the capture recorded one, because a deployment that advertises
        `steer: false` has no sequence to show and must not fail a polling check
        for it. AC-T1's steer evidence is W4-03's, which S6 owns.
        """
        capture = browser_control_run_payload(steer_status_sequence=sequence)
        assert run_w4(tmp_path, "W4-08", capture=capture).status == _mod.STATUS_PASSED


class TestSummarizeFixtureCleanupUsesTheDeclaredVerificationIds:
    """The hardcoded "W2-10" was a latent false green for any third wave.

    `resource_teardown_expected` is derived from whether the wave HAS post-cleanup
    checks, while the absence lookup used a literal ID. A wave whose verification
    check had a different name would set the first True, find no "W2-10", take the
    "this wave declares none" branch and publish absence_verified=True with nothing
    having verified absence. Both now derive from POST_CLEANUP_CHECK_IDS.
    """

    @staticmethod
    def _result(check_id: str, status: str):
        return _mod.CheckResult(
            check_id=check_id, status=status, description="d", acceptance_ids=("x",)
        )

    def test_the_default_verification_ids_come_from_the_declared_set(self):
        import inspect

        default = inspect.signature(_mod.summarize_fixture_cleanup).parameters[
            "verification_ids"
        ].default
        assert set(default) == set(_mod.POST_CLEANUP_CHECK_IDS)

    def test_a_differently_named_verification_check_still_withholds(self):
        """The regression: a failed post-cleanup check under another ID must not pass."""
        cleanup = _mod.CleanupOutcome(ok=True, notes=[], deletions=[], declared_items=0)
        teardown = _mod.ResourceTeardown(
            configured=True, invoked=True, ok=True, exit_code=0,
            started_at="2026-09-24T10:00:00Z", finished_at="2026-09-24T10:01:00Z",
            stdout_digest=None, notes=[],
            verification_present_before=False, verification_digest_before=None,
        )
        results = [self._result("W9-99", _mod.STATUS_FAILED)]
        summary = _mod.summarize_fixture_cleanup(
            cleanup, teardown, results,
            resource_teardown_expected=True,
            verification_ids=("W9-99",),
        )
        assert summary.absence_verified is False
        assert summary.ok is False
        assert any("W9-99" in note for note in summary.notes)

    def test_a_wave_with_no_verification_check_is_unaffected(self):
        """Wave 1 declares none, so there is no unestablished absence to withhold."""
        cleanup = _mod.CleanupOutcome(ok=True, notes=[], deletions=[], declared_items=0)
        summary = _mod.summarize_fixture_cleanup(
            cleanup, None, [], resource_teardown_expected=False
        )
        assert summary.ok is True


# ---------------------------------------------------------------------------
# End-to-end: the real collector → the real artifact file → the real evaluator
#
# Everything above this line drives the evaluator from hand-written artifact
# payloads. That is the right shape for testing one predicate against one mutated
# field, and it is NOT sufficient for #3970's kickoff requirement, which is
# explicit: "Tests must run through the real collector-to-artifact-to-evaluator
# entrypoint using controlled transports."
#
# The distinction is not pedantic. A hand-written payload is a fixture example,
# and the kickoff names that too: "a dictionary of booleans or a fixture example
# is not a producer." A test suite built only on such payloads proves the
# evaluator rejects bad INPUT while saying nothing about whether anything can
# produce good input — so a collector that emitted `verified: False` whenever it
# could not reach the deployment would pass every test above and still convert
# "we did not look" into "we looked and it was wrong" on a live run.
#
# So the tests below wire the actual `operator-wave4` collectors to controlled
# transports (an injected `fetch`, `dist_listing`, `read_result`, `run_lookup`,
# `identity_lookup`, `flag_lookup`, `git_runner`), write `Artifact.build()` to
# disk as the real artifact files, and drive the actual `Driver` over the actual
# `WAVE_CHECKS[4]` manifest. Nothing between the measurement and the verdict is
# stubbed.
#
# The counterexamples the kickoff enumerates each get a test, and each is
# expressed by breaking the TRANSPORT rather than by editing the artifact: a 503
# from the SPA route, a wave report showing 9/10, a `gh run view` response whose
# named job failed. That is what makes them tests of the producer.
# ---------------------------------------------------------------------------

# Loaded as a package so `from .collector import ...` resolves. The directory is
# hyphenated deliberately — `platform/scripts/operator/__init__.py` would shadow the
# standard library's `operator` module for anything importing with `platform/scripts`
# on the path, and a hyphen cannot appear in an importable name, which makes the
# collision impossible to recreate by accident. See the package docstring.
_W4_PKG_DIR = Path(__file__).resolve().parent.parent / "operator-wave4"
_w4_spec = importlib.util.spec_from_file_location(
    "operator_wave4", _W4_PKG_DIR / "__init__.py", submodule_search_locations=[str(_W4_PKG_DIR)]
)
assert _w4_spec and _w4_spec.loader
_w4 = importlib.util.module_from_spec(_w4_spec)
sys.modules["operator_wave4"] = _w4
_w4_spec.loader.exec_module(_w4)

_collector = importlib.import_module("operator_wave4.collector")
_preflight = importlib.import_module("operator_wave4.preflight")
_consolidated = importlib.import_module("operator_wave4.consolidated")
_index = importlib.import_module("operator_wave4.evidence_index")


# The deployed frontend's revision: a commit that exists in `CommitGraph` and is
# contained in both deployed components, since a correct SPA build comes from the
# same history as the backend it calls.
FRONTEND_REVISION = "e" * 40
# The hashed assets a build of that revision emits. Content-hashed names are what
# make the served/built comparison a real fingerprint rather than a name check.
FRONTEND_ASSETS = ("index-a1b2c3d4.js", "index-9f8e7d6c.css")


def w4_commit_graph(**overrides) -> CommitGraph:
    """`CommitGraph` extended with the frontend commit wave 4 adds.

    Built on the wave-2 topology rather than replacing it: wave 4's preflight
    requires the prior waves' accepted revisions to be contained in what is deployed,
    so the same ancestry the wave-2 tests rely on has to keep holding.
    """
    parents = {
        WAVE1_ACCEPTED_REVISION: (),
        WAVE2_REVISION: (WAVE1_ACCEPTED_REVISION,),
        FRONTEND_REVISION: (WAVE2_REVISION,),
        DEPLOYED_WORKER_REVISION: (FRONTEND_REVISION,),
        DEPLOYED_GATEWAY_REVISION: (FRONTEND_REVISION,),
        # The default branch tip, present as a real node because "merged" is asked of
        # the graph: the preflight records a story as merged only if `git merge-base
        # --is-ancestor <story> origin/main` says so. Modelling main as a node that
        # reaches both deployed components is what makes that query answerable, and
        # what lets a test express "not merged" by handing over a story commit that
        # main does not reach — rather than by editing the artifact's `merged` flag.
        "origin/main": (DEPLOYED_WORKER_REVISION, DEPLOYED_GATEWAY_REVISION),
    }
    parents.update(overrides)
    return CommitGraph(parents)


def staleness_runner(graph: CommitGraph, *, touched: dict | None = None, answerable: bool = True):
    """`graph.runner()` extended to answer the surface-modification query.

    The consolidating checks ask `git log -1 --format=%cI -- <surface>` for every
    source path their criteria cover, and `CommitGraph` models ancestry only. Without
    an answer every one of them is `not_run` on an unanswerable staleness question —
    which is the harness behaving correctly on a shallow clone, and useless as a
    fixture for anything else.

    `touched` maps a surface path to the instant it was last modified, so a test can
    make ONE surface postdate the evidence and watch that check alone fail. The
    default is an instant comfortably before any evidence timestamp: not stale.

    `answerable=False` models the shallow clone itself — git exits nonzero for the
    path — because "the harness could not look" must stay distinguishable from "it
    looked and the surface is newer". The first is not_run and the second is failed.
    """
    inner = graph.runner()
    touched = touched or {}
    default = relative_time(-86_400)

    def run(argv):
        if "log" in argv:
            if not answerable:
                return SimpleNamespace(returncode=128, stdout="", stderr="shallow clone")
            path = argv[-1]
            return SimpleNamespace(
                returncode=0, stdout=touched.get(path, default) + "\n", stderr=""
            )
        return inner(argv)

    return run


def spa_transport(
    assets: Sequence[str] = FRONTEND_ASSETS, *, status: int = 200, body: str | None = None
):
    """A controlled `fetch` answering the SPA route the way a deployment would.

    Returns `(status, body)` and nothing else, so the collector's refusal semantics
    are exercised against real response shapes: a 503 from an unreachable deployment,
    a 200 carrying an error page with no asset references, and a 200 carrying the
    index HTML with hashed `<script>`/`<link>` tags.
    """

    def fetch(url: str) -> tuple[int, str]:
        if body is not None:
            return status, body
        tags = "".join(
            f'<script type="module" src="/assets/{name}"></script>'
            if name.endswith(".js")
            else f'<link rel="stylesheet" href="/assets/{name}">'
            for name in assets
        )
        return status, f"<!doctype html><html><head>{tags}</head><body></body></html>"

    return fetch


def wave_report(wave: int, **overrides) -> dict:
    """One earlier wave's own `result.json`, in the shape `build_report` writes.

    The counts come from the real manifest length rather than a literal, so a wave
    whose manifest grows does not leave this fixture quietly describing a partial
    acceptance as a full one.
    """
    required = len(_mod.WAVE_CHECKS[wave]) if wave in _mod.WAVE_CHECKS else 12
    report = {
        "evaluation": _mod.WAVE_EVALUATIONS.get(wave, "3969"),
        "run_id": f"wave{wave}-run-001",
        "revision": WAVE1_ACCEPTED_REVISION if wave == 1 else WAVE2_REVISION,
        "passed": required,
        "required": required,
        "fixture_cleanup": {"ok": True},
    }
    report.update(overrides)
    return report


def wave_reports(**overrides) -> dict:
    """The three prior waves' reports, keyed by wave.

    Wave 3 is present here even though this revision registers no wave-3 MANIFEST.
    That separation is deliberate and is a thing the tests below rely on: a report
    can exist while the manifest does not, and the evaluator must still refuse —
    `_assert_prior_wave_accepted` raises `PrerequisiteMissingError` for an
    unregistered wave, so wave 4 cannot be completed by supplying wave 3's paperwork.
    """
    reports = {wave: wave_report(wave) for wave in (1, 2, 3)}
    reports.update({int(wave): report for wave, report in overrides.items()})
    return reports


def reports_with(wave: int, **overrides) -> dict:
    """The three prior reports, with ONE wave's own report altered.

    Wave numbers are integers and keyword arguments are not, which is why this exists
    rather than callers writing `wave_reports(2=...)`. Altering the report — not the
    acceptance record derived from it — is what keeps these counterexamples tests of
    the collector: `measure_prior_wave` has to carry a 9/10 through as a 9/10.
    """
    return wave_reports(**{str(wave): wave_report(wave, **overrides)})


def reports_without(wave: int) -> dict:
    """The prior reports with one wave's report absent from disk entirely.

    Distinct from a report that says something wrong: this is the collection gap, and
    it must arrive as a named refusal rather than as a finding about the deployment.
    """
    return {number: report for number, report in wave_reports().items() if number != wave}


def run_lookup_for(gates: Sequence[str] | None = None, **overrides):
    """A controlled `gh run view` transport, one response per gate.

    The response carries the real field locations (`databaseId`, `headSha`, `jobs[]`
    with per-job `conclusion`), because the evaluator PARSES it at those locations —
    a document that merely mentions the right strings does not pass, so a fixture that
    fabricated a flatter shape would be testing nothing.
    """
    names = tuple(gates if gates is not None else _mod.WAVE4_REQUIRED_CI_GATES)
    responses = {
        name: {
            "databaseId": 4400 + index,
            # Tested at the frontend commit, because these are the FRONTEND gates and
            # the evaluator requires each gate's tested revision to be contained in
            # every deployed component. A gate run on a later branch commit — the
            # deployed worker, say — passed on code the served bundle does not carry,
            # which the evaluator correctly rejects.
            "headSha": FRONTEND_REVISION,
            "attempt": 1,
            "event": "push",
            "conclusion": "success",
            "url": f"https://github.com/aws-e/adp/actions/runs/{4400 + index}",
            "jobs": [{"name": name, "conclusion": "success"}],
        }
        for index, name in enumerate(names)
    }
    responses.update(overrides)

    def lookup(gate: str):
        if gate not in responses:
            return _collector.Refused(f"`gh run view` found no run for {gate!r}")
        return responses[gate]

    return lookup


def collect_preflight(
    tmp_path: Path,
    *,
    config: dict,
    fetch=None,
    assets: Sequence[str] = FRONTEND_ASSETS,
    reports: dict | None = None,
    run_lookup=None,
    identity=None,
    flags=None,
    graph: CommitGraph | None = None,
    frontend_ref: str = FRONTEND_REVISION,
):
    """Run the REAL preflight collector against controlled transports.

    Returns `(artifact, graph)`. Every collaborator is injected, and each one is a
    seam a counterexample below breaks: `fetch` is the deployment, `reports` is the
    prior waves' own evidence, `run_lookup` is GitHub, `identity`/`flags` are the
    gateway's answers about authorization, and `graph` is the commit graph.
    """
    graph = graph if graph is not None else w4_commit_graph()
    runner = graph.runner()

    def git_runner(argv):
        result = runner(argv)
        return _collector.CommandResult(
            argv=tuple(argv),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # `git rev-parse` is not part of `CommitGraph`'s vocabulary (it answers ancestry
    # and existence), so refs resolve here. Identity mapping on purpose: the tests
    # pass full SHAs, and a ref that is not a known commit must resolve to nothing so
    # `measure_revision` refuses rather than inventing.
    def resolving_runner(argv):
        if "rev-parse" in argv:
            ref = argv[-1]
            if ref in graph.parents:
                return _collector.CommandResult(
                    argv=tuple(argv), returncode=0, stdout=ref + "\n", stderr=""
                )
            return _collector.CommandResult(
                argv=tuple(argv), returncode=128, stdout="", stderr=f"unknown revision {ref}"
            )
        return git_runner(argv)

    reports = wave_reports() if reports is None else reports

    def read_result(wave: int):
        if wave not in reports:
            raise FileNotFoundError(f"no result.json for wave {wave}")
        return reports[wave]

    artifact = _preflight.collect(
        config=config,
        retrieved_at=relative_time(-60),
        frontend_ref=frontend_ref,
        story_refs={"S7": FRONTEND_REVISION},
        prior_waves=(1, 2, 3),
        gates=_mod.WAVE4_REQUIRED_CI_GATES,
        fetch=fetch if fetch is not None else spa_transport(assets),
        dist_listing=lambda revision: list(assets),
        read_result=read_result,
        run_lookup=run_lookup if run_lookup is not None else run_lookup_for(),
        identity_lookup=lambda: (
            identity if identity is not None else {"role": "owner", "is_run_owner": True}
        ),
        flag_lookup=lambda: (
            flags
            if flags is not None
            else {"ordinary_users_gated": True, "ordinary_flags_off": True}
        ),
        deployed_components=deployed_components(),
        git_runner=resolving_runner,
    )
    return artifact, graph


def evidence_document(check_id: str, **overrides) -> dict:
    """The owning wave's evidence document, as that wave's run wrote it.

    This is the INPUT to the consolidating collectors, not an artifact: the collector
    transcribes it and adds the wave-4 metadata. Modelling it separately is what makes
    the transcription testable — a test can record a `failed` criterion or a `False`
    proof here and assert the collector carried it through rather than tidied it up.
    """
    spec = _consolidated.CONSOLIDATED_ARTIFACTS[check_id]
    document: dict = {
        "evaluation": _mod.WAVE_EVALUATIONS.get(spec["wave"], "3969"),
        "evidenced_revision": FRONTEND_REVISION,
        # Comfortably after every surface's last modification in the real repository,
        # since the staleness assertion compares against `git log` on this checkout.
        "evidenced_at": relative_time(0),
        "criteria": {
            acceptance_id: {
                "status": _mod.STATUS_PASSED,
                "evidence": [f"wave{spec['wave']}/result.json#{acceptance_id}"],
                "live": acceptance_id in _mod.LIVE_EVIDENCE_REQUIRED_IDS,
            }
            for acceptance_id in _mod.CHECK_ACCEPTANCE_IDS[check_id]
        },
    }
    document.update(W4_EVIDENCE_EXTRAS[check_id])
    document.update(overrides)
    return document


# The per-row fields each consolidating check's own row demands, recorded by the wave
# that observed them. Kept beside `evidence_document` rather than inside it so a test
# can see at a glance which fields belong to which row.
W4_EVIDENCE_EXTRAS: dict[str, dict] = {
    "W4-03": {
        "fifo_order_proven": True,
        "retry_delivery_proven": True,
        "pending_cap_proven": True,
        "sdk_bound_text_proven": True,
        "fixture_pivot": {"executed": True, "at": "2026-09-20T09:00:00Z"},
        "merged_test_pr": {
            "merged": True,
            "url": "https://github.com/aws-e/adp/pull/5901",
        },
    },
    "W4-05": {
        "cancel_left_run_untouched": True,
        "confirmed_abort_terminal": True,
        "repeat_and_double_abort": True,
        "stats_writer_assertions": True,
        "finalized_comment_count": 1,
        "aborted_renderers": {"InvocationChain": True, "InvocationDetail": True},
    },
    "W4-06": {
        "non_gateway_probe_blocked": True,
        "bundle_scan_supplemental": True,
    },
    "W4-09": {
        "flag_off_events_digest": "sha256:runtime",
        "flag_on_events_digest": "sha256:runtime",
        "differing_fields": [],
        "ordinary_flags_off": True,
    },
}


def aborted_row(**overrides) -> dict:
    """The DynamoDB row an aborted run leaves, as a read of it returns.

    A read, not a description: W4-05's row demands "the actual row has completed_at",
    which is the difference between a UI that renders a terminal state and a record
    that is one.
    """
    row = {
        "run_id": "msg-live",
        "status": _mod.ABORTED_STATUS,
        "completed_at": relative_time(-120),
        "generation": 2,
    }
    row.update(overrides)
    return row


def w4_stats_body(**overrides) -> dict:
    """A live `agent-run-stats` response carrying exactly the deployed model's fields.

    Named `w4_` rather than `stats_body` because wave 2 already has a `stats_body`
    helper in this file with a different shape, and at module scope the second
    definition silently replaces the first. That is worth a note: an identically-named
    fixture broke thirteen wave-2 tests with a message about a missing `aborted`
    counter — a failure that reads as a defect in the code under test and is in fact a
    collision between two test helpers. Wave 3's collectors will want their own; the
    prefix is what keeps them from doing this to each other.

    Built from the fields the harness PARSES out of `stats_schemas.py` rather than
    from a literal list, so adding a field to `StatsResponse` does not leave this
    fixture describing a response the dashboard would render blanks for. That is the
    same reason `_stats_response_fields` parses instead of transcribing.
    """
    driver = _mod.Driver(valid_config(), MagicMock(), _mod.ArtifactStore(None, {}))
    body = {field: [] for field in driver._stats_response_fields()}
    body.update(overrides)
    return body


def collect_consolidated_artifact(
    check_id: str, *, config: dict, document: dict | None = None, extra=None
):
    """Run the REAL consolidating collector for one check."""
    document = evidence_document(check_id) if document is None else document
    return _consolidated.collect_consolidated(
        check_id,
        read_evidence=lambda wave: document,
        acceptance_ids=_mod.CHECK_ACCEPTANCE_IDS[check_id],
        proofs=W4_CONSOLIDATED_PROOFS[check_id],
        extra_fields=extra if extra is not None else w4_extra_fields(check_id, config=config),
        fixture_identity={
            "account_id": config["account_id"],
            "environment": config["environment"],
            "run_id": config["live_run_id"],
        },
    )


# The fields each consolidating collector carries verbatim from its source document.
# These are the wave-4 rows' individually-named proofs plus the recorded observations
# the evaluator requires as values rather than as booleans; `collect_consolidated`
# refuses an absent one and emits a recorded `False` unchanged.
W4_CONSOLIDATED_PROOFS: dict[str, tuple[str, ...]] = {
    "W4-03": (
        "fifo_order_proven",
        "retry_delivery_proven",
        "pending_cap_proven",
        "sdk_bound_text_proven",
        "fixture_pivot",
        "merged_test_pr",
    ),
    "W4-05": (
        "cancel_left_run_untouched",
        "confirmed_abort_terminal",
        "repeat_and_double_abort",
        "stats_writer_assertions",
        "finalized_comment_count",
        "aborted_renderers",
    ),
    "W4-06": ("non_gateway_probe_blocked", "bundle_scan_supplemental"),
    "W4-09": (
        "flag_off_events_digest",
        "flag_on_events_digest",
        "differing_fields",
        "ordinary_flags_off",
    ),
}


def w4_extra_fields(check_id: str, *, config: dict, row=None, stats=None) -> dict:
    """The live observations a consolidating artifact carries beyond the transcription.

    Measured by their own collectors against controlled transports, because each talks
    to a different system: W4-05's terminal row is a DynamoDB read, and W4-09's stats
    provenance is an HTTP GET.
    """
    if check_id == "W4-05":
        row = aborted_row() if row is None else row
        return {
            "completed_at_observed": _consolidated.measure_aborted_row(
                config["live_run_id"], row_lookup=lambda run_id: row
            )
        }
    if check_id == "W4-09":
        status, body = stats if stats is not None else (200, w4_stats_body())
        source, keys = _consolidated.measure_stats_source(
            f"{config['gateway_url']}/api/activity/agent-run-stats",
            get=lambda url: (status, body),
        )
        return {"stats_source": source, "stats_response_keys": keys}
    return {}


def prior_report(config: dict, *, statuses: dict | None = None) -> dict:
    """A previous evaluator run's `result.json`, built by the REAL `build_report`.

    Assembled from `CheckResult`s through the harness's own reporter rather than
    hand-written, because the index collector reads it at the shape that function
    produces. A hand-written report could drift from it and the drift would be
    invisible — the collector would read fields nothing writes.

    This is the browser-backed criteria's source: a Playwright capture reports what the
    DOM did, not which acceptance IDs it satisfied, so what maps observations onto
    criteria is the evaluator's own check, and its verdict is what gets borrowed.
    """
    statuses = statuses or {}
    results = [
        _mod.CheckResult(
            check_id=spec.check_id,
            status=statuses.get(spec.check_id, _mod.STATUS_PASSED),
            description=spec.description,
            acceptance_ids=spec.acceptance_ids,
            artifacts=[f"artifacts/{spec.check_id}.json"],
        )
        for spec in _mod.WAVE4_CHECKS
    ]
    return _mod.build_report(
        config,
        results,
        cleanup_ok=True,
        wave=4,
        expected_ids=tuple(spec.check_id for spec in _mod.WAVE4_CHECKS),
    )


def collect_index(
    *,
    config: dict,
    artifacts: dict,
    report: dict | None = None,
    reports: dict | None = None,
    bundle_revision: str = FRONTEND_REVISION,
):
    """Run the REAL index collector, deriving all 37 rows from collected evidence.

    Returns `(artifact, refusals)`. The refusal map is half the output: it names the
    criteria that could not be derived, which is the actionable half and the one the
    kickoff's "missing measurements ... must remain NOT RUN/failure" clause is about.

    `acceptance_ids` comes from `all_acceptance_ids()` — the same derivation the
    evaluator compares against — because two independent derivations of "the 37" could
    disagree, and then the compiler would be authoring the mismatch W4-10 exists to
    detect.
    """
    consolidated_map = {
        _consolidated.CONSOLIDATED_ARTIFACTS[check_id]["artifact"]: _mod.CHECK_ACCEPTANCE_IDS[
            check_id
        ]
        for check_id in _consolidated.CONSOLIDATED_ARTIFACTS
    }
    # AC-F3 and the pause family: evidenced by the browser capture, so their row comes
    # from the evaluator's verdict on it rather than from any recorded field.
    report_backed = {
        "W4-02": _mod.CHECK_ACCEPTANCE_IDS["W4-02"],
        "W4-04": _mod.CHECK_ACCEPTANCE_IDS["W4-04"],
    }
    criteria_sources, report_sources = _index.build_sources(
        consolidated=consolidated_map,
        report_backed=report_backed,
        owners={
            "wave4_steering_evidence": "#3969 (wave 3 steering)",
            "wave4_abort_evidence": "#3969 (wave 3 abort)",
            "wave4_security_matrix": "#3969 (wave 3 security)",
            "wave4_runtime_comparison": "#3968 (wave 2 runtime)",
            "W4-02": "#5878 (browser capture)",
            "W4-04": "#5878 (browser capture)",
        },
    )
    reports = wave_reports() if reports is None else reports
    return _index.compile_index(
        acceptance_ids=_mod.all_acceptance_ids(),
        criteria_sources=criteria_sources,
        report_sources=report_sources,
        artifacts=artifacts,
        prior_report=prior_report(config) if report is None else report,
        bundle_revision=bundle_revision,
        compiled_revision=FRONTEND_REVISION,
        compiled_at=relative_time(0),
        prior_waves=(1, 2, 3),
        read_result=lambda wave: reports[wave],
        fixture_identity={
            "account_id": config["account_id"],
            "environment": config["environment"],
            "run_id": config["live_run_id"],
        },
    )


@dataclass
class CollectedRun:
    """One full collection, as it reaches the evaluator.

    `results` are the real `CheckResult`s, `refusals` is what the collectors could not
    measure, and `paths` is where each artifact was written — so a test can assert both
    the verdict and the reason it was reached.
    """

    results: dict
    refusals: dict
    paths: dict
    config: dict
    report: dict


def collect_and_evaluate(
    tmp_path: Path,
    *,
    preflight_kwargs: dict | None = None,
    documents: dict | None = None,
    index_kwargs: dict | None = None,
    capture=None,
    config: dict | None = None,
    specs=None,
    graph: CommitGraph | None = None,
    git_runner=None,
) -> CollectedRun:
    """Collect every wave-4 artifact for real, write it to disk, and evaluate it.

    This is the entrypoint the kickoff's requirement names. The chain is:

        controlled transport → operator-wave4 collector → Artifact.build()
            → a real JSON file on disk → ArtifactStore → Driver → CheckResult

    Nothing in the middle is stubbed. In particular `Artifact.build()` is what writes
    the file, so a refused measurement reaches the evaluator as an ABSENT KEY — which
    is the property that makes a collection gap a not_run instead of a finding about
    the deployment, and it cannot be tested any other way than by running both halves.

    The browser capture is the one artifact NOT produced here: it is #5878's producer
    and this repository consumes it as a contract. Passing it as a payload is therefore
    correct rather than a shortcut — a competing capture implementation is explicitly
    out of this issue's ownership.
    """
    config = live_config(tmp_path) if config is None else config
    graph = graph if graph is not None else w4_commit_graph()
    artifacts: dict = {}
    refusals: dict = {}
    paths: dict = {}

    preflight, graph = collect_preflight(
        tmp_path, config=config, graph=graph, **(preflight_kwargs or {})
    )
    artifacts["wave4_preflight"] = preflight.build()
    refusals["wave4_preflight"] = preflight.refusals

    documents = documents or {}
    for check_id, spec in _consolidated.CONSOLIDATED_ARTIFACTS.items():
        artifact = collect_consolidated_artifact(
            check_id, config=config, document=documents.get(check_id)
        )
        artifacts[spec["artifact"]] = artifact.build()
        refusals[spec["artifact"]] = artifact.refusals

    index, index_refusals = collect_index(
        config=config, artifacts=artifacts, **(index_kwargs or {})
    )
    artifacts["wave4_evidence_index"] = index.build()
    refusals["wave4_evidence_index"] = {**index.refusals, **index_refusals}

    # The browser capture, consumed from #5878's producer rather than made here.
    artifacts["browser_control_run"] = (
        browser_control_run_payload(bundle_revision=FRONTEND_REVISION)
        if capture is None
        else capture
    )
    # Wave 1 and wave 2's artifacts come along because the wave-4 config declares the
    # full set; the wave-4 checks do not read them, but a config that declared only
    # wave 4's would not be the config an operator runs.
    payloads = {**artifact_payloads(), **artifacts}
    payloads.pop("browser_control_run", None)
    payloads["browser_control_run"] = artifacts["browser_control_run"]

    for name, payload in payloads.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        paths[name] = path
    config = dict(config)
    config["artifacts"] = {name: f"{name}.json" for name in payloads}

    probe = _mod.Probe(config["gateway_url"], gateway_stub())
    store = _mod.ArtifactStore(tmp_path, config["artifacts"])
    driver = _mod.Driver(
        config,
        probe,
        store,
        dynamodb=ddb_stub(),
        git_runner=git_runner if git_runner is not None else staleness_runner(graph),
    )
    selected = _mod.WAVE4_CHECKS if specs is None else specs
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        results = _mod.run_checks(
            driver,
            selected,
            manifest_ids=tuple(spec.check_id for spec in _mod.WAVE4_CHECKS),
        )
    report = _mod.build_report(
        config,
        results,
        cleanup_ok=True,
        wave=4,
        expected_ids=tuple(spec.check_id for spec in _mod.WAVE4_CHECKS),
    )
    return CollectedRun(
        results={result.check_id: result for result in results},
        refusals=refusals,
        paths=paths,
        config=config,
        report=report,
    )


# A wave-3 manifest, used ONLY to model the revision this one becomes after #3969
# lands. It is patched in by `with_wave_three` and is never registered in
# `agent-control-eval.py`: wave 3's twelve checks are ADP developer #3969's namespace,
# and inventing them here would both trespass on that ownership and — much worse —
# make `--wave 3` report a pass on a manifest nobody delivered.
#
# Twelve rows because #3969's driver has twelve; the acceptance IDs are deliberately
# EMPTY, so `all_acceptance_ids()` still returns exactly 37. That is the property being
# protected: wave 4's consolidation is over the criteria the waves declare, and a
# stand-in manifest that contributed IDs would change the number this evaluation is
# counting.
WAVE3_STANDIN: tuple = tuple(
    _mod.CheckSpec(f"W3-{index:02d}", (), f"wave 3 check {index} (owned by #3969)")
    for index in range(1, 13)
)


def with_wave_three():
    """Patch a wave-3 manifest and evaluation in, as the merged revision will have.

    Wave 4's row closes all four evaluations, so with wave 3 unregistered the honest
    outcome is `not_run` — and the tests above assert exactly that. But an evaluator
    that could ONLY ever report not_run would be untestable in its passing direction,
    and "this check can never pass" is its own kind of broken: it makes the check
    indistinguishable from one that is simply missing.

    So this models the post-#3969 revision for the tests that need a full report. It
    patches the two registries the prerequisite is read from and nothing else, which
    means every other assertion in those tests is still made by the real code.
    """
    return (
        patch.dict(_mod.WAVE_CHECKS, {3: WAVE3_STANDIN}, clear=False),
        patch.dict(_mod.WAVE_EVALUATIONS, {3: "3969"}, clear=False),
    )


def collect_with_wave_three(tmp_path: Path, **kwargs) -> CollectedRun:
    """`collect_and_evaluate` on the revision where wave 3 is registered."""
    manifest, evaluations = with_wave_three()
    with manifest, evaluations:
        return collect_and_evaluate(tmp_path, **kwargs)


class TestTheCollectorToEvaluatorPathIsReal:
    """The producer exists, and what it produces is what the evaluator reads.

    This is the kickoff's "a dictionary of booleans or a fixture example is not a
    producer" requirement, asserted rather than asserted-about: the artifacts these
    tests evaluate were built by `operator-wave4`'s collectors from injected
    transports, written to disk by `Artifact.build()`, and read back by the real
    `ArtifactStore`.
    """

    def test_a_complete_collection_passes_every_wave_four_check(self, tmp_path):
        """The positive observation, without which no counterexample means anything.

        If a correct collection could not produce a passing report, then every failure
        below would be ambiguous between "the counterexample was caught" and "nothing
        can ever pass".
        """
        run = collect_with_wave_three(tmp_path)
        assert {cid: r.status for cid, r in run.results.items()} == {
            spec.check_id: _mod.STATUS_PASSED for spec in _mod.WAVE4_CHECKS
        }
        assert run.report["passed"] == run.report["required"] == 10
        assert run.report["not_run"] == 0
        assert _mod.report_is_passing(run.report)

    def test_nothing_was_measured_by_default(self, tmp_path):
        """No collector filled a gap: a complete collection has no refusals at all."""
        run = collect_with_wave_three(tmp_path)
        assert {name: gaps for name, gaps in run.refusals.items() if gaps} == {}

    def test_wave_three_unregistered_keeps_the_wave_incomplete(self, tmp_path):
        """The honest state of THIS revision, reached through the real path.

        Not a contrived failure: the collection is complete and correct, the transports
        all answer, and wave 4 is still incomplete because wave 3's manifest does not
        exist here. The kickoff's "missing prerequisite acceptance ... must remain NOT
        RUN/failure and nonzero" is this case.
        """
        run = collect_and_evaluate(tmp_path)
        assert run.results["W4-01"].status == _mod.STATUS_NOT_RUN
        assert run.results["W4-10"].status == _mod.STATUS_NOT_RUN
        assert "wave 3" in run.results["W4-01"].message
        assert not _mod.report_is_passing(run.report)

    def test_the_artifacts_the_evaluator_read_are_the_files_the_collector_wrote(
        self, tmp_path
    ):
        """No payload was injected past the file boundary.

        The serialization round trip is part of what is under test: `Artifact.build()`
        omits refused fields, and "omitted" only means anything if the evaluator is
        reading the written file rather than an in-memory dict.
        """
        run = collect_with_wave_three(tmp_path)
        for name in _mod.REQUIRED_ARTIFACT_KEYS:
            if not name.startswith("wave4_"):
                continue
            payload = json.loads(run.paths[name].read_text(encoding="utf-8"))
            absent = [
                key for key in _mod.REQUIRED_ARTIFACT_KEYS[name] if key not in payload
            ]
            assert absent == [], (name, absent)



class TestMissingPriorAcceptanceCannotBeConsolidated:
    """Wave 4's row closes all four evaluations, so an unaccepted wave stops it.

    Every case here is expressed by changing what an earlier wave's OWN report says —
    the transport `measure_prior_wave` reads — rather than by editing the acceptance
    record inside the artifact. That is the distinction that matters: the collector
    summarises a report it did not write, and the summary has to carry a partial
    acceptance through as partial.
    """

    def test_a_nine_of_ten_prior_wave_is_not_an_acceptance(self, tmp_path):
        """The case re-typing evidence by hand would erase.

        A wave that reported 9/10 is a wave with an open criterion. `accepted` is
        derived from the counts the report itself carries, so there is no step at which
        an operator's "wave 2 is done" could enter the record.
        """
        short = reports_with(2, passed=len(_mod.WAVE_CHECKS[2]) - 1)
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"reports": short}, index_kwargs={"reports": short}
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert "wave 2" in run.results["W4-01"].message
        assert not _mod.report_is_passing(run.report)

    def test_a_prior_wave_that_left_its_fixture_enabled_is_not_a_baseline(self, tmp_path):
        """DP-INV-1: an accepted wave whose cleanup failed left the flag on.

        Its environment is not a usable baseline for a later wave's observations, so
        the acceptance does not carry forward even though every check passed.
        """
        dirty = reports_with(2, fixture_cleanup={"ok": False})
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"reports": dirty}, index_kwargs={"reports": dirty}
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert not _mod.report_is_passing(run.report)

    def test_a_report_recording_no_cleanup_outcome_is_refused_not_defaulted(self, tmp_path):
        """A missing cleanup outcome is a gap, not a success.

        The collector refuses wave 2's whole record rather than defaulting
        `cleanup_ok`, so the acceptance arrives absent and W4-01 names the wave. A
        collector that defaulted it would have the evaluator accepting a wave nobody
        confirmed cleanup for — "we did not look" recorded as "it was fine".

        The second assertion is about the OPERATOR's half. `prior_waves` is emitted as a
        partial map on purpose (the evaluator names the missing wave, which beats
        omitting the field), and that used to make the refused entry's reason vanish —
        leaving "no acceptance recorded for wave 2" with nothing saying why. The reason
        is the actionable content, so it is reported per-entry.
        """
        silent = wave_report(2)
        silent.pop("fixture_cleanup")
        reports = wave_reports(**{"2": silent})
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"reports": reports}, index_kwargs={"reports": reports}
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert "wave 2" in run.results["W4-01"].message
        assert "cleanup" in run.refusals["wave4_preflight"]["prior_waves[2]"]

    def test_an_absent_prior_report_is_a_named_refusal(self, tmp_path):
        """The collection gap, which must not read as a finding about the deployment."""
        absent = reports_without(2)
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"reports": absent}, index_kwargs={"reports": absent}
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}
        assert "wave 2" in run.results["W4-01"].message

    def test_a_prior_wave_accepted_on_an_uncontained_revision_is_stale(self, tmp_path):
        """Accepted, and about a build this one no longer contains.

        The subtlest of the four: wave 2's report is a true record of a real
        acceptance, and it still cannot carry forward, because the revision it was
        accepted on is not an ancestor of what is deployed. Expressed by handing over a
        revision the commit graph does not place under the deployment — so the answer
        comes from `git merge-base --is-ancestor` rather than from a recorded
        `compatible_with_current_revision`, which is the conclusion the check exists to
        reach.
        """
        orphan = "f" * 40
        graph = w4_commit_graph(**{orphan: ()})
        stale = reports_with(2, revision=orphan)
        run = collect_with_wave_three(
            tmp_path,
            graph=graph,
            git_runner=staleness_runner(graph),
            preflight_kwargs={"reports": stale},
            index_kwargs={"reports": stale},
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert "ancestor" in run.results["W4-01"].message


class TestMismatchedRunGenerationSourceAndAssets:
    """Every "which thing did we observe" mismatch, each from a broken transport.

    These are the cases where each individual record is internally consistent and the
    records disagree with each other. No single artifact is wrong, which is why the
    evaluator has to compare them rather than validate them.
    """

    def test_an_unreachable_spa_route_refuses_the_asset_evidence(self, tmp_path):
        """A 503 is a collection failure, not an asset mismatch.

        The `else False` this whole package exists to prevent: `verify()` failing and
        `verify()` never running must not produce the same emitted value. So the served
        assets are refused, the key is absent, and W4-01 reports a missing measurement
        rather than "the deployment serves the wrong bundle".
        """
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"fetch": spa_transport(status=503, body="unavailable")}
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}
        assert run.refusals["wave4_preflight"], "a 503 must leave a named refusal"

    def test_served_assets_that_disagree_with_the_build_fail(self, tmp_path):
        """A deployment serving a bundle this revision did not build.

        The comparison is a fingerprint rather than a name check, which is what makes
        it catch the real case: a CloudFront cache still serving the previous build's
        content-hashed files while every revision field says the new one.
        """
        stale_assets = ("index-00000000.js", "index-11111111.css")
        run = collect_with_wave_three(
            tmp_path,
            preflight_kwargs={
                "fetch": spa_transport(stale_assets),
                # `dist_listing` is what the build produced; `fetch` is what is served.
                "assets": FRONTEND_ASSETS,
            },
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}

    def test_a_gate_whose_named_job_failed_is_not_a_passing_gate(self, tmp_path):
        """Parsed at the real field location, so a green summary cannot cover a red job.

        `gh run view` reports a top-level `conclusion` AND a per-job one. A workflow
        can conclude `success` while the job that IS the gate did not run or failed,
        and reading only the top level is how that passes unnoticed.
        """
        gate = _mod.WAVE4_REQUIRED_CI_GATES[0]
        broken = run_lookup_for()(gate)
        broken = {**broken, "jobs": [{"name": gate, "conclusion": "failure"}]}
        run = collect_with_wave_three(
            tmp_path, preflight_kwargs={"run_lookup": run_lookup_for(**{gate: broken})}
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}

    def test_a_gate_tested_on_code_the_deployment_lacks_fails(self, tmp_path):
        """A gate that passed, on a commit the served bundle does not contain."""
        gate = _mod.WAVE4_REQUIRED_CI_GATES[0]
        orphan = "f" * 40
        graph = w4_commit_graph(**{orphan: ()})
        elsewhere = {**run_lookup_for()(gate), "headSha": orphan}
        run = collect_with_wave_three(
            tmp_path,
            graph=graph,
            git_runner=staleness_runner(graph),
            preflight_kwargs={"run_lookup": run_lookup_for(**{gate: elsewhere})},
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert gate in run.results["W4-01"].message

    def test_a_capture_of_a_different_bundle_is_not_evidence_about_this_one(self, tmp_path):
        """The browser drove one build and the preflight describes another.

        Not reconcilable in the preflight's favour: every wave-4 browser observation is
        about whichever bundle the browser actually loaded.
        """
        run = collect_with_wave_three(
            tmp_path, capture=browser_control_run_payload(bundle_revision="f" * 40)
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED
        assert "bundle" in run.results["W4-01"].message

    def test_an_aborted_row_read_for_a_different_run_is_rejected(self, tmp_path):
        """A terminal row is only evidence if it is THIS run's row.

        W4-05 demands the actual row, and a read that returned a different run's row
        would demonstrate that some run somewhere reached a terminal state.
        """
        config = live_config(tmp_path)
        artifact = collect_consolidated_artifact(
            "W4-05",
            config=config,
            extra=w4_extra_fields(
                "W4-05", config=config, row=aborted_row(run_id="msg-someone-else")
            ),
        )
        observed = artifact.build()["completed_at_observed"]
        assert observed["run_id"] == "msg-someone-else", (
            "the collector must report the row it actually read, not the one it wanted"
        )

    def test_a_row_that_never_reached_terminal_is_reported_as_it_was_read(self, tmp_path):
        """A recorded non-terminal status is EMITTED, not refused.

        The transcription asymmetry, at the row level: refusing it would omit the key,
        the evaluator would report not_run, and "we looked and it is still running"
        would have become "we did not look" — a softer report of a worse fact.
        """
        config = live_config(tmp_path)
        artifact = collect_consolidated_artifact(
            "W4-05",
            config=config,
            extra=w4_extra_fields("W4-05", config=config, row=aborted_row(status="running")),
        )
        assert artifact.build()["completed_at_observed"]["status"] == "running"

    def test_a_row_missing_completed_at_is_refused_rather_than_completed(self, tmp_path):
        """An absent field in the row read is a gap, and a gap is a refusal."""
        config = live_config(tmp_path)
        artifact = collect_consolidated_artifact(
            "W4-05",
            config=config,
            extra=w4_extra_fields("W4-05", config=config, row=aborted_row(completed_at="")),
        )
        assert "completed_at_observed" not in artifact.build()
        assert "completed_at" in artifact.refusals["completed_at_observed"]


class TestStaleAndTouchedEvidenceMustBeRerun:
    """Consolidated evidence describes the code it was taken against.

    If a covered surface changed after the evidence was taken, the evidence describes
    code that is no longer deployed — a true observation of a build nobody runs. The
    kickoff's "rerun requirements from observations, not from operator-supplied success
    claims" is this: staleness is computed from the commit graph, and the answer is
    never read out of the artifact.
    """

    def test_a_surface_modified_after_the_evidence_fails_that_check_alone(self, tmp_path):
        """One touched surface, one failing check — and the others unaffected.

        Precision is the property under test. A staleness check that failed the whole
        wave would tell an operator to rerun everything; this one names the surface and
        the check whose evidence it invalidates.
        """
        surface = _mod.WAVE4_CONSOLIDATED_SOURCES["W4-06"]["surfaces"][0]
        graph = w4_commit_graph()
        run = collect_with_wave_three(
            tmp_path,
            graph=graph,
            git_runner=staleness_runner(graph, touched={surface: relative_time(600)}),
        )
        assert run.results["W4-06"].status == _mod.STATUS_FAILED
        assert surface in run.results["W4-06"].message

    def test_unanswerable_staleness_is_not_run_rather_than_not_stale(self, tmp_path):
        """A shallow clone cannot answer, and must not be read as "nothing changed".

        This is the case a boolean would collapse. "The surface was not modified after
        the evidence" and "we could not determine when the surface was modified" have
        opposite consequences, and only the second is the harness's own problem.
        """
        graph = w4_commit_graph()
        run = collect_with_wave_three(
            tmp_path, graph=graph, git_runner=staleness_runner(graph, answerable=False)
        )
        for check_id in ("W4-03", "W4-05", "W4-06", "W4-09"):
            assert run.results[check_id].status == _mod.STATUS_NOT_RUN, check_id
        assert not _mod.report_is_passing(run.report)

    def test_evidence_taken_at_an_uncontained_revision_fails(self, tmp_path):
        """Consolidated evidence about a build the deployment does not contain."""
        orphan = "f" * 40
        graph = w4_commit_graph(**{orphan: ()})
        run = collect_with_wave_three(
            tmp_path,
            graph=graph,
            git_runner=staleness_runner(graph),
            documents={"W4-06": evidence_document("W4-06", evidenced_revision=orphan)},
        )
        assert run.results["W4-06"].status == _mod.STATUS_FAILED

    def test_evidence_without_an_instant_makes_staleness_unanswerable(self, tmp_path):
        """No `evidenced_at` means no ordering, so currency cannot be established.

        The collector refuses the field rather than stamping "now" — a timestamp the
        collector invented would say the evidence postdates every surface, which is
        precisely the thing nobody observed.

        `failed`, not `not_run`, and the distinction is the evaluator's deliberate one:
        an ABSENT artifact is not_run (nobody recorded this observation) while an
        artifact PRESENT but missing a required key is failed ("an incomplete artifact
        is a claim without its evidence"). Refusing a field is therefore not a route to
        a softer verdict, and it is still correct — what it buys is that an unmeasured
        field is never reported as a measured one.
        """
        run = collect_with_wave_three(
            tmp_path, documents={"W4-03": evidence_document("W4-03", evidenced_at="")}
        )
        assert run.results["W4-03"].status == _mod.STATUS_FAILED
        assert "evidenced_at" in run.results["W4-03"].message
        assert "evidenced_at" in run.refusals["wave4_steering_evidence"]

    def test_a_recorded_false_proof_is_distinguishable_from_an_absent_one(self, tmp_path):
        """The transcription asymmetry, stated as a test.

        A proof recorded as `False` is emitted verbatim; an ABSENT proof is refused and
        the key is omitted. Both fail — the evaluator treats a present-but-incomplete
        artifact as a claim without its evidence — but they fail with DIFFERENT
        messages, and that is the whole point:

        * `'fifo_order_proven' is False, expected an observed True` — we looked, and the
          property does not hold. Someone has a bug to fix.
        * `missing required keys ['fifo_order_proven']` — we did not look. Someone has a
          measurement to take.

        Collapsing them would send an operator to the wrong one of those two places.
        """
        observed_false = collect_with_wave_three(
            tmp_path, documents={"W4-03": evidence_document("W4-03", fifo_order_proven=False)}
        )
        assert observed_false.results["W4-03"].status == _mod.STATUS_FAILED
        assert "is False" in observed_false.results["W4-03"].message
        # Emitted, so nothing was refused: the collector measured it and it was false.
        assert "fifo_order_proven" not in observed_false.refusals["wave4_steering_evidence"]

        document = evidence_document("W4-03")
        document.pop("fifo_order_proven")
        unmeasured = collect_with_wave_three(tmp_path, documents={"W4-03": document})
        assert unmeasured.results["W4-03"].status == _mod.STATUS_FAILED
        assert "missing required keys" in unmeasured.results["W4-03"].message
        assert "records no 'fifo_order_proven'" in unmeasured.refusals[
            "wave4_steering_evidence"
        ]["fifo_order_proven"]

    def test_an_unreadable_source_document_names_one_cause(self, tmp_path):
        """Four gaps from one problem must not read as four problems."""

        def unreadable(wave):
            raise FileNotFoundError("wave3/steering-evidence.json: no such file")

        artifact = _consolidated.collect_consolidated(
            "W4-03",
            read_evidence=unreadable,
            acceptance_ids=_mod.CHECK_ACCEPTANCE_IDS["W4-03"],
            proofs=W4_CONSOLIDATED_PROOFS["W4-03"],
            fixture_identity={"account_id": "1", "environment": "dev", "run_id": "r"},
        )
        reasons = set(artifact.refusals.values())
        assert len(reasons) == 1, f"one cause, one message: {reasons}"
        assert "no such file" in reasons.pop()


class TestWrongIdentityOrDestinationIsNotEvidence:
    """Who made the observation, and where the request went.

    Both are properties of the observation rather than of the system, and both are ways
    a capture can be entirely truthful about a run that does not demonstrate what the
    criterion needs. The browser capture is #5878's producer — consumed here as a
    contract, never reimplemented — so these break the CAPTURE's recorded facts and the
    gateway's identity answer, which are the real inputs.
    """

    def test_a_capture_taken_as_a_nonowner_cannot_evidence_the_owner_path(self, tmp_path):
        """The identity the preflight recorded has to be the one the row needs."""
        run = collect_with_wave_three(
            tmp_path,
            preflight_kwargs={"identity": {"role": "nonowner", "is_run_owner": False}},
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}

    def test_an_unanswerable_identity_is_refused_rather_than_assumed_owner(self, tmp_path):
        """A gateway that could not say who we are has not said we are the owner."""
        run = collect_with_wave_three(
            tmp_path,
            preflight_kwargs={
                "identity": _collector.Refused("GET /auth/whoami returned 500")
            },
        )
        assert run.results["W4-01"].status in {_mod.STATUS_FAILED, _mod.STATUS_NOT_RUN}
        assert "browser_identity" in run.refusals["wave4_preflight"]

    def test_a_request_leaving_the_control_surface_fails(self, tmp_path):
        """A control command that went somewhere other than the gateway's activity API.

        The destination is the security property: a dashboard that reached a pod
        directly would work perfectly and would have bypassed the authorization the
        whole control path exists to impose.
        """
        run = collect_with_wave_three(
            tmp_path,
            capture=browser_control_run_payload(
                bundle_revision=FRONTEND_REVISION,
                request_destinations=["http://10.0.4.17:8080/agent/state"],
            ),
        )
        assert run.results["W4-06"].status == _mod.STATUS_FAILED
        assert "AC-S1" in run.results["W4-06"].message
        assert "10.0.4.17" in run.results["W4-06"].message

    def test_a_capture_carrying_a_pod_address_fails_on_privacy(self, tmp_path):
        """Existing browser privacy requirements, preserved rather than relaxed.

        A capture that recorded an internal address in a request body is evidence of a
        leak, and it stays a failure — this issue does not get to weaken it to
        accommodate a producer that finds it inconvenient.
        """
        run = collect_with_wave_three(
            tmp_path,
            capture=browser_control_run_payload(
                bundle_revision=FRONTEND_REVISION,
                request_bodies_contain_pod_address=True,
            ),
        )
        assert run.results["W4-06"].status == _mod.STATUS_FAILED
        assert "pod address" in run.results["W4-06"].message

    def test_ordinary_flags_left_on_is_a_failure_not_a_footnote(self, tmp_path):
        """The kickoff's own constraint, as a predicate: ordinary flags stay off.

        A wave-4 observation taken with the ordinary-user flag enabled was taken in an
        environment the evaluation forbids, whatever it went on to observe.
        """
        run = collect_with_wave_three(
            tmp_path,
            preflight_kwargs={
                "flags": {"ordinary_users_gated": True, "ordinary_flags_off": False}
            },
        )
        assert run.results["W4-01"].status == _mod.STATUS_FAILED


class TestTheIndexCannotBeAuthoredComplete:
    """W4-10's index is the most forgeable artifact in the evaluation.

    37 rows of `{"status": "passed"}` satisfies every structural check about SHAPE, so
    the compiler is built to be unable to type one: every row is DERIVED from a
    collected artifact's own criteria map or from a prior evaluator verdict, and a
    criterion with neither source gets no row at all.
    """

    def test_a_criterion_absent_from_its_source_gets_no_row(self, tmp_path):
        """The central property: a gap in the evidence is a gap in the index.

        Expressed by dropping one criterion from the SOURCE DOCUMENT, so the absence
        propagates through the real compiler. The index comes out one row short, the
        refusal names which and why, and W4-10 fails naming the same ID — three
        independent places agreeing, none of them authored.
        """
        dropped = _mod.CHECK_ACCEPTANCE_IDS["W4-06"][0]
        document = evidence_document("W4-06")
        del document["criteria"][dropped]
        run = collect_with_wave_three(tmp_path, documents={"W4-06": document})

        assert dropped in run.refusals["wave4_evidence_index"]
        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert dropped not in index["criteria"]
        assert run.results["W4-10"].status == _mod.STATUS_FAILED
        assert dropped in run.results["W4-10"].message

    def test_a_status_with_no_evidence_behind_it_is_refused(self, tmp_path):
        """"Passed" with nothing to retrieve is the substitute for evidence.

        Refused TWICE, at two independent points, and the messages differ because the
        two refusals are about different things:

        * the consolidating collector drops the entry while transcribing, because an
          entry with no evidence reference is not a transcribable observation;
        * the index compiler then finds no entry to derive a row from.

        The second is the one asserted on the index's refusal map. That the first
        happened first is why its wording is "has no entry" rather than "no evidence":
        the empty-evidence entry never made it as far as the compiler, which is the
        stronger outcome — the claim was refused at the earliest point that could see it.
        """
        target = _mod.CHECK_ACCEPTANCE_IDS["W4-06"][0]
        document = evidence_document("W4-06")
        document["criteria"][target] = {"status": _mod.STATUS_PASSED, "evidence": []}
        run = collect_with_wave_three(tmp_path, documents={"W4-06": document})

        matrix = json.loads(run.paths["wave4_security_matrix"].read_text(encoding="utf-8"))
        assert target not in matrix["criteria"], (
            "an entry with no evidence behind it is not a transcribable observation"
        )
        assert "has no entry for it" in run.refusals["wave4_evidence_index"][target]
        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert target not in index["criteria"]
        assert run.results["W4-06"].status == _mod.STATUS_FAILED
        assert run.results["W4-10"].status == _mod.STATUS_FAILED

    def test_a_failed_source_status_is_carried_through_not_upgraded(self, tmp_path):
        """The one thing the compiler must never do is write `passed` over a `failed`.

        Emitted WITH its real status rather than omitted, because an omitted row reads
        as "no entry" — true, but less useful than "this criterion is failing".
        """
        target = _mod.CHECK_ACCEPTANCE_IDS["W4-06"][0]
        document = evidence_document("W4-06")
        document["criteria"][target]["status"] = _mod.STATUS_FAILED
        run = collect_with_wave_three(tmp_path, documents={"W4-06": document})

        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert index["criteria"][target]["status"] == _mod.STATUS_FAILED
        assert run.results["W4-10"].status == _mod.STATUS_FAILED
        assert target in run.results["W4-10"].message

    def test_a_mocked_observation_cannot_satisfy_a_live_row(self, tmp_path):
        """A unit mock reproducing the same shape proves the double behaves as written.

        `live` is read from the source, never inferred, which is what makes this
        checkable at all: a compiler that defaulted it would answer the question W4-10
        turns on, on the artifact's behalf.
        """
        target = next(
            ac
            for ac in _mod.CHECK_ACCEPTANCE_IDS["W4-06"]
            if ac in _mod.LIVE_EVIDENCE_REQUIRED_IDS
        )
        document = evidence_document("W4-06")
        document["criteria"][target]["live"] = False
        run = collect_with_wave_three(tmp_path, documents={"W4-06": document})
        assert run.results["W4-10"].status == _mod.STATUS_FAILED
        assert "non-live" in run.results["W4-10"].message

    def test_an_unrecorded_liveness_is_refused_rather_than_defaulted(self, tmp_path):
        """Not recorded is not the same as not live, and neither is it live."""
        target = _mod.CHECK_ACCEPTANCE_IDS["W4-06"][0]
        document = evidence_document("W4-06")
        document["criteria"][target].pop("live")
        run = collect_with_wave_three(tmp_path, documents={"W4-06": document})
        assert "live" in run.refusals["wave4_evidence_index"][target]
        assert target not in json.loads(
            run.paths["wave4_evidence_index"].read_text(encoding="utf-8")
        )["criteria"]

    def test_a_verdict_cannot_be_borrowed_for_a_criterion_its_check_does_not_carry(
        self, tmp_path
    ):
        """A passing check may only evidence the criteria it itself declares.

        Without this, any green check could lend its status to whatever AC the caller
        pointed at it — which is how one passing browser capture would come to evidence
        thirty-seven criteria.
        """
        config = live_config(tmp_path)
        source = _index.ReportSource(
            check_id="W4-02", owner="#5878", acceptance_ids=("AC-A1",)
        )
        row = _index._row_from_report(
            source,
            "AC-A1",
            prior_report(config),
            bundle_revision=FRONTEND_REVISION,
        )
        assert _collector.is_refused(row)
        assert "do not include it" in row.reason

    def test_a_report_backed_row_needs_the_bundle_it_was_captured_against(self, tmp_path):
        """No bundle revision means the verdict's currency cannot be established."""
        config = live_config(tmp_path)
        artifact, refusals = collect_index(
            config=config,
            artifacts={},
            bundle_revision=_collector.Refused("the capture records no bundle_revision"),
        )
        for acceptance_id in _mod.CHECK_ACCEPTANCE_IDS["W4-02"]:
            assert acceptance_id in refusals
            assert "bundle_revision" in refusals[acceptance_id]
        assert "criteria" not in artifact.build()

    def test_an_index_deriving_nothing_is_refused_whole(self, tmp_path):
        """An empty index is a broken collection, and says so.

        Emitting `criteria: {}` would satisfy the key-presence check and then fail the
        coverage check with 37 missing IDs — the right outcome by a route that reads as
        "the index is broken" rather than "no evidence was collected".
        """
        config = live_config(tmp_path)
        artifact, refusals = collect_index(config=config, artifacts={}, report={})
        assert "criteria" not in artifact.build()
        assert "not one of the 37" in artifact.refusals["criteria"]
        # And every criterion is individually accounted for, so "the index is empty"
        # never has to be inferred from the absence of rows.
        assert set(refusals) >= set(_mod.all_acceptance_ids())

    def test_no_criterion_is_claimed_by_two_sources(self, tmp_path):
        """Two rows for one criterion let a pass and a fail coexist.

        Asserted on the real manifests rather than on a constructed overlap, because
        this is the property that has to keep holding as the manifests change: if a
        future wave gave one AC to both a consolidated artifact and a browser-backed
        check, the compiler records the conflict instead of silently picking.
        """
        consolidated_ids = [
            acceptance_id
            for check_id in _consolidated.CONSOLIDATED_ARTIFACTS
            for acceptance_id in _mod.CHECK_ACCEPTANCE_IDS[check_id]
        ]
        report_ids = [
            acceptance_id
            for check_id in ("W4-02", "W4-04")
            for acceptance_id in _mod.CHECK_ACCEPTANCE_IDS[check_id]
        ]
        every = consolidated_ids + report_ids
        assert len(every) == len(set(every)), sorted(
            {ac for ac in every if every.count(ac) > 1}
        )

    def test_the_index_covers_exactly_the_thirty_seven_and_no_more(self, tmp_path):
        """The count is computed from the manifests at both ends.

        Neither the compiler nor the check carries a literal 37 it could be adjusted to
        match, which is what stops "make the numbers agree" from being a valid fix.
        """
        run = collect_with_wave_three(tmp_path)
        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert set(index["criteria"]) == set(_mod.all_acceptance_ids())
        assert len(index["criteria"]) == _mod.WAVE4_TOTAL_ACCEPTANCE_IDS


class TestAFabricatedFullReportIsUnreachable:
    """The kickoff's hardest requirement: a full 10/10 that cannot hide any of the 37.

    Every check above could pass on a perfectly-compiled index inside a run that
    reported not_runs, and that combination is precisely the fabricated pass. W4-10
    asserts against THIS RUN's own results rather than against the index's
    self-description, which is what makes the two impossible to separate.
    """

    def test_a_complete_index_cannot_certify_a_run_with_not_runs(self, tmp_path):
        """A hand-authored complete index inside an incomplete run is rejected.

        The index here is the REAL one, compiled from real evidence and genuinely
        complete — so this is the strongest form of the case: not a forgery, a correct
        index presented as a wave it does not complete. Wave 3 is unregistered, so the
        criteria are not in the consolidated set, and W4-10 refuses on that rather than
        on anything about the index.
        """
        run = collect_and_evaluate(tmp_path)  # no wave-3 manifest in this revision
        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert set(index["criteria"]) == set(_mod.all_acceptance_ids()), (
            "the index is genuinely complete; the run is not"
        )
        assert run.results["W4-10"].status == _mod.STATUS_NOT_RUN
        assert run.report["not_run"] > 0
        assert not _mod.report_is_passing(run.report)

    def test_running_a_partition_of_the_wave_is_not_a_short_inventory(self, tmp_path):
        """The distinction the inventory check rests on, asserted so it stays true.

        `main` runs wave 4 in two partitions — the checks before fixture cleanup and the
        ones after — and passes the FULL manifest as `manifest_ids` to both. So a
        selection shorter than the wave is normal operation, and W4-10 must not fail on
        it: a self-completeness check comparing against its own partition would always
        agree with itself, which is the bug this separation avoids.

        This test exists to keep the next test honest. Without it, "a short inventory
        fails" could be satisfied by a check that fails on any partial RUN, which would
        break the real two-phase invocation.
        """
        selected = tuple(
            spec for spec in _mod.WAVE4_CHECKS if spec.check_id not in {"W4-07"}
        )
        run = collect_with_wave_three(tmp_path, specs=selected)
        assert run.results["W4-10"].status == _mod.STATUS_PASSED
        assert "W4-07" not in run.results

    def test_an_inventory_short_a_check_cannot_consolidate_the_wave(self, tmp_path):
        """A consolidation is not complete inside a report missing one of the ten.

        The forgeable version of the above: not a partition of a full run, but a run
        whose own inventory is short — a report that would present nine results as the
        wave. Every check in it passes, and W4-10 fails anyway, because what it asserts
        is the run's completeness rather than the index's description of it.
        """
        config = live_config(tmp_path)
        short = tuple(
            spec.check_id for spec in _mod.WAVE4_CHECKS if spec.check_id != "W4-07"
        )
        manifest, evaluations = with_wave_three()
        with manifest, evaluations:
            base = collect_and_evaluate(tmp_path, config=config)
            store = _mod.ArtifactStore(tmp_path, base.config["artifacts"])
            driver = _mod.Driver(
                base.config,
                _mod.Probe(base.config["gateway_url"], gateway_stub()),
                store,
                dynamodb=ddb_stub(),
                git_runner=staleness_runner(w4_commit_graph()),
            )
            with patch.dict("os.environ", IDENTITY_ENV, clear=False):
                results = _mod.run_checks(
                    driver, _mod.WAVE4_CHECKS, manifest_ids=short
                )
        verdicts = {result.check_id: result for result in results}
        assert verdicts["W4-10"].status == _mod.STATUS_FAILED
        assert "inventory" in verdicts["W4-10"].message

    def test_the_report_is_nonzero_whenever_any_check_is_not_run(self, tmp_path):
        """"Missing measurements ... must remain NOT RUN/failure and nonzero."

        Asserted through `report_is_passing`, the same predicate the exit code is
        derived from, so this is the exit status rather than a proxy for it.
        """
        graph = w4_commit_graph()
        run = collect_with_wave_three(
            tmp_path, graph=graph, git_runner=staleness_runner(graph, answerable=False)
        )
        assert run.report["not_run"] > 0
        assert not _mod.report_is_passing(run.report)

    def test_cleanup_is_part_of_the_verdict_not_a_postscript(self, tmp_path):
        """A wave whose fixture was left enabled has not completed.

        `build_report` takes the cleanup outcome, and a passing wave with failed cleanup
        is the DP-INV-1 state: the flag is still on. Partial cleanup keeps the report
        nonzero, which is the kickoff's "incomplete cleanup must remain ... nonzero".
        """
        run = collect_with_wave_three(tmp_path)
        assert _mod.report_is_passing(run.report)

        dirty = _mod.build_report(
            run.config,
            list(run.results.values()),
            cleanup_ok=False,
            wave=4,
            expected_ids=tuple(spec.check_id for spec in _mod.WAVE4_CHECKS),
        )
        assert dirty["passed"] == dirty["required"] == 10
        assert not _mod.report_is_passing(dirty), (
            "ten of ten checks passing does not complete a wave whose fixture is still enabled"
        )

    def test_every_one_of_the_thirty_seven_is_named_in_the_passing_report(self, tmp_path):
        """A full report accounts for each criterion individually.

        The point of "cannot hide any of the 37": the report does not summarise them as
        a count. Each ID appears against the check that carries it, so a reader can go
        from any single criterion to the evidence for it.
        """
        run = collect_with_wave_three(tmp_path)
        assert _mod.report_is_passing(run.report)
        reported = {
            acceptance_id
            for entry in run.report["checks"].values()
            for acceptance_id in entry.get("acceptance_ids", ())
            if acceptance_id not in _mod._NON_ACCEPTANCE_ROW_LABELS
        }
        index = json.loads(run.paths["wave4_evidence_index"].read_text(encoding="utf-8"))
        assert reported <= set(index["criteria"])
        assert set(_mod.all_acceptance_ids()) == set(index["criteria"])
