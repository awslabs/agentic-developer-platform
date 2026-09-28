"""Placeholder-key guard for marker verification — Issue #4128.

The blocking prerequisite for #4073 findings #18 + #1b.

``infra/secrets.tf`` seeds the marker-signing secret with the literal string
``PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND`` and marks ``secret_string`` as
``ignore_changes``, so an operator is expected to replace it out-of-band. In any
environment where that never happened, the placeholder IS the live secret value
— and it is readable by anyone with the repo.

Before this change ``_load_verification_keys()`` returned ``[]`` only when the
env var was unset or the read threw, so the placeholder loaded as a genuine HMAC
key and ``verify_marker`` returned a confident ``True``/``False`` computed
against a public secret. That is strictly worse than not verifying: it reports
success. These tests prove the hole is closed — the verdict must be ``None``
(indeterminate) so the caller's fail-closed policy applies instead.

Note this inverts the premise of #4073's adopted decision 5, which assumed the
placeholder already produced ``None``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.marker_parse import parse_marker  # noqa: E402
from common.marker_verify import (  # noqa: E402
    _is_placeholder_key,
    _load_verification_keys,
    reset_key_cache,
    verify_marker,
)

# The exact literal Terraform ships (infra/secrets.tf).
PLACEHOLDER = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
REAL_KEY = "a-real-generated-key-32-bytes!!!"
TEST_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:marker-ph"


@pytest.fixture(autouse=True)
def _clear_key_cache():
    reset_key_cache()
    yield
    reset_key_cache()


def _sign(key: str, marker_fields: dict) -> str:
    """Sign using the canonical input format (worker's marker_signing.py)."""
    signing_input = (
        f"{marker_fields['correlation_id']}:{marker_fields['root_human_id']}"
        f":{marker_fields['is_human_rooted']}:{marker_fields['invocation_id']}"
        f":{marker_fields['chain_depth']}"
    )
    sig = hmac.new(
        key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


_FIELDS = {
    "correlation_id": "corr-ph-001",
    "root_human_id": "victim-human",
    "is_human_rooted": "true",
    "invocation_id": "msg-ph",
    "chain_depth": "1",
}


def _marker_text(signature: str | None) -> str:
    parts = [
        f"adp-correlation:{_FIELDS['correlation_id']}",
        f"adp-root-human:{_FIELDS['root_human_id']}",
        f"adp-is-human-rooted:{_FIELDS['is_human_rooted']}",
        f"adp-invocation:{_FIELDS['invocation_id']}",
        f"adp-chain-depth:{_FIELDS['chain_depth']}",
    ]
    if signature:
        parts.append(f"adp-sig:{signature}")
    return f"<!-- {' '.join(parts)} -->"


def _mock_sm(current: str, previous: str | None = None) -> MagicMock:
    client = MagicMock()

    def _get(**kwargs):
        stage = kwargs.get("VersionStage")
        if stage == "AWSCURRENT":
            return {"SecretString": current}
        if stage == "AWSPREVIOUS" and previous is not None:
            return {"SecretString": previous}
        raise Exception("No such version")

    client.get_secret_value.side_effect = _get
    return client


class TestIsPlaceholderKey:
    """The predicate itself."""

    def test_terraform_literal_is_placeholder(self):
        assert _is_placeholder_key(PLACEHOLDER) is True

    def test_placeholder_with_surrounding_whitespace(self):
        """A trailing newline from a shell heredoc must not defeat the guard."""
        assert _is_placeholder_key(f"  {PLACEHOLDER}\n") is True

    def test_empty_secret_is_placeholder(self):
        """An empty HMAC key is not a meaningful secret either."""
        assert _is_placeholder_key("") is True
        assert _is_placeholder_key("   \n") is True

    def test_real_key_is_not_placeholder(self):
        assert _is_placeholder_key(REAL_KEY) is False


class TestPlaceholderYieldsNoKeys:
    """_load_verification_keys must refuse the placeholder."""

    def test_placeholder_current_loads_no_keys(self):
        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "common.secrets._get_client", return_value=_mock_sm(PLACEHOLDER)
            ):
                assert _load_verification_keys() == []

    def test_real_current_loads_one_key(self):
        """Control: a real key still loads, so the guard is not a blanket break."""
        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                assert _load_verification_keys() == [REAL_KEY.encode("utf-8")]

    def test_placeholder_as_previous_version_is_not_a_grace_key(self):
        """Rotating AWAY from the placeholder leaves it as AWSPREVIOUS.

        The 7-day rotation grace window must not resurrect it as an accepted
        key — otherwise the public secret keeps verifying for a week after the
        operator fixes the env.
        """
        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "common.secrets._get_client",
                return_value=_mock_sm(REAL_KEY, previous=PLACEHOLDER),
            ):
                keys = _load_verification_keys()
        assert keys == [REAL_KEY.encode("utf-8")]


class TestVerifyMarkerNeverTrustsPlaceholder:
    """The property the issue asks to prove: never True/False on a public key."""

    def test_marker_signed_with_placeholder_returns_none_not_true(self):
        """THE HOLE: an attacker signs with the repo-readable placeholder.

        Pre-fix this returned True — a forged human-rooted claim reported as
        VERIFIED, against a secret anyone can read out of infra/secrets.tf.
        """
        forged_sig = _sign(PLACEHOLDER, _FIELDS)
        marker = parse_marker(_marker_text(forged_sig))
        assert marker is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "common.secrets._get_client", return_value=_mock_sm(PLACEHOLDER)
            ):
                verdict = verify_marker(marker)

        assert verdict is None, "placeholder key must never produce a verified verdict"
        assert verdict is not True

    def test_unrelated_signature_under_placeholder_returns_none_not_false(self):
        """Indeterminate, not a confident rejection.

        A False here would also be a lie — it would be a verdict computed
        against a key that is not a secret at all. Only None is honest.
        """
        marker = parse_marker(_marker_text("some-unrelated-signature-value"))
        assert marker is not None

        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        with patch.dict(os.environ, env, clear=False):
            with patch(
                "common.secrets._get_client", return_value=_mock_sm(PLACEHOLDER)
            ):
                verdict = verify_marker(marker)

        assert verdict is None
        assert verdict is not False

    def test_real_key_still_verifies_and_still_rejects(self):
        """Control: with a real key, verification is fully operative.

        Guards against a fix that makes verify_marker uselessly return None
        everywhere — the per-env enablement in the rollout depends on this
        actually working once a real key is set.
        """
        env = {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}
        good = parse_marker(_marker_text(_sign(REAL_KEY, _FIELDS)))
        bad = parse_marker(_marker_text(_sign(PLACEHOLDER, _FIELDS)))

        with patch.dict(os.environ, env, clear=False):
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                assert verify_marker(good) is True
                reset_key_cache()
            with patch("common.secrets._get_client", return_value=_mock_sm(REAL_KEY)):
                # Signed with the placeholder, verified against the real key.
                assert verify_marker(bad) is False
