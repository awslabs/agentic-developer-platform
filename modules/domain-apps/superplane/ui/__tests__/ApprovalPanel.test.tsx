import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, expect, it } from 'vitest';

import { server } from '@/mocks/server';
import { ApprovalPanel } from '@superplane-ui/ApprovalPanel';
import { ScopeGuard } from '@superplane-ui/client';
import { ENDPOINTS, type OperationApproval } from '@superplane-ui/contract';

const approval: OperationApproval = {
  approval_id: 'approval-1', workspace_id: 'workspace-1', result: 'pending',
  can_decide: false, action: 'provision', target: { account: '123456789012', region: 'us-east-1' },
  plan_digest: 'reviewed-plan', envelope: { max_resource_units: 2, max_runtime_seconds: 60, max_cost_micros: 1_000_000 },
  expires_at: '2999-01-01T00:00:00Z', revoked: false,
};
const previous = ENDPOINTS.decideApproval.served;
afterEach(() => { (ENDPOINTS.decideApproval as { served: boolean }).served = previous; });

it('shows the operation limits but grants no decision controls to the requester', () => {
  render(<ApprovalPanel approval={approval} guard={new ScopeGuard()} onChange={() => {}} />);
  expect(screen.getByText(/Maximum cost: 1 USD/)).toBeInTheDocument();
  expect(screen.getByText(/123456789012/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /approve this operation once/i })).toBeNull();
});

it('submits only the selected human decision for the reviewed approval', async () => {
  (ENDPOINTS.decideApproval as { served: boolean }).served = true;
  let decision: unknown;
  server.use(http.post('/api/superplane/v1/operation-approvals/approval-1/decision', async ({ request }) => {
    decision = await request.json();
    return HttpResponse.json({ ...approval, result: 'allowed-once' });
  }));
  const user = userEvent.setup();
  render(<ApprovalPanel approval={{ ...approval, can_decide: true }} guard={new ScopeGuard()} onChange={() => {}} />);
  await user.click(screen.getByRole('button', { name: /approve this operation once/i }));
  await waitFor(() => expect(decision).toEqual({ result: 'allowed-once' }));
});

it('does not offer a decision for expired approval authority', () => {
  render(<ApprovalPanel approval={{ ...approval, can_decide: true, expires_at: '2000-01-01T00:00:00Z' }} guard={new ScopeGuard()} onChange={() => {}} />);
  expect(screen.queryByRole('button', { name: /approve this operation once/i })).toBeNull();
});
