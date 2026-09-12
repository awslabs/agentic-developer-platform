import { beforeEach, describe, expect, it, vi } from 'vitest';
import { switchWorkspace, listWorkspaces, type Workspace } from '@/services/workspaces';
import { apiClient } from '@/services/api';
import * as auth from '@/services/auth';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
vi.mock('@/services/auth', async () => ({
  ...await vi.importActual<typeof import('@/services/auth')>('@/services/auth'),
  refreshToken: vi.fn(), getCurrentUserFromToken: vi.fn(), getIdToken: vi.fn(), clearTokens: vi.fn(),
}));

const selected: Workspace = { org_id: 'work', name: 'SOPHOS-IT', user_id: 'work-user', role: 'member', team_id: '', department_id: '', is_current: true };
const token = (overrides = {}) => `header.${btoa(JSON.stringify({ 'custom:org_id': 'work', 'custom:role': 'member', ...overrides }))}.signature`;
const assign = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal('location', { ...window.location, assign });
  vi.mocked(apiClient.post).mockResolvedValue(selected);
  vi.mocked(auth.refreshToken).mockResolvedValue({ token: token(), expiresAt: '' });
  vi.mocked(auth.getIdToken).mockReturnValue(token());
  vi.mocked(auth.getCurrentUserFromToken).mockReturnValue({ id: 'sub', orgId: 'work', permissions: [], createdAt: '' });
});

describe('workspace session transition', () => {
  it('lists memberships without requiring a connection', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ items: [selected] });
    expect(await listWorkspaces()).toEqual([selected]);
    expect(apiClient.get).toHaveBeenCalledWith('/auth/workspaces', undefined);
  });

  it('refreshes both tokens then resets the entire page and cache', async () => {
    await switchWorkspace('work');
    expect(apiClient.post).toHaveBeenCalledWith('/auth/workspaces/select', { org_id: 'work' });
    expect(auth.refreshToken).toHaveBeenCalledWith({ fresh: true });
    expect(assign).toHaveBeenCalledWith('/');
    expect(auth.clearTokens).not.toHaveBeenCalled();
  });

  it('keeps the existing session when the server rejects a switch', async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error('Membership removed'));
    await expect(switchWorkspace('work')).rejects.toThrow('Membership removed');
    expect(auth.refreshToken).not.toHaveBeenCalled();
    expect(auth.clearTokens).not.toHaveBeenCalled();
    expect(assign).not.toHaveBeenCalled();
  });

  it.each(['refresh failure', 'wrong access org', 'stale access team', 'stale ID role', 'missing ID token', 'unreadable user'])('requires sign-in after %s', async (scenario) => {
    if (scenario === 'refresh failure') vi.mocked(auth.refreshToken).mockRejectedValue(new Error('Expired'));
    if (scenario === 'wrong access org') vi.mocked(auth.refreshToken).mockResolvedValue({ token: token({ 'custom:org_id': 'home' }), expiresAt: '' });
    if (scenario === 'stale access team') vi.mocked(auth.refreshToken).mockResolvedValue({ token: token({ 'custom:team_id': 'old-team' }), expiresAt: '' });
    if (scenario === 'stale ID role') vi.mocked(auth.getIdToken).mockReturnValue(token({ 'custom:role': 'org_admin' }));
    if (scenario === 'missing ID token') vi.mocked(auth.getIdToken).mockReturnValue(null);
    if (scenario === 'unreadable user') vi.mocked(auth.getCurrentUserFromToken).mockReturnValue(null);
    await expect(switchWorkspace('work')).rejects.toThrow('Please sign in again');
    expect(auth.clearTokens).toHaveBeenCalledOnce();
    expect(assign).toHaveBeenCalledWith('/login?error=workspace_refresh_required');
    expect(assign).not.toHaveBeenCalledWith('/');
  });
});
