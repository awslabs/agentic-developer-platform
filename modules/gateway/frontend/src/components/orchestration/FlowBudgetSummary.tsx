import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { getFlowBudget, getNodeBudget } from '@/services/flowBudget';
import { formatWireMoney, parseWireMoney } from '@/utils/cost';

const money = (value: string | null) => parseWireMoney(value) === null ? 'Unavailable' : formatWireMoney(value);
const days = (seconds: number | null) => seconds === null ? 'Unavailable' : `${Math.round((seconds / 86400) * 10) / 10} days`;
const fields: { key: 'limit_usd' | 'spent_usd' | 'reserved_usd' | 'remaining_usd'; label: string }[] = [
  { key: 'limit_usd', label: 'Flow budget' },
  { key: 'spent_usd', label: 'Recorded spend' },
  { key: 'reserved_usd', label: 'Reserved and held' },
  { key: 'remaining_usd', label: 'Remaining' },
];

export function FlowBudgetSummary({ flowId, active }: { flowId: string; active: boolean }) {
  const { user } = useAuth();
  const query = useQuery({
    queryKey: ['orchestration', 'flow-budget', user?.orgId, flowId],
    queryFn: ({ signal }) => getFlowBudget(flowId, signal),
    enabled: active && !!user?.orgId,
    staleTime: 30_000,
    refetchInterval: active ? 60_000 : false,
    retry: false,
  });
  const data = query.data?.flow_id === flowId ? query.data : undefined;
  const proposed = data?.policy_status === 'proposed' ? data.proposed : null;
  const recoveryHold = parseWireMoney(data?.balance.recovery_hold_usd ?? null);
  return <section aria-label="Delivery flow budget" className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-semibold text-gray-900 dark:text-white">Budget</h2>
      {data && !query.isError && (proposed
        ? <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">Proposed · awaiting acceptance</span>
        : <span className="text-xs text-gray-600 dark:text-gray-300">Enforcement {data.enforcement_enabled ? 'on' : 'off'}</span>)}
    </div>
    {query.isError ? <p role="status" className="mt-2 text-sm">Budget information is unavailable. <button type="button" className="underline" onClick={() => void query.refetch()}>Retry budget</button></p>
      : !data ? <p className="mt-2 text-sm text-gray-500">Loading budget…</p> : proposed ? <>
        <p className="mt-2 text-sm text-gray-700 dark:text-gray-200">No budget is in force. Accepting plan v{proposed.plan_version} grants these limits; nothing can spend until then.</p>
        <dl aria-label="Proposed budget" className="mt-3 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <div><dt className="text-xs text-gray-500 dark:text-gray-400">Proposed flow budget</dt><dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-white">{money(proposed.limit_usd)}</dd></div>
          <div><dt className="text-xs text-gray-500 dark:text-gray-400">Concurrent nodes</dt><dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-white">{proposed.concurrency ?? 'Unavailable'}</dd></div>
          <div><dt className="text-xs text-gray-500 dark:text-gray-400">Attempts per node</dt><dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-white">{proposed.attempts_per_node ?? 'Unavailable'}</dd></div>
          <div><dt className="text-xs text-gray-500 dark:text-gray-400">Wall clock</dt><dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-white">{days(proposed.wall_clock_seconds)}</dd></div>
        </dl>
        <p className="mt-3 text-sm text-gray-700 dark:text-gray-200">
          Recorded spend so far: <strong>{money(data.balance.spent_usd)}</strong>
          {proposed.expires_at && <> · Authority expires <time dateTime={proposed.expires_at}>{new Date(proposed.expires_at).toLocaleDateString()}</time></>}
        </p>
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Plan hash <span className="font-mono">{proposed.plan_hash.slice(0, 12)}</span>. Quote the full hash when accepting; a different hash means the plan changed since this view.</p>
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Updated <time dateTime={data.observed_at}>{new Date(data.observed_at).toLocaleTimeString()}</time></p>
      </> : <>
        <dl className="mt-3 grid grid-cols-2 gap-4 lg:grid-cols-4">
          {fields.map(({ key, label }) => <div key={key}>
            <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-white">{money(data.balance[key])}</dd>
          </div>)}
        </dl>
        <p className="mt-3 text-sm text-gray-700 dark:text-gray-200">
          Per-run limit: <strong>{money(data.run_limit_usd)}</strong>
          {data.concurrency !== null && <> · Concurrent nodes: <strong>{data.concurrency}</strong></>}
        </p>
        {data.balance.request_reserved_usd != null && data.balance.recovery_hold_usd != null && <dl aria-label="Reservation breakdown" className="mt-3 flex flex-wrap gap-x-6 gap-y-2 text-sm">
          <div><dt>Unsettled model requests</dt><dd className="font-semibold tabular-nums">{money(data.balance.request_reserved_usd)}</dd></div>
          <div><dt>Historical recovery hold</dt><dd className="font-semibold tabular-nums">{money(data.balance.recovery_hold_usd)}</dd></div>
        </dl>}
        {recoveryHold !== null && recoveryHold > 0 && <p role="status" className="mt-2 text-sm text-amber-800 dark:text-amber-200">Historical usage could not be fully reconstructed during recovery. This hold is not recorded spend or active work. It reduces the available budget until reconciled from evidence or explicitly resolved by a budget owner.</p>}
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Model usage only; excludes build and infrastructure costs. Reserved and held amounts include unsettled requests and any historical recovery holds. Interrupted requests may remain reserved until their usage is verified. Remaining excludes the next worker’s reservation.</p>
        {(data.accounting_incomplete || data.balance.remaining_usd === null) && <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">Remaining is unavailable until the budget and accounting records can be verified.</p>}
        {!data.enforcement_enabled && <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">Limits are configured but are not currently enforced. Usage continues to be recorded.</p>}
        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">Updated <time dateTime={data.observed_at}>{new Date(data.observed_at).toLocaleTimeString()}</time></p>
      </>}
  </section>;
}

export function StoryRunBudgets({ flowId, nodeId, active }: { flowId: string; nodeId: string; active: boolean }) {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: ['orchestration', 'node-budget', user?.orgId, flowId, nodeId],
    queryFn: ({ signal }) => getNodeBudget(flowId, nodeId, signal),
    enabled: active && open && !!user?.orgId,
    staleTime: 30_000,
    refetchInterval: active && open ? 60_000 : false,
    retry: false,
  });
  const data = query.data?.flow_id === flowId && query.data.node_id === nodeId ? query.data : undefined;
  return <details className="mt-3 rounded border border-gray-200 p-2 text-xs dark:border-gray-700" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer py-1 font-medium">Run budgets</summary>
    {open && (query.isError ? <p role="status">Run budgets are unavailable. <button type="button" className="underline" onClick={() => void query.refetch()}>Retry run budgets</button></p>
      : !data ? <p>Loading run budgets…</p> : <>
        {!data.runs.length ? <p>Runs appear here after their first recorded model call.</p> : <>
          <p className="my-2 text-gray-500 dark:text-gray-400">Limits reflect current flow settings. Each run’s recorded spend is retained across retries.</p>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <caption className="sr-only">Budget by agent run</caption>
              <thead><tr>{['Run', 'Limit', 'Spent', 'Reserved', 'Remaining'].map(label => <th key={label} scope="col" className="whitespace-nowrap p-2 font-medium">{label}</th>)}</tr></thead>
              <tbody>{data.runs.map(run => <tr key={run.run_id} className="border-t border-gray-100 dark:border-gray-700">
                <th scope="row" className="whitespace-nowrap p-2 font-normal"><Link className="underline" to={`/activity?id=${encodeURIComponent(run.run_id)}`}>
                  {run.persona?.replace(/^agent-/, '') ?? 'Agent run'} <span className="block font-mono">{run.run_id.slice(0, 8)}</span>
                </Link></th>
                {fields.map(({ key }) => <td key={key} className="whitespace-nowrap p-2 tabular-nums">{money(run.balance[key])}</td>)}
              </tr>)}</tbody>
            </table>
          </div>
          {data.runs.some(run => run.balance.remaining_usd === null) && <p className="mt-2 text-gray-500 dark:text-gray-400">Expired or unavailable counters have no remaining balance shown.</p>}
          {data.truncated && <p className="mt-2">Showing the 20 most recently active runs for this story.</p>}
        </>}
        {!data.enforcement_enabled && <p className="mt-2">Budget enforcement is off for this flow.</p>}
      </>)}
  </details>;
}
