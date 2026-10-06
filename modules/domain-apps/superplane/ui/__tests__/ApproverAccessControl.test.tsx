import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it } from 'vitest';

import { server } from '@/mocks/server';
import { ApproverAccessControl } from '@superplane-ui/ApproverAccessControl';
import { ScopeGuard } from '@superplane-ui/client';
import { WorkspaceDetails } from '@superplane-ui/WorkspaceDetails';

const path = '/api/superplane/v1/workspaces/workspace-1/access/v1';
const administrator = {
  workspace_id: 'workspace-1', grant_id: 'grant-owner', revision: 1,
  principal_type: 'human', subject: 'owner',
  effective_permissions: ['workspace:read', 'workspace:administer'],
  source: 'preexisting_grant',
  granted_by: null, reason: null, request_id: null,
};

beforeEach(() => {
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
});

it('grants only read access to a selected distinct immutable human subject', async () => {
  let sent: Record<string, unknown> | null = null;
  server.use(
    http.get(`${path}/me`, () => HttpResponse.json(administrator)),
    http.post(`${path}/grants`, async ({ request }) => {
      sent = await request.json() as Record<string, unknown>;
      return HttpResponse.json({
        ...administrator, grant_id: 'grant-approver', subject: 'human-approver',
        effective_permissions: ['workspace:read'], granted_by: 'owner',
        source: 'explicit_assignment',
        reason: 'approver_setup', request_id: sent.request_id,
      });
    }),
  );
  const user = userEvent.setup();
  render(<ApproverAccessControl workspaceId="workspace-1" guard={new ScopeGuard()} sessionToken="test-token" />);
  await user.type(await screen.findByLabelText(/immutable ADP subject/), 'human-approver');
  expect(screen.getByRole('region', { name: 'Share workspace read access' })).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Share workspace read access' })).toBeInTheDocument();
  expect(screen.getByText(/does not make the recipient an approver or grant organization access/)).toBeInTheDocument();
  expect(screen.queryByText(/approval setup|set up.*approver/i)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Grant read access' }));
  await waitFor(() => expect(sent).not.toBeNull());
  expect(sent).toMatchObject({
    target_subject: 'human-approver', principal_type: 'human',
    permissions: ['workspace:read'], reason: 'approver_setup', expected_revision: 0,
  });
  expect(sent?.request_id).toMatch(/^[0-9a-f-]{36}$/);
  expect(await screen.findByText(/Read access granted to human-approver/)).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Approval authority and organization access were not granted.');
});

it('offers read sharing rather than approver setup from workspace details', async () => {
  server.use(
    http.get('/api/superplane/v1/workspaces/workspace-1', () => HttpResponse.json({
      id: 'workspace-1', org_id: 'org-1', name: 'research', display_name: 'Research',
      isolation_mode: 'dedicated', status: 'Active', is_default: false,
      created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
    })),
    http.get(`${path}/me`, () => HttpResponse.json(administrator)),
  );
  const user = userEvent.setup();
  render(<WorkspaceDetails workspaceId="workspace-1" guard={new ScopeGuard()} sessionToken="test-token"
    scope={{ orgId: 'org-1', deploymentId: 'test-deployment' }} now={Date.now()} />);
  await user.click(await screen.findByRole('button', { name: 'Share workspace read access' }));
  expect(await screen.findByLabelText(/immutable ADP subject/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /approver/i })).not.toBeInTheDocument();
  expect(screen.getByText(/does not make the recipient an approver or grant organization access/)).toBeInTheDocument();
});

it('does not display a grant form for a viewer or a mismatched workspace', async () => {
  server.use(http.get(`${path}/me`, () => HttpResponse.json({
    ...administrator, effective_permissions: ['workspace:read'],
  })));
  const { unmount } = render(<ApproverAccessControl workspaceId="workspace-1" guard={new ScopeGuard()} sessionToken="test-token" />);
  expect(await screen.findByText(/Only a current explicit workspace administrator/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Grant read access' })).toBeNull();
  unmount();
  server.use(http.get(`${path}/me`, () => HttpResponse.json({
    ...administrator, workspace_id: 'workspace-other',
  })));
  render(<ApproverAccessControl workspaceId="workspace-1" guard={new ScopeGuard()} sessionToken="test-token" />);
  expect(await screen.findByText(/response this version of ADP does not understand/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Grant read access' })).toBeNull();
});

it('retains request identity across an uncertain retry and refuses email identity', async () => {
  let deliveries = 0;
  const identities: unknown[] = [];
  server.use(
    http.get(`${path}/me`, () => HttpResponse.json(administrator)),
    http.post(`${path}/grants`, async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      identities.push(body.request_id);
      deliveries += 1;
      return deliveries === 1 ? new HttpResponse(null, { status: 503 }) : HttpResponse.json({
        ...administrator, grant_id: 'grant-approver', subject: 'human-approver',
        effective_permissions: ['workspace:read'], granted_by: 'owner',
        source: 'explicit_assignment',
        reason: 'approver_setup', request_id: body.request_id,
      });
    }),
  );
  const user = userEvent.setup();
  render(<ApproverAccessControl workspaceId="workspace-1" guard={new ScopeGuard()} sessionToken="test-token" />);
  const subject = await screen.findByLabelText(/immutable ADP subject/);
  await user.type(subject, 'approver@example.invalid');
  await user.click(screen.getByRole('button', { name: 'Grant read access' }));
  expect(screen.getByText(/not an email address/)).toBeInTheDocument();
  expect(identities).toHaveLength(0);
  await user.clear(subject);
  await user.type(subject, 'human-approver');
  await user.click(screen.getByRole('button', { name: 'Grant read access' }));
  await waitFor(() => expect(identities).toHaveLength(1));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Grant read access' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: 'Grant read access' }));
  await waitFor(() => expect(identities).toHaveLength(2));
  expect(identities[0]).toBe(identities[1]);
});
