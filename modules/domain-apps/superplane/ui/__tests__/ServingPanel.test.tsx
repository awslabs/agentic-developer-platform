import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { server } from '@/mocks/server';
import { ServingPanel } from '@superplane-ui/ServingPanel';
import { ScopeGuard } from '@superplane-ui/client';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import { memoryReceiptStore, readReceipt, type ReceiptStore } from '@superplane-ui/operations';
import { listDeployments, parseServingReview } from '@superplane-ui/workloads';

const api = (path: string) => `/api${DOMAIN_BASE}${path}`;
const root = '/workspaces/ws-1/deployments';
const scope = { deploymentId: 'test', orgId: 'org-a' };
const revision = 'a'.repeat(64);
const depId = '11111111-1111-4111-8111-111111111111';
const workload = { deployment_id: depId, name: 'test-model', status: 'Created', operation_id: 'paid-create', operation_state: 'succeeded' };
let store: ReceiptStore;
let reviewedRequest: Record<string, unknown>;

function review(requestId: string, action = 'provision') {
  const plan = { provider_account_id: '111122223333', region: 'us-east-1', namespace: 'workspace-one',
    workload: { kind: 'serving', image: 'test/serving@sha256:' + 'b'.repeat(64) } };
  return {
    deployment_id: depId, request_id: requestId, revision,
    controller_plan: plan,
    approval_request: {
      workspace_id: 'ws-1', action, idempotency_key: requestId,
      parameters: { controller_plan: JSON.stringify(plan), controller_deployment_id: depId,
        max_resource_units: action === 'teardown' ? '0' : '1', max_runtime_seconds: '900', max_cost_micros: action === 'teardown' ? '0' : '2000000' },
    },
  };
}
function approval(overrides = {}) {
  return { approval_id: 'approval-one', workspace_id: 'ws-1', result: 'allowed-once', can_decide: false,
    expires_at: '2099-01-01T00:00:00Z', revoked: false, request: reviewedRequest, plan_digest: revision,
    envelope: { max_resource_units: 1, max_runtime_seconds: 900, max_cost_micros: 2000000 }, ...overrides };
}

beforeEach(() => {
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
  store = memoryReceiptStore();
  reviewedRequest = review('unset').approval_request;
  server.use(
    http.get(api(root), () => HttpResponse.json({ workspace_id: 'ws-1', deployments: [workload] })),
    http.post(api(`${root}/preview`), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      const result = review(body.operation_id); reviewedRequest = result.approval_request;
      return HttpResponse.json(result);
    }),
    http.post(api(`${root}/${depId}/teardown-preview`), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      const result = review(body.operation_id, 'teardown'); reviewedRequest = result.approval_request;
      return HttpResponse.json(result);
    }),
    http.post(api('/operation-approvals'), () => HttpResponse.json(approval())),
    http.get(api('/operation-approvals/approval-one'), () => HttpResponse.json(approval())),
  );
});
const panel = (workspaceId = 'ws-1', mayManage = true) =>
  <ServingPanel workspaceId={workspaceId} scope={scope} store={store} mayManage={mayManage} />;

async function prepare(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Deployment name'), 'new-model');
  await user.type(screen.getByLabelText('Serving profile'), 'gpu-profile');
  await user.type(screen.getByLabelText('Model name'), 'organization/model');
  await user.click(screen.getByRole('button', { name: 'Prepare serving review' }));
  await user.click(screen.getByRole('button', { name: 'Review serving plan' }));
  await screen.findByText('Maximum additional cost: 2 USD. Observed cost: unknown.');
}
async function approve(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Request workload approval' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Submit approved deployment' })).toBeEnabled());
}

it('retains the exact request and approval through a lost create reply and reload', async () => {
  const submitted: Array<Record<string, unknown>> = [];
  let lost = true;
  server.use(http.post(api(root), async ({ request }) => {
    submitted.push(await request.json() as Record<string, unknown>);
    return lost ? HttpResponse.error() : HttpResponse.json({ ...workload, name: 'new-model' });
  }));
  const user = userEvent.setup();
  const first = render(panel());
  await prepare(user); await approve(user);
  await user.click(screen.getByRole('button', { name: 'Submit approved deployment' }));
  await screen.findByText('Workload request unavailable');
  const saved = readReceipt(store, scope, 'serving:ws-1:create:new-model');
  expect(saved).toMatchObject({ state: 'unknown', approvalId: 'approval-one', submissionStage: 'submitted' });
  first.unmount(); lost = false;
  render(panel());
  await prepare(user);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Submit approved deployment' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'Submit approved deployment' }));
  await waitFor(() => expect(readReceipt(store, scope, 'serving:ws-1:create:new-model')).toMatchObject({ state: 'succeeded', operationId: 'paid-create' }));
  expect(submitted).toHaveLength(2);
  expect(submitted[0]).toEqual(submitted[1]);
  expect(submitted[0]).toMatchObject({ operation_id: saved?.idempotencyKey, approval_id: 'approval-one', plan_revision: revision,
    name: 'new-model', profile_id: 'gpu-profile', model_name: 'organization/model', replicas: 1, max_model_len: null });
  expect(submitted[0]).not.toHaveProperty('namespace');
  const persisted = store.keys().map((key) => store.getItem(key)).join('');
  expect(persisted).not.toContain('organization/model');
  expect(persisted).not.toContain('test/serving');
  expect(screen.queryByText(/cleanup completed/i)).not.toBeInTheDocument();
});

it('submits an approved stop with its original deployment ID and never infers cleanup from success', async () => {
  const deletes: unknown[] = [];
  server.use(http.delete(api(`${root}/${depId}`), async ({ request }) => {
    deletes.push(await request.json());
    return HttpResponse.json({ name: 'test-model', status: 'Deleting', operation_id: 'paid-stop', operation_state: 'succeeded' });
  }));
  const user = userEvent.setup(); render(panel());
  await user.click(await screen.findByRole('button', { name: 'Review stop for test-model' }));
  await user.click(screen.getByRole('button', { name: 'Review stop plan' }));
  await user.click(await screen.findByRole('button', { name: 'Request workload approval' }));
  await user.click(await screen.findByRole('button', { name: 'Submit approved stop' }));
  await waitFor(() => expect(deletes).toHaveLength(1));
  expect(deletes[0]).toEqual({ operation_id: expect.any(String), approval_id: 'approval-one', plan_revision: revision });
  await waitFor(() => expect(readReceipt(store, scope, `serving:ws-1:stop:${depId}`)).toMatchObject({ state: 'succeeded', operationId: 'paid-stop' }));
  expect(screen.getByText(/Existing resources and charges remain unresolved/)).toBeInTheDocument();
  expect(screen.queryByText(/cleanup completed/i)).not.toBeInTheDocument();
});

it.each(['revoked', 'changed-plan', 'wrong-workspace'])('refuses %s before submission', async (change) => {
  const submit = vi.fn();
  server.use(http.post(api(root), () => { submit(); return HttpResponse.json(workload); }));
  const user = userEvent.setup(); render(panel());
  await prepare(user); await approve(user);
  if (change === 'changed-plan') {
    server.use(http.post(api(`${root}/preview`), async ({ request }) => {
      const body = await request.json() as { operation_id: string };
      return HttpResponse.json({ ...review(body.operation_id), revision: 'c'.repeat(64) });
    }));
  } else {
    server.use(http.get(api('/operation-approvals/approval-one'), () => HttpResponse.json(
      approval(change === 'revoked' ? { revoked: true } : { workspace_id: 'another-workspace' }))));
  }
  await user.click(screen.getByRole('button', { name: 'Submit approved deployment' }));
  await screen.findByText('Workload request unavailable');
  expect(submit).not.toHaveBeenCalled();
});

it('discards late lists after workspace changes and clears inaccessible status', async () => {
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  server.use(
    http.get(api(root), async () => { await waiting; return HttpResponse.json({ workspace_id: 'ws-1', deployments: [workload] }); }),
    http.get(api('/workspaces/ws-2/deployments'), () => HttpResponse.json({ workspace_id: 'ws-2', deployments: [{ ...workload, name: 'second-workspace-model' }] })),
  );
  const view = render(panel()); view.rerender(panel('ws-2')); release();
  await screen.findByText('second-workspace-model');
  expect(screen.queryByText('test-model')).not.toBeInTheDocument();
  server.use(http.get(api('/workspaces/ws-2/deployments'), () => new HttpResponse(null, { status: 403 })));
  await userEvent.click(screen.getByRole('button', { name: 'Refresh serving workloads' }));
  await screen.findByText('Workload status unavailable');
  expect(screen.queryByText('second-workspace-model')).not.toBeInTheDocument();
});

it('shows read-only users status without submit or stop controls', async () => {
  render(panel('ws-1', false));
  await screen.findByText('test-model');
  expect(screen.queryByRole('button', { name: /Review stop/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(screen.getByText('Cleanup and observed cost: not reported')).toBeInTheDocument();
});

it('rejects another workspace list and malformed or mismatched approval plans', async () => {
  server.use(http.get(api(root), () => HttpResponse.json({ workspace_id: 'wrong-workspace', deployments: [workload] })));
  expect((await listDeployments(new ScopeGuard(), 'ws-1')).ok).toBe(false);
  expect(parseServingReview(review('request'), 'ws-1', 'request', 'provision')).not.toBeNull();
  expect(parseServingReview(review('request'), 'ws-2', 'request', 'provision')).toBeNull();
  expect(parseServingReview(review('request'), 'ws-1', 'other-request', 'provision')).toBeNull();
  expect(parseServingReview(review('request'), 'ws-1', 'request', 'teardown')).toBeNull();
  const malformed = review('request');
  malformed.approval_request.parameters.max_cost_micros = 'unknown';
  expect(parseServingReview(malformed, 'ws-1', 'request', 'provision')).toBeNull();
});
