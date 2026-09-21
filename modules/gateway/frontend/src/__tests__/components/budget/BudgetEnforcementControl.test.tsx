import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BudgetEnforcementControl } from '@/components/budget/BudgetEnforcementControl';
import { apiClient } from '@/services/api';

const state = vi.hoisted(() => ({ admin: true }));
vi.mock('@/contexts/AuthContext', () => ({ useAuthContext: () => ({ user: { orgId: 'org' } }) }));
vi.mock('@/hooks/usePermissions', () => ({ usePermissions: () => ({ isPlatformAdmin: () => state.admin }) }));
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
const on = { global_enabled: true, flow_enabled: null, effective_enabled: true, accounting_incomplete: false, revision: 0 };
function show(flowId?: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><BudgetEnforcementControl flowId={flowId} /></QueryClientProvider>);
}

beforeEach(() => { vi.resetAllMocks(); state.admin = true; vi.mocked(apiClient.get).mockResolvedValue(on); });

describe('budget enforcement controls', () => {
  it('switches global enforcement off using the displayed revision', async () => {
    vi.mocked(apiClient.post).mockImplementation(async () => {
      const off = { ...on, global_enabled: false, effective_enabled: false, revision: 1 };
      vi.mocked(apiClient.get).mockResolvedValue(off);
      return off;
    });
    show();
    await userEvent.click(await screen.findByRole('switch'));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/budget/enforcement', expect.objectContaining({ enabled: false, expected_revision: 0 })));
    expect(await screen.findByText('Budget enforcement setting saved.')).toBeInTheDocument();
    expect(screen.getByRole('switch')).not.toBeChecked();
    expect(screen.getByText(/Usage and costs continue to be recorded/)).toBeInTheDocument();
  });

  it('targets only the selected flow', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ ...on, flow_enabled: false, effective_enabled: false, revision: 1 });
    show('flow-123');
    await userEvent.click(await screen.findByRole('switch'));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/budget/enforcement/flows/flow-123', expect.objectContaining({ enabled: false })));
  });

  it('shows that global off takes precedence over flow on', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ ...on, global_enabled: false, flow_enabled: true, effective_enabled: false });
    show('flow-123');
    expect(await screen.findByText(/Global enforcement is off/)).toBeInTheDocument();
    expect(screen.getByRole('switch')).toBeChecked();
    expect(screen.getByText('Off')).toBeInTheDocument();
  });

  it('keeps failed writes visible and refreshes the saved value', async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error('revision conflict'));
    show();
    await userEvent.click(await screen.findByRole('switch'));
    expect(await screen.findByRole('alert')).toHaveTextContent('The change was not saved');
    expect(screen.getByRole('switch')).toBeChecked();
  });

  it('shows read failures without inventing an enabled setting', async () => {
    vi.mocked(apiClient.get).mockRejectedValue(new Error('unavailable'));
    show();
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be loaded');
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });

  it('lets members see status without mutation controls', async () => {
    state.admin = false;
    show('flow-123');
    expect(await screen.findByText(/A platform administrator/)).toBeInTheDocument();
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
  });
});
