import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { WorkspaceSelector } from '@/components/WorkspaceSelector';
import { listWorkspaces, switchWorkspace, type Workspace } from '@/services/workspaces';

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { id: 'login-sub', orgId: 'home' } }) }));
vi.mock('@/services/workspaces', () => ({ listWorkspaces: vi.fn(), switchWorkspace: vi.fn() }));
const workspaces: Workspace[] = [
  { org_id: 'home', name: 'Pranavsharma1000', user_id: 'home-user', role: 'org_admin', team_id: '', department_id: '', is_current: true },
  { org_id: 'work', name: 'SOPHOS-IT', user_id: 'work-user', role: 'member', team_id: '', department_id: '', is_current: false },
];

beforeEach(() => {
  vi.clearAllMocks();
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

  it('reloads after a membership-list failure', async () => {
    vi.mocked(listWorkspaces).mockRejectedValueOnce(new Error('Offline'));
    render(<WorkspaceSelector />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load organizations');
    fireEvent.click(screen.getByRole('button', { name: 'Reload organizations' }));
    await waitFor(() => expect(screen.getByRole('option', { name: 'SOPHOS-IT' })).toBeInTheDocument());
  });
});
