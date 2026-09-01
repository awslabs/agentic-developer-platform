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
"""

from __future__ import annotations

import pytest

from src.orchestration.engine_commands import (
    EngineCommandReport,
    _handle_row,
)


def _row(**overrides) -> dict:
    """A marked engine-command row as the webhook Lambda writes it."""
    row = {
        "event_id": "evt-1",
        "arrived_at": "2026-09-01T19:58:00Z",
        "tenant_id": "org-1",
        "repo": "aws-e/adp",
        "issue_number": 4599,
        "installation_id": "555",
        "engine_command_body": "@agent-engine accept",
        "engine_command_sender_github_id": "100",
    }
    row.update(overrides)
    return row


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

        await _handle_row(
            _ExplodingSession(),
            _row(engine_command_sender_is_bot=True),
            report,
            access=None,
        )

        assert len(report.pending) == 1
        # An empty message is the "consume without commenting" signal the flush
        # phase already understands — the same one a body that parses to nothing
        # uses. No new mechanism, so no new way for a reply to escape.
        assert report.pending[0].message == ""

    async def test_a_bot_row_never_posts_the_refusal(self):
        """The literal string #4589 saw on the thread must not be produced."""
        from src.orchestration.engine_commands import _UNIFORM_REFUSAL

        report = EngineCommandReport()
        await _handle_row(
            _ExplodingSession(),
            _row(engine_command_sender_is_bot=True),
            report,
            access=None,
        )

        assert _UNIFORM_REFUSAL not in {ack.message for ack in report.pending}

    async def test_a_bot_row_needs_no_installation(self):
        """Nothing is resolved for a comment that was never a command.

        `installation_id=None` on the ack is what tells the flush phase there is no
        credential to use, which is correct precisely because nothing is being sent.
        """
        report = EngineCommandReport()
        await _handle_row(
            _ExplodingSession(),
            _row(engine_command_sender_is_bot=True),
            report,
            access=None,
        )

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
            _row(engine_command_body="@agent-engine halt", engine_command_sender_is_bot=True),
            report,
            access=None,
        )

        assert report.pending[0].message == ""


@pytest.mark.asyncio
class TestTheFlagDefaultsToHuman:
    """An absent flag must mean "human", i.e. exactly today's behaviour.

    The field is written by the webhook Lambda, a separate deploy unit that ships
    BEFORE this reader. During that window — and for every row already in the
    30-day table — the attribute is absent, and a row that predates the field must
    behave as it always did rather than being silently swallowed.

    Proven by contrast: an absent or false flag must get PAST the guard. These
    assert it reaches the session, which the exploding session reports as the
    AssertionError below — a positive control on the guard not over-firing.
    """

    async def test_an_absent_flag_does_not_short_circuit(self):
        report = EngineCommandReport()

        with pytest.raises(AssertionError, match="must not touch the session"):
            await _handle_row(_ExplodingSession(), _row(), report, access=None)

    async def test_an_explicit_false_does_not_short_circuit(self):
        report = EngineCommandReport()

        with pytest.raises(AssertionError, match="must not touch the session"):
            await _handle_row(
                _ExplodingSession(),
                _row(engine_command_sender_is_bot=False),
                report,
                access=None,
            )
