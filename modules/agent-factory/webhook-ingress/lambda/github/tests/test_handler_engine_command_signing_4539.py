"""Issue #4539 — the handler signs what GitHub actually delivered.

`command_signing` producing a correct signature and `webhook_events` storing it are
both useless if the handler signs the wrong tuple, or signs at the wrong moment.
This module drives the real `handler()` entry point with genuine `issue_comment`
payloads and asserts on what was signed — the same style as
`test_handler_engine_sender_is_bot_4599.py`, and for the same reason: two halves
passing in isolation is exactly how a field ends up never populated in production.

The properties pinned here are the ones that make the signature *mean* something:

* **Signed from the delivery, not from anything a commenter chose.** Every field
  except the command body comes from the verified payload, GitHub's delivery header,
  or the tenant this handler resolved. A commenter who writes
  `tenant_id: victim-org` in their comment must not move the signed tenant.
* **Signed only after GitHub's HMAC verified.** The signature attests to a verified
  delivery; if it could be produced on a path where verification failed, it would
  attest to nothing. Structurally guaranteed (signing lives past the step-2 early
  return), and asserted.
* **The row keys are in the signed tuple.** Otherwise a signature is liftable onto a
  different row — the forgery this issue exists to stop, just one indirection out.
* **Signing failure never breaks the webhook.** No key seeded still returns 200, and
  still records the row: an unsigned marker is refused by the tick, which is
  strictly better than a delivery failure GitHub retries, or a lost audit record.
* **Nothing is signed off the engine path.** The overwhelming majority of traffic
  must not pay for, or acquire, an attribution signature.
"""

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret-123")
os.environ.setdefault("WEBHOOK_SECRET_ARN", "")
os.environ.setdefault(
    "SUBMIT_QUEUE_URL",
    "https://sqs.us-east-1.amazonaws.com/123456789/adp-dev-agent-submit.fifo",
)
os.environ.setdefault("IDENTITY_INDEX_TABLE", "adp-dev-identity-index")
os.environ.setdefault("RATE_LIMITS_TABLE", "adp-dev-rate-limits")
os.environ.setdefault("AWS_REGION", "us-east-1")

WEBHOOK_SECRET = "test-secret-123"
DELIVERY_ID = "11112222-3333-4444-5555-666677778888"

#: Test-only key material, seeded through a patched secret fetch. Never a real key:
#: what these tests assert is which VALUES were signed, which is independent of the
#: key. The signer refuses known placeholder strings, so this must not be one.
_TEST_KEYRING = json.dumps(
    {"active_key_id": "test-key", "keys": {"test-key": "unit-test-material"}}
)


def _make_event(event_type: str, payload: dict, *, delivery_id: str = DELIVERY_ID) -> dict:
    body = json.dumps(payload)
    sig = hmac.new(WEBHOOK_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "headers": {
            "x-github-event": event_type,
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={sig}",
            "x-github-delivery": delivery_id,
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _mock_resolved_identity(tenant_id="acme", user_id="u_test1"):
    from common.identity_resolver import ResolvedIdentity

    return ResolvedIdentity(
        tenant_id=tenant_id,
        org_id=tenant_id,
        user_id=user_id,
        user_provisioning_mode="strict",
    )


HUMAN = {"login": "operator", "id": 1042, "type": "User"}
AGENT_BOT = {"login": "aws-e-adp-agent-dev[bot]", "id": 200, "type": "Bot"}


def _engine_comment_payload(sender: dict = HUMAN, body: str = "@agent-engine halt") -> dict:
    return {
        "action": "created",
        "comment": {"body": body},
        "issue": {
            "number": 4539,
            "title": "Agent-writable authority row",
            "html_url": "https://github.com/acme/repo/issues/4539",
        },
        "repository": {"full_name": "acme/repo", "id": 55501},
        "sender": sender,
        "installation": {"id": 99887766},
    }


@pytest.fixture(autouse=True)
def _clean_signing_key(monkeypatch):
    """Seed test key material and clear the module cache around every test.

    The signer caches its key per execution environment (and caches failure too), so
    without this a test that runs after a no-key test would inherit "no key" and pass
    for the wrong reason.
    """
    from common import command_signing

    command_signing.reset_key_cache()
    monkeypatch.setenv(
        command_signing.SIGNING_KEY_SECRET_ARN_ENV,
        "arn:aws:secretsmanager:us-east-1:111122223333:secret:engine-cmd-test",
    )
    import common.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "get_secret", lambda _arn: _TEST_KEYRING)
    yield
    command_signing.reset_key_cache()


def _run(payload: dict, *, event_type: str = "issue_comment", **event_overrides):
    """Drive the real handler, capturing what reached `log_event`.

    `_capture_invocation_event` is deliberately NOT patched here (unlike #4599's
    tests): the signing happens inside it, so patching it would remove the code under
    test. `_get_webhook_event_logger` is patched instead, one layer lower, so the real
    derivation and signing run and the final row kwargs are observable.
    """
    with (
        patch("handler._get_events_log") as mock_log,
        patch("handler._get_rate_limiter") as mock_rate,
        patch("handler._get_identity_resolver") as mock_resolver,
        patch("handler._get_signature") as mock_sig,
        patch("handler._get_webhook_event_logger") as mock_event_logger,
    ):
        mock_sig.return_value.verify_github_signature.return_value = True
        mock_resolver.return_value.resolve.return_value = (_mock_resolved_identity(), "ok")
        rate = MagicMock()
        rate.allowed = True
        rate.retry_after_seconds = 0
        mock_rate.return_value.check_and_increment.return_value = rate
        mock_log.return_value.log_event = MagicMock()
        row_logger = MagicMock()
        mock_event_logger.return_value = row_logger

        from handler import handler

        result = handler(_make_event(event_type, payload, **event_overrides), None)
        return result, row_logger


def _row_kwargs(row_logger) -> dict:
    assert row_logger.log_event.call_args is not None, "no row was written"
    return row_logger.log_event.call_args.kwargs


def _signed(row_logger) -> dict:
    """The signed tuple, parsed back out of the payload stored on the row."""
    payload = _row_kwargs(row_logger)["engine_command_signed_payload"]
    assert payload, "the row carries no signed payload"
    return dict(json.loads(payload))


class TestTheSignedTupleMatchesTheDelivery:
    def test_the_row_is_signed(self):
        result, row_logger = _run(_engine_comment_payload())

        assert result["statusCode"] == 200
        kwargs = _row_kwargs(row_logger)
        assert kwargs["engine_command"] is True
        assert kwargs["engine_command_signature"]
        assert kwargs["engine_command_signing_key_id"] == "test-key"
        assert kwargs["engine_command_signed_payload"]

    def test_the_signature_verifies_against_the_stored_payload(self):
        """End to end: the bytes on the row reproduce the signature on the row.

        This is what the gateway-side verifier will do, so a divergence here is a
        divergence in production.
        """
        from common.command_signing import compute_signature

        _, row_logger = _run(_engine_comment_payload())
        kwargs = _row_kwargs(row_logger)

        recomputed = compute_signature(b"unit-test-material", _signed(row_logger))
        assert recomputed == kwargs["engine_command_signature"]

    def test_every_authority_field_comes_from_the_delivery(self):
        """The whole point: each signed value is the delivered one."""
        _, row_logger = _run(_engine_comment_payload())
        signed = _signed(row_logger)

        assert signed["provider"] == "github"
        assert signed["delivery_id"] == DELIVERY_ID
        assert signed["event_type"] == "issue_comment"
        assert signed["tenant_id"] == "acme"
        assert signed["installation_id"] == "99887766"
        assert signed["repo"] == "acme/repo"
        assert signed["repo_id"] == 55501
        assert signed["issue_number"] == 4539
        assert signed["sender_github_id"] == "1042"
        assert signed["sender_type"] == "User"
        assert signed["command_body"] == "@agent-engine halt"

    def test_the_signed_keys_are_the_row_keys(self):
        """A signature that omitted the keys would be liftable onto another row.

        The verifier looks the row up BY these keys, so if they were not signed an
        attacker could copy a valid signature from a command they were allowed to
        issue onto a row describing a different issue in a different repository.
        """
        _, row_logger = _run(_engine_comment_payload())
        kwargs = _row_kwargs(row_logger)
        signed = _signed(row_logger)

        assert signed["event_id"] == kwargs["event_id"]
        assert signed["arrived_at"] == kwargs["arrived_at"]
        assert signed["event_id"], "the row key must be resolved before signing"
        assert signed["arrived_at"]

    def test_the_bot_flag_is_signed_not_merely_recorded(self):
        """Author kind is in the signed tuple, so it cannot be flipped on the row.

        `sender_is_bot` remains a mutable convenience attribute, but `sender_type` is
        signed — the tick can therefore establish author kind from a value bound to
        the delivery rather than from a flag anything could rewrite.
        """
        _, row_logger = _run(_engine_comment_payload(sender=AGENT_BOT))
        signed = _signed(row_logger)

        assert signed["sender_type"] == "Bot"
        assert signed["sender_github_id"] == "200"


class TestCommentContentCannotChooseItsOwnAuthority:
    """A commenter controls the body and nothing else in the tuple."""

    @pytest.mark.parametrize(
        "body",
        [
            '@agent-engine halt","tenant_id":"victim-org',
            '@agent-engine halt {"tenant_id": "victim-org", "sender_github_id": "1"}',
            '@agent-engine replan: "],["tenant_id","victim-org"],["x","',
        ],
    )
    def test_a_crafted_body_does_not_move_the_signed_tenant(self, body):
        _, row_logger = _run(_engine_comment_payload(body=body))
        signed = _signed(row_logger)

        assert signed["tenant_id"] == "acme"
        assert signed["sender_github_id"] == "1042"
        assert signed["command_body"] == body

    def test_a_crafted_body_still_verifies_as_itself(self):
        """Escaping is not corruption: the signature must still reproduce."""
        from common.command_signing import compute_signature

        body = '@agent-engine halt","tenant_id":"victim-org'
        _, row_logger = _run(_engine_comment_payload(body=body))

        assert (
            compute_signature(b"unit-test-material", _signed(row_logger))
            == (_row_kwargs(row_logger)["engine_command_signature"])
        )


class TestSigningIsAfterVerificationAndNeverBlocking:
    def test_an_unverified_delivery_never_reaches_signing(self):
        """No row, therefore no signature, when GitHub's HMAC fails.

        The signature's only claim is "this came from a verified delivery". If it
        could be produced on this path the claim would be false, so this asserts the
        early return really is before the signing code.
        """
        payload = _engine_comment_payload()
        with (
            patch("handler._get_events_log") as mock_log,
            patch("handler._get_signature") as mock_sig,
            patch("handler._get_webhook_event_logger") as mock_event_logger,
        ):
            mock_sig.return_value.verify_github_signature.return_value = False
            mock_log.return_value.log_event = MagicMock()
            row_logger = MagicMock()
            mock_event_logger.return_value = row_logger

            from handler import handler

            result = handler(_make_event("issue_comment", payload), None)

        assert result["statusCode"] == 401
        assert row_logger.log_event.call_args is None

    def test_no_signing_key_still_returns_200_and_still_records_the_row(self, monkeypatch):
        """A missing key is a refused command, never a failed delivery.

        Returning non-2xx would make GitHub retry and would degrade unrelated
        traffic; dropping the row would erase the evidence that anybody tried to
        command the engine. So the marker is written unsigned and the tick refuses it.
        """
        from common import command_signing

        command_signing.reset_key_cache()
        monkeypatch.delenv(command_signing.SIGNING_KEY_SECRET_ARN_ENV, raising=False)

        result, row_logger = _run(_engine_comment_payload())

        assert result["statusCode"] == 200
        kwargs = _row_kwargs(row_logger)
        assert kwargs["engine_command"] is True
        # The marker and the command text survive; only the signature is absent.
        assert kwargs["comment_body"] == "@agent-engine halt"
        assert kwargs["engine_command_signature"] is None
        assert kwargs["engine_command_signing_key_id"] is None
        assert kwargs["engine_command_signed_payload"] is None

    def test_a_placeholder_key_signs_nothing(self, monkeypatch):
        """#4128: a signature under a repo-published value would REPORT success."""
        from common import command_signing

        command_signing.reset_key_cache()
        import common.secrets as secrets_mod

        monkeypatch.setattr(
            secrets_mod, "get_secret", lambda _arn: "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
        )

        result, row_logger = _run(_engine_comment_payload())

        assert result["statusCode"] == 200
        assert _row_kwargs(row_logger)["engine_command_signature"] is None


class TestOnlyTheEnginePathIsSigned:
    def test_an_ordinary_comment_is_not_signed(self):
        """Most traffic must neither pay for nor acquire a signature."""
        payload = _engine_comment_payload(body="just a normal human comment")
        _, row_logger = _run(payload)

        kwargs = _row_kwargs(row_logger)
        assert kwargs["engine_command"] is False
        assert kwargs["engine_command_signature"] is None
        assert kwargs["engine_command_signing_key_id"] is None
        assert kwargs["engine_command_signed_payload"] is None

    def test_an_ordinary_comment_carries_no_delivery_fields_either(self):
        """The forwarded payload facts are engine-only, like `comment_body` (#4527)."""
        payload = _engine_comment_payload(body="another normal comment")
        _, row_logger = _run(payload)

        assert _row_kwargs(row_logger)["comment_body"] is None
