import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { WorkspaceProviderPanel } from '@superplane-ui/WorkspaceProviderPanel';

beforeEach(() => { window.sessionStorage.setItem('cognito_access_token', 'test-token'); });
const observation = { credential_valid: true, permissions_sufficient: true, quota_available: true, checked_at: new Date().toISOString() };
const metadata = { workspace_id: 'ws', managed: true, credential_id: 'spda1:handle', account_id: '123456789012', expires_at: new Date(Date.now() + 60_000).toISOString(), admits_new_work: true };
function panel(onObservation = vi.fn()) {
  return <WorkspaceProviderPanel workspaceId="ws" sessionToken="test-token" providers={['aws']} mayManage onObservation={onObservation} />;
}
it('restores managed provider evidence on re-entry and refreshes only when requested', async () => {
  let validation: unknown = observation;
  let posts = 0;
  server.use(
    http.get('/api/superplane/v1/workspaces/ws/provider-authority', () => HttpResponse.json({ ...metadata, validation })),
    http.post('/api/superplane/v1/provider-authorities/spda1%3Ahandle/validation', () => {
      posts++; validation = { ...observation, quota_available: false }; return HttpResponse.json(validation);
    }),
  );
  const onObservation = vi.fn();
  const view = render(panel(onObservation));
  expect(await screen.findByText(/installation-managed AWS connection/)).toBeInTheDocument();
  await waitFor(() => expect(onObservation).toHaveBeenLastCalledWith(expect.objectContaining({ workspaceId: 'ws', validation: expect.objectContaining({ quota_available: true }) })));
  expect(posts).toBe(0);
  await userEvent.click(screen.getByRole('button', { name: 'Refresh AWS validation' }));
  await waitFor(() => expect(onObservation).toHaveBeenLastCalledWith(expect.objectContaining({ validation: expect.objectContaining({ quota_available: false }) })));
  view.unmount(); render(panel(onObservation));
  expect(await screen.findByText(/installation-managed AWS connection/)).toBeInTheDocument();
  expect(posts).toBe(1);
});
it('refuses another workspace observation', async () => {
  server.use(http.get('/api/superplane/v1/workspaces/ws/provider-authority', () => HttpResponse.json({ ...metadata, workspace_id: 'other', validation: observation })));
  const onObservation = vi.fn(); render(panel(onObservation));
  expect(await screen.findByText('Provider observation did not match this workspace.')).toBeInTheDocument();
  expect(onObservation).toHaveBeenLastCalledWith({ workspaceId: 'ws', validation: null, admitsNewWork: null });
  expect(screen.queryByRole('button', { name: 'Refresh AWS validation' })).not.toBeInTheDocument();
});
