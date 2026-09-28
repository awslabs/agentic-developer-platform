import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { server } from '@/mocks/server';
import { LifecycleProposalPanel } from '@superplane-ui/LifecycleProposalPanel';
import { ScopeGuard, previewLifecycleProposal } from '@superplane-ui/client';
import { DOMAIN_BASE, ENDPOINTS } from '@superplane-ui/contract';
import { claimPreviewIdentity, markSubmissionStage, memoryReceiptStore, readReceipt, type ReceiptStore } from '@superplane-ui/operations';

const api = (path: string) => `/api${DOMAIN_BASE}${path}`;
const path = '/workspaces/ws-1/lifecycle-proposals';
const scope = { deploymentId: 'dev', orgId: 'org-a' };
const proposal = {
  status: 'awaiting_plan_approval', artifact_id: 'artifact-1', workspace_id: 'ws-1',
  source_operation_id: 'initial-operation', request_revision: 'original-revision',
  phase: 'apply-infrastructure', account_id: '111122223333', target: { region: 'us-east-1' },
  plan_file_sha256: 'a'.repeat(64), plan_json_sha256: 'b'.repeat(64),
  inventory: [{ address: 'aws_vpc.workspace', actions: ['create'] }], estimate: null,
};
const approvalRequest = {
  workspace_id: 'ws-1', action: 'provision', idempotency_key: 'continuation-1',
  parameters: { lifecycle_artifact_id: 'artifact-1', terraform_plan_file_sha256: proposal.plan_file_sha256,
    terraform_plan_sha256: proposal.plan_json_sha256, lifecycle_source_operation_id: 'initial-operation' },
};
const approval = {
  approval_id: 'approval-1', workspace_id: 'ws-1', result: 'allowed-once',
  can_decide: false, expires_at: '2099-01-01T00:00:00Z', revoked: false,
  request: approvalRequest, plan_digest: 'review-revision', envelope: {},
};
const review = { ...proposal, request_id: 'continuation-1', revision: 'review-revision', approval_request: approvalRequest };
const names: Array<keyof typeof ENDPOINTS> = [
  'listLifecycleProposals', 'previewLifecycleProposal', 'continueLifecycleProposal',
  'requestApproval', 'getApproval', 'getOperation', 'recoverOperation',
];
let restore: () => void;
let store: ReceiptStore;

beforeEach(() => {
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
  store = memoryReceiptStore();
  const saved = names.map((name) => [name, ENDPOINTS[name].served] as const);
  for (const name of names) (ENDPOINTS[name] as { served: boolean }).served = true;
  restore = () => { for (const [name, served] of saved) (ENDPOINTS[name] as { served: boolean }).served = served; };
  server.use(
    http.get(api(path), () => HttpResponse.json({ workspace_id: 'ws-1', proposals: [proposal] })),
    http.post(api(`${path}/artifact-1/preview`), () => HttpResponse.json(review)),
    http.post(api('/operation-approvals'), () => HttpResponse.json(approval)),
    http.get(api('/operation-approvals/approval-1'), () => HttpResponse.json(approval)),
    http.get(api('/operations/by-idempotency/continuation-1'), () => new HttpResponse(null, { status: 503 })),
    http.get(api('/operations/phase-operation'), () => HttpResponse.json({
      request_id: 'continuation-1', provisioning_operation_id: 'phase-operation', workspace_id: 'ws-1', state: 'running',
    })),
  );
});
afterEach(() => restore());

function panel(mintKey = vi.fn(() => 'continuation-1')) {
  return render(<LifecycleProposalPanel workspaceId="ws-1" scope={scope} store={store} mayManage mintKey={mintKey} />);
}
async function reachApproval(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Review this lifecycle plan' }));
  await user.click(await screen.findByRole('button', { name: 'Request approval for this phase' }));
  await screen.findByRole('button', { name: 'Continue approved phase' });
}

it('preserves one distinct continuation identity, exact approval and receipt across a lost reply and reload', async () => {
  const requests: unknown[] = [];
  const submissions: unknown[] = [];
  let lost = true;
  server.use(
    http.post(api('/operation-approvals'), async ({ request }) => {
      requests.push(await request.json());
      return HttpResponse.json(approval);
    }),
    http.post(api(`${path}/artifact-1/continue`), async ({ request }) => {
      submissions.push(await request.json());
      if (lost) return HttpResponse.error();
      return HttpResponse.json({ request_id: 'continuation-1', provisioning_operation_id: 'phase-operation', workspace_id: 'ws-1', state: 'succeeded' });
    }),
  );
  const user = userEvent.setup();
  const mint = vi.fn(() => 'continuation-1');
  const first = panel(mint);
  await reachApproval(user);
  expect(requests).toEqual([approvalRequest]);
  expect(screen.getByText(`Saved plan SHA-256: ${proposal.plan_file_sha256}`)).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Continue approved phase' }));
  await screen.findByText(/Lifecycle continuation unavailable/);
  expect(readReceipt(store, scope, 'continue:ws-1:artifact-1')).toMatchObject({ idempotencyKey: 'continuation-1', state: 'unknown', submissionStage: 'submitted' });
  first.unmount();
  lost = false;
  panel(mint);
  await user.click(await screen.findByRole('button', { name: 'Review this lifecycle plan' }));
  const resume = await screen.findByRole('button', { name: 'Continue approved phase' });
  await waitFor(() => expect(resume).toBeEnabled());
  await user.click(resume);
  await screen.findByText(/This phase completed/);
  expect(submissions).toEqual([
    { operation_id: 'continuation-1', approval_id: 'approval-1' },
    { operation_id: 'continuation-1', approval_id: 'approval-1' },
  ]);
  expect(mint).toHaveBeenCalledTimes(1);
  expect(readReceipt(store, scope, 'continue:ws-1:artifact-1')).toMatchObject({ operationId: 'phase-operation', state: 'succeeded' });
  expect(screen.queryByText(/workspace ready/i)).not.toBeInTheDocument();
});

it('refuses a changed reviewed revision before continuing', async () => {
  let changed = false;
  const submit = vi.fn();
  server.use(
    http.post(api(`${path}/artifact-1/preview`), () => HttpResponse.json({ ...review, revision: changed ? 'changed' : review.revision })),
    http.post(api(`${path}/artifact-1/continue`), () => { submit(); return HttpResponse.json({}); }),
  );
  const user = userEvent.setup();
  panel();
  await reachApproval(user);
  changed = true;
  await user.click(screen.getByRole('button', { name: 'Continue approved phase' }));
  await screen.findByText(/reviewed plan changed/);
  expect(submit).not.toHaveBeenCalled();
  expect(readReceipt(store, scope, 'continue:ws-1:artifact-1')?.submissionStage).toBe('approval');
});

it.each([
  { result: 'pending', expires_at: '2099-01-01T00:00:00Z' },
  { result: 'allowed-once', expires_at: '2000-01-01T00:00:00Z' },
  { result: 'rejected', expires_at: '2099-01-01T00:00:00Z' },
])('does not submit a requester-only or invalid approval: $result $expires_at', async (state) => {
  server.use(
    http.post(api('/operation-approvals'), () => HttpResponse.json({ ...approval, ...state })),
    http.get(api('/operation-approvals/approval-1'), () => HttpResponse.json({ ...approval, ...state })),
  );
  const user = userEvent.setup();
  panel();
  await reachApproval(user);
  expect(screen.getByRole('button', { name: 'Continue approved phase' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Approve this operation once' })).not.toBeInTheDocument();
});

it('rejects a preview whose exact approval names a different continuation identity', async () => {
  server.use(http.post(api(`${path}/artifact-1/preview`), () => HttpResponse.json({
    ...review, approval_request: { ...approvalRequest, idempotency_key: 'another-request' },
  })));
  const result = await previewLifecycleProposal(new ScopeGuard(), 'ws-1', 'artifact-1', 'continuation-1');
  expect(result.ok).toBe(false);
});

it('recovers a submitted continuation after the server advances past its source proposal', async () => {
  const intent = 'continue:ws-1:artifact-1';
  await claimPreviewIdentity(store, scope, intent, {}, () => 'continuation-1', new Date().toISOString());
  await markSubmissionStage(store, scope, intent, 'continuation-1', 'submitted', 'approval-1');
  server.use(
    http.get(api(path), () => HttpResponse.json({ workspace_id: 'ws-1', proposals: [] })),
    http.get(api('/operations/by-idempotency/continuation-1'), () => HttpResponse.json({
      request_id: 'continuation-1', provisioning_operation_id: 'phase-operation', workspace_id: 'ws-1', state: 'succeeded',
    })),
  );
  panel();
  await screen.findByText('Operation: phase-operation');
  expect(readReceipt(store, scope, intent)).toMatchObject({ operationId: 'phase-operation', state: 'succeeded' });
  expect(screen.getByText(/This phase completed/)).toBeInTheDocument();
});

it('discards a late preview after leaving the workspace', async () => {
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  server.use(
    http.post(api(`${path}/artifact-1/preview`), async () => { await waiting; return HttpResponse.json(review); }),
    http.get(api('/workspaces/ws-2/lifecycle-proposals'), () => HttpResponse.json({ workspace_id: 'ws-2', proposals: [] })),
  );
  const user = userEvent.setup();
  const view = panel();
  await user.click(await screen.findByRole('button', { name: 'Review this lifecycle plan' }));
  view.unmount();
  render(<LifecycleProposalPanel workspaceId="ws-2" scope={{ ...scope, orgId: 'org-b' }} store={store} mayManage={false} />);
  release();
  await screen.findByText('No next plan is available yet.');
  expect(readReceipt(store, { ...scope, orgId: 'org-b' }, 'continue:ws-1:artifact-1')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Request approval for this phase' })).not.toBeInTheDocument();
});
