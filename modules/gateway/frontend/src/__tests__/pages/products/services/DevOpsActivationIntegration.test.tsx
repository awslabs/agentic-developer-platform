import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DevOpsServiceTab from '@/pages/products/services/DevOpsServiceTab';
import { getDevOpsConfiguration, getDevOpsOptions, saveDevOpsConfiguration } from '@/services/l5Services';
import { configuration, reference } from '../../../fixtures/devOpsActivation';

vi.mock('@/services/l5Services', async importOriginal => ({ ...await importOriginal<typeof import('@/services/l5Services')>(),
  getDevOpsConfiguration: vi.fn(), getDevOpsOptions: vi.fn(), saveDevOpsConfiguration: vi.fn() }));
vi.mock('@/pages/products/services/DevOpsAccessSummary', () => ({ default: () => null }));
vi.mock('@/pages/products/services/L5OwnerConsent', () => ({ default: () => null }));
vi.mock('@/pages/products/services/DevOpsOperations', () => ({ default: ({ children }: { children: () => React.ReactNode }) => children() }));
vi.mock('@/pages/products/services/DevOpsActivationSetup', () => ({ default: ({ onActivated, configuration: current }: {
  onActivated: (value: unknown) => void; configuration: { value: typeof configuration };
}) => <button type="button" onClick={() => onActivated({ value: { ...current.value, revision: 4, hash: 'activated',
  settings: { ...current.value.settings, enabled: true, devops: { ...current.value.settings.devops,
    accounts: [{ account_id: 'activated-account' }], access_policy_ref: reference('policy', 'activated-policy') } } }, etag: '"activated"' })}>Confirm reviewed activation</button> }));

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getDevOpsConfiguration).mockResolvedValue({ value: configuration, etag: '"config-hash"' });
  vi.mocked(getDevOpsOptions).mockResolvedValue({ connections: [reference()], sources: configuration.settings.source_refs,
    capabilities: ['investigation'], regions: ['us-east-1'], unavailable_reasons: [] });
});
describe('activation configuration handoff', () => {
  it('retains local setup drafts and prevents them overwriting newly activated resource selections', async () => {
    render(<DevOpsServiceTab productId="product" projectId="project" tenantId="tenant" />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Change setup' })).toBeEnabled());
    await userEvent.click(screen.getByRole('button', { name: 'Change setup' }));
    await screen.findByRole('button', { name: 'Confirm reviewed activation' });
    await userEvent.click(screen.getByText('Advanced configuration', { selector: 'summary' }));
    const accounts = screen.getByLabelText('Accounts and workload references (JSON array)');
    const policy = screen.getByLabelText('Project access policy reference (revisioned JSON)');
    fireEvent.change(accounts, { target: { value: '[{"unsaved":"account draft"}]' } });
    fireEvent.change(policy, { target: { value: '{"unsaved":"policy draft"}' } });
    fireEvent.change(screen.getByLabelText('AWS Region'), { target: { value: 'us-west-2' } });
    await userEvent.click(screen.getByRole('button', { name: 'Confirm reviewed activation' }));
    expect(accounts).toHaveValue('[{"unsaved":"account draft"}]');
    expect(policy).toHaveValue('{"unsaved":"policy draft"}');
    expect(screen.getByLabelText('AWS Region')).toHaveValue('us-west-2');
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
    expect(screen.getByText(/Investigation setup is saved/)).toBeInTheDocument();
    expect(saveDevOpsConfiguration).not.toHaveBeenCalled();
  });
});
