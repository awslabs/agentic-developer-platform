import { useEffect, useRef, useState } from 'react';

import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';

import { isSuperseded, previewRetirement, recoverOperation, type ScopeGuard } from './client';
import { ENDPOINTS, type OperationReceipt, type OperationState, type RetirementReview, unavailableFor, type Unavailable } from './contract';
import { claimPreviewIdentity, readReceipt, recordRetirementLineage, type ClaimOutcome, type ReceiptScope, type ReceiptStore } from './operations';

type ReviewState =
  | { phase: 'idle' | 'loading' }
  | { phase: 'failed'; unavailable: Unavailable }
  | { phase: 'reviewed'; review: RetirementReview };

const OPERATION_PROGRESS: Record<OperationState, string> = {
  accepted: 'The request was accepted, not completed. Check the original request rather than submitting another removal.',
  running: 'Work may still be running. Check the original request rather than submitting another removal.',
  succeeded: 'The operation reported success, but provider absence and preserved resources have not been verified.',
  failed: 'The operation failed. Some owned resources may remain; keep the original request for authorized recovery.',
  cancelled: 'The operation was cancelled. Some owned resources may remain; keep the original request for authorized recovery.',
  unknown: 'The outcome is uncertain. Resources may still exist; recover the original request before any further action.',
};

export function RetirementPanel({ workspaceId, scope, store, guard, sessionToken }: {
  workspaceId: string;
  scope: ReceiptScope;
  store: ReceiptStore;
  guard: ScopeGuard;
  sessionToken: string;
}) {
  const [state, setState] = useState<ReviewState>({ phase: 'idle' });
  const [requestId, setRequestId] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<
    { phase: 'idle' | 'loading' } | { phase: 'observed'; receipt: OperationReceipt } |
    { phase: 'failed'; unavailable: Unavailable }
  >({ phase: 'idle' });
  const recoverRequest = async () => {
    if (!scope.principalId || getAccessToken() !== sessionToken) {
      setRecovery({ phase: 'failed', unavailable: {
        reason: 'not-permitted', detail: 'Your session changed. Sign in again to recover this removal request.',
      } });
      return;
    }
    let saved;
    try { saved = readReceipt(store, scope, `retire-workspace:${workspaceId}`); }
    catch { saved = null; }
    if (!saved || (requestId !== null && saved.idempotencyKey !== requestId)) {
      setRecovery({ phase: 'failed', unavailable: {
        reason: 'unknown', detail: 'The original scoped removal request could not be read. No new removal was submitted.',
      } });
      return;
    }
    setRequestId(saved.idempotencyKey);
    if (saved.submissionStage !== 'submitted') {
      setRecovery({ phase: 'failed', unavailable: {
        reason: 'unknown', detail: 'The original removal review is saved, but no removal was submitted. Review it again with the same request ID.',
      } });
      return;
    }
    const generation = guard.current();
    setRecovery({ phase: 'loading' });
    const result = await recoverOperation(guard, saved.idempotencyKey);
    if (isSuperseded(result) || !guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
    if (!result.ok) {
      if ('unavailable' in result) setRecovery({ phase: 'failed', unavailable: result.unavailable });
      return;
    }
    if (result.value.idempotencyKey !== saved.idempotencyKey || result.value.workspaceId !== workspaceId) {
      setRecovery({ phase: 'failed', unavailable: {
        reason: 'unknown', detail: 'The service returned a removal request for another workspace or identity. No new removal was submitted.',
      } });
      return;
    }
    if (result.value.operationId) {
      let savedLineage;
      try {
        savedLineage = await recordRetirementLineage(
          store, scope, `retire-workspace:${workspaceId}`, saved.idempotencyKey,
          workspaceId, { retirementOperationId: result.value.operationId },
        );
      } catch { savedLineage = null; }
      if (!guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
      if (!savedLineage) {
        setRecovery({ phase: 'failed', unavailable: {
          reason: 'unknown', detail: 'The original removal operation could not be saved safely. Keep the request ID and retry the lookup.',
        } });
        return;
      }
    }
    setRecovery({ phase: 'observed', receipt: result.value });
  };
  const reviewRef = useRef<HTMLDivElement>(null);
  const problemRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (state.phase === 'reviewed') reviewRef.current?.focus();
    if (state.phase === 'failed') problemRef.current?.focus();
  }, [state.phase]);

  const reviewRemoval = async () => {
    if (!ENDPOINTS.previewRetirement.served || state.phase === 'loading') return;
    if (!scope.principalId) {
      setState({ phase: 'failed', unavailable: {
        reason: 'not-permitted', detail: 'Your signed-in identity is unavailable. Sign in again before reviewing removal.',
      } });
      return;
    }
    if (getAccessToken() !== sessionToken) {
      setState({ phase: 'failed', unavailable: {
        reason: 'not-permitted', detail: 'Your session changed. Sign in and review the workspace again.',
      } });
      return;
    }
    const generation = guard.current();
    setState({ phase: 'loading' });
    let claim: ClaimOutcome;
    try {
      claim = await claimPreviewIdentity(
        store, scope, `retire-workspace:${workspaceId}`, { workspaceId },
        () => crypto.randomUUID(), new Date().toISOString(),
      );
    } catch {
      if (!guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
      setState({ phase: 'failed', unavailable: {
        reason: 'unknown',
        detail: 'Browser storage could not save or recover the removal review request. Restore storage access and retry review without clearing saved request references.',
      } });
      return;
    }
    if (!guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
    if (claim.kind === 'conflict') {
      setState({ phase: 'failed', unavailable: {
        reason: 'unknown', detail: claim.detail,
      } });
      return;
    }
    setRequestId(claim.receipt.idempotencyKey);
    const result = await previewRetirement(guard, workspaceId, {
      operation_id: claim.receipt.idempotencyKey,
    });
    if (isSuperseded(result) || !guard.isCurrent(generation) || getAccessToken() !== sessionToken) return;
    if (!result.ok) {
      if ('unavailable' in result) setState({ phase: 'failed', unavailable: result.unavailable });
      return;
    }
    setState({ phase: 'reviewed', review: result.value });
  };

  const review = state.phase === 'reviewed' ? state.review : null;
  const deletionSteps = review?.steps.filter((step) =>
    step.operation_kind.startsWith('delete-') || step.operation_kind.startsWith('revoke-')) ?? [];

  return (
    <section aria-label="Workspace retirement" className="min-w-0 rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <h3 className="font-semibold">Workspace retirement</h3>
      <div role="group" aria-label="Removal request" className="mt-3 space-y-2">
        {requestId && <p className="break-all">Saved removal request ID: {requestId}</p>}
        <Button variant="secondary" disabled={recovery.phase === 'loading'}
          onClick={() => void recoverRequest()}>Recover removal request</Button>
        {recovery.phase === 'loading' && <p role="status">Checking the original request without submitting another removal…</p>}
        {recovery.phase === 'failed' && <Alert variant="warning" title="Removal request status unavailable">
          {recovery.unavailable.detail} Keep the original request ID and retry the lookup; no new removal was submitted.
          Provider absence is not verified; residual cost is unknown.
        </Alert>}
        {recovery.phase === 'observed' && <div role="status">
          <p>Operation state: {recovery.receipt.state}</p>
          <p className="break-all">Server operation ID: {recovery.receipt.operationId ?? 'Not yet assigned'}</p>
          <p>Last observed: {Number.isFinite(Date.parse(recovery.receipt.observedAt))
            ? new Date(recovery.receipt.observedAt).toISOString() : 'Not reported'}</p>
          <p>{OPERATION_PROGRESS[recovery.receipt.state]}</p>
          <p>Verified removal: Not established. Operation status alone does not prove resource deletion or preservation.</p>
          <p>Residual cost: Unknown. Preserved or partially cleaned resources may continue to incur charges.</p>
        </div>}
      </div>
      <p className="mt-2 text-sm">Review owned deletions and resources that must survive before requesting removal. Reviewing does not delete anything.</p>
      {!ENDPOINTS.previewRetirement.served && (
        <Alert variant="warning" title="Removal review unavailable">
          {unavailableFor('previewRetirement').detail}
        </Alert>
      )}
      <Button variant="secondary" className="mt-3"
        disabled={!ENDPOINTS.previewRetirement.served || state.phase === 'loading'}
        onClick={() => void reviewRemoval()}>
        Review removal
      </Button>
      {state.phase === 'loading' && <p role="status">Reading the owned resource inventory…</p>}
      {state.phase === 'failed' && (
        <div ref={problemRef} tabIndex={-1} role="group" aria-label="Removal review problem">
          <Alert variant="warning" title="Removal review unavailable">
            {state.unavailable.detail} No removal was submitted. Resolve the reported problem before retrying review.
          </Alert>
        </div>
      )}
      {review && (
        <div ref={reviewRef} tabIndex={-1} role="group" className="mt-4 min-w-0 space-y-3 rounded p-2" aria-label="Retirement review">
          <dl className="grid min-w-0 gap-2 text-sm sm:grid-cols-2 [&>div]:min-w-0">
            <div><dt>Workspace ID</dt><dd className="break-all">{review.workspace_id}</dd></div>
            <div><dt>Review request ID</dt><dd className="break-all">{review.request_id}</dd></div>
            <div><dt>Original operation ID</dt><dd className="break-all">{review.source_operation_id}</dd></div>
            <div><dt>Lifecycle artifact ID</dt><dd className="break-all">{review.lifecycle_artifact_id}</dd></div>
            <div><dt>Target account</dt><dd className="break-all">{review.account_id}</dd></div>
            <div><dt>Region</dt><dd>{review.region}</dd></div>
            <div><dt>Reviewed plan revision</dt><dd className="break-all">{review.revision}</dd></div>
            <div><dt>Additional cost</dt><dd>Unknown; this preview has no cost estimate. Preserved resources may continue to incur charges.</dd></div>
          </dl>
          <div>
            <h4 className="font-semibold">Owned deletion steps</h4>
            {deletionSteps.length === 0 ? <p>No owned deletion steps were reported.</p> : (
              <ol className="list-decimal pl-5">{deletionSteps.map((step) => (
                <li key={step.step_id} className="[overflow-wrap:anywhere]">{step.operation_kind}: {step.target} ({step.provider})</li>
              ))}</ol>
            )}
          </div>
          <div>
            <h4 className="font-semibold">Preserved resources</h4>
            {review.preserved.length === 0 ? <p>No preserved resources were reported.</p> : (
              <ul className="list-disc pl-5">{review.preserved.map((resource) => (
                <li key={resource} className="[overflow-wrap:anywhere]">{resource}</li>
              ))}</ul>
            )}
            <p className="text-sm">These are planned survivors, not verified preservation evidence.</p>
          </div>
          <Alert variant="warning" title="Removal cannot be submitted">
            The service requires separately approved cleanup access before it can admit a complete removal plan. No approval request is available and no deletion was submitted.
          </Alert>
          <Button disabled aria-describedby="retirement-admission-unavailable">Remove workspace</Button>
          <p id="retirement-admission-unavailable" className="text-sm">Unavailable until the service provides a complete approved retirement plan.</p>
        </div>
      )}
    </section>
  );
}
