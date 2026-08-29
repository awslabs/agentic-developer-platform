/**
 * The delivery-journey graph — intent → done, on one page, updating live.
 * Issue #4212 (EPIC #4191, intent #4120).
 *
 * The four things this view exists to answer, and where each is:
 *
 *  - **How much is left** (AC-1) — the rollup bar spans the *whole* journey,
 *    pending nodes included, and queued chips render as first-class cards. A view
 *    of only what already ran always looks nearly finished.
 *  - **What runs in parallel** (AC-2) — waves with independent branches render as
 *    side-by-side columns, computed from the edges in `flowLayout`.
 *  - **Where we are, and what is stuck** (AC-3) — the current node is ringed with
 *    `aria-current`; stalled and halted carry distinct reason badges rather than
 *    collapsing into `failed`.
 *  - **What it cost** (AC-4/AC-22) — per node and rolled up, three-valued, with
 *    the scope label on every figure. `unknown` never renders `$0.00`.
 *
 * **Containers group, they never execute** (§8.2). EPICs are `<section>`s and waves
 * are column headers — neither is a node, neither has a state fill, and neither is
 * clickable. Rendering a container as an executable vertex invites an operator to
 * ask why "wave-2" is not running.
 *
 * **Layout is CSS Grid and semantic HTML** (§9): no graph library, no new runtime
 * dependency. The structure is genuinely a nested list, and a real list is what
 * makes it navigable by screen reader.
 */

import { useParams } from 'react-router-dom';
import { useFlowGraph } from '@/hooks/useFlowGraph';
import { countByDisplayState } from '@/utils/nodeState';
import { groupIntoEpics, blockingPredecessors } from '@/utils/flowLayout';
import { RollupBar } from '@/components/orchestration/RollupBar';
import { NodeChip } from '@/components/orchestration/NodeChip';
import { GateControls } from '@/components/orchestration/GateControls';
import { CostFigureDisplay } from '@/components/orchestration/CostFigureDisplay';
import { LastUpdated } from '@/components/LastUpdated';
import { Alert, Spinner } from '@/components/ui';

export function GraphView() {
  const { flowId } = useParams<{ flowId: string }>();
  const { data, isPending, isError, error, dataUpdatedAt, isFetching } = useFlowGraph(flowId);

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
  const epics = groupIntoEpics(data);

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

        <RollupBar counts={counts} total={segmented} />
      </header>

      {data.nodes.length === 0 && (
        <p className="text-sm text-gray-600 dark:text-gray-400" data-testid="graph-empty">
          This flow has no planned work yet.
        </p>
      )}

      {epics.map((epic) => (
        // A grouping container, not a vertex: no state fill, not interactive.
        <section
          key={epic.epicRef}
          data-testid={`epic-${epic.epicRef}`}
          data-container="epic"
          aria-labelledby={`epic-heading-${epic.epicRef}`}
          className="rounded-lg border border-gray-200 p-4 dark:border-gray-700"
        >
          <h2
            id={`epic-heading-${epic.epicRef}`}
            className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400"
          >
            {epic.epicRef}
          </h2>

          <div className="flex gap-4 overflow-x-auto pb-2">
            {epic.waves.map((wave) => (
              <div
                key={wave.waveRef}
                data-testid={`wave-${epic.epicRef}-${wave.waveRef}`}
                data-container="wave"
                data-branch-count={wave.branches.length}
                className="min-w-[16rem] flex-1"
              >
                <h3 className="mb-2 text-xs font-medium text-gray-500 dark:text-gray-400">{wave.waveRef}</h3>

                {/* One column per independent branch (AC-2). A wave whose nodes
                    are chained by an edge collapses to a single column, because
                    they are sequential and drawing them side by side would claim
                    concurrency the engine will not deliver. */}
                <div
                  className="grid gap-3"
                  style={{ gridTemplateColumns: `repeat(${wave.branches.length}, minmax(0, 1fr))` }}
                >
                  {wave.branches.map((branch, branchIndex) => (
                    <ul
                      key={branch[0]?.id ?? branchIndex}
                      data-testid={`branch-${epic.epicRef}-${wave.waveRef}-${branchIndex}`}
                      className="space-y-2"
                    >
                      {branch.map((node) => (
                        <NodeChip
                          key={node.id}
                          node={node}
                          blockedBy={blockingPredecessors(data, node)}
                          controls={flowId ? <GateControls node={node} flowId={flowId} /> : undefined}
                        />
                      ))}
                    </ul>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

export default GraphView;
