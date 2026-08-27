"""determine_correlation verifies provenance on ALL THREE paths — Issue #4128.

``verify_marker`` previously ran ONLY in the Rule-4 marker-only branch
(handler.py). Three other paths read marker- or pointer-borne provenance with no
verification at all:

  - Rule 1 (pointer + marker, SAME correlation_id) — the marker supplies the
    ``parent_invocation_id`` fallback.
  - Rule 2 (pointer + marker, DIFFERENT correlation_id) — the marker's OWN
    ``correlation_id`` / ``root_human_id`` / ``is_human_rooted`` WIN over the
    server-written pointer's. This is the third path, and it is the one #4073's
    design did not name.
  - Rule 3 (pointer only) — the fail-closed landing site for a rejected claim.

``marker_text`` comes from a GitHub comment body or PR body, i.e. from anyone who
can comment on the repo. These tests assert that an unverified claim cannot move
the chain or its root human.

CI path: under ``lambda/`` so ``webhook-ingress-ci.yml``'s
``pytest lambda/ -m "not integration"`` executes it.
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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.marker_verify import reset_key_cache  # noqa: E402
from handler import determine_correlation  # noqa: E402

REAL_KEY = "a-real-generated-key-32-bytes!!!"
PLACEHOLDER = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
TEST_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123:secret:handler-prov"

VICTIM = "victim-human-id"
CHANNEL = "github:repo=org/repo,issue=1"


@pytest.fixture(autouse=True)
def _clean():
    reset_key_cache()
    yield
    reset_key_cache()


class _Identity:
    def __init__(self, user_kind="bot", user_id="bot-sender"):
        self.user_kind = user_kind
        self.user_id = user_id


def _sign(key, correlation_id, root_human_id, is_human_rooted, invocation_id, chain_depth):
    signing_input = (
        f"{correlation_id}:{root_human_id}:{is_human_rooted}:{invocation_id}:{chain_depth}"
    )
    sig = hmac.new(key.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")


def _marker_text(
    *,
    correlation_id,
    root_human_id=VICTIM,
    is_human_rooted="true",
    invocation_id="inv-marker",
    chain_depth="1",
    key: str | None = None,
) -> str:
    parts = [
        f"adp-correlation:{correlation_id}",
        f"adp-root-human:{root_human_id}",
        f"adp-is-human-rooted:{is_human_rooted}",
        f"adp-invocation:{invocation_id}",
        f"adp-chain-depth:{chain_depth}",
    ]
    if key is not None:
        parts.append(
            "adp-sig:"
            + _sign(key, correlation_id, root_human_id, is_human_rooted, invocation_id, chain_depth)
        )
    return f"<!-- {' '.join(parts)} -->"


def _pointer(**overrides) -> dict:
    defaults = {
        "correlation_id": "corr-POINTER",
        "root_human_id": "real-human",
        "is_human_rooted": True,
        "triggering_invocation_id": "inv-pointer",
        "chain_depth": 2,
        "last_triggered_persona": None,
        "recent_triggered_personas": set(),
        "recent_trigger_count": 0,
    }
    defaults.update(overrides)
    return defaults


def _mock_sm(secret_value: str) -> MagicMock:
    client = MagicMock()

    def _get(**kwargs):
        if kwargs.get("VersionStage") == "AWSCURRENT":
            return {"SecretString": secret_value}
        raise Exception("no previous version")

    client.get_secret_value.side_effect = _get
    return client


def _run(pointer, marker_text, *, secret=REAL_KEY, marker_trusted=False):
    """Invoke determine_correlation with a stubbed pointer store + signing key."""
    store = MagicMock()
    store.read_pointer.return_value = pointer

    with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
        with patch("common.secrets._get_client", return_value=_mock_sm(secret)):
            with patch("handler._get_correlation_store", return_value=store):
                return determine_correlation(
                    {},
                    _Identity(),
                    CHANNEL,
                    marker_text=marker_text,
                    marker_trusted=marker_trusted,
                )


# =============================================================================
# Path 1 — pointer + marker, SAME correlation_id
# =============================================================================


class TestPath1PointerPlusMatchingMarker:
    """Rule 1: the marker supplies the parent_invocation_id fallback."""

    def test_forged_marker_cannot_supply_parent_invocation(self):
        """A forged marker no longer injects a lineage edge on the Rule-1 path.

        The pointer here has NO triggering_invocation_id, which is exactly when
        the marker's invocation_id was used as the fallback (#1738).
        """
        pointer = _pointer(correlation_id="corr-SAME", triggering_invocation_id=None)
        marker = _marker_text(
            correlation_id="corr-SAME", invocation_id="inv-FORGED", key="attacker-key-xxx"
        )
        ctx = _run(pointer, marker)

        assert ctx["parent_invocation_id"] != "inv-FORGED"
        # Pointer remains authoritative for the chain itself.
        assert ctx["correlation_id"] == "corr-SAME"
        assert ctx["root_human_id"] == "real-human"

    def test_validly_signed_marker_still_supplies_parent(self):
        """Regression #1738: the issue→PR parent edge still populates."""
        pointer = _pointer(correlation_id="corr-SAME", triggering_invocation_id=None)
        marker = _marker_text(
            correlation_id="corr-SAME", invocation_id="inv-REAL", key=REAL_KEY
        )
        ctx = _run(pointer, marker)
        assert ctx["parent_invocation_id"] == "inv-REAL"

    def test_server_synthesized_marker_is_trusted_unsigned(self):
        """Regression #1731/#1735: the PR fallback marker is unsigned by design.

        It is built server-side from a server-written pointer, so marker_trusted
        short-circuits verification. Without this the issue→PR lineage would
        break the moment verification was switched on.
        """
        pointer = _pointer(correlation_id="corr-SAME", triggering_invocation_id=None)
        marker = _marker_text(correlation_id="corr-SAME", invocation_id="inv-SYNTH", key=None)
        ctx = _run(pointer, marker, marker_trusted=True)
        assert ctx["parent_invocation_id"] == "inv-SYNTH"


# =============================================================================
# Path 2 — pointer + marker, DIFFERENT correlation_id (the unnamed third path)
# =============================================================================


class TestPath2CrossChannelHop:
    """Rule 2: the marker overrides the pointer's chain entirely."""

    def test_forged_marker_cannot_redirect_the_chain(self):
        """THE HOLE: a forged cross-channel claim rewrote correlation + root human.

        Pre-fix this branch returned the MARKER's correlation_id and
        root_human_id with no verification, so any commenter could redirect a
        chain onto a fabricated one rooted at any human they named.
        """
        pointer = _pointer(correlation_id="corr-POINTER", root_human_id="real-human")
        marker = _marker_text(
            correlation_id="corr-ATTACKER",
            root_human_id=VICTIM,
            is_human_rooted="true",
            key="attacker-key-xxx",
        )
        ctx = _run(pointer, marker)

        assert ctx["correlation_id"] == "corr-POINTER", "forged marker redirected the chain"
        assert ctx["root_human_id"] == "real-human"
        assert ctx["root_human_id"] != VICTIM

    def test_unsigned_marker_cannot_claim_human_rooted(self):
        """Unsigned cross-channel claim → authority stripped (fail-closed).

        Same policy #3179 set for Rule 4. Lineage may continue; human authority
        may not be asserted without a signature.
        """
        pointer = _pointer(correlation_id="corr-POINTER")
        marker = _marker_text(
            correlation_id="corr-OTHER", root_human_id=VICTIM, is_human_rooted="true", key=None
        )
        ctx = _run(pointer, marker)

        assert ctx["correlation_id"] == "corr-OTHER"  # lineage continues
        assert ctx["is_human_rooted"] is False, "unsigned marker claimed human authority"

    def test_placeholder_key_does_not_bless_a_cross_channel_claim(self):
        """Signed with the public placeholder → indeterminate, not verified."""
        pointer = _pointer(correlation_id="corr-POINTER")
        marker = _marker_text(
            correlation_id="corr-OTHER",
            root_human_id=VICTIM,
            is_human_rooted="true",
            key=PLACEHOLDER,
        )
        ctx = _run(pointer, marker, secret=PLACEHOLDER)
        assert ctx["is_human_rooted"] is False

    def test_validly_signed_cross_channel_hop_still_works(self):
        """Regression: a legitimate signed cross-channel hop is preserved."""
        pointer = _pointer(correlation_id="corr-POINTER")
        marker = _marker_text(
            correlation_id="corr-OTHER",
            root_human_id="real-human",
            is_human_rooted="true",
            key=REAL_KEY,
        )
        ctx = _run(pointer, marker)
        assert ctx["correlation_id"] == "corr-OTHER"
        assert ctx["root_human_id"] == "real-human"
        assert ctx["is_human_rooted"] is True


# =============================================================================
# Path 3 — pointer only (landing site for rejected claims)
# =============================================================================


class TestPath3PointerOnly:
    """Rule 3: a discarded forged marker degrades to the pointer, not to a mint."""

    def test_forged_marker_falls_back_to_pointer_not_new_chain(self):
        pointer = _pointer(correlation_id="corr-POINTER", chain_depth=3)
        marker = _marker_text(correlation_id="corr-ATTACKER", key="attacker-key-xxx")
        ctx = _run(pointer, marker)

        assert ctx["correlation_id"] == "corr-POINTER"
        assert ctx["is_new_chain"] is False
        assert ctx["chain_depth"] == 4  # inherited from the pointer, not reset

    def test_pointer_only_no_marker_unaffected(self):
        """Regression: the plain pointer-only path is unchanged."""
        ctx = _run(_pointer(correlation_id="corr-POINTER", chain_depth=1), None)
        assert ctx["correlation_id"] == "corr-POINTER"
        assert ctx["chain_depth"] == 2


# =============================================================================
# Path 4 — marker only (already verified pre-#4128; must not regress)
# =============================================================================


class TestPath4MarkerOnly:
    """Rule 4 behaviour from #3179 is preserved by the refactor."""

    def test_forged_marker_only_mints_new_bot_chain(self):
        marker = _marker_text(correlation_id="corr-ATTACKER", key="attacker-key-xxx")
        ctx = _run(None, marker)
        assert ctx["is_new_chain"] is True
        assert ctx["is_human_rooted"] is False
        assert ctx["chain_depth"] == 0

    def test_unsigned_marker_only_strips_human_rooted(self):
        marker = _marker_text(correlation_id="corr-U", root_human_id=VICTIM, key=None)
        ctx = _run(None, marker)
        assert ctx["correlation_id"] == "corr-U"
        assert ctx["is_human_rooted"] is False

    def test_signed_marker_only_keeps_authority(self):
        marker = _marker_text(correlation_id="corr-S", root_human_id="real-human", key=REAL_KEY)
        ctx = _run(None, marker)
        assert ctx["correlation_id"] == "corr-S"
        assert ctx["is_human_rooted"] is True
        assert ctx["root_human_id"] == "real-human"


# =============================================================================
# Human senders are unaffected
# =============================================================================


class TestHumanSenderUnaffected:
    def test_human_sender_still_mints_new_human_rooted_chain(self):
        """Regression: a human's own action needs no marker signature."""
        store = MagicMock()
        store.read_pointer.return_value = _pointer()
        with patch("handler._get_correlation_store", return_value=store):
            ctx = determine_correlation(
                {},
                _Identity(user_kind="human", user_id="human-42"),
                CHANNEL,
                marker_text=_marker_text(correlation_id="corr-X", key=None),
            )
        assert ctx["is_new_chain"] is True
        assert ctx["is_human_rooted"] is True
        assert ctx["root_human_id"] == "human-42"
