"""Tests for the `@agent-engine` comment parser (issue #4527).

The parser is the platform's first surface where text written by anyone who can
comment on an issue selects a write path. It holds no authority — resolving the
target, checking `plan:approve` and performing the transition all happen elsewhere
— but it decides *which* of those paths the applier will take, so a parse that says
"halt" where the human wrote something else is a state change nobody asked for.

Being pure makes the hostile cases cheap to enumerate, which is the point of the
module boundary. So they are enumerated:

- **Blockquotes are not commands.** GitHub's "Quote reply" button is the single most
  likely way a command is accidentally re-issued, and quoting a halt would halt the
  plan again, attributed to whoever pressed the button.
- **Only the first command counts.** "Apply every command we can find" turns one
  careless comment into several state changes.
- **The tag is a token.** `@agent-engineering-team` in prose must not halt a plan.
- **The verb is anchored.** "do not halt" is not a halt.
- **Arguments are bounded.** No parse may hand the applier a value it has to defend
  against.

`TestNoParseIsAuthorization` is the load-bearing one: it proves at source level that
this module cannot act, only describe.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from src.orchestration.adapters import github_commands as parser_module
from src.orchestration.adapters.github_commands import (
    ENGINE_TAG,
    REPLAN_TEXT_MAX_LEN,
    CommandVerb,
    parse_engine_command,
)

_PARSER_PATH = Path(inspect.getfile(parser_module))


def _parser_ast() -> ast.Module:
    return ast.parse(_PARSER_PATH.read_text())


class TestTheFiveVerbs:
    """Each v1 command resolves to its verb, and to nothing else."""

    def test_accept(self):
        command = parse_engine_command("@agent-engine accept")
        assert command is not None
        assert command.verb is CommandVerb.ACCEPT
        assert command.gate_ref is None

    def test_halt(self):
        assert parse_engine_command("@agent-engine halt").verb is CommandVerb.HALT

    def test_resume(self):
        assert parse_engine_command("@agent-engine resume").verb is CommandVerb.RESUME

    def test_approve_gate_carries_the_gate_ref(self):
        command = parse_engine_command("@agent-engine approve gate 3")
        assert command.verb is CommandVerb.APPROVE_GATE
        assert command.gate_ref == "3"

    def test_replan_carries_its_text(self):
        command = parse_engine_command("@agent-engine replan: split wave 2 in half")
        assert command.verb is CommandVerb.REPLAN
        assert command.text == "split wave 2 in half"

    def test_the_verb_is_case_insensitive(self):
        """A human types what reads naturally, including at the start of a sentence."""
        assert parse_engine_command("@Agent-Engine HALT").verb is CommandVerb.HALT

    def test_trailing_prose_after_a_bare_verb_is_ignored(self):
        """`halt` is the command; what follows is the human explaining themselves."""
        assert parse_engine_command("@agent-engine halt — this is going nowhere").verb is CommandVerb.HALT


class TestApproveGateArguments:
    """The one command that takes a value from untrusted text."""

    def test_a_hash_prefix_is_accepted(self):
        """`approve gate #12` is what a human types when the gate is an issue."""
        assert parse_engine_command("@agent-engine approve gate #12").gate_ref == "12"

    def test_leading_zeroes_normalise(self):
        """`gate 007` and `gate 7` must address the same node.

        Without this, a zero-padded ref resolves to no node and the command is
        refused for a reason the commenter cannot see.
        """
        assert parse_engine_command("@agent-engine approve gate 007").gate_ref == "7"

    def test_an_all_zero_ref_stays_representable(self):
        """`lstrip("0")` alone would produce "", which reads as "no gate given"."""
        assert parse_engine_command("@agent-engine approve gate 000").gate_ref == "0"

    def test_multiple_spaces_are_tolerated(self):
        assert parse_engine_command("@agent-engine approve   gate   4").gate_ref == "4"

    def test_a_non_numeric_gate_is_not_a_command(self):
        """No digits, no parse. The applier never sees a ref it must validate."""
        assert parse_engine_command("@agent-engine approve gate all") is None

    def test_an_absurdly_long_number_is_not_a_command(self):
        """Bounded at parse time, so no downstream query is handed 40 digits."""
        assert parse_engine_command("@agent-engine approve gate " + "9" * 40) is None

    def test_a_bare_approve_is_not_a_command(self):
        """There is no bare `approve` verb, and it must not fall through to one.

        `accept` answers the flow's single outstanding gate; `approve` without a
        gate number is ambiguous, and resolving ambiguity by guessing is how a
        command approves the wrong gate.
        """
        assert parse_engine_command("@agent-engine approve") is None


class TestReplanText:
    def test_the_text_is_stripped(self):
        assert parse_engine_command("@agent-engine replan:    too many nodes   ").text == "too many nodes"

    def test_the_colon_is_required(self):
        """Otherwise "we should replan this eventually" records a request nobody made."""
        assert parse_engine_command("@agent-engine replan this eventually") is None

    def test_an_empty_replan_is_still_a_request(self):
        """Recording it beats dropping it.

        The human asked for a re-plan and forgot to say why. Refusing outright
        leaves them waiting for something that never happens; recording an
        empty-texted request means someone sees it.
        """
        command = parse_engine_command("@agent-engine replan:")
        assert command.verb is CommandVerb.REPLAN
        assert command.text == ""

    def test_long_text_is_capped_not_rejected(self):
        """The text lands in `reason` on an append-only row, so it is bounded.

        Capped rather than refused for the same reason as above — and at the same
        2000 chars the dashboard's reason field allows, so the two input paths do
        not accept different-sized reasons.
        """
        command = parse_engine_command("@agent-engine replan: " + "x" * 5000)
        assert len(command.text) == REPLAN_TEXT_MAX_LEN


class TestTheTagIsAToken:
    def test_a_longer_agent_name_is_prose(self):
        assert parse_engine_command("cc @agent-engineering-team halt the rollout") is None

    def test_a_suffixed_tag_is_prose(self):
        assert parse_engine_command("@agent-engine-v2 halt") is None

    def test_a_body_with_no_tag_is_nothing(self):
        assert parse_engine_command("halt this plan please") is None

    def test_the_tag_must_precede_the_verb(self):
        """Text BEFORE the tag is address or prose, never the command.

        Otherwise "do not halt, @agent-engine" would halt the plan — the reading
        most opposite to what was written.
        """
        assert parse_engine_command("do not halt, @agent-engine") is None

    def test_the_exported_tag_matches_what_is_parsed(self):
        """`ENGINE_TAG` is the value the webhook Lambda's regex is pinned against."""
        assert ENGINE_TAG == "@agent-engine"
        assert parse_engine_command(f"{ENGINE_TAG} halt").verb is CommandVerb.HALT


class TestAnchoring:
    def test_a_negated_verb_is_not_a_command(self):
        """The verb must be the first thing after the tag."""
        assert parse_engine_command("@agent-engine do not halt") is None

    def test_a_verb_embedded_in_a_word_is_not_a_command(self):
        """`\\b` after the verb, so `haltingly` is prose."""
        assert parse_engine_command("@agent-engine haltingly approaching a decision") is None

    def test_the_verb_may_be_the_whole_remainder(self):
        assert parse_engine_command("@agent-engine resume").verb is CommandVerb.RESUME


class TestBlockquotesAreNotCommands:
    def test_a_quoted_command_is_ignored(self):
        """The "Quote reply" case: replying must not re-issue what was quoted."""
        body = "> @agent-engine halt\n\nagreed, that was the right call"
        assert parse_engine_command(body) is None

    def test_an_indented_quote_is_also_ignored(self):
        """GitHub renders a leading-whitespace `>` as a quote too."""
        assert parse_engine_command("   > @agent-engine halt") is None

    def test_a_real_command_below_a_quote_still_parses(self):
        """Skipping quotes must not skip the comment.

        The realistic shape of a reply-then-command: quote what you are responding
        to, then issue the command.
        """
        body = "> @agent-engine accept\n\nno — @agent-engine halt"
        assert parse_engine_command(body).verb is CommandVerb.HALT


class TestOnlyTheFirstCommandCounts:
    def test_two_commands_resolve_to_the_first(self):
        """One comment must never produce two state changes."""
        command = parse_engine_command("@agent-engine halt\n@agent-engine accept")
        assert command.verb is CommandVerb.HALT

    def test_two_commands_on_one_line_resolve_to_the_first(self):
        command = parse_engine_command("@agent-engine halt and also @agent-engine accept")
        assert command.verb is CommandVerb.HALT

    def test_prose_addressing_the_engine_does_not_shadow_a_later_command(self):
        """A tagged line with no verb must not stop the scan.

        Otherwise "thanks @agent-engine" on line 1 would swallow the real command
        on line 2 — a command silently lost, which is the failure mode this bridge
        exists to remove.
        """
        body = "thanks @agent-engine\n@agent-engine halt"
        assert parse_engine_command(body).verb is CommandVerb.HALT


class TestDegenerateInput:
    def test_none_is_accepted(self):
        """A marked row with a missing body is a real, if broken, state.

        It must not raise on the tick path: one malformed row cannot be allowed to
        stop every other tenant's commands from being applied.
        """
        assert parse_engine_command(None) is None

    def test_empty_is_accepted(self):
        assert parse_engine_command("") is None

    def test_an_unrecognised_verb_is_nothing(self):
        assert parse_engine_command("@agent-engine explode") is None

    def test_an_enormous_body_terminates(self):
        """Bounded scan, so a pathological body cannot be a tick-time cost."""
        assert parse_engine_command("no commands here\n" * 100_000) is None


class TestNoParseIsAuthorization:
    """Source-level proof that this module can describe but not act.

    The load-bearing test. Untrusted text selects a write path here, so the only
    durable guarantee is that the selection and the writing live in different
    modules — a behavioural test can only show that today's code does not write.
    """

    def test_it_imports_nothing_that_could_write(self):
        """No session, no repository, no state machine, no AWS, no HTTP.

        A parser that imported any of these could be edited into acting, and the
        edit would look local and harmless.
        """
        tree = _parser_ast()
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        forbidden = {"sqlalchemy", "boto3", "botocore", "httpx", "requests", "src"}
        assert not (imported & forbidden), f"the parser must stay pure; it imports {sorted(imported & forbidden)}"

    def test_it_defines_no_async_function(self):
        """Everything here is a function of the text. I/O would need `await`."""
        tree = _parser_ast()
        assert not [n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]

    def test_the_parsed_command_carries_no_identity(self):
        """`EngineCommand` must not be mistakable for an authorization.

        No actor, no org, no permission — so nothing downstream can read authority
        off a parse result, which is exactly the "commenter identity trusted from
        the payload" bug class the issue names.
        """
        from src.orchestration.adapters.github_commands import EngineCommand

        fields = set(EngineCommand.__dataclass_fields__)
        assert fields == {"verb", "gate_ref", "text"}

    def test_a_parsed_command_is_frozen(self):
        """A request about text that already exists cannot be edited into another."""
        import dataclasses

        command = parse_engine_command("@agent-engine halt")
        with __import__("pytest").raises(dataclasses.FrozenInstanceError):
            command.verb = CommandVerb.ACCEPT
