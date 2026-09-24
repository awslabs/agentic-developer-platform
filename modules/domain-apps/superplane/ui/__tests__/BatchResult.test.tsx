import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import { BatchResult } from '@superplane-ui/BatchResult';
import type { ServingDeployment } from '@superplane-ui/workloads';

const api = `/api${DOMAIN_BASE}/workspaces/ws-1/batch-jobs/job-id/result`;
const row: ServingDeployment = { deploymentId: 'job-id', name: 'training', status: 'Deleted', operationId: 'stop-operation', sourceOperationId: 'original-operation', operationState: 'succeeded', providerUid: 'original-job', cancellationRequested: false, cleanupStatus: 'confirmed' };
const response = { workspace_id: 'ws-1', job_id: 'job-id', operation_id: 'original-operation', media_type: 'text/plain', status: 'retained',
  result: { content: '<script>throw new Error("injected")</script>', job_uid: 'original-job', pod_uid: 'original-pod', captured_at: '2026-09-24T12:00:00Z', sha256: 'a'.repeat(64), redacted: false } };
beforeEach(() => { window.sessionStorage.setItem('cognito_access_token', 'test-token'); });

it('shows retained output after cleanup as text and reauthorizes downloads', async () => {
  let requests = 0;
  server.use(http.get(api, () => { requests++; return HttpResponse.json(response); }));
  const create = vi.fn((_blob: Blob) => 'blob:fixture');
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn(); });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  const view = render(<BatchResult workspaceId="ws-1" row={row} />);
  await userEvent.click(screen.getByRole('button', { name: /View result/ }));
  await screen.findByLabelText('Batch result text');
  expect(screen.getByLabelText('Batch result text')).toHaveTextContent('<script>');
  expect(view.container.querySelector('script')).toBeNull();
  await userEvent.click(screen.getByRole('button', { name: 'Download text result' }));
  await vi.waitFor(() => expect(create).toHaveBeenCalledOnce());
  expect(requests).toBe(2);
  expect(create.mock.calls[0][0]).toBeInstanceOf(Blob);
  expect(click).toHaveBeenCalledOnce();
  click.mockRestore(); vi.unstubAllGlobals();
});

it('clears output on refused download and rejects foreign or oversized responses', async () => {
  server.use(http.get(api, () => HttpResponse.json(response)));
  render(<BatchResult workspaceId="ws-1" row={row} />);
  await userEvent.click(screen.getByRole('button', { name: /View result/ }));
  await screen.findByLabelText('Batch result text');
  server.use(http.get(api, () => new HttpResponse(null, { status: 403 })));
  await userEvent.click(screen.getByRole('button', { name: /Download text/ }));
  await screen.findByText('Batch result unavailable');
  expect(screen.queryByLabelText('Batch result text')).toBeNull();
  for (const changed of [{ ...response, operation_id: 'foreign' }, { ...response, result: { ...response.result, content: 'é'.repeat(9000) } }]) {
    server.use(http.get(api, () => HttpResponse.json(changed)));
    await userEvent.click(screen.getByRole('button', { name: /View result/ }));
    expect(screen.queryByLabelText('Batch result text')).toBeNull();
  }
});

it('discards delayed output after the workspace changes', async () => {
  let resolve: ((value: Response) => void) | undefined;
  server.use(http.get(api, () => new Promise<Response>((done) => { resolve = done; })));
  const view = render(<BatchResult workspaceId="ws-1" row={row} />);
  await userEvent.click(screen.getByRole('button', { name: /View result/ }));
  await vi.waitFor(() => expect(resolve).toBeDefined());
  view.rerender(<BatchResult workspaceId="ws-2" row={row} />);
  resolve!(HttpResponse.json(response));
  expect(screen.queryByLabelText('Batch result text')).toBeNull();
});
