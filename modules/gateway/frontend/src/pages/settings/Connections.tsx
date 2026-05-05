/**
 * Connections page — link external services (GitHub, Slack, etc.) to ADP.
 *
 * Issue #465: GitHub App install + auto-provision flow.
 *
 * URL: /settings/connections
 *
 * On arrival with ?success=1 the page shows a success toast and refreshes
 * the connection list. On ?error=<code>&message=<msg> it shows an error toast.
 */

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useToast } from '@/contexts/ToastContext';
import {
  deleteGitHubConnection,
  listConnections,
  startGitHubInstall,
  type GitHubConnectionItem,
} from '@/services/connections';
import { GitHubTile } from './components/GitHubTile';

export default function Connections() {
  const [searchParams, setSearchParams] = useSearchParams();
  const toast = useToast();

  const [connections, setConnections] = useState<GitHubConnectionItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isInstalling, setIsInstalling] = useState(false);

  // -------------------------------------------------------------------------
  // Load connections
  // -------------------------------------------------------------------------

  const loadConnections = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await listConnections();
      setConnections(data.connections);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load connections';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    loadConnections();
  }, [loadConnections]);

  // -------------------------------------------------------------------------
  // Handle redirect back from GitHub (success or error)
  // -------------------------------------------------------------------------

  useEffect(() => {
    const success = searchParams.get('success');
    const errorCode = searchParams.get('error');
    const errorMessage = searchParams.get('message');

    if (success === '1') {
      toast.success('GitHub connected! You can now trigger agents from this org.');
      // Reload so the new installation appears in the list
      loadConnections();
      // Clean up URL params
      setSearchParams({}, { replace: true });
    } else if (errorCode) {
      const displayMessage =
        errorMessage || `GitHub connection failed (${errorCode}). Please try again.`;
      toast.error(displayMessage);
      setSearchParams({}, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -------------------------------------------------------------------------
  // Install handler — POST install-start → redirect to GitHub
  // -------------------------------------------------------------------------

  const handleInstall = async () => {
    setIsInstalling(true);
    try {
      const result = await startGitHubInstall();
      window.location.href = result.install_url;
      // Note: page navigates away; isInstalling stays true intentionally
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to start GitHub install flow';
      toast.error(message);
      setIsInstalling(false);
    }
  };

  // -------------------------------------------------------------------------
  // Disconnect handler
  // -------------------------------------------------------------------------

  const handleDisconnect = async (installationId: number) => {
    try {
      await deleteGitHubConnection(installationId);
      toast.success('GitHub installation disconnected.');
      await loadConnections();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to disconnect installation';
      toast.error(message);
      throw err; // Let InstallationCard reset its state
    }
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      {/* Page header */}
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Connections</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Connect external services to enable agent triggers and integrations.
        </p>
      </div>

      {/* GitHub section */}
      <div className="space-y-6">
        <GitHubTile
          connections={connections}
          isLoading={isLoading}
          onInstall={handleInstall}
          onDisconnect={handleDisconnect}
          isInstalling={isInstalling}
        />

        {/* Future integrations — placeholder tiles */}
        <ComingSoonTile name="Slack" icon="💬" />
        <ComingSoonTile name="Google" icon="🔵" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// ComingSoonTile — placeholder for future integrations
// ---------------------------------------------------------------------------

function ComingSoonTile({ name, icon }: { name: string; icon: string }) {
  return (
    <section className="rounded-xl border border-gray-200 bg-white p-6 opacity-60 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-2xl" aria-hidden="true">
            {icon}
          </span>
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{name}</h2>
            <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">
              Connect your {name} workspace to enable agent integrations.
            </p>
          </div>
        </div>
        <span className="rounded-full bg-gray-100 px-3 py-1 text-xs font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400">
          Coming soon
        </span>
      </div>
    </section>
  );
}
