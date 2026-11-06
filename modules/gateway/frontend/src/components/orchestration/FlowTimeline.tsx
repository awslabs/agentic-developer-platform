import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useFlowTimeline } from '@/hooks/useFlowTimeline';
import type { FlowGraph } from '@/types/orchestration';
import { durationLabel, timelineState, timingRange } from '@/utils/flowTimeline';
import { CostFigureDisplay } from './CostFigureDisplay';

const date = (value: string | number) => new Date(value).toLocaleString();

export function FlowTimeline({ flow, active, onOpenNode }: { flow: FlowGraph; active: boolean; onOpenNode: (id: string) => void }) {
  const [offset, setOffset] = useState(0);
  const [order, setOrder] = useState('plan');
  const [openedHistory, setOpenedHistory] = useState<Record<string, boolean>>({});
  const query = useFlowTimeline(flow.flow_id, offset, active);
  const data = query.data?.flow_id === flow.flow_id ? query.data : undefined;
  const byId = new Map(flow.nodes.map(node => [node.id, node]));
  const rows = (data?.nodes ?? []).map(timing => ({ timing, node: byId.get(timing.node_id), range: timingRange(timing, data!.server_time) }));
  if (order === 'plan') {
    const position = new Map(flow.nodes.map((node, index) => [node.id, index]));
    rows.sort((a, b) => (position.get(a.timing.node_id) ?? Infinity) - (position.get(b.timing.node_id) ?? Infinity));
  }
  if (order === 'duration') rows.sort((a, b) => (b.range?.duration ?? -1) - (a.range?.duration ?? -1));
  if (order === 'attempts') rows.sort((a, b) => b.timing.attempts - a.timing.attempts);
  const ranges = rows.flatMap(row => row.range ? [row.range] : []);
  const first = Math.min(...ranges.map(range => range.start));
  const last = Math.max(...ranges.map(range => range.end));
  const span = Math.max(1, last - first);
  return <section aria-label="Story timeline" className="min-w-0 space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="max-w-3xl space-y-1">
        <h2 className="text-lg font-semibold">Story timeline</h2>
        <p className="text-sm text-gray-600 dark:text-gray-400">Elapsed time includes queues, reviews and retries.</p>
        <details className="text-xs text-gray-500 dark:text-gray-400"><summary className="cursor-pointer py-1">How timings, attempts and costs are measured</summary><p className="mt-1">Elapsed wall time runs from first recorded start to acceptance or stop. This is not active agent time. Attempts count dispatches for each step; review and repair runs may occur within an attempt. Agent spend covers the whole step. Build and delivery phases appear separately when recorded as separate steps.</p></details>
      </div>
      <label className="text-sm">Order on this page
        <select value={order} onChange={event => setOrder(event.target.value)} className="ml-2 rounded border bg-transparent p-2">
          <option value="plan">Plan order</option><option value="duration">Longest elapsed</option><option value="attempts">Most attempts</option>
        </select>
      </label>
    </div>
    {query.isPending && <p role="status">Loading story timings…</p>}
    {query.isError && <div role="alert" className="rounded border border-amber-400 p-3">
      <p>Story timings could not be loaded. The delivery plan is still available.</p>
      <button type="button" className="mt-2 underline" onClick={() => void query.refetch()}>Retry timeline</button>
    </div>}
    {data && <>
      <p className="text-xs text-gray-500">Updated {date(data.server_time)} · {data.total} story steps</p>
      {ranges.length > 0 && <div className="flex justify-between gap-4 text-xs text-gray-600 dark:text-gray-400" aria-label="Timeline scale">
        <span>{date(first)}</span><span>{date(last)}</span>
      </div>}
      {rows.length === 0 && <p>{data.total === 0 ? 'No story steps in this plan yet.' : <>No story steps on this page. <button type="button" className="underline" onClick={() => setOffset(0)}>Return to first page</button></>}</p>}
      <ol className="space-y-3">
        {rows.map(({ timing, node, range }) => <li key={timing.node_id} data-testid={`timeline-${timing.node_id}`}
          className="min-w-0 rounded-lg border border-gray-200 p-4 [overflow-wrap:anywhere] dark:border-gray-700">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
              <button type="button" className="w-full text-left font-medium text-primary-700 underline dark:text-primary-300" onClick={() => onOpenNode(timing.node_id)}>{node?.title ?? 'Story no longer in the current plan'}</button>
              <p className="text-xs text-gray-500">{node?.wave_ref} · {timelineState(timing.state)}</p>
            </div>
            <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
              <span>{range ? `${durationLabel(range.duration)}${range.ongoing ? ' so far' : ''}` : 'Timing unavailable'}</span>
              <span>{timing.attempts} {timing.attempts === 1 ? 'attempt' : 'attempts'}</span>
              {node && <CostFigureDisplay figure={node.cost} label="Agent spend" />}
            </div>
          </div>
          {range ? <>
            <div className="relative mt-3 h-3 rounded bg-gray-100 dark:bg-gray-800" dir="ltr" aria-hidden="true">
              <div className={`absolute h-3 min-w-px rounded ${timing.state === 'passed' ? 'bg-green-600' : range.ongoing ? 'bg-blue-500' : 'bg-amber-500'}`}
                style={{ left: `${(range.start - first) / span * 100}%`, width: `${(range.end - range.start) / span * 100}%` }} />
            </div>
            <p className="mt-1 text-xs text-gray-500">Started {date(range.start)} · {range.ongoing ? 'Observed through' : timing.state === 'passed' ? 'Accepted' : 'Stopped'} {date(range.end)}</p>
          </> : <p className="mt-2 text-xs text-gray-500">{timing.attempts === 0 && !timing.started_at ? 'No recorded start.' : 'The recorded history does not establish both timing boundaries.'}</p>}
          <details className="mt-3 text-xs" onToggle={event => {
            if (event.currentTarget.open) setOpenedHistory(current => current[timing.node_id] ? current : { ...current, [timing.node_id]: true });
          }}>
            <summary className="cursor-pointer py-1">Recorded progress ({timing.transitions.length}{timing.transitions_truncated ? ' most recent' : ''} transitions)</summary>
            {timing.transitions_truncated && <p className="mt-2">Earlier transitions are omitted. The start time still uses the full recorded history.</p>}
            {openedHistory[timing.node_id] && <ol className="mt-2 space-y-1 border-l pl-3">{timing.transitions.map((event, index) => {
              const next = timing.transitions[index + 1];
              const elapsed = next ? Date.parse(next.at) - Date.parse(event.at) : null;
              return <li key={event.id}><time dateTime={event.at}>{date(event.at)}</time> · {timelineState(event.to_state)}{elapsed !== null && elapsed >= 0 ? ` · ${durationLabel(elapsed)} until next transition` : ''}</li>;
            })}</ol>}
            {node?.run_id && <Link className="mt-2 inline-block underline" to={`/activity?id=${encodeURIComponent(node.run_id)}`}>Inspect latest run</Link>}
          </details>
        </li>)}
      </ol>
      {data.total > data.limit && <nav className="flex items-center justify-between gap-3 text-sm" aria-label="Timeline pagination">
        <button type="button" className="rounded border p-2 disabled:opacity-50" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - data.limit))}>Previous stories</button>
        <span>{offset + 1}–{offset + rows.length} of {data.total}</span>
        <button type="button" className="rounded border p-2 disabled:opacity-50" disabled={offset + rows.length >= data.total} onClick={() => setOffset(offset + data.limit)}>Next stories</button>
      </nav>}
    </>}
  </section>;
}
