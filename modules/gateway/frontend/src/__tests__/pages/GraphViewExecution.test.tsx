vi.mock('@/components/budget/BudgetEnforcementControl', () => ({ BudgetEnforcementControl: () => <div data-testid="budget-enforcement-control" /> }));
/**
 * Integration: the delivery ledger reaching the existing graph page — issue #5145.
 *
 * `ExecutionProgress.test.tsx` renders the panel in isolation and proves it tells
 * the truth. What it cannot prove is that the panel is *wired* — that the page
 * queries the ledger once for the whole flow, hands each story its own row, and
 * puts the result **inside the story journey #5207 already built** rather than
 * beside it as a second delivery view. That is the integration this file asserts,
 * because "correct component, never mounted" and "correct component, mounted as a
 * duplicate journey" both pass every unit test in the change.
 *
 * It is a separate file from `GraphView.test.tsx` on purpose: that suite's baseline
 * is a *failing* ledger read, which is itself the assertion that a ledger outage
 * cannot blank the plan. Resolving the ledger there would erase that guarantee.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import GraphView from '@/pages/GraphView';
import type {
  ExecutionSummary,
  FlowExecution,
  FlowGraph,
  GraphNode,
} from '@/types/orchestration';

vi.mock('@/services/orchestration', () => ({
  getFlowGraph: vi.fn(),
  getFlowExecution: vi.fn(),
  approveGate: vi.fn(),
  rejectGate: vi.fn(),
  resumeNode: vi.fn(),
}));

vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ hasPermission: () => false }),
}));

import { getFlowGraph, getFlowExecution } from '@/services/orchestration';

const mockGetFlowGraph = getFlowGraph as ReturnType<typeof vi.fn>;
const mockGetFlowExecution = getFlowExecution as ReturnType<typeof vi.fn>;

const SERVER_TIME = '2026-09-18T12:00:00+00:00';

function makeNode(overrides: Partial<GraphNode> = {}): GraphNode {
  return {
    id: `id-${overrides.node_ref ?? 'story-a'}`,
    epic_ref: 'epic-1',
    wave_ref: 'wave-1',
    node_ref: 'story-a',
    kind: 'story',
    title: 'Story A',
    state: 'running',
    stalled: false,
    issue_ref: null,
    attempts: 0,
    cost: { status: 'unknown', amount_usd: null, reason: 'not_started' },
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
    cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', partial: true },
    ...overrides,
  };
}

function summary(overrides: Partial<ExecutionSummary> = {}): ExecutionSummary {
  return {
    id: 'exec-1',
    // Keyed by the node's internal id, which is what the ledger stores — not the
    // human `node_ref`. Getting this wrong renders every panel as "no activity"
    // while the request succeeds, so the fixture uses the real id shape.
    node_id: 'id-story-a',
    cycle: 1,
    phase: 'delivering',
    status: 'runnable',
    revision: 3,
    attempts: 1,
    next_check_at: '2026-09-18T12:15:00+00:00',
    deadline_at: null,
    progressed_at: '2026-09-18T11:55:00+00:00',
    progress_note: null,
    block: null,
    pending_action_key: null,
    notification_receipt_ref: null,
    handoff_receipt_ref: null,
    created_at: '2026-09-18T11:00:00+00:00',
    updated_at: '2026-09-18T11:55:00+00:00',
    actions: [],
    action_overflow: false,
    ...overrides,
  };
}

function ledger(executions: ExecutionSummary[], overrides: Partial<FlowExecution> = {}): FlowExecution {
  return {
    flow_id: 'flow-1',
    server_time: SERVER_TIME,
    executions,
    total: executions.length,
    limit: 200,
    offset: 0,
    legacy: executions.length === 0,
    ...overrides,
  };
}

function renderGraph() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 5 * 60 * 1000 } },
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
  mockGetFlowExecution.mockResolvedValue(ledger([summary()]));
});

describe('the ledger panel lands inside the existing story journey', () => {
  it('renders the panel within the journey element, not as a sibling view', async () => {
    // The issue's integration boundary. A second top-level delivery section would
    // pass every rendering test in this change and still be the duplicate dashboard
    // the scope explicitly forbids, so containment is asserted structurally.
    renderGraph();

    const journey = await screen.findByTestId('story-journey-story-a');
    expect(within(journey).getByTestId('execution-progress-story-a')).toBeInTheDocument();
  });

  it('keeps the journey stage list it was added to', async () => {
    // #5084/#5105 are migrating this UI. Replacing the stage list rather than
    // extending it would silently undo in-flight work.
    renderGraph();

    const journey = await screen.findByTestId('story-journey-story-a');
    expect(within(journey).getByTestId('node-stage-story-a')).toBeInTheDocument();
    expect(within(journey).getByRole('list', { name: 'Story journey' })).toBeInTheDocument();
  });

  it('shows the phase and the block that the stage list cannot express', async () => {
    // The reason the panel exists: the journey's stage vocabulary has no way to say
    // "waiting on a named human for a named input".
    mockGetFlowExecution.mockResolvedValue(
      ledger([
        summary({
          status: 'blocked',
          phase: 'awaiting_review',
          block: {
            code: 'human_gate_required',
            owner: 'platform-operator',
            required_input: 'approve the merge gate for story-a',
            remaining_gates: ['gate:merge-approval'],
            progressed_at: '2026-09-18T09:00:00+00:00',
            detail: null,
          },
        }),
      ])
    );
    renderGraph();

    const block = await screen.findByTestId('execution-block-story-a');
    expect(block.textContent).toMatch(/A platform operator/);
    expect(block.textContent).toMatch(/approve the merge gate for story-a/);
    expect(screen.getByTestId('execution-progress-story-a')).toHaveAttribute('data-tone', 'attention');
  });
});

describe('the ledger is read once for the whole flow', () => {
  it('issues exactly one execution request for a multi-story flow', async () => {
    // The issue forbids fetching per node. With three stories a per-chip query is
    // three requests, and on a real flow it is one per story per poll.
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'story-a', id: 'id-story-a' }),
          makeNode({ node_ref: 'story-b', id: 'id-story-b', title: 'Story B' }),
          makeNode({ node_ref: 'story-c', id: 'id-story-c', title: 'Story C' }),
        ],
      })
    );
    mockGetFlowExecution.mockResolvedValue(
      ledger([
        summary({ id: 'exec-a', node_id: 'id-story-a' }),
        summary({ id: 'exec-b', node_id: 'id-story-b' }),
      ])
    );
    renderGraph();

    await screen.findByTestId('execution-progress-story-a');
    expect(mockGetFlowExecution).toHaveBeenCalledTimes(1);
  });

  it('gives each story its own row and says nothing about the uncovered one', async () => {
    // Fanning one row across every chip is the failure this catches: it would show
    // story-c as delivering when the ledger has no record of it at all.
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'story-a', id: 'id-story-a' }),
          makeNode({ node_ref: 'story-c', id: 'id-story-c', title: 'Story C' }),
        ],
      })
    );
    mockGetFlowExecution.mockResolvedValue(
      ledger([summary({ node_id: 'id-story-a', phase: 'delivering' })], { total: 1 })
    );
    renderGraph();

    await screen.findByTestId('execution-progress-story-a');
    // story-c has no row: an absence panel, not a borrowed status.
    const absent = screen.getByTestId('execution-absent-story-c');
    expect(absent.textContent).toMatch(/no delivery activity recorded yet/i);
    expect(absent).toHaveAttribute('data-legacy', 'false');
    expect(screen.queryByTestId('execution-progress-story-c')).toBeNull();
  });
});

describe('a ledger read that has not answered is not an absence', () => {
  it('renders no panel at all until the ledger responds', async () => {
    // An absence panel here would assert "no execution record", which means the flow
    // predates the ledger — a claim about a response that simply has not arrived.
    let release: (view: FlowExecution) => void = () => {};
    mockGetFlowExecution.mockReturnValue(
      new Promise<FlowExecution>((resolve) => {
        release = resolve;
      })
    );
    renderGraph();

    // The graph itself is up — the panel is what is pending.
    await screen.findByTestId('story-journey-story-a');
    expect(screen.queryByTestId('execution-absent-story-a')).toBeNull();
    expect(screen.queryByTestId('execution-progress-story-a')).toBeNull();

    release(ledger([summary()]));
    await waitFor(() =>
      expect(screen.getByTestId('execution-progress-story-a')).toBeInTheDocument()
    );
  });

  it('keeps the plan readable when the ledger read fails outright', async () => {
    // Separate query, separate failure. The graph is the older load-bearing view and
    // a ledger outage must not take it down — nor claim an absence it cannot know.
    mockGetFlowExecution.mockRejectedValue(new Error('ledger unavailable'));
    renderGraph();

    await screen.findByTestId('story-journey-story-a');
    expect(screen.getByTestId('node-stage-story-a')).toBeInTheDocument();
    expect(screen.queryByTestId('graph-error')).toBeNull();
    expect(screen.queryByTestId('execution-absent-story-a')).toBeNull();
  });

  /**
   * The assertions above are all NEGATIVE — they establish that a ledger failure does
   * not blank the plan and does not masquerade as an absence. Every one of them also
   * holds when the failure is simply DISCARDED, which is the defect the visible notice
   * was added to fix: a silently swallowed read is pixel-identical to the feature not
   * existing, so the screen tells whoever is debugging it that there is nothing to
   * debug. Verified by deleting the notice from `GraphView.tsx` — the negative
   * assertions stayed green, so they could not have been what was holding it in place.
   *
   * This test is the positive half: the failure has to be STATED. It asserts the
   * notice is present and that its text does not overclaim — a read that failed must
   * not be reported as delivery having stopped, only as this view being unable to say
   * where delivery stands.
   */
  it('states the ledger failure instead of discarding it', async () => {
    mockGetFlowExecution.mockRejectedValue(new Error('ledger unavailable'));
    renderGraph();

    const notice = await screen.findByTestId('execution-unavailable');
    expect(notice.textContent).toMatch(/could not be read/i);
    // The plan is still trustworthy, and the notice must say so rather than implying
    // the absence of progress data means the absence of progress.
    expect(notice.textContent).toMatch(/does not mean delivery has\s+stopped/i);
  });
});

describe('legacy flows', () => {
  it('names a pre-ledger flow as having no record rather than rendering nothing', async () => {
    // The truthfulness requirement at the integration level: with no panel, the
    // stage list stands alone and reads as a complete account of delivery.
    mockGetFlowExecution.mockResolvedValue(ledger([]));
    renderGraph();

    const absent = await screen.findByTestId('execution-absent-story-a');
    expect(absent.textContent).toMatch(/no execution record/i);
    expect(absent).toHaveAttribute('data-legacy', 'true');
  });
});

describe('non-story nodes', () => {
  it('adds no panel to gates and evaluations', async () => {
    // The ledger tracks delivery executions. A gate is an approval, and an absence
    // panel on one would invent a delivery-tracking gap that does not exist.
    mockGetFlowGraph.mockResolvedValue(
      makeGraph({
        nodes: [
          makeNode({ node_ref: 'story-a', id: 'id-story-a' }),
          makeNode({ node_ref: 'gate-1', id: 'id-gate-1', kind: 'gate', title: 'Wave gate' }),
        ],
      })
    );
    renderGraph();

    await screen.findByTestId('execution-progress-story-a');
    expect(screen.queryByTestId('execution-progress-gate-1')).toBeNull();
    expect(screen.queryByTestId('execution-absent-gate-1')).toBeNull();
  });
});
