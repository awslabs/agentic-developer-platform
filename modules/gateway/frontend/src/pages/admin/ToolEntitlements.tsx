import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';
import { Permission } from '@/types';
import { getOrganizations, getOrgTeams } from '@/services/admin';
import {
  getToolAccess,
  getToolsCatalog,
  getToolsConsentDefault,
  getToolEntitlements,
  putToolEntitlement,
  putToolsConsentDefault,
  type CatalogTool,
  type ConsentPosture,
  type ToolCatalog,
  type ToolEntitlement,
  type ToolAccessReport,
  type ToolsConsentDefault,
} from '@/services/toolEntitlements';

interface Option {
  id: string;
  name: string;
}

function errorText(error: unknown): string {
  if (error && typeof error === 'object') {
    const response = error as { detail?: unknown; message?: unknown };
    if (typeof response.detail === "string") return response.detail;
    if (typeof response.message === "string") return response.message;
  }
  return 'Tool settings are unavailable; please retry.';
}

export default function ToolEntitlements() {
  const { user, hasPermission, isPlatformAdmin } = usePermissions();
  const canManage = hasPermission(Permission.TOOLS_MANAGE);
  const platformAdmin = isPlatformAdmin();
  const [organizations, setOrganizations] = useState<Option[]>([]);
  const [tenantId, setTenantId] = useState(platformAdmin ? '' : (user?.orgId ?? ''));
  const [teams, setTeams] = useState<Option[]>([]);
  const [teamId, setTeamId] = useState('');
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null);
  const [entitlements, setEntitlements] = useState<ToolEntitlement[]>([]);
  const [limits, setLimits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [accessUser, setAccessUser] = useState('');
  const [access, setAccess] = useState<ToolAccessReport | null>(null);
  const [accessLoading, setAccessLoading] = useState(false);
  const [consentDefault, setConsentDefault] = useState<ToolsConsentDefault | null>(null);

  useEffect(() => {
    if (!canManage || !tenantId) { setConsentDefault(null); return; }
    let active = true;
    getToolsConsentDefault(tenantId)
      .then(result => { if (active) setConsentDefault(result); })
      .catch(cause => { if (active) { setConsentDefault(null); setError(errorText(cause)); } });
    return () => { active = false; };
  }, [canManage, tenantId]);

  async function saveConsentDefault(value: string) {
    setSaving('consent-default');
    setError(null);
    setNotice(null);
    try {
      const next = await putToolsConsentDefault(tenantId, value === 'platform' ? null : (value as ConsentPosture));
      setConsentDefault(next);
      setNotice(`Consent posture for the tenant is now ${next.consent_default}${next.tenant_override ? '' : ' (platform default)'}.`);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setSaving(null);
    }
  }

  useEffect(() => {
    if (platformAdmin) return;
    setTenantId(user?.orgId ?? "");
    setTeamId("");
    setCatalog(null);
    setEntitlements([]);
    setAccess(null);
  }, [platformAdmin, user?.orgId]);

  useEffect(() => {
    if (!canManage || !platformAdmin) return;
    let active = true;
    async function loadOrganizations() {
      try {
        const items: Option[] = [];
        for (let page = 1; page <= 100; page++) {
          const response = await getOrganizations({ page, pageSize: 100 });
          if (!active) return;
          items.push(...response.items.map(org => ({ id: org.id, name: org.name })));
          if (!response.hasMore) { setOrganizations(items); return; }
        }
        throw new Error('Too many organizations to display.');
      } catch (cause) {
        if (active) setError(errorText(cause));
      }
    }
    void loadOrganizations();
    return () => { active = false; };
  }, [canManage, platformAdmin]);

  useEffect(() => {
    if (!canManage || !tenantId) return;
    let active = true;
    async function loadTeams() {
      try {
        const items: Option[] = [];
        for (let page = 1; page <= 100; page++) {
          const response = await getOrgTeams(tenantId, { page, pageSize: 100 });
          if (!active) return;
          items.push(...response.items.map(team => ({ id: team.id, name: team.name })));
          if (!response.hasMore) { setTeams(items); return; }
        }
        throw new Error('Too many teams to display.');
      } catch (cause) {
        if (active) setError(errorText(cause));
      }
    }
    void loadTeams();
    return () => { active = false; };
  }, [canManage, tenantId]);

  const refresh = useCallback(async (selectedTenant: string, selectedTeam: string) => {
    const [nextCatalog, nextEntitlements] = await Promise.all([
      getToolsCatalog(selectedTenant), getToolEntitlements(selectedTenant, selectedTeam || null),
    ]);
    return { nextCatalog, nextEntitlements };
  }, []);

  useEffect(() => {
    if (!canManage || !tenantId) return;
    let active = true;
    setLoading(true);
    setError(null);
    setCatalog(null);
    setEntitlements([]);
    setAccess(null);
    void refresh(tenantId, teamId).then(({ nextCatalog, nextEntitlements }) => {
      if (!active) return;
      setCatalog(nextCatalog);
      setEntitlements(nextEntitlements.entitlements);
      setLimits(Object.fromEntries(nextCatalog.tools.map(tool => [
        tool.name,
        nextEntitlements.entitlements.find(row => row.tool_name === tool.name && row.team_id === (teamId || null))?.max_cost_usd ?? '',
      ])));
    }).catch(cause => { if (active) setError(errorText(cause)); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [canManage, tenantId, teamId, refresh]);

  async function save(tool: CatalogTool, enabled: boolean, cost: string | null) {
    const selectedTeam = teamId || null;
    const row = entitlements.find(item => item.tool_name === tool.name && item.team_id === selectedTeam);
    setSaving(tool.name);
    setError(null);
    setNotice(null);
    try {
      setAccess(null);
      await putToolEntitlement({
        tenant_id: tenantId, team_id: selectedTeam, tool_name: tool.name,
        enabled, max_cost_usd: cost, expected_revision: row?.revision ?? 0,
      });
      const next = await getToolEntitlements(tenantId, selectedTeam);
      setEntitlements(next.entitlements);
      setNotice(`${tool.name} updated for ${selectedTeam ? 'this team' : 'the tenant'}.`);
    } catch (cause) {
      setError(errorText(cause));
      try {
        const next = await getToolEntitlements(tenantId, selectedTeam);
        setEntitlements(next.entitlements);
      } catch { setCatalog(null); }
    } finally {
      setSaving(null);
    }
  }

  async function checkAccess() {
    if (!tenantId || !accessUser.trim()) return;
    setAccessLoading(true);
    setAccess(null);
    setError(null);
    try {
      setAccess(await getToolAccess(tenantId, accessUser.trim()));
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setAccessLoading(false);
    }
  }

  if (!canManage) return <p>You do not have permission to manage tools.</p>;
  if (!platformAdmin && tenantId !== (user?.orgId ?? "")) return <p>Switching organization…</p>;

  return (
    <main className="mx-auto max-w-5xl space-y-5 p-6">
      <h1 className="text-2xl font-semibold">Tool access</h1>
      <p>Tools start disabled for each tenant. A team may narrow an enabled tenant grant. User consent and <Link to="/ratelimits" className="underline">rate limits</Link> still apply.</p>
      {platformAdmin ? (
        <label className="block">Organization
          <select aria-label="Organization" disabled={Boolean(saving)} className="ml-3 rounded border p-2" value={tenantId} onChange={event => {
            setTenantId(event.target.value); setTeamId(''); setTeams([]); setCatalog(null); setEntitlements([]); setNotice(null);
          }}>
            <option value="">Select an organization</option>
            {organizations.map(org => <option key={org.id} value={org.id}>{org.name}</option>)}
          </select>
        </label>
      ) : <p>Organization: {user?.orgId ?? 'No organization selected'}</p>}
      {tenantId && <label className="block">Scope
        <select aria-label="Scope" disabled={Boolean(saving)} className="ml-3 rounded border p-2" value={teamId} onChange={event => {
          setTeamId(event.target.value); setCatalog(null); setEntitlements([]); setNotice(null);
        }}>
          <option value="">Entire tenant</option>
          {teams.map(team => <option key={team.id} value={team.id}>{team.name}</option>)}
        </select>
      </label>}
      {tenantId && <section aria-label="Consent posture" className="rounded border p-4">
        <h2 className="font-semibold">Consent</h2>
        <p>By default members get every tool the tenant enables for their team and can switch individual tools off. Explicit opt-in makes each member tick tools in Settings before agents may use them.</p>
        {consentDefault ? (
          <label>Posture
            <select aria-label="Consent posture" className="ml-2 rounded border p-1" disabled={Boolean(saving)}
              value={consentDefault.tenant_override ?? 'platform'} onChange={event => void saveConsentDefault(event.target.value)}>
              <option value="platform">Platform default ({consentDefault.platform_default})</option>
              <option value="granted">Granted by default (members opt out)</option>
              <option value="explicit">Explicit opt-in per member</option>
            </select>
            <span className="ml-3 text-sm">Effective: {consentDefault.consent_default}</span>
          </label>
        ) : <p>Consent posture unavailable.</p>}
      </section>}
      {tenantId && <section aria-label="Effective tool access" className="rounded border p-4">
        <h2 className="font-semibold">Check a user's effective access</h2>
        <p>This read-only check uses the user's actual team. Policy ready does not verify a live provider or remaining budget.</p>
        <label>User ID <input className="ml-2 rounded border p-1" value={accessUser}
          onChange={event => { setAccessUser(event.target.value); setAccess(null); }} /></label>
        <button className="ml-2 rounded border px-3 py-1" type="button"
          disabled={!accessUser.trim() || accessLoading} onClick={() => void checkAccess()}>Check access</button>
        {access && <div role="status"><p>User {access.user_id} · team {access.team_id}</p>
          <ul>{access.tools.map(tool => <li key={tool.name}>
            {tool.name}: {tool.ready ? 'Policy ready' : tool.reason?.replace(/_/g, ' ')}{tool.discovery_reason && `; hidden from discovery: ${tool.discovery_reason.replace(/_/g, ' ')}`}
          </li>)}</ul>
        </div>}
      </section>}
      {error && <p role="alert" className="text-red-700">{error}</p>}
      {notice && <p role="status" className="text-green-700">{notice}</p>}
      {loading && <p role="status">Loading tools…</p>}
      {catalog && <>
        <p>Connectors: {catalog.connectors.map(connector => `${connector.name}${connector.dynamic ? ' (dynamic catalogue)' : ''}`).join(', ') || 'none'}.</p>
        {catalog.tools.length === 0 && <p>No statically described tools are available from this gateway.</p>}
        <ul className="space-y-4">
          {catalog.tools.map(tool => {
            const tenant = entitlements.find(row => row.tool_name === tool.name && row.team_id === null);
            const team = teamId ? entitlements.find(row => row.tool_name === tool.name && row.team_id === teamId) : null;
            const selected = teamId ? team : tenant;
            const enabled = Boolean(tenant?.enabled && (!teamId || team?.enabled !== false));
            const ceiling = limits[tool.name] ?? '';
            const locked = Boolean(saving || loading || (teamId && !tenant?.enabled));
            return <li key={tool.name} className="rounded border p-4">
              <div className="flex items-center gap-3">
                <label><input type="checkbox" checked={enabled} disabled={locked} onChange={event => void save(tool, event.target.checked, selected?.max_cost_usd ?? null)} /> {tool.name}</label>
                <span>{tool.connector} · {tool.credential_kinds.join(', ') || 'no credential kind'}</span>
              </div>
              <p>{tool.description}</p>
              {teamId && <p>{team ? 'Team override' : 'Inherited from tenant'}{!tenant?.enabled ? ' · Enable at tenant scope first' : ''}</p>}
              <label>Maximum cost per call (USD; optional)
                <input className="ml-2 w-36 rounded border p-1" aria-label={`${tool.name} cost limit`} type="number" min="0.000001" step="0.000001" value={ceiling} disabled={locked || !enabled} onChange={event => setLimits(previous => ({ ...previous, [tool.name]: event.target.value }))} />
              </label>
              <button className="ml-2 rounded border px-3 py-1" type="button" disabled={locked || !enabled} onClick={() => void save(tool, enabled, ceiling || null)}>Save limit</button>
            </li>;
          })}
        </ul>
      </>}
    </main>
  );
}
