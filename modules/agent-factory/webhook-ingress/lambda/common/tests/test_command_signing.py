"""The signer half of engine-command attribution (issue #4539).

An `@agent-engine` command reaches the orchestration engine as a DynamoDB row, so
the row is what carries *who asked for what, on which plan*. Before #4539 those
fields were ordinary mutable attributes: GitHub's HMAC was verified on the delivery
and nothing carried that verification forward, so anything able to write the row
could choose the acting identity and routing target of a human approval.

These tests pin the properties that make the signature meaningful. Each one
corresponds to a way the mechanism could exist and still not protect anything:

* **Canonicalization is exact and shared.** Asserted against
  `contracts/engine-command-envelope/v1/engine-command-envelope.golden.json`, the
  same fixture the gateway-side verifier's suite reads. Two independent
  implementations of "canonical JSON" is the whole risk of a cross-deploy-unit
  signature, and the vectors are what convert a silent production refusal into a
  CI failure.
* **The signed set is closed.** A missing, unknown or wrongly-typed field refuses
  rather than signing a shorter or looser tuple. A field the verifier trusts but
  the signature does not cover would reintroduce the original defect.
* **Body content cannot restructure the envelope.** Quotes, brackets, newlines and
  non-ASCII text in a `replan:` directive are data, never structure.
* **An unusable key is not a usable one.** A placeholder secret, a malformed
  keyring, or an `active_key_id` without material must raise — never produce a
  confident signature under a value that ships in the repo (#4128's lesson).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common import command_signing
from common.command_signing import (
    COMMAND_BODY_MAX_CHARS,
    ENVELOPE_FIELD_NAMES,
    ENVELOPE_FIELDS,
    ENVELOPE_VERSION,
    PROVIDER_GITHUB,
    CommandSigningError,
    canonical_bytes,
    compute_signature,
    parse_keyring,
    sign_command,
    validate_envelope,
)

# tests/[0] common/[1] lambda/[2] webhook-ingress/[3] agent-factory/[4]
# modules/[5] <repo root>/[6]
_CONTRACT = (
    Path(__file__).resolve().parents[6]
    / "contracts"
    / "engine-command-envelope"
    / "v1"
    / "engine-command-envelope.golden.json"
)


def _contract() -> dict:
    """Load the shared golden fixture, or fail loudly.

    Resolved inside a function rather than at import time because `common/` is
    zipped into every deployed Lambda artifact (`package-lambdas.sh` excludes only
    `<module>/tests/*` for in-process modules), so a module-scope path resolution
    against the repo root could fail at import in a packaged context — the same
    reason `test_persona_catalogue_parity.py` defers its path.

    A missing fixture is an assertion, never a skip: skipping here would silently
    stop checking the one property that keeps the two deploy units in agreement.
    """
    assert _CONTRACT.is_file(), (
        f"missing {_CONTRACT} — this test's path arithmetic is stale. Fix the path "
        "rather than skipping, or signer/verifier canonicalization parity stops "
        "being checked and drift ships as a total command outage."
    )
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))


def _envelope(**overrides) -> dict:
    """A spec-valid envelope, with the fixture's first vector as the base."""
    base = dict(_contract()["vectors"][0]["envelope"])
    base.update(overrides)
    return base


class TestGoldenVectorParity:
    """Byte-for-byte agreement with the fixture the verifier also reads."""

    def test_every_vector_canonicalizes_exactly(self):
        contract = _contract()
        for vector in contract["vectors"]:
            produced = canonical_bytes(vector["envelope"]).decode("utf-8")
            assert produced == vector["canonical_utf8"], (
                f"vector {vector['name']!r} canonicalizes differently: {vector['why']}"
            )

    def test_every_vector_signs_exactly(self):
        contract = _contract()
        key = contract["test_signing_key"].encode("utf-8")
        for vector in contract["vectors"]:
            assert compute_signature(key, vector["envelope"]) == vector["signature"], (
                f"vector {vector['name']!r} signs differently: {vector['why']}"
            )

    def test_field_order_matches_the_contract(self):
        """The fixture's declared order IS this module's order.

        Checked for equality, not containment: a field added to the code but not the
        contract would otherwise pass here and then be rejected by a verifier reading
        the contract's order.
        """
        assert list(ENVELOPE_FIELD_NAMES) == _contract()["field_order"]

    def test_field_types_match_the_contract(self):
        declared = _contract()["field_types"]
        assert {name: kind.__name__ for name, kind in ENVELOPE_FIELDS} == declared

    def test_protocol_version_matches_the_contract(self):
        assert ENVELOPE_VERSION == _contract()["protocol_version"]

    def test_unicode_is_utf8_not_escapes(self):
        """The specific divergence `ensure_ascii=True` would cause.

        Called out separately from the vector loop because it is the failure that
        would pass every English-language test and then refuse the first accented
        `replan:` in production.
        """
        envelope = _envelope(command_body="@agent-engine replan: Änderung 日本語")
        produced = canonical_bytes(envelope).decode("utf-8")
        assert "Änderung" in produced
        assert "\\u" not in produced

    def test_newlines_survive_as_escaped_json(self):
        envelope = _envelope(command_body="line one\nline two")
        assert "\\n" in canonical_bytes(envelope).decode("utf-8")


class TestTheSignedSetIsClosed:
    """A tuple that is not exactly the spec must not be signable."""

    def test_unknown_field_refuses(self):
        with pytest.raises(CommandSigningError, match="unknown envelope field"):
            validate_envelope(_envelope(smuggled="value"))

    @pytest.mark.parametrize("field", ENVELOPE_FIELD_NAMES)
    def test_every_field_is_required(self, field):
        envelope = _envelope()
        del envelope[field]
        with pytest.raises(CommandSigningError, match="missing envelope field"):
            validate_envelope(envelope)

    def test_numeric_field_given_a_string_refuses(self):
        """`"4539"` must not be coercible into the `4539` slot.

        Otherwise the two sides could disagree about which one they signed while both
        believing they agreed — and a verifier that coerces would accept a tuple the
        signer never produced.
        """
        with pytest.raises(CommandSigningError, match="must be int"):
            validate_envelope(_envelope(issue_number="4539"))

    def test_string_field_given_a_number_refuses(self):
        with pytest.raises(CommandSigningError, match="must be str"):
            validate_envelope(_envelope(installation_id=99887766))

    def test_boolean_in_an_int_slot_refuses(self):
        """`bool` is a subclass of `int`; a True must not pass as 1."""
        with pytest.raises(CommandSigningError, match="must be int"):
            validate_envelope(_envelope(repo_id=True))

    def test_wrong_protocol_version_refuses(self):
        with pytest.raises(CommandSigningError, match="protocol_version must be"):
            validate_envelope(_envelope(protocol_version="99"))

    def test_wrong_provider_refuses(self):
        """A GitLab- or EventBridge-shaped tuple is not a GitHub comment.

        The provider is in the signed set so a verifier cannot be handed another
        channel's row and accept it as a verified GitHub command.
        """
        with pytest.raises(CommandSigningError, match="provider must be"):
            validate_envelope(_envelope(provider="gitlab"))

    def test_oversize_body_refuses_rather_than_truncating(self):
        """Truncating would sign something other than what arrived.

        The signature's only claim is "this is the delivered tuple". Signing a
        shortened body would make that claim false for the one field a human actually
        typed, so the bound refuses and the row is written unsigned and quarantined.
        """
        oversize = _envelope(command_body="x" * (COMMAND_BODY_MAX_CHARS + 1))
        with pytest.raises(CommandSigningError, match="over the .* bound"):
            validate_envelope(oversize)

    def test_a_body_at_the_bound_still_signs(self):
        """Positive control: the bound rejects excess, not legitimate paragraphs."""
        envelope = _envelope(command_body="x" * COMMAND_BODY_MAX_CHARS)
        assert compute_signature(b"k", envelope)

    def test_oversize_non_body_field_refuses(self):
        with pytest.raises(CommandSigningError, match="over the .* bound"):
            validate_envelope(_envelope(repo="a" * 5000))

    def test_canonical_bytes_validates_first(self):
        """No canonical bytes may exist for a tuple that failed the spec.

        If `canonical_bytes` skipped validation, a caller that reached it directly
        could sign an unvalidated tuple — so validation lives inside it rather than
        being the caller's responsibility to remember.
        """
        with pytest.raises(CommandSigningError):
            canonical_bytes(_envelope(smuggled="value"))


class TestBodyContentCannotForgeStructure:
    """A `replan:` directive is arbitrary human text and must stay data."""

    @pytest.mark.parametrize(
        "body",
        [
            '"],["tenant_id","attacker-org"],["x","',
            '@agent-engine replan: ["tenant_id", "victim"]',
            '@agent-engine replan: a\\",\\"b',
            '@agent-engine replan: {"sender_github_id": "1"}',
        ],
    )
    def test_a_crafted_body_does_not_move_another_field(self, body):
        """The tuple parsed back out is still the tuple that went in.

        Round-tripping the canonical bytes is the assertion: if body content could
        break out of its JSON string it would appear as a structural element, and the
        parsed pair list would no longer match the envelope.
        """
        envelope = _envelope(command_body=body)
        parsed = dict(json.loads(canonical_bytes(envelope).decode("utf-8")))
        assert parsed == envelope
        assert parsed["tenant_id"] == envelope["tenant_id"]

    def test_two_bodies_differing_only_in_escaping_sign_differently(self):
        a = _envelope(command_body='x","y')
        b = _envelope(command_body='x\\","y')
        assert compute_signature(b"k", a) != compute_signature(b"k", b)


class TestTamperingChangesTheSignature:
    """Every signed field is load-bearing, proven one field at a time."""

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
            ("key_id", "2026-06"),
        ],
    )
    def test_changing_one_field_invalidates_the_signature(self, field, tampered):
        original = _envelope()
        assert original[field] != tampered, f"{field} tamper value is not a change"
        signature = compute_signature(b"k", original)
        assert compute_signature(b"k", _envelope(**{field: tampered})) != signature

    def test_a_different_key_produces_a_different_signature(self):
        envelope = _envelope()
        assert compute_signature(b"key-a", envelope) != compute_signature(
            b"key-b", envelope
        )


class TestKeyring:
    """An unusable keyring must raise, never yield a confident signature."""

    def _keyring(self, **overrides) -> str:
        doc = {
            "active_key_id": "2026-09",
            "keys": {"2026-09": "aaaa", "2026-06": "bbbb"},
            "previous_valid_until": "2026-09-22T00:00:00Z",
        }
        doc.update(overrides)
        return json.dumps(doc)

    def test_parses_active_key_and_overlap(self):
        active, keys, until = parse_keyring(self._keyring())
        assert active == "2026-09"
        assert keys == {"2026-09": b"aaaa", "2026-06": b"bbbb"}
        assert until == "2026-09-22T00:00:00Z"

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
    def test_placeholder_secret_refuses(self, value):
        """#4128's lesson: a signature under a repo-published value REPORTS success.

        That is strictly worse than no signature, so an un-rotated placeholder is
        treated as no key at all and signing raises.
        """
        with pytest.raises(CommandSigningError, match="placeholder"):
            parse_keyring(value)

    def test_non_json_secret_refuses(self):
        with pytest.raises(CommandSigningError, match="not JSON"):
            parse_keyring("not-json-at-all")

    def test_json_that_is_not_an_object_refuses(self):
        with pytest.raises(CommandSigningError, match="must be a JSON object"):
            parse_keyring('["a"]')

    def test_missing_active_key_id_refuses(self):
        with pytest.raises(CommandSigningError, match="active_key_id"):
            parse_keyring(json.dumps({"keys": {"a": "b"}}))

    def test_active_key_id_without_material_refuses(self):
        with pytest.raises(CommandSigningError, match="no key material"):
            parse_keyring(self._keyring(keys={"2026-06": "bbbb"}))

    def test_a_placeholder_valued_key_is_dropped(self):
        """A half-rotated secret must fail closed on the placeholder id, not use it."""
        _, keys, _ = parse_keyring(
            self._keyring(keys={"2026-09": "aaaa", "2026-06": "PLACEHOLDER"})
        )
        assert set(keys) == {"2026-09"}

    def test_a_placeholder_active_key_refuses(self):
        with pytest.raises(CommandSigningError, match="no key material"):
            parse_keyring(self._keyring(keys={"2026-09": "CHANGEME"}))

    def test_absent_overlap_window_is_none(self):
        _, _, until = parse_keyring(
            json.dumps({"active_key_id": "k", "keys": {"k": "v"}})
        )
        assert until is None


class TestSignCommand:
    """The handler-facing entry point."""

    @pytest.fixture(autouse=True)
    def _clean_cache(self, monkeypatch):
        command_signing.reset_key_cache()
        yield
        command_signing.reset_key_cache()

    def _seed_key(self, monkeypatch, secret_value: str) -> None:
        monkeypatch.setenv(
            command_signing.SIGNING_KEY_SECRET_ARN_ENV,
            "arn:aws:secretsmanager:us-east-1:111122223333:secret:test-abc",
        )
        import common.secrets as secrets_mod

        monkeypatch.setattr(secrets_mod, "get_secret", lambda _arn: secret_value)

    def _call(self, **overrides):
        args = dict(
            delivery_id="d-1",
            event_type="issue_comment",
            event_id="msg-1",
            arrived_at="2026-09-15T21:15:31Z",
            tenant_id="acme-corp",
            installation_id="99887766",
            repo_id=123,
            repo="acme-corp/app",
            issue_number=4539,
            sender_github_id="1042",
            sender_type="User",
            command_body="@agent-engine halt",
            signed_at="2026-09-15T21:15:32Z",
        )
        args.update(overrides)
        return sign_command(**args)

    def test_returns_key_id_signature_and_the_exact_signed_fields(self, monkeypatch):
        """The signed fields are returned so the ROW can store what was signed.

        The verifier rebuilds canonical bytes from this stored tuple rather than from
        the row's other mutable attributes, and then refuses a row whose mutable
        copies disagree with it. Returning the envelope is what makes that possible.
        """
        self._seed_key(
            monkeypatch, json.dumps({"active_key_id": "k1", "keys": {"k1": "secret"}})
        )
        key_id, signature, signed = self._call()

        assert key_id == "k1"
        assert signature == compute_signature(b"secret", signed)
        assert set(signed) == set(ENVELOPE_FIELD_NAMES)
        assert signed["key_id"] == "k1"
        assert signed["provider"] == PROVIDER_GITHUB
        assert signed["protocol_version"] == ENVELOPE_VERSION
        assert signed["tenant_id"] == "acme-corp"

    def test_signs_under_the_active_key_not_a_previous_one(self, monkeypatch):
        self._seed_key(
            monkeypatch,
            json.dumps({"active_key_id": "new", "keys": {"new": "n", "old": "o"}}),
        )
        key_id, signature, signed = self._call()
        assert key_id == "new"
        assert signature == compute_signature(b"n", signed)
        assert signature != compute_signature(b"o", signed)

    def test_missing_secret_arn_refuses(self, monkeypatch):
        monkeypatch.delenv(command_signing.SIGNING_KEY_SECRET_ARN_ENV, raising=False)
        with pytest.raises(CommandSigningError, match="is not set"):
            self._call()

    def test_placeholder_secret_refuses(self, monkeypatch):
        self._seed_key(monkeypatch, "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND")
        with pytest.raises(CommandSigningError, match="placeholder"):
            self._call()

    def test_a_repeat_call_after_failure_still_refuses(self, monkeypatch):
        """The failure is cached like the success is.

        A warm Lambda that failed to load a key must not retry Secrets Manager on
        every delivery — that turns a misconfiguration into throttling — and must not
        appear to succeed later without a cold start.
        """
        self._seed_key(monkeypatch, "PLACEHOLDER")
        with pytest.raises(CommandSigningError):
            self._call()
        with pytest.raises(CommandSigningError, match="no engine-command signing key"):
            self._call()

    def test_oversize_body_refuses_at_the_entry_point(self, monkeypatch):
        self._seed_key(
            monkeypatch, json.dumps({"active_key_id": "k1", "keys": {"k1": "s"}})
        )
        with pytest.raises(CommandSigningError, match="over the .* bound"):
            self._call(command_body="x" * (COMMAND_BODY_MAX_CHARS + 1))

    def test_the_signature_is_reproducible(self, monkeypatch):
        """Same tuple, same key, same signature — the verifier depends on it."""
        self._seed_key(
            monkeypatch, json.dumps({"active_key_id": "k1", "keys": {"k1": "s"}})
        )
        first = self._call()[1]
        command_signing.reset_key_cache()
        self._seed_key(
            monkeypatch, json.dumps({"active_key_id": "k1", "keys": {"k1": "s"}})
        )
        assert self._call()[1] == first
