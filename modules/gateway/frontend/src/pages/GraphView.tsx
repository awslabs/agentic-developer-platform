import { ExecutionWindowControl } from '@/components/orchestration/ExecutionWindowControl';
import { BudgetEnforcementControl } from '@/components/budget/BudgetEnforcementControl';
import { FlowExecutionControl } from '@/components/orchestration/FlowExecutionControl';
/** Delivery flow: collapsible waves with dependency-ordered parallel groups. */
import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useFlowGraph } from '@/hooks/useFlowGraph';
import { useFlowExecution } from '@/hooks/useFlowExecution';
import { countByDisplayState } from '@/utils/nodeState';
import { countStories } from '@/utils/storyCounts';
import { groupIntoEpics } from '@/utils/flowLayout';
import { RollupBar } from '@/components/orchestration/RollupBar';
import { PlanSummary } from '@/components/orchestration/PlanSummary';
import { NodeChip } from '@/components/orchestration/NodeChip';
import { WaveCard } from '@/components/orchestration/WaveCard';
import { CostFigureDisplay } from '@/components/orchestration/CostFigureDisplay';
import { ExecutionProgress } from '@/components/orchestration/ExecutionProgress';
import { executionForNode } from '@/utils/executionProgress';
import { LastUpdated } from '@/components/LastUpdated';
import { Alert, Spinner } from '@/components/ui';

export function GraphView() {
  const { flowId } = useParams<{ flowId: string }>();
  const [expandedWaves, setExpandedWaves] = useState<Record<string, boolean>>({});
  const { data, isPending, isError, error, dataUpdatedAt, isFetching } = useFlowGraph(flowId);
  // The delivery ledger (issue #5145), queried once for the whole flow and handed
  // down — never per node, which on a long flow would be one request per chip.
  // Deliberately a separate query from the graph: a ledger read failing must not
  // blank the plan, and the graph is the older, load-bearing view.
  // `isError` is read, not discarded: a denied or failed ledger read that merely
  // removed the panel would be pixel-identical to the feature not existing, which is
  // the worst outcome for whoever is debugging it — the screen would actively suggest
  // there is nothing to debug. It is surfaced as a non-blocking notice below while the
  // graph stays fully usable.
  const {
    data: executionView,
    isError: executionFailed,
    error: executionError,
  } = useFlowExecution(flowId);

  if (isPending) {
    return (
      <div className="flex items-center gap-3 p-6" data-testid="graph-loading">
        <Spinner size="sm" />
        <span className="text-sm text-gray-600 dark:text-gray-400">Loading the delivery journey…</span>
      </div>
    );
  }

  if (isError || !data) {
    // Deliberately not an empty graph. "No nodes" tells an operator "no work
    // left", which is a different and more misleading answer than "not found" —
    // and a cross-tenant or deleted flow_id 404s here.
    // `Alert` takes no `data-testid`, so the test hook goes on a wrapper rather
    // than widening a shared component's props for one caller.
    return (
      <div className="p-6" data-testid="graph-error">
        <Alert variant="error" title="This flow could not be loaded">
          {(error as { message?: string } | null)?.message ||
            'No such flow, or it belongs to another organisation.'}
        </Alert>
      </div>
    );
  }

  const counts = countByDisplayState(data.nodes);
  const segmented = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const activeNodes = data.nodes.filter((node) => node.state !== 'superseded');
  const stories = countStories(activeNodes);
  const historicalNodes = data.nodes.filter((node) => node.state === 'superseded');
  const epics = groupIntoEpics({ ...data, nodes: activeNodes });
  const changesRequested = activeNodes.filter((node) => node.state === 'rejected_at_gate');
  const waves = epics.flatMap((epic) => epic.waves.map((wave) => ({ epicRef: epic.epicRef, wave })));
  const firstUnfinished = waves.find(({ wave }) => wave.nodes.some((node) => node.state !== 'passed'));
  const waveKey = (epicRef: string, waveRef: string) => JSON.stringify([flowId, epicRef, waveRef]);
  // Rendered only once the ledger read has returned. Before that the journey shows
  // exactly what it shows today: an absence panel here would claim "no execution
  // record" — which means the flow predates the ledger — about a flow whose record
  // simply has not arrived yet.
  const renderExecution = executionView
    ? (node: typeof activeNodes[number]) => {
        if (node.kind !== 'story') return null;
        const { execution, earlierCycles } = executionForNode(executionView, node.id);
        return (
          <ExecutionProgress
            nodeRef={node.node_ref}
            execution={execution}
            // The response's own clock, so age is two instants from one source.
            serverTime={executionView.server_time}
            legacy={executionView.legacy}
            earlierCycles={earlierCycles}
            // The graph's own verdict, which is the ONLY thing that may render this
            // panel as complete. The ledger's `concluded` means "no further pickup"
            // and is reached by a cycle that gave up as well as one that delivered,
            // so acceptance has to come from here — the authoritative state — rather
            // than be inferred from the execution row.
            nodeAccepted={node.state === 'passed'}
          />
        );
      }
    : undefined;
  const setAllExpanded = (expanded: boolean) => setExpandedWaves(Object.fromEntries(
    waves.map(({ epicRef, wave }) => [waveKey(epicRef, wave.waveRef), expanded])
  ));

  return (
    <div className="space-y-6 p-6">
      <header className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">{data.title}</h1>
            {/* The origin strip: the intent this whole journey traces back to.
                `slug` is shown, never the internal graph address (§7.2). */}
            {/* Same reasoning as the node chips: the intent is shown as a number,
                not a link, because the payload carries no repo to build a URL from. */}
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-400" data-testid="origin-strip">
              <span className="font-mono">{data.slug}</span>
              {data.intent_ref && (
                <span className="font-mono"> · from intent #{String(data.intent_ref).replace(/^#/, '')}</span>
              )}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <CostFigureDisplay figure={data.cost} label="Total" showScope />
            <LastUpdated dataUpdatedAt={dataUpdatedAt} isFetching={isFetching} />
          </div>
        </div>

        <ExecutionWindowControl key={flowId} flowId={flowId!} window={data.execution_window} />
        <FlowExecutionControl flowId={flowId!} paused={data.execution_paused} />
        <BudgetEnforcementControl key={flowId} flowId={flowId} />

        <PlanSummary
          stories={stories.implementation}
          evaluationStories={stories.evaluation}
          waves={new Set(activeNodes.map((node) => `${node.epic_ref}/${node.wave_ref}`)).size}
          gates={activeNodes.filter((node) => node.kind === 'gate').length}
          evaluations={activeNodes.filter((node) => node.kind === 'eval').length}
          policy={data.execution_policy}
        />
        <RollupBar counts={counts} total={segmented} stories={{
          complete: stories.complete,
          total: stories.total,
        }} />
      </header>

      {changesRequested.length > 0 && (
        <Alert variant="warning" title="Changes requested">
          Work behind the affected gates is paused. Requesting changes records feedback; it does not start an agent to revise the plan.
          {' '}Review the notes below, update the plan as needed, then use “Reopen review” for another approval decision.
        </Alert>
      )}

      {/* A ledger read that failed says so, rather than vanishing. `warning`, not
          `error`: the plan below is intact and current, and only the delivery-progress
          detail is missing — styling this as an error would overstate the damage. It is
          purely informational; nothing here retries or mutates, and the graph,
          the waves and every existing control continue to work. */}
      {executionFailed && (
        <div data-testid="execution-unavailable">
          <Alert variant="warning" title="Execution progress is unavailable">
            The delivery plan below is current, but the execution ledger could not be read, so
            per-story progress, blocks and evidence are not shown. This does not mean delivery has
            stopped — it means this view cannot currently tell you where it stands.
            {(executionError as { message?: string } | null)?.message
              ? ` (${(executionError as { message?: string }).message})`
              : null}
          </Alert>
        </div>
      )}

      {activeNodes.length === 0 && (
        <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="graph-empty">
          This flow has no planned work yet.
        </p>
      )}

      {waves.length > 0 && (
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="max-w-2xl space-y-1 text-sm text-gray-600 dark:text-gray-400">
            <p className="font-medium text-gray-900 dark:text-gray-100">Read each wave from top to bottom.</p>
            <p>Parallel paths can progress together. Each step starts after its own dependencies pass, subject to approvals and worker availability.</p>
            <p className="text-xs">Wave cards group the plan. Their dependency links determine execution order.</p>
          </div>
          <div className="flex shrink-0 gap-3 text-sm">
            <button type="button" onClick={() => setAllExpanded(true)} className="text-blue-700 underline dark:text-blue-300">Expand all waves</button>
            <button type="button" onClick={() => setAllExpanded(false)} className="text-blue-700 underline dark:text-blue-300">Collapse all waves</button>
          </div>
        </div>
      )}

      {epics.map((epic) => (
        <section
          key={epic.epicRef}
          data-testid={`epic-${epic.epicRef}`}
          data-container="epic"
          aria-labelledby={`epic-heading-${epic.epicRef}`}
          className="min-w-0 space-y-3"
        >
          <h2 id={`epic-heading-${epic.epicRef}`} className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {epic.title || epic.epicRef}
          </h2>
          {epic.description && <p className="max-w-4xl whitespace-pre-line text-sm leading-relaxed text-gray-600 dark:text-gray-300">{epic.description}</p>}
          {epic.waves.map((wave) => {
            const key = waveKey(epic.epicRef, wave.waveRef);
            const needsAttention = wave.nodes.some((node) => ['running', 'awaiting_merge', 'awaiting_gate', 'rejected_at_gate', 'failed', 'halted'].includes(node.state) || node.stalled);
            const expanded = expandedWaves[key] ?? (needsAttention || firstUnfinished?.wave === wave);
            return (
              <WaveCard
                key={key}
                epicRef={epic.epicRef}
                wave={wave}
                graph={data}
                expanded={expanded}
                onToggle={() => setExpandedWaves((current) => ({ ...current, [key]: !expanded }))}
                renderExecution={renderExecution}
              />
            );
          })}
        </section>
      ))}
      {historicalNodes.length > 0 && (
        <details className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
          <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-400">
            Superseded steps ({historicalNodes.length}) — history, excluded from the current plan
          </summary>
          <ul className="mt-3 space-y-2">
            {historicalNodes.map((node) => (
              <NodeChip key={node.id} node={node} execution={renderExecution?.(node)} />
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export default GraphView;
