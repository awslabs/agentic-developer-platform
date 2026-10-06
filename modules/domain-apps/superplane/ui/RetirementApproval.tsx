import { useEffect, useState } from 'react';

import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';
import { ApprovalPanel } from './ApprovalPanel';
import {
  getApproval, getOperation, isSuperseded, previewRetirement, previewRetirementAccess,
  recoverOperation, requestApproval, submitRetirement, type ScopeGuard,
} from './client';
import type { OperationApproval, Unavailable } from './contract';
import {
  isTerminal, markSubmissionStage, recordObservationExclusive, recordRetirementLineage,
  type ReceiptScope, type ReceiptStore, type StoredReceipt,
} from './operations';
import { useFreshnessClock } from './readiness';

export function RetirementApproval({ workspaceId, retirementId, review, preparation = false,
  initialReceipt, scope, store, guard, sessionToken }: {
  workspaceId: string; retirementId: string; review: { revision: string; approval_request: Record<string, unknown> | null };
  preparation?: boolean; initialReceipt: StoredReceipt; scope: ReceiptScope; store: ReceiptStore;
  guard: ScopeGuard; sessionToken: string;
}) {
  const [receipt, setReceipt] = useState(initialReceipt);
  const [approval, setApproval] = useState<OperationApproval | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [busy, setBusy] = useState(false);
  const now = useFreshnessClock();
  const intent = `${preparation ? 'prepare-retirement-access' : 'retire-workspace'}:${workspaceId}`;
  const current = () => Boolean(scope.principalId) && getAccessToken() === sessionToken;
  useEffect(() => {
    if (!receipt.approvalId) return;
    let active = true;
    void getApproval(guard, receipt.approvalId).then((result) => {
      if (active && getAccessToken() === sessionToken && result.ok && result.value.workspace_id === workspaceId) setApproval(result.value);
    });
    return () => { active = false; };
  }, [guard, receipt.approvalId, sessionToken, workspaceId]);
  const observe = async () => {
    if (!current()) return;
    const generation = guard.current();
    const result = receipt.operationId ? await getOperation(guard, receipt.operationId) : await recoverOperation(guard, receipt.idempotencyKey);
    if (isSuperseded(result) || !current()) return;
    if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
    if (result.value.idempotencyKey !== receipt.idempotencyKey || result.value.workspaceId !== workspaceId) {
      setProblem({ reason: 'unknown', detail: 'The operation did not match the saved removal request.' }); return;
    }
    try {
      const saved = await recordObservationExclusive(store, scope, intent, result.value);
      if (!guard.isCurrent(generation) || !current()) return;
      if (saved) setReceipt(saved);
      setProblem(null);
    } catch { if (current()) setProblem({ reason: 'unknown', detail: 'The observed status could not be saved. Keep the original request reference.' }); }
  };
  const ask = async () => {
    if (!current() || busy || !review.approval_request || receipt.submissionStage === 'submitted') return;
    setBusy(true);
    const generation = guard.current();
    try {
      const saved = await markSubmissionStage(store, scope, intent, receipt.idempotencyKey, 'approval');
      if (!saved || !guard.isCurrent(generation) || !current()) return;
      setReceipt(saved);
      const result = await requestApproval(guard, review.approval_request);
      if (isSuperseded(result) || !current()) return;
      if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
      if (result.value.workspace_id !== workspaceId || result.value.plan_digest !== review.revision) {
        setProblem({ reason: 'unknown', detail: 'The approval does not match the reviewed removal phase.' }); return;
      }
      const updated = await markSubmissionStage(store, scope, intent, receipt.idempotencyKey, 'approval', result.value.approval_id);
      if (!updated || !guard.isCurrent(generation) || !current()) return;
      setReceipt(updated); setApproval(result.value); setProblem(null);
    } catch { if (current()) setProblem({ reason: 'unknown', detail: 'The approval reference could not be saved. Recover the same review before retrying.' }); }
    finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  const approved = approval?.result === 'allowed-once' && !approval.revoked &&
    Date.parse(approval.expires_at) > now && approval.workspace_id === workspaceId && approval.plan_digest === review.revision;
  const submit = async (retry = false) => {
    const approvalId = retry ? receipt.approvalId : approval?.approval_id;
    if (!current() || busy || !approvalId || (!retry && (!approved || receipt.submissionStage === 'submitted'))) return;
    setBusy(true);
    const generation = guard.current();
    try {
      if (!retry) {
      const checked = preparation
        ? await previewRetirementAccess(guard, workspaceId, { operation_id: retirementId })
        : await previewRetirement(guard, workspaceId, { operation_id: retirementId });
      if ('superseded' in checked || !current()) return;
      if (!checked.ok) { if ('unavailable' in checked) setProblem(checked.unavailable); return; }
      if (checked.value.admission_available !== true || checked.value.revision !== review.revision) {
        setProblem({ reason: 'unknown', detail: 'The removal plan changed. Review it again before submitting.' }); return;
      }
      }
      const saved = await markSubmissionStage(store, scope, intent, receipt.idempotencyKey, 'submitted', approvalId);
      if (!saved || !guard.isCurrent(generation) || !current()) return;
      setReceipt(saved);
      const result = await submitRetirement(guard, workspaceId, {
        operation_id: retirementId, plan_revision: review.revision, approval_id: approvalId,
      }, preparation);
      if (isSuperseded(result) || !current()) return;
      if (!result.ok) {
        if ('unavailable' in result) setProblem(result.unavailable);
        return;
      }
      if (result.value.idempotencyKey !== saved.idempotencyKey) {
        setProblem({ reason: 'unknown', detail: 'The response did not match the saved request; recover its original status.' }); return;
      }
      const updated = await recordObservationExclusive(store, scope, intent, result.value);
      if (!updated || !guard.isCurrent(generation) || !current()) return;
      setReceipt(updated);
      if (updated.operationId) await recordRetirementLineage(store, scope, `retire-workspace:${workspaceId}`,
        retirementId, workspaceId, preparation ? { accessRequestId: updated.idempotencyKey, accessOperationId: updated.operationId }
          : { retirementOperationId: updated.operationId });
      if (guard.isCurrent(generation) && current()) setProblem(null);
    } catch { if (current()) setProblem({ reason: 'unknown', detail: 'The result is uncertain. Recover the saved request before any further removal action.' }); }
    finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  return <section className="space-y-3" aria-label={preparation ? 'Cleanup preparation approval' : 'Removal approval'}>
    <p className="break-all">Request reference: {receipt.idempotencyKey}</p>
    {receipt.submissionStage !== 'submitted' && (!approval || approval.plan_digest !== review.revision ||
      approval.revoked || Date.parse(approval.expires_at) <= now) && <Button disabled={busy} onClick={() => void ask()}>
      Request approval for {preparation ? 'cleanup preparation' : 'removal'}
    </Button>}
    {approval && <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />}
    {receipt.submissionStage !== 'submitted' && approval && <Button variant="danger" disabled={busy || !approved} onClick={() => void submit()}>
      {preparation ? 'Prepare approved removal' : 'Remove workspace'}
    </Button>}
    {receipt.submissionStage === 'submitted' && <>
      <p role="status">{preparation ? 'Preparation' : 'Removal'} operation: {receipt.state}.</p>
      <p>{preparation && receipt.state === 'succeeded' ? 'Preparation completed. Review removal to approve the saved destroy plan.'
        : 'Operation status alone does not establish provider absence or residual cost.'}</p>
      {receipt.operationId && <p className="break-all">Operation: {receipt.operationId}</p>}
      {!receipt.operationId && receipt.approvalId && <Button disabled={busy} onClick={() => void submit(true)}>Retry saved request</Button>}
      <Button variant="secondary" disabled={busy} onClick={() => void observe()}>{isTerminal(receipt.state) ? 'Recheck operation status' : 'Recover operation status'}</Button>
    </>}
    {problem && <Alert variant="warning" title="Removal phase unavailable">{problem.detail}</Alert>}
  </section>;
}
