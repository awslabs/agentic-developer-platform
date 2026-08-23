/**
 * InstallationCard — displays a single GitHub App installation.
 *
 * Issue #465: used inside GitHubTile to list connected GitHub orgs.
 */

import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { type GitHubConnectionItem } from '@/services/connections';
import { VerificationRow, hasUnhealthyCheck, type CheckState } from './VerificationRow';

interface InstallationCardProps {
  connection: GitHubConnectionItem;
  onDisconnect: (installationId: number) => Promise<void>;
  /** Issue #3018: Hide Disconnect button for non-active tenant connections. */
  readOnly?: boolean;
}

export function InstallationCard({ connection, onDisconnect, readOnly = false }: InstallationCardProps) {
  // Issue #3073: Disconnect is visible when the server says the caller can manage
  // AND the card is not readOnly (non-active tenant connections stay hidden).
  const showDisconnect = !readOnly && connection.can_manage;
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const handleDisconnect = async () => {
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    setIsDisconnecting(true);
    try {
      await onDisconnect(connection.installation_id);
    } finally {
      setIsDisconnecting(false);
      setConfirmDelete(false);
    }
  };

  const repos = connection.repositories ?? [];
  const repoLabel =
    connection.repository_selection === 'all'
      ? 'All repositories'
      : `${connection.repository_count} repo${connection.repository_count !== 1 ? 's' : ''}`;

  // -------------------------------------------------------------------------
  // Issue #4016: the badge used to be a hardcoded green "Installed ✓" — it
  // reported that a row existed, not that the installation worked. It now
  // reflects the verification checks.
  // -------------------------------------------------------------------------
  const v = connection.verification;
  const checks: CheckState[] = [
    v?.record_present,
    v?.tenant_secret_seeded,
    v?.identity_index_row,
    v?.reverse_identity_row,
  ];
  // No verification block at all (older API, or checks unavailable) → keep the
  // previous badge rather than inventing a scary state.
  const anyBroken = checks.some((c) => c === false);
  const anyUnverified = v != null && hasUnhealthyCheck(checks);

  const badge = anyBroken ? (
    <Badge variant="danger">Needs attention</Badge>
  ) : anyUnverified ? (
    <Badge variant="warning">Partly verified</Badge>
  ) : (
    <Badge variant="success">Installed ✓</Badge>
  );

  return (
    <div className="flex items-start justify-between rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-900 dark:text-gray-100">
            {connection.account_login}
          </span>
          {badge}
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {repoLabel} · Installation #{connection.installation_id}
        </span>
        {repos.length > 0 && (
          <ul className="mt-1 flex flex-wrap gap-1">
            {repos.map((name) => (
              <li
                key={name}
                className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300"
              >
                {name}
              </li>
            ))}
          </ul>
        )}

        {/* Issue #4016: only rendered when something is not verified-green, so a
            healthy installation looks exactly as it did before. */}
        {anyUnverified && (
          <ul className="mt-2 space-y-1.5">
            <VerificationRow
              label="Recorded in this workspace"
              state={v?.record_present}
              brokenDetail="GitHub reports this installation, but the platform has no record of it — it cannot be managed from here. An operator needs to reconcile it."
            />
            <VerificationRow
              label="Agent credentials"
              state={v?.tenant_secret_seeded}
              brokenDetail="No credentials are stored for this workspace, so any agent triggered here will fail on startup."
            />
            <VerificationRow
              label="Webhook routing"
              state={v?.identity_index_row}
              brokenDetail="Events from GitHub for this installation cannot be matched to this workspace, so labels and @-mentions will be ignored."
            />
            <VerificationRow
              label="Agent-to-agent dispatch"
              state={v?.reverse_identity_row}
              brokenDetail="Agents cannot summon other agents for this workspace. This repairs itself the first time a webhook arrives."
            />
          </ul>
        )}
      </div>

      <div className="flex items-center gap-2">
        <a
          href={connection.manage_url || connection.configure_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-primary-600 hover:underline dark:text-primary-400"
        >
          Manage repositories ↗
        </a>
        {showDisconnect ? (
          <Button
            variant="danger"
            size="sm"
            onClick={handleDisconnect}
            disabled={isDisconnecting}
          >
            {isDisconnecting ? 'Disconnecting…' : confirmDelete ? 'Confirm?' : 'Disconnect'}
          </Button>
        ) : readOnly ? (
          <span
            className="text-xs text-gray-400 dark:text-gray-500"
            title="Switch to this workspace to manage connections"
          >
            Disconnect unavailable
          </span>
        ) : null}
      </div>
    </div>
  );
}
