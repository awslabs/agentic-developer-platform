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

import importlib.util
import json
import re
import sys
from pathlib import Path
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
        """Waves 2-4 belong to later stories (§7).

        Asking for one must not emit a report with zero required checks, which
        would satisfy `.passed == .required` at 0 == 0 and read as a clean pass.
        """
        path = write_config(tmp_path, valid_config())

        assert _mod.main(["--wave", "3", "--config", str(path)]) == _mod.EXIT_CONFIG

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


    def test_a_partially_implemented_wave_two_cannot_exit_zero(self, tmp_path: Path):
        """The honesty guarantee that replaces wave 2's blanket refusal (#3964).

        S5 registers wave 2 so its four checks can actually run, which removes the
        `EXIT_CONFIG` that previously made `--wave 2` safe by making it impossible.
        What must survive that change is the property the refusal was protecting:
        wave 2 cannot report success while six of its ten checks have no predicate.

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
        # The remaining unowned checks are NOT RUN and say so — never skipped, which
        # some gates tolerate, and never passed. W2-03..W2-05 dropped off this list
        # when S2 (#3961) implemented them; W2-01 (wave preflight) and W2-10 (#3968's
        # cleanup and security recheck) are still nobody's delivered work, and they
        # are what keeps `--wave 2` unable to exit 0 on a partially implemented wave.
        for check_id in ("W2-01", "W2-10"):
            entry = report["checks"][check_id]
            assert entry["status"] == _mod.STATUS_NOT_RUN, check_id
            assert "not implemented in this revision" in entry["message"], check_id
            # §7: every evidence list is nonempty, including for a check that
            # could not run — its evidence is the reason.
            assert entry["evidence"], check_id


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
    }


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
            return reply(200, dict(state_body))
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


def ddb_stub(item: dict | None = None) -> MagicMock:
    client = dynamodb_with_schema(CORRECT_SCHEMA)
    client.get_item.return_value = {"Item": item} if item else {}
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

    def test_waves_one_and_two_are_carried_by_this_revision(self):
        """S1 delivered wave 1; S3 #3962 adds wave 2's manifest with W2-02.

        Was `== (1,)` when S1 was the only landed story. Updated rather than
        deleted, because the property it protects is unchanged: a wave reachable
        from the CLI must have a real manifest behind it, so `--wave 3` is still
        a refusal rather than an empty pass.
        """
        assert _mod.SUPPORTED_WAVES == (1, 2)
        assert 3 not in _mod.WAVE_CHECKS

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

        Updated when S2 (#3961) landed W2-03..W2-05: the point of this assertion is
        that W2-02 maps to AC-T7 and that every registered predicate resolves to a
        real method, not that S3 is the only story to have delivered one.
        """
        assert set(_mod.WAVE2_PREDICATES) == {
            "W2-02",  # S3 #3962 — AC-T7, this story's own
            "W2-03",  # S2 #3961
            "W2-04",  # S2 #3961
            "W2-05",  # S2 #3961
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
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
        """#3967 accepted wave 1 and is closed; #3968 owns wave 2.

        A wave-2 report labelled 3967 would attach evidence to a finished
        evaluation.
        """
        assert _mod.WAVE_EVALUATIONS[1] == "3967"
        assert _mod.WAVE_EVALUATIONS[2] == "3968"
        assert set(_mod.WAVE_EVALUATIONS) == set(_mod.WAVE_CHECKS)
        assert set(_mod.WAVE_REVISIONS) == set(_mod.WAVE_CHECKS)


    def test_waves_three_and_four_are_still_unregistered(self):
        """S4/S6 extend wave 3, S7 wave 4 (§7). Wave 2 is registered as of #3964."""
        assert _mod.SUPPORTED_WAVES == (1, 2)
        assert 3 not in _mod.WAVE_CHECKS
        assert 4 not in _mod.WAVE_CHECKS


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

        S2 (#3961) has since added W2-03..W2-05 for AC-P1/P2/P3/P5/P6, so the
        implemented set is asserted as S3's + S2's + S5's rather than S5's alone.
        The two IDs that remain unimplemented are named explicitly below, because
        "everything is implemented" and "everything is implemented except the two
        the wave preflight and #3968 own" are different states and only the second
        one is true.
        """
        assert set(_mod.WAVE2_PREDICATES) == {
            "W2-02",  # S3 #3962 — AC-T7
            "W2-03",  # S2 #3961 — AC-P1
            "W2-04",  # S2 #3961 — AC-P2
            "W2-05",  # S2 #3961 — AC-P3/P5/P6
            "W2-06",
            "W2-07",
            "W2-08",
            "W2-09",
        }
        for check_id, method_name in _mod.WAVE2_PREDICATES.items():
            assert hasattr(_mod.Driver, method_name), check_id


    def test_every_unimplemented_wave_two_check_names_its_owner(self):
        """A NOT RUN with no owner is a dead end for the operator reading it."""
        implemented = set(_mod.WAVE2_PREDICATES)
        all_ids = {spec.check_id for spec in _mod.WAVE2_CHECKS}

        assert set(_mod.PENDING_CHECK_OWNERS) == all_ids - implemented
        assert all(owner.strip() for owner in _mod.PENDING_CHECK_OWNERS.values())


class TestTheMirroredControlRuntimeConstants:
    """The harness mirrors three values from TypeScript. This is the seam.

    `CONTROL_PROTOCOL_VERSION`, `CLAUDE_ADAPTER_ID` and
    `EXPECTED_CLAUDE_SDK_VERSION` are copied rather than imported, deliberately:
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
    PACKAGE_JSON = REPO_ROOT / "modules" / "agent-factory" / "agent" / "package.json"

    def test_the_typescript_sources_exist(self):
        """Named deliverables of #3962. A rename must fail here, not silently pass."""
        assert self.RUNTIME.is_file()
        assert self.ADAPTER.is_file()

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

        ok, notes = _mod.run_cleanup(config, dynamodb)

        assert ok is True
        dynamodb.delete_item.assert_called_once_with(
            TableName="t", Key={"event_id": {"S": "e1"}, "arrived_at": {"S": "a1"}}
        )
        # A consistent read, because an eventually-consistent one can report an
        # item gone before it is.
        assert dynamodb.get_item.call_args.kwargs["ConsistentRead"] is True
        assert "confirms absence" in " ".join(notes)

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

        ok, notes = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e"}]}, dynamodb
        )

        assert ok is False
        dynamodb.delete_item.assert_not_called()
        assert "partial-key" in " ".join(notes)

    def test_a_surviving_item_is_a_cleanup_failure(self):
        dynamodb = ddb_stub(item={"event_id": {"S": "e"}})

        ok, notes = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e", "arrived_at": "a"}]},
            dynamodb,
        )

        assert ok is False
        assert "still present" in " ".join(notes)

    def test_a_delete_error_is_a_cleanup_failure(self):
        dynamodb = ddb_stub()
        dynamodb.delete_item.side_effect = RuntimeError("AccessDenied")

        ok, notes = _mod.run_cleanup(
            {"invocation_table": "t", "cleanup_items": [{"event_id": "e", "arrived_at": "a"}]},
            dynamodb,
        )

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
        """
        code, report = self._run(tmp_path, wave=2)

        assert code == _mod.EXIT_CHECKS_FAILED
        assert report["cleanup_ok"] is True
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

        Wave 3 has no manifest, so it must still refuse *before* loading a config
        and must write no evidence — an empty report for an unwritten wave would
        read as "nothing to prove here".
        """
        config = write_config(tmp_path, live_config(tmp_path))
        evidence = tmp_path / "evidence"

        code = _mod.main(
            ["--wave", "3", "--config", str(config), "--evidence-dir", str(evidence)]
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

    def test_the_runbook_warns_that_wave_two_is_incomplete(self):
        """`--wave 2` runs and exits nonzero. An operator must not read that as broken."""
        text = self.DOC.read_text(encoding="utf-8")

        assert "Wave 2 is incomplete on purpose" in text

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


@pytest.mark.parametrize("implemented", [[], ["pause", "resume"]])
def test_w2_02_accepts_each_wave2_stage(tmp_path, implemented):
    caps = {verb: verb in implemented for verb in _mod.CONTROL_VERBS}
    result = run_w2_02(
        tmp_path,
        contract=neutral_contract_payload(implemented_verbs=implemented),
        client=gateway_stub(capabilities=caps),
    )
    assert result.status == _mod.STATUS_PASSED


@pytest.mark.parametrize("implemented", [["abort"], ["steer"], "pause", None])
def test_w2_02_rejects_invalid_stage_declaration(tmp_path, implemented):
    result = run_w2_02(tmp_path, contract=neutral_contract_payload(implemented_verbs=implemented))
    assert result.status == _mod.STATUS_FAILED


@pytest.mark.parametrize("second", [None, [], "echo"])
def test_w2_02_rejects_non_object_second_adapter(tmp_path, second):
    result = run_w2_02(tmp_path, contract=neutral_contract_payload(second_adapter=second))
    assert result.status == _mod.STATUS_FAILED


ABORTED_RUN_ID = "msg-aborted-001"

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
            # It must NOT have become an aborted run.
            "native_interrupt_status": "failed",
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


def wave2_config(tmp_path: Path, **overrides) -> dict:
    """`live_config` plus the wave-2 artifacts and the seeded aborted run."""
    payloads = overrides.pop("artifact_payloads", None) or {
        **artifact_payloads(),
        **wave2_artifact_payloads(),
        **pause_artifact_payloads(),
    }
    config = live_config(tmp_path, artifact_payloads=payloads)
    config["aborted_run_id"] = ABORTED_RUN_ID
    config.update(overrides)
    return config


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


def run_wave2(tmp_path: Path, *, config=None, client=None) -> dict:
    """Drive the four wave-2 checks S5 owns and return ``{check_id: CheckResult}``."""
    config = config if config is not None else wave2_config(tmp_path)
    probe = _mod.Probe(config["gateway_url"], client or wave2_gateway_stub())
    artifacts = _mod.ArtifactStore(tmp_path, config.get("artifacts") or {})
    driver = _mod.Driver(config, probe, artifacts, dynamodb=ddb_stub())
    with patch.dict("os.environ", IDENTITY_ENV, clear=False):
        results = _mod.run_checks(driver, _mod.WAVE2_CHECKS)
    return {result.check_id: result for result in results}


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

    def test_the_unowned_checks_are_not_run_not_passed(self, tmp_path: Path):
        """The honesty property, at the driver level.

        Narrowed from five IDs to two when S2 (#3961) implemented W2-03..W2-05: a
        check with a predicate must no longer report `not_run` on a complete
        fixture, and the two that remain have no predicate in this revision.
        """
        results = run_wave2(tmp_path)

        for check_id in ("W2-01", "W2-10"):
            assert results[check_id].status == _mod.STATUS_NOT_RUN, check_id
            assert "delivered by" in results[check_id].message, check_id

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
        payloads = {**artifact_payloads(), **wave2_artifact_payloads()}
        payloads["harness_neutrality"]["native_interrupt_status"] = "aborted"

        results = run_wave2(
            tmp_path, config=wave2_config(tmp_path, artifact_payloads=payloads)
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



def test_combined_wave2_runs_both_stories_and_keeps_the_full_gate(tmp_path):
    config = wave2_config(tmp_path)
    results = run_wave2(tmp_path, config=config)
    passed = {key for key, value in results.items() if value.status == _mod.STATUS_PASSED}
    # S3's W2-02, S2's W2-03..W2-05 and S5's W2-06..W2-09 — eight of the ten.
    assert passed == {
        "W2-02",
        "W2-03",
        "W2-04",
        "W2-05",
        "W2-06",
        "W2-07",
        "W2-08",
        "W2-09",
    }
    assert {key for key, value in results.items() if value.status == _mod.STATUS_NOT_RUN} == set(_mod.PENDING_CHECK_OWNERS)
    report = _mod.build_report(config, list(results.values()), cleanup_ok=True, wave=2,
                              expected_ids=tuple(spec.check_id for spec in _mod.WAVE2_CHECKS))
    assert report["required"] == 10
    # The arithmetic that matters is that it still does not add up to ten: the
    # wave cannot report success while W2-01 and W2-10 have no predicate, however
    # many of the other eight pass.
    assert report["passed"] == 8
    assert report["not_run"] == 2
    assert report["failed"] == 0
    assert not _mod.report_is_passing(report)
