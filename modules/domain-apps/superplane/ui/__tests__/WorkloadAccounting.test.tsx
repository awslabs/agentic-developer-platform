import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { DOMAIN_BASE } from '@superplane-ui/contract';
import { WorkloadAccounting } from '@superplane-ui/WorkloadAccounting';
import type { ServingDeployment } from '@superplane-ui/workloads';

const api = `/api${DOMAIN_BASE}/workspaces/ws-1/batch-jobs/job-id/accounting`;
const row: ServingDeployment = { deploymentId: 'job-id', name: 'training', status: 'Created', operationId: 'operation', operationState: 'succeeded', providerUid: null, cancellationRequested: false, cleanupStatus: 'unconfirmed' };
const result = { workspace_id: 'ws-1', deployment_id: 'job-id', kind: 'batch', checked_at: '2026-09-24T12:00:00Z',
  workspace_committed_budget_micros: '9007199254740993', workspace_reservation_cap_micros: '0', workspace_budget_state: 'exhausted', estimated_cost_micros: null, observed_cost_micros: null, cost_reconciliation: 'unavailable', recorded_resources: [{ kind: 'storage', count: 1 }],
  operations: [{ action: 'provision', operation_id: 'operation', approved_max_cost_micros: '2000000', budget_held_micros: '2000000', budget_state: 'confirmed', shared_reservation_state: 'retained', accounting_consistent: false, updated_at: '2026-09-24T11:59:00Z' }] };
beforeEach(() => { window.sessionStorage.setItem('cognito_access_token', 'test-token'); });
const panel = () => <WorkloadAccounting workspaceId="ws-1" row={row} kind="batch" />;

it('separates reservations from unknown costs and preserves exact large values and zero caps', async () => {
  server.use(http.get(api, () => HttpResponse.json(result)));
  render(panel()); await userEvent.click(screen.getByRole('button', { name: 'View budget for training' }));
  await screen.findByText(/Original workload: approved ceiling 2 USD; budget held 2 USD/);
  expect(screen.getByText(/Workspace committed budget: 9007199254.740993 USD. Reservation cap: 0 USD. Capacity: exhausted/)).toBeInTheDocument();
  expect(screen.getByText(/Observed provider cost: unknown/)).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('Accounting acknowledgement is incomplete');
  expect(screen.getByText(/1 storage/)).toBeInTheDocument();
});

it('clears accounting after revocation and rejects a different workload response', async () => {
  server.use(http.get(api, () => HttpResponse.json(result)));
  render(panel()); await userEvent.click(screen.getByRole('button', { name: /View budget/ }));
  await screen.findByText(/Original workload:/);
  server.use(http.get(api, () => new HttpResponse(null, { status: 403 })));
  await userEvent.click(screen.getByRole('button', { name: /View budget/ }));
  await screen.findByText('Workload accounting unavailable');
  expect(screen.queryByText(/Original workload:/)).not.toBeInTheDocument();
  server.use(http.get(api, () => HttpResponse.json({ ...result, deployment_id: 'foreign-workload' })));
  await userEvent.click(screen.getByRole('button', { name: /View budget/ }));
  expect(screen.queryByText(/Original workload:/)).not.toBeInTheDocument();
});
