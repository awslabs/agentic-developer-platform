import { useEffect, useRef, useState, type FormEvent } from 'react';

import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';

import { getWorkspaceAccess, grantWorkspaceAccess, isSuperseded, type HumanWorkspaceAccess, type ScopeGuard } from './client';

type AccessState =
  | { phase: 'loading' }
  | { phase: 'unavailable'; detail: string }
  | { phase: 'viewer'; access: HumanWorkspaceAccess }
  | { phase: 'admin'; access: HumanWorkspaceAccess };

export function ApproverAccessControl({ workspaceId, guard, sessionToken }: {
  workspaceId: string;
  guard: ScopeGuard;
  sessionToken: string;
}) {
  const [state, setState] = useState<AccessState>({ phase: 'loading' });
  const [target, setTarget] = useState('');
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState('');
  const requestId = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    if (getAccessToken() !== sessionToken) {
      setState({ phase: 'unavailable', detail: 'Your session changed. Select the workspace again.' });
      return;
    }
    void getWorkspaceAccess(guard, workspaceId).then((outcome) => {
      if (!active || isSuperseded(outcome) || getAccessToken() !== sessionToken) return;
      if (!outcome.ok) {
        if ('unavailable' in outcome) setState({ phase: 'unavailable', detail: outcome.unavailable.detail });
        return;
      }
      setState(outcome.value.effective_permissions.includes('workspace:administer')
        ? { phase: 'admin', access: outcome.value } : { phase: 'viewer', access: outcome.value });
    });
    return () => { active = false; };
  }, [workspaceId, guard, sessionToken]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (sending || state.phase !== 'admin') return;
    const subject = target.trim();
    if (!subject || subject.includes('@') || subject === state.access.subject) {
      setResult('Enter a different current human’s immutable ADP subject, not an email address.');
      return;
    }
    if (getAccessToken() !== sessionToken) {
      setResult('Your session changed. Select the workspace again.');
      return;
    }
    requestId.current ??= crypto.randomUUID();
    setSending(true);
    setResult('');
    const outcome = await grantWorkspaceAccess(guard, workspaceId, subject, requestId.current);
    setSending(false);
    if (isSuperseded(outcome) || getAccessToken() !== sessionToken) return;
    if (!outcome.ok) {
      if ('unavailable' in outcome) setResult(outcome.unavailable.detail);
      return;
    }
    requestId.current = null;
    setResult(`Read access granted to ${outcome.value.subject} (grant ${outcome.value.grant_id}, revision ${outcome.value.revision}). The other person must sign in under their own identity.`);
  }

  return (
    <section aria-label="Set up workspace approver" className="mt-4">
      <h4 className="font-semibold">Set up a distinct human approver</h4>
      {state.phase === 'loading' && <p role="status">Checking your current workspace access…</p>}
      {state.phase === 'unavailable' && <Alert variant="warning" title="Access check unavailable">{state.detail}</Alert>}
      {state.phase === 'viewer' && <p>Only a current explicit workspace administrator can grant another person access. Your access comes from {state.access.source === 'explicit_assignment' ? 'an explicit assignment' : 'a pre-existing grant without recorded assignment provenance'}.</p>}
      {state.phase === 'admin' && (
        <form onSubmit={(event) => { void submit(event); }}>
          <p>Grant read-only workspace access for approval setup. This does not approve a request or give the other person your identity.</p>
          <label htmlFor="approver-subject">Other current human’s immutable ADP subject (not email)</label>
          <input id="approver-subject" className="block w-full rounded border p-2" value={target}
            onChange={(event) => { setTarget(event.target.value); requestId.current = null; setResult(''); }}
            required maxLength={255} autoComplete="off" disabled={sending} />
          <Button type="submit" disabled={sending} className="mt-2">Grant read access</Button>
        </form>
      )}
      {result && <p role="status">{result}</p>}
    </section>
  );
}
