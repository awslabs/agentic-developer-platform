/**
 * InstallationCard — displays a single GitHub App installation.
 *
 * Issue #465: used inside GitHubTile to list connected GitHub orgs.
 */

import { useState } from 'react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { type GitHubConnectionItem } from '@/services/connections';

interface InstallationCardProps {
  connection: GitHubConnectionItem;
  onDisconnect: (installationId: number) => Promise<void>;
  /** Issue #3018: Hide Disconnect button for non-active tenant connections. */
  readOnly?: boolean;
}

export function InstallationCard({ connection, onDisconnect, readOnly = false }: InstallationCardProps) {
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

  return (
    <div className="flex items-center justify-between rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-900 dark:text-gray-100">
            {connection.account_login}
          </span>
          <Badge variant="success">Installed ✓</Badge>
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
        {!readOnly && (
          <Button
            variant="danger"
            size="sm"
            onClick={handleDisconnect}
            disabled={isDisconnecting}
          >
            {isDisconnecting ? 'Disconnecting…' : confirmDelete ? 'Confirm?' : 'Disconnect'}
          </Button>
        )}
      </div>
    </div>
  );
}
