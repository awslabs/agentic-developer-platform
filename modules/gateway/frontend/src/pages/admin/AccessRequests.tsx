import { useState, useEffect, useCallback } from 'react';
import {
  getAccessRequests,
  approveAccessRequest,
  denyAccessRequest,
  type AccessRequestItem,
} from '@/services/onboarding';

export default function AccessRequests() {
  const [requests, setRequests] = useState<AccessRequestItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionInProgress, setActionInProgress] = useState<string | null>(null);
  const [denyModalId, setDenyModalId] = useState<string | null>(null);
  const [denyNote, setDenyNote] = useState('');
  const [toast, setToast] = useState<string | null>(null);

  const fetchRequests = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const items = await getAccessRequests();
      // Sort by created_at descending (newest first)
      items.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      setRequests(items);
    } catch {
      setError('Failed to load access requests.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRequests();
  }, [fetchRequests]);

  // Auto-dismiss toast
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  const handleApprove = async (item: AccessRequestItem) => {
    setActionInProgress(item.id);
    try {
      await approveAccessRequest(item.id);
      setRequests((prev) => prev.filter((r) => r.id !== item.id));
      setToast(`Approved ${item.target_login} into tenant ${item.proposed_tenant_id}`);
    } catch {
      setError(`Failed to approve request for ${item.target_login}.`);
    } finally {
      setActionInProgress(null);
    }
  };

  const handleDenyConfirm = async () => {
    if (!denyModalId) return;
    const item = requests.find((r) => r.id === denyModalId);
    if (!item) return;

    setActionInProgress(item.id);
    try {
      await denyAccessRequest(item.id, denyNote || undefined);
      setRequests((prev) => prev.filter((r) => r.id !== item.id));
      setToast(`Denied ${item.target_login}`);
    } catch {
      setError(`Failed to deny request for ${item.target_login}.`);
    } finally {
      setActionInProgress(null);
      setDenyModalId(null);
      setDenyNote('');
    }
  };

  if (isLoading) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Access Requests</h1>
        <p className="mt-4 text-gray-500 dark:text-gray-400">Loading...</p>
      </div>
    );
  }

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Access Requests</h1>

      {/* Toast notification */}
      {toast && (
        <div className="mt-4 rounded-md bg-green-50 p-3 text-sm text-green-800 dark:bg-green-900/20 dark:text-green-200" role="status">
          {toast}
        </div>
      )}

      {error && (
        <div className="mt-4 rounded-md bg-red-50 p-3 text-sm text-red-800 dark:bg-red-900/20 dark:text-red-200" role="alert">
          {error}
        </div>
      )}

      {requests.length === 0 ? (
        <p className="mt-6 text-gray-500 dark:text-gray-400">No pending access requests.</p>
      ) : (
        <div className="mt-6 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 dark:border-gray-700">
              <tr>
                <th className="pb-3 font-medium text-gray-500 dark:text-gray-400">User</th>
                <th className="pb-3 font-medium text-gray-500 dark:text-gray-400">Tenant ID</th>
                <th className="pb-3 font-medium text-gray-500 dark:text-gray-400">Motivation</th>
                <th className="pb-3 font-medium text-gray-500 dark:text-gray-400">Requested</th>
                <th className="pb-3 font-medium text-gray-500 dark:text-gray-400">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
              {requests.map((item) => (
                <tr key={item.id}>
                  <td className="py-3">
                    <div className="flex items-center gap-2">
                      {item.avatar_url && (
                        <img
                          src={item.avatar_url}
                          alt=""
                          className="h-8 w-8 rounded-full"
                        />
                      )}
                      <span className="font-medium text-gray-900 dark:text-white">
                        {item.target_login}
                      </span>
                    </div>
                  </td>
                  <td className="py-3 text-gray-700 dark:text-gray-300">
                    {item.proposed_tenant_id}
                  </td>
                  <td className="py-3 max-w-xs">
                    <p className="truncate text-gray-600 dark:text-gray-400" title={item.motivation}>
                      {item.motivation}
                    </p>
                  </td>
                  <td className="py-3 text-gray-500 dark:text-gray-400">
                    {new Date(item.created_at).toLocaleDateString()}
                  </td>
                  <td className="py-3">
                    <div className="flex gap-2">
                      <button
                        onClick={() => handleApprove(item)}
                        disabled={actionInProgress === item.id}
                        className="rounded bg-green-600 px-3 py-1 text-xs font-medium text-white hover:bg-green-700 disabled:opacity-50"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => setDenyModalId(item.id)}
                        disabled={actionInProgress === item.id}
                        className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
                      >
                        Deny
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Deny confirmation modal */}
      {denyModalId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-sm rounded-lg bg-white p-6 shadow-lg dark:bg-gray-800">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">Deny Request</h2>
            <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
              Optionally provide a reason for denying this request.
            </p>
            <textarea
              value={denyNote}
              onChange={(e) => setDenyNote(e.target.value)}
              rows={3}
              className="mt-3 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 dark:bg-gray-700 dark:text-white"
              placeholder="Reason (optional)"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => { setDenyModalId(null); setDenyNote(''); }}
                className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                Cancel
              </button>
              <button
                onClick={handleDenyConfirm}
                disabled={!!actionInProgress}
                className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
              >
                Deny
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
