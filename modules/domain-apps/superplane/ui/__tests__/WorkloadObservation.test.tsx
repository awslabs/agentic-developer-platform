import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import { WorkloadObservation } from '@superplane-ui/WorkloadObservation';
import type { ServingDeployment } from '@superplane-ui/workloads';

const api = (path: string) => `/api${DOMAIN_BASE}${path}`;
const row: ServingDeployment = { deploymentId: 'job-id', name: 'training', status: 'Created', operationId: 'operation', operationState: 'succeeded', providerUid: 'original-job', cancellationRequested: false, cleanupStatus: 'unconfirmed' };
const root = '/workspaces/ws-1/batch-jobs/job-id/observation';
const result = { workspace_id: 'ws-1', deployment_id: 'job-id', kind: 'batch', uid: 'original-job', checked_at: '2026-09-24T12:00:00Z', state: 'running', pods: [{ uid: 'original-pod', phase: 'Running', ready: false, restarts: 0, exit_code: null }], logs: null, logs_pod_uid: null, logs_truncated: false };
beforeEach(() => { window.sessionStorage.setItem('cognito_access_token', 'test-token'); });
const panel = (workspaceId = 'ws-1', target = row) => <WorkloadObservation workspaceId={workspaceId} row={target} kind="batch" />;

it('reads only on inspection, selects original Pod logs and renders output as text', async () => {
  const queries: URLSearchParams[] = [];
  server.use(http.get(api(root), ({ request }) => {
    const query = new URL(request.url).searchParams; queries.push(query);
    return HttpResponse.json(query.get('logs') === 'true' ? { ...result, logs: '<script>window.bad=true</script>\nepoch 1 [redacted]', logs_pod_uid: 'original-pod', logs_truncated: true } : result);
  }));
  render(panel()); expect(queries).toHaveLength(0);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Inspect status and logs for training' }));
  await screen.findByText(/Observed workload: running/);
  await user.selectOptions(screen.getByLabelText('Pod log window'), 'original-pod');
  const log = await screen.findByLabelText('Logs for training');
  expect(log).toHaveTextContent('<script>window.bad=true</script>');
  expect(log.querySelector('script')).toBeNull();
  expect(queries.at(-1)?.get('pod_uid')).toBe('original-pod');
  expect(screen.getByText(/Earlier output may be omitted/)).toBeInTheDocument();
});

it('clears observed status and logs when read access is revoked', async () => {
  server.use(http.get(api(root), () => HttpResponse.json(result)));
  render(panel()); const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /Inspect status/ }));
  await screen.findByText(/Observed workload: running/);
  server.use(http.get(api(root), () => new HttpResponse(null, { status: 403 })));
  await user.click(screen.getByRole('button', { name: /Inspect status/ }));
  await screen.findByText('Workload observation unavailable');
  expect(screen.queryByText(/Observed workload:/)).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Pod log window')).not.toBeInTheDocument();
});

it('drops a late prior workspace read and refuses replacement workload identity', async () => {
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  let started = false;
  server.use(http.get(api(root), async () => { started = true; await waiting; return HttpResponse.json(result); }),
    http.get(api('/workspaces/ws-2/batch-jobs/job-id/observation'), () => HttpResponse.json({ ...result, workspace_id: 'ws-2', uid: 'replacement-job' })));
  const view = render(panel()); const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /Inspect status/ }));
  await waitFor(() => expect(started).toBe(true));
  view.rerender(panel('ws-2')); release();
  await user.click(screen.getByRole('button', { name: /Inspect status/ }));
  await screen.findByText('Workload observation unavailable');
  expect(screen.queryByText(/Observed workload:/)).not.toBeInTheDocument();
});
