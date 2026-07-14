/**
 * Tenant Org Links — admin page for linking/unlinking GitHub orgs to a tenant.
 *
 * Issue #2954: Platform-admin can link multiple GitHub orgs to one parent
 * tenant (many:many, attach-forward-only).
 */

import { useState, useEffect, useCallback } from 'react';
import { getOrganizations } from '@/services/admin';
import {
  getLinkedOrgs,
  linkOrgToTenant,
  unlinkOrgFromTenant,
  type LinkedOrgItem,
} from '@/services/tenantLinks';

interface TenantOption {
  id: string;
  name: string;
}

export default function TenantOrgLinks() {
  const [tenants, setTenants] = useState<TenantOption[]>([]);
  const [selectedTenantId, setSelectedTenantId] = useState<string>('');
  const [linkedOrgs, setLinkedOrgs] = useState<LinkedOrgItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isLinking, setIsLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [linkInput, setLinkInput] = useState('');
  const [unlinkTarget, setUnlinkTarget] = useState<LinkedOrgItem | null>(null);

  // Load tenants on mount
  const fetchTenants = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const resp = await getOrganizations({ page: 1, pageSize: 100 });
      setTenants(resp.items.map((o) => ({ id: o.id, name: o.name })));
    } catch {
      setError('Failed to load organizations.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTenants();
  }, [fetchTenants]);

  // Load linked orgs when tenant selection changes
  const fetchLinkedOrgs = useCallback(async (tenantId: string) => {
    if (!tenantId) {
      setLinkedOrgs([]);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const resp = await getLinkedOrgs(tenantId);
      setLinkedOrgs(resp.linkedOrgs);
    } catch {
      setError('Failed to load linked organizations.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchLinkedOrgs(selectedTenantId);
  }, [selectedTenantId, fetchLinkedOrgs]);

  // Auto-dismiss toast
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(timer);
  }, [toast]);

  const handleLink = async () => {
    if (!selectedTenantId || !linkInput.trim()) return;
    setIsLinking(true);
    setError(null);
    try {
      const resp = await linkOrgToTenant(selectedTenantId, linkInput.trim());
      setToast(`Linked "${resp.orgName}" to this tenant.`);
      setLinkInput('');
      await fetchLinkedOrgs(selectedTenantId);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to link organization.';
      setError(message);
    } finally {
      setIsLinking(false);
    }
  };

  const handleUnlink = async (org: LinkedOrgItem) => {
    if (!selectedTenantId || !org.githubOrgId) return;
    setError(null);
    try {
      await unlinkOrgFromTenant(selectedTenantId, org.githubOrgId);
      setToast(`Unlinked "${org.orgName}" from this tenant.`);
      setUnlinkTarget(null);
      await fetchLinkedOrgs(selectedTenantId);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : 'Failed to unlink organization.';
      setError(message);
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">
        Tenant Org Links
      </h1>
      <p className="text-gray-600 dark:text-gray-400 mb-6">
        Link multiple GitHub organizations to a single parent tenant. Members of
        linked orgs will resolve to the parent tenant on their next login.
      </p>

      {/* Attach-forward-only warning */}
      <div className="mb-6 rounded-md border border-yellow-300 bg-yellow-50 dark:bg-yellow-900/20 dark:border-yellow-600 p-4">
        <p className="text-sm font-medium text-yellow-800 dark:text-yellow-200">
          Attach-forward-only: Linking an org does NOT migrate existing users or
          repos from the linked org&apos;s own tenant. Only new logins after the
          link will resolve to the parent tenant.
        </p>
      </div>

      {/* Toast notification */}
      {toast && (
        <div className="mb-4 rounded-md bg-green-50 dark:bg-green-900/20 border border-green-300 dark:border-green-600 p-3">
          <p className="text-sm text-green-800 dark:text-green-200">{toast}</p>
        </div>
      )}

      {/* Error display */}
      {error && (
        <div className="mb-4 rounded-md bg-red-50 dark:bg-red-900/20 border border-red-300 dark:border-red-600 p-3">
          <p className="text-sm text-red-800 dark:text-red-200">{error}</p>
        </div>
      )}

      {/* Tenant selector */}
      <div className="mb-6">
        <label
          htmlFor="tenant-select"
          className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1"
        >
          Parent Tenant
        </label>
        <select
          id="tenant-select"
          value={selectedTenantId}
          onChange={(e) => setSelectedTenantId(e.target.value)}
          className="w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
        >
          <option value="">Select a tenant...</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      {/* Link form */}
      {selectedTenantId && (
        <div className="mb-6 rounded-md border border-gray-200 dark:border-gray-700 p-4">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">
            Link a GitHub Organization
          </h2>
          <div className="flex gap-3">
            <input
              type="text"
              value={linkInput}
              onChange={(e) => setLinkInput(e.target.value)}
              placeholder="GitHub Org ID (numeric)"
              className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400"
              aria-label="GitHub Organization ID"
            />
            <button
              onClick={handleLink}
              disabled={isLinking || !linkInput.trim()}
              className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isLinking ? 'Linking...' : 'Link Org'}
            </button>
          </div>
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Enter the numeric GitHub Organization ID of the org you want to link
            to this tenant.
          </p>
        </div>
      )}

      {/* Linked orgs list */}
      {selectedTenantId && (
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">
            Linked Organizations
          </h2>
          {isLoading ? (
            <p className="text-gray-500 dark:text-gray-400 text-sm">Loading...</p>
          ) : linkedOrgs.length === 0 ? (
            <p className="text-gray-500 dark:text-gray-400 text-sm">
              No organizations are currently linked to this tenant.
            </p>
          ) : (
            <div className="border border-gray-200 dark:border-gray-700 rounded-md overflow-hidden">
              <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                <thead className="bg-gray-50 dark:bg-gray-800">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      Org Name
                    </th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      GitHub Org ID
                    </th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                  {linkedOrgs.map((org) => (
                    <tr key={org.orgId}>
                      <td className="px-4 py-3 text-sm text-gray-900 dark:text-gray-100">
                        {org.orgName}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400 font-mono">
                        {org.githubOrgId ?? '—'}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => setUnlinkTarget(org)}
                          className="text-sm text-red-600 dark:text-red-400 hover:text-red-800 dark:hover:text-red-300"
                        >
                          Unlink
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Unlink confirmation modal */}
      {unlinkTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="bg-white dark:bg-gray-800 rounded-lg shadow-xl p-6 max-w-md w-full mx-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">
              Confirm Unlink
            </h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
              Are you sure you want to unlink <strong>{unlinkTarget.orgName}</strong>{' '}
              from this tenant? New logins from this org will resolve to its own
              standalone tenant going forward.
            </p>
            <div className="flex justify-end gap-3">
              <button
                onClick={() => setUnlinkTarget(null)}
                className="rounded-md border border-gray-300 dark:border-gray-600 px-4 py-2 text-sm text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700"
              >
                Cancel
              </button>
              <button
                onClick={() => handleUnlink(unlinkTarget)}
                className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
              >
                Unlink
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
