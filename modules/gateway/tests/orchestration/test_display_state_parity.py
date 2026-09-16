"""The nine→five projection is the same on both sides of the wire (#4869).

`frontend/src/utils/nodeState.ts` has held this mapping since #4212; the flows
list needs it *in SQL*, because per-flow counts have to be aggregated by the
database rather than reduced in Python over every node of every flow on a page.
That makes two copies, and two copies of a state mapping drift.

The drift is silent in the worst way: both halves keep compiling, both keep
passing their own tests, and the only symptom is a list page whose numbers
disagree with the graph page it links to — one screen saying a flow is running
while the other says it is stalled. Nobody files that as a bug against a mapping
table; they file it as "the flows page is wrong".

So this test reads the TypeScript as **text** and compares it key for key with
`display_state.py`. Reading the source rather than trusting a convention is the
point: a runtime test cannot see the frontend at all, which is exactly how the
second edit gets dropped.

Three properties:

  - **Every one of the nine engine states is mapped, on both sides.** An unmapped
    state counts into no bucket and silently understates a flow's size.
  - **Each state maps to the same display state on both sides**, including
    `superseded → nothing` and the substring trap `rejected_at_gate → stalled`
    (it *contains* "rejected", the banned phantom state).
  - **Render order matches**, so the rollup segments on the list page appear in
    the same order as on the graph page.

Plus one assertion on the derived SQL `FILTER` lists, since those are what the
database actually counts with — a correct `ENGINE_TO_DISPLAY` inverted wrongly
would produce the same drift by a different route.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.orchestration.display_state import (
    DISPLAY_STATE_ORDER,
    DISPLAY_TO_ENGINE,
    ENGINE_TO_DISPLAY,
    DisplayState,
)
from src.orchestration.state import NodeState

_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_NODE_STATE_TS = _GATEWAY_ROOT / "frontend" / "src" / "utils" / "nodeState.ts"


def _ts_source() -> str:
    """The TypeScript projection module, as text.

    A missing file must fail loudly rather than skip: a silently skipped parity
    test is indistinguishable from a passing one, and this test exists precisely
    for the case where somebody moved or rewrote that file.
    """
    assert _NODE_STATE_TS.is_file(), f"the frontend projection module is missing: {_NODE_STATE_TS}"
    return _NODE_STATE_TS.read_text(encoding="utf-8")


def _parse_ts_projection(source: str) -> dict[str, str | None]:
    """Extract `engineStateToDisplayState`'s switch as `{engine state: display or None}`.

    Parsed from the function body only, not the whole file: `DISPLAY_STATES` also
    contains the five display names as object keys, and `toDisplayState` returns
    `'stalled'` for the stall flag — matching `return` statements file-wide would
    pick up both and produce a mapping that looks plausible and is wrong.

    Cases fall through to a shared `return` (three states share `stalled`), so
    labels accumulate until a return is reached and are then all bound to it.
    """
    start = source.index("export function engineStateToDisplayState")
    # The `default:` arm returns null for unrecognised states, which is a
    # deploy-skew allowance rather than part of the projection — stop there so it
    # is not read as a tenth mapping.
    end = source.index("default:", start)
    body = source[start:end]

    mapping: dict[str, str | None] = {}
    pending: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if case := re.fullmatch(r"case '([a-z_]+)':", stripped):
            pending.append(case.group(1))
            continue
        if returned := re.fullmatch(r"return (?:'([a-z_]+)'|null);", stripped):
            for state in pending:
                mapping[state] = returned.group(1)  # None for `return null;`
            pending.clear()

    assert not pending, f"parsed cases with no return: {pending}"
    return mapping


def _parse_ts_order(source: str) -> list[str]:
    """Extract the `DISPLAY_STATE_ORDER` array literal in declaration order."""
    match = re.search(r"export const DISPLAY_STATE_ORDER: DisplayState\[\] = \[([^\]]*)\];", source)
    assert match, "DISPLAY_STATE_ORDER was not found in nodeState.ts"
    return re.findall(r"'([a-z_]+)'", match.group(1))


@pytest.fixture(scope="module")
def ts_projection() -> dict[str, str | None]:
    return _parse_ts_projection(_ts_source())


class TestTheParserItself:
    """Guard the guard: a parser that silently finds nothing passes everything.

    Every assertion below compares two collections. If the TypeScript side parses
    to `{}` — because the file was reformatted, or the function renamed — an
    equality check against a nine-key dict fails loudly, but a subset or
    membership check would not. These two tests make the parser's own failure
    unmistakable.
    """

    def test_the_parser_finds_nine_states(self, ts_projection):
        assert len(ts_projection) == 10, f"parsed {len(ts_projection)} states from nodeState.ts, expected 9: {ts_projection}"

    def test_the_parser_finds_five_ordered_display_states(self):
        assert len(_parse_ts_order(_ts_source())) == 5


class TestProjectionParity:
    def test_both_sides_map_all_nine_engine_states(self, ts_projection):
        """A state missing from either side is counted into no bucket at all."""
        assert set(ts_projection) == {state.value for state in NodeState}
        assert set(ENGINE_TO_DISPLAY) == set(NodeState)

    @pytest.mark.parametrize("engine_state", list(NodeState), ids=lambda state: state.value)
    def test_each_engine_state_projects_the_same_on_both_sides(self, engine_state, ts_projection):
        """Parametrized per state so a failure names the one that drifted.

        `superseded` is included: it maps to nothing on both sides, and a side that
        started bucketing it would double-count one piece of work.
        """
        backend = ENGINE_TO_DISPLAY[engine_state]
        expected = None if backend is None else backend.value

        assert ts_projection[engine_state.value] == expected

    def test_rejected_at_gate_is_stalled_on_both_sides(self, ts_projection):
        """The substring trap, called out explicitly in `nodeState.ts`.

        `rejected_at_gate` contains "rejected" — the banned phantom state the
        engine raises `ValueError` on — so any check written against substrings
        rather than whole keys mishandles it. It is a real state that needs a human
        to move it, which is what "Stalled — needs help" says.
        """
        assert ts_projection["rejected_at_gate"] == DisplayState.STALLED.value
        assert ENGINE_TO_DISPLAY[NodeState.REJECTED_AT_GATE] is DisplayState.STALLED

    def test_neither_side_carries_a_phantom_state(self, ts_projection):
        """`rejected` and `skipped` are from an earlier draft; the engine rejects both.

        Matched as whole keys, not substrings, for the reason above.
        """
        for phantom in ("rejected", "skipped"):
            assert phantom not in ts_projection
            assert phantom not in {state.value for state in NodeState}

    def test_the_five_display_states_are_the_same_closed_vocabulary(self, ts_projection):
        source = _ts_source()

        assert set(_parse_ts_order(source)) == {state.value for state in DisplayState}
        assert {value for value in ts_projection.values() if value is not None} == {state.value for state in DisplayState}

    def test_render_order_matches(self):
        """The list page's rollup segments must read in the graph page's order.

        Same flow, two screens, two different left-to-right orders is a reading
        error waiting to happen — an operator compares the wrong segments.
        """
        assert _parse_ts_order(_ts_source()) == [state.value for state in DISPLAY_STATE_ORDER]


class TestDerivedFilterLists:
    """The inverted mapping is what SQL counts with, so it is checked directly."""

    def test_every_bucket_lists_exactly_the_states_that_project_into_it(self, ts_projection):
        """A correct projection inverted wrongly drifts just as badly.

        Derived from the *TypeScript* side, so this closes the loop: the states the
        database filters on are the states the UI would have bucketed.
        """
        expected: dict[str, set[str]] = {state.value: set() for state in DisplayState}
        for engine_state, display in ts_projection.items():
            if display is not None:
                expected[display].add(engine_state)

        assert {display.value: set(states) for display, states in DISPLAY_TO_ENGINE.items()} == expected

    def test_superseded_appears_in_no_bucket(self):
        """Structural exclusion, not a `state != 'superseded'` predicate.

        A predicate is something a later edit can drop; absence from the inverted
        mapping cannot be dropped by accident.
        """
        for states in DISPLAY_TO_ENGINE.values():
            assert NodeState.SUPERSEDED.value not in states

    def test_the_buckets_partition_the_eight_countable_states(self):
        """No state counted twice, and none of the eight left out.

        Overlap would make `total_nodes` exceed a flow's real node count; a gap
        would understate it. Both make the rollup bar disagree with the graph.
        """
        listed = [state for states in DISPLAY_TO_ENGINE.values() for state in states]

        assert len(listed) == len(set(listed)), f"a state is in two buckets: {listed}"
        assert set(listed) == {state.value for state in NodeState} - {NodeState.SUPERSEDED.value}
