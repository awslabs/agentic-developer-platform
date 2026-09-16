import type { FlowGraph, GraphNode } from '@/types/orchestration';
import { nodeDependencies, type WaveGroup } from '@/utils/flowLayout';
import { countByDisplayState } from '@/utils/nodeState';
import { NodeChip } from './NodeChip';
import { GateControls } from './GateControls';

export function waveLabel(ref: string): string {
  return ref.replace(/^wave[-_](\d+)$/i, 'Wave $1');
}

interface WaveCardProps {
  epicRef: string;
  wave: WaveGroup;
  graph: FlowGraph;
  expanded: boolean;
  onToggle: () => void;
}

export function WaveCard({ epicRef, wave, graph, expanded, onToggle }: WaveCardProps) {
  const counts = countByDisplayState(wave.nodes);
  const stories = wave.nodes.filter((node) => node.kind === 'story');
  const gates = wave.nodes.filter((node) => node.kind === 'gate').length;
  const evaluations = wave.nodes.filter((node) => node.kind === 'eval').length;
  const done = stories.filter((node) => node.state === 'passed').length;
  const changed = wave.nodes.some((node) => node.state === 'rejected_at_gate');
  const status = changed ? 'Changes requested'
    : counts.stalled ? 'Needs attention'
      : counts.in_progress ? 'In progress'
        : counts.gate ? 'Waiting for review'
          : counts.complete === wave.nodes.length ? 'Complete'
            : counts.complete ? 'Partly complete' : 'Not started';
  const bodyId = `wave-body-${encodeURIComponent(epicRef)}-${encodeURIComponent(wave.waveRef)}`;
  const headingId = `${bodyId}-heading`;
  const renderNode = (node: GraphNode) => (
    <NodeChip
      key={node.id}
      node={node}
      dependencies={nodeDependencies(graph, node)}
      controls={<GateControls node={node} flowId={graph.flow_id} />}
    />
  );

  return (
    <section
      data-testid={`wave-${epicRef}-${wave.waveRef}`}
      data-container="wave"
      aria-labelledby={headingId}
      className="min-w-0 overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-900"
    >
      <h3 id={headingId}>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={onToggle}
          className="flex w-full items-start gap-3 p-4 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-blue-500 dark:hover:bg-gray-800"
        >
          <span aria-hidden="true" className="mt-0.5 text-gray-500">{expanded ? '▾' : '▸'}</span>
          <span className="min-w-0 flex-1 space-y-1">
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="text-base font-semibold text-gray-900 dark:text-gray-100">{waveLabel(wave.waveRef)}</span>
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {stories.length} {stories.length === 1 ? 'story' : 'stories'}
              </span>
              <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${changed || counts.stalled
                ? 'bg-orange-100 text-orange-900 dark:bg-orange-900/40 dark:text-orange-200'
                : counts.in_progress || counts.gate
                  ? 'bg-blue-100 text-blue-900 dark:bg-blue-900/40 dark:text-blue-200'
                  : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'}`}>
                {status}
              </span>
            </span>
            <span className="block text-xs font-normal text-gray-500 dark:text-gray-400">
              {done} of {stories.length} stories complete · {gates} approval {gates === 1 ? 'gate' : 'gates'} · {evaluations} {evaluations === 1 ? 'evaluation' : 'evaluations'}
            </span>
            <span className="block text-xs font-normal text-gray-600 dark:text-gray-400">
              {wave.dependsOn.length
                ? `Dependencies in ${wave.dependsOn.map((dependency) => `${dependency.epicRef === epicRef ? '' : dependency.epicRef + ' / '}${waveLabel(dependency.waveRef)}`).join(', ')}`
                : 'No dependencies on other waves'}
            </span>
          </span>
        </button>
      </h3>

      <div id={bodyId} hidden={!expanded} className="border-t border-gray-200 p-4 dark:border-gray-700">
        <ol className="space-y-5">
          {wave.stages.map((stage, index) => (
            <li key={stage[0].id} data-testid={`stage-${epicRef}-${wave.waveRef}-${index}`}>
              <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
                <span className="inline-flex h-6 w-6 items-center justify-center rounded-full bg-gray-100 font-semibold text-gray-700 dark:bg-gray-800 dark:text-gray-300">
                  {index + 1}
                </span>
                <span className="font-medium text-gray-700 dark:text-gray-300">Dependency group {index + 1}</span>
                {stage.length > 1 && (
                  <span className="rounded-full bg-blue-50 px-2 py-1 font-medium text-blue-800 dark:bg-blue-900/40 dark:text-blue-200">
                    Parallel paths · {stage.length} steps
                  </span>
                )}
                {index > 0 && <span className="text-gray-500 dark:text-gray-400">↓ After each step’s own dependencies pass</span>}
              </div>
              <ul
                className="grid items-start gap-3 border-l-2 border-gray-200 pl-3 dark:border-gray-700"
                style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 17rem), 1fr))' }}
              >
                {stage.map(renderNode)}
              </ul>
            </li>
          ))}
        </ol>
        {wave.unordered.length > 0 && (
          <div className="space-y-3" role="alert">
            <p className="text-sm text-amber-800 dark:text-amber-200">
              Dependency order could not be determined for these steps. Check the plan for missing or circular dependencies.
            </p>
            <ul className="space-y-3">{wave.unordered.map(renderNode)}</ul>
          </div>
        )}
      </div>
    </section>
  );
}
