import { apiClient } from './api';
import { clearTokens, getCurrentUserFromToken, getIdToken, parseTokenPayload, refreshToken } from './auth';

export interface Workspace {
  org_id: string;
  name: string;
  user_id: string;
  role: string;
  team_id: string;
  department_id: string;
  is_current: boolean;
}

export async function listWorkspaces(signal?: AbortSignal): Promise<Workspace[]> {
  const response = await apiClient.get<{ items: Workspace[] }>('/auth/workspaces', signal);
  return response.items;
}

/** Refresh both tokens before loading any data from the selected workspace. */
export async function switchWorkspace(orgId: string): Promise<void> {
  const selected = await apiClient.post<Workspace>('/auth/workspaces/select', { org_id: orgId });
  try {
    const { token } = await refreshToken({ fresh: true });
    const user = getCurrentUserFromToken();
    const expected: Record<string, string> = {
      'custom:org_id': selected.org_id,
      'custom:team_id': selected.team_id,
      'custom:department_id': selected.department_id,
      'custom:role': selected.role,
    };
    // Validate access scope as well as the ID token used by the UI. Missing
    // empty team/dept claims are equivalent to empty strings.
    for (const jwt of [token, getIdToken()]) {
      const payload = jwt ? parseTokenPayload<Record<string, unknown>>(jwt) : null;
      if (!payload || Object.entries(expected).some(([key, value]) => (payload[key] ?? '') !== value)) {
        throw new Error('The refreshed session does not match the selected organization');
      }
    }
    if (!user || user.orgId !== selected.org_id) {
      throw new Error('Could not read the selected workspace session');
    }
  } catch {
    // Server selection may already be saved. A new login recovers the current
    // claims; do not leave old data visible under a newly selected label.
    clearTokens();
    window.location.assign('/login?error=workspace_refresh_required');
    throw new Error('Please sign in again to finish switching organizations.');
  }
  // A full navigation drops in-flight requests, component state and query
  // caches, including pages whose cache keys predate workspace switching.
  window.location.assign('/');
}
