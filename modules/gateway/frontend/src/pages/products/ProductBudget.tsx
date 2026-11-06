import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { workspacePath } from './productWorkspaceNavigation';
import { Button } from '@/components/ui';
import { useAuth } from '@/hooks/useAuth';
import { getProjectBudget, setProjectBudget } from '@/services/products';

const dollars = (value: string | number | null) => value === null ? 'Unavailable' : Number(value).toLocaleString('en-US', { style: 'currency', currency: 'USD' });

export default function ProductBudget({ productId, projectId }: { productId: string; projectId: string }) {
  const { user } = useAuth();
  const client = useQueryClient();
  const queryKey = ['project-budget', user?.orgId, user?.id, projectId];
  const budget = useQuery({ queryKey, queryFn: () => getProjectBudget(projectId), staleTime: 0, refetchInterval: 30_000 });
  const [draft, setDraft] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (amount: string) => setProjectBudget(projectId, amount),
    onSuccess: value => { client.setQueryData(queryKey, value); setDraft(null); },
  });
  if (budget.isPending) return <p role="status">Loading Project budget…</p>;
  if (budget.isError || !budget.data || budget.data.product_id !== productId || budget.data.project_id !== projectId) return <section className="blueprint-card product-card"><p role="alert">Could not load this Project’s budget.</p><Button onClick={() => void budget.refetch()}>Retry</Button></section>;
  const data = budget.data;
  const value = draft ?? (data.budget_usd === null ? '' : String(data.budget_usd));
  const selected = new URLSearchParams({ product: productId, project: projectId }).toString();
  return <section aria-label="Project budget and spend" className="blueprint-card product-card space-y-5">
    <div><h2>{data.project_name}{data.is_base ? ' · Base Project' : ''}</h2>
      <p className="product-muted">Flows and agents share this Project’s total AI usage budget. Each metered request reserves funds before it starts; work stops when the remaining budget cannot cover it. Per-run and other spending limits still apply. {data.is_base && 'New Product work uses this Project by default.'}</p></div>
    <dl className="grid grid-cols-2 gap-5 md:grid-cols-4">
      {[
        ['Budget', data.budget_usd === null ? 'No limit set' : dollars(data.budget_usd)],
        ['Spent', dollars(data.spent_usd)], ['Reserved for running work', dollars(data.reserved_usd)],
        ['Available', data.budget_usd === null && data.accounting_status !== 'unavailable' ? 'No limit set' : dollars(data.available_usd)],
      ].map(([label, amount]) => <div key={label}><dt className="product-muted">{label}</dt><dd className="mt-1 text-xl font-semibold">{amount}</dd></div>)}
    </dl>
    {data.accounting_status === 'unavailable' && <p role="alert">Spend is temporarily unavailable. Refresh to check the latest balance.</p>}
    {data.accounting_status === 'pending' && <p role="status">Some usage is awaiting confirmation. Its reserved amount remains included.</p>}
    {data.can_manage && <form className="flex flex-wrap items-end gap-3" onSubmit={event => { event.preventDefault(); save.mutate(value); }}>
      <label>Total Project budget (USD)<input type="number" aria-label="Total Project budget (USD)" min="0.01" max="99999999.99" step="0.01" required value={value} disabled={save.isPending}
        onChange={event => setDraft(event.target.value)} className="mt-1 block" placeholder="e.g. 500" /></label>
      <Button type="submit" disabled={save.isPending || !value || Number(value) <= 0 || (data.budget_usd !== null && Number(value) === Number(data.budget_usd))}>{save.isPending ? 'Saving…' : 'Save budget'}</Button>
      {save.isError && <p role="alert">Could not save the budget. Your entry is retained; retry to confirm it.</p>}
      {save.isSuccess && !save.isPending && draft === null && <p role="status">Budget saved.</p>}
    </form>}
    {!data.can_manage && <p className="product-muted">The product owner or a platform administrator can set this budget.</p>}
    <div className="flex flex-wrap gap-4"><Link className="underline" to={workspacePath(productId, projectId, 'build')}>View project delivery flows</Link><Link className="underline" to={`${workspacePath(productId, projectId, 'build')}/activity`}>View project agent activity</Link></div>
    <div className="flex flex-wrap gap-4"><Link className="underline" to={`/flows/new?${selected}`}>Create a flow</Link><Link className="underline" to={`/chat?${selected}`}>Start an agent conversation</Link></div>
    <div><h3 className="font-semibold">Spend by flow</h3>
      {data.flows.length ? <ul className="mt-3 space-y-3">{data.flows.map(flow => <li key={flow.flow_id ?? 'standalone'} className="flex flex-wrap justify-between gap-2">
        <span>{flow.flow_id ? <Link className="underline" to={`/flows/${flow.flow_id}`}>{flow.flow_name ?? 'Delivery flow'}</Link> : 'Planning and standalone agents'} <span className="product-muted">· {flow.run_count} runs</span></span><span>{dollars(flow.spent_usd)}</span>
      </li>)}</ul> : <p className="product-muted mt-2">No metered usage recorded for this Project yet.</p>}
    </div>
    <p className="product-muted text-sm">Includes AI usage attributed to this Project at execution time. Cloud infrastructure costs are separate.</p>
  </section>;
}
