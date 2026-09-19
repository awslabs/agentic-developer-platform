"""Build genuinely signed engine-command rows for tests (issue #4539).

Shared, not copied into each test module, for one reason: every test that drives
`_handle_row` now has to get past attribution verification first, and a per-file
copy of "how a signed row is shaped" is exactly how one file ends up asserting
against a row shape production never writes.

The key here is the contract's own test key, so a row built by this helper is signed
the same way `test_command_attribution.py` and the webhook-ingress signer's tests
sign — one definition of the canonical form across all three.

**Nothing here is real key material.** The value comes from
`contracts/engine-command-envelope/v1/engine-command-envelope.golden.json`, whose
`test_signing_key` is a published fixture string; the signer and verifier both refuse
known placeholder values, and this is deliberately not one of those either, so the
fixtures exercise the real code path rather than the placeholder guard.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from src.orchestration import command_attribution
from src.orchestration.command_attribution import (
    KEY_ID_ATTR,
    PROTOCOL_VERSION_ATTR,
    SIGNATURE_ATTR,
    SIGNED_PAYLOAD_ATTR,
    canonical_bytes,
)

# tests/orchestration/[0] tests/[1] gateway/[2] modules/[3] <repo root>/[4]
_CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "engine-command-envelope" / "v1" / "engine-command-envelope.golden.json"


def contract() -> dict[str, Any]:
    """The shared golden fixture, or a loud failure.

    An assertion rather than a skip: skipping would silently stop these tests
    exercising the real verification path, and they would then pass against rows no
    publisher writes.
    """
    assert _CONTRACT.is_file(), f"missing {_CONTRACT} — this helper's path arithmetic is stale. Fix the path rather than skipping."
    return json.loads(_CONTRACT.read_text(encoding="utf-8"))


def signing_key() -> bytes:
    """The contract's fixture key material.

    NOT named `test_key`: pytest collects any `test_*` name a test module imports, so
    that spelling turns a helper into a spurious "test" that returns bytes.
    """
    return contract()["test_signing_key"].encode("utf-8")


def envelope(**overrides: Any) -> dict[str, Any]:
    """The contract's first vector, with any field replaced."""
    base = dict(contract()["vectors"][0]["envelope"])
    base.update(overrides)
    return base


def sign(envelope_dict: dict[str, Any], *, key: bytes | None = None) -> str:
    """Sign an envelope the way the publisher does.

    Computed here rather than read from the fixture so that a test overriding a field
    still gets a VALID signature — otherwise every override would be testing the
    tampering path by accident.
    """
    digest = hmac.new(
        key if key is not None else signing_key(),
        canonical_bytes(envelope_dict),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def signed_row(
    envelope_dict: dict[str, Any] | None = None,
    *,
    key: bytes | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A marked, signed row exactly as the webhook Lambda writes one.

    `overrides` are applied to the ROW after signing, so passing one is how a test
    creates a row that disagrees with its own signed tuple.
    """
    env = envelope_dict if envelope_dict is not None else envelope()
    row = {
        "event_id": env["event_id"],
        "arrived_at": env["arrived_at"],
        "tenant_id": env["tenant_id"],
        "repo": env["repo"],
        "issue_number": env["issue_number"],
        "installation_id": env["installation_id"],
        "engine_command_status": "pending",
        "engine_command_body": env["command_body"],
        "engine_command_sender_github_id": env["sender_github_id"],
        "engine_command_sender_is_bot": env["sender_type"] == "Bot",
        SIGNATURE_ATTR: sign(env, key=key),
        KEY_ID_ATTR: env["key_id"],
        SIGNED_PAYLOAD_ATTR: canonical_bytes(env).decode("utf-8"),
        PROTOCOL_VERSION_ATTR: env["protocol_version"],
    }
    row.update(overrides)
    return row


@pytest.fixture
def signing_keyring(monkeypatch: pytest.MonkeyPatch):
    """Seed the contract's test key as the active key and clear the module cache.

    NOT autouse: a module importing this helper opts in explicitly, because a test
    that wants the genuine "no key configured" refusal must be able to run without
    it. The cache is cleared on both sides — it caches failure too, so a test running
    after a no-key test would otherwise inherit "no key" and pass for the wrong
    reason.
    """
    command_attribution.reset_key_cache()
    monkeypatch.setattr(
        command_attribution,
        "_load_keyring",
        lambda: ("2026-09", {"2026-09": signing_key()}, None),
    )
    yield
    command_attribution.reset_key_cache()
