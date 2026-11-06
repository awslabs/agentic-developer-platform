import { useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';
import { call, isSuperseded, type ScopeGuard } from './client';
import type { OperationReceipt } from './contract';
import { useFreshnessClock } from './readiness';

type Review = {
  reauthorization_id: string; operation_id: string; expected_version: number;
  original_approval_id: string; plan_digest: string; request: unknown; envelope: unknown;
  result: string; can_decide: boolean; expires_at: string;
};

function parseReview(raw: unknown): Review | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  if (typeof row.reauthorization_id !== 'string' || typeof row.operation_id !== 'string' ||
      typeof row.expected_version !== 'number' || typeof row.plan_digest !== 'string' ||
      typeof row.original_approval_id !== 'string' || typeof row.expires_at !== 'string' ||
      typeof row.result !== 'string' || typeof row.can_decide !== 'boolean' || !row.request || !row.envelope) return null;
  return row as Review;
}

export function DispatchRecoveryPanel({ workspaceId, orgId, operation, guard, sessionToken, onResumed, mode = 'undispatched' }: {
  workspaceId: string; orgId: string; operation: OperationReceipt; guard: ScopeGuard;
  sessionToken: string; onResumed: () => void;
  mode?: 'undispatched' | 'provider';
}) {
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const params = { workspace_id: workspaceId };
  const now = useFreshnessClock();
  const expired = review !== null && Date.parse(review.expires_at) <= now;
  const storageKey = `adp.superplane.${mode === 'provider' ? 'provider' : 'dispatch'}-recovery.${orgId}.${workspaceId}.${operation.operationId}.${operation.version}`;
  async function act(action: 'review' | 'approve' | 'resume') {
    if (busy || getAccessToken() !== sessionToken) return;
    setBusy(true); setProblem(null);
    try {
      if (action === 'review') {
        const key = storageKey;
        let requestId = window.localStorage.getItem(key);
        if (!requestId) { requestId = crypto.randomUUID(); window.localStorage.setItem(key, requestId); }
        const result = await call(guard, mode === 'provider' ? 'reviewProviderRecovery' : 'reviewDispatchRecovery', params,
          { expected_version: operation.version, idempotency_key: requestId }, parseReview);
        if (isSuperseded(result) || getAccessToken() !== sessionToken) return;
        if (result.ok && result.value.operation_id === operation.operationId && result.value.expected_version === operation.version) setReview(result.value);
        else if (!result.ok && 'unavailable' in result) setProblem(result.unavailable.detail);
        else setProblem('The recovery review did not match this operation.');
      } else if (review && action === 'approve') {
        const result = await call(guard, mode === 'provider' ? 'decideProviderRecovery' : 'decideDispatchRecovery', params,
          { reauthorization_id: review.reauthorization_id, result: 'allowed-once' }, parseReview);
        if (isSuperseded(result) || getAccessToken() !== sessionToken) return;
        if (result.ok && result.value.reauthorization_id === review.reauthorization_id && result.value.operation_id === review.operation_id && result.value.expected_version === review.expected_version && result.value.plan_digest === review.plan_digest) setReview(result.value);
        else if (!result.ok && 'unavailable' in result) setProblem(result.unavailable.detail);
      } else if (review) {
        const result = await call(guard, mode === 'provider' ? 'resumeProviderRecovery' : 'resumeDispatchRecovery', params,
          { reauthorization_id: review.reauthorization_id }, (raw) => {
            if (!raw || typeof raw !== 'object') return null;
            const value = raw as Record<string, unknown>;
            return value.operation_id === operation.operationId && value.reauthorization_id === review.reauthorization_id &&
              value.status === 'recovery-recorded' ? true : null;
          });
        if (isSuperseded(result) || getAccessToken() !== sessionToken) return;
        if (result.ok) onResumed();
        else if ('unavailable' in result) setProblem(result.unavailable.detail);
      }
    } catch {
      setProblem('Recovery could not be confirmed. Keep this workspace and its original request; retry the same review.');
    } finally { setBusy(false); }
  }
  return <section aria-label="Recover original provisioning" className="mt-4 space-y-3">
    <p>{mode === 'provider'
      ? 'If preparation has stopped, recovery can retry this original plan after fresh approval and confirmation that the previous attempt received no AWS credentials.'
      : 'Provisioning is not progressing. Recovery can retry this original plan only after fresh approval and proof that no execution started.'}</p>
    {problem && <Alert variant="warning" title="Recovery unavailable">{problem}</Alert>}
    <Button variant="secondary" disabled={busy} onClick={() => { void act('review'); }}>Review original request for recovery</Button>
    {review && <>
      <p>Original operation: {review.operation_id}. Approval expires: {review.expires_at}.</p>
      {expired && <><p>This recovery approval expired. Request a fresh review of the same plan.</p><Button disabled={busy} variant="secondary" onClick={() => {
        window.localStorage.removeItem(storageKey); setReview(null); void act('review');
      }}>Request fresh recovery review</Button></>}
      <details><summary>Review original plan and limits</summary><pre className="overflow-auto text-xs">{JSON.stringify({ request: review.request, limits: review.envelope }, null, 2)}</pre></details>
      {!expired && review.can_decide && <Button disabled={busy} onClick={() => { void act('approve'); }}>Approve recovery of this plan</Button>}
      {review.result === 'pending' && !review.can_decide && <p>The original selected approver must approve this recovery.</p>}
      {!expired && review.result === 'allowed-once' && <Button disabled={busy} onClick={() => { void act('resume'); }}>Resume original provisioning</Button>}
    </>}
  </section>;
}
