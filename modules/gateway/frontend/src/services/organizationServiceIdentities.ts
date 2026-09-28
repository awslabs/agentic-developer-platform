import { apiClient } from './api';

export type IdentitySource = 'cognito' | 'iam' | 'legacy';
export const identitySources: IdentitySource[] = ['cognito', 'iam', 'legacy'];
export const identitySourceLabels: Record<IdentitySource, string> = {
  cognito: 'Cognito', iam: 'IAM agent', legacy: 'Registered service account',
};
export interface OrganizationServiceIdentity {
  id: string;
  source: IdentitySource;
  name: string;
  teamId?: string;
  departmentId?: string;
  status: string;
}
interface WireIdentity {
  client_id?: string;
  agent_id?: string;
  id?: string;
  name?: string;
  agent_name?: string;
  org_id: string;
  team_id?: string;
  department_id?: string;
  status?: string;
}
interface WirePage<T> { items: T[]; has_more?: boolean; last_key?: string | null }

/** Use the existing ORG_READ-gated APIs, always with the selected organization. */
export async function loadServiceIdentityPage(orgId: string, source: IdentitySource, cursor = '1') {
  const params = new URLSearchParams({ page_size: '50' });
  let path: string;
  if (source === 'legacy') {
    params.set('page', cursor);
    path = `/admin/organizations/${encodeURIComponent(orgId)}/service-accounts`;
  } else {
    params.set('org_id', orgId);
    if (source === 'cognito') params.set('page', cursor);
    else if (cursor !== '1') params.set('last_key', cursor);
    path = source === 'cognito' ? '/admin/agents' : '/admin/registry/agents';
  }
  const page = await apiClient.get<WirePage<WireIdentity>>(`${path}?${params}`);
  const items = page.items.map((row): OrganizationServiceIdentity => {
    if (row.org_id !== orgId) throw new Error('Unexpected organization in service account response');
    const id = source === 'cognito' ? row.client_id : source === 'iam' ? row.agent_id : row.id;
    if (!id) throw new Error('Service account response is missing its identity');
    return { id, source, name: row.name || row.agent_name || id, teamId: row.team_id || undefined,
      departmentId: row.department_id || undefined, status: row.status || 'Registered' };
  });
  const next = source === 'iam' ? page.last_key || null : page.has_more ? String(Number(cursor) + 1) : null;
  return { items, next };
}

export interface OrganizationIdentityHierarchy {
  departments: Record<string, string>;
  teams: Record<string, { name: string; departmentId: string }>;
}

/** Follow metadata pages so accounts outside the first department/team page get names too. */
export async function loadIdentityHierarchy(orgId: string): Promise<OrganizationIdentityHierarchy> {
  async function allPages<T>(kind: 'departments' | 'teams'): Promise<T[]> {
    const result: T[] = [];
    for (let page = 1; ; page++) {
      const response = await apiClient.get<WirePage<T>>(
        `/admin/organizations/${encodeURIComponent(orgId)}/${kind}?page=${page}&page_size=100`,
      );
      result.push(...response.items);
      if (!response.has_more) return result;
    }
  }
  const [departments, teams] = await Promise.all([
    allPages<{ id: string; name: string }>('departments'),
    allPages<{ id: string; name: string; department_id: string }>('teams'),
  ]);
  return {
    departments: Object.fromEntries(departments.map(row => [row.id, row.name])),
    teams: Object.fromEntries(teams.map(row => [row.id, { name: row.name, departmentId: row.department_id }])),
  };
}
