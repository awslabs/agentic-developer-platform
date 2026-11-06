import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DevOpsAccessSummary from '@/pages/products/services/DevOpsAccessSummary';
import { listOwnerGrants, type OwnerGrant } from '@/services/l5OwnerConsent';
import type { DevOpsBinding, DevOpsConfiguration } from '@/services/l5Services';
vi.mock('@/services/l5OwnerConsent', async original => ({ ...await original<typeof import('@/services/l5OwnerConsent')>(), listOwnerGrants: vi.fn() }));
const scope = { tenant_id: 'tenant', product_id: 'product', project_id: 'project', owner_id: 'owner' };
const configuration = { scope, configuration_id: 'config', revision: 3, hash: 'hash', settings: { enabled: true } } as DevOpsConfiguration;
const binding = { binding_id: 'binding', generation: 2 } as DevOpsBinding;
const grant = () => ({ ...scope, service_id: 'l5-devops-worker', active: true, revoked_at: null, expires_at: new Date(Date.now() + 60_000).toISOString(), capabilities: ['investigation'],
  execution_scope: { ...scope, service_id: 'l5-devops-worker', created_by: 'owner', configuration_id: 'config', configuration_revision: 3, configuration_hash: 'hash',
    binding_id: 'binding', binding_generation: 2, capabilities: ['investigation'] },
}) as unknown as OwnerGrant;
beforeEach(() => vi.resetAllMocks());
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('saved investigation permission summary', () => {
  it('shows the matching grant expiry and expires it without implying run readiness', async () => {
    vi.useFakeTimers(); vi.mocked(listOwnerGrants).mockResolvedValue({ items: [grant()] });
    await act(async () => { render(<DevOpsAccessSummary configuration={configuration} binding={binding} refreshKey={0} />); });
    expect(screen.getByRole('status')).toHaveTextContent('Owner permission expires');
    expect(screen.getByRole('status')).toHaveTextContent('Run readiness is checked when you start');
    await act(async () => { vi.advanceTimersByTime(60_001); });
    expect(screen.getByRole('status')).toHaveTextContent('Owner permission expired');
  });
  it.each([{ purpose: 'devops_native_ui' }, { configuration_revision: 2 }, { binding_generation: 1 }, { created_by: 'other-owner' }, { configuration_hash: 'old' }])('does not treat a different grant scope as run permission: %j', async change => {
    const item = grant(); item.execution_scope = { ...item.execution_scope!, ...change };
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [item] });
    render(<DevOpsAccessSummary configuration={configuration} binding={binding} refreshKey={0} />);
    expect(await screen.findByText('Owner permission needs review')).toBeVisible();
  });
  it('redacts foreign grants and ignores a response after the project changes', async () => {
    let done!: (value: { items: OwnerGrant[] }) => void;
    vi.mocked(listOwnerGrants).mockReturnValueOnce(new Promise(resolve => { done = resolve; })).mockResolvedValue({ items: [] });
    const view = render(<DevOpsAccessSummary configuration={configuration} binding={binding} refreshKey={0} />);
    view.rerender(<DevOpsAccessSummary configuration={{ ...configuration, scope: { ...configuration.scope, project_id: 'other-project' } }} binding={binding} refreshKey={0} />);
    await screen.findByText('Owner permission needs review');
    await act(async () => { done({ items: [grant()] }); });
    expect(screen.getByRole('status')).not.toHaveTextContent('expires');
  });
});
