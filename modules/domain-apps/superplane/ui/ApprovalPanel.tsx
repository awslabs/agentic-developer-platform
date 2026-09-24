import { useEffect, useState } from 'react';

import { Alert, Button, Input } from '@/components/ui';

import { decideApproval, getApproval, isSuperseded, ScopeGuard } from './client';
import type { OperationApproval, Unavailable } from './contract';
import { useFreshnessClock } from './readiness';

export function ApprovalPanel({ approval, guard, onChange }: {
  approval: OperationApproval;
  guard: ScopeGuard;
  onChange: (approval: OperationApproval) => void;
}) {
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [busy, setBusy] = useState(false);
  const now = useFreshnessClock();
  const expires = Date.parse(approval.expires_at);
  const current = Number.isFinite(expires) && expires > now && !approval.revoked;
  const act = async (decision?: 'allowed-once' | 'rejected') => {
    setBusy(true);
    const result = decision
      ? await decideApproval(guard, approval.approval_id, decision)
      : await getApproval(guard, approval.approval_id);
    if (isSuperseded(result)) return;
    setBusy(false);
    if (result.ok) { onChange(result.value); setProblem(null); }
    else if ('unavailable' in result) setProblem(result.unavailable);
  };
  return <section className="mt-4 rounded border p-4" aria-label="Operation approval">
    <h3 className="font-semibold">Operation approval</h3>
    <p>Approval reference: {approval.approval_id}</p>
    <p>Workspace: {approval.workspace_id}</p>
    <p>Action: {approval.action}</p>
    {Object.entries(approval.target).map(([key, value]) => <p key={key}>{key.replace(/_/g, ' ')}: {value}</p>)}
    <p>Status: {approval.result}{!current ? ' (expired or revoked)' : ''}</p>
    <p>Maximum resources: {approval.envelope.max_resource_units ?? 'not reported'}</p>
    <p>Maximum runtime: {approval.envelope.max_runtime_seconds ?? 'not reported'} seconds</p>
    <p>Maximum cost: {approval.envelope.max_cost_micros === null ? 'not reported' : `${approval.envelope.max_cost_micros / 1_000_000} USD`}</p>
    <p>Plan reference: {approval.plan_digest}</p>
    <p>Expires: {approval.expires_at || 'not reported'}</p>
    {approval.result === 'pending' && !approval.can_decide && <p>Share this approval reference with a selected approver.</p>}
    <div className="mt-3 flex flex-wrap gap-2">
      <Button variant="secondary" disabled={busy} onClick={() => void act()}>Refresh approval</Button>
      {approval.can_decide && approval.result === 'pending' && current && <>
        <Button disabled={busy} onClick={() => void act('allowed-once')}>Approve this operation once</Button>
        <Button variant="danger" disabled={busy} onClick={() => void act('rejected')}>Reject operation</Button>
      </>}
    </div>
    {problem && <Alert variant="warning" title="Approval unavailable">{problem.detail}</Alert>}
  </section>;
}

/** Lets a selected approver open the reference shared by the requester. */
export function ApprovalLookup() {
  const [guard] = useState(() => new ScopeGuard());
  const [id, setId] = useState('');
  const [approval, setApproval] = useState<OperationApproval | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  useEffect(() => () => guard.supersede(), [guard]);
  const load = async () => {
    const result = await getApproval(guard, id.trim());
    if (isSuperseded(result)) return;
    if (result.ok) { setApproval(result.value); setProblem(null); }
    else if ('unavailable' in result) setProblem(result.unavailable);
  };
  return <section className="mt-6" aria-label="Review an operation approval">
    <Input name="approval-reference" label="Approval reference" value={id} onChange={(event) => setId(event.target.value)} />
    <Button variant="secondary" disabled={!id.trim()} onClick={() => void load()}>Open approval</Button>
    {approval && <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />}
    {problem && <Alert variant="warning" title="Approval unavailable">{problem.detail}</Alert>}
  </section>;
}
