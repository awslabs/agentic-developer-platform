"""The consumer half of engine-command attribution (issue #4539).

An `@agent-engine` command arrives here as a DynamoDB row, so the row carries *who
asked for what, on which plan*. Those fields were ordinary mutable attributes:
GitHub's HMAC was verified on the delivery and nothing carried that forward, so
anything able to write the row could choose the acting identity and routing target
of a human approval.

`test_command_signing.py` in webhook-ingress pins the signer. This file pins the
verifier, and the two read the SAME golden fixture — that shared fixture is the only
thing standing between two independent canonicalization implementations and a silent
production outage, because the two deploy units cannot import each other.

The properties under test, each corresponding to a way this could exist and protect
nothing:

* **Parity with the signer, byte for byte.** Asserted against the contract's vectors.
* **A tampered row refuses.** Every signed field, one at a time.
* **A signature is not liftable.** Cross-row, cross-tenant, cross-repo and
  cross-installation replay all refuse — including the case the old code missed,
  where an ABSENT installation on the row skipped the comparison entirely.
* **Unknown keys, versions and shapes fail closed.** Never "assume the active key",
  never "guess the layout", never a verdict under a placeholder secret (#4128).
* **No coercion.** `"4539"` is not `4539`, and `True` is not `1`.
* **A valid signature authorizes nothing.** Pinned as an explicit assertion about
  the returned type, because the tempting misreading of this module is the one that
  would turn it into an authorization bypass.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.orchestration import command_attribution
from src.orchestration.command_attribution import (
    ENVELOPE_FIELD_NAMES,
    ENVELOPE_FIELDS,
    KEY_ID_ATTR,
    REASON_BAD_SIGNATURE,
    REASON_MALFORMED_PAYLOAD,
    REASON_MISSING_KEY_ID,
    REASON_MISSING_PAYLOAD,
    REASON_MISSING_SIGNATURE,
    REASON_NO_KEY,
    REASON_PAYLOAD_TOO_LARGE,
    REASON_ROW_MISMATCH,
    REASON_STALE_KEY_ID,
    REASON_UNKNOWN_KEY_ID,
    REASON_UNKNOWN_PROTOCOL,
    REASON_WRONG_PROVIDER,
    SIGNATURE_ATTR,
    SIGNED_PAYLOAD_ATTR,
    AttributionError,
    VerifiedCommand,
    canonical_bytes,
    verify_row,
)

# tests/orchestration/[0] tests/[1] gateway/[2] modules/[3] <repo root>/[4]
_CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "engine-command-envelope" / "v1" / "engine-command-envelope.golden.json"


def _contract() -> dict:
    """Load the shared golden fixture, or fail loudly.

    An assertion, never a skip: skipping would silently stop checking the one
    property that keeps this verifier and the webhook-ingress signer in agreement,
    and the symptom of drift is every real human command being refused.
    """
    assert _CONTRACT.is_file(), (
        f"missing {_CONTRACT} — this test's path arithmetic is stale. Fix the path "
        "rather than skipping, or signer/verifier canonicalization parity stops "
        "being checked."
    )
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))


#: The real loader, captured before any test patches it, so a test that needs to
#: exercise the genuine "no key configured" path can restore it.
_REAL_LOAD_KEYRING = command_attribution._load_keyring


def _test_key() -> bytes:
    return _contract()["test_signing_key"].encode("utf-8")


def _sign(key: bytes, envelope: dict) -> str:
    """Sign the way the signer does, so these tests never assume the code is right."""
    digest = hmac.new(key, canonical_bytes(envelope), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _envelope(**overrides) -> dict:
    base = dict(_contract()["vectors"][0]["envelope"])
    base.update(overrides)
    return base


def _row(envelope: dict | None = None, *, key: bytes | None = None, **overrides) -> dict:
    """A marked, signed row exactly as the webhook Lambda writes it."""
    envelope = envelope if envelope is not None else _envelope()
    key = key if key is not None else _test_key()
    row = {
        "event_id": envelope["event_id"],
        "arrived_at": envelope["arrived_at"],
        "tenant_id": envelope["tenant_id"],
        "repo": envelope["repo"],
        "issue_number": envelope["issue_number"],
        "installation_id": envelope["installation_id"],
        "engine_command_status": "pending",
        "engine_command_body": envelope["command_body"],
        "engine_command_sender_github_id": envelope["sender_github_id"],
        "engine_command_sender_is_bot": envelope["sender_type"] == "Bot",
        SIGNATURE_ATTR: _sign(key, envelope),
        KEY_ID_ATTR: envelope["key_id"],
        SIGNED_PAYLOAD_ATTR: canonical_bytes(envelope).decode("utf-8"),
        "engine_command_protocol_version": envelope["protocol_version"],
    }
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def _keyring(monkeypatch):
    """Seed the contract's test key as the active key, and clear the cache.

    The keyring is cached per process (and caches failure too), so without this a
    test following a no-key test would inherit "no key" and pass for the wrong
    reason.
    """
    command_attribution.reset_key_cache()
    _seed(monkeypatch, {"active_key_id": "2026-09", "keys": {"2026-09": _key_text()}})
    yield
    command_attribution.reset_key_cache()


def _key_text() -> str:
    return _contract()["test_signing_key"]


def _seed(monkeypatch, keyring: dict) -> None:
    """Install a keyring without reaching Secrets Manager."""
    command_attribution.reset_key_cache()
    monkeypatch.setenv(
        command_attribution.SIGNING_KEY_SECRET_ARN_ENV,
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:engine-cmd-test",
    )
    monkeypatch.setattr(
        command_attribution,
        "_load_keyring",
        lambda: command_attribution.parse_keyring(json.dumps(keyring)),
    )


class TestGoldenVectorParity:
    """Byte-for-byte agreement with the signer, via the shared contract."""

    def test_every_vector_canonicalizes_exactly(self):
        for vector in _contract()["vectors"]:
            produced = canonical_bytes(vector["envelope"]).decode("utf-8")
            assert produced == vector["canonical_utf8"], (
                f"vector {vector['name']!r} canonicalizes differently here than in the signer: {vector['why']}"
            )

    def test_every_vector_signs_exactly(self):
        key = _test_key()
        for vector in _contract()["vectors"]:
            assert _sign(key, vector["envelope"]) == vector["signature"], f"vector {vector['name']!r} signs differently: {vector['why']}"

    def test_field_order_matches_the_contract(self):
        """Equality, not containment: a field added here but not there would drift."""
        assert list(ENVELOPE_FIELD_NAMES) == _contract()["field_order"]

    def test_field_types_match_the_contract(self):
        declared = _contract()["field_types"]
        assert {name: kind.__name__ for name, kind in ENVELOPE_FIELDS} == declared

    def test_every_vector_verifies_as_a_row(self):
        """End to end: each vector, written as a row, verifies."""
        for vector in _contract()["vectors"]:
            verified = verify_row(_row(vector["envelope"]))
            assert verified.command_body == vector["envelope"]["command_body"]


class TestAValidRowVerifies:
    def test_the_signed_tuple_is_returned(self):
        verified = verify_row(_row())
        envelope = _envelope()

        assert isinstance(verified, VerifiedCommand)
        assert verified.tenant_id == envelope["tenant_id"]
        assert verified.installation_id == envelope["installation_id"]
        assert verified.repo == envelope["repo"]
        assert verified.issue_number == envelope["issue_number"]
        assert verified.sender_github_id == envelope["sender_github_id"]
        assert verified.command_body == envelope["command_body"]

    def test_the_returned_tuple_is_immutable(self):
        """A later stage must not be able to rewrite a verified authority field."""
        verified = verify_row(_row())
        with pytest.raises(Exception):
            verified.tenant_id = "attacker-org"  # type: ignore[misc]

    def test_a_unicode_command_verifies(self):
        envelope = _envelope(command_body="@agent-engine replan: Änderung 日本語\nzeile")
        assert verify_row(_row(envelope)).command_body == envelope["command_body"]

    def test_verification_carries_no_authorization(self):
        """The misreading that would make this module an authorization bypass.

        `VerifiedCommand` deliberately exposes no permission, role, or approval —
        only the delivered tuple. Membership, PLAN_APPROVE and the human-only gates
        run afterwards, unchanged. This asserts the shape of that boundary so a
        future change cannot quietly attach an entitlement to a signature.
        """
        verified = verify_row(_row())
        attributes = set(vars(verified))

        for forbidden in ("authorized", "role", "permissions", "can_approve", "access"):
            assert forbidden not in attributes, (
                f"VerifiedCommand grew {forbidden!r}: a valid signature must never stand in for a membership or permission check"
            )

    def test_author_kind_comes_from_the_signed_tuple(self):
        """The bot flag on the row is a convenience; `sender_type` is the evidence."""
        assert verify_row(_row(_envelope(sender_type="Bot"))).sender_is_bot is True
        assert verify_row(_row(_envelope(sender_type="User"))).sender_is_bot is False

    def test_a_flipped_row_bot_flag_does_not_change_the_verdict(self):
        """Author kind is not forgeable by rewriting the mutable flag."""
        row = _row(_envelope(sender_type="Bot"), engine_command_sender_is_bot=False)
        assert verify_row(row).sender_is_bot is True


class TestTamperingRefuses:
    @pytest.mark.parametrize(
        ("field", "tampered"),
        [
            ("tenant_id", "attacker-org"),
            ("installation_id", "11111111"),
            ("repo", "attacker/evil"),
            ("repo_id", 999),
            ("issue_number", 1),
            ("sender_github_id", "999999"),
            ("sender_type", "Bot"),
            ("command_body", "@agent-engine accept"),
            ("delivery_id", "00000000-0000-4000-8000-000000000000"),
            ("event_type", "pull_request"),
            ("event_id", "msg-9999"),
            ("arrived_at", "2030-01-01T00:00:00Z"),
            ("signed_at", "2030-01-01T00:00:00Z"),
        ],
    )
    def test_rewriting_a_signed_field_in_the_payload_refuses(self, field, tampered):
        """The payload is edited but the signature is left as it was."""
        original = _envelope()
        assert original[field] != tampered, f"{field} tamper value is not a change"

        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(_envelope(**{field: tampered})).decode("utf-8")

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_BAD_SIGNATURE

    @pytest.mark.parametrize(
        ("attr", "tampered"),
        [
            ("tenant_id", "attacker-org"),
            ("repo", "attacker/evil"),
            ("issue_number", 1),
            ("installation_id", "11111111"),
            ("engine_command_body", "@agent-engine accept"),
            ("engine_command_sender_github_id", "999999"),
            ("event_id", "other-event"),
            ("arrived_at", "2030-01-01T00:00:00Z"),
        ],
    )
    def test_rewriting_a_mutable_row_copy_refuses(self, attr, tampered):
        """A row whose visible content differs from what was verified is refused.

        The tick reads authority from the signed tuple, so this is not itself a
        bypass — but continuing would mean acting on a row an operator reading the
        table would be actively misled by.
        """
        with pytest.raises(AttributionError) as exc:
            verify_row(_row(**{attr: tampered}))
        assert exc.value.reason == REASON_ROW_MISMATCH

    def test_a_corrupted_signature_refuses(self):
        row = _row()
        row[SIGNATURE_ATTR] = "A" * len(row[SIGNATURE_ATTR])
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_BAD_SIGNATURE

    def test_a_signature_from_another_key_refuses(self):
        row = _row(key=b"some-other-key-entirely")
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_BAD_SIGNATURE

    def test_a_reordered_payload_refuses(self):
        """Order is part of the data, not an emergent property of field names.

        Refused at validation, not by the signature: rebuilding the dict would
        normalise the order away and the bytes would match again. All the values
        would still be authenticated — so this is not a forgery path — but the
        contract's declared `field_order` would become a claim nothing enforces.
        """
        pairs = json.loads(canonical_bytes(_envelope()).decode("utf-8"))
        pairs[0], pairs[1] = pairs[1], pairs[0]
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = json.dumps(pairs, separators=(",", ":"))

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD


class TestSignaturesAreNotLiftable:
    """A signature valid for one command must not validate another."""

    def test_a_signature_cannot_be_moved_to_another_row(self):
        """The row keys are signed, so a lifted signature names the wrong row."""
        victim = _row(_envelope(event_id="evt-victim"))
        attacker = _row(_envelope(event_id="evt-attacker"))
        attacker[SIGNATURE_ATTR] = victim[SIGNATURE_ATTR]
        attacker[SIGNED_PAYLOAD_ATTR] = victim[SIGNED_PAYLOAD_ATTR]

        with pytest.raises(AttributionError) as exc:
            verify_row(attacker)
        assert exc.value.reason == REASON_ROW_MISMATCH

    def test_a_command_cannot_be_replayed_into_another_tenant(self):
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(_envelope(tenant_id="attacker-org")).decode("utf-8")
        row["tenant_id"] = "attacker-org"

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_BAD_SIGNATURE

    def test_a_command_cannot_be_pointed_at_another_repository(self):
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(_envelope(repo="attacker/evil", repo_id=1)).decode("utf-8")
        row["repo"] = "attacker/evil"

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_BAD_SIGNATURE

    def test_an_absent_row_installation_still_refuses(self):
        """The exact gap the pre-#4539 check had.

        `engine_commands.py` guarded the installation comparison with
        `row_installation and ...`, so a row with an absent or empty
        `installation_id` skipped the check entirely — omitting the attribute was
        enough to bypass it. Here an absent installation is a MISMATCH against the
        signed one, never a skipped comparison.
        """
        for absent in ("", "   ", None):
            with pytest.raises(AttributionError) as exc:
                verify_row(_row(installation_id=absent))
            assert exc.value.reason == REASON_ROW_MISMATCH

    def test_a_missing_row_installation_is_not_a_pass(self):
        """Stated as its own test because the failure mode was a silent skip."""
        row = _row()
        del row["installation_id"]
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_ROW_MISMATCH


class TestMissingAndMalformedInputRefuses:
    def test_an_unsigned_row_refuses(self):
        """What a publisher with no key seeded produces — refused, not applied."""
        row = _row()
        del row[SIGNATURE_ATTR]
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MISSING_SIGNATURE

    def test_a_row_predating_signing_refuses(self):
        """A #4527-era row carries none of the four attributes."""
        row = {
            "event_id": "old-1",
            "arrived_at": "2026-08-01T00:00:00Z",
            "tenant_id": "acme-corp",
            "repo": "acme-corp/app",
            "issue_number": 4527,
            "installation_id": "99887766",
            "engine_command_status": "pending",
            "engine_command_body": "@agent-engine halt",
            "engine_command_sender_github_id": "1042",
        }
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MISSING_SIGNATURE

    def test_a_missing_key_id_is_never_the_active_key(self):
        """Falling back would let the row's author choose the checking key."""
        row = _row()
        del row[KEY_ID_ATTR]
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MISSING_KEY_ID

    def test_a_missing_payload_refuses(self):
        row = _row()
        del row[SIGNED_PAYLOAD_ATTR]
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MISSING_PAYLOAD

    def test_a_row_key_id_disagreeing_with_the_signed_one_refuses(self):
        """Verification must use the key the tuple committed to."""
        row = _row()
        row[KEY_ID_ATTR] = "2026-06"
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_ROW_MISMATCH

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            '{"protocol_version": "1"}',
            '[["protocol_version"]]',
            '[["protocol_version","1","extra"]]',
            "[[1,2]]",
            '[["protocol_version","1"],["protocol_version","1"]]',
        ],
    )
    def test_a_malformed_payload_refuses(self, payload):
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = payload
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD

    def test_an_oversize_payload_refuses_before_parsing(self):
        """Bounded first, so a hostile row cannot choose the parse cost."""
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = "[" + ("x" * 20000)
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_PAYLOAD_TOO_LARGE

    def test_an_unknown_field_in_the_payload_refuses(self):
        pairs = json.loads(canonical_bytes(_envelope()).decode("utf-8"))
        pairs.append(["smuggled", "value"])
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = json.dumps(pairs, separators=(",", ":"))
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD

    @pytest.mark.parametrize("field", ENVELOPE_FIELD_NAMES)
    def test_a_missing_payload_field_refuses(self, field):
        pairs = [pair for pair in json.loads(canonical_bytes(_envelope()).decode("utf-8")) if pair[0] != field]
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = json.dumps(pairs, separators=(",", ":"))
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD

    def test_a_string_in_a_numeric_slot_is_not_coerced(self):
        """`"4539"` must not be read as `4539`."""
        pairs = [[name, "4539" if name == "issue_number" else value] for name, value in json.loads(canonical_bytes(_envelope()).decode("utf-8"))]
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = json.dumps(pairs, separators=(",", ":"))
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD

    def test_a_boolean_in_a_numeric_slot_refuses(self):
        """`bool` is an `int` subclass; `True` must not pass as `1`."""
        pairs = [[name, True if name == "repo_id" else value] for name, value in json.loads(canonical_bytes(_envelope()).decode("utf-8"))]
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = json.dumps(pairs, separators=(",", ":"))
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_MALFORMED_PAYLOAD

    def test_an_unknown_protocol_version_refuses_rather_than_guessing(self):
        envelope = _envelope(protocol_version="99")
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(envelope).decode("utf-8")
        row[SIGNATURE_ATTR] = _sign(_test_key(), envelope)

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_UNKNOWN_PROTOCOL

    def test_another_providers_row_refuses(self):
        """A GitLab- or EventBridge-shaped tuple is not a verified GitHub comment."""
        envelope = _envelope(provider="gitlab")
        row = _row()
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(envelope).decode("utf-8")
        row[SIGNATURE_ATTR] = _sign(_test_key(), envelope)

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_WRONG_PROVIDER

    def test_the_protocol_check_precedes_the_signature_check(self):
        """An unknown version refuses even when correctly signed.

        Otherwise the verifier would be computing canonical bytes for a layout it
        does not know, which is exactly "guessing at the layout".
        """
        envelope = _envelope(protocol_version="99")
        row = _row(envelope)
        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert exc.value.reason == REASON_UNKNOWN_PROTOCOL


class TestKeys:
    def test_no_key_configured_refuses(self, monkeypatch):
        """An environment that never seeded the key refuses every command.

        `_load_keyring` is restored to the real implementation first: the autouse
        fixture patches it, so without this the test would assert against a stub and
        pass no matter what the real loader does with a missing ARN.
        """
        monkeypatch.setattr(command_attribution, "_load_keyring", _REAL_LOAD_KEYRING)
        command_attribution.reset_key_cache()
        monkeypatch.delenv(command_attribution.SIGNING_KEY_SECRET_ARN_ENV, raising=False)
        with pytest.raises(AttributionError) as exc:
            verify_row(_row())
        assert exc.value.reason == REASON_NO_KEY

    @pytest.mark.parametrize(
        "value",
        [
            "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND",
            "PLACEHOLDER",
            "CHANGEME",
            "",
            "   ",
        ],
    )
    def test_a_placeholder_secret_refuses(self, value):
        """#4128: a verdict under a repo-published value would REPORT success."""
        with pytest.raises(AttributionError) as exc:
            command_attribution.parse_keyring(value)
        assert exc.value.reason == REASON_NO_KEY

    def test_an_unknown_key_id_refuses(self, monkeypatch):
        _seed(monkeypatch, {"active_key_id": "other", "keys": {"other": "material"}})
        with pytest.raises(AttributionError) as exc:
            verify_row(_row())
        assert exc.value.reason == REASON_UNKNOWN_KEY_ID

    def test_a_previous_key_verifies_inside_the_overlap_window(self, monkeypatch):
        """The window is what lets rows signed just before a rotation be consumed."""
        _seed(
            monkeypatch,
            {
                "active_key_id": "2026-12",
                "keys": {"2026-12": "new-material", "2026-09": _key_text()},
                "previous_valid_until": "2026-12-31T00:00:00Z",
            },
        )
        verified = verify_row(_row(), now=datetime(2026, 12, 25, tzinfo=UTC))
        assert verified.key_id == "2026-09"

    def test_a_previous_key_refuses_after_the_window(self, monkeypatch):
        """Expiry is what stops a retired key being valid forever."""
        _seed(
            monkeypatch,
            {
                "active_key_id": "2026-12",
                "keys": {"2026-12": "new-material", "2026-09": _key_text()},
                "previous_valid_until": "2026-12-31T00:00:00Z",
            },
        )
        with pytest.raises(AttributionError) as exc:
            verify_row(_row(), now=datetime(2027, 1, 1, tzinfo=UTC))
        assert exc.value.reason == REASON_STALE_KEY_ID

    def test_a_previous_key_with_no_window_refuses(self, monkeypatch):
        """No declared overlap means retired, not unbounded."""
        _seed(
            monkeypatch,
            {
                "active_key_id": "2026-12",
                "keys": {"2026-12": "new-material", "2026-09": _key_text()},
            },
        )
        with pytest.raises(AttributionError) as exc:
            verify_row(_row())
        assert exc.value.reason == REASON_STALE_KEY_ID

    def test_an_unparseable_window_is_treated_as_expired(self, monkeypatch):
        """The fail-closed reading of "we cannot tell if this key is still valid"."""
        _seed(
            monkeypatch,
            {
                "active_key_id": "2026-12",
                "keys": {"2026-12": "new-material", "2026-09": _key_text()},
                "previous_valid_until": "whenever",
            },
        )
        with pytest.raises(AttributionError) as exc:
            verify_row(_row())
        assert exc.value.reason == REASON_STALE_KEY_ID

    def test_the_active_key_needs_no_window(self, monkeypatch):
        """Positive control: the overlap logic must not gate the active key."""
        _seed(monkeypatch, {"active_key_id": "2026-09", "keys": {"2026-09": _key_text()}})
        assert verify_row(_row()).key_id == "2026-09"

    def test_a_placeholder_valued_key_is_dropped(self):
        _, keys, _ = command_attribution.parse_keyring(
            json.dumps(
                {
                    "active_key_id": "a",
                    "keys": {"a": "real-material", "b": "PLACEHOLDER"},
                }
            )
        )
        assert set(keys) == {"a"}


class TestRefusalReasonsAreSafe:
    """Reasons reach logs and a metric dimension, so they must be bounded."""

    def test_no_reason_contains_row_content(self):
        """A crafted body must not appear in the refusal reason.

        An unbounded reason would put attacker-chosen text into operator surfaces
        and blow up metric cardinality.
        """
        marker = "SENTINEL-ATTACKER-CONTROLLED-TEXT"
        row = _row(_envelope(command_body=f"@agent-engine halt {marker}"))
        row[SIGNATURE_ATTR] = "A" * 43

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert marker not in exc.value.reason
        assert marker not in str(exc.value)

    def test_no_reason_contains_the_signature_or_key(self):
        row = _row()
        signature = row[SIGNATURE_ATTR]
        row[SIGNED_PAYLOAD_ATTR] = canonical_bytes(_envelope(tenant_id="attacker-org")).decode("utf-8")

        with pytest.raises(AttributionError) as exc:
            verify_row(row)
        assert signature not in str(exc.value)
        assert _key_text() not in str(exc.value)

    def test_every_reason_is_from_the_bounded_set(self):
        """Each refusal path uses a declared constant, so the dimension is finite."""
        known = {value for name, value in vars(command_attribution).items() if name.startswith("REASON_") and isinstance(value, str)}
        cases = [
            _row(tenant_id="attacker-org"),
            _row(installation_id=""),
        ]
        no_sig = _row()
        del no_sig[SIGNATURE_ATTR]
        cases.append(no_sig)

        for row in cases:
            with pytest.raises(AttributionError) as exc:
                verify_row(row)
            assert exc.value.reason in known
