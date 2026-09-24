import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { BatchPanel } from '@superplane-ui/BatchPanel';
import { ScopeGuard } from '@superplane-ui/client';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import { memoryReceiptStore, readReceipt, type ReceiptStore } from '@superplane-ui/operations';
import { listBatchJobs, parseServingReview } from '@superplane-ui/workloads';

const api = (path: string) => `/api${DOMAIN_BASE}${path}`;
const root = '/workspaces/ws-1/batch-jobs';
const scope = { deploymentId: 'test', orgId: 'org-a' };
const revision = 'a'.repeat(64);
const jobId = '11111111-1111-4111-8111-111111111111';
const options = { image: 'test/batch@sha256:' + 'b'.repeat(64), command: ['/app/run'], args: ['--input', '/app/data.json'], gpu_count: 1, cpu: '2000m', memory: '8Gi' };
const job = { job_id: jobId, name: 'earlier-job', status: 'Created', operation_id: 'paid-create', operation_state: 'succeeded', provider_uid: 'original-job-uid' };
let store: ReceiptStore;
let reviewedRequest: Record<string, unknown>;

function review(requestId: string, action = 'provision') {
  const plan = { provider_account_id: '111122223333', region: 'us-east-1', namespace: 'workspace-one', workload: { kind: 'batch', ...options, port: null, auth_secret: null } };
  return { deployment_id: jobId, job_id: jobId, request_id: requestId, revision,
    approval_request: { workspace_id: 'ws-1', action, idempotency_key: requestId,
      parameters: { controller_plan: JSON.stringify(plan), controller_deployment_id: jobId,
        max_resource_units: action === 'teardown' ? '0' : '1', max_runtime_seconds: '900', max_cost_micros: action === 'teardown' ? '0' : '2000000' } } };
}
function approval(overrides = {}) {
  return { approval_id: 'approval-one', workspace_id: 'ws-1', result: 'allowed-once', can_decide: false,
    expires_at: '2099-01-01T00:00:00Z', revoked: false, request: reviewedRequest, plan_digest: revision,
    envelope: { max_resource_units: 1, max_runtime_seconds: 900, max_cost_micros: 2000000 }, ...overrides };
}
beforeEach(() => {
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
  store = memoryReceiptStore(); reviewedRequest = review('unset').approval_request;
  server.use(
    http.get(api('/workspaces/:workspaceId/batch-profiles'), ({ params }) => HttpResponse.json({ workspace_id: params.workspaceId, can_submit: true, can_review_teardown: true,
      profiles: [{ profile_id: 'batch-profile', image: options.image, batch_options: options }] })),
    http.get(api(root), () => HttpResponse.json({ workspace_id: 'ws-1', jobs: [job], truncated: false })),
    http.post(api(`${root}/preview`), async ({ request }) => {
      const body = await request.json() as { operation_id: string }; const value = review(body.operation_id); reviewedRequest = value.approval_request; return HttpResponse.json(value);
    }),
    http.post(api(`${root}/${jobId}/teardown-preview`), async ({ request }) => {
      const body = await request.json() as { operation_id: string }; const value = review(body.operation_id, 'teardown'); reviewedRequest = value.approval_request; return HttpResponse.json(value);
    }),
    http.post(api('/operation-approvals'), () => HttpResponse.json(approval())),
    http.get(api('/operation-approvals/approval-one'), () => HttpResponse.json(approval())),
    http.get(api('/operations/by-idempotency/:requestId'), () => new HttpResponse(null, { status: 503 })),
    http.get(api('/operations/:operationId'), () => new HttpResponse(null, { status: 503 })),
  );
});
const panel = (workspaceId = 'ws-1') => <BatchPanel workspaceId={workspaceId} scope={scope} store={store} />;
async function prepare(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(/Job name/), 'new-job');
  await user.selectOptions(screen.getByLabelText('Batch profile'), 'batch-profile');
  await user.click(screen.getByRole('button', { name: 'Prepare batch review' }));
  await user.click(screen.getByRole('button', { name: 'Review batch plan' }));
  await screen.findByText('Maximum additional cost: 2 USD. Observed cost: unknown.');
}

it('keeps batch invocation and original request after a lost reply and reload without a serving mutation', async () => {
  const submitted: unknown[] = [];
  let lost = true;
  const wrongRoute = vi.fn();
  server.use(
    http.post(api('/workspaces/ws-1/deployments'), () => { wrongRoute(); return HttpResponse.json({}); }),
    http.post(api(root), async ({ request }) => { submitted.push(await request.json()); return lost ? HttpResponse.error() : HttpResponse.json(job); }),
  );
  const user = userEvent.setup(); const view = render(panel());
  await prepare(user);
  await user.click(screen.getByRole('button', { name: 'Request workload approval' }));
  await user.click(await screen.findByRole('button', { name: 'Submit approved batch job' }));
  await screen.findByText('Workload request unavailable');
  const saved = readReceipt(store, scope, 'batch:ws-1:create:new-job');
  expect(saved).toMatchObject({ state: 'unknown', submissionStage: 'submitted' });
  view.unmount(); lost = false; render(panel());
  await prepare(user);
  await user.click(await screen.findByRole('button', { name: 'Submit approved batch job' }));
  await waitFor(() => expect(submitted).toHaveLength(2));
  expect(submitted[0]).toEqual(submitted[1]);
  expect(submitted[0]).toMatchObject({ operation_id: saved?.idempotencyKey, batch_options: options, name: 'new-job', approval_id: 'approval-one' });
  expect(wrongRoute).not.toHaveBeenCalled();
  expect(readReceipt(store, scope, 'serving:ws-1:create:new-job')).toBeNull();
  expect(store.keys().map((key) => store.getItem(key)).join('')).not.toContain('/app/data.json');
});

it('stops by original Job identity and keeps cleanup unconfirmed on operation success', async () => {
  const deletes: unknown[] = [];
  server.use(http.delete(api(`${root}/${jobId}`), async ({ request }) => { deletes.push(await request.json()); return HttpResponse.json({ ...job, status: 'Deleting', operation_id: 'paid-stop' }); }));
  const user = userEvent.setup(); render(panel());
  await user.click(await screen.findByRole('button', { name: 'Review stop for earlier-job' }));
  await user.click(screen.getByRole('button', { name: 'Review stop plan' }));
  await user.click(await screen.findByRole('button', { name: 'Request workload approval' }));
  await user.click(await screen.findByRole('button', { name: 'Submit approved stop' }));
  await waitFor(() => expect(deletes).toHaveLength(1));
  expect(deletes[0]).toEqual({ operation_id: expect.any(String), approval_id: 'approval-one', plan_revision: revision });
  expect(screen.queryByText(/confirmed by the backend/)).not.toBeInTheDocument();
  expect(screen.getByText(/Observed cost: unknown. Cleanup: unconfirmed/)).toBeInTheDocument();
});

it('discards stale workspace responses and clears status when access is revoked', async () => {
  let release!: () => void; const waiting = new Promise<void>((resolve) => { release = resolve; });
  server.use(http.get(api(root), async () => { await waiting; return HttpResponse.json({ workspace_id: 'ws-1', jobs: [job], truncated: false }); }),
    http.get(api('/workspaces/ws-2/batch-jobs'), () => HttpResponse.json({ workspace_id: 'ws-2', jobs: [{ ...job, name: 'second-job' }], truncated: true })));
  const view = render(panel()); view.rerender(panel('ws-2')); release();
  await screen.findByText('second-job'); expect(screen.queryByText('earlier-job')).not.toBeInTheDocument();
  expect(screen.getByText(/Showing the latest 100 jobs/)).toBeInTheDocument();
  server.use(http.get(api('/workspaces/ws-2/batch-jobs'), () => new HttpResponse(null, { status: 403 })));
  await userEvent.click(screen.getByRole('button', { name: 'Refresh batch jobs' }));
  await screen.findByText('Batch status unavailable'); expect(screen.queryByText('second-job')).not.toBeInTheDocument();
});

it('refuses serving plans, a different Job identity and unbounded list payloads', async () => {
  const value = review('request');
  expect(parseServingReview(value, 'ws-1', 'request', 'provision', undefined, 'batch')).not.toBeNull();
  expect(parseServingReview(value, 'ws-1', 'request', 'provision')).toBeNull();
  expect(parseServingReview({ ...value, job_id: 'different' }, 'ws-1', 'request', 'provision', undefined, 'batch')).toBeNull();
  server.use(http.get(api(root), () => HttpResponse.json({ workspace_id: 'ws-1', jobs: Array(101).fill(job), truncated: true })));
  expect((await listBatchJobs(new ScopeGuard(), 'ws-1')).ok).toBe(false);
});
