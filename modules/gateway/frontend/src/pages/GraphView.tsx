import { ExecutionWindowControl } from '@/components/orchestration/ExecutionWindowControl';
import { BudgetEnforcementControl } from '@/components/budget/BudgetEnforcementControl';
import { FlowBudgetSummary, StoryRunBudgets } from '@/components/orchestration/FlowBudgetSummary';
import { FlowExecutionControl } from '@/components/orchestration/FlowExecutionControl';
import { FlowArchiveControl } from '@/components/orchestration/FlowArchiveControl';
import { openGates, shortTime } from '@/utils/openGates';
/** Delivery flow: BOLTs contain their engine phases; other waves retain their cards. */
import { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom';
import { StoryProgress } from '@/components/orchestration/StoryProgress';
import { FlowTimeline } from '@/components/orchestration/FlowTimeline';
import { AmendmentCount } from '@/components/orchestration/AmendmentCount';
import { flowSummary, intentLabel, nodeNextStep } from '@/utils/flowSummary';
import { useFlowGraph } from '@/hooks/useFlowGraph';
import { useFlowActivity } from '@/hooks/useFlowActivity';
import { useFlowExecution } from '@/hooks/useFlowExecution';
import { countByDisplayState } from '@/utils/nodeState';
import { countStories } from '@/utils/storyCounts';
import { groupIntoEpics, type WaveGroup } from '@/utils/flowLayout';
import { RollupBar } from '@/components/orchestration/RollupBar';
import { PlanSummary } from '@/components/orchestration/PlanSummary';
import { NodeChip } from '@/components/orchestration/NodeChip';
import { WaveCard } from '@/components/orchestration/WaveCard';
import { CostFigureDisplay } from '@/components/orchestration/CostFigureDisplay';
import { ExecutionProgress } from '@/components/orchestration/ExecutionProgress';
import { AmendmentReviewPanel } from '@/components/orchestration/AmendmentReviewPanel';
import { executionForNode } from '@/utils/executionProgress';
import { withStoryActivity } from '@/utils/runtimeBudgetWait';
import { LastUpdated } from '@/components/LastUpdated';
import { Alert, Spinner } from '@/components/ui';

const TABS = ['Overview', 'Timeline', 'Evidence', 'Settings', 'Plan changes'] as const;

export function GraphView() {
  const { flowId } = useParams<{ flowId: string }>();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const rawTab = searchParams.get('tab');
  const activeTab = TABS.find(tab => tab.toLowerCase().replace(' ', '-') === rawTab) ?? 'Overview';
  const [visitedTabs, setVisitedTabs] = useState<Record<string, boolean>>({});
  const tabKey = (tab: string) => `${flowId}:${tab}`;
  const selectTab = (tab: typeof TABS[number]) => {
    setVisitedTabs(current => ({ ...current, [tabKey(activeTab)]: true, [tabKey(tab)]: true }));
    const next = new URLSearchParams(searchParams);
    if (tab === 'Overview') next.delete('tab'); else next.set('tab', tab.toLowerCase().replace(' ', '-'));
    setSearchParams(next, { replace: true });
  };
  const [focusNode, setFocusNode] = useState<string>();
  useEffect(() => {
    if (!focusNode || activeTab !== 'Overview') return;
    const element = document.getElementById(`flow-node-${focusNode}`);
    if (element) {
      // The application header wraps on narrow screens. Measure it at the
      // moment of navigation so long plans never scroll a decision behind it.
      const headerHeight = document.querySelector('.blueprint-header')?.getBoundingClientRect().height ?? 80;
      element.style.scrollMarginTop = `${headerHeight + 16}px`;
      element.scrollIntoView?.({ block: 'start' });
      element.focus({ preventScroll: true });
      setFocusNode(undefined);
    }
  }, [focusNode, activeTab]);
  const [expandedWaves, setExpandedWaves] = useState<Record<string, boolean>>({});
  const [historyFlowId, setHistoryFlowId] = useState<string>();
  const { data, isPending, isError, error, dataUpdatedAt, isFetching, refetch } = useFlowGraph(flowId);
  const activeGraphNodes = useMemo(() => data?.nodes.filter((node) => node.state !== 'superseded') ?? [], [data]);
  const epics = useMemo(() => data ? groupIntoEpics({ ...data, nodes: activeGraphNodes }) : [], [data, activeGraphNodes]);
  const waves = epics.flatMap((epic) => epic.waves.map((wave) => ({ epicRef: epic.epicRef, wave })));
  const boltCount = waves.filter(({ wave }) => wave.phases).length;
  const standaloneWaveCount = waves.length - boltCount;
  const firstUnfinished = waves.find(({ wave }) => wave.nodes.some((node) => node.state !== 'passed'));
  const waveKey = (epicRef: string, waveRef: string) => JSON.stringify([flowId, epicRef, waveRef]);
  const waveExpanded = (epicRef: string, wave: typeof waves[number]['wave']) => {
    const needsAttention = wave.nodes.some((node) => ['running', 'awaiting_merge', 'awaiting_gate', 'rejected_at_gate', 'failed', 'halted'].includes(node.state) || node.stalled);
    return expandedWaves[waveKey(epicRef, wave.waveRef)] ?? (needsAttention || (firstUnfinished?.epicRef === epicRef && firstUnfinished.wave.waveRef === wave.waveRef));
  };
  const visibleNodes = activeTab !== 'Overview' ? [] : waves.flatMap(({ epicRef, wave }) => waveExpanded(epicRef, wave)
    ? wave.nodes.filter((node) => node.kind === 'story') : []);
  const activity = useFlowActivity(flowId, data, visibleNodes, dataUpdatedAt);
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
  } = useFlowExecution(flowId, {
    // Initial ledger work must not compete with the core plan read. A previous
    // flow's graph is not sufficient when navigating to another flow.
    enabled: !isPending && !isError && data?.flow_id === flowId,
  });

  if (isPending) {
    return (
      <div className="flex items-center gap-3 p-6" data-testid="graph-loading" role="status">
        <Spinner size="sm" />
        <span className="text-sm text-gray-600 dark:text-gray-400">Loading the delivery journey…</span>
      </div>
    );
  }

  if (isError || !data || data.flow_id !== flowId) {
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
        <button type="button" className="mt-3 rounded border px-3 py-2 focus:ring-2 focus:ring-primary-500" onClick={() => void refetch()}>Retry loading flow</button>
      </div>
    );
  }

  const returnTo = location.state?.flowsReturn;
  const flowsReturn = typeof returnTo === 'string' && /^\/(?:flows|products\/[^/?#]+\/projects\/[^/?#]+\/flows)(?:\?[^#]*)?$/.test(returnTo) ? returnTo : '/flows';
  const openNode = (nodeId: string) => {
    const containing = waves.find(({ wave }) => wave.nodes.some(node => node.id === nodeId));
    if (containing) setExpandedWaves(current => ({ ...current, [waveKey(containing.epicRef, containing.wave.waveRef)]: true }));
    selectTab('Overview');
    setFocusNode(nodeId);
  };
  const productContextReported = Boolean(data.product_id || data.product_status);
  const counts = countByDisplayState(data.nodes);
  const segmented = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const activeNodes = activeGraphNodes;
  const stories = countStories(activeNodes, boltCount > 0);
  const historicalNodes = data.nodes.filter((node) => node.state === 'superseded');
  const changesRequested = activeNodes.filter((node) => node.state === 'rejected_at_gate');
  const displayGraph = data.activity_included === false ? {
    ...data,
    nodes: data.nodes.map((node) => {
      const item = activity.item(node);
      const current = item && activity.status(node) !== 'unavailable'
        ? withStoryActivity(node, item.execution_history) : node;
      // SQL task progress can remain available when optional run-history reads
      // fail. A new attempt receives its own item; earlier cards never leak in.
      return item ? { ...current, run_insight: item.run_insight ?? null } : current;
    }),
  } : data;
  // Activity changes node details, never dependency order. Reuse the layout
  // instead of traversing the dependency graph again for every activity batch.
  const summary = flowSummary(displayGraph);
  const displayById = new Map(displayGraph.nodes.map((node) => [node.id, node]));
  const displayNodes = (nodes: typeof activeNodes) => nodes.map((node) => displayById.get(node.id) ?? node);
  const displayWave = (wave: WaveGroup): WaveGroup => ({
    ...wave, nodes: displayNodes(wave.nodes), stages: wave.stages.map(displayNodes),
    unordered: displayNodes(wave.unordered), phases: wave.phases?.map(displayWave),
  });
  const displayEpics = data.activity_included === false
    ? epics.map((epic) => ({ ...epic, waves: epic.waves.map(displayWave) })) : epics;
  // Rendered only once the ledger read has returned. Before that the journey shows
  // exactly what it shows today: an absence panel here would claim "no execution
  // record" — which means the flow predates the ledger — about a flow whose record
  // simply has not arrived yet.
  const renderExecutionProgress = executionView?.flow_id === flowId
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
  const renderExecution = (node: typeof activeNodes[number]) => node.kind === 'story' ? <>
    {renderExecutionProgress?.(node)}
    <StoryRunBudgets key={`${flowId}:${node.id}`} flowId={flowId!} nodeId={node.id} active={activeTab === 'Overview' || activeTab === 'Evidence'} />
  </> : null;
  const nodesWithEvidence = new Set(executionView?.flow_id === flowId ? executionView.executions.filter(execution => execution.actions.length > 0).map(execution => execution.node_id) : []);
  const evidenceNodes = activeNodes.filter(node => node.kind === 'eval' || node.kind === 'gate' || node.issue_url || node.bound_pull_request || nodesWithEvidence.has(node.id));
  const setAllExpanded = (expanded: boolean) => setExpandedWaves(Object.fromEntries(
    waves.map(({ epicRef, wave }) => [waveKey(epicRef, wave.waveRef), expanded])
  ));

  return (
    <div className="min-w-0 space-y-4 p-3 [overflow-wrap:anywhere] sm:p-6">
      <Link className="inline-block py-1 text-sm text-primary-700 underline dark:text-primary-300" to={flowsReturn}>← Delivery flows</Link>
      <header className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <h1 title={data.title} className="line-clamp-3 text-xl font-semibold text-gray-900 dark:text-gray-100">{data.title}</h1>
            {/* The origin strip: the intent this whole journey traces back to.
                `slug` is shown, never the internal graph address (§7.2). */}
            {/* Same reasoning as the node chips: the intent is shown as a number,
                not a link, because the payload carries no repo to build a URL from. */}
            <p className="mt-1 text-sm text-gray-600 dark:text-gray-400" data-testid="origin-strip">
              <span className="font-mono">{data.slug}</span>
              {data.intent_ref && (
                <span className="font-mono"> · {intentLabel(String(data.intent_ref))}</span>
              )}
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <CostFigureDisplay figure={data.cost} label="Agent spend" />
            <LastUpdated dataUpdatedAt={dataUpdatedAt} isFetching={isFetching} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <p className="rounded bg-gray-100 px-3 py-1 text-sm font-semibold dark:bg-gray-800" data-testid="flow-status">{summary.state}</p>
          {data.product_name && <p className="text-sm text-gray-600 dark:text-gray-400">Product: {data.product_name}</p>}
        </div>
        <StoryProgress complete={stories.complete} total={stories.total} />
        {!summary.complete && data.execution_paused && !data.archived_at && <p className="text-sm">New work is paused. <button type="button" className="underline" onClick={() => selectTab('Settings')}>Review execution settings</button></p>}
        {!summary.complete && data.execution_window?.status === 'expired' && <Alert variant="warning" title="Execution window expired">
          Further execution needs a renewed window. <button type="button" className="underline" onClick={() => selectTab('Settings')}>Review execution settings</button>
        </Alert>}
        {summary.attention.length > 0 && <div className="rounded-lg border border-amber-300 p-3 text-sm" aria-label="Current blockers">
          <p className="font-medium">{summary.attention.length} {summary.attention.length === 1 ? 'step needs' : 'steps need'} attention</p>
          <ul className="mt-2 space-y-2">{summary.attention.slice(0, 3).map(node => <li key={node.id}>
            <button type="button" className="font-medium underline" onClick={() => openNode(node.id)}>{node.title}</button>: {nodeNextStep(node)}
            {node.delivery_progress?.actor && node.delivery_progress.actor !== 'none' && <span className="block text-xs">Responsible: {node.delivery_progress.actor.replace(/-/g, ' ')}</span>}
          </li>)}</ul>
          {summary.attention.length > 3 && <p className="mt-2">{summary.attention.length - 3} more affected steps in the plan below.</p>}
        </div>}
      </header>

      {/* Gates chain: the engine presents the next one within a minute of an approval,
          and it looks like the one just answered. Name the open gate, its wave and
          when it appeared, so "waiting on you" is never mistaken for a lost decision. */}
      {openGates(data).length > 0 && (
        <div data-testid="open-gates">
          <Alert variant="warning" title={openGates(data).length === 1 ? 'One decision is awaiting review' : `${openGates(data).length} decisions are awaiting review`}>
            <ul className="list-disc pl-5">
              {openGates(data).map(({ node, waveTitle, since }) => (
                <li key={node.id} data-testid={`open-gate-${node.node_ref}`}>
                  <a className="underline" onClick={(event) => { event.preventDefault(); openNode(node.id); }} href={`#flow-node-${node.id}`}>{node.title}</a>
                  {' '}— {waveTitle}{since ? <> · presented at <time dateTime={since}>{shortTime(since)}</time></> : null}
                </li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      {changesRequested.length > 0 && (
        <Alert variant="warning" title="Changes requested">
          Work behind the affected gates is paused. Requesting changes records feedback; it does not start an agent to revise the plan.
          {' '}Review the notes below, update the plan as needed, then use “Reopen review” for another approval decision.
        </Alert>
      )}

      <div role="tablist" aria-label="Flow views" className="flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-gray-700">
        {TABS.map((tab, index) => <button key={tab} type="button" role="tab" id={`flow-tab-${index}`} aria-controls={`flow-panel-${index}`}
          aria-selected={activeTab === tab} tabIndex={activeTab === tab ? 0 : -1}
          className={`shrink-0 border-b-2 px-3 py-3 text-sm focus:ring-2 focus:ring-primary-500 ${activeTab === tab ? 'border-primary-500 font-semibold text-primary-700 dark:text-primary-300' : 'border-transparent text-gray-600 dark:text-gray-400'}`}
          onClick={() => selectTab(tab)} onKeyDown={event => {
            const next = event.key === 'ArrowRight' ? (index + 1) % TABS.length : event.key === 'ArrowLeft' ? (index + TABS.length - 1) % TABS.length : event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : null;
            if (next !== null) { event.preventDefault(); selectTab(TABS[next]); document.getElementById(`flow-tab-${next}`)?.focus(); }
          }}>{tab}{tab === 'Plan changes' && <AmendmentCount flowId={flowId!} />}</button>)}
      </div>
      <section id="flow-panel-0" role="tabpanel" aria-labelledby="flow-tab-0" hidden={activeTab !== 'Overview'} className="space-y-4">
      {(activeTab === 'Overview' || visitedTabs[tabKey('Overview')]) && <>
      <FlowBudgetSummary key={flowId} flowId={flowId!} active={activeTab === 'Overview'} />
      <details className="rounded border border-gray-200 p-3 dark:border-gray-700">
        <summary className="cursor-pointer text-sm">Plan structure, targets and permissions</summary>
        <div className="mt-3 space-y-3">
        <PlanSummary
          stories={stories.implementation}
          evaluationStories={stories.evaluation}
          waves={standaloneWaveCount}
          bolts={boltCount}
          gates={activeNodes.filter((node) => node.kind === 'gate').length}
          evaluations={activeNodes.filter((node) => node.kind === 'eval').length}
          policy={data.execution_policy}
          product={productContextReported ? { id: data.product_id, name: data.product_name, status: data.product_status } : null}
          planningEpics={productContextReported ? data.planning_epics : undefined}
          storyTargets={productContextReported ? data.nodes : undefined}
        />
          <RollupBar counts={counts} total={segmented} />
        </div>
      </details>
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
            <p className="font-medium text-gray-900 dark:text-gray-100">{boltCount > 0 ? 'Each BOLT contains its build, deployment and evidence stages.' : 'Read each wave from top to bottom.'}</p>
            <p>Parallel paths can progress together. Each step starts after its own dependencies pass, subject to approvals and worker availability.</p>
            <p className="text-xs">{boltCount > 0 ? 'Open a BOLT to see its phases. Dependency links still determine when each step can run.' : 'Wave cards group the plan. Their dependency links determine execution order.'}</p>
          </div>
          <div className="flex shrink-0 gap-3 text-sm">
            <button type="button" onClick={() => setAllExpanded(true)} className="text-blue-700 underline dark:text-blue-300">{boltCount > 0 ? 'Expand all groups' : 'Expand all waves'}</button>
            <button type="button" onClick={() => setAllExpanded(false)} className="text-blue-700 underline dark:text-blue-300">{boltCount > 0 ? 'Collapse all groups' : 'Collapse all waves'}</button>
          </div>
        </div>
      )}

      {displayEpics.map((epic) => (
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
            const expanded = waveExpanded(epic.epicRef, wave);
            return (
              <WaveCard
                key={key}
                epicRef={epic.epicRef}
                wave={wave}
                graph={displayGraph}
                expanded={expanded}
                onToggle={() => setExpandedWaves((current) => ({ ...current, [key]: !expanded }))}
                renderExecution={renderExecution}
                renderActivityStatus={data.activity_included === false ? (node) => {
                  const status = activity.status(node);
                  return status === 'loading' ? <span data-testid={`activity-loading-${node.id}`}>Loading activity…</span>
                    : status === 'unavailable' ? <span role="status" data-testid={`activity-unavailable-${node.id}`}>Activity unavailable; delivery status still comes from the plan. <button type="button" className="underline" onClick={() => activity.retry(node)}>Retry activity</button></span>
                      : status === 'not_started' ? <span data-testid={`activity-not-started-${node.id}`}>Activity not started</span> : null;
                } : undefined}
              />
            );
          })}
        </section>
      ))}
      {historicalNodes.length > 0 && (
        <details key={`history-${flowId}`} onToggle={(event) => {
          if (event.currentTarget.open) setHistoryFlowId(flowId);
        }} className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
          <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-400">
            Superseded steps ({historicalNodes.length}) — history, excluded from the current plan
          </summary>
          {historyFlowId === flowId && <ul className="mt-3 space-y-2">
            {historicalNodes.map((node) => (
              <NodeChip key={node.id} node={node} execution={renderExecution?.(node)} />
            ))}
          </ul>}
        </details>
      )}
      </>}
      </section>
      <section id="flow-panel-1" role="tabpanel" aria-labelledby="flow-tab-1" hidden={activeTab !== 'Timeline'}>
        {(activeTab === 'Timeline' || visitedTabs[tabKey('Timeline')]) && <FlowTimeline key={`timeline-${flowId}`} flow={data} active={activeTab === 'Timeline'} onOpenNode={openNode} />}
      </section>
      <section id="flow-panel-2" role="tabpanel" aria-labelledby="flow-tab-2" hidden={activeTab !== 'Evidence'} className="space-y-3">
        {(activeTab === 'Evidence' || visitedTabs[tabKey('Evidence')]) && <>
          <h2 className="text-lg font-semibold">Delivery evidence</h2>
          <p className="text-sm text-gray-600 dark:text-gray-400">Execution results and acceptance are separate. A successful run can still need evidence or human review.</p>
          {executionFailed && <p role="alert">Execution evidence could not be loaded.</p>}
          <ul className="space-y-3">{evidenceNodes.map(node => <li key={node.id} className="rounded border p-3 text-sm">
            <button type="button" className="font-medium underline" onClick={() => openNode(node.id)}>{node.title}</button>
            <p>Acceptance: {node.state === 'passed' ? 'Accepted' : node.state === 'waived' ? 'Waived by owner approval' : 'Pending'}</p>
            {node.issue_url && <a className="mr-3 underline" href={node.issue_url} target="_blank" rel="noreferrer">Issue and evidence</a>}
            {node.bound_pull_request && <a className="underline" href={node.bound_pull_request.url} target="_blank" rel="noreferrer">Pull request #{node.bound_pull_request.pr_number}</a>}
            {renderExecution?.(node)}
          </li>)}</ul>
          {!evidenceNodes.length && <p>No linked delivery evidence is available yet.</p>}
        </>}
      </section>
      <section id="flow-panel-3" role="tabpanel" aria-labelledby="flow-tab-3" hidden={activeTab !== 'Settings'} className="space-y-4">
        {(activeTab === 'Settings' || visitedTabs[tabKey('Settings')]) && <>
          <h2 className="text-lg font-semibold">Execution settings</h2>
        <ExecutionWindowControl key={`window-${flowId}`} flowId={flowId!} window={data.execution_window} />
        {!data.archived_at && <FlowExecutionControl flowId={flowId!} paused={data.execution_paused} recoveryEnabled={data.recovery_enabled} />}
        <FlowArchiveControl flowId={flowId!} archived={Boolean(data.archived_at)} />
        <BudgetEnforcementControl key={`budget-${flowId}`} flowId={flowId} />

        </>}
      </section>
      <section id="flow-panel-4" role="tabpanel" aria-labelledby="flow-tab-4" hidden={activeTab !== 'Plan changes'}>
        {(activeTab === 'Plan changes' || visitedTabs[tabKey('Plan changes')]) && <AmendmentReviewPanel key={`amendment-${flowId}`} flow={data} />}
      </section>

    </div>
  );
}

export default GraphView;
