import { HttpResponse, http } from 'msw';
import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { server } from '@/mocks/server';
import { DispatchRecoveryPanel } from '@superplane-ui/DispatchRecoveryPanel';
import { ScopeGuard } from '@superplane-ui/client';
import { DOMAIN_BASE } from '@superplane-ui/contract';

const path = `/api${DOMAIN_BASE}/workspaces/ws/dispatch-recovery`;
const original = { operationId: 'operation', idempotencyKey: 'original-request', workspaceId: 'ws',
  version: 2, state: 'unknown' as const, observedAt: new Date().toISOString() };
const review = { reauthorization_id: 'renewal', operation_id: 'operation', expected_version: 2,
  original_approval_id: 'old-approval', plan_digest: 'digest', request: { action: 'provision' },
  envelope: { max_cost_micros: 100 }, result: 'pending', can_decide: true,
  expires_at: new Date(Date.now() + 600_000).toISOString() };

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.setItem('cognito_access_token', 'test-token');
});

it.each(['undispatched', 'provider'] as const)('requires explicit fresh decision for %s recovery of the same operation', async (mode) => {
  const recoveryPath = mode === 'provider' ? path.replace('/dispatch-recovery', '/provider-recovery') : path;
  const seen: string[] = [];
  const keys: string[] = [];
  server.use(
    http.post(recoveryPath + '/review', async ({ request }) => {
      const body = await request.json() as { expected_version: number; idempotency_key: string };
      expect(body.expected_version).toBe(2); keys.push(body.idempotency_key); seen.push('review');
      return HttpResponse.json(review);
    }),
    http.post(recoveryPath + '/decision', async ({ request }) => {
      expect(await request.json()).toEqual({ reauthorization_id: 'renewal', result: 'allowed-once' });
      seen.push('approve'); return HttpResponse.json({ ...review, result: 'allowed-once', can_decide: false });
    }),
    http.post(recoveryPath + '/resume', async ({ request }) => {
      expect(await request.json()).toEqual({ reauthorization_id: 'renewal' }); seen.push('resume');
      return HttpResponse.json({ status: 'recovery-recorded', operation_id: 'operation', reauthorization_id: 'renewal' });
    }),
  );
  const resumed = vi.fn();
  render(<DispatchRecoveryPanel workspaceId="ws" orgId="org" operation={original} mode={mode}
    guard={new ScopeGuard()} sessionToken="test-token" onResumed={resumed} />);
  expect(seen).toEqual([]);
  await userEvent.click(screen.getByRole('button', { name: 'Review original request for recovery' }));
  expect(await screen.findByRole('button', { name: 'Approve recovery of this plan' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: 'Resume original provisioning' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Review original request for recovery' }));
  expect(keys[0]).toBe(keys[1]);
  await userEvent.click(screen.getByRole('button', { name: 'Approve recovery of this plan' }));
  await userEvent.click(await screen.findByRole('button', { name: 'Resume original provisioning' }));
  await waitFor(() => expect(resumed).toHaveBeenCalledOnce());
  expect(seen).toEqual(['review', 'review', 'approve', 'resume']);
});

it('refuses to act after the authenticated session changes', async () => {
  const handler = vi.fn(() => HttpResponse.json(review));
  server.use(http.post(path + '/review', handler));
  render(<DispatchRecoveryPanel workspaceId="ws" orgId="org" operation={original}
    guard={new ScopeGuard()} sessionToken="expired-token" onResumed={vi.fn()} />);
  await userEvent.click(screen.getByRole('button', { name: 'Review original request for recovery' }));
  expect(handler).not.toHaveBeenCalled();
});
