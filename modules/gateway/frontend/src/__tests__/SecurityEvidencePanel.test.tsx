import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SecurityEvidencePanel from '@/pages/products/services/SecurityEvidencePanel';

const api = vi.hoisted(() => ({ action: vi.fn(), read: vi.fn() }));
vi.mock('@/services/securityEvidence', () => ({ securityEvidenceAction: api.action, readSecurityEvidence: api.read }));

beforeEach(() => vi.resetAllMocks());

it('discards an old tenant response after the Product scope changes', async () => {
  let resolve: (value: unknown) => void = () => {};
  api.action.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  const challenge = vi.fn();
  const { rerender } = render(<SecurityEvidencePanel productId="old-product" projectId="old-project" tenantId="old-tenant" onChallenge={challenge} />);
  fireEvent.change(screen.getByLabelText('Request ID'), { target: { value: 'old-request' } });
  fireEvent.click(screen.getByText('Preview owner consent'));
  rerender(<SecurityEvidencePanel productId="new-product" projectId="new-project" tenantId="new-tenant" onChallenge={challenge} />);
  await act(async () => resolve({ request: { selection: { product_id: 'old-product', project_id: 'old-project', tenant_id: 'old-tenant' }, release: { run_id: 1 }, expires_at: 'later' },
    snapshot: { configuration: { configuration_id: 'private-old-configuration', revision: 1 } } }));
  expect(screen.queryByText(/private-old-configuration/)).toBeNull();
  expect(screen.queryByText(/Approve this exact snapshot/)).toBeNull();
});

it('discards a prepared response after the request ID changes', async () => {
  let resolve: (value: unknown) => void = () => {};
  api.action.mockReturnValueOnce(new Promise(done => { resolve = done; }));
  render(<SecurityEvidencePanel productId="product" projectId="project" tenantId="tenant" onChallenge={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Request ID'), { target: { value: 'old-request' } });
  fireEvent.click(screen.getByText('Prepare my check'));
  fireEvent.change(screen.getByLabelText('Request ID'), { target: { value: 'new-request' } });
  await act(async () => resolve({ request: { selection: { product_id: 'product', project_id: 'project', tenant_id: 'tenant', phase: 'edit' }, release: { run_id: 1 }, expires_at: 'later' },
    snapshot: { configuration: { configuration_id: 'old-snapshot', revision: 1 } } }));
  expect(screen.queryByText(/old-snapshot/)).toBeNull();
  expect(screen.queryByText('Use requested Security choices')).toBeNull();
});
