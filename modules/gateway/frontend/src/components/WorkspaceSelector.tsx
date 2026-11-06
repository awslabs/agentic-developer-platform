import { useEffect, useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { listWorkspaces, switchWorkspace, type Workspace } from '@/services/workspaces';
import { getAccessStatus } from '@/services/onboarding';

/**
 * A refused membership list is not a loading problem — #8246.
 *
 * The list read is refused outright once the signed-in actor's membership of
 * the current tenant has been revoked. Reporting that as "Could not load
 * organizations." with a reload button describes a permanent refusal as a
 * transient one and invites a retry that cannot succeed, so a forbidden
 * response is reported separately and without the retry.
 */
function isMembershipRefused(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 403;
}

export function WorkspaceSelector() {
  const { user } = useAuth();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refused, setRefused] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setRefused(false);
    const onFailure = (err: unknown, refreshing = false) => {
      if (controller.signal.aborted) return;
      if (isMembershipRefused(err)) {
        setWorkspaces([]);
        setRefused(true);
      } else {
        if (!refreshing) setWorkspaces([]);
        setError(refreshing ? 'Could not refresh organizations.' : 'Could not load organizations.');
      }
    };
    const showWorkspaces = (items: Workspace[]) => {
      if (!controller.signal.aborted) setWorkspaces(items);
    };
    listWorkspaces(controller.signal)
      .then((items) => {
        showWorkspaces(items);
        if (controller.signal.aborted) return;
        // Existing organizations are usable immediately. Keep login-time
        // discovery of newly connected organizations, without gating the page
        // or holding the selector disabled while Cognito/GitHub respond.
        void getAccessStatus(true, controller.signal).then((status) => {
          if (controller.signal.aborted || status.status !== 'registered') return;
          return listWorkspaces(controller.signal).then(showWorkspaces).catch((err: unknown) => onFailure(err, true));
        }).catch(() => { /* Discovery is best-effort; existing access is unchanged. */ });
      })
      .catch(onFailure)
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [user?.id, user?.orgId, attempt]);

  const select = async (orgId: string) => {
    if (!orgId || orgId === user?.orgId || switching) return;
    setSwitching(true);
    setError(null);
    try {
      await switchWorkspace(orgId);
    } catch (err) {
      const detail = (err as { detail?: unknown })?.detail;
      setError(err instanceof Error ? err.message : typeof detail === 'string' ? detail : 'Could not switch organization. Please try again.');
      setSwitching(false);
    }
  };

  return (
    <div className="py-2" aria-busy={loading || switching}>
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="workspace-selector" className="text-sm font-medium text-gray-700 dark:text-gray-300">Organization</label>
        <select
          id="workspace-selector"
          value={user?.orgId ?? ''}
          disabled={loading || switching || workspaces.length === 0}
          onChange={(event) => void select(event.target.value)}
          className="max-w-64 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
        >
          {!workspaces.some((workspace) => workspace.org_id === user?.orgId) && (
            <option value={user?.orgId ?? ''}>
              {loading ? 'Loading organizations…' : refused ? 'No organization available' : 'Select organization'}
            </option>
          )}
          {workspaces.map((workspace) => <option key={workspace.org_id} value={workspace.org_id}>{workspace.name}</option>)}
        </select>
        {switching && <span role="status" className="text-sm text-gray-600 dark:text-gray-300">Switching organization…</span>}
        {error && (
          <div role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}{' '}
            <button type="button" onClick={() => setAttempt((value) => value + 1)} className="underline">Reload organizations</button>
          </div>
        )}
        {refused && !error && (
          <span className="text-sm text-gray-600 dark:text-gray-300">
            Your access to this organization is no longer available.
          </span>
        )}
      </div>
    </div>
  );
}
