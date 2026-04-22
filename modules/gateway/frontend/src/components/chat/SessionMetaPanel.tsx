/**
 * SessionMetaPanel — displays AG-UI STATE_DELTA metadata.
 *
 * Issue #97 Phase 2: Shows session metadata (token counts, turn count)
 * updated in real-time from STATE_DELTA events. Renders as a compact
 * info bar above the message input.
 */

import type { SessionMeta } from '@/types/ag-ui-events';

interface SessionMetaPanelProps {
  meta?: SessionMeta;
}

export function SessionMetaPanel({ meta }: SessionMetaPanelProps) {
  if (!meta || (!meta.tokens && !meta.turnCount)) return null;

  return (
    <div
      className="flex items-center gap-4 px-4 py-1.5 text-xs text-gray-500 dark:text-gray-400 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/30"
      role="status"
      aria-label="Session metadata"
    >
      {meta.turnCount != null && (
        <span>
          Turns: <strong className="text-gray-700 dark:text-gray-300">{meta.turnCount}</strong>
        </span>
      )}
      {meta.tokens && (
        <>
          <span>
            In: <strong className="text-gray-700 dark:text-gray-300">{formatTokens(meta.tokens.input)}</strong>
          </span>
          <span>
            Out: <strong className="text-gray-700 dark:text-gray-300">{formatTokens(meta.tokens.output)}</strong>
          </span>
        </>
      )}
    </div>
  );
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}
