import { useState } from 'react';
import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';
import { isSuperseded, previewRetirementAccess, type ScopeGuard } from './client';
import { ENDPOINTS, type RetirementAccessReview, type Unavailable } from './contract';
import { claimPreviewIdentity, recordRetirementLineage, type ReceiptScope, type ReceiptStore, type StoredReceipt } from './operations';
import { RetirementApproval } from './RetirementApproval';

export function RetirementPreparation(props: {
  workspaceId: string; scope: ReceiptScope; store: ReceiptStore; guard: ScopeGuard; sessionToken: string;
}) {
  const { workspaceId, scope, store, guard, sessionToken } = props;
  const [review, setReview] = useState<RetirementAccessReview | null>(null);
  const [receipt, setReceipt] = useState<StoredReceipt | null>(null);
  const [retirementId, setRetirementId] = useState<string | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [busy, setBusy] = useState(false);
  const prepare = async () => {
    if (busy || !scope.principalId || getAccessToken() !== sessionToken) return;
    const generation = guard.current();
    setBusy(true);
    try {
      const root = await claimPreviewIdentity(store, scope, `retire-workspace:${workspaceId}`,
        { workspaceId }, () => crypto.randomUUID(), new Date().toISOString());
      if (!guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
      if (root.kind === 'conflict') { setProblem({ reason: 'unknown', detail: root.detail }); return; }
      setRetirementId(root.receipt.idempotencyKey);
      const result = await previewRetirementAccess(guard, workspaceId, { operation_id: root.receipt.idempotencyKey });
      if (isSuperseded(result) || getAccessToken() !== sessionToken) return;
      if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
      if (result.value.admission_available !== true) {
        setProblem({ reason: 'not-deployed', detail: 'This workspace does not have an available managed cleanup preparation.' }); return;
      }
      const access = await claimPreviewIdentity(store, scope, `prepare-retirement-access:${workspaceId}`,
        { workspaceId, retirementId: root.receipt.idempotencyKey, revision: result.value.revision },
        () => result.value.request_id, new Date().toISOString());
      if (!guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
      if (access.kind === 'conflict') { setProblem({ reason: 'unknown', detail: access.detail }); return; }
      if (access.receipt.idempotencyKey !== result.value.request_id) {
        setProblem({ reason: 'unknown', detail: 'The original preparation identity differs. Recover its status before continuing.' }); return;
      }
      const saved = await recordRetirementLineage(store, scope, `retire-workspace:${workspaceId}`,
        root.receipt.idempotencyKey, workspaceId, { accessRequestId: result.value.request_id });
      if (!saved || !guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
      setReceipt(access.receipt); setReview(result.value); setProblem(null);
    } catch { setProblem({ reason: 'unknown', detail: 'The preparation request could not be saved. Restore browser storage and recover the same request.' }); }
    finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  if (!ENDPOINTS.previewRetirementAccess.served) return null;
  return <section className="my-4 space-y-3 rounded border p-3" aria-label="Prepare workspace removal">
    <h4 className="font-semibold">Prepare workspace removal</h4>
    <p>First approve temporary cleanup access, a persistent admission fence and preparation of an exact destroy plan. Infrastructure removal requires a second approval.</p>
    <Button variant="secondary" disabled={busy} onClick={() => void prepare()}>Review cleanup preparation</Button>
    {review && receipt && retirementId && <>
      <p>Additional resource and cost allowance: zero. Existing resources can continue to incur charges.</p>
      <details><summary>Cleanup authority and preserved resources</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify({ authority: review.authority, preserved: review.preserved }, null, 2)}</pre></details>
      <RetirementApproval key={review.revision} {...props} review={review} initialReceipt={receipt} retirementId={retirementId} preparation />
    </>}
    {problem && <Alert variant="warning" title="Preparation unavailable">{problem.detail}</Alert>}
  </section>;
}
