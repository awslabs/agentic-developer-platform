/**
 * Tests for the delivery flows list page — issue #4869.
 *
 * The page exists because the graph view is addressable only by flow id, so
 * before it a flow nobody had the id for was invisible along with every gate
 * waiting on a human. These tests are ordered by how much damage each failure
 * does:
 *
 *  - **The nav entry is feature-gated.** Ungated, an environment not running the
 *    engine advertises a menu item whose route redirects away with no explanation.
 *  - **The two empty states are different copy.** "No flows yet" and "no flows
 *    match these filters" prompt opposite actions — set one up, versus clear the
 *    filter you forgot was on.
 *  - **An error renders an alert, never an empty list.** An empty list asserts
 *    "you have no delivery work", which is a wrong and specific claim to make when
 *    the truth is that the request failed.
 *  - **The wave rail is the same height at 3 waves and at 14**, and never
 *    re-sorted: `wave-10` must not precede `wave-2`, a bug invisible below 10
 *    waves and visible at exactly the scale the rail exists for.
 *  - **Filters round-trip through the query string**, so a "what's stalled" link
 *    survives being pasted into Slack.
 *  - **`unknown` cost never renders `$0.00`.**
 *
 * jsdom computes no layout, so "same height" is asserted on the height class —
 * the only observable form of that property here. `WAVE_RAIL_HEIGHT_CLASS` is
 * imported rather than hard-coded so the assertion cannot drift from the render.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import FlowsList, { WAVE_RAIL_HEIGHT_CLASS } from '@/pages/FlowsList';
import { Navigation } from '@/components/Navigation';
import type { DesignHistory, FlowList, FlowStatus, FlowSummary, WaveSummary } from '@/types/orchestration';

vi.mock('@/services/orchestration', () => ({
  listFlows: vi.fn(),
}));

// Navigation's own dependencies, stubbed so the nav-gating tests below exercise
// only the `orchestration_engine` branch rather than the whole permission tree.
vi.mock('@/services/auth', () => ({ getAccessToken: () => null }));

const mockUsePermissions = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => mockUsePermissions(),
}));

const mockUseFeatures = vi.fn();
vi.mock('@/hooks/useFeatures', () => ({
  useFeatures: () => mockUseFeatures(),
}));

import { listFlows } from '@/services/orchestration';

const mockListFlows = listFlows as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const SCOPE = 'agent run costs only; excludes build/infra';

function makeWave(overrides: Partial<WaveSummary> = {}): WaveSummary {
  return {
    epic_ref: 'epic-1',
    wave_ref: 'wave-1',
    total: 3,
    done: 0,
    display_counts: { queued: 3, in_progress: 0, gate: 0, stalled: 0, complete: 0 },
    ...overrides,
  };
}

function makeFlow(overrides: Partial<FlowSummary> = {}): FlowSummary {
  return {
    id: '76fea8c6-8e16-4279-aab2-e7b30989be68',
    slug: 'aidlc-delivery-loop-4645',
    title: 'Delivery loop for #4645',
    intent_ref: '4645',
    // Both default to null — the honest state for a flow whose design loop was
    // never captured (#4885), and the state every other test in this file wants.
    description: null,
    design_history: null,
    status: 'queued',
    awaiting_gate_count: 0,
    stalled_count: 0,
    display_counts: { queued: 3, in_progress: 0, gate: 0, stalled: 0, complete: 0 },
    total_nodes: 3,
    epic_count: 1,
    wave_count: 1,
    current_wave_ref: 'wave-1',
    waves: [makeWave()],
    delivery_cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', scope: SCOPE },
    created_at: '2026-09-02T00:59:33Z',
    updated_at: null,
    ...overrides,
  };
}

/** Zero chips for every status, so a fixture only names what it cares about. */
function makeStatusCounts(overrides: Partial<Record<FlowStatus, number>> = {}): Record<FlowStatus, number> {
  return {
    attention_needed: 0,
    awaiting_you: 0,
    running: 0,
    queued: 0,
    complete: 0,
    empty: 0,
    ...overrides,
  };
}

function makeList(overrides: Partial<FlowList> = {}): FlowList {
  const flows = overrides.flows ?? [makeFlow()];
  return {
    flows,
    total: flows.length,
    limit: 25,
    offset: 0,
    status_counts: makeStatusCounts({ queued: flows.length }),
    ...overrides,
  };
}

/** A flow with `count` waves, ordered as the server sends them (first appearance). */
function makeFlowWithWaves(count: number, overrides: Partial<FlowSummary> = {}): FlowSummary {
  const waves = Array.from({ length: count }, (_, index) =>
    makeWave({ wave_ref: `wave-${index + 1}`, total: 2, done: index === 0 ? 2 : 0 })
  );
  return makeFlow({
    waves,
    wave_count: count,
    current_wave_ref: count > 1 ? 'wave-2' : 'wave-1',
    ...overrides,
  });
}

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/** Exposes the live URL so filter round-tripping can be asserted, not inferred. */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>;
}

/**
 * Render at `/flows`, with a `/flows/:flowId` route so a card click resolves to a
 * real navigation rather than a router warning.
 */
function renderFlowsList(initialEntry = '/flows') {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        // So an error test fails on the first attempt rather than after retries.
        retry: false,
        // Mirrors the app's global staleTime — the setting the explicit
        // refetchInterval in `useFlows` exists to defeat.
        staleTime: 5 * 60 * 1000,
      },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LocationProbe />
        <Routes>
          <Route path="/flows" element={<FlowsList />} />
          <Route path="/flows/:flowId" element={<div data-testid="graph-view-stub">graph</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function permissions(overrides: Record<string, unknown> = {}) {
  return {
    isPlatformAdmin: () => false,
    isOrgAdmin: () => false,
    isDeptAdmin: () => false,
    user: { orgId: 'org-1', deptId: 'dept-1' },
    canViewOrganizations: () => false,
    canViewLogs: () => false,
    canViewMetrics: () => false,
    canViewPool: () => false,
    canViewBudgets: () => false,
    canViewRateLimits: () => false,
    ...overrides,
  };
}

function features(overrides: Record<string, unknown> = {}) {
  return {
    chat: false,
    knowledge: false,
    indexing: false,
    connections: false,
    credentials: false,
    system_dashboard: false,
    logs: false,
    gitlab: false,
    budget_spend: false,
    orchestration_engine: false,
    // Issue #3960 — off, like every other flag in this fixture.
    agent_control: false,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockListFlows.mockResolvedValue(makeList());
  mockUsePermissions.mockReturnValue(permissions());
  mockUseFeatures.mockReturnValue(features({ orchestration_engine: true }));
});

// ---------------------------------------------------------------------------
// The nav entry — the whole reason this issue exists
// ---------------------------------------------------------------------------

describe('the Delivery Flows nav entry', () => {
  function renderNavigation() {
    return render(
      <MemoryRouter>
        <Navigation />
      </MemoryRouter>
    );
  }

  it('is hidden when orchestration_engine is off', () => {
    // Fail-closed: `ALL_FEATURES_ENABLED` has this flag false (#4209), so a
    // pending or failed /features fetch hides the entry rather than advertising a
    // page that redirects away.
    mockUseFeatures.mockReturnValue(features({ orchestration_engine: false }));
    renderNavigation();

    expect(screen.queryByText('Delivery Flows')).not.toBeInTheDocument();
  });

  it('links to /flows when the engine is enabled', () => {
    mockUseFeatures.mockReturnValue(features({ orchestration_engine: true }));
    renderNavigation();

    expect(screen.getByRole('link', { name: /Delivery Flows/ })).toHaveAttribute('href', '/flows');
  });

  it('is visible to a member with no admin role', () => {
    // Ungated by permission on purpose: the endpoint requires USAGE_READ, which is
    // exactly what a MEMBER has — and it resolves to [] on the ID-token path
    // (#4389), so a client-side permission check would hide the page from the
    // operators it exists for.
    mockUsePermissions.mockReturnValue(permissions());
    mockUseFeatures.mockReturnValue(features({ orchestration_engine: true }));
    renderNavigation();

    expect(screen.getByRole('link', { name: /Delivery Flows/ })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Loading, empty and error states
// ---------------------------------------------------------------------------

describe('loading, empty and error states', () => {
  it('shows skeleton cards on first load, not a bare spinner', async () => {
    // Cards have real height. A spinner replaced by 25 cards shifts everything the
    // operator was about to click.
    mockListFlows.mockReturnValue(new Promise(() => {}));
    renderFlowsList();

    expect(screen.getByTestId('flows-skeleton')).toBeInTheDocument();
    expect(screen.queryByTestId('flows-list')).not.toBeInTheDocument();
  });

  it('uses distinct copy for "no flows yet" and "no flows match these filters"', async () => {
    mockListFlows.mockResolvedValue(makeList({ flows: [], total: 0, status_counts: makeStatusCounts() }));
    const { unmount } = renderFlowsList();

    // Unfiltered: the org has nothing. The action is to register a plan.
    await waitFor(() => expect(screen.getByTestId('flows-empty')).toBeInTheDocument());
    const unfilteredCopy = screen.getByTestId('flows-empty').textContent ?? '';
    expect(screen.queryByTestId('flows-empty-filtered')).not.toBeInTheDocument();
    unmount();

    // Filtered: flows exist, this search matched none. The action is to clear it.
    renderFlowsList('/flows?q=nothing-matches-this');
    await waitFor(() => expect(screen.getByTestId('flows-empty-filtered')).toBeInTheDocument());
    const filteredCopy = screen.getByTestId('flows-empty-filtered').textContent ?? '';
    expect(screen.queryByTestId('flows-empty')).not.toBeInTheDocument();

    expect(unfilteredCopy).not.toBe(filteredCopy);
    expect(filteredCopy).toMatch(/filter/i);
  });

  it('renders an alert on error and never an empty list', async () => {
    mockListFlows.mockRejectedValue(new Error('Network request failed'));
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('flows-error')).toBeInTheDocument());
    // The distinction that matters: an empty list would assert "you have no
    // delivery work", which is a claim about the org, not about the request.
    expect(screen.queryByTestId('flows-list')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flows-empty')).not.toBeInTheDocument();
    expect(screen.queryByTestId('flows-empty-filtered')).not.toBeInTheDocument();
  });

  it('surfaces the error message rather than a generic failure', async () => {
    mockListFlows.mockRejectedValue(new Error('Permission denied'));
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('flows-error')).toHaveTextContent('Permission denied'));
  });
});

// ---------------------------------------------------------------------------
// The card
// ---------------------------------------------------------------------------

describe('a flow card', () => {
  it('navigates to the flow graph when clicked', async () => {
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    await userEvent.click(screen.getByTestId(`flow-card-${makeFlow().id}`));

    await waitFor(() => expect(screen.getByTestId('graph-view-stub')).toBeInTheDocument());
    expect(screen.getByTestId('location')).toHaveTextContent(`/flows/${makeFlow().id}`);
  });

  it('is a real link, so it is keyboard reachable and middle-clickable', async () => {
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.getByTestId(`flow-card-${makeFlow().id}`)).toHaveAttribute('href', `/flows/${makeFlow().id}`);
  });

  it('shows the "waiting on you" affordance when a gate is open', async () => {
    mockListFlows.mockResolvedValue(
      makeList({ flows: [makeFlow({ status: 'awaiting_you', awaiting_gate_count: 1 })] })
    );
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('awaiting-you')).toHaveTextContent('1 waiting on you'));
  });

  it('shows both calls to action when a flow is stalled AND gated', async () => {
    // `status` is first-match-wins, so this flow reports only `attention_needed` —
    // and the gate still needs answering. Surfacing only the status would drop it.
    mockListFlows.mockResolvedValue(
      makeList({ flows: [makeFlow({ status: 'attention_needed', awaiting_gate_count: 1, stalled_count: 2, display_counts: { queued: 0, in_progress: 0, gate: 1, stalled: 2, complete: 0 } })] })
    );
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('awaiting-you')).toHaveTextContent('1 waiting on you'));
    expect(screen.getByTestId('stalled-count')).toHaveTextContent('2 stalled');
    // Scoped to the card: the chip row carries the same label tenant-wide, so an
    // unscoped query matches both and says nothing about what the card reports.
    const card = screen.getByTestId(`flow-card-${makeFlow().id}`);
    expect(within(card).getByText('Needs attention')).toBeInTheDocument();
  });

  it('uses the rollup stall count and distinguishes completed stories from gates', async () => {
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({
      status: 'attention_needed', stalled_count: 9, completed_story_count: 4,
      story_count: 24, total_nodes: 26,
      display_counts: { queued: 14, in_progress: 4, gate: 0, stalled: 3, complete: 5 },
    })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('story-completion-count')).toHaveTextContent('4 of 24 implementation stories complete'));
    expect(screen.getByTestId('stalled-count')).toHaveTextContent('3 stalled');
    expect(screen.getByTestId('legend-stalled')).toHaveTextContent('3');
    expect(screen.getByTestId('legend-in_progress')).toHaveTextContent('4');
    expect(screen.getByTestId('legend-complete')).toHaveTextContent('5');
    expect(screen.getByText('All 26 work items, including implementation stories, evaluations and approval gates')).toBeVisible();
  });

  it('shows neither affordance when nothing needs a human', async () => {
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ status: 'running' })] }));
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());
    expect(screen.queryByTestId('awaiting-you')).not.toBeInTheDocument();
    expect(screen.queryByTestId('stalled-count')).not.toBeInTheDocument();
  });

  it('shows the slug and intent number, never the internal graph address', async () => {
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const card = screen.getByTestId(`flow-card-${makeFlow().id}`);
    expect(card).toHaveTextContent('aidlc-delivery-loop-4645');
    expect(card).toHaveTextContent('from intent #4645');
    // §7.2: the joined `flow/epic/wave/node` address is the cost join key and is
    // internal. Rendering it invites someone to depend on its format.
    expect(card.textContent).not.toContain('aidlc-delivery-loop-4645/epic-1');
  });

  it('does not render a flow-level engine state', async () => {
    // `OrchestrationFlow.state` has zero writers and is permanently "pending", so
    // the API does not send it. Nothing here should invent one.
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.getByTestId(`flow-card-${makeFlow().id}`).textContent).not.toMatch(/\bpending\b/i);
  });
});

// ---------------------------------------------------------------------------
// The design story on the card (#4885)
// ---------------------------------------------------------------------------

/** A settled history: four gates approved, reverse-engineering skipped by scope. */
function settledHistory(): DesignHistory {
  return {
    scope: 'poc',
    stages: [
      { name: 'intent-capture', state: 'approved', approved_at: '2026-09-01T12:05:00Z' },
      { name: 'reverse-engineering', state: 'skipped' },
      { name: 'requirements-analysis', state: 'approved', approved_at: '2026-09-01T12:30:00Z' },
      { name: 'delivery-planning', state: 'approved', approved_at: '2026-09-01T12:44:00Z' },
      { name: 'loop-proposal', state: 'approved', approved_at: '2026-09-01T13:02:00Z' },
    ],
  };
}

describe('the design story on a card', () => {
  beforeEach(() => {
    mockUseFeatures.mockReturnValue({ features: { orchestration_engine: true }, isLoading: false });
  });

  it('renders no design strip at all when no history was captured', async () => {
    // The headline guardrail. Every flow registered before this feature is in this
    // state, and an empty or all-pending strip would claim five gates that may never
    // have happened — a fabricated record renders as real and looks authoritative.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: null })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.queryByTestId('design-strip')).not.toBeInTheDocument();
    expect(screen.queryByTestId('design-strip-collapsed')).not.toBeInTheDocument();
    expect(screen.getByTestId(`flow-card-${makeFlow().id}`).textContent).not.toMatch(/design gate/i);
  });

  it('renders no description line when none was recorded', async () => {
    // A null description is "nobody wrote one", not an empty paragraph.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ description: null })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.queryByTestId('flow-description')).not.toBeInTheDocument();
  });

  it('shows the use case in the author’s words when there is one', async () => {
    const description = 'Delivery plans showed what they were doing but not what they were for.';
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ description })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.getByTestId('flow-description')).toHaveTextContent(description);
  });

  it('collapses a settled history to one line and names the scope', async () => {
    // Nothing needs answering, so this is reassurance, not a call to action. Five
    // chips here would push the rollup and wave rail below the fold.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: settledHistory() })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const collapsed = screen.getByTestId('design-strip-collapsed');
    expect(collapsed).toHaveTextContent('4 of 5 design gates approved');
    // Scope is what makes "4 of 5" legible rather than looking unfinished.
    expect(collapsed).toHaveTextContent('poc scope');
    expect(screen.queryByTestId('design-strip')).not.toBeInTheDocument();
  });

  it('expands when a gate is open, and says which one', async () => {
    const history: DesignHistory = {
      scope: 'auto',
      stages: [
        { name: 'intent-capture', state: 'approved', approved_at: '2026-09-01T12:05:00Z' },
        { name: 'reverse-engineering', state: 'skipped' },
        { name: 'requirements-analysis', state: 'approved', approved_at: '2026-09-01T12:30:00Z' },
        { name: 'delivery-planning', state: 'open' },
        { name: 'loop-proposal', state: 'not_reached' },
      ],
    };
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: history })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.getByTestId('design-strip')).toBeInTheDocument();
    // The caption carries the same information as the chips, for a screen reader and
    // for anyone who cannot tell the ringed chip from its neighbours.
    expect(screen.getByTestId('design-strip-caption')).toHaveTextContent('delivery-planning');
  });

  it('distinguishes a skipped gate from one not yet reached', async () => {
    // They must never merge: `skipped` means this loop's scope never runs the gate,
    // `not_reached` means it will and has not got there. Showing the first as the
    // second displays outstanding work that is never coming.
    const history: DesignHistory = {
      scope: 'poc',
      stages: [
        { name: 'reverse-engineering', state: 'skipped' },
        { name: 'delivery-planning', state: 'open' },
        { name: 'loop-proposal', state: 'not_reached' },
      ],
    };
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: history })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const skipped = screen.getByTestId('design-stage-reverse-engineering');
    const notReached = screen.getByTestId('design-stage-loop-proposal');
    expect(skipped).toHaveAttribute('data-state', 'skipped');
    expect(notReached).toHaveAttribute('data-state', 'not_reached');
    expect(skipped.className).not.toEqual(notReached.className);
    // Struck through, so it reads as "ruled out" rather than "outstanding".
    expect(skipped.className).toContain('line-through');
    expect(notReached.className).not.toContain('line-through');
  });

  it('renders the gates in canonical order, not the order the document listed them', async () => {
    // The strip reads as a pipeline. A document that happens to list the last gate
    // first must not render the design loop backwards.
    const history: DesignHistory = {
      scope: 'auto',
      stages: [
        { name: 'loop-proposal', state: 'open' },
        { name: 'intent-capture', state: 'approved', approved_at: '2026-09-01T12:05:00Z' },
      ],
    };
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: history })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const chips = within(screen.getByTestId('design-strip')).getAllByTestId(/^design-stage-/);
    expect(chips.map((chip) => chip.getAttribute('data-testid'))).toEqual([
      'design-stage-intent-capture',
      'design-stage-loop-proposal',
    ]);
  });

  it('omits a stage the document did not record rather than showing it pending', async () => {
    // "Not recorded" is not a state. A partial history is expected — an author omits
    // what they cannot establish — and the denominator stays 5 either way.
    const history: DesignHistory = {
      scope: 'auto',
      stages: [
        { name: 'intent-capture', state: 'approved', approved_at: '2026-09-01T12:05:00Z' },
        { name: 'delivery-planning', state: 'open' },
      ],
    };
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ design_history: history })] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.queryByTestId('design-stage-reverse-engineering')).not.toBeInTheDocument();
    expect(screen.queryByTestId('design-stage-loop-proposal')).not.toBeInTheDocument();
    expect(screen.getByTestId('design-strip-caption')).toHaveTextContent('1 of 5 design gates approved');
  });

  it('renders nothing for a history whose stage list is empty', async () => {
    // Defensive: the server rejects an empty list at write time, so this can only
    // arrive from an older row or a hand-edited payload. Either way there is nothing
    // to say, and "0 of 5 approved" would read as a stalled design loop.
    mockListFlows.mockResolvedValue(
      makeList({ flows: [makeFlow({ design_history: { scope: 'auto', stages: [] } })] })
    );
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.queryByTestId('design-strip')).not.toBeInTheDocument();
    expect(screen.queryByTestId('design-strip-collapsed')).not.toBeInTheDocument();
  });

  it('keeps the whole card a single link into the graph', async () => {
    // The strip and description are inside the anchor, so they must not introduce a
    // nested interactive element — that would break keyboard traversal of the card.
    mockListFlows.mockResolvedValue(
      makeList({ flows: [makeFlow({ description: 'A use case.', design_history: settledHistory() })] })
    );
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const card = screen.getByTestId(`flow-card-${makeFlow().id}`);
    expect(within(card).queryByRole('button')).not.toBeInTheDocument();
    expect(within(card).queryByRole('link')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The wave rail
// ---------------------------------------------------------------------------

describe('the wave rail', () => {
  it('is the same height at 3 waves and at 14', async () => {
    // The requirement behind this (PR #4884): EPIC #4191 ran to seven waves, and
    // per-wave cards truncate labels and drop to 8px text at that width. Segments
    // narrow as waves multiply; the strip never grows.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlowWithWaves(3)] }));
    const { unmount } = renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('wave-rail')).toBeInTheDocument());
    const threeWaveClasses = screen.getByTestId('wave-rail').className;
    expect(screen.getByTestId('wave-rail')).toHaveAttribute('data-wave-count', '3');
    unmount();

    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlowWithWaves(14)] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('wave-rail')).toBeInTheDocument());
    const fourteenWaveClasses = screen.getByTestId('wave-rail').className;
    expect(screen.getByTestId('wave-rail')).toHaveAttribute('data-wave-count', '14');

    expect(threeWaveClasses).toContain(WAVE_RAIL_HEIGHT_CLASS);
    expect(fourteenWaveClasses).toBe(threeWaveClasses);
  });

  it('renders one segment per wave in the order the server sent them', async () => {
    // Never re-sorted client-side: `wave-10` sorts before `wave-2`
    // lexicographically, so sorting here would ring the wrong wave — invisible
    // below 10 waves and visible at exactly the scale this rail is for.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlowWithWaves(14)] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('wave-rail')).toBeInTheDocument());

    const rendered = Array.from(screen.getByTestId('wave-rail').children).map((node) =>
      node.getAttribute('data-testid')
    );

    expect(rendered).toEqual(
      Array.from({ length: 14 }, (_, index) => `wave-segment-epic-1-wave-${index + 1}`)
    );
  });

  it('rings the current wave and only the current wave', async () => {
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlowWithWaves(14)] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('wave-rail')).toBeInTheDocument());

    const current = Array.from(screen.getByTestId('wave-rail').children).filter(
      (node) => node.getAttribute('data-current') === 'true'
    );

    expect(current).toHaveLength(1);
    expect(current[0]).toHaveAttribute('data-testid', 'wave-segment-epic-1-wave-2');
  });

  it('describes the position in prose, by rail index rather than by parsing the ref', async () => {
    // Nothing constrains `wave_ref` to `wave-<n>` — `wave-as` is legal — so the
    // position cannot come from the name. Two lettered refs prove the caption is
    // derived from array order.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [
          makeFlow({
            wave_count: 2,
            current_wave_ref: 'wave-bs',
            waves: [
              makeWave({ wave_ref: 'wave-as', total: 2, done: 2 }),
              makeWave({ wave_ref: 'wave-bs', total: 4, done: 1 }),
            ],
          }),
        ],
      })
    );
    renderFlowsList();

    await waitFor(() =>
      expect(screen.getByTestId('wave-rail-caption')).toHaveTextContent(
        'Now on wave-bs — wave 2 of 2, 1 of 4 steps done.'
      )
    );
  });

  it('says all waves are complete rather than naming a last wave', async () => {
    // Naming one would read as "this is where the work is".
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [
          makeFlow({
            status: 'complete',
            current_wave_ref: null,
            wave_count: 2,
            waves: [
              makeWave({ wave_ref: 'wave-1', total: 2, done: 2 }),
              makeWave({ wave_ref: 'wave-2', total: 2, done: 2 }),
            ],
          }),
        ],
      })
    );
    renderFlowsList();

    await waitFor(() =>
      expect(screen.getByTestId('wave-rail-caption')).toHaveTextContent('All 2 waves complete.')
    );
  });

  it('handles a flow with no waves at all', async () => {
    // A plan that compiled to an empty graph. The rail must say so, not render an
    // ambiguous blank strip.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [
          makeFlow({
            status: 'empty',
            total_nodes: 0,
            wave_count: 0,
            epic_count: 0,
            current_wave_ref: null,
            waves: [],
            display_counts: { queued: 0, in_progress: 0, gate: 0, stalled: 0, complete: 0 },
          }),
        ],
      })
    );
    renderFlowsList();

    await waitFor(() =>
      expect(screen.getByTestId('wave-rail-caption')).toHaveTextContent('No waves planned yet.')
    );
    // Scoped to the card — the `empty` chip carries the same label.
    const card = screen.getByTestId(`flow-card-${makeFlow().id}`);
    expect(within(card).getByText('No work planned')).toBeInTheDocument();
  });

  it('carries the prose description as its accessible name', async () => {
    // A shape cannot say "wave 2 of 14" to a screen reader, and the ringed segment
    // is not distinguishable from its neighbours without sight.
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlowWithWaves(14)] }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('wave-rail')).toBeInTheDocument());

    expect(screen.getByTestId('wave-rail')).toHaveAttribute('aria-label', 'Now on wave-2 — wave 2 of 14, 0 of 2 steps done.');
  });
});

// ---------------------------------------------------------------------------
// Delivery cost
// ---------------------------------------------------------------------------

describe('delivery cost', () => {
  it('never renders an unknown cost as $0.00', async () => {
    // The whole point of the three-valued model: `$0.00` asserts the work was
    // free, while `unknown` says nobody measured it.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [makeFlow({ delivery_cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', scope: SCOPE } })],
      })
    );
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    const figure = screen.getByTestId('cost-figure');
    expect(figure).toHaveTextContent('—');
    expect(figure.textContent).not.toContain('$0.00');
    // A bare dash reads as a UI bug, so every unknown explains itself.
    expect(figure).toHaveAttribute('title', expect.stringContaining('No metered usage'));
  });

  it('renders a verified zero as $0.00, which is a different fact', async () => {
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [makeFlow({ delivery_cost: { status: 'none_incurred', amount_usd: '0', scope: SCOPE } })],
      })
    );
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('cost-figure')).toHaveTextContent('$0.00'));
  });

  it('renders a known amount and carries its scope caption', async () => {
    // These totals exclude build and infra spend, so the figure must never travel
    // without saying so — someone will make a budget decision on it.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [makeFlow({ delivery_cost: { status: 'known', amount_usd: '1.87', scope: SCOPE } })],
      })
    );
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('cost-figure')).toHaveTextContent('$1.87'));
    expect(screen.getByTestId('cost-figure')).toHaveAttribute('title', expect.stringContaining(SCOPE));
  });
});

// ---------------------------------------------------------------------------
// Filters — in the URL, so a link survives being shared
// ---------------------------------------------------------------------------

describe('filters live in the query string', () => {
  it('reads its initial filters from the URL', async () => {
    renderFlowsList('/flows?q=4645&status=awaiting_you&needs_me=true&sort=stalled');

    await waitFor(() => expect(mockListFlows).toHaveBeenCalled());
    expect(mockListFlows).toHaveBeenCalledWith(
      expect.objectContaining({ q: '4645', status: 'awaiting_you', needs_me: true, sort: 'stalled' })
    );
  });

  it('writes a status chip selection into the URL', async () => {
    // This is what makes a "what's stalled" link shareable: filters in component
    // state produce the same URL whatever is selected, so the colleague who opens
    // the pasted link sees a different page from the one described.
    mockListFlows.mockResolvedValue(makeList({ status_counts: makeStatusCounts({ attention_needed: 2, queued: 1 }) }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('status-chips')).toBeInTheDocument());

    await userEvent.click(screen.getByTestId('status-chip-attention_needed'));

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/flows?status=attention_needed')
    );
    expect(mockListFlows).toHaveBeenLastCalledWith(expect.objectContaining({ status: 'attention_needed' }));
  });

  it('clears a status filter when its selected chip is clicked again', async () => {
    // Otherwise a chip is a trap you can only leave via the sort control.
    renderFlowsList('/flows?status=queued');
    await waitFor(() => expect(screen.getByTestId('status-chip-queued')).toHaveAttribute('aria-pressed', 'true'));

    await userEvent.click(screen.getByTestId('status-chip-queued'));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/flows'));
    expect(screen.getByTestId('location').textContent).not.toContain('status=');
  });

  it('writes the needs-me filter into the URL and sends it to the API', async () => {
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('needs-me-filter')).toBeInTheDocument());

    await userEvent.click(screen.getByTestId('needs-me-filter'));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('needs_me=true'));
    expect(mockListFlows).toHaveBeenLastCalledWith(expect.objectContaining({ needs_me: true }));
  });

  it('writes the search box into the URL', async () => {
    renderFlowsList();
    await waitFor(() => expect(screen.getByLabelText('Search')).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText('Search'), '4645');

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('q=4645'));
  });

  it('omits default values, so an unfiltered page has a clean URL', async () => {
    renderFlowsList('/flows?status=queued');
    await waitFor(() => expect(screen.getByTestId('status-chips')).toBeInTheDocument());

    await userEvent.click(screen.getByTestId('status-chip-queued'));

    // Not `/flows?sort=created&needs_me=false&offset=0` — a shared link should
    // carry the filters actually applied and nothing else.
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/flows'));
    expect(screen.getByTestId('location').textContent).toBe('/flows');
  });

  it('ignores an unrecognised status or sort in a hand-edited URL', async () => {
    // The URL is user-editable. Forwarding `?status=on_fire` would come back 422
    // and render as "this page is broken" rather than as the list they can use.
    renderFlowsList('/flows?status=on_fire&sort=cost');

    await waitFor(() => expect(mockListFlows).toHaveBeenCalled());
    expect(mockListFlows).toHaveBeenCalledWith(
      expect.objectContaining({ status: undefined, sort: 'created' })
    );
  });

  it('returns to the first page when a filter changes', async () => {
    // Changing a filter while on page 3 of the old result set lands on a page that
    // may not exist in the new one, which renders as "no matches".
    renderFlowsList('/flows?offset=50');
    await waitFor(() => expect(screen.getByTestId('needs-me-filter')).toBeInTheDocument());

    await userEvent.click(screen.getByTestId('needs-me-filter'));

    await waitFor(() => expect(mockListFlows).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0 })));
    expect(screen.getByTestId('location').textContent).not.toContain('offset=');
  });
});

// ---------------------------------------------------------------------------
// Chips, summary and paging
// ---------------------------------------------------------------------------

describe('the status chips and the summary line', () => {
  it('renders all six chips including zeroes', async () => {
    // Absence of a status is information, and a chip row whose shape changes as
    // work moves is harder to scan than one that does not.
    mockListFlows.mockResolvedValue(makeList({ status_counts: makeStatusCounts({ queued: 1 }) }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('status-chips')).toBeInTheDocument());

    const chips = screen.getByTestId('status-chips');
    for (const status of ['attention_needed', 'awaiting_you', 'running', 'queued', 'complete', 'empty']) {
      expect(within(chips).getByTestId(`status-chip-${status}`)).toBeInTheDocument();
    }
    expect(within(chips).getByTestId('status-chip-complete')).toHaveTextContent('0');
  });

  it('keeps the chips tenant-wide while a filter narrows the rows', async () => {
    // "Showing 1 of 5" with chips totalling 5: the chips describe the population
    // being chosen among. Scoped to the filtered set they would read 0 for every
    // unselected status, which is noise.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: [makeFlow({ status: 'awaiting_you', awaiting_gate_count: 1 })],
        total: 1,
        status_counts: makeStatusCounts({ awaiting_you: 1, running: 4 }),
      })
    );
    renderFlowsList('/flows?needs_me=true');

    await waitFor(() => expect(screen.getByTestId('flows-summary')).toHaveTextContent('Showing 1 of 1'));
    expect(screen.getByTestId('status-chip-running')).toHaveTextContent('4');
  });

  it('reports the filtered total, not the page length', async () => {
    // A `total` describing only the page would make the pager lie about how much
    // is there — and destroy trust in every other number on the page.
    mockListFlows.mockResolvedValue(
      makeList({
        flows: Array.from({ length: 25 }, (_, index) => makeFlow({ id: `flow-${index}`, slug: `loop-${index}` })),
        total: 30,
      })
    );
    renderFlowsList();

    await waitFor(() => expect(screen.getByTestId('flows-summary')).toHaveTextContent('Showing 25 of 30 flows'));
  });

  it('pages forward through the URL and disables Previous on the first page', async () => {
    mockListFlows.mockResolvedValue(
      makeList({
        flows: Array.from({ length: 25 }, (_, index) => makeFlow({ id: `flow-${index}`, slug: `loop-${index}` })),
        total: 30,
      })
    );
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-next')).toBeInTheDocument());

    expect(screen.getByTestId('flows-prev')).toBeDisabled();
    await userEvent.click(screen.getByTestId('flows-next'));

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('offset=25'));
    expect(mockListFlows).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 25 }));
  });

  it('hides the pager when everything fits on one page', async () => {
    mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow()], total: 1 }));
    renderFlowsList();
    await waitFor(() => expect(screen.getByTestId('flows-list')).toBeInTheDocument());

    expect(screen.queryByTestId('flows-next')).not.toBeInTheDocument();
  });
});


it('includes evaluation stories in the flow total and completion count', async () => {
  mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({
    story_count: 9, eval_story_count: 5, gate_count: 1, eval_count: 6, wave_count: 6, total_nodes: 16,
    completed_story_count: 1, completed_eval_story_count: 1,
  })] }));
  renderFlowsList();
  const summary = await screen.findByTestId('plan-summary');
  expect(summary).toHaveTextContent('14 stories across 6 waves');
  expect(summary).toHaveTextContent('9 implementation · 5 evaluation · 1 approval gate · 1 evaluation checkpoint');
  expect(screen.getByTestId('story-completion-count')).toHaveTextContent('2 of 14 stories complete');
});

it('shows the story count and requested-change hold on the summary card', async () => {
  mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({
    status: 'attention_needed', changes_requested_count: 1,
    story_count: 27, gate_count: 14, eval_count: 4, wave_count: 4, total_nodes: 45,
  })] }));
  renderFlowsList();
  expect(await screen.findByText('27 implementation stories across 4 waves')).toBeInTheDocument();
  expect(screen.getByText('14 approval gates · 4 evaluations')).toBeInTheDocument();
  expect(screen.getByText('Changes requested')).toBeInTheDocument();
  expect(screen.getByTestId('changes-requested-summary')).toHaveTextContent('Work behind these gates is paused');
});

it('renders flow pause controls outside the navigation link', async () => {
  mockListFlows.mockResolvedValue(makeList({ flows: [makeFlow({ execution_paused: true })] }));
  mockUsePermissions.mockReturnValue({ hasPermission: () => true });
  renderFlowsList();
  const button = await screen.findByRole('button', { name: 'Resume flow' });
  expect(button.closest('a')).toBeNull();
  expect(screen.getByText('Paused')).toBeInTheDocument();
});
