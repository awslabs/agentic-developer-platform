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
- **Code is not a command** (#4599). A backticked or fenced command name is how a
  human writes *about* a command — in a doc, a table, a design note, or the bridge's
  own success message. A separate rule from the blockquote one, and not implied by
  it: this was the actual defect behind the maiden-voyage noise.
- **The tag must be addressed, not described** (#4599). A word running into the tag
  means prose. Weaker than "the tag must lead the comment" on purpose — that rule
  would have dropped the `cc @agent-engine halt` form and looked like a dead engine.
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
    """Each command resolves to its verb, and to nothing else."""

    def test_accept(self):
        command = parse_engine_command("@agent-engine accept")
        assert command is not None
        assert command.verb is CommandVerb.ACCEPT
        assert command.gate_ref is None
        # Plain `accept` carries no draft, so nothing downstream can read one off it
        # and go looking for "the" amendment.
        assert command.draft_ref is None

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


class TestAcceptAmendment:
    """`accept amendment <draft-id>` (#4529) — the longest-prefix rule that matters.

    `\\Aaccept\\b` matches "accept amendment 1234…" perfectly well, so if the specific
    spelling were checked after the bare one, every amendment acceptance would silently
    answer the acceptance GATE instead. That is not a cosmetic mis-parse: it is a
    different write path, taken on the very plan the human was trying to replace, and
    it would report success. Hence a test per direction rather than one happy path.
    """

    DRAFT = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def test_it_resolves_to_the_amendment_verb_and_carries_the_draft(self):
        command = parse_engine_command(f"@agent-engine accept amendment {self.DRAFT}")
        assert command.verb is CommandVerb.ACCEPT_AMENDMENT
        assert command.draft_ref == self.DRAFT

    def test_it_is_not_read_as_a_bare_accept(self):
        """The failure this ordering exists to prevent, stated as its own test."""
        command = parse_engine_command(f"@agent-engine accept amendment {self.DRAFT}")
        assert command.verb is not CommandVerb.ACCEPT

    def test_a_bare_accept_never_becomes_an_amendment_acceptance(self):
        """The other direction, and the more dangerous one.

        A human answering a gate must not have a whole replacement plan applied
        because a draft happened to be pending. There is no "latest amendment"
        resolution anywhere in the system, and this is where that starts.
        """
        for body in ("@agent-engine accept", "@agent-engine accept the plan", "@agent-engine accept now"):
            assert parse_engine_command(body).verb is CommandVerb.ACCEPT, body
            assert parse_engine_command(body).draft_ref is None, body

    def test_the_id_is_case_normalised(self):
        """Draft ids are lowercase hex; a human may paste them in any case.

        Without normalising, an id typed back in caps resolves to no row and the
        command is refused for a reason the commenter cannot see — the same failure
        `gate 007` was fixed for.
        """
        assert parse_engine_command(f"@agent-engine accept amendment {self.DRAFT.upper()}").draft_ref == self.DRAFT

    def test_amendment_with_no_id_is_recognised_but_carries_none(self):
        """It must NOT fall through to the bare `accept`.

        Falling through is the dangerous reading: the human asked to apply an
        amendment and would instead answer the acceptance gate — a real state change
        they did not request. Recognising the shape with `draft_ref=None` lets the
        applier reply "name the draft" and write nothing.
        """
        command = parse_engine_command("@agent-engine accept amendment")
        assert command.verb is CommandVerb.ACCEPT_AMENDMENT
        assert command.draft_ref is None

    def test_a_malformed_id_carries_none_rather_than_a_guess(self):
        """Not a partial match, and not a fallthrough to bare `accept` either."""
        for body in (
            "@agent-engine accept amendment the-latest-one",
            "@agent-engine accept amendment ../../etc/passwd",
            "@agent-engine accept amendment 1234",  # too short to be a draft id
        ):
            command = parse_engine_command(body)
            assert command.verb is CommandVerb.ACCEPT_AMENDMENT, body
            assert command.draft_ref is None, body

    def test_the_id_length_is_bounded(self):
        """So no parse hands the applier an argument it must defend against.

        The same rule as the gate ref. A 10KB "id" is matched only up to the draft-id
        shape's ceiling, and what remains cannot be a real row.
        """
        command = parse_engine_command("@agent-engine accept amendment " + "a" * 5000)
        assert command.verb is CommandVerb.ACCEPT_AMENDMENT
        assert command.draft_ref is None or len(command.draft_ref) <= 36

    def test_trailing_prose_after_the_id_is_ignored(self):
        command = parse_engine_command(f"@agent-engine accept amendment {self.DRAFT} — looks right to me")
        assert command.draft_ref == self.DRAFT

    def test_it_is_not_an_authorization(self):
        """A parsed id is a reference the text contained, nothing more.

        The parser cannot know whether this draft exists, is in the commenter's
        tenant, is on the flow under discussion, or is still pending — and it must not
        try, because answering any of those here would tell anyone who can comment
        which ids are real.
        """
        command = parse_engine_command("@agent-engine accept amendment " + "f" * 36)
        assert command.draft_ref == "f" * 36


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


class TestCodeIsNotACommand:
    """Issue #4599: a backticked command is documentation, not an instruction.

    This is the defect the issue was filed for, though not the one it described. The
    verb anchor, the token match and the blockquote skip all shipped in #4527 and all
    work; what was missing was any notion of Markdown code. Inside a code span the
    tag IS still immediately followed by the verb, so every existing guard passes —
    and a backticked command name is exactly how a human writes *about* a command in
    a doc, a table or a design note. The false-trigger rate tracked how much the
    feature was being documented.
    """

    def test_an_inline_span_in_prose_is_not_a_command(self):
        """The maiden-voyage shape (#4589): a design note explaining the feature."""
        body = "a human posts `@agent-engine accept` to approve the plan."
        assert parse_engine_command(body) is None

    def test_a_span_that_is_the_whole_line_is_not_a_command(self):
        """No prose to disqualify it — the backticks alone must be enough.

        Called out explicitly in the architect's review because a line-level rule
        that only looked at surrounding prose would let this one through.
        """
        assert parse_engine_command("`@agent-engine accept`") is None

    def test_a_table_cell_span_is_not_a_command(self):
        """A docs table listing the commands must not issue every one of them."""
        body = "| Command | Effect |\n|---|---|\n| `@agent-engine halt` | stop spend |"
        assert parse_engine_command(body) is None

    def test_a_bulleted_span_is_not_a_command(self):
        assert parse_engine_command("- `@agent-engine resume` clears a halt") is None

    def test_a_fenced_block_is_not_a_command(self):
        body = "run this:\n```\n@agent-engine accept\n```"
        assert parse_engine_command(body) is None

    def test_a_fence_with_an_info_string_is_not_a_command(self):
        body = "```text\n@agent-engine halt\n```"
        assert parse_engine_command(body) is None

    def test_a_tilde_fence_is_not_a_command(self):
        """GitHub renders `~~~` as a fence too."""
        assert parse_engine_command("~~~\n@agent-engine halt\n~~~") is None

    def test_an_indented_fence_in_a_list_item_is_not_a_command(self):
        body = "- example:\n  ```\n  @agent-engine accept\n  ```"
        assert parse_engine_command(body) is None

    def test_a_real_command_after_a_closed_fence_still_parses(self):
        """Code-awareness must not swallow the rest of the comment.

        The realistic shape: show the command in a block, then actually issue it.
        """
        body = "```\n@agent-engine halt\n```\n@agent-engine accept"
        assert parse_engine_command(body).verb is CommandVerb.ACCEPT

    def test_a_command_with_a_trailing_span_still_parses(self):
        """Stripping spans must not drop the line they were on.

        A real command that happens to cite a file is still a command, which is why
        spans are removed from a line rather than disqualifying it.
        """
        command = parse_engine_command("@agent-engine halt — see `docs/runbook.md`")
        assert command.verb is CommandVerb.HALT


class TestUnclosedFencesFailSafe:
    """Issue #4599: an unterminated fence swallows the rest of the body.

    Load-bearing direction, not an accident of implementation. Bodies are truncated
    at `ENGINE_COMMAND_BODY_MAX_CHARS` on the webhook side, which can cut a body
    mid-fence and leave an opening ``` with no partner. If an unclosed fence were
    treated as "not really a fence", truncation would become a way to smuggle a
    command *out* of a code block: pad to the cap, open a fence, and the fence
    silently stops applying.

    Erring the other way can only lose a command written after an unterminated
    fence — a body that renders as code on GitHub anyway, so the human cannot see
    their command as a command either.
    """

    def test_a_command_after_an_unclosed_fence_is_not_a_command(self):
        body = "here is how:\n```\n@agent-engine accept"
        assert parse_engine_command(body) is None

    def test_a_command_after_an_unclosed_tilde_fence_is_not_a_command(self):
        assert parse_engine_command("docs:\n~~~\n@agent-engine halt") is None

    def test_a_fence_of_the_other_character_does_not_close_a_block(self):
        """A ``` inside a ~~~ block is content, exactly as Markdown renders it."""
        body = "~~~\n```\n@agent-engine accept\n~~~"
        assert parse_engine_command(body) is None


class TestTheTagMustBeAddressedNotDescribed:
    """Issue #4599: the address-only prefix rule.

    Deliberately NOT the "leading token" rule the issue proposed. A bare-leading-
    token requirement would reject `cc @agent-engine halt` (a form the parser's own
    comment documents) and `no — @agent-engine halt` (a shipped, tested reply shape),
    delivering row 1 of the issue's own blast-radius table: real human commands
    silently ignored, making the engine look dead.

    The discriminator is whether the sentence STOPS at the tag or flows through it.
    """

    def test_a_word_running_into_the_tag_is_prose(self):
        assert parse_engine_command("the operator should @agent-engine halt the plan") is None

    def test_describing_the_command_without_backticks_is_prose(self):
        """Belt-and-braces with the code rule: prose is prose unquoted too."""
        assert parse_engine_command("a human posts @agent-engine accept to approve") is None

    def test_a_bare_command_parses(self):
        assert parse_engine_command("@agent-engine accept").verb is CommandVerb.ACCEPT

    def test_the_cc_form_parses(self):
        """The documented address form. The regression guard for over-tightening."""
        assert parse_engine_command("cc @agent-engine halt").verb is CommandVerb.HALT

    def test_the_slash_cc_form_parses(self):
        assert parse_engine_command("/cc @agent-engine halt").verb is CommandVerb.HALT

    def test_leading_whitespace_parses(self):
        assert parse_engine_command("   @agent-engine resume").verb is CommandVerb.RESUME

    def test_a_bulleted_command_parses(self):
        assert parse_engine_command("- @agent-engine accept").verb is CommandVerb.ACCEPT

    def test_an_ordered_list_command_parses(self):
        assert parse_engine_command("1. @agent-engine accept").verb is CommandVerb.ACCEPT

    def test_another_mention_before_the_tag_parses(self):
        assert parse_engine_command("@alice @agent-engine halt").verb is CommandVerb.HALT

    def test_a_clause_boundary_before_the_tag_parses(self):
        """Punctuation ends the preceding thought, so the tag begins a new one."""
        assert parse_engine_command("no — @agent-engine halt").verb is CommandVerb.HALT
        assert parse_engine_command("as discussed, @agent-engine accept").verb is CommandVerb.ACCEPT


class TestTheRegistrationNoteIsNotACommand:
    """Issue #4599: the bridge's own success message must not trigger the bridge.

    The real source of the #4589 noise, and the reason it fired *repeatedly* rather
    than occasionally: `engine_registration._success_note` posts on every successful
    draft registration, and it quoted the accept command inline. Every registration
    therefore produced a marked row, a parse, a refusal (the author is a bot with no
    `PLAN_APPROVE`) and a "this command cannot be applied by this account" reply.

    Pinned as a literal fixture rather than by importing the worker, because the
    emitter lives in a different deploy unit (`agent-worker-image`) that this suite
    cannot import. If that note's shape changes back to an inline span, this test is
    what catches it.
    """

    _SUCCESS_NOTE = (
        "### Delivery loop registered with the orchestration engine\n"
        "\n"
        "**Plan**: `flow-abc` (v1) — 12 nodes, 14 edges\n"
        "**State**: `draft` — the plan is visible in the graph UI and executes nothing.\n"
        "**Acceptance gate**: `gate/acceptance`\n"
        "\n"
        "Reply with the following to start execution:\n"
        "\n"
        "```\n"
        "@agent-engine accept\n"
        "```"
    )

    def test_the_success_note_does_not_parse_as_a_command(self):
        assert parse_engine_command(self._SUCCESS_NOTE) is None

    def test_the_old_inline_form_would_have_parsed(self):
        """Proof the fixture above is actually testing something.

        The pre-#4599 note said "Reply `@agent-engine accept` to start execution."
        Both the code rule AND the address rule now reject it; this documents that
        the old shape really was a live command, so nobody reads the test above as
        vacuous.
        """
        old = "Reply `@agent-engine accept` to start execution."
        assert parse_engine_command(old) is None


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

        Equality-checked, so every new field is this review moment. `draft_ref`
        (#4529) passes it for the same reason `gate_ref` does: it is a *reference the
        text contained*, not a fact about the commenter. The applier still has to
        establish that the draft exists, is in this tenant, is on the flow under
        discussion, is pending, and that this commenter may accept it — none of which
        this object asserts.
        """
        from src.orchestration.adapters.github_commands import EngineCommand

        fields = set(EngineCommand.__dataclass_fields__)
        assert fields == {"verb", "gate_ref", "text", "draft_ref"}

    def test_a_parsed_command_is_frozen(self):
        """A request about text that already exists cannot be edited into another."""
        import dataclasses

        command = parse_engine_command("@agent-engine halt")
        with __import__("pytest").raises(dataclasses.FrozenInstanceError):
            command.verb = CommandVerb.ACCEPT
