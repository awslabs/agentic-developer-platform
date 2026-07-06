/**
 * GitHubTile — GitHub App connection section on the Connections page.
 *
 * Issue #465: Shows current installations + install button.
 * Issue #2596: Registration + lifecycle states (platform_admin gated).
 *
 * Renders one of four states:
 * - platform_admin + not registered → "Set up GitHub App" with owner choice
 * - platform_admin + registered → app info + Rotate/Disconnect + install UI
 * - non-platform_admin + not registered → "ask a platform admin" message
 * - any admin + registered → existing install/connect UI (unchanged)
 */

import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { type GitHubConnectionItem } from '@/services/connections';
import type { AppStatusResponse } from '@/services/connections';
import { InstallationCard } from './InstallationCard';

interface GitHubTileProps {
  connections: GitHubConnectionItem[];
  isLoading: boolean;
  onInstall: () => void;
  onDisconnect: (installationId: number) => Promise<void>;
  isInstalling: boolean;
  /** Whether the current user is a platform admin. */
  isPlatformAdmin: boolean;
  /** GitHub App registration status (null while loading). */
  appStatus: AppStatusResponse | null;
  /** Whether the status fetch failed (e.g. AccessDenied / 503). */
  appStatusError?: boolean;
  /** Called when admin initiates registration with owner choice. */
  onRegister: (ownerType: 'user' | 'org', org?: string) => Promise<void>;
  /** Called to rotate the App private key. */
  onRotateKey: () => Promise<void>;
  /** Called to disconnect (unregister) the App entirely. */
  onDisconnectApp: () => Promise<void>;
  /** Issue #3071: Called to switch the active workspace (tenant). */
  onSwitchTenant?: (tenantId: string) => Promise<void>;
}

export function GitHubTile({
  connections,
  isLoading,
  onInstall,
  onDisconnect,
  isInstalling,
  isPlatformAdmin,
  appStatus,
  appStatusError = false,
  onRegister,
  onRotateKey,
  onDisconnectApp,
  onSwitchTenant,
}: GitHubTileProps) {
  // For non-platform-admins, appStatus is null (they can't call the status endpoint).
  // In that case, assume registered so the existing install UI is shown.
  // Only show "not registered" when we have an explicit false from the API.
  // IMPORTANT: If the status fetch *failed* (appStatusError=true), do NOT default
  // to unregistered for platform admins — that would show the "Set up GitHub App"
  // CTA which can overwrite a live App's credentials. Show an error state instead.
  const isRegistered = isPlatformAdmin
    ? (appStatusError ? undefined : (appStatus?.registered ?? false))
    : (appStatus?.registered ?? true);

  return (
    <section className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm dark:border-gray-700 dark:bg-gray-900">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          {/* GitHub icon */}
          <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-gray-900 text-white dark:bg-gray-700">
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 24 24"
              fill="currentColor"
              className="h-6 w-6"
              aria-hidden="true"
            >
              <path d="M12 0C5.374 0 0 5.373 0 12c0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23A11.509 11.509 0 0112 5.803c1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576C20.566 21.797 24 17.3 24 12c0-6.627-5.373-12-12-12z" />
            </svg>
          </span>
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">GitHub</h2>
            <p className="mt-0.5 text-sm text-gray-500 dark:text-gray-400">
              {isRegistered === undefined
                ? 'GitHub App status could not be determined.'
                : isRegistered
                  ? 'Install the ADP Agent app on your GitHub org to trigger agents from labels and @-mentions.'
                  : 'Register a GitHub App to enable agent triggers and integrations.'}
            </p>
          </div>
        </div>

        {/* Install button — only shown when app is confirmed registered */}
        {isRegistered === true && (
          <Button
            onClick={onInstall}
            disabled={isInstalling || isLoading}
            isLoading={isInstalling}
            variant={connections.length > 0 ? 'secondary' : 'primary'}
            size="sm"
          >
            {isInstalling
              ? 'Opening GitHub…'
              : connections.length > 0
                ? '+ Add another connection'
                : 'Install on GitHub'}
          </Button>
        )}
      </div>

      {/* Body — varies by registration state + role */}
      {isLoading && !appStatus ? (
        <div className="mt-4 animate-pulse space-y-3">
          <div className="h-16 rounded-lg bg-gray-100 dark:bg-gray-800" />
        </div>
      ) : isRegistered === undefined ? (
        // --- Status unavailable (e.g. AccessDenied / 503) ---
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 dark:border-red-700 dark:bg-red-900/20">
          <p className="text-sm font-medium text-red-800 dark:text-red-200">
            Unable to check GitHub App status
          </p>
          <p className="mt-1 text-sm text-red-700 dark:text-red-300">
            The status check failed (possibly an IAM permissions issue). A GitHub App
            may already be registered &mdash; do not re-register without confirming.
            Check the gateway logs or contact an administrator.
          </p>
        </div>
      ) : !isRegistered ? (
        // --- Unregistered state ---
        isPlatformAdmin ? (
          <RegistrationForm onRegister={onRegister} />
        ) : (
          <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-900/20">
            <p className="text-sm text-amber-800 dark:text-amber-200">
              GitHub isn&apos;t set up yet &mdash; ask a platform admin to configure the GitHub App.
            </p>
          </div>
        )
      ) : (
        // --- Registered state ---
        <>
          {/* Issue #2708: warn when the App is registered but GitHub sign-in
              isn't wired (broker OAuth secret missing/placeholder). */}
          {isPlatformAdmin && appStatus?.login_enabled === false && (
            <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-900/20">
              <p className="text-sm font-medium text-amber-800 dark:text-amber-200">
                GitHub sign-in not wired
              </p>
              <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
                The App is registered, but &ldquo;Sign in with GitHub&rdquo; won&apos;t work until the
                OAuth credentials are stored. Re-register the App, or see the deployment docs for
                wiring login manually.
              </p>
            </div>
          )}

          {/* App info (platform admin only) */}
          {isPlatformAdmin && appStatus && (
            <AppInfoPanel
              appStatus={appStatus}
              onRotateKey={onRotateKey}
              onDisconnectApp={onDisconnectApp}
            />
          )}

          {/* Installations list */}
          {isLoading ? (
            <div className="mt-4 animate-pulse space-y-3">
              <div className="h-16 rounded-lg bg-gray-100 dark:bg-gray-800" />
            </div>
          ) : connections.length > 0 ? (
            <ConnectionsList connections={connections} onDisconnect={onDisconnect} onSwitchTenant={onSwitchTenant} />
          ) : null}
        </>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// deriveGroupDisplayName — use the GitHub org login (account_login) from the
// first connection in the group rather than the internal tenant_name slug.
// Falls back to tenant_name (internal slug) only if no connections have an
// account_login (shouldn't happen in practice).
// ---------------------------------------------------------------------------

function deriveGroupDisplayName(conns: GitHubConnectionItem[]): string {
  // Prefer the GitHub org display name (account_login) from the first connection
  const firstLogin = conns.find((c) => c.account_login)?.account_login;
  if (firstLogin) return firstLogin;
  // Fallback to the API-provided tenant_name (internal slug)
  return conns[0]?.tenant_name ?? 'Unknown';
}

// ---------------------------------------------------------------------------
// ConnectionsList — groups connections by workspace when multi-workspace (Issue #3018)
// ---------------------------------------------------------------------------

function ConnectionsList({
  connections,
  onDisconnect,
  onSwitchTenant,
}: {
  connections: GitHubConnectionItem[];
  onDisconnect: (installationId: number) => Promise<void>;
  onSwitchTenant?: (tenantId: string) => Promise<void>;
}) {
  const [switchingTenantId, setSwitchingTenantId] = useState<string | null>(null);
  // Check if we have multi-workspace data (tenant_id present on any connection)
  const isMultiWorkspace = connections.some((c) => c.tenant_id != null);

  if (!isMultiWorkspace) {
    // Single workspace: flat list (legacy behavior)
    return (
      <div className="mt-4 space-y-3">
        {connections.map((conn) => (
          <InstallationCard
            key={conn.installation_id}
            connection={conn}
            onDisconnect={onDisconnect}
          />
        ))}
      </div>
    );
  }

  // Multi-workspace: group by tenant_id (internal key)
  const groups = new Map<string, GitHubConnectionItem[]>();
  for (const conn of connections) {
    const key = conn.tenant_id ?? '__unknown__';
    const list = groups.get(key) ?? [];
    list.push(conn);
    groups.set(key, list);
  }

  // Sort: active workspace first, then alphabetical by display name
  const sortedEntries = [...groups.entries()].sort(([, aConns], [, bConns]) => {
    const aActive = aConns[0]?.is_active_tenant ?? false;
    const bActive = bConns[0]?.is_active_tenant ?? false;
    if (aActive && !bActive) return -1;
    if (!aActive && bActive) return 1;
    const aName = deriveGroupDisplayName(aConns);
    const bName = deriveGroupDisplayName(bConns);
    return aName.localeCompare(bName);
  });

  // Find the active tenant name for the "Viewing" chip context
  const activeTenantName = sortedEntries.find(
    ([, conns]) => conns[0]?.is_active_tenant,
  )?.[1]?.[0]?.tenant_name ?? 'another workspace';

  const handleSwitch = async (tenantId: string) => {
    if (!onSwitchTenant) return;
    setSwitchingTenantId(tenantId);
    try {
      await onSwitchTenant(tenantId);
    } finally {
      setSwitchingTenantId(null);
    }
  };

  return (
    <div className="mt-4 space-y-4">
      {sortedEntries.map(([groupId, groupConns]) => {
        const isActive = groupConns[0]?.is_active_tenant ?? false;
        const displayName = deriveGroupDisplayName(groupConns);

        return (
          <div key={groupId}>
            <div className="mb-2 flex items-center gap-2">
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {displayName}
              </span>
              {isActive ? (
                <span className="inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-800 dark:bg-green-900/30 dark:text-green-300">
                  Active
                </span>
              ) : (
                <span className="inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300">
                  Viewing — you&apos;re working in {activeTenantName}
                </span>
              )}
              {!isActive && onSwitchTenant && (
                <button
                  type="button"
                  onClick={() => handleSwitch(groupId)}
                  disabled={switchingTenantId === groupId}
                  className="inline-flex items-center rounded-md bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-700 hover:bg-primary-100 disabled:opacity-50 dark:bg-primary-900/30 dark:text-primary-300 dark:hover:bg-primary-900/50"
                >
                  {switchingTenantId === groupId ? 'Switching…' : 'Switch to this workspace'}
                </button>
              )}
            </div>
            <div className="space-y-3">
              {groupConns.map((conn) => (
                <InstallationCard
                  key={conn.installation_id}
                  connection={conn}
                  onDisconnect={onDisconnect}
                  readOnly={!isActive}
                />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// RegistrationForm — owner_type choice + Create button (platform_admin only)
// ---------------------------------------------------------------------------

function RegistrationForm({
  onRegister,
}: {
  onRegister: (ownerType: 'user' | 'org', org?: string) => Promise<void>;
}) {
  const [ownerType, setOwnerType] = useState<'user' | 'org'>('user');
  const [orgName, setOrgName] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const canSubmit = ownerType === 'user' || orgName.trim().length > 0;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setIsSubmitting(true);
    try {
      await onRegister(ownerType, ownerType === 'org' ? orgName.trim() : undefined);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="mt-4 space-y-4">
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 dark:border-blue-700 dark:bg-blue-900/20">
        <p className="text-sm font-medium text-blue-800 dark:text-blue-200">
          Set up GitHub App
        </p>
        <p className="mt-1 text-sm text-blue-700 dark:text-blue-300">
          Create a GitHub App to enable agent triggers. This opens GitHub in a new tab
          where you&apos;ll approve the app creation.
        </p>
      </div>

      {/* Owner type radio */}
      <fieldset>
        <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">
          App owner
        </legend>
        <div className="mt-2 space-y-2">
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="owner_type"
              value="user"
              checked={ownerType === 'user'}
              onChange={() => setOwnerType('user')}
              className="h-4 w-4 text-primary-600 focus:ring-primary-500"
            />
            <span className="text-sm text-gray-700 dark:text-gray-300">
              Personal account (user-owned)
            </span>
          </label>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="owner_type"
              value="org"
              checked={ownerType === 'org'}
              onChange={() => setOwnerType('org')}
              className="h-4 w-4 text-primary-600 focus:ring-primary-500"
            />
            <span className="text-sm text-gray-700 dark:text-gray-300">
              Organization
            </span>
          </label>
        </div>
      </fieldset>

      {/* Org name input (shown when org selected) */}
      {ownerType === 'org' && (
        <div>
          <label
            htmlFor="github-org-name"
            className="block text-sm font-medium text-gray-700 dark:text-gray-300"
          >
            Organization name
          </label>
          <input
            id="github-org-name"
            type="text"
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
            placeholder="my-org"
            className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:border-primary-500 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100"
          />
        </div>
      )}

      {/* Submit */}
      <Button
        onClick={handleSubmit}
        disabled={!canSubmit || isSubmitting}
        isLoading={isSubmitting}
        variant="primary"
        size="md"
      >
        {isSubmitting ? 'Creating…' : 'Create on GitHub'}
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AppInfoPanel — shows app slug/ID + Rotate/Disconnect (platform_admin only)
// ---------------------------------------------------------------------------

function AppInfoPanel({
  appStatus,
  onRotateKey,
  onDisconnectApp,
}: {
  appStatus: AppStatusResponse;
  onRotateKey: () => Promise<void>;
  onDisconnectApp: () => Promise<void>;
}) {
  const [isRotating, setIsRotating] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);

  const handleRotate = async () => {
    setIsRotating(true);
    try {
      await onRotateKey();
    } finally {
      setIsRotating(false);
    }
  };

  const handleDisconnect = async () => {
    if (!confirmDisconnect) {
      setConfirmDisconnect(true);
      return;
    }
    setIsDisconnecting(true);
    try {
      await onDisconnectApp();
    } finally {
      setIsDisconnecting(false);
      setConfirmDisconnect(false);
    }
  };

  return (
    <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
            {appStatus.app_slug ?? 'GitHub App'}
          </p>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            ID: {appStatus.app_id ?? 'unknown'}
            {appStatus.owner_type && ` · ${appStatus.owner_type}`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            onClick={handleRotate}
            disabled={isRotating || isDisconnecting}
            isLoading={isRotating}
            variant="outline"
            size="sm"
          >
            Rotate key
          </Button>
          <Button
            onClick={handleDisconnect}
            disabled={isRotating || isDisconnecting}
            isLoading={isDisconnecting}
            variant={confirmDisconnect ? 'danger' : 'outline'}
            size="sm"
          >
            {confirmDisconnect ? 'Confirm disconnect?' : 'Disconnect app'}
          </Button>
        </div>
      </div>
    </div>
  );
}
