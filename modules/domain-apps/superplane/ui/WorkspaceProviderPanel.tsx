import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';
import { call, isSuperseded, parseValidation, ScopeGuard } from './client';
import type { ValidationReading } from './contract';
import { ProviderConnectionPanel, type ProviderConnectionPanelProps } from './ProviderConnectionPanel';

type Authority = {
  workspace_id: string; managed: boolean; credential_id: string | null;
  account_id: string | null; expires_at: string | null;
  admits_new_work: boolean; validation: ValidationReading | null;
};
function parse(raw: unknown): Authority | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  if (typeof row.workspace_id !== 'string' || typeof row.managed !== 'boolean' ||
      (row.managed && (typeof row.credential_id !== 'string' || !row.credential_id.startsWith('spda1:')))) return null;
  return { workspace_id: row.workspace_id, managed: row.managed,
    credential_id: typeof row.credential_id === 'string' ? row.credential_id : null,
    account_id: typeof row.account_id === 'string' ? row.account_id : null,
    expires_at: typeof row.expires_at === 'string' ? row.expires_at : null,
    admits_new_work: row.admits_new_work === true, validation: parseValidation(row.validation) };
}

export function WorkspaceProviderPanel(props: ProviderConnectionPanelProps & { sessionToken: string }) {
  const { workspaceId, sessionToken, onObservation } = props;
  const [authority, setAuthority] = useState<Authority | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const guard = useRef(new ScopeGuard());
  const observe = useCallback(async () => {
    const result = await call(guard.current, 'workspaceProviderAuthority', { workspace_id: workspaceId }, undefined, parse);
    if (isSuperseded(result) || getAccessToken() !== sessionToken) return;
    if (!result.ok || result.value.workspace_id !== workspaceId) {
      setAuthority(null);
      setProblem(!result.ok && 'unavailable' in result ? result.unavailable.detail : 'Provider observation did not match this workspace.');
      onObservation?.({ workspaceId, validation: null, admitsNewWork: null });
      return;
    }
    setAuthority(result.value); setProblem(null);
    if (result.value.managed) onObservation?.({ workspaceId, validation: result.value.validation, admitsNewWork: result.value.admits_new_work });
  }, [workspaceId, sessionToken, onObservation]);
  useEffect(() => {
    guard.current = new ScopeGuard();
    setAuthority(null); setProblem(null);
    onObservation?.({ workspaceId, validation: null, admitsNewWork: null });
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() { await observe(); if (!stopped) timer = setTimeout(() => { void poll(); }, 30_000); }
    void poll();
    return () => { stopped = true; clearTimeout(timer); guard.current.supersede(); };
  }, [observe, workspaceId, onObservation]);
  async function refresh() {
    if (!authority?.credential_id || busy || getAccessToken() !== sessionToken) return;
    setBusy(true);
    const result = await call(guard.current, 'validateProviderAuthority', { credential_id: authority.credential_id }, {}, parseValidation);
    if (!isSuperseded(result) && getAccessToken() === sessionToken) {
      if (!result.ok && 'unavailable' in result) {
        setProblem(result.unavailable.detail);
        onObservation?.({ workspaceId, validation: null, admitsNewWork: null });
      } else if (result.ok) await observe();
      setBusy(false);
    }
  }
  if (authority?.managed === false) return <ProviderConnectionPanel {...props} />;
  return <section aria-label="Workspace provider authority" className="space-y-3 rounded-lg border p-4">
    <h2 className="font-semibold">Workspace AWS connection</h2>
    {problem && <Alert variant="warning" title="Provider observation unavailable">{problem}</Alert>}
    {authority?.managed ? <>
      <p>This workspace uses an installation-managed AWS connection for account {authority.account_id}.</p>
      <p>Authority expires: {authority.expires_at ?? 'Unknown'}. Last validation: {authority.validation?.checked_at ?? 'Not observed'}.</p>
      {props.mayManage && <Button disabled={busy} variant="secondary" onClick={() => { void refresh(); }}>Refresh AWS validation</Button>}
    </> : !problem && <p>Checking the workspace provider connection…</p>}
  </section>;
}
