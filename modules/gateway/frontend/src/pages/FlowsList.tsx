/**
 * The delivery flows list — the engine's entry point in the UI. Issue #4869.
 *
 * The graph view (#4212) has always been addressable only by flow id, so a flow
 * nobody had the id for was invisible — along with every gate waiting on a human.
 * This page is the index that fixes that, and the one place an operator can answer
 * "what is in flight, and what needs me" without being handed a link.
 *
 * Four things are load-bearing rather than stylistic:
 *
 * **1. The wave rail is one fixed-height strip, not one card per wave.** A
 * requirement, not a preference (PR #4884): EPIC #4191 ran to seven waves, and
 * per-wave cards truncate their labels and drop to 8px text at that width. The
 * rail is the same height for 3 waves or 20 — segments get narrower, never
 * shorter — so a list of cards does not reflow into a wall as flows grow. Per-wave
 * detail is deliberately not duplicated here: the graph page already lays waves
 * out as columns. This card is the summary; that page is the detail.
 *
 * **2. Filtering and paging are server-side, and the two empty states differ.**
 * "You have no flows yet" and "nothing matches these filters" prompt opposite
 * actions — set one up, versus clear the filter you forgot was on — so they are
 * different copy, not one shared "nothing here".
 *
 * **3. An error renders an alert, never an empty list.** An empty list asserts
 * "you have no delivery work", which is a specific and wrong claim to make when
 * the truth is that the request failed.
 *
 * **4. `unknown` cost never renders `$0.00`**, via `CostFigureDisplay` and
 * `utils/cost.ts`. There is no formatter in this file: five of them existed before
 * #4207 consolidated the lot, and they disagreed on exactly this case.
 *
 * The status chips cover the selected Product or tenant, before search/status
 * filtering — they describe
 * the population being chosen among, so with a filter on the summary reads
 * "Showing 3 of 5" while the chips still total 5. A chip scoped to the filtered
 * set would read 0 for every unselected status, which is noise.
 */

import { useEffect } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { StoryProgress } from '@/components/orchestration/StoryProgress';
import { intentLabel } from '@/utils/flowSummary';
import { FlowExecutionControl } from '@/components/orchestration/FlowExecutionControl';
import { FlowArchiveControl } from '@/components/orchestration/FlowArchiveControl';
import { RollupBar } from '@/components/orchestration/RollupBar';
import { PlanSummary } from '@/components/orchestration/PlanSummary';
import { CostFigureDisplay } from '@/components/orchestration/CostFigureDisplay';
import { LastUpdated } from '@/components/LastUpdated';
import { Alert, Badge, Input, Select } from '@/components/ui';
import {
  FLOWS_PAGE_SIZE,
  FLOW_SORTS,
  FLOW_STATUSES,
  useFlowFilters,
  useFlows,
  type FlowFilters,
  type FlowSort,
} from '@/hooks/useFlows';
import {
  DESIGN_STAGES,
  type DesignHistory,
  type DesignStageName,
  type DesignStageState,
  type FlowStatus,
  type FlowSummary,
  type WaveSummary,
} from '@/types/orchestration';

/**
 * The rail's height, as one exported constant.
 *
 * Exported so the "same height at 3 waves and at 14" test can assert the class is
 * applied identically without hard-coding a Tailwind token — and so a change to
 * the height cannot land on one branch of the render and not the other. jsdom
 * computes no layout, so the class is the only observable form of this property.
 */
export const WAVE_RAIL_HEIGHT_CLASS = 'h-10';

/** Human labels for the six statuses. The chips and the card badge share them. */
const STATUS_LABELS: Record<FlowStatus, string> = {
  archived: 'Archived',
  attention_needed: 'Needs attention',
  awaiting_you: 'Awaiting review',
  running: 'Running',
  queued: 'Queued',
  complete: 'Complete',
  empty: 'No work planned',
};

/** Badge variants, worst-news-first like the status precedence itself. */
const STATUS_VARIANTS: Record<FlowStatus, 'default' | 'success' | 'warning' | 'danger' | 'info'> = {
  archived: 'default',
  attention_needed: 'danger',
  awaiting_you: 'warning',
  running: 'info',
  queued: 'default',
  complete: 'success',
  empty: 'default',
};

/** Titles (or refs) of the waves that currently hold an open gate. */
function gatedWaves(flow: FlowSummary): string[] {
  return flow.waves.filter((wave) => (wave.display_counts?.gate ?? 0) > 0).map((wave) => wave.title || wave.wave_ref);
}

const SORT_LABELS: Record<FlowSort, string> = {
  created: 'Newest first',
  updated: 'Recently updated',
  stalled: 'Most stalled first',
};

/** A wave is finished when every one of its steps is done. */
function isFinished(wave: WaveSummary): boolean {
  return wave.total > 0 && wave.done >= wave.total;
}

/**
 * One prose sentence describing where the flow is in its waves.
 *
 * Beneath the rail rather than inside it, because the rail is a shape and a shape
 * cannot say "wave 3 of 14" to a screen reader or to someone who cannot tell the
 * ringed segment from its neighbours.
 */
function railDescription(flow: FlowSummary): string {
  if (flow.wave_count === 0) return 'No waves planned yet.';

  // Position is the *index in the array*, which is first-appearance order from the
  // server. Deliberately not parsed out of the ref: nothing constrains `wave_ref`
  // to `wave-<n>` (`wave-as` is legal), so "wave 10 of 14" cannot come from the
  // name.
  const index = flow.waves.findIndex((wave) => wave.wave_ref === flow.current_wave_ref && !isFinished(wave));
  if (flow.current_wave_ref === null || index < 0) {
    return `All ${flow.wave_count} ${flow.wave_count === 1 ? 'wave' : 'waves'} complete.`;
  }

  const wave = flow.waves[index];
  return `Now on ${wave.title || wave.wave_ref} — wave ${index + 1} of ${flow.wave_count}, ${wave.done} of ${wave.total} steps done.`;
}

/**
 * The wave rail: one segment per wave, in the order the server sent them.
 *
 * **The array is never re-sorted here.** The server orders waves by first
 * appearance (`MIN(node.created_at)`); sorting by `wave_ref` in the client would
 * put `wave-10` before `wave-2` and ring the wrong wave — a bug invisible below 10
 * waves and visible at exactly the 7–14 wave scale this rail exists for.
 */
function WaveRail({ flow }: { flow: FlowSummary }) {
  const description = railDescription(flow);
  const currentWave = flow.waves.find((wave) => wave.wave_ref === flow.current_wave_ref && !isFinished(wave));

  return (
    <div className="space-y-1">
      {/* Fixed height, whatever the wave count. `items-end` so the taller current
          segment grows downward-anchored inside the strip instead of enlarging it. */}
      <div
        data-testid="wave-rail"
        data-wave-count={flow.wave_count}
        // `role="img"` with the prose label: the strip is a picture of progress,
        // and its individual segments are decoration once the sentence below says
        // the same thing in words.
        role="img"
        aria-label={description}
        className={`flex ${WAVE_RAIL_HEIGHT_CLASS} w-full items-end gap-0.5`}
      >
        {flow.waves.map((wave) => {
          const current = wave === currentWave;
          const finished = isFinished(wave);
          return (
            <div
              key={`${wave.epic_ref}/${wave.wave_ref}`}
              data-testid={`wave-segment-${wave.epic_ref}-${wave.wave_ref}`}
              data-current={current ? 'true' : 'false'}
              aria-hidden="true"
              title={`${wave.epic_ref} · ${wave.title || wave.wave_ref} — ${wave.done} of ${wave.total} done${wave.description ? '. ' + wave.description : ''}`}
              // Width flexes, height does not: `flex-1` narrows segments as waves
              // multiply, which is what keeps 20 waves the same height as 3.
              className={`flex-1 rounded-sm ${
                current ? 'h-full ring-2 ring-primary-500' : 'h-1/2'
              } ${
                finished
                  ? 'bg-green-500'
                  : wave.display_counts.stalled > 0
                    ? 'bg-orange-400'
                    : wave.done > 0
                      ? 'bg-blue-500'
                      : 'bg-gray-300 dark:bg-gray-600'
              }`}
            />
          );
        })}
      </div>
      <p className="text-xs text-gray-600 dark:text-gray-400" data-testid="wave-rail-caption">
        {description}
      </p>
      {currentWave?.description && <p className="text-sm text-gray-600 dark:text-gray-400">{currentWave.description}</p>}
    </div>
  );
}

/**
 * Per-state presentation for a design gate. One table, so the strip and its
 * screen-reader sentence cannot disagree about what a state means.
 *
 * `skipped` and `not_reached` are visually distinct on purpose (#4885): struck
 * through versus plain. Collapsing them would tell an operator that a gate their
 * scope deliberately skipped is still outstanding work.
 */
const DESIGN_STAGE_STYLES: Record<DesignStageState, { icon: string; word: string; className: string }> = {
  approved: {
    icon: '✓',
    word: 'approved',
    className: 'bg-green-100 text-green-900 dark:bg-green-900 dark:text-green-100',
  },
  open: {
    icon: '🚦',
    word: 'waiting on a human',
    className: 'bg-amber-100 text-amber-900 ring-1 ring-amber-500 dark:bg-amber-900 dark:text-amber-100',
  },
  skipped: {
    icon: '⊘',
    word: 'skipped for this scope',
    className: 'bg-gray-100 text-gray-500 line-through dark:bg-gray-800 dark:text-gray-400',
  },
  not_reached: {
    icon: '○',
    word: 'not started',
    className: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
  },
};

/** Short labels for the strip. The full stage name is in each chip's `title`. */
const DESIGN_STAGE_LABELS: Record<DesignStageName, string> = {
  'intent-capture': 'Intent',
  'reverse-engineering': 'Reverse-eng',
  'requirements-analysis': 'Requirements',
  'delivery-planning': 'Planning',
  'loop-proposal': 'Loop',
};

/**
 * The design-gate strip: which of the five AIDLC gates ran, and how they ended.
 *
 * **Collapsed unless a gate is open.** A settled history is one line of reassurance
 * — "✓ 4 of 5 design gates approved" — and spending five chips on it would push the
 * rollup and wave rail, which describe work still moving, below the fold. An `open`
 * gate is the one case an operator may need to act on, so that expands.
 *
 * Stages are rendered in canonical `DESIGN_STAGES` order, not the order the author
 * listed them: the strip reads as a sequence, and a document that happens to list
 * `loop-proposal` first must not render the pipeline backwards. Stages absent from
 * the document are omitted entirely rather than shown as pending — "not recorded" is
 * not a state, and inventing one fabricates a gate that may never have existed.
 *
 * Renders `null` for a null history. That is the whole no-fabrication contract at
 * the UI layer: a flow whose design loop was never captured says nothing about it.
 */
function DesignStrip({ history }: { history: DesignHistory | null }) {
  if (!history) return null;

  const byName = new Map(history.stages.map((stage) => [stage.name, stage]));
  const ordered = DESIGN_STAGES.map((name) => byName.get(name)).filter(
    (stage): stage is NonNullable<typeof stage> => stage !== undefined,
  );
  if (ordered.length === 0) return null;

  const approved = ordered.filter((stage) => stage.state === 'approved').length;
  const open = ordered.filter((stage) => stage.state === 'open');
  // "of 5", not "of ordered.length": five gates exist whether or not this document
  // recorded all of them, and "4 of 4" would imply the design loop was shorter.
  const summary = `${approved} of ${DESIGN_STAGES.length} design gates approved`;

  if (open.length === 0) {
    return (
      <p
        data-testid="design-strip-collapsed"
        data-design-scope={history.scope}
        className="text-xs text-gray-600 dark:text-gray-400"
      >
        <span aria-hidden="true">✓ </span>
        {summary}
        {/* Scope is what makes a skipped gate legible — "3 of 5" reads as unfinished
            until you know a poc scope skips one by design. */}
        <span className="text-gray-500 dark:text-gray-500"> · {history.scope} scope</span>
      </p>
    );
  }

  return (
    <div className="space-y-1" data-testid="design-strip" data-design-scope={history.scope}>
      <div className="flex flex-wrap gap-1">
        {ordered.map((stage) => {
          const style = DESIGN_STAGE_STYLES[stage.state];
          return (
            <span
              key={stage.name}
              data-testid={`design-stage-${stage.name}`}
              data-state={stage.state}
              title={`${stage.name} — ${style.word}`}
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${style.className}`}
            >
              <span aria-hidden="true">{style.icon}</span>
              {DESIGN_STAGE_LABELS[stage.name]}
            </span>
          );
        })}
      </div>
      {/* The chips are colour and glyph; this sentence is the same information for a
          screen reader and for anyone who cannot tell the ringed chip from its
          neighbours. Same reasoning as the wave rail's caption. */}
      <p className="text-xs text-gray-600 dark:text-gray-400" data-testid="design-strip-caption">
        {summary} · design waiting on a human at {open.map((stage) => stage.name).join(', ')}.
      </p>
    </div>
  );
}

/**
 * One flow, as a whole-card link into its graph.
 *
 * The card is a `<Link>` rather than a `div` with an onClick: it is genuinely a
 * navigation to `/flows/<id>`, and a real anchor is what makes it keyboard
 * reachable, middle-clickable, and announced as a link.
 */
function FlowCard({ flow }: { flow: FlowSummary }) {
  const location = useLocation();
  const allStories = flow.eval_story_count !== undefined && flow.completed_eval_story_count !== undefined;
  const total = flow.story_count === undefined ? undefined : flow.story_count + (allStories ? flow.eval_story_count! : 0);
  const complete = flow.completed_story_count === undefined ? undefined : flow.completed_story_count + (allStories ? flow.completed_eval_story_count! : 0);
  const action = flow.status === 'archived' ? 'View archived flow' : flow.awaiting_gate_count > 0 ? 'Review decisions' : flow.display_counts.stalled > 0 ? 'Inspect blockers' : 'Open flow';
  const currentWave = flow.waves.find(wave => wave.wave_ref === flow.current_wave_ref && !isFinished(wave));
  return (
    <li className="blueprint-flow-card min-w-0 overflow-hidden rounded-lg border border-gray-200 dark:border-gray-700">
      <Link to={`/flows/${flow.id}`} state={{ flowsReturn: `${location.pathname}${location.search}`, flowsScroll: window.scrollY }}
        onClick={() => {
          try { sessionStorage.setItem(`flows-scroll:${location.pathname}${location.search}`, String(window.scrollY)); } catch { /* Navigation still works without storage. */ }
        }}
        data-testid={`flow-card-${flow.id}`}
        className="block space-y-2 p-4 transition-colors hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:hover:bg-gray-800">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h2 title={flow.title} className="line-clamp-2 font-medium text-gray-900 dark:text-gray-100">{flow.title}</h2>
            <p className="mt-0.5 truncate text-xs text-gray-500">{flow.product_id ? 'Product-bound · ' : ''}{flow.intent_ref ? intentLabel(String(flow.intent_ref)) : flow.slug}</p>
          </div>
          <Badge variant={STATUS_VARIANTS[flow.status]}>{flow.status !== 'archived' && flow.changes_requested_count ? 'Changes requested' : STATUS_LABELS[flow.status]}</Badge>
        </div>
        <p className="line-clamp-1 text-sm text-gray-600 dark:text-gray-400" data-testid="flow-next-step">
          {flow.execution_paused && flow.status !== 'complete' && flow.status !== 'archived' ? 'Execution is paused. ' : ''}
          {flow.status !== 'archived' && flow.awaiting_gate_count > 0 && <span data-testid="awaiting-you">{flow.awaiting_gate_count} awaiting review{gatedWaves(flow).length ? ` · ${gatedWaves(flow).join(', ')}` : ''}. </span>}
          {flow.status !== 'archived' && flow.display_counts.stalled > 0 && <span data-testid="stalled-count">{flow.display_counts.stalled} stalled steps. </span>}
          {flow.status === 'complete' ? 'All required work is complete.' : flow.status === 'archived' ? 'Archived flow; open to inspect its history.' : flow.changes_requested_count ? 'Review feedback before reopening the affected gates.' : currentWave ? `Current group: ${currentWave.title || currentWave.wave_ref}` : flow.description || 'Open the plan to inspect progress and next steps.'}
        </p>
        {total !== undefined && complete !== undefined
          ? <StoryProgress total={total} complete={complete} scope={allStories ? 'stories' : 'implementation stories'} />
          : <p className="text-xs">{flow.display_counts.complete} of {flow.total_nodes} work items complete</p>}
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <CostFigureDisplay figure={flow.delivery_cost} label="Agent spend" />
          <span className="font-medium text-primary-700 dark:text-primary-300">{action} →</span>
        </div>
      </Link>
      <details className="border-t border-gray-100 px-4 text-xs dark:border-gray-700">
        <summary className="cursor-pointer py-2 text-gray-500">More details and controls</summary>
        <div className="space-y-3 pb-3">
          <p className="text-xs text-gray-500">{flow.slug}</p>
          {flow.description && <p data-testid="flow-description" className="text-sm">{flow.description}</p>}
          {!!flow.changes_requested_count && <p data-testid="changes-requested-summary">{flow.changes_requested_count} gates need changes. Work behind these gates is paused. Review feedback before reopening review.</p>}
          {flow.story_count !== undefined && flow.gate_count !== undefined && flow.eval_count !== undefined && <PlanSummary stories={flow.story_count} evaluationStories={flow.eval_story_count} waves={flow.wave_count} gates={flow.gate_count} evaluations={flow.eval_count} />}
          <RollupBar counts={flow.display_counts} total={flow.total_nodes} />
          <WaveRail flow={flow} />
          <DesignStrip history={flow.design_history} />
          {flow.status !== 'archived' && <FlowExecutionControl flowId={flow.id} paused={flow.execution_paused} compact />}
          <FlowArchiveControl flowId={flow.id} archived={flow.status === 'archived'} compact />
        </div>
      </details>
    </li>
  );
}

/**
 * Placeholder cards for the first load.
 *
 * Cards, not a bare spinner: these rows have real height, and a spinner that is
 * replaced by 25 cards shifts everything the operator was about to click.
 */
function SkeletonCards() {
  return (
    <ul className="space-y-3" data-testid="flows-skeleton" aria-busy="true" aria-label="Loading delivery flows">
      {[0, 1, 2].map((index) => (
        <li
          key={index}
          className="animate-pulse rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        >
          <div className="h-4 w-1/3 rounded bg-gray-200 dark:bg-gray-700" />
          <div className="mt-2 h-3 w-1/4 rounded bg-gray-200 dark:bg-gray-700" />
          <div className="mt-4 h-4 w-full rounded-full bg-gray-200 dark:bg-gray-700" />
          <div className={`mt-3 ${WAVE_RAIL_HEIGHT_CLASS} w-full rounded bg-gray-200 dark:bg-gray-700`} />
        </li>
      ))}
    </ul>
  );
}

/** Whether any filter is narrowing the list — decides which empty copy shows. */
function hasActiveFilters(filters: FlowFilters): boolean {
  return Boolean(filters.q) || Boolean(filters.status) || filters.needsMe;
}

function flowLoadError(error: unknown): string {
  const failure = error as { message?: unknown; detail?: unknown } | null;
  if (typeof failure?.detail === 'string') return failure.detail;
  if (typeof failure?.message === 'string') return failure.message;
  return 'The list could not be fetched. Try again in a moment.';
}

export function FlowsList({ productId, projectId, readerKey }: { productId?: string; projectId?: string; readerKey?: string } = {}) {
  const { filters, setFilters } = useFlowFilters();
  const { data, isPending, isError, error, dataUpdatedAt, isFetching, refetch } = useFlows(filters, productId, projectId, readerKey);
  const location = useLocation();
  useEffect(() => {
    try {
      const key = `flows-scroll:${location.pathname}${location.search}`;
      const saved = sessionStorage.getItem(key);
      if (data && saved !== null) {
        window.scrollTo(0, Number(saved));
        sessionStorage.removeItem(key);
      }
    } catch { /* The list remains usable without storage. */ }
  }, [data, location.pathname, location.search]);
  const clearFilters = () => setFilters({ q: '', status: undefined, needsMe: false, includeArchived: false, offset: 0 });

  const total = data?.total ?? 0;
  const shown = data?.flows.length ?? 0;
  const filtered = hasActiveFilters(filters);
  const Heading = productId ? 'h2' : 'h1';

  return (
    <div className="space-y-4">
      <header className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <Heading className="text-xl font-semibold text-gray-900 dark:text-gray-100">Delivery Flows</Heading>
            <div className="flex gap-4 text-sm text-primary-700 dark:text-primary-300"><Link className="underline" to={productId ? `/flows/new?${new URLSearchParams({ product: productId, ...(projectId ? { project: projectId } : {}) })}` : '/flows/new'}>New flow</Link><Link className="underline" to={`${location.pathname}?needs_me=true&sort=stalled`}>Attention view</Link></div>
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-400">
              {projectId ? 'Delivery flows in this project share its total AI usage budget.' : productId ? 'Track delivery across all projects in this product.' : 'Track delivery, review decisions and inspect agent spend.'}
            </p>
          </div>
          <LastUpdated dataUpdatedAt={dataUpdatedAt} isFetching={isFetching} />
        </div>

        {/* Chips: Product/tenant counts, never scoped to the active filter. Clicking
            one filters; clicking the selected one clears it, so a chip is not a
            trap you can only leave via the sort control. */}
        {data && (
          <ul className="flex gap-2 overflow-x-auto pb-1 [&>li]:shrink-0" data-testid="status-chips">
            {FLOW_STATUSES.map((status) => {
              const selected = filters.status === status;
              return (
                <li key={status}>
                  <button
                    type="button"
                    data-testid={`status-chip-${status}`}
                    aria-pressed={selected}
                    onClick={() => setFilters({ status: selected ? undefined : status })}
                    className={`rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
                      selected
                        ? 'border-primary-500 bg-primary-100 text-primary-800 dark:bg-primary-900 dark:text-primary-100'
                        : 'border-gray-300 text-gray-700 hover:bg-gray-100 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-800'
                    }`}
                  >
                    {STATUS_LABELS[status]}
                    {/* Zero-count chips still render: "nothing is stalled" is
                        information, and a chip row whose shape changes as work
                        moves is harder to scan than one that does not. */}
                    <span className="ml-1.5 tabular-nums">{data.status_counts[status] ?? 0}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        <div className="flex flex-wrap items-end gap-3">
          <div className="w-full sm:w-64">
            <Input
              name="flows-search"
              label="Search"
              placeholder="Title, slug, or intent number"
              value={filters.q}
              onChange={(event) => setFilters({ q: event.target.value })}
            />
          </div>

          <div className="w-40">
            <Select
              name="flows-sort"
              label="Sort"
              value={filters.sort}
              options={FLOW_SORTS.map((sort) => ({ value: sort, label: SORT_LABELS[sort] }))}
              onChange={(event) => setFilters({ sort: event.target.value as FlowSort })}
            />
          </div>

          <label className="flex items-center gap-2 pb-2 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="checkbox"
              data-testid="needs-me-filter"
              checked={filters.needsMe}
              onChange={(event) => setFilters({ needsMe: event.target.checked })}
              className="h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
            />
            Needs attention or review
          </label>

          <label className="flex items-center gap-2 pb-2 text-sm text-gray-700 dark:text-gray-300">
            <input
              type="checkbox"
              data-testid="include-archived-filter"
              checked={filters.includeArchived}
              onChange={(event) => setFilters({ includeArchived: event.target.checked })}
              className="h-4 w-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
            />
            Include archived flows
          </label>
        </div>
      </header>

      {filtered && <button type="button" className="rounded border px-3 py-2 text-sm" onClick={clearFilters}>Clear filters</button>}
      {isPending && <SkeletonCards />}

      {isError && (
        // Deliberately not an empty list: "no flows" is a claim about the
        // organisation, and the truth here is that the request failed.
        <div data-testid="flows-error">
          <Alert variant="error" title="Delivery flows could not be loaded">
            {flowLoadError(error)}
          </Alert>
          <button type="button" className="mt-2 rounded border px-3 py-2 text-sm" onClick={() => void refetch()}>Retry loading flows</button>
        </div>
      )}

      {data && (
        <>
          {data.flows.some((flow) => flow.delivery_cost.reason === 'cost_refresh_unavailable') && (
            <Alert variant="info" title="Spend is temporarily unavailable">
              Delivery progress is available. Spend will refresh automatically.
            </Alert>
          )}
          <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="flows-summary">
            {/* `total` is the count of rows matching the filters across ALL pages,
                not this page's length — so this sentence stays true while paging. */}
            Showing {shown} of {total} {total === 1 ? 'flow' : 'flows'}
            {filtered && ' matching your filters'}
          </p>

          {shown === 0 &&
            (filtered ? (
              <div data-testid="flows-empty-filtered">
                <Alert variant="info" title="No flows match these filters">
                  {productId ? 'No flows in this product match your filters. Clear the filters to see all its flows.' : 'Nothing in your organisation matches this search. Clear the filters to see every flow.'}
                </Alert>
              </div>
            ) : (
              <div data-testid="flows-empty">
                <Alert variant="info" title="No delivery flows yet">
                  {projectId ? 'No delivery flows are linked to this project yet.' : productId ? 'No delivery flows are linked to this product yet.' : 'Your organisation has no delivery plans. One appears here as soon as a plan is registered.'}
                </Alert>
              </div>
            ))}

          {shown > 0 && (
            <ul className="space-y-3" data-testid="flows-list">
              {data.flows.map((flow) => (
                <FlowCard key={flow.id} flow={flow} />
              ))}
            </ul>
          )}

          {total > FLOWS_PAGE_SIZE && (
            <nav className="flex items-center justify-between gap-3" aria-label="Flows pagination">
              <button
                type="button"
                data-testid="flows-prev"
                disabled={filters.offset === 0}
                onClick={() => setFilters({ offset: Math.max(0, filters.offset - FLOWS_PAGE_SIZE) })}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300"
              >
                Previous
              </button>
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {filters.offset + 1}–{filters.offset + shown} of {total}
              </span>
              <button
                type="button"
                data-testid="flows-next"
                disabled={filters.offset + shown >= total}
                onClick={() => setFilters({ offset: filters.offset + FLOWS_PAGE_SIZE })}
                className="rounded-lg border border-gray-300 px-3 py-1.5 text-sm text-gray-700 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300"
              >
                Next
              </button>
            </nav>
          )}
        </>
      )}
    </div>
  );
}

export default FlowsList;
