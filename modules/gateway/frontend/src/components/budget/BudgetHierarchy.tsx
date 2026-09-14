import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card, Button } from '@/components/ui';
import { Modal } from '@/components/ui/Modal';
import { getBudgetHierarchy, getPeopleSpend, type BudgetNode, type BudgetLimit } from '@/services/budgetOverview';
import { setPersonCapFor, deletePersonCapFor, setPersonDefault, deletePersonDefault } from '@/services/personCap';
import { formatWireMoney, parseWireMoney } from '@/utils/cost';

const amountLabel = (limit: BudgetLimit) => limit.amount_usd == null ? 'No budget set' : formatWireMoney(limit.amount_usd);
const buttonStyle = 'text-sm whitespace-nowrap text-primary-700 dark:text-primary-300 hover:underline';
const levelLabel = { platform: 'Platform', org: 'Organization', team: 'Team', user: 'User' };

function HierarchyNode({ node, path, onEdit }: { node: BudgetNode; path: string[]; onEdit: (node: BudgetNode, path: string[]) => void }) {
  // Component keys are stable across refetches, so edits preserve expansion state.
  const [expanded, setExpanded] = useState(node.kind === 'platform' || node.kind === 'org');
  const fullPath = [...path, node.name];
  const expandable = node.kind !== 'user';
  return <li>
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3 border-b border-gray-100 dark:border-gray-700">
      <div className="flex-1 min-w-36">
        {expandable ? <button className="font-medium text-left" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><span aria-hidden="true" className="inline-block w-5">{expanded ? '▾' : '▸'}</span>{node.name}</button> : <span className="font-medium">{node.name}</span>}
        <span className="block mt-0.5 text-xs text-gray-500 dark:text-gray-400">{levelLabel[node.kind]}</span>
      </div>
      <div className="text-right text-sm"><p className="font-medium tabular-nums">{amountLabel(node.effective)}</p><p className="text-xs text-gray-500 dark:text-gray-400">{node.configured_usd != null ? (node.effective.source === 'admin' || node.effective.source === 'own' || node.kind !== 'user' ? 'Set here' : 'Override is advisory; inherited budget applies') : node.effective.source_label ? `Inherited · ${node.effective.source_label}` : 'No inherited budget'}</p></div>
      <button className={buttonStyle} aria-label={`${node.configured_usd == null ? 'Set' : 'Edit'} budget for ${node.name}`} onClick={() => onEdit(node, fullPath)}>{node.configured_usd == null ? 'Set budget' : 'Edit budget'}</button>
    </div>
    {expandable && expanded && <ul className="ml-3 sm:ml-6 pl-3 sm:pl-4 border-l border-gray-200 dark:border-gray-700">{node.children.length ? node.children.map(child => <HierarchyNode key={child.key} node={child} path={fullPath} onEdit={onEdit} />) : <li className="py-3 text-sm text-gray-500">No members yet</li>}</ul>}
  </li>;
}

function BudgetEditor({ node, path, onClose }: { node: BudgetNode; path: string[]; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState(node.configured_usd || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function save(remove: boolean) {
    setSaving(true); setError('');
    try {
      if (node.kind === 'user') {
        if (!node.person_anchor) throw new Error('This person could not be resolved. Refresh the hierarchy.');
        if (remove) await deletePersonCapFor(node.person_anchor, 'monthly');
        else await setPersonCapFor(node.person_anchor, 'monthly', value);
      } else {
        const scope = { scope_type: node.kind, org: node.org_id || undefined, team: node.team_id || undefined };
        if (remove) await deletePersonDefault(scope, 'monthly');
        else await setPersonDefault(scope, 'monthly', value);
      }
      await Promise.all(['budgetHierarchy', 'peopleSpend', 'myPersonCap', 'myBudget'].map(key => queryClient.invalidateQueries({ queryKey: [key] })));
      onClose();
    } catch (err) {
      const message = err && typeof err === 'object' && 'message' in err && typeof err.message === 'string' ? err.message : null;
      setError(message || 'Budget could not be saved. Please retry.');
    } finally { setSaving(false); }
  }
  return <Modal isOpen title={`${node.configured_usd == null ? 'Set' : 'Edit'} monthly budget`} onClose={() => { if (!saving) onClose(); }}>
    <form onSubmit={e => { e.preventDefault(); void save(false); }} className="space-y-4">
      <p className="text-sm text-gray-500 dark:text-gray-400">{path.join(' → ')}</p>
      <label className="block text-sm font-medium">Monthly budget (USD)<input autoFocus required type="number" min="0.01" max="99999999.99" step="0.01" value={value} onChange={e => setValue(e.target.value)} className="mt-2 block w-full rounded-lg border border-gray-300 dark:border-gray-600 p-2 bg-white dark:bg-gray-800" /></label>
      <p className="text-sm text-gray-500 dark:text-gray-400">{node.kind === 'user' ? 'One budget for this person’s direct usage and cloud agents, across all their memberships.' : 'This monthly amount applies to each person who inherits it. More specific budgets take precedence, including higher amounts.'}</p>
      <p className="text-sm">Without this override: <strong>{amountLabel(node.fallback)}</strong>{node.fallback.source_label && ` · ${node.fallback.source_label}`}</p>
      <p className="text-xs text-gray-500 dark:text-gray-400">Budget changes can take up to 60 seconds to reach enforcement. Existing additional restrictions still apply.</p>
      {error && <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex flex-wrap gap-3 justify-end">
        {node.configured_usd != null && <Button type="button" variant="secondary" disabled={saving} onClick={() => void save(true)}>Remove override</Button>}
        <Button type="button" variant="secondary" disabled={saving} onClick={onClose}>Cancel</Button>
        <Button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save budget'}</Button>
      </div>
    </form>
  </Modal>;
}

function PeopleSpendReport() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const { data, isPending, error, refetch } = useQuery({ queryKey: ['peopleSpend', page, search], queryFn: () => getPeopleSpend(page, search) });
  return <Card>
    <div className="flex flex-wrap justify-between gap-3"><div><h2 className="text-lg font-semibold">Spend by person</h2><p className="text-sm text-gray-500 dark:text-gray-400">This month · All workspaces · Each person counted once</p></div><input type="search" aria-label="Search people" placeholder="Search people" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-2 bg-white dark:bg-gray-800" /></div>
    {isPending ? <p className="mt-4" role="status">Loading people…</p> : error ? <p role="alert" className="mt-4">Spend report unavailable. <button className="underline" onClick={() => void refetch()}>Retry</button></p> : data && <>
      <div className="overflow-x-auto mt-4"><table className="w-full text-sm tabular-nums"><thead><tr>{['User', 'Direct usage', 'Cloud agents', 'Total spent', 'Monthly budget', 'Remaining'].map((label, i) => <th scope="col" key={label} className={`px-2 py-3 whitespace-nowrap ${i ? 'text-right' : 'text-left'}`}>{label}</th>)}</tr></thead><tbody>
        {data.items.map(person => { const budget = parseWireMoney(person.budget.amount_usd); const total = parseWireMoney(person.spend.total_usd); const remaining = budget != null && total != null ? budget - total : null;
          return <tr key={person.person_anchor} className="border-t border-gray-200 dark:border-gray-700"><th scope="row" className="text-left font-normal px-2 py-3"><span className="font-medium">{person.name}</span><span className="block text-xs text-gray-500">{person.email}</span></th>{[person.spend.direct_usd, person.spend.cloud_usd, person.spend.total_usd].map((v, i) => <td key={i} className="text-right px-2 py-3">{formatWireMoney(v)}</td>)}<td className="text-right px-2 py-3">{amountLabel(person.budget)}<span className="block text-xs text-gray-500">{person.budget.source_label}{person.budget.enforcement_mode && person.budget.enforcement_mode !== 'hard' ? ' · Advisory' : ''}</span></td><td className="text-right px-2 py-3 whitespace-nowrap">{remaining == null ? '—' : `${formatWireMoney(String(Math.abs(remaining)))}${remaining < 0 ? ' over' : ''}`}</td></tr>;
        })}
      </tbody></table></div>
      {!data.items.length && <p className="py-4 text-sm">No people found.</p>}
      <div className="mt-4 flex items-center justify-between text-sm"><span>{data.total} people</span><div className="flex items-center gap-3"><Button variant="secondary" disabled={page === 1} onClick={() => setPage(page - 1)}>Previous</Button><span>Page {page}</span><Button variant="secondary" disabled={page * data.page_size >= data.total} onClick={() => setPage(page + 1)}>Next</Button></div></div>
    </>}
    <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">Settled model usage attributed to people. Unattended automation and shared workspace controls remain in additional restrictions.</p>
  </Card>;
}

export function BudgetHierarchy() {
  const { data, isPending, error, refetch } = useQuery({ queryKey: ['budgetHierarchy'], queryFn: getBudgetHierarchy });
  const [editing, setEditing] = useState<{ node: BudgetNode; path: string[] } | null>(null);
  return <div className="space-y-5">
    <Card><h2 className="text-lg font-semibold">Budget hierarchy</h2><p className="mt-1 text-sm text-gray-500 dark:text-gray-400">Organization → Team → User. Each amount is a monthly budget per person. The closest configured budget applies.</p>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">People with several memberships appear in each branch. Their individual budget is shared; the lowest budget at the closest configured level applies.</p>
      {isPending ? <p role="status" className="mt-4">Loading hierarchy…</p> : error ? <p role="alert" className="mt-4">Budget hierarchy unavailable. <button className="underline" onClick={() => void refetch()}>Retry</button></p> : data && <ul className="mt-4"><HierarchyNode node={data} path={[]} onEdit={(node, path) => setEditing({ node, path })} /></ul>}
    </Card>
    <PeopleSpendReport />
    {editing && <BudgetEditor node={editing.node} path={editing.path} onClose={() => setEditing(null)} />}
  </div>;
}
