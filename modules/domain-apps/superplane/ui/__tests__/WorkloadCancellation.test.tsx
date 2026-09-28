import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { WorkloadCancellation } from '@superplane-ui/WorkloadCancellation';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import type { ServingDeployment } from '@superplane-ui/workloads';

const path = `/api${DOMAIN_BASE}/workspaces/workspace/batch-jobs/original-job/cancellation`;
const row: ServingDeployment = { name: 'training', deploymentId: 'original-job', operationId: 'original-operation',
  status: 'Pending', operationState: 'accepted', providerUid: null, cancellationRequested: false, cleanupStatus: 'unconfirmed' };
const answer = { deployment_id: 'original-job', job_id: 'original-job', workspace_id: 'workspace', operation_id: 'original-operation',
  operation_state: 'running', cancellation_requested: true, cleanup_status: 'unconfirmed' };
beforeEach(() => window.sessionStorage.setItem('cognito_access_token', 'test-token'));

it('retries a lost cancellation reply with the original operation and never claims cleanup', async () => {
  const bodies: unknown[] = []; const progress = vi.fn();
  server.use(http.post(path, async ({ request }) => {
    bodies.push(await request.json()); return bodies.length === 1 ? HttpResponse.error() : HttpResponse.json(answer);
  }));
  const user = userEvent.setup();
  const view = render(<WorkloadCancellation workspaceId="workspace" row={row} kind="batch" onProgress={progress} />);
  await user.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await screen.findByText('Cancellation outcome unavailable');
  expect(progress).not.toHaveBeenCalled();
  view.unmount();
  render(<WorkloadCancellation workspaceId="workspace" row={row} kind="batch" onProgress={progress} />);
  await user.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await screen.findByText('Cancellation requested. Resource cleanup remains subject to backend reconciliation.');
  expect(bodies).toEqual([{ operation_id: 'original-operation' }, { operation_id: 'original-operation' }]);
  expect(progress).toHaveBeenCalledOnce();
  expect(screen.queryByText(/confirmed no workload/)).not.toBeInTheDocument();
});

it('requires matching backend non-execution evidence and refuses a different operation', async () => {
  const progress = vi.fn();
  server.use(http.post(path, () => HttpResponse.json({ ...answer, operation_id: 'foreign', cleanup_status: 'not-required' })));
  render(<WorkloadCancellation workspaceId="workspace" row={row} kind="batch" onProgress={progress} />);
  await userEvent.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await screen.findByText('Cancellation outcome unavailable');
  expect(progress).not.toHaveBeenCalled();
  server.use(http.post(path, () => HttpResponse.json({ ...answer, operation_state: 'cancelled', cleanup_status: 'not-required' })));
  await userEvent.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await screen.findByText('Cancelled before dispatch. The backend confirmed no workload was started.');
  expect(progress).toHaveBeenCalledOnce();
});

it('drops cancellation replies after a workspace switch and disables a revoked action', async () => {
  let release!: () => void; const waiting = new Promise<void>((resolve) => { release = resolve; });
  let received = false; const progress = vi.fn();
  server.use(http.post(path, async () => { received = true; await waiting; return HttpResponse.json(answer); }));
  const view = render(<WorkloadCancellation workspaceId="workspace" row={row} kind="batch" onProgress={progress} />);
  await userEvent.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await waitFor(() => expect(received).toBe(true));
  view.rerender(<WorkloadCancellation workspaceId="other" row={row} kind="batch" onProgress={progress} />);
  release();
  server.use(http.post(path.replace('/workspace/', '/other/'), () => new HttpResponse(null, { status: 403 })));
  await userEvent.click(screen.getByRole('button', { name: /Cancel pending operation/ }));
  await screen.findByText('Cancellation outcome unavailable');
  expect(screen.getByRole('button', { name: /Cancel pending operation/ })).toBeDisabled();
  expect(progress).not.toHaveBeenCalled();
  expect(screen.queryByText(/Cancellation requested\./)).not.toBeInTheDocument();
});
