import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { getAccessStatus } from '@/services/onboarding';
import { WorkspaceSelector } from '@/components/WorkspaceSelector';
import { listWorkspaces, switchWorkspace, type Workspace } from '@/services/workspaces';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'login-sub', orgId: 'home' } }) }));
vi.mock('@/services/onboarding', () => ({ getAccessStatus: vi.fn() }));
vi.mock('@/services/workspaces', () => ({ listWorkspaces: vi.fn(), switchWorkspace: vi.fn() }));
const workspaces: Workspace[] = [
  { org_id: 'home', name: 'Pranavsharma1000', user_id: 'home-user', role: 'org_admin', team_id: '', department_id: '', is_current: true },
  { org_id: 'work', name: 'SOPHOS-IT', user_id: 'work-user', role: 'member', team_id: '', department_id: '', is_current: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAccessStatus).mockImplementation(() => new Promise(() => {}));
  vi.mocked(listWorkspaces).mockResolvedValue(workspaces);
});

describe('organization selector', () => {
  it('shows all memberships and current org, including an org without GitHub', async () => {
    render(<WorkspaceSelector />);
    expect(await screen.findByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Organization' })).toHaveValue('home');
  });

  it('prevents duplicate switches while the new session loads', async () => {
    vi.mocked(switchWorkspace).mockReturnValue(new Promise(() => {}));
    render(<WorkspaceSelector />);
    await screen.findByRole('option', { name: 'SOPHOS-IT' });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'work' } });
    expect(switchWorkspace).toHaveBeenCalledWith('work');
    expect(screen.getByRole('combobox')).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Switching organization');
  });

  it('keeps the current org label and allows retry on switch failure', async () => {
    vi.mocked(switchWorkspace).mockRejectedValue({ detail: 'Membership no longer exists' });
    render(<WorkspaceSelector />);
    await screen.findByRole('option', { name: 'SOPHOS-IT' });
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'work' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Membership no longer exists');
    expect(screen.getByRole('combobox')).toHaveValue('home');
    expect(screen.getByRole('combobox')).not.toBeDisabled();
  });

  // #8246: the list read is refused, not failing, once this actor's membership
  // of the current tenant has been revoked. Saying "Could not load
  // organizations" and offering a reload described a permanent refusal as a
  // transient one, and was the only message a revoked actor saw anywhere.
  it('reports a refused membership list without offering a futile reload', async () => {
    vi.mocked(listWorkspaces).mockRejectedValueOnce({ status: 403, detail: { error: 'tenant_membership_revoked' } });
    render(<WorkspaceSelector />);
    expect(await screen.findByText('Your access to this organization is no longer available.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reload organizations' })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Organization' })).toBeDisabled();
  });

  it('reloads after a membership-list failure', async () => {
    vi.mocked(listWorkspaces).mockRejectedValueOnce(new Error('Offline'));
    render(<WorkspaceSelector />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load organizations');
    fireEvent.click(screen.getByRole('button', { name: 'Reload organizations' }));
    await waitFor(() => expect(screen.getByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument());
  });
});

it('keeps organizations usable during discovery and adds newly discovered memberships', async () => {
  let finish!: () => void;
  vi.mocked(getAccessStatus).mockImplementation(() => new Promise(resolve => {
    finish = () => resolve({ status: 'registered' });
  }));
  vi.mocked(listWorkspaces).mockResolvedValueOnce([workspaces[0]]).mockResolvedValue(workspaces);
  render(<WorkspaceSelector />);
  await waitFor(() => expect(getAccessStatus).toHaveBeenCalledWith(true, expect.any(AbortSignal)));
  expect(screen.getByRole('combobox')).toBeEnabled();
  expect(screen.queryByRole('option', { name: 'SOPHOS-IT' })).not.toBeInTheDocument();
  await act(async () => finish());
  expect(await screen.findByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument();
  expect(screen.getByRole('combobox')).toHaveValue('home');
});

it('does not discard existing organizations when provider discovery fails', async () => {
  vi.mocked(getAccessStatus).mockRejectedValue(new Error('Provider unavailable'));
  render(<WorkspaceSelector />);
  await waitFor(() => expect(getAccessStatus).toHaveBeenCalled());
  expect(screen.getByRole('combobox')).toBeEnabled();
  expect(screen.getByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('retains usable organizations if the background list refresh fails temporarily', async () => {
  vi.mocked(getAccessStatus).mockResolvedValue({ status: 'registered' });
  vi.mocked(listWorkspaces).mockResolvedValueOnce(workspaces).mockRejectedValueOnce(new Error('Offline'));
  render(<WorkspaceSelector />);
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not refresh organizations.');
  expect(screen.getByRole('combobox')).toBeEnabled();
  expect(screen.getByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument();
  expect(screen.getByRole('combobox')).toHaveValue('home');
});

it('clears organizations when the background refresh reports revoked access', async () => {
  vi.mocked(getAccessStatus).mockResolvedValue({ status: 'registered' });
  vi.mocked(listWorkspaces).mockResolvedValueOnce(workspaces).mockRejectedValueOnce({ status: 403 });
  render(<WorkspaceSelector />);
  expect(await screen.findByText('Your access to this organization is no longer available.')).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'SOPHOS-IT' })).not.toBeInTheDocument();
  expect(screen.getByRole('combobox')).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Reload organizations' })).not.toBeInTheDocument();
});

it('cancels discovery on unmount and ignores a late result', async () => {
  let finish!: () => void;
  vi.mocked(getAccessStatus).mockImplementation(() => new Promise(resolve => {
    finish = () => resolve({ status: 'registered' });
  }));
  const view = render(<WorkspaceSelector />);
  await waitFor(() => expect(getAccessStatus).toHaveBeenCalled());
  const signal = vi.mocked(getAccessStatus).mock.calls[0][1]!;
  view.unmount();
  expect(signal.aborted).toBe(true);
  await act(async () => finish());
  expect(listWorkspaces).toHaveBeenCalledTimes(1);
});
