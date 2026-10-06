import { HttpResponse, http } from 'msw';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { server } from '@/mocks/server';
import { ScopeGuard } from '@superplane-ui/client';
import { DOMAIN_BASE, ENDPOINTS } from '@superplane-ui/contract';
import { claimPreviewIdentity, markSubmissionStage, memoryReceiptStore, readReceipt, recordObservation, type ReceiptStore } from '@superplane-ui/operations';
import { RetirementPanel } from '@superplane-ui/RetirementPanel';

const workspaceId = '6ac27035-3856-49d6-bb80-3c19b73a4511';
const API = (path: string) => `/api${DOMAIN_BASE}${path}`;
const scope = { orgId: 'org-a', deploymentId: window.location.origin, principalId: 'principal-a' };

function reviewFor(requestId: string) {
  return {
    request_id: requestId, workspace_id: workspaceId,
    source_operation_id: 'phase-before-removal', source_payload_digest: 'a'.repeat(64),
    lifecycle_artifact_id: 'artifact-1', account_id: '000000000000', region: 'example-region-1',
    inventory_sha256: 'b'.repeat(64), lifecycle_policy_sha256: 'c'.repeat(64),
    runtime_config_sha256: 'd'.repeat(64), revision: 'e'.repeat(64),
    steps: [
      { step_id: 'step-1', provider: 'superplane-governance', operation_kind: 'block-governed-admission', target: 'workspace' },
      { step_id: 'step-2', provider: 'superplane-kubernetes', operation_kind: 'delete-namespace', target: 'owned-namespace' },
    ],
    preserved: ['the supplied cluster remains owned by its operator'],
    admission_available: false, blocked_reason: 'staged_cleanup_access_required', approval_request: null,
  };
}

function mount(store: ReceiptStore = memoryReceiptStore(), guard = new ScopeGuard(), activeScope = scope, activeWorkspaceId = workspaceId) {
  render(<RetirementPanel workspaceId={activeWorkspaceId} scope={activeScope} store={store} guard={guard} sessionToken="test-token" />);
  return store;
}

const served = ENDPOINTS.previewRetirement.served;
beforeEach(() => {
  (ENDPOINTS.previewRetirement as { served: boolean }).served = true;
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
});
afterEach(() => {
  (ENDPOINTS.previewRetirement as { served: boolean }).served = served;
});

describe('C1 retirement preview and admission refusal', () => {
  it.each(['read', 'write', 'draft-write'] as const)('recovers from a receipt %s failure without losing a saved request identity', async (failure) => {
    const backing = memoryReceiptStore();
    const intent = `retire-workspace:${workspaceId}`;
    if (failure === 'read') {
      await claimPreviewIdentity(backing, scope, intent, { workspaceId }, () => 'saved-review-request', new Date().toISOString());
    }
    let unavailable = true;
    let writes = 0;
    const store: ReceiptStore = {
      ...backing,
      getItem(key) {
        if (unavailable && failure === 'read') throw new Error('private-storage-error-tripwire');
        return backing.getItem(key);
      },
      setItem(key, value) {
        writes += 1;
        if (unavailable && (failure === 'write' || (failure === 'draft-write' && writes === 2))) {
          throw new Error('private-storage-error-tripwire');
        }
        backing.setItem(key, value);
      },
    };
    const requestIds: string[] = [];
    server.use(http.post(API('/workspaces/:workspaceId/retirement/preview'), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      expect(readReceipt(backing, scope, intent)?.idempotencyKey).toBe(body.operation_id);
      requestIds.push(body.operation_id);
      return HttpResponse.json(reviewFor(body.operation_id));
    }));
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    const problem = await screen.findByRole('group', { name: 'Removal review problem' });
    expect(problem).toHaveTextContent(/browser storage/i);
    expect(problem).toHaveTextContent(/retry/i);
    expect(problem).not.toHaveTextContent('private-storage-error-tripwire');
    await waitFor(() => expect(problem).toHaveFocus());
    expect(screen.getByRole('button', { name: 'Review removal' })).toBeEnabled();
    expect(screen.queryByText(/Reading the owned resource inventory/)).toBeNull();
    expect(requestIds).toEqual([]);
    const saved = readReceipt(backing, scope, intent);
    if (failure === 'write') expect(saved).toBeNull();
    else expect(saved?.idempotencyKey).toBeTruthy();
    unavailable = false;
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    await screen.findByRole('group', { name: 'Retirement review' });
    if (saved) expect(requestIds).toEqual([saved.idempotencyKey]);
    cleanup();
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    await screen.findByRole('group', { name: 'Retirement review' });
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
    expect(backing.keys()).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Remove workspace' })).toBeDisabled();
  });

  it.each(['scope', 'session'] as const)('discards a receipt storage failure after the %s changes', async (change) => {
    const guard = new ScopeGuard();
    let reads = 0;
    let previews = 0;
    const store: ReceiptStore = {
      ...memoryReceiptStore(),
      getItem() {
        reads += 1;
        if (change === 'scope') guard.supersede();
        else window.sessionStorage.removeItem('cognito_access_token');
        throw new Error('private-storage-error-tripwire');
      },
    };
    server.use(http.post(API('/workspaces/:workspaceId/retirement/preview'), () => {
      previews += 1;
      return HttpResponse.json({});
    }));
    mount(store, guard);
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    expect(reads).toBe(1);
    expect(previews).toBe(0);
    expect(store.keys()).toHaveLength(0);
    expect(screen.queryByRole('group', { name: 'Removal review problem' })).toBeNull();
    expect(screen.queryByRole('group', { name: 'Retirement review' })).toBeNull();
  });

  it('shows the exact owned deletions, survivors and unknown cost without submitting deletion', async () => {
    const user = userEvent.setup();
    const requestIds: string[] = [];
    let admissions = 0;
    server.use(
      http.post(API('/workspaces/:workspaceId/retirement/preview'), async ({ params, request }) => {
        expect(params.workspaceId).toBe(workspaceId);
        const body = await request.json() as { operation_id: string };
        expect(body.operation_id).toMatch(/^[0-9a-f-]{36}$/);
        requestIds.push(body.operation_id);
        return HttpResponse.json(reviewFor(body.operation_id));
      }),
      http.post(API('/workspaces/:workspaceId/retirement'), () => {
        admissions += 1;
        return HttpResponse.json({ status: 'accepted' });
      }),
    );
    const store = mount();
    await user.click(screen.getByRole('button', { name: 'Review removal' }));
    const review = await screen.findByLabelText('Retirement review');
    expect(within(review).getByText(/delete-namespace: owned-namespace/)).toBeInTheDocument();
    expect(within(review).queryByText(/block-governed-admission: workspace/)).toBeNull();
    expect(within(review).getByText(/supplied cluster remains owned by its operator/)).toBeInTheDocument();
    expect(within(review).getByText(/Unknown; this preview has no cost estimate/)).toBeInTheDocument();
    expect(within(review).getByText(/No approval request is available/)).toBeInTheDocument();
    expect(within(review).getByRole('region', { name: 'Retirement approval' })).toContainElement(
      screen.getByRole('button', { name: 'Remove workspace' }),
    );
    expect(within(review).getByRole('button', { name: 'Remove workspace' })).toBeDisabled();
    expect(admissions).toBe(0);
    await user.click(screen.getByRole('button', { name: 'Review removal' }));
    await waitFor(() => expect(requestIds).toHaveLength(2));
    expect(requestIds[1]).toBe(requestIds[0]);
    expect(store.keys()).toHaveLength(1);
  });

  it('moves keyboard focus from review to the blocked plan without submitting deletion', async () => {
    const user = userEvent.setup();
    server.use(http.post(API('/workspaces/:workspaceId/retirement/preview'), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      return HttpResponse.json(reviewFor(body.operation_id));
    }));
    mount();
    const trigger = screen.getByRole('button', { name: 'Review removal' });
    trigger.focus();
    await user.keyboard('{Enter}');
    const review = await screen.findByRole('group', { name: 'Retirement review' });
    await waitFor(() => expect(review).toHaveFocus());
    expect(within(review).getByRole('button', { name: 'Remove workspace' })).toBeDisabled();
  });

  it.each([403, 503])('reports a %i preview refusal and sends no admission request', async (status) => {
    let admissions = 0;
    server.use(
      http.post(API('/workspaces/:workspaceId/retirement/preview'), () => HttpResponse.json({ detail: 'retirement review unavailable' }, { status })),
      http.post(API('/workspaces/:workspaceId/retirement'), () => {
        admissions += 1;
        return HttpResponse.json({ status: 'accepted' });
      }),
    );
    mount();
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    expect(await screen.findByText(/No removal was submitted/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('group', { name: 'Removal review problem' })).toHaveFocus());
    expect(screen.queryByLabelText('Retirement review')).toBeNull();
    expect(admissions).toBe(0);
  });

  it('refuses a response claiming admission is available under the still-blocked contract', async () => {
    let admissions = 0;
    server.use(
      http.post(API('/workspaces/:workspaceId/retirement/preview'), async ({ request }) => {
        const body = await request.json() as { operation_id: string };
        return HttpResponse.json({ ...reviewFor(body.operation_id), admission_available: true });
      }),
      http.post(API('/workspaces/:workspaceId/retirement'), () => {
        admissions += 1;
        return HttpResponse.json({ status: 'accepted' });
      }),
    );
    mount();
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    expect(await screen.findByText(/No removal was submitted/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove workspace' })).toBeNull();
    expect(admissions).toBe(0);
  });

  it('refuses review after a revoked session without claiming a receipt or submitting a request', async () => {
    let previews = 0;
    server.use(http.post(API('/workspaces/:workspaceId/retirement/preview'), () => {
      previews += 1;
      return HttpResponse.json({});
    }));
    const store = mount();
    window.sessionStorage.removeItem('cognito_access_token');
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    expect(await screen.findByText(/session changed/i)).toBeInTheDocument();
    expect(previews).toBe(0);
    expect(store.keys()).toHaveLength(0);
  });

  it('discards a retirement response after the session changes and never offers deletion', async () => {
    let release!: () => void;
    let requested!: () => void;
    const started = new Promise<void>((resolve) => { requested = resolve; });
    server.use(http.post(API('/workspaces/:workspaceId/retirement/preview'), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      await new Promise<void>((resolve) => { release = resolve; requested(); });
      return HttpResponse.json(reviewFor(body.operation_id));
    }));
    const store = mount();
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    await started;
    window.sessionStorage.setItem('cognito_access_token', 'different-session');
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByLabelText('Retirement review')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove workspace' })).toBeNull();
    expect(store.keys()).toHaveLength(1);
  });

  it('disables review when the proxy does not serve the preview route', () => {
    (ENDPOINTS.previewRetirement as { served: boolean }).served = false;
    mount();
    expect(screen.getByRole('button', { name: 'Review removal' })).toBeDisabled();
    expect(screen.getByText(/does not support reviewing the exact workspace retirement inventory/i)).toBeInTheDocument();
  });
});


describe('source-backed removal request re-entry', () => {
  const originalRequestId = 'bd2c9abd-b27b-4ab0-a521-0828d8e0ae39';

  async function savedRequest(submitted: boolean) {
    const store = memoryReceiptStore();
    const intent = `retire-workspace:${workspaceId}`;
    await claimPreviewIdentity(store, scope, intent, { workspaceId }, () => originalRequestId, new Date().toISOString());
    if (submitted) await markSubmissionStage(store, scope, intent, originalRequestId, 'submitted');
    return store;
  }

  it('uses the original scoped request after refresh and does not mistake running for removed', async () => {
    const store = await savedRequest(true);
    const lookups: string[] = [];
    let admissions = 0;
    server.use(
      http.get(API('/operations/by-idempotency/:requestId'), ({ params }) => {
        lookups.push(String(params.requestId));
        return HttpResponse.json({
          request_id: originalRequestId, provisioning_operation_id: 'server-operation-id',
          workspace_id: workspaceId, state: 'running', phase: 'execution', retryable: false,
        });
      }),
      http.post(API('/workspaces/:workspaceId/retirement'), () => {
        admissions += 1;
        return HttpResponse.json({});
      }),
    );
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText('Operation state: running')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Retirement outcome' })).toHaveTextContent('Verified removal: Not established');
    expect(screen.getByText('Server operation ID: server-operation-id')).toBeInTheDocument();
    expect(screen.getByText(/Operation status alone does not prove resource deletion/i)).toBeInTheDocument();
    cleanup();
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText('Operation state: running')).toBeInTheDocument();
    expect(lookups).toEqual([originalRequestId, originalRequestId]);
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.idempotencyKey).toBe(originalRequestId);
    expect(store.keys()).toHaveLength(1);
    expect(admissions).toBe(0);
  });

  it('refuses a substituted workspace operation without clearing the original receipt', async () => {
    const store = await savedRequest(true);
    server.use(http.get(API('/operations/by-idempotency/:requestId'), () => HttpResponse.json({
      request_id: originalRequestId, provisioning_operation_id: 'other-operation',
      workspace_id: 'other-workspace', state: 'succeeded', phase: 'execution',
    })));
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/another workspace or identity/i)).toBeInTheDocument();
    expect(screen.queryByText('Operation state: succeeded')).toBeNull();
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.idempotencyKey).toBe(originalRequestId);
  });

  it('does not look up a review-only identity as though it were submitted', async () => {
    const store = await savedRequest(false);
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/original removal review is saved, but no removal was submitted/i)).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Retirement outcome' })).toHaveTextContent('residual cost is unknown');
    expect(screen.getByText(`Saved removal request ID: ${originalRequestId}`)).toBeInTheDocument();
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.idempotencyKey).toBe(originalRequestId);
  });
});

describe('retirement principal and scope recovery', () => {
  const originalRequestId = 'fc44b266-78c2-412a-b04a-e33770575776';

  async function uncertainReceipt() {
    const store = memoryReceiptStore();
    const intent = `retire-workspace:${workspaceId}`;
    await claimPreviewIdentity(store, scope, intent, { workspaceId }, () => originalRequestId, new Date().toISOString());
    await markSubmissionStage(store, scope, intent, originalRequestId, 'submitted');
    recordObservation(store, scope, intent, { idempotencyKey: originalRequestId, operationId: null, state: 'unknown', workspaceId });
    return store;
  }

  it('preserves an uncertain request through A to B to A without revealing it to B', async () => {
    const store = await uncertainReceipt();
    let lookups = 0;
    server.use(http.get(API('/operations/by-idempotency/:requestId'), () => {
      lookups += 1;
      return HttpResponse.json({ detail: 'operation not currently readable' }, { status: 503 });
    }));
    mount(store, new ScopeGuard(), { ...scope, principalId: 'principal-b' });
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/original scoped removal request could not be read/i)).toBeInTheDocument();
    expect(lookups).toBe(0);
    cleanup();
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/Keep the original request ID and retry the lookup/i)).toBeInTheDocument();
    expect(lookups).toBe(1);
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)).toMatchObject({
      idempotencyKey: originalRequestId, state: 'unknown', submissionStage: 'submitted',
    });
  });

  it('does not recover a different workspace from the same signed-in scope', async () => {
    const store = await uncertainReceipt();
    mount(store, new ScopeGuard(), scope, 'different-workspace');
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/original scoped removal request could not be read/i)).toBeInTheDocument();
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.idempotencyKey).toBe(originalRequestId);
  });
  it('drops a late recovery reply after the session and scope change', async () => {
    const store = await uncertainReceipt();
    const guard = new ScopeGuard();
    let release!: () => void;
    let requested!: () => void;
    const started = new Promise<void>((resolve) => { requested = resolve; });
    const pending = new Promise<void>((resolve) => { release = resolve; });
    server.use(http.get(API('/operations/by-idempotency/:requestId'), async () => {
      requested();
      await pending;
      return HttpResponse.json({
        request_id: originalRequestId, provisioning_operation_id: 'late-operation',
        workspace_id: workspaceId, state: 'succeeded', phase: 'execution',
      });
    }));
    mount(store, guard);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    await started;
    guard.supersede();
    window.sessionStorage.removeItem('cognito_access_token');
    release();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('Operation state: succeeded')).toBeNull();
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)).toMatchObject({
      idempotencyKey: originalRequestId, state: 'unknown', operationId: null,
    });
  });

  it('refuses removal review without a current principal identity', async () => {
    const store = memoryReceiptStore();
    mount(store, new ScopeGuard(), { deploymentId: scope.deploymentId, orgId: scope.orgId });
    await userEvent.click(screen.getByRole('button', { name: 'Review removal' }));
    expect(await screen.findByText(/signed-in identity is unavailable/i)).toBeInTheDocument();
    expect(store.keys()).toHaveLength(0);
  });
});


describe('retirement progress is not cleanup proof', () => {
  const originalRequestId = '0ea80a51-7003-471a-a068-19741601856f';

  it.each(['accepted', 'running', 'succeeded', 'failed', 'cancelled', 'unknown'] as const)(
    'reports %s independently from verified absence and residual cost', async (operationState) => {
      const store = memoryReceiptStore();
      const intent = `retire-workspace:${workspaceId}`;
      await claimPreviewIdentity(store, scope, intent, { workspaceId }, () => originalRequestId, new Date().toISOString());
      await markSubmissionStage(store, scope, intent, originalRequestId, 'submitted');
      let admissions = 0;
      server.use(
        http.get(API('/operations/by-idempotency/:requestId'), ({ params }) => {
          expect(params.requestId).toBe(originalRequestId);
          return HttpResponse.json({
            request_id: originalRequestId, provisioning_operation_id: 'operation-1',
            workspace_id: workspaceId, state: operationState, phase: 'execution',
            observed_at: '2026-10-05T11:25:00Z', retryable: false,
          });
        }),
        http.post(API('/workspaces/:workspaceId/retirement'), () => {
          admissions += 1;
          return HttpResponse.json({});
        }),
      );
      mount(store);
      await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
      expect(await screen.findByText(`Operation state: ${operationState}`)).toBeInTheDocument();
      expect(screen.getByText('Verified removal: Not established. Operation status alone does not prove resource deletion or preservation.')).toBeInTheDocument();
      expect(screen.getByText(/Residual cost: Unknown/)).toBeInTheDocument();
      expect(screen.getByText('Last observed: 2026-10-05T11:25:00.000Z')).toBeInTheDocument();
      if (operationState === 'succeeded') expect(screen.getByText(/provider absence and preserved resources have not been verified/i)).toBeInTheDocument();
      if (operationState === 'failed' || operationState === 'cancelled') expect(screen.getByText(/Some owned resources may remain/i)).toBeInTheDocument();
      expect(admissions).toBe(0);
      expect(readReceipt(store, scope, intent)?.retirementOperationId).toBe('operation-1');
    },
  );

  it('leaves 503 recovery and an unfinished preview unverified rather than calling them zero-cost cleanup', async () => {
    const store = memoryReceiptStore();
    const intent = `retire-workspace:${workspaceId}`;
    await claimPreviewIdentity(store, scope, intent, { workspaceId }, () => originalRequestId, new Date().toISOString());
    await markSubmissionStage(store, scope, intent, originalRequestId, 'submitted');
    server.use(http.get(API('/operations/by-idempotency/:requestId'), () => HttpResponse.json({ detail: 'operation status unavailable' }, { status: 503 })));
    mount(store);
    await userEvent.click(screen.getByRole('button', { name: 'Recover removal request' }));
    expect(await screen.findByText(/Provider absence is not verified; residual cost is unknown/i)).toBeInTheDocument();
    expect(readReceipt(store, scope, intent)?.idempotencyKey).toBe(originalRequestId);
  });
});
