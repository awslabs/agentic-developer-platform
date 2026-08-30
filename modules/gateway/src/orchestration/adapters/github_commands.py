"""Parse an ``@agent-engine`` comment body into one engine command (Issue #4527).

The webhook Lambda recognises the tag and marks the event row; it deliberately
does not parse. Parsing needs the graph, the tenant and the approval record, none
of which that component can see (#4303's closed-routes table), so the body travels
to the tick and is turned into an :class:`EngineCommand` here.

**A separate module from ``github_comments.py``, on purpose.** That module is the
gate-answer *applier*, and its docstring states that parsing a comment into a
``GateAnswer`` is the caller's job — a claim its test suite enforces with an AST
assertion that the module does not import ``re``. This module is that caller's
parser. Putting the regexes there would break the test and, more to the point,
would mix "what did the human ask for" with "may this human have it", which are
the two questions the bridge has to keep separate.

**Pure. No I/O, no session, no AWS.** Everything here is a function of the comment
text. That is what makes the hostile cases — a command inside a quoted block, two
commands in one comment, a gate number of ``99999999999999`` — cheap to enumerate
as tests rather than expensive to reason about. Authority lives entirely on the
other side of this boundary: this module can say "this text asks for a halt" and
nothing else. It cannot halt anything.

**Untrusted input.** A comment body is written by anyone who can comment on the
issue, which on a public repository is anyone at all. So:

* the tag must be matched as a token, not a substring — ``@agent-engineering``
  in prose is not a command;
* only the FIRST command in a body is honoured, and the rest of that line is the
  only place arguments are read from. A body containing ``accept`` and ``halt``
  resolves to whichever appears first and never to both, because "apply every
  command we can find" turns one careless comment into several state changes;
* quoted lines (Markdown ``>`` blockquotes) are skipped, so replying to a comment
  that contained a command does not re-issue it — GitHub's "Quote reply" button
  makes that the single most likely way a command is accidentally repeated;
* the gate number is bounded and must be all digits, so no parse here can produce
  an argument the applier has to defend against.

What this module does NOT do, and must not grow into: resolving which node a
command targets, deciding whether the commenter may issue it, or writing anything.
Those live in ``engine_commands.py`` and ``github_comments.py`` respectively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ENGINE_TAG",
    "CommandVerb",
    "EngineCommand",
    "parse_engine_command",
]


# The tag that addresses the engine. Must stay identical to the webhook Lambda's
# `_ENGINE_TAG_RE` in `github/intent_parser.py`: that component decides which
# events are marked, this one decides which marked events mean anything, and a
# disagreement between the two is a command that is stored and never acted on
# (or, worse, acted on without ever having been marked). The two live in separate
# deploy units and cannot import each other — same constraint `dispatch_pass.py`
# documents for the envelope contract — so the pair is pinned by a test on each
# side rather than shared as code.
ENGINE_TAG = "@agent-engine"


class CommandVerb(StrEnum):
    """The five commands v1 accepts.

    A closed enum rather than a free string because the verb selects which write
    path runs. A parser that could yield an unrecognised verb would push the
    "what does this mean?" decision into the applier, where the answer would have
    to be a default — and a default that acts is how an unrecognised command
    becomes an unintended state change.
    """

    ACCEPT = "accept"  # Accept the plan as it stands
    APPROVE_GATE = "approve_gate"  # Approve one gate, addressed by its node ref
    HALT = "halt"  # Stop spending on this plan
    RESUME = "resume"  # Clear a halt or a failure and let work continue
    REPLAN = "replan"  # Record a request to re-plan, with free text


# Verb spellings, checked in this order. Ordered longest-prefix-first *within* a
# shared first word so a more specific spelling always wins: `approve gate 3`
# must not be read as a bare `approve`. There is no bare `approve`, but the
# ordering is what keeps that true if one is ever added.
#
# Anchored at the start of the remaining text (`\A`) rather than searched, so
# `do not halt` cannot be read as a halt: the verb has to be the first thing after
# the tag or after a line start.
_VERB_PATTERNS: tuple[tuple[re.Pattern[str], CommandVerb], ...] = (
    # `approve gate <n>` — one or more spaces, and an optional `#`, because
    # `approve gate #12` is what a human types when the gate is an issue.
    (re.compile(r"\Aapprove\s+gate\s+#?(?P<gate>\d{1,9})\b", re.IGNORECASE), CommandVerb.APPROVE_GATE),
    # `replan:` requires the colon. Without it, "we should replan this eventually"
    # in prose would record a replan request nobody made.
    (re.compile(r"\Areplan\s*:\s*(?P<text>.*)", re.IGNORECASE), CommandVerb.REPLAN),
    (re.compile(r"\Aaccept\b", re.IGNORECASE), CommandVerb.ACCEPT),
    (re.compile(r"\Ahalt\b", re.IGNORECASE), CommandVerb.HALT),
    (re.compile(r"\Aresume\b", re.IGNORECASE), CommandVerb.RESUME),
)

# The tag as a token: not followed by a word character or a hyphen, so
# `@agent-engineering-team` and `@agent-engine-v2` are prose, not commands. Kept
# separate from `_VERB_PATTERNS` because the tag locates the command and the verb
# identifies it — two steps, so "tagged but unparseable" is a distinguishable
# outcome from "not addressed to the engine at all".
_TAG_RE = re.compile(re.escape(ENGINE_TAG) + r"(?![\w-])", re.IGNORECASE)

# A Markdown blockquote line. Skipped so GitHub's "Quote reply" cannot re-issue a
# command: quoting a comment that said `@agent-engine halt` would otherwise halt
# the plan a second time, attributed to whoever pressed the button.
_QUOTE_RE = re.compile(r"\A\s*>")

# Free text carried on a `replan:`. Bounded because it lands in `reason` on an
# append-only row, and the same 2000-char cap the dashboard applies to a reason
# (`controls.py`'s `Field(max_length=2000)`) is the right ceiling here — the two
# input paths must not accept different-sized reasons.
REPLAN_TEXT_MAX_LEN = 2000

# How much of a comment body is scanned. A body is capped at 4000 chars on the
# webhook side (`ENGINE_COMMAND_BODY_MAX_CHARS`), so this is belt-and-braces
# against a body that reached the tick by some other route; the command is on the
# tag's line, so a real command is never past this.
_MAX_BODY_SCAN_CHARS = 8000


@dataclass(frozen=True)
class EngineCommand:
    """One parsed command. **A request, not an authorization.**

    Frozen because a parsed command is a statement about text that already
    exists. Nothing here has been checked against the graph, the tenant, or the
    commenter's permissions — the applier does all three, and the fields carry no
    identity precisely so that this object cannot be mistaken for one that has.

    Attributes:
        verb: Which of the five commands this is.
        gate_ref: The gate's node ref, for ``APPROVE_GATE`` only. A string
            because ``OrchestrationNode.node_ref`` is a string address component,
            not an integer — parsing it to an int and back would invent a
            normalisation the graph does not use.
        text: The free text of a ``replan:``, capped and stripped. Empty for
            every other verb.
    """

    verb: CommandVerb
    gate_ref: str | None = None
    text: str = ""


def _candidate_lines(body: str) -> list[str]:
    """The lines of ``body`` a command may appear on, in order.

    Blockquotes are dropped here rather than filtered later so that no downstream
    step can accidentally reach one.
    """
    return [line for line in body[:_MAX_BODY_SCAN_CHARS].splitlines() if not _QUOTE_RE.match(line)]


def parse_engine_command(body: str | None) -> EngineCommand | None:
    """The first engine command in ``body``, or None if there is not one.

    Returns None for three genuinely different situations, and that is deliberate:
    a body with no tag, a body whose tag is part of a longer word, and a tagged
    body whose verb is unrecognised all mean "do nothing". Distinguishing them in
    the return type would tempt a caller into replying differently to each, and
    "the engine did not understand you" is a reply that tells anyone who can
    comment which spellings probe the parser. The caller logs which case occurred;
    the commenter learns nothing.

    Args:
        body: The raw comment text. ``None`` and ``""`` are accepted and yield
            None, because a marked row with a missing body is a real (if broken)
            state and must not raise on the tick path.

    Returns:
        The first :class:`EngineCommand` found, or None.
    """
    if not body:
        return None

    for line in _candidate_lines(body):
        tag = _TAG_RE.search(line)
        if tag is None:
            continue

        # Only the text after the tag, on the tag's own line, is the command.
        # Anything before it is address ("cc @agent-engine") or prose.
        remainder = line[tag.end() :].strip()

        for pattern, verb in _VERB_PATTERNS:
            match = pattern.match(remainder)
            if match is None:
                continue

            if verb is CommandVerb.APPROVE_GATE:
                # `lstrip("0")` so `gate 007` and `gate 7` address the same node
                # rather than resolving to nothing; `or "0"` keeps an all-zeroes
                # ref representable instead of collapsing to the empty string,
                # which would silently become "no gate specified".
                return EngineCommand(verb=verb, gate_ref=match.group("gate").lstrip("0") or "0")

            if verb is CommandVerb.REPLAN:
                text = match.group("text").strip()[:REPLAN_TEXT_MAX_LEN]
                # An empty `replan:` is still a replan request. Recording it with
                # no text is more honest than refusing: the human asked for a
                # re-plan and forgot to say why, and dropping the request
                # entirely would leave them waiting for something that never
                # happens.
                return EngineCommand(verb=verb, text=text)

            return EngineCommand(verb=verb)

        # Tagged but no verb matched. Keep scanning: a body may address the engine
        # in prose on one line and carry the real command on another.

    return None
