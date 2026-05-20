/**
 * GitHubTile — GitHub App connection section on the Connections page.
 *
 * Issue #465: Shows current installations + install button.
 */

import { Button } from '@/components/ui/Button';
import { type GitHubConnectionItem } from '@/services/connections';
import { InstallationCard } from './InstallationCard';

interface GitHubTileProps {
  connections: GitHubConnectionItem[];
  isLoading: boolean;
  onInstall: () => void;
  onDisconnect: (installationId: number) => Promise<void>;
  isInstalling: boolean;
}

export function GitHubTile({
  connections,
  isLoading,
  onInstall,
  onDisconnect,
  isInstalling,
}: GitHubTileProps) {
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
              Install the ADP Agent app on your GitHub org to trigger agents from labels and
              @-mentions.
            </p>
          </div>
        </div>

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
      </div>

      {/* Installations list */}
      {isLoading ? (
        <div className="mt-4 animate-pulse space-y-3">
          <div className="h-16 rounded-lg bg-gray-100 dark:bg-gray-800" />
        </div>
      ) : connections.length > 0 ? (
        <div className="mt-4 space-y-3">
          {connections.map((conn) => (
            <InstallationCard
              key={conn.installation_id}
              connection={conn}
              onDisconnect={onDisconnect}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}
