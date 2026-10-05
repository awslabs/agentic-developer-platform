import { useEffect, useRef, useState } from 'react';

import { Alert, Button } from '@/components/ui';
import { getAccessToken } from '@/services/auth';

import { isSuperseded, previewRetirement, type ScopeGuard } from './client';
import { ENDPOINTS, type RetirementReview, unavailableFor, type Unavailable } from './contract';
import { claimPreviewIdentity, type ClaimOutcome, type ReceiptScope, type ReceiptStore } from './operations';

type ReviewState =
  | { phase: 'idle' | 'loading' }
  | { phase: 'failed'; unavailable: Unavailable }
  | { phase: 'reviewed'; review: RetirementReview };

export function RetirementPanel({ workspaceId, scope, store, guard, sessionToken }: {
  workspaceId: string;
  scope: ReceiptScope;
  store: ReceiptStore;
  guard: ScopeGuard;
  sessionToken: string;
}) {
  const [state, setState] = useState<ReviewState>({ phase: 'idle' });
  const reviewRef = useRef<HTMLDivElement>(null);
  const problemRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (state.phase === 'reviewed') reviewRef.current?.focus();
    if (state.phase === 'failed') problemRef.current?.focus();
  }, [state.phase]);

  const reviewRemoval = async () => {
    if (!ENDPOINTS.previewRetirement.served || state.phase === 'loading') return;
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
    <section aria-label="Workspace retirement" className="rounded-lg border border-gray-200 p-4 dark:border-gray-700">
      <h3 className="font-semibold">Workspace retirement</h3>
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
        <div ref={reviewRef} tabIndex={-1} role="group" className="mt-4 space-y-3" aria-label="Retirement review">
          <dl className="grid gap-2 text-sm sm:grid-cols-2">
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
                <li key={step.step_id} className="break-words">{step.operation_kind}: {step.target} ({step.provider})</li>
              ))}</ol>
            )}
          </div>
          <div>
            <h4 className="font-semibold">Preserved resources</h4>
            {review.preserved.length === 0 ? <p>No preserved resources were reported.</p> : (
              <ul className="list-disc pl-5">{review.preserved.map((resource) => (
                <li key={resource} className="break-words">{resource}</li>
              ))}</ul>
            )}
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
