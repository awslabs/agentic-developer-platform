import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@/components/ui';
import { getMonthlySpend } from '@/services/budgetOverview';
import { getMySelection } from '@/services/bedrockRoutingSelf';
import { getMyBudget } from '@/services/budgetSpend';
import { usePersonCap } from '@/hooks/usePersonCap';
import { formatWireMoney, parseWireMoney } from '@/utils/cost';
import { BudgetLines } from './BudgetLines';
import { PerOrgSpend } from './PerOrgSpend';
import type { BudgetPeriodType } from '@/types/budget';

const muted = 'text-sm text-gray-500 dark:text-gray-400';
const dateLabel = (value: string) => new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, { day: 'numeric', month: 'long', timeZone: 'UTC' });

export function BedrockBillingAccount() {
  const { data, isPending, error } = useQuery({ queryKey: ['myBedrockSelection'], queryFn: getMySelection });
  const effective = data?.effective;
  return <Card>
    <p className="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">Bedrock routing · AWS account</p>
    {isPending ? <p role="status">Loading account…</p> : error || !effective ? <p role="status">Account details unavailable</p> : <>
      <p className="mt-2 text-lg font-semibold">{effective.destination_label || (effective.rung === 'platform' ? 'Platform account' : 'AWS account')}</p>
      <p className="mt-1 text-sm">Account ID: <span className="font-mono">{effective.account_id || 'Unavailable'}</span></p>
      <p className={`mt-1 ${muted}`}>Routing source: {({ user: 'Individual', team: 'Team default', org: 'Organization default', platform: 'Platform default', self: 'Your selection' } as Record<string, string>)[effective.rung] || effective.rung}</p>
    </>}
    <p className={`mt-2 ${muted}`}>Your personal calls and cloud agents follow your routing: Individual → Team → Organization → Platform default. Historical spend may include other accounts.</p>
  </Card>;
}

function AdditionalPeriodLimits({ period }: { period: BudgetPeriodType }) {
  const { data, error } = useQuery({ queryKey: ['myBudget', period], queryFn: () => getMyBudget(period) });
  const cap = usePersonCap(period);
  return <div className="space-y-3">
    <h3 className="font-medium capitalize">{period} restrictions</h3>
    {error || cap.error ? <p role="status">Some restrictions are unavailable.</p> : null}
    {period !== 'monthly' && cap.data?.cap_status === 'capped' && <p>{formatWireMoney(cap.data.cap_usd)} personal budget · {cap.data.enforcement_mode === 'hard' ? 'Enforced' : 'Advisory'}</p>}
    {data?.binding && <p className={muted}>Applicable {data.binding.label || data.binding.entity_type} budget: {formatWireMoney(data.binding.remaining_usd)} remaining · {data.binding.enforcement_mode}</p>}
    {data && <BudgetLines lines={data.lines} />}
    {!!data?.per_org?.length && <PerOrgSpend perOrg={data.per_org} identityStatus={data.identity_status} personCapStatus={cap.data?.cap_status} personCapEnforcing={cap.data?.enforcement_mode === 'hard'} />}
  </div>;
}

export function MonthlySpendView() {
  const spend = useQuery({ queryKey: ['monthlySpend'], queryFn: getMonthlySpend, refetchInterval: 30_000 });
  const cap = usePersonCap('monthly');
  const envelope = useQuery({ queryKey: ['myBudget', 'monthly'], queryFn: () => getMyBudget('monthly'), refetchInterval: 30_000 });
  const [restrictionsOpen, setRestrictionsOpen] = useState(false);
  const total = parseWireMoney(spend.data?.totals.total_usd);
  const budget = parseWireMoney(cap.data?.cap_usd);
  const remaining = total != null && budget != null ? budget - total : null;
  const percent = total != null && budget != null && budget > 0 && cap.data?.enforcement_mode === 'hard' ? total / budget * 100 : null;
  return <div className="space-y-5">
    <BedrockBillingAccount />
    <Card>
      <p className={muted}>{spend.data ? new Date(`${spend.data.month}T00:00:00Z`).toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' }) : 'This month'} · USD</p>
      <div className="mt-3 flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="text-3xl font-semibold tabular-nums">{spend.isPending ? 'Loading spend…' : total == null || spend.error ? 'Spend unavailable' : `${formatWireMoney(spend.data?.totals.total_usd)} spent`}</h2>
        <p className="text-lg text-gray-500 dark:text-gray-400">{cap.isPending ? 'Loading budget…' : cap.error || !cap.data ? 'Budget unavailable' : cap.data.cap_status === 'uncapped' ? 'No monthly budget set' : `of ${formatWireMoney(cap.data.cap_usd)} this month`}</p>
      </div>
      {percent != null && !spend.error && <div role="progressbar" aria-label="Monthly budget used" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, percent)} aria-valuetext={`${percent.toFixed(1)}% of monthly budget used`} className="h-2 mt-5 rounded-full bg-gray-100 dark:bg-gray-700 overflow-hidden"><div className="h-full bg-primary-500" style={{ width: `${Math.min(100, percent)}%` }} /></div>}
      <div className="flex justify-between flex-wrap gap-2 mt-3 text-sm">
        {remaining != null && !spend.error && <p>{formatWireMoney(String(Math.abs(remaining)))} {remaining < 0 ? 'over budget' : 'remaining'}</p>}
        {spend.data && <p className={muted}>Resets {dateLabel(spend.data.resets_at)} · UTC</p>}
      </div>
      <dl className="grid grid-cols-2 gap-4 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
        <div><dt className={muted}>Direct usage</dt><dd className="mt-1 text-xl tabular-nums">{formatWireMoney(spend.error ? null : spend.data?.totals.direct_usd)}</dd></div>
        <div><dt className={muted}>Cloud agents</dt><dd className="mt-1 text-xl tabular-nums">{formatWireMoney(spend.error ? null : spend.data?.totals.cloud_usd)}</dd></div>
      </dl>
      <p className={`mt-4 ${muted}`}>Model usage across your workspaces. Managed by your platform admin.</p>
      {cap.data?.enforcement_mode && cap.data.enforcement_mode !== 'hard' && <p className="mt-2 text-sm text-amber-700 dark:text-amber-300">This monthly budget is advisory.</p>}
      {envelope.data?.binding?.enforcement_mode === 'shadow' && <p className="mt-2 text-sm">Workspace restrictions are currently advisory (shadow mode).</p>}
      <p className={`mt-2 ${muted}`}>{envelope.data?.freshness?.cost_backfill_lag ? 'Recent usage is still being priced.' : 'Settled usage; recent requests may still be awaiting pricing.'}</p>
      {spend.error && <button className="mt-2 underline" onClick={() => void spend.refetch()}>Retry spend</button>}
      {cap.error && <button className="mt-2 ml-3 underline" onClick={() => void cap.refetch()}>Retry budget</button>}
    </Card>
    <Card><details><summary className="cursor-pointer font-medium">Usage breakdown</summary>
      <p className={`mt-2 ${muted}`}>Daily spend for this month · UTC. Today is in progress.</p>
      {spend.isPending ? <p role="status">Loading daily spend…</p> : !spend.data || spend.error ? <p role="status">Daily spend unavailable</p> : !spend.data.daily_complete ? <p role="status" className="mt-3">Daily spend is not yet reconciled with the monthly total. The monthly summary includes all settled usage.</p> : <div className="overflow-x-auto mt-4"><table className="w-full text-sm tabular-nums">
        <thead><tr className="border-b border-gray-200 dark:border-gray-700">{['Date', 'Direct usage', 'Cloud agents', 'Total'].map((h, i) => <th scope="col" key={h} className={`py-3 px-2 whitespace-nowrap ${i ? 'text-right' : 'text-left'}`}>{h}</th>)}</tr></thead>
        <tbody>{spend.data.days.map(day => <tr key={day.date} className="border-b border-gray-100 dark:border-gray-700"><th scope="row" className="text-left font-normal py-3 px-2 whitespace-nowrap">{dateLabel(day.date)}{day.in_progress && <span className="ml-2 text-xs text-gray-500">In progress</span>}</th>{[day.direct_usd, day.cloud_usd, day.total_usd].map((value, i) => <td key={i} className="text-right py-3 px-2">{formatWireMoney(value)}</td>)}</tr>)}</tbody>
        <tfoot><tr><th scope="row" className="text-left py-3 px-2">Month to date</th>{[spend.data.totals.direct_usd, spend.data.totals.cloud_usd, spend.data.totals.total_usd].map((value, i) => <td key={i} className="text-right font-semibold py-3 px-2">{formatWireMoney(value)}</td>)}</tr></tfoot>
      </table></div>}
    </details></Card>
    <Card><details><summary className="cursor-pointer font-medium">Budget details</summary><p className="mt-3 text-sm">{cap.error ? 'Budget source unavailable' : cap.data?.source ? ({ own: 'Individual budget', admin: 'Individual budget', team_default: 'Team default', org_default: 'Organization default', platform_default: 'Platform default' }[cap.data.source]) : cap.data?.cap_status === 'uncapped' ? 'No budget set' : 'Loading budget source…'}</p>
      <p className={`mt-2 ${muted}`}>Individual → Team → Organization → Platform default. The closest configured budget applies. With several memberships at the same level, the lowest budget applies.</p>
      <details className="mt-4" onToggle={e => setRestrictionsOpen(e.currentTarget.open)}><summary className="cursor-pointer text-sm font-medium">Additional restrictions</summary><p className={`my-3 ${muted}`}>Existing daily, weekly and workspace controls can also restrict requests while your monthly budget has money remaining.</p>
        {restrictionsOpen && <div className="space-y-5">{(['monthly', 'weekly', 'daily'] as const).map(period => <AdditionalPeriodLimits key={period} period={period} />)}</div>}
      </details>
    </details></Card>
  </div>;
}
