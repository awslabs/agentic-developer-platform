import { useEffect, useState } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { listWorkspaces, switchWorkspace, type Workspace } from '@/services/workspaces';

export function WorkspaceSelector() {
  const { user } = useAuth();
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    listWorkspaces(controller.signal)
      .then((items) => { if (!controller.signal.aborted) setWorkspaces(items); })
      .catch(() => { if (!controller.signal.aborted) setError('Could not load organizations.'); })
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
            <option value={user?.orgId ?? ''}>{loading ? 'Loading organizations…' : 'Select organization'}</option>
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
      </div>
    </div>
  );
}
