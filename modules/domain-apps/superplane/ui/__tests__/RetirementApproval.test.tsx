import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, expect, it } from 'vitest';
import { server } from '@/mocks/server';
import { RetirementApproval } from '@superplane-ui/RetirementApproval';
import { ScopeGuard } from '@superplane-ui/client';
import { claimPreviewIdentity, memoryReceiptStore, readReceipt } from '@superplane-ui/operations';

const workspaceId = 'workspace-1';
const retirementId = '723ee352-087b-4935-92d2-9abeb4f36512';
const scope = { deploymentId: window.location.origin, orgId: 'org-1', principalId: 'human-1' };
const revision = 'a'.repeat(64);
const review = {
  request_id: retirementId, workspace_id: workspaceId, source_operation_id: 'bootstrap',
  source_payload_digest: 'b'.repeat(64), lifecycle_artifact_id: 'c'.repeat(64),
  account_id: '123456789012', region: 'us-east-1', inventory_sha256: 'd'.repeat(64),
  lifecycle_policy_sha256: 'e'.repeat(64), runtime_config_sha256: 'f'.repeat(64),
  steps: [], preserved: ['AWS account'], admission_available: true, blocked_reason: null, revision,
  approval_request: { workspace_id: workspaceId, action: 'teardown', idempotency_key: retirementId,
    parameters: { lifecycle_phase: 'retire-workspace' } },
};
beforeEach(() => window.sessionStorage.setItem('cognito_access_token', 'test-token'));

it.each([false, true])('binds second approval and saved request; changed review refuses before submission (%s)', async (changed) => {
  const store = memoryReceiptStore();
  const claim = await claimPreviewIdentity(store, scope, `retire-workspace:${workspaceId}`, { workspaceId },
    () => retirementId, new Date().toISOString());
  if (claim.kind === 'conflict') throw new Error('fixture claim failed');
  const approval = { approval_id: 'approval-1', workspace_id: workspaceId, result: 'allowed-once',
    plan_digest: revision, expires_at: '2999-01-01T00:00:00Z', revoked: false,
    request: review.approval_request, envelope: { max_resource_units: 0, max_cost_micros: 0, max_runtime_seconds: 3600 } };
  let submitted = 0;
  server.use(
    http.post('/api/superplane/v1/operation-approvals', async ({ request }) => {
      expect(await request.json()).toEqual(review.approval_request);
      return HttpResponse.json(approval);
    }),
    http.get('/api/superplane/v1/operation-approvals/approval-1', () => HttpResponse.json(approval)),
    http.post('/api/superplane/v1/workspaces/workspace-1/retirement/preview', () => HttpResponse.json({ ...review, revision: changed ? '0'.repeat(64) : revision })),
    http.post('/api/superplane/v1/workspaces/workspace-1/retirement', async ({ request }) => {
      submitted += 1;
      expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.submissionStage).toBe('submitted');
      expect(await request.json()).toEqual({ operation_id: retirementId, plan_revision: revision, approval_id: 'approval-1' });
      return HttpResponse.json({ request_id: retirementId, workspace_id: workspaceId, operation_id: 'removal-1',
        phase: 'retire-workspace', state: 'accepted', retirement_complete: false });
    }),
  );
  render(<RetirementApproval workspaceId={workspaceId} retirementId={retirementId} review={review}
    initialReceipt={claim.receipt} scope={scope} store={store} guard={new ScopeGuard()} sessionToken="test-token" />);
  expect(screen.queryByRole('button', { name: 'Remove workspace' })).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Request approval for removal' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Remove workspace' }));
  if (changed) {
    expect(await screen.findByText(/removal plan changed/i)).toBeInTheDocument();
    expect(submitted).toBe(0);
  } else {
    await waitFor(() => expect(submitted).toBe(1));
    expect(await screen.findByText('Removal operation: accepted.')).toBeInTheDocument();
    expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.retirementOperationId).toBe('removal-1');
  }
});

it('retries an uncertain submission with its original request and approval', async () => {
  const store = memoryReceiptStore();
  const claim = await claimPreviewIdentity(store, scope, `retire-workspace:${workspaceId}`, { workspaceId },
    () => retirementId, new Date().toISOString());
  if (claim.kind === 'conflict') throw new Error('fixture claim failed');
  const approval = { approval_id: 'approval-1', workspace_id: workspaceId, result: 'allowed-once',
    plan_digest: revision, expires_at: '2999-01-01T00:00:00Z', revoked: false,
    request: review.approval_request, envelope: { max_resource_units: 0, max_cost_micros: 0, max_runtime_seconds: 3600 } };
  let submitted = 0;
  let previews = 0;
  server.use(
    http.post('/api/superplane/v1/operation-approvals', () => HttpResponse.json(approval)),
    http.get('/api/superplane/v1/operation-approvals/approval-1', () => HttpResponse.json(approval)),
    http.post('/api/superplane/v1/workspaces/workspace-1/retirement/preview', () => {
      previews += 1; return HttpResponse.json(review);
    }),
    http.post('/api/superplane/v1/workspaces/workspace-1/retirement', async ({ request }) => {
      submitted += 1;
      expect(await request.json()).toEqual({ operation_id: retirementId, plan_revision: revision, approval_id: 'approval-1' });
      if (submitted === 1) return new HttpResponse(null, { status: 503 });
      return HttpResponse.json({ request_id: retirementId, workspace_id: workspaceId, operation_id: 'removal-1',
        phase: 'retire-workspace', state: 'accepted', retirement_complete: false });
    }),
  );
  render(<RetirementApproval workspaceId={workspaceId} retirementId={retirementId} review={review}
    initialReceipt={claim.receipt} scope={scope} store={store} guard={new ScopeGuard()} sessionToken="test-token" />);
  await userEvent.click(screen.getByRole('button', { name: 'Request approval for removal' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Remove workspace' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Retry saved request' }));
  expect(await screen.findByText('Removal operation: accepted.')).toBeInTheDocument();
  expect(submitted).toBe(2);
  expect(previews).toBe(1);
  expect(readReceipt(store, scope, `retire-workspace:${workspaceId}`)?.idempotencyKey).toBe(retirementId);
});
