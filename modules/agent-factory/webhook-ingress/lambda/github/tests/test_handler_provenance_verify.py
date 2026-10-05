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

# Issue #5663 (A09): lineage authority is now also bound to the job's own
# tenant/installation/repo. These #4128 marker-verification tests are not about that
# predicate, so the stubbed chain row and the job context are kept CONSISTENT —
# otherwise every case here would pass for the new reason rather than the one it
# documents. The predicate itself is asserted in test_handler_lineage_context_5663.py.
TENANT = "org"
REPO = "org/repo"
INSTALLATION = "4242"


@pytest.fixture(autouse=True)
def _clean():
    reset_key_cache()
    yield
    reset_key_cache()


class _Identity:
    def __init__(self, user_kind="bot", user_id="bot-sender", tenant_id=TENANT):
        self.user_kind = user_kind
        self.user_id = user_id
        self.tenant_id = tenant_id  # Issue #5663: server-resolved tenant of the job


def _payload() -> dict:
    """Signature-verified payload fields the A09 job context is derived from."""
    return {"repository": {"full_name": REPO}, "installation": {"id": INSTALLATION}}


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


def _chain_row(
    correlation_id: str,
    *,
    root_human_id: str | None = "real-human",
    is_human_rooted: bool | None = True,
    chain_depth: int | None = 2,
    event_id: str = "evt-chain",
) -> dict:
    """A server-written ``webhook-events`` row, in this job's own tenant/repo.

    Issue #5663 (A09): the MARKER paths (Rules 2 and 4) now resolve their human
    authority from this row too, not from the marker's claim — a valid marker
    signature is not an authority boundary, because the signed input names no tenant
    and the signing key is fleet-wide. So the marker-only and cross-channel cases
    below must stub a row for the marker's OWN correlation_id, exactly as #4129
    already required for the pointer paths. Same reasoning as ``_chain_from``: keep
    the row consistent with the job context so each test still fails for the reason
    it documents.
    """
    return {
        "event_id": event_id,
        "correlation_id": correlation_id,
        "root_human_id": root_human_id,
        "is_human_rooted": is_human_rooted,
        "chain_depth": chain_depth,
        "tenant_id": TENANT,
        "installation_id": INSTALLATION,
        "repo": REPO,
    }


def _chain_from(pointer: dict | None) -> dict | None:
    """The server-written webhook-events row a legitimate pointer corresponds to.

    Issue #4129: chain provenance is now read from the ``correlation-index`` GSI
    rather than off the pointer row, so these #4128 tests must stub BOTH. Mirroring
    the pointer's values here keeps each test asserting what it was written to
    assert (marker-verification behaviour) rather than accidentally re-testing
    #4129's fail-closed path.
    """
    if pointer is None:
        return None
    return {
        "event_id": pointer.get("triggering_invocation_id") or "evt-chain",
        "correlation_id": pointer["correlation_id"],
        "root_human_id": pointer.get("root_human_id"),
        "is_human_rooted": pointer.get("is_human_rooted"),
        "chain_depth": pointer.get("chain_depth"),
        # Issue #5663: matches ``_payload()`` / ``_Identity`` — see the note above.
        "tenant_id": TENANT,
        "installation_id": INSTALLATION,
        "repo": REPO,
    }


def _run(
    pointer,
    marker_text,
    *,
    secret=REAL_KEY,
    marker_trusted=False,
    chain="from-pointer",
    marker_chain=None,
):
    """Invoke determine_correlation with a stubbed pointer store + signing key.

    Args:
        chain: The server-written chain row for the POINTER's correlation_id.
            Defaults to one mirroring ``pointer`` (see :func:`_chain_from`).
        marker_chain: Issue #5663 — the row for the MARKER's correlation_id, when
            that differs from the pointer's. ``None`` means "the webhook has no
            record of the chain the marker names", which is the realistic shape for
            a fabricated marker and now correctly confers no human authority.

    The lookup is keyed BY CORRELATION ID rather than returning one row for every
    call. That precision matters here: with a single fixed row, a test asserting a
    cross-channel hop into ``corr-OTHER`` would be silently answered with
    ``corr-POINTER``'s row and pass without exercising the hop at all.
    """
    store = MagicMock()
    store.read_pointer.return_value = pointer
    pointer_chain = _chain_from(pointer) if chain == "from-pointer" else chain

    rows: dict[str, dict] = {}
    for row in (pointer_chain, marker_chain):
        if row:
            rows[str(row.get("correlation_id") or "")] = row

    with patch.dict(os.environ, {"MARKER_SIGNING_KEY_SECRET_ARN": TEST_SECRET_ARN}):
        with patch("common.secrets._get_client", return_value=_mock_sm(secret)):
            with patch("handler._get_correlation_store", return_value=store):
                with patch(
                    "handler._resolve_chain_record",
                    side_effect=lambda cid: rows.get(str(cid or "")),
                ):
                    return determine_correlation(
                        _payload(),
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
        # #5663: a real server-written row for corr-OTHER, rooted at a DIFFERENT human
        # than the marker names. Pre-#5663 this test passed because the marker was
        # unsigned; it now passes for the stronger reason that the marker's claim is
        # not consulted at all. Supplying the row is what makes that visible — without
        # it the assertion would be satisfied by the no-chain fail-closed path instead.
        marker_chain = _chain_row("corr-OTHER", root_human_id="real-human")
        ctx = _run(pointer, marker, marker_chain=marker_chain)

        assert ctx["correlation_id"] == "corr-OTHER"  # lineage continues
        assert ctx["is_human_rooted"] is True  # ...from the SERVER row, not the marker
        assert ctx["root_human_id"] == "real-human"
        assert ctx["root_human_id"] != VICTIM, "unsigned marker claimed human authority"

    def test_placeholder_key_does_not_bless_a_cross_channel_claim(self):
        """Signed with the public placeholder → indeterminate, not verified."""
        pointer = _pointer(correlation_id="corr-POINTER")
        marker = _marker_text(
            correlation_id="corr-OTHER",
            root_human_id=VICTIM,
            is_human_rooted="true",
            key=PLACEHOLDER,
        )
        # #5663: as above — the row for corr-OTHER exists and names a different human,
        # so the assertion is about WHOSE authority is carried, not merely about the
        # absence of any.
        ctx = _run(
            pointer,
            marker,
            secret=PLACEHOLDER,
            marker_chain=_chain_row("corr-OTHER", root_human_id="real-human"),
        )
        assert ctx["root_human_id"] == "real-human"
        assert ctx["root_human_id"] != VICTIM

    def test_validly_signed_cross_channel_hop_still_works(self):
        """Regression: a legitimate signed cross-channel hop is preserved."""
        pointer = _pointer(correlation_id="corr-POINTER")
        marker = _marker_text(
            correlation_id="corr-OTHER",
            root_human_id="real-human",
            is_human_rooted="true",
            key=REAL_KEY,
        )
        ctx = _run(
            pointer, marker, marker_chain=_chain_row("corr-OTHER", root_human_id="real-human")
        )
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
        # Inherited from the pointer's chain, not reset (#4268: unchanged, not +1).
        assert ctx["chain_depth"] == 3

    def test_pointer_only_no_marker_unaffected(self):
        """Regression: the plain pointer-only path is unchanged."""
        ctx = _run(_pointer(correlation_id="corr-POINTER", chain_depth=1), None)
        assert ctx["correlation_id"] == "corr-POINTER"
        assert ctx["chain_depth"] == 1  # inherited unchanged (#4268)


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
        """A legitimate marker-only hop still inherits its human — via the chain row.

        #5663: the authority now comes from the server-written row for the marker's
        correlation_id rather than from the signed marker itself, so this regression
        case must stub that row. The marker still selects WHICH chain; it no longer
        states whose authority the chain carries.
        """
        marker = _marker_text(correlation_id="corr-S", root_human_id="real-human", key=REAL_KEY)
        ctx = _run(None, marker, marker_chain=_chain_row("corr-S", root_human_id="real-human"))
        assert ctx["correlation_id"] == "corr-S"
        assert ctx["is_human_rooted"] is True
        assert ctx["root_human_id"] == "real-human"

    def test_a_signed_marker_naming_another_tenants_human_gets_nothing(self):
        """#5663: the escalation a valid signature used to walk straight through.

        The signing key is one fleet-wide secret and the signed input names no
        tenant, so a worker in ANY tenant can mint a valid signature over ANY human's
        id. Authority must therefore come from the chain row and be refused when that
        row belongs to another tenant.
        """
        marker = _marker_text(correlation_id="corr-X", root_human_id=VICTIM, key=REAL_KEY)
        foreign = _chain_row("corr-X", root_human_id=VICTIM)
        foreign["tenant_id"] = "another-tenant"
        foreign["repo"] = "another-tenant/secrets"
        ctx = _run(None, marker, marker_chain=foreign)

        assert ctx["is_human_rooted"] is False
        assert ctx["root_human_id"] != VICTIM
        # Lineage still connects — the refusal narrows authority, it does not fragment.
        assert ctx["correlation_id"] == "corr-X"


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
