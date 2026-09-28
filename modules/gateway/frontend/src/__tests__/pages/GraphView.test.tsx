vi.mock('@/components/budget/BudgetEnforcementControl', () => ({ BudgetEnforcementControl: () => <div data-testid="budget-enforcement-control" /> }));
/**
 * Tests for the delivery-journey graph view — issue #4212.
 *
 * Several of this story's acceptance criteria are assertions *about the
 * implementation*, not about rendered output, and they are grouped in
 * `describe('source-level guarantees')` at the bottom. They read files off disk
 * on purpose: "does not import `usePollingStatus`" and "no glyph map contains
 * `rejected`" cannot be observed from the DOM, and a rendering test would pass
 * happily while the forbidden import sat there.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within, act, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import GraphView from '@/pages/GraphView';
import type { FlowGraph, GraphNode, NodeEngineState } from '@/types/orchestration';

vi.mock('@/services/orchestration', () => ({
  getFlowGraph: vi.fn(),
  // Issue #4213: the view now renders `GateControls` per node. This suite is
  // about layout and cost, so the controls are stubbed out at their two
  // dependencies rather than by wrapping every case in an AuthProvider —
  // `GateControls` has its own suite for permission and mutation behaviour.
  approveGate: vi.fn(),
  rejectGate: vi.fn(),
  resumeNode: vi.fn(),
  getGatePlanPreview: vi.fn(),
  // Issue #5145: the page now also reads the delivery ledger. Stubbed here (and
  // left unresolved) so this suite keeps asserting the graph exactly as it does
  // today — the ledger panel has its own suite in `GraphViewExecution.test.tsx`.
  // Declared rather than omitted: an absent export would make the hook throw
  // instead of simply having no data, which is a different code path.
  getFlowExecution: vi.fn(),
}));

// Without a permission the controls render nothing, which is the correct
// baseline for a layout suite: no extra buttons in the chips being asserted on.
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ hasPermission: () => false }),
}));

import { getFlowGraph, getFlowExecution } from '@/services/orchestration';

const mockGetFlowGraph = getFlowGraph as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeNode(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    id: `id-${overrides.node_ref ?? 'story-a'}`,
    epic_ref: 'epic-1',
    wave_ref: 'wave-1',
    node_ref: 'story-a',
    kind: 'story',
    title: 'Story A',
    state: 'pending',
    stalled: false,
    issue_ref: null,
    attempts: 0,
    cost: { status: 'unknown', amount_usd: null, reason: 'not_started', scope: 'agent run costs only; excludes build/infra' },
    ...overrides,
  };
}

function makeGraph(overrides: Partial<FlowGraph> = {}): FlowGraph {
  return {
    flow_id: 'flow-1',
    slug: 'delivery-loop',
    title: 'Delivery loop',
    intent_ref: '4120',
    state: 'running',
    nodes: [makeNode()],
    edges: [],
    cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', partial: true, scope: 'agent run costs only; excludes build/infra' },
    ...overrides,
  };
}

/** A fresh client per test. `retry: false` so an error test fails on the first try. */
function renderGraph() {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        // Mirrors the app's global staleTime — the setting the polling ACs are
        // about. If the hook depended on cache invalidation instead of an
        // explicit interval, the polling test below would catch it here.
        staleTime: 5 * 60 * 1000,
      },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/flows/flow-1']}>
        <Routes>
          <Route path="/flows/:flowId" element={<GraphView />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetFlowGraph.mockResolvedValue(makeGraph());
  // A failing ledger read is the baseline for this suite, and it doubles as an
  // assertion by construction: every test below still passes, which is what
  // "a ledger failure must not blank the plan" means in practice.
  (getFlowExecution as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no ledger in this suite'));
});

// ---------------------------------------------------------------------------
// Issue #5145 — a failed ledger read is stated, not hidden
// ---------------------------------------------------------------------------

describe('a failed execution read is visible and non-blocking', () => {
  /**
   * This suite's baseline is already a REJECTED ledger read (see `beforeEach`), so
   * every other test here proves the graph survives one. What none of them prove is
   * that the failure is *stated*.
   *
   * Discarding `isError` makes a denied read and an unbuilt feature pixel-identical:
   * the panel simply is not there. That is the worst outcome for whoever is debugging
   * it, because the screen actively suggests there is nothing to debug — and it hides
   * precisely the symptom a tenant-scoping failure would produce, which is the same
   * defect the backend's own tenant-predicate test guards from the other end.
   */
  it('uses current display states for both graph nodes and rollup counts', async () => {
    mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
      makeNode({ node_ref: 'active', state: 'running', display_state: 'in_progress' }),
      makeNode({ node_ref: 'blocked', state: 'running', display_state: 'stalled' }),
      makeNode({ node_ref: 'capacity', state: 'running', display_state: 'queued' }),
      makeNode({ node_ref: 'delivered', state: 'passed', stalled: true, display_state: 'stalled' }),
      makeNode({ node_ref: 'gate-done', kind: 'gate', state: 'passed' }),
    ] }));
    renderGraph();
    await waitFor(() => expect(screen.getByTestId('story-completion-count')).toHaveTextContent('1 of 4 stories complete'));
    for (const [state, count] of Object.entries({ in_progress: 1, stalled: 1, queued: 1, complete: 2 })) {
      expect(screen.getAllByTestId(`rollup-segment-${state}`)[0]).toHaveAttribute('data-count', String(count));
    }
    expect(screen.getByTestId('node-blocked')).toHaveAttribute('data-display-state', 'stalled');
    expect(screen.getByTestId('node-blocked')).not.toHaveAttribute('aria-current');
    expect(screen.getByTestId('node-capacity')).toHaveAttribute('data-display-state', 'queued');
    expect(screen.getByTestId('node-delivered')).toHaveAttribute('data-display-state', 'complete');
    expect(within(screen.getByTestId('node-delivered')).queryByText('Stalled')).not.toBeInTheDocument();
  });

  it('says execution progress is unavailable rather than silently omitting it', async () => {
    renderGraph();

    const notice = await screen.findByTestId('execution-unavailable');
    expect(notice).toHaveTextContent(/execution progress is unavailable/i);
    // It must not overstate the damage: the plan below is intact and current.
    expect(notice).toHaveTextContent(/delivery plan below is current/i);
    // And it must not imply delivery stopped — the view cannot tell either way.
    expect(notice).toHaveTextContent(/does not mean delivery has stopped/i);
  });

  it('leaves the graph fully usable when the ledger read fails', async () => {
    // The graph is the older, load-bearing view. A ledger outage degrades one panel;
    // it must not blank the plan, which is why the two are separate queries.
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({ nodes: [makeNode({ node_ref: 'story-a', title: 'Story A', state: 'running' })] })
    );
    renderGraph();

    expect(await screen.findByTestId('execution-unavailable')).toBeInTheDocument();
    expect(screen.getByText('Story A')).toBeInTheDocument();
    // Not the whole-page error state: that is reserved for the graph itself failing.
    expect(screen.queryByTestId('graph-error')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC-1 — the whole journey, pending included
// ---------------------------------------------------------------------------

describe('AC-1: the full journey renders, including work that has not run', () => {
  it('renders nodes that have never run', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'story-done', title: 'Done story', state: 'passed', cost: { status: 'known', amount_usd: '4.00' } }),
          makeNode({ node_ref: 'story-future', title: 'Future story', state: 'pending' }),
        ],
      })
    );
    renderGraph();

    expect(await screen.findByText('Future story')).toBeInTheDocument();
    expect(screen.getByText('Done story')).toBeInTheDocument();
  });

  it('counts pending nodes in the rollup, so the bar shows work remaining', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'a', state: 'passed' }),
          makeNode({ node_ref: 'b', state: 'pending' }),
          makeNode({ node_ref: 'c', state: 'pending' }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('rollup-bar');
    // The whole point of AC-1: 1 of 3 done, not "100% complete".
    expect(screen.getByTestId('rollup-segment-queued')).toHaveAttribute('data-count', '2');
    expect(screen.getByTestId('rollup-segment-complete')).toHaveAttribute('data-count', '1');
  });

  it('names the blocking predecessor on a queued node', async () => {
    const first = makeNode({ node_ref: 'first', title: 'Build the thing', state: 'running' });
    const second = makeNode({ node_ref: 'second', title: 'Ship the thing', state: 'pending' });
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({ nodes: [first, second], edges: [{ from_node_id: first.id, to_node_id: second.id }] })
    );
    renderGraph();

    expect(await screen.findByTestId('node-blocked-by-second')).toHaveTextContent('Waiting on Build the thing');
  });

  it('does not name a predecessor that already finished', async () => {
    const first = makeNode({ node_ref: 'first', title: 'Finished work', state: 'passed' });
    const second = makeNode({ node_ref: 'second', state: 'pending' });
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({ nodes: [first, second], edges: [{ from_node_id: first.id, to_node_id: second.id }] })
    );
    renderGraph();

    await screen.findByTestId('node-second');
    expect(screen.queryByTestId('node-blocked-by-second')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC-2 — parallel branches are branches, not a list
// ---------------------------------------------------------------------------

describe('AC-2: parallel work renders as distinct branches', () => {
  it('renders unconnected nodes in one wave as separate branches', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'left', title: 'Left branch' }),
          makeNode({ node_ref: 'right', title: 'Right branch' }),
        ],
        edges: [],
      })
    );
    renderGraph();

    const wave = await screen.findByTestId('wave-epic-1-wave-1');
    expect(within(wave).getByText('Parallel paths · 2 steps')).toBeVisible();
    expect(within(wave).getAllByTestId(/^stage-/)).toHaveLength(1);
  });

  it('renders chained steps in successive dependency groups', async () => {
    // The discriminator for AC-2: sharing a wave is not sufficient for
    // concurrency. These two are sequenced by an edge, so rendering them side by
    // side would claim parallelism the engine will not deliver.
    const a = makeNode({ node_ref: 'a', title: 'First' });
    const b = makeNode({ node_ref: 'b', title: 'Second' });
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({ nodes: [a, b], edges: [{ from_node_id: a.id, to_node_id: b.id }] })
    );
    renderGraph();

    const wave = await screen.findByTestId('wave-epic-1-wave-1');
    expect(within(wave).queryByText(/Parallel paths/)).not.toBeInTheDocument();
    expect(within(wave).getAllByTestId(/^stage-/)).toHaveLength(2);
  });

  it('renders a three-way fan-out as three branches', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: ['a', 'b', 'c'].map((ref) => makeNode({ node_ref: ref, title: `Branch ${ref}` })),
      })
    );
    renderGraph();

    const wave = await screen.findByTestId('wave-epic-1-wave-1');
    expect(within(wave).getByText('Parallel paths · 3 steps')).toBeVisible();
  });

  it('separates waves within an EPIC and EPICs from each other', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'a', epic_ref: 'epic-1', wave_ref: 'wave-1' }),
          makeNode({ node_ref: 'b', epic_ref: 'epic-1', wave_ref: 'wave-2' }),
          makeNode({ node_ref: 'c', epic_ref: 'epic-2', wave_ref: 'wave-1' }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('epic-epic-1');
    expect(screen.getByTestId('epic-epic-2')).toBeInTheDocument();
    expect(screen.getByTestId('wave-epic-1-wave-1')).toBeInTheDocument();
    expect(screen.getByTestId('wave-epic-1-wave-2')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC-3 — current position; stalled and halted vs running
// ---------------------------------------------------------------------------

describe('AC-3: current position, and stalled/halted distinguishable from running', () => {
  it('marks the running node as the current position', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [makeNode({ node_ref: 'a', state: 'passed' }), makeNode({ node_ref: 'b', state: 'running' })],
      })
    );
    renderGraph();

    const current = await screen.findByTestId('node-b');
    expect(current).toHaveAttribute('aria-current', 'step');
    expect(screen.getByTestId('node-a')).not.toHaveAttribute('aria-current');
  });

  it('treats a gate-waiting node as the current position too', async () => {
    // A flow parked on a gate has its position AT that gate; an operator asking
    // "where are we" needs that answered whether or not tokens are burning.
    mockGetFlowGraph.mockResolvedValue({ ...makeGraph(), nodes: [makeNode({ node_ref: 'g', kind: 'gate', state: 'awaiting_gate' })] });
    renderGraph();

    expect(await screen.findByTestId('node-g')).toHaveAttribute('aria-current', 'step');
  });

  it('distinguishes stalled from running by state and by badge', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'run', state: 'running' }),
          makeNode({ node_ref: 'stall', state: 'failed', stalled: true }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('node-run');
    expect(screen.getByTestId('node-run')).toHaveAttribute('data-display-state', 'in_progress');
    expect(screen.getByTestId('node-stall')).toHaveAttribute('data-display-state', 'stalled');
    expect(screen.getByTestId('node-reason-stall')).toHaveTextContent('Stalled');
  });

  it('distinguishes HALTED from stalled, though both need intervention', async () => {
    // The two share a fill because both need a human, so the badge is the only
    // thing that separates "the engine gave up" from "someone stopped this".
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'stall', state: 'failed', stalled: true }),
          makeNode({ node_ref: 'halt', state: 'halted' }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('node-halt');
    expect(screen.getByTestId('node-reason-halt')).toHaveTextContent('Halted');
    expect(screen.getByTestId('node-reason-stall')).toHaveTextContent('Stalled');
  });

  it('does not mark a stalled node as the current position', async () => {
    mockGetFlowGraph.mockResolvedValue({ ...makeGraph(), nodes: [makeNode({ node_ref: 's', state: 'failed', stalled: true })] });
    renderGraph();

    expect(await screen.findByTestId('node-s')).not.toHaveAttribute('aria-current');
  });

  it('labels a plainly failed node "Failed", not "Stalled"', async () => {
    mockGetFlowGraph.mockResolvedValue({ ...makeGraph(), nodes: [makeNode({ node_ref: 'f', state: 'failed', stalled: false })] });
    renderGraph();

    expect(await screen.findByTestId('node-reason-f')).toHaveTextContent('Failed');
  });
});

// ---------------------------------------------------------------------------
// AC-4 / AC-22 — three-valued cost, scope labels, never $0.00 for unknown
// ---------------------------------------------------------------------------

describe('AC-4/AC-22: cost is three-valued and unknown never renders $0.00', () => {
  it('renders an unknown node cost as the no-data indicator, never $0.00', async () => {
    mockGetFlowGraph.mockResolvedValue({
      ...makeGraph(),
      nodes: [makeNode({ node_ref: 'a', cost: { status: 'unknown', amount_usd: null, reason: 'not_started' } })],
    });
    renderGraph();

    const node = await screen.findByTestId('node-a');
    const cost = within(node).getByTestId('cost-figure');
    expect(cost).toHaveTextContent('—');
    expect(cost).not.toHaveTextContent('$0.00');
  });

  it('renders unknown as the indicator even when an amount is wrongly attached', async () => {
    // The exact seam where absence becomes $0.00: the status is authoritative
    // over the number, so a bad payload still must not print a currency figure.
    mockGetFlowGraph.mockResolvedValue({
      ...makeGraph(),
      nodes: [makeNode({ node_ref: 'a', cost: { status: 'unknown', amount_usd: '0', reason: 'no_usage_rows' } })],
    });
    renderGraph();

    const node = await screen.findByTestId('node-a');
    expect(within(node).getByTestId('cost-figure')).not.toHaveTextContent('$0.00');
  });

  it('renders a VERIFIED zero as $0.00 — that one is a real measurement', async () => {
    mockGetFlowGraph.mockResolvedValue({
      ...makeGraph(),
      nodes: [makeNode({ node_ref: 'a', cost: { status: 'none_incurred', amount_usd: '0' } })],
    });
    renderGraph();

    const node = await screen.findByTestId('node-a');
    expect(within(node).getByTestId('cost-figure')).toHaveTextContent('$0.00');
  });

  it('renders a known amount with sub-cent precision', async () => {
    mockGetFlowGraph.mockResolvedValue({
      ...makeGraph(),
      nodes: [makeNode({ node_ref: 'a', cost: { status: 'known', amount_usd: '0.001234' } })],
    });
    renderGraph();

    const node = await screen.findByTestId('node-a');
    expect(within(node).getByTestId('cost-figure')).toHaveTextContent('$0.0012');
  });

  it('explains every unknown figure rather than showing a bare dash', async () => {
    mockGetFlowGraph.mockResolvedValue({
      ...makeGraph(),
      nodes: [makeNode({ node_ref: 'a', cost: { status: 'unknown', amount_usd: null, reason: 'not_costable' } })],
    });
    renderGraph();

    const node = await screen.findByTestId('node-a');
    expect(within(node).getByTestId('cost-figure')).toHaveAttribute(
      'title',
      expect.stringContaining('nothing to bill')
    );
  });

  it('carries the scope label on every figure, including the rollup', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [makeNode({ node_ref: 'a', cost: { status: 'known', amount_usd: '2.00', scope: 'agent run costs only; excludes build/infra' } })],
        cost: { status: 'known', amount_usd: '2.00', partial: false, scope: 'agent run costs only; excludes build/infra' },
      })
    );
    renderGraph();

    // Visible on the headline figure — the one most likely to be screenshotted
    // into a budget conversation without its caveat.
    expect(await screen.findByText(/agent run costs only; excludes build\/infra/)).toBeInTheDocument();
    const node = screen.getByTestId('node-a');
    expect(within(node).getByTestId('cost-figure')).toHaveAttribute('title', expect.stringContaining('agent run costs only'));
  });

  it('marks a partial rollup as a lower bound, not a total', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [makeNode({ node_ref: 'a' })],
        cost: { status: 'known', amount_usd: '10.00', partial: true, unknown_node_count: 1 },
      })
    );
    renderGraph();

    const figures = await screen.findAllByTestId('cost-figure');
    const rollup = figures[0];
    expect(rollup).toHaveTextContent('≥$10.00');
    expect(rollup).toHaveAttribute('title', expect.stringContaining('At least this much'));
  });
});

// ---------------------------------------------------------------------------
// Taxonomy — containers group, runs are not vertices
// ---------------------------------------------------------------------------

describe('taxonomy: containers group, runs are detail', () => {
  it('renders EPIC and wave as grouping containers, never as nodes', async () => {
    mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [makeNode({ node_ref: 'a' })] }));
    renderGraph();

    const epic = await screen.findByTestId('epic-epic-1');
    expect(epic).toHaveAttribute('data-container', 'epic');
    expect(screen.getByTestId('wave-epic-1-wave-1')).toHaveAttribute('data-container', 'wave');
    // Expanding a container does not execute anything.
    expect(epic).not.toHaveAttribute('data-display-state');
    expect(epic.tagName).toBe('SECTION');
  });

  it('gives containers no state fill and no aria-current', async () => {
    mockGetFlowGraph.mockResolvedValue({ ...makeGraph(), nodes: [makeNode({ node_ref: 'a', state: 'running' })] });
    renderGraph();

    const epic = await screen.findByTestId('epic-epic-1');
    expect(epic).not.toHaveAttribute('aria-current');
    expect(within(epic).getByTestId('node-a')).toHaveAttribute('aria-current', 'step');
  });

  it('renders retries as an attempt count on ONE node, not as extra nodes', async () => {
    // §8.4: story is the graph floor. Four tries is one node with four attempts;
    // rendering runs as vertices would make retry look like fan-out.
    mockGetFlowGraph.mockResolvedValue({ ...makeGraph(), nodes: [makeNode({ node_ref: 'a', attempts: 4, state: 'running' })] });
    renderGraph();

    expect(await screen.findByTestId('node-attempts-a')).toHaveTextContent('4 attempts');
    expect(screen.getAllByTestId(/^node-a$/)).toHaveLength(1);
  });

  it('renders only the three executable kinds', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 's', kind: 'story' }),
          makeNode({ node_ref: 'e', kind: 'eval' }),
          makeNode({ node_ref: 'g', kind: 'gate' }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('node-s');
    for (const [ref, kind] of [['s', 'story'], ['e', 'eval'], ['g', 'gate']]) {
      expect(screen.getByTestId(`node-${ref}`)).toHaveAttribute('data-node-kind', kind);
    }
  });

  it('does not count a superseded node in the rollup', async () => {
    // Counting it would double-count one piece of work, pushing the bar's totals
    // past the real node count.
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [makeNode({ node_ref: 'old', state: 'superseded' }), makeNode({ node_ref: 'new', state: 'running' })],
      })
    );
    renderGraph();

    await screen.findByTestId('rollup-bar');
    expect(screen.getByTestId('rollup-segment-in_progress')).toHaveAttribute('data-count', '1');
    expect(screen.queryByTestId('rollup-segment-queued')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Accessibility (§9.4)
// ---------------------------------------------------------------------------

describe('accessibility: the bar is described in render order (§9.4)', () => {
  it('enumerates states in the same order they render', async () => {
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'done', state: 'passed' }),
          makeNode({ node_ref: 'queued', state: 'pending' }),
          makeNode({ node_ref: 'run', state: 'running' }),
        ],
      })
    );
    renderGraph();

    const bar = await screen.findByTestId('rollup-bar');
    const label = bar.getAttribute('aria-label') ?? '';
    // Render order is queued → in progress → complete, regardless of the order
    // the nodes arrived in. A hand-written label would drift from the segments.
    expect(label.indexOf('Queued')).toBeLessThan(label.indexOf('In progress'));
    expect(label.indexOf('In progress')).toBeLessThan(label.indexOf('Complete'));
    expect(label).toContain('3 work items');
  });

  it('does not rely on colour alone — every state has a label in the legend', async () => {
    renderGraph();

    const legend = await screen.findByTestId('rollup-legend');
    for (const label of ['Complete', 'In progress', 'Waiting on a gate', 'Stalled — needs help', 'Queued']) {
      expect(within(legend).getByText(new RegExp(label.replace(/[.*+?^${}()|[\]\\—]/g, '\\$&')))).toBeInTheDocument();
    }
  });
});

// ---------------------------------------------------------------------------
// Not-found — never an empty graph
// ---------------------------------------------------------------------------

describe('a flow that cannot be read shows not-found, never an empty graph', () => {
  it('shows an error rather than a zero-node journey', async () => {
    mockGetFlowGraph.mockRejectedValue({ error: 'Not Found', message: 'no orchestration flow' });
    renderGraph();

    expect(await screen.findByTestId('graph-error')).toBeInTheDocument();
    // "No nodes" would read as "no work left" — a different, more misleading
    // answer than "not found".
    expect(screen.queryByTestId('rollup-bar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('graph-empty')).not.toBeInTheDocument();
  });

  it('distinguishes a genuinely empty flow from an unreadable one', async () => {
    mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [] }));
    renderGraph();

    expect(await screen.findByTestId('graph-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('graph-error')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Live refresh — the adversarial polling assertions
// ---------------------------------------------------------------------------

describe('live refresh: polling is explicit, not inherited from staleTime', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('refetches on its own interval despite a 5-minute global staleTime', async () => {
    // The adversarial case. The QueryClient here carries the app's real
    // staleTime: 5 * 60 * 1000. A view relying on cache invalidation would sit
    // unchanged for five minutes and still LOOK live, because the data is real,
    // just old. Advancing 30s must produce a second call.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderGraph();

    await waitFor(() => expect(mockGetFlowGraph).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(mockGetFlowGraph).toHaveBeenCalledTimes(2);
  });

  it('keeps polling on subsequent intervals', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderGraph();
    await waitFor(() => expect(mockGetFlowGraph).toHaveBeenCalledTimes(1));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000);
    });

    expect(mockGetFlowGraph.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it('does not blank the graph while a background poll is in flight', async () => {
    // `placeholderData: keepPreviousData` — the v4 boolean form is a silent no-op
    // on the pinned 5.62.0, which would flash a spinner twice a minute.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [makeNode({ node_ref: 'a', title: 'Persisted story' })] }));
    renderGraph();
    expect(await screen.findByText('Persisted story')).toBeInTheDocument();

    let release: (value: FlowGraph) => void = () => {};
    mockGetFlowGraph.mockReturnValue(new Promise<FlowGraph>((resolve) => { release = resolve; }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    // Still on screen mid-flight, not replaced by the loading state.
    expect(screen.getByText('Persisted story')).toBeInTheDocument();
    expect(screen.queryByTestId('graph-loading')).not.toBeInTheDocument();

    await act(async () => {
      release(makeGraph({ nodes: [makeNode({ node_ref: 'a', title: 'Persisted story' })] }));
    });
  });
});

// ---------------------------------------------------------------------------
// Source-level guarantees — not observable from the DOM
// ---------------------------------------------------------------------------

describe('source-level guarantees', () => {
  const read = (relative: string) => readFileSync(resolve(__dirname, '../../', relative), 'utf8');

  /**
   * Source with comments stripped.
   *
   * The forbidden-pattern guards below must scan **code**, not prose. Scanning
   * raw text makes documenting a rule violate it — the first version of this
   * suite failed because `useFlowGraph.ts`'s docstring explains *why*
   * `usePollingStatus` and `keepPreviousData: true` are wrong, and because
   * `nodeState.ts` documents that `rejected` is a phantom state. A guard whose
   * only escape is deleting the explanation trains exactly the wrong reflex, so
   * it strips comments and asserts on what actually executes.
   */
  const readCode = (relative: string) =>
    read(relative)
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/(^|[^:])\/\/.*$/gm, '$1');

  const GRAPH_SOURCES = [
    'hooks/useFlowGraph.ts',
    'pages/GraphView.tsx',
    'utils/nodeState.ts',
    'utils/flowLayout.ts',
    'components/orchestration/RollupBar.tsx',
    'components/orchestration/NodeChip.tsx',
    'components/orchestration/CostFigureDisplay.tsx',
    // Issue #5145 — the delivery execution panel mounts inside this same page, so
    // it inherits every guard in this block. Listing it here is the point: a second
    // polling query on the graph route is exactly where the shared-cadence and
    // extra-dependency rules would otherwise be quietly re-broken.
    'hooks/useFlowExecution.ts',
    'utils/executionProgress.ts',
    'components/orchestration/ExecutionProgress.tsx',
  ];

  it('sets refetchInterval explicitly in the hook', () => {
    // Asserted at source level as well as behaviourally: the behavioural test
    // above would also pass if someone reached the same cadence via a global
    // default, which is the thing §3.3.1 forbids.
    expect(readCode('hooks/useFlowGraph.ts')).toMatch(/refetchInterval:\s*(FLOW_GRAPH_REFETCH_INTERVAL_MS|30_000)/);
  });

  it('never imports usePollingStatus (§3.3.1 MUST NOT)', () => {
    for (const file of GRAPH_SOURCES) {
      expect(readCode(file), `${file} must not import usePollingStatus`).not.toMatch(/usePollingStatus/);
    }
  });

  it('does not rely on the global staleTime', () => {
    // A local `staleTime` on this query would reintroduce the dependency the
    // explicit interval exists to remove.
    expect(readCode('hooks/useFlowGraph.ts')).not.toMatch(/staleTime/);
  });

  it('uses the named keepPreviousData import, not the v4 boolean', () => {
    const source = readCode('hooks/useFlowGraph.ts');
    expect(source).toMatch(/placeholderData:\s*keepPreviousData/);
    // `keepPreviousData: true` is accepted silently and does nothing on 5.62.0.
    expect(source).not.toMatch(/keepPreviousData:\s*true/);
  });

  it('has no phantom state: no glyph map mentions `rejected` or `skipped`', () => {
    // Matched as whole keys, not substrings — `rejected_at_gate` is a REAL engine
    // state that legitimately contains "rejected", and a naive substring check
    // would fail on valid code and push someone to delete correct handling.
    const source = readCode('utils/nodeState.ts');
    expect(source).not.toMatch(/(?<![a-z_])rejected(?![a-z_])/);
    expect(source).not.toMatch(/(?<![a-z_])skipped(?![a-z_])/);
  });

  it('handles rejected_at_gate, which is the real state that is NOT a phantom', () => {
    expect(readCode('utils/nodeState.ts')).toContain('rejected_at_gate');
  });

  it('adds no runtime dependency and no graph library (§9: BUILD not adopt)', () => {
    for (const file of GRAPH_SOURCES) {
      const imports = [...readCode(file).matchAll(/from\s+'([^']+)'/g)].map((m) => m[1]);
      for (const specifier of imports) {
        const allowed =
          specifier.startsWith('@/') ||
          specifier.startsWith('.') ||
          specifier === 'react-router-dom' ||
          specifier === '@tanstack/react-query' ||
          specifier === 'react';
        expect(allowed, `${file} imports unexpected package ${specifier}`).toBe(true);
      }
    }
  });

  it('uses the /orchestration prefix, not /api/orchestration', () => {
    // CloudFront's strip-api-prefix removes the leading /api, and apiClient
    // already prepends VITE_API_URL. Spelling /api here yields /api/api/... which
    // hits the SPA fallback and returns HTML with a 200 (issue #4330).
    const source = readCode('services/orchestration.ts');
    expect(source).toMatch(/`\/orchestration\/flows\//);
    expect(source).not.toMatch(/['`]\/api\//);
  });

  it('renders the five contract states with their mandated dark text on amber and grey', () => {
    // White on #fab219 or #c3c2b7 fails contrast; the contract mandates the dark
    // pairing rather than leaving it to each call site.
    const source = readCode('utils/nodeState.ts');
    expect(source).toContain("fill: '#fab219'");
    expect(source).toContain("text: '#5c4200'");
    expect(source).toContain("fill: '#c3c2b7'");
    expect(source).toContain("text: '#52514e'");
  });
});

// ---------------------------------------------------------------------------
// Projection unit tests — every engine state maps somewhere deliberate
// ---------------------------------------------------------------------------

describe('the nine→five projection covers every engine state', () => {
  it('maps all nine states without falling through by accident', async () => {
    const { engineStateToDisplayState } = await import('@/utils/nodeState');
    const expected: Record<NodeEngineState, string | null> = {
      pending: 'queued',
      ready: 'queued',
      running: 'in_progress',
      awaiting_gate: 'gate',
      passed: 'complete',
      rejected_at_gate: 'stalled',
      failed: 'stalled',
      halted: 'stalled',
      superseded: null,
    };
    for (const [state, want] of Object.entries(expected)) {
      expect(engineStateToDisplayState(state as NodeEngineState), state).toBe(want);
    }
  });

  it('prefers the stall flag over the raw state', async () => {
    const { toDisplayState } = await import('@/utils/nodeState');
    // A stall lands the node in `failed`, so reading `state` first erases the
    // difference between "needs a human" and "broke".
    expect(toDisplayState({ state: 'failed', stalled: true })).toBe('stalled');
    expect(toDisplayState({ state: 'running', stalled: false })).toBe('in_progress');
  });

  it('gives an unrecognised future state no segment rather than a sixth colour', async () => {
    const { engineStateToDisplayState } = await import('@/utils/nodeState');
    expect(engineStateToDisplayState('some_future_state' as NodeEngineState)).toBeNull();
  });
});


it('counts Task API evaluation issues as stories and keeps checkpoints and historical attempts separate', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
    ...Array.from({ length: 9 }, (_, i) => makeNode({
      node_ref: `t${i}`, wave_ref: 'component-delivery', issue_ref: String(5793 + i),
      state: i === 0 ? 'passed' : 'pending',
    })),
    ...Array.from({ length: 5 }, (_, i) => makeNode({
      node_ref: `v${i + 1}`, kind: 'eval', wave_ref: `validation-v${i + 1}`, issue_ref: String(5802 + i),
      state: i === 0 ? 'passed' : 'pending',
    })),
    makeNode({ node_ref: 'checkpoint', kind: 'eval', wave_ref: 'component-delivery', state: 'passed' }),
    makeNode({ node_ref: 'accept', kind: 'gate', wave_ref: 'component-delivery', state: 'passed' }),
    makeNode({ node_ref: 'old-v1', kind: 'eval', wave_ref: 'validation-v1', issue_ref: '5802', state: 'superseded' }),
  ] }));
  renderGraph();
  const summary = await screen.findByTestId('plan-summary');
  expect(summary).toHaveTextContent('14 stories across 6 waves');
  expect(summary).toHaveTextContent('9 implementation · 5 evaluation · 1 approval gate · 1 evaluation checkpoint');
  expect(screen.getByTestId('story-completion-count')).toHaveTextContent('2 of 14 stories complete');
  expect(screen.getByTestId('wave-epic-1-component-delivery')).toHaveTextContent('9 implementation stories');
  for (let i = 1; i <= 5; i++) {
    const wave = screen.getByTestId(`wave-epic-1-validation-v${i}`);
    expect(within(wave).getByRole('heading')).toHaveTextContent('1 evaluation story');
    expect(within(wave).getByRole('heading')).not.toHaveTextContent('0 stories');
    expect(wave).toHaveTextContent(`${i === 1 ? 1 : 0} of 1 stories complete`);
    expect(within(wave).getByText('Evaluation story')).toBeInTheDocument();
  }
});

it('does not label a checkpoint-only wave as an evaluation story', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [makeNode({ node_ref: 'check', kind: 'eval' })] }));
  renderGraph();
  const wave = await screen.findByTestId('wave-epic-1-wave-1');
  expect(within(wave).getByRole('heading')).toHaveTextContent('1 evaluation checkpoint');
  expect(wave).not.toHaveTextContent('0 of 0 stories complete');
  expect(screen.queryByTestId('story-completion-count')).not.toBeInTheDocument();
});

it('separates stories from controls and explains a persisted change request', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
    makeNode({ node_ref: 'story-1' }),
    makeNode({ node_ref: 'story-2', wave_ref: 'wave-2' }),
    makeNode({ node_ref: 'old-story', state: 'superseded' }),
    makeNode({ node_ref: 'gate', kind: 'gate', state: 'rejected_at_gate',
      last_gate_decision: { action: 'changes_requested', reason: '[input-path=dashboard] Clarify the migration plan', created_at: '2026-09-14T00:00:00Z' } }),
    makeNode({ node_ref: 'eval', kind: 'eval' }),
  ] }));
  renderGraph();
  expect(await screen.findByText('2 stories across 2 waves')).toBeInTheDocument();
  expect(screen.getByTestId('plan-summary')).toHaveTextContent('2 implementation · 0 evaluation · 1 approval gate · 1 evaluation checkpoint');
  // Two alerts can coexist on this page now: the change request asserted here and
  // the #5145 ledger-read notice, which this suite's rejected `getFlowExecution`
  // baseline raises in every test. The query names which one it means rather than
  // relying on there being exactly one.
  const changeRequest = screen
    .getAllByRole('alert')
    .find((alert) => alert.textContent?.includes('Changes requested'));
  expect(changeRequest).toHaveTextContent('does not start an agent to revise the plan');
  expect(screen.getByTestId('gate-feedback')).toHaveTextContent('Clarify the migration plan');
  expect(screen.getByTestId('gate-feedback')).not.toHaveTextContent('input-path');
  expect(screen.getByTestId('node-old-story')).not.toBeVisible();
  expect(screen.getByText(/Superseded steps \(1\)/)).toBeVisible();
});

it('explains a change request submitted without a note through the old dashboard', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
    makeNode({ node_ref: 'gate', kind: 'gate', state: 'rejected_at_gate',
      last_gate_decision: { action: 'changes_requested', reason: '[input-path=dashboard]', created_at: '2026-09-14T00:02:07Z' } }),
  ] }));
  renderGraph();
  expect(await screen.findByText('No change description was provided.')).toBeVisible();
  expect(screen.getByTestId('gate-feedback')).not.toHaveTextContent('input-path');
});


it('opens the first unfinished wave and supports collapse and expand all', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
    makeNode({ node_ref: 'done', wave_ref: 'wave-1', state: 'passed' }),
    makeNode({ node_ref: 'current', wave_ref: 'wave-2' }),
    makeNode({ node_ref: 'future', wave_ref: 'wave-3' }),
  ] }));
  renderGraph();
  const done = await screen.findByTestId('wave-epic-1-wave-1');
  const current = screen.getByTestId('wave-epic-1-wave-2');
  const future = screen.getByTestId('wave-epic-1-wave-3');
  expect(within(done).getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  expect(within(current).getByRole('button')).toHaveAttribute('aria-expanded', 'true');
  expect(within(future).getByRole('button')).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByTestId('node-future')).not.toBeVisible();
  fireEvent.click(within(current).getByRole('button'));
  expect(screen.getByTestId('node-current')).not.toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Expand all waves' }));
  expect(screen.getByTestId('node-future')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Collapse all waves' }));
  expect(screen.getByTestId('node-future')).not.toBeVisible();
});

it('keeps later waves open when they need review or attention', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [
    makeNode({ node_ref: 'first' }),
    makeNode({ node_ref: 'problem', wave_ref: 'wave-2', state: 'rejected_at_gate' }),
  ] }));
  renderGraph();
  expect(await screen.findByTestId('node-problem')).toBeVisible();
});

it('preserves a collapsed wave through background refresh', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    renderGraph();
    const wave = await screen.findByTestId('wave-epic-1-wave-1');
    fireEvent.click(within(wave).getByRole('button'));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(mockGetFlowGraph.mock.calls.length).toBeGreaterThan(1);
    expect(within(wave).getByRole('button')).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByTestId('node-story-a')).not.toBeVisible();
  } finally { vi.useRealTimers(); }
});

it('shows shared-gate fan-out and fan-in as separate groups', async () => {
  const gate = makeNode({ node_ref: 'start', kind: 'gate' });
  const a = makeNode({ node_ref: 'a' });
  const b = makeNode({ node_ref: 'b' });
  const review = makeNode({ node_ref: 'review', kind: 'eval' });
  mockGetFlowGraph.mockResolvedValue(makeGraph({ nodes: [review, a, gate, b], edges: [
    { from_node_id: gate.id, to_node_id: a.id }, { from_node_id: gate.id, to_node_id: b.id },
    { from_node_id: a.id, to_node_id: review.id }, { from_node_id: b.id, to_node_id: review.id },
  ] }));
  renderGraph();
  const parallel = await screen.findByTestId('stage-epic-1-wave-1-1');
  expect(within(parallel).getByTestId('node-a')).toBeVisible();
  expect(within(parallel).getByTestId('node-b')).toBeVisible();
  expect(within(parallel).queryByTestId('node-review')).not.toBeInTheDocument();
  expect(screen.getByTestId('node-blocked-by-review')).toHaveTextContent('Waiting on 2 steps');
});

it('shows the persisted per-flow pause state alongside graph progress', async () => {
  mockGetFlowGraph.mockResolvedValue(makeGraph({ execution_paused: true }));
  renderGraph();
  expect(await screen.findByText('Paused')).toBeInTheDocument();
  expect(screen.getByText(/Resume preserves progress and attempts/)).toBeInTheDocument();
});


it('shows wave names and descriptions while collapsed and resolves dependency names', async () => {
  const a = makeNode({ id: 'a', node_ref: 'a' });
  const b = makeNode({ id: 'b', node_ref: 'b', wave_ref: 'wave-2' });
  mockGetFlowGraph.mockResolvedValue(makeGraph({
    nodes: [a, b],
    edges: [{ from_node_id: 'a', to_node_id: 'b' }],
    wave_metadata: [
      { epic_ref: a.epic_ref, wave_ref: a.wave_ref, title: 'Contracts and design', description: 'Freeze the contracts and verify the baseline.' },
      { epic_ref: b.epic_ref, wave_ref: b.wave_ref, title: 'Worker execution', description: 'Build and qualify the worker.' },
    ],
  }));
  renderGraph();
  expect(await screen.findByText('Contracts and design')).toBeVisible();
  expect(screen.getByText('Freeze the contracts and verify the baseline.')).toBeVisible();
  expect(screen.getByText('Dependencies in Contracts and design')).toBeVisible();
  const wave = screen.getByTestId(`wave-${a.epic_ref}-${a.wave_ref}`);
  const button = within(wave).getByRole('button', { name: /Contracts and design/ });
  if (button.getAttribute('aria-expanded') === 'true') fireEvent.click(button);
  expect(button).toHaveAttribute('aria-expanded', 'false');
  expect(screen.getByText('Freeze the contracts and verify the baseline.')).toBeVisible();
});


it('shows the epic purpose directly below its readable heading', async () => {
  const node = makeNode();
  const description = 'Services need to submit tasks without a GitHub issue.\n\nProvide durable acceptance and retained progress, preserving tenant isolation.';
  mockGetFlowGraph.mockResolvedValue(makeGraph({
    nodes: [node],
    epic_metadata: [{ epic_ref: node.epic_ref, title: 'External task invocation', description }],
  }));
  renderGraph();
  const heading = await screen.findByRole('heading', { name: 'External task invocation' });
  expect(heading.nextElementSibling).toHaveTextContent('Services need to submit tasks without a GitHub issue.');
  expect(heading.nextElementSibling).toHaveTextContent('preserving tenant isolation.');
  expect(heading.nextElementSibling).toBeVisible();
});
