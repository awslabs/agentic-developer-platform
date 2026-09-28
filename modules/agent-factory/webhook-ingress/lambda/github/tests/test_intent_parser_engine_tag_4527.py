"""Issue #4527 — the `@agent-engine` tag is recognised, and spawns nothing.

The webhook Lambda's whole job on this path is a two-line decision: is this comment
addressed to the orchestration engine, and if so, mark the row and stop. What makes
it worth a test file of its own is that both halves have a specific way of going
wrong, and the issue's impact table names them:

* **Routed to persona dispatch.** Persona routing is a first-match substring scan
  over a dict, so anything that ends up in that dict wins on whatever line it
  happens to occupy. A tag that reached it would spawn an agent pod per command and
  lose the command itself. The tag is therefore checked BEFORE the persona scan, and
  the tests below pin that ordering with a comment that contains both an engine tag
  and a persona mention.
* **Enqueued anyway.** The engine path must produce no SQS message and no gateway
  call, because the tick — not this Lambda — is what applies a command. That is
  free here (the existing `intent is None` branch enqueues nothing), and a test
  pins it so a later refactor cannot quietly make "no intent" mean "default
  intent".

Also pinned: the tag is matched as a token, so `@agent-engineering-team` in prose
does not become a command that halts somebody's plan.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from common import skip_reasons
from intent_parser import extract_intent, extract_intent_with_reason


def _human_sender():
    return {"login": "alice", "id": 100, "type": "User"}


def _bot_sender():
    return {"login": "aws-e-adp-agent-dev[bot]", "id": 900, "type": "Bot"}


def _comment(body: str, *, sender=None, number: int = 4527):
    return {
        "action": "created",
        "comment": {"body": body},
        "issue": {"number": number},
        "sender": sender if sender is not None else _human_sender(),
    }


class TestEngineTagRecognised:
    """Every command spelling is recognised and reported as the engine reason."""

    def test_halt_is_recognised(self):
        intent, reason = extract_intent_with_reason("issue_comment", _comment("@agent-engine halt"))
        assert intent is None
        assert reason == skip_reasons.ENGINE_COMMAND

    def test_every_v1_verb_is_recognised(self):
        """Recognition must not depend on the verb.

        This Lambda deliberately does not parse the command — it cannot, because
        parsing needs the graph and the tenant (#4303). So a body it marks may carry
        any of the five verbs, or a verb this Lambda has never heard of, and the
        outcome is identical: mark and stop. Pinning all five here is what stops a
        later "optimisation" from adding a verb list the tick would then disagree
        with.
        """
        for body in (
            "@agent-engine accept",
            "@agent-engine approve gate 3",
            "@agent-engine halt",
            "@agent-engine resume",
            "@agent-engine replan: split wave 2 in half",
        ):
            intent, reason = extract_intent_with_reason("issue_comment", _comment(body))
            assert intent is None, body
            assert reason == skip_reasons.ENGINE_COMMAND, body

    def test_case_is_insensitive(self):
        """GitHub renders @-mentions case-insensitively, so `@Agent-Engine` works."""
        intent, reason = extract_intent_with_reason("issue_comment", _comment("@Agent-Engine Halt"))
        assert intent is None
        assert reason == skip_reasons.ENGINE_COMMAND

    def test_a_tag_mid_sentence_is_recognised(self):
        """The tag need not start the comment — a human writes prose around it."""
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("this looks wrong, so @agent-engine halt please"),
        )
        assert intent is None
        assert reason == skip_reasons.ENGINE_COMMAND

    def test_a_bot_comment_carrying_the_tag_is_also_marked(self):
        """Bot senders are not special-cased, and must not be.

        The tag alone grants nothing: the tick resolves the commenter to a platform
        identity server-side and applies `plan:approve`, so a bot with no linked
        identity is refused THERE. Excluding bots here would instead mean a bot
        comment took the ordinary persona path, which is the routing bug this file
        exists to prevent.
        """
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("@agent-engine halt", sender=_bot_sender()),
        )
        assert intent is None
        assert reason == skip_reasons.ENGINE_COMMAND


class TestTagIsCheckedBeforePersonaRouting:
    """The ordering guard. Persona routing is first-match, so order is the fix."""

    def test_a_comment_naming_both_the_engine_and_a_persona_goes_to_the_engine(self):
        """The exact case the issue's validation section calls for.

        Persona routing scans a dict for the first mention that appears anywhere in
        the body. A comment that says both must resolve to the engine, because the
        engine tag is the one the human addressed as a command — the persona name is
        prose about who should look at it.
        """
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("@agent-engine halt — @agent-developer will pick this up tomorrow"),
        )
        assert intent is None, "the engine tag must win over a persona mention in prose"
        assert reason == skip_reasons.ENGINE_COMMAND

    def test_the_persona_name_first_in_the_body_still_loses(self):
        """Ordering must come from the code path, not from where the words fall.

        If the tag check were merely *near* the persona scan rather than before it,
        this is the body that would slip through: the persona mention is first in
        the text, so a substring scan reaches it first.
        """
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("@agent-developer fyi — @agent-engine halt"),
        )
        assert intent is None
        assert reason == skip_reasons.ENGINE_COMMAND

    def test_an_ordinary_persona_mention_still_dispatches(self):
        """The regression check: nothing about normal traffic changes.

        A comment with no engine tag must still produce a real intent, or this
        change would have turned off agent dispatch for everyone.
        """
        intent = extract_intent("issue_comment", _comment("@agent-developer please fix this"))
        assert intent is not None
        assert intent.persona == "developer"


class TestTagIsMatchedAsAToken:
    """A substring match here would let prose halt a plan."""

    def test_agent_engineering_team_is_prose(self):
        """`@agent-engineering-team` contains `@agent-engine` as a substring.

        A `\\b`-style match is not enough on its own either: word-boundary logic
        treats a hyphen as a boundary. The tag is matched with a negative lookahead
        on word characters AND hyphens, which is what makes this body prose.
        """
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("cc @agent-engineering-team for visibility"),
        )
        assert reason != skip_reasons.ENGINE_COMMAND
        assert intent is None

    def test_a_suffixed_tag_is_prose(self):
        """`@agent-engine-v2` names something that is not this engine."""
        intent, reason = extract_intent_with_reason(
            "issue_comment",
            _comment("@agent-engine-v2 halt"),
        )
        assert reason != skip_reasons.ENGINE_COMMAND
        assert intent is None

    def test_an_untagged_comment_is_unaffected(self):
        intent, reason = extract_intent_with_reason("issue_comment", _comment("looks good to me"))
        assert intent is None
        assert reason == skip_reasons.NO_MENTION


class TestTheEngineTagIsNotAPersona:
    """Structural: the tag must not be in the persona catalogue at all."""

    def test_the_tag_is_absent_from_the_mention_map(self):
        """Membership in that dict IS the bug, not merely a route to it.

        Every entry in `MENTION_TO_PERSONA` names an agent to spawn a pod for. An
        entry for the engine would make `spawn_persona` the consumer of every
        command — a wasted pod per command and the command itself lost. It would
        also break `test_persona_catalogue_parity`, which asserts the dict matches
        `docs/agent-catalogue.md` two ways.
        """
        from intent_parser import MENTION_TO_PERSONA

        assert "@agent-engine" not in MENTION_TO_PERSONA
        assert not any("engine" in mention for mention in MENTION_TO_PERSONA)


class TestNothingIsEnqueued:
    """No SQS message, no gateway call — the marked row is the whole delivery."""

    def test_no_intent_means_no_envelope_can_be_built(self):
        """`extract_intent` returning None is what makes the enqueue impossible.

        The handler's enqueue path is reached only from a non-None intent, so this
        assertion is the enqueue assertion: there is nothing for the publisher to
        publish. Pinned as its own test because "engine commands are never
        enqueued" is a hard rule from the issue (the Lambda has no VPC, DB or
        gateway access and must never gain any), and a future change that gave the
        engine path a synthetic intent would satisfy every other test in this file
        while breaking that rule.
        """
        assert extract_intent("issue_comment", _comment("@agent-engine halt")) is None
