"""The tick ignores bot-authored engine commands (issue #4599).

A live engine command is a *human* act. The platform's own agents narrate what
commands do — in status comments, plans and design notes — and until #4599 every
such narration was marked pending, parsed, refused and answered with "this command
cannot be applied by this account" on the issue thread.

**These tests pin a NOISE filter, not an authorization boundary**, and that
distinction is the reason this file has a docstring. Authority already holds without
the guard: bot identities seed with ``role="agent"``, which is absent from
``_MEMBERSHIP_ROLE_TO_ADMIN_ROLE``, so ``membership_role_to_admin_role`` fails closed
to ``MEMBER``, which lacks ``PLAN_APPROVE``. Deleting the guard makes threads noisy
again; it does not make a bot able to approve anything. Nobody reading these tests
should relax that RBAC on the strength of them.

The guard runs before the parse and before any database call, so a bot row needs no
session, no installation and no identity — which is itself the property under test:
the cheapest possible exit for input that should produce nothing.

**Updated for issue #4539.** Author kind is now read from the SIGNED ``sender_type``
rather than from the row's mutable ``engine_command_sender_is_bot`` flag, so these
rows are genuinely signed (via ``signed_command_rows``) and the guard is exercised
downstream of verification, which is where it now sits. Two consequences are pinned
below as tests in their own right: an unsigned row never reaches the guard at all,
and flipping the row's bot flag no longer changes the outcome.
"""

from __future__ import annotations

import pytest

from src.orchestration.engine_commands import (
    EngineCommandReport,
    _handle_row,
)

from .signed_command_rows import envelope, signed_row, signing_keyring  # noqa: F401

pytestmark = pytest.mark.usefixtures("signing_keyring")


def _row(**overrides) -> dict:
    """A marked, signed engine-command row as the webhook Lambda writes it."""
    return signed_row(envelope(command_body="@agent-engine accept"), **overrides)


def _bot_row(**envelope_overrides) -> dict:
    """A signed row whose SIGNED author kind is a bot."""
    fields = {
        "command_body": "@agent-engine accept",
        "sender_type": "Bot",
        "sender_github_id": "200",
    }
    fields.update(envelope_overrides)
    return signed_row(envelope(**fields))


class _ExplodingSession:
    """Any database use at all is a test failure.

    The point of checking author-kind first is that a bot comment costs nothing —
    no identity resolution, no installation lookup, no plan read. A session that
    raises on contact is how that stays true as the applier grows.
    """

    def __getattr__(self, name):
        raise AssertionError(f"the bot guard must not touch the session (tried {name!r})")


@pytest.mark.asyncio
class TestBotAuthoredCommandsAreConsumedQuietly:
    async def test_a_bot_row_queues_an_empty_reply(self):
        """Consumed so it stops being re-read, with no comment posted."""
        report = EngineCommandReport()

        await _handle_row(_ExplodingSession(), _bot_row(), report, access=None)

        assert len(report.pending) == 1
        # An empty message is the "consume without commenting" signal the flush
        # phase already understands — the same one a body that parses to nothing
        # uses. No new mechanism, so no new way for a reply to escape.
        assert report.pending[0].message == ""

    async def test_a_bot_row_never_posts_the_refusal(self):
        """The literal string #4589 saw on the thread must not be produced."""
        from src.orchestration.engine_commands import _UNIFORM_REFUSAL

        report = EngineCommandReport()
        await _handle_row(_ExplodingSession(), _bot_row(), report, access=None)

        assert _UNIFORM_REFUSAL not in {ack.message for ack in report.pending}

    async def test_a_bot_row_needs_no_installation(self):
        """Nothing is resolved for a comment that was never a command.

        `installation_id=None` on the ack is what tells the flush phase there is no
        credential to use, which is correct precisely because nothing is being sent.
        """
        report = EngineCommandReport()
        await _handle_row(_ExplodingSession(), _bot_row(), report, access=None)

        assert report.pending[0].installation_id is None

    async def test_the_guard_precedes_the_parse(self):
        """A bot row is dropped even when its body is a perfectly valid command.

        Otherwise the guard would only be filtering rows the parser already rejects,
        which would make it useless for the case it exists for: an agent status
        comment that legitimately contains a leading-token command.
        """
        report = EngineCommandReport()
        await _handle_row(
            _ExplodingSession(),
            _bot_row(command_body="@agent-engine halt"),
            report,
            access=None,
        )

        assert report.pending[0].message == ""

    async def test_a_bot_row_is_a_refusal_not_a_quarantine(self):
        """Issue #4539: a signed bot comment is noise, not a forgery.

        The two counters must not blur: a bot narrating a command is an ordinary,
        expected event, and counting it as a quarantine would put a constant
        background rate into the metric an operator watches for tampering.
        """
        report = EngineCommandReport()
        await _handle_row(_ExplodingSession(), _bot_row(), report, access=None)

        assert report.commands_refused == 1
        assert report.commands_quarantined == 0
        assert report.quarantined == []


@pytest.mark.asyncio
class TestAuthorKindComesFromTheSignedTuple:
    """Issue #4539: the row's own bot flag no longer decides anything.

    Before #4539 the guard read `engine_command_sender_is_bot` off the row. That flag
    is one more attribute anything able to write the row could set, so on its own it
    could be used either to silence a real human command or (once RBAC changed) to
    dress a bot up as a human. `sender_type` is inside the signed tuple.
    """

    async def test_flipping_the_row_flag_does_not_silence_a_human(self):
        """A human row with the bot flag set to True still gets past the guard.

        Proven by contrast: reaching the session is what the exploding session reports
        as the AssertionError below, so this is a positive control on the guard not
        firing on an attacker-set flag.
        """
        report = EngineCommandReport()

        with pytest.raises(AssertionError, match="must not touch the session"):
            await _handle_row(
                _ExplodingSession(),
                _row(engine_command_sender_is_bot=True),
                report,
                access=None,
            )

    async def test_clearing_the_row_flag_does_not_animate_a_bot(self):
        """A signed bot row with the flag set to False is still treated as a bot."""
        report = EngineCommandReport()
        bot = _bot_row()
        bot["engine_command_sender_is_bot"] = False

        await _handle_row(_ExplodingSession(), bot, report, access=None)

        assert report.pending[0].message == ""
        assert report.commands_refused == 1


@pytest.mark.asyncio
class TestTheGuardIsDownstreamOfVerification:
    """An unsigned row must not reach the guard at all (issue #4539).

    Not a regression in the guard, a statement about ordering: a row that never
    established it came from a verified delivery is quarantined before author kind is
    even consulted, so it produces no ack — not even an empty one.
    """

    async def test_an_unsigned_bot_row_is_quarantined_not_refused(self):
        report = EngineCommandReport()
        row = _bot_row()
        for attr in (
            "engine_command_signature",
            "engine_command_signing_key_id",
            "engine_command_signed_payload",
        ):
            row.pop(attr)

        await _handle_row(_ExplodingSession(), row, report, access=None)

        assert report.commands_quarantined == 1
        assert report.commands_refused == 0
        # No ack at all, empty or otherwise: `pending` is the list that can post.
        assert report.pending == []
        assert len(report.quarantined) == 1


@pytest.mark.asyncio
class TestSignedHumanCommandsProceed:
    """A positive control: the whole file would pass vacuously if nothing got through.

    If verification refused every row, every test above would still see "no session
    contact" and the suite would look healthy while the bridge was dead. These assert
    a signed human row does reach the session.
    """

    async def test_an_ordinary_signed_human_row_reaches_the_session(self):
        report = EngineCommandReport()

        with pytest.raises(AssertionError, match="must not touch the session"):
            await _handle_row(_ExplodingSession(), _row(), report, access=None)

    async def test_it_was_counted_as_read_before_it_got_there(self):
        """`commands_read` is recorded from the verified tuple's tenant."""
        report = EngineCommandReport()

        with pytest.raises(AssertionError):
            await _handle_row(_ExplodingSession(), _row(), report, access=None)

        assert report.commands_read == 1
        assert report.per_org["acme-corp"]["commands_read"] == 1
