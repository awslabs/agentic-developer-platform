import { useCallback, useEffect, useRef, useState } from 'react';
import { Badge, Button, Card, Input, Table } from '@/components/ui';
import {
  identitySources, identitySourceLabels, loadIdentityHierarchy, loadServiceIdentityPage,
  type IdentitySource, type OrganizationIdentityHierarchy, type OrganizationServiceIdentity,
} from '@/services/organizationServiceIdentities';

type SourcePage = { source: IdentitySource; cursor: string | null; error?: string };
const firstPages = (): SourcePage[] => identitySources.map(source => ({ source, cursor: '1' }));

/** Mounted with key=orgId: switching organizations immediately discards the old roster. */
export function ServiceIdentityList({ orgId }: { orgId: string }) {
  const [items, setItems] = useState<OrganizationServiceIdentity[]>([]);
  const [pages, setPages] = useState(firstPages);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [hierarchy, setHierarchy] = useState<OrganizationIdentityHierarchy>({ departments: {}, teams: {} });
  const [hierarchyError, setHierarchyError] = useState(false);
  const mounted = useRef(false);
  const generation = useRef(0);

  const load = useCallback(async (requested: SourcePage[]) => {
    const requestGeneration = ++generation.current;
    setLoading(true);
    const pending = requested.filter(page => page.cursor !== null);
    const results = await Promise.allSettled(pending.map(page => loadServiceIdentityPage(orgId, page.source, page.cursor!)));
    if (!mounted.current || generation.current !== requestGeneration) return;
    const received: OrganizationServiceIdentity[] = [];
    const updated = [...requested];
    results.forEach((result, index) => {
      const page = pending[index];
      const position = updated.findIndex(value => value.source === page.source);
      if (result.status === 'fulfilled') {
        received.push(...result.value.items);
        updated[position] = { source: page.source, cursor: result.value.next };
      } else {
        updated[position] = { ...page, error: `${identitySourceLabels[page.source]} accounts could not be loaded.` };
      }
    });
    setItems(previous => [...new Map([...previous, ...received].map(row => [`${row.source}:${row.id}`, row])).values()]);
    setPages(updated);
    setLoading(false);
  }, [orgId]);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    void load(firstPages());
    void loadIdentityHierarchy(orgId).then(result => {
      if (active) setHierarchy(result);
    }).catch(() => { if (active) setHierarchyError(true); });
    return () => { active = false; mounted.current = false; generation.current++; };
  }, [orgId, load]);

  const departmentId = (row: OrganizationServiceIdentity) => row.departmentId || (row.source === 'iam' && row.teamId ? hierarchy.teams[row.teamId]?.departmentId : undefined);
  const department = (row: OrganizationServiceIdentity) => {
    const id = departmentId(row);
    return id ? hierarchy.departments[id] || id : row.source === 'iam' && row.teamId ? 'Department unavailable' : 'Unassigned';
  };
  const team = (row: OrganizationServiceIdentity) => row.teamId ? hierarchy.teams[row.teamId]?.name || row.teamId : 'Unassigned';
  const visible = items.filter(row => [row.name, row.id, department(row), team(row), identitySourceLabels[row.source]]
    .some(value => value.toLowerCase().includes(search.toLowerCase())));
  const errors = pages.flatMap(page => page.error ? [page.error] : []);

  return (
    <Card padding="none">
      <div className="p-4 space-y-3 border-b border-gray-200 dark:border-gray-700">
        <h3 className="font-semibold text-gray-900 dark:text-white">Service accounts</h3>
        <p className="text-sm text-gray-500 dark:text-gray-400">Service identities and their department and team assignments.</p>
        <Input name="service-account-search" aria-label="Search service accounts" placeholder="Search name, department or team…"
          value={search} onChange={event => setSearch(event.target.value)} />
        {errors.length > 0 && <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">{errors.join(' ')} The list may be incomplete.</p>}
        {hierarchyError && <p role="alert" className="text-sm text-amber-700 dark:text-amber-300">Department and team names could not be loaded. Available assignment IDs are shown.</p>}
      </div>
      <Table data={visible} keyExtractor={row => `${row.source}:${row.id}`} isLoading={loading && items.length === 0}
        emptyMessage={errors.length ? 'No service accounts could be displayed.' : search ? 'No matching service accounts in the loaded list.' : 'No service accounts in this organization.'}
        columns={[
          { key: 'name', header: 'Identity', render: row => <div className="space-y-1">
            <div className="font-medium text-gray-900 dark:text-white">{row.name}</div>
            <div className="flex items-center gap-2"><Badge variant="info" size="sm">Service account</Badge><span className="text-xs text-gray-500">{identitySourceLabels[row.source]}</span></div>
          </div> },
          { key: 'department', header: 'Department', render: department },
          { key: 'team', header: 'Team', render: team },
          { key: 'status', header: 'Status', render: row => <Badge variant={row.status === 'active' ? 'success' : 'default'}>{row.status}</Badge> },
        ]} />
      <div className="p-4 flex items-center justify-between gap-3">
        <p className="text-xs text-gray-500">{items.length} service accounts loaded{search ? ` · ${visible.length} matching` : ''}</p>
        {pages.some(page => page.cursor !== null) && <Button variant="secondary" size="sm" disabled={loading} onClick={() => void load(pages)}>
          {loading ? 'Loading…' : errors.length ? 'Retry / load more accounts' : 'Load more service accounts'}
        </Button>}
      </div>
    </Card>
  );
}
