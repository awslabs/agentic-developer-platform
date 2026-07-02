/**
 * Connections page — link external services (GitHub, Slack, etc.) to ADP.
 *
 * Issue #465: GitHub App install + auto-provision flow.
 * Issue #2596: GitHub App registration + lifecycle states (platform_admin gated).
 *
 * URL: /settings/connections
 *
 * On arrival with ?success=1 the page shows a success toast and refreshes
 * the connection list. On ?error=<code>&message=<msg> it shows an error toast.
 * On arrival with ?github_app=registered it shows a success toast and refetches
 * the app status.
 */

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useToast } from '@/contexts/ToastContext';
import { FreeTierBanner } from '@/components/FreeTierBanner';
import { useAuth } from '@/hooks/useAuth';
import { AdminRole } from '@/types';
import {
  deleteGitHubConnection,
  disconnectGitHubApp,
  getGitHubAppStatus,
  listConnections,
  rotateGitHubAppKey,
  startGitHubAppRegistration,
  startGitHubInstall,
  type AppStatusResponse,
  type GitHubConnectionItem,
} from '@/services/connections';
import { GitHubTile } from './components/GitHubTile';

/** Well-known org ID for the adp-default free-tier tenant. */
const ADP_DEFAULT_ORG_ID = '00000000-0000-4000-a000-000000000001';

export default function Connections() {
  const [searchParams, setSearchParams] = useSearchParams();
  const toast = useToast();
  const { user, hasRole } = useAuth();

  const isPlatformAdmin = hasRole(AdminRole.PLATFORM_ADMIN);

  const [connections, setConnections] = useState<GitHubConnectionItem[]>([]);
  const [appStatus, setAppStatus] = useState<AppStatusResponse | null>(null);
  const [appStatusError, setAppStatusError] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isInstalling, setIsInstalling] = useState(false);

  // -------------------------------------------------------------------------
  // Load connections + app status
  // -------------------------------------------------------------------------

  const loadConnections = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await listConnections();
      setConnections(data?.connections ?? []);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load connections';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [toast]);

  const loadAppStatus = useCallback(async () => {
    if (!isPlatformAdmin) return;
    try {
      const status = await getGitHubAppStatus();
      setAppStatus(status);
      setAppStatusError(false);
    } catch {
      // Mark that we failed to fetch status so the tile can distinguish
      // "not registered" from "status unavailable" and avoid showing the
      // dangerous "Set up GitHub App" CTA that could overwrite a live App.
      setAppStatusError(true);
    }
  }, [isPlatformAdmin]);

  useEffect(() => {
    loadConnections();
    loadAppStatus();
  }, [loadConnections, loadAppStatus]);

  // -------------------------------------------------------------------------
  // Handle redirect back from GitHub (success or error or app registered)
  // -------------------------------------------------------------------------

  useEffect(() => {
    const success = searchParams.get('success');
    const errorCode = searchParams.get('error');
    const errorMessage = searchParams.get('message');
    const githubApp = searchParams.get('github_app');

    if (success === '1') {
      toast.success('GitHub connected! You can now trigger agents from this org.');
      loadConnections();
      setSearchParams({}, { replace: true });
    } else if (githubApp === 'registered') {
      toast.success('GitHub App registered successfully!');
      loadAppStatus();
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
  // Disconnect handler (installation-level)
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
  // Registration handler — manifest flow (Issue #2596)
  // -------------------------------------------------------------------------

  const handleRegister = async (ownerType: 'user' | 'org', org?: string) => {
    try {
      const result = await startGitHubAppRegistration({ owner_type: ownerType, org });
      if (result.status === 'already_registered') {
        toast.success('GitHub App is already registered.');
        await loadAppStatus();
        return;
      }
      if (result.post_url && result.manifest) {
        // Submit manifest to GitHub in the same tab (preserves SPA session for
        // the callback redirect). State nonce is included as a form field so
        // GitHub echoes it back as ?state= on the redirect.
        submitManifestToGitHub(result.post_url, JSON.stringify(result.manifest), result.state);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to start registration';
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Rotate key handler (Issue #2596)
  // -------------------------------------------------------------------------

  const handleRotateKey = async () => {
    try {
      const result = await rotateGitHubAppKey();
      toast.success(result.message || 'Key rotated successfully.');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to rotate key';
      toast.error(message);
      throw err;
    }
  };

  // -------------------------------------------------------------------------
  // Disconnect app handler (Issue #2596)
  // -------------------------------------------------------------------------

  const handleDisconnectApp = async () => {
    try {
      const result = await disconnectGitHubApp();
      toast.success(result.message || 'GitHub App disconnected.');
      setAppStatus({ registered: false, app_slug: null, app_id: null, owner_type: null, created_at: null });
      setConnections([]);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to disconnect app';
      toast.error(message);
      throw err;
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

      {/* Free tier banner — shown when user is on adp-default */}
      {user?.orgId === ADP_DEFAULT_ORG_ID && <FreeTierBanner />}

      {/* GitHub section */}
      <div className="space-y-6">
        <GitHubTile
          connections={connections}
          isLoading={isLoading}
          onInstall={handleInstall}
          onDisconnect={handleDisconnect}
          isInstalling={isInstalling}
          isPlatformAdmin={isPlatformAdmin}
          appStatus={appStatus}
          appStatusError={appStatusError}
          onRegister={handleRegister}
          onRotateKey={handleRotateKey}
          onDisconnectApp={handleDisconnectApp}
        />

        {/* Future integrations — placeholder tiles */}
        <ComingSoonTile name="Slack" icon="&#128172;" />
        <ComingSoonTile name="Google" icon="&#128309;" />
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

// ---------------------------------------------------------------------------
// Helper: submit manifest to GitHub via form POST (same tab)
// ---------------------------------------------------------------------------

function submitManifestToGitHub(postUrl: string, manifest: string, state?: string | null): void {
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = postUrl;
  // No target="_blank" — stay in the same tab so the callback redirect
  // lands in a window that already has the SPA auth session (Issue #2682).

  const manifestInput = document.createElement('input');
  manifestInput.type = 'hidden';
  manifestInput.name = 'manifest';
  manifestInput.value = manifest;
  form.appendChild(manifestInput);

  // GitHub echoes the state field back as ?state= on the post-create redirect.
  // Without this, the callback receives no state and fails with missing_state.
  if (state) {
    const stateInput = document.createElement('input');
    stateInput.type = 'hidden';
    stateInput.name = 'state';
    stateInput.value = state;
    form.appendChild(stateInput);
  }

  document.body.appendChild(form);
  form.submit();
  document.body.removeChild(form);
}
