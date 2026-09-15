/**
 * InvocationChain — tree view of an agent invocation chain.
 *
 * Issue #1461: Phase 6 — Surface agent-to-agent lineage on the screen.
 *
 * Renders a correlation chain as an indented tree (by parent_invocation_id).
 * Falls back to flat date-ordered list when parent edges are null (pre-feature rows).
 * Shows root human indicator and whether the chain is human-rooted.
 */

import { useQuery } from '@tanstack/react-query';
import { Alert, Button } from '@/components/ui';
import { TableSkeleton } from '@/components/LoadingScreen';
import { getMyInvocationChain, getAdminInvocationChain } from '@/services/activity';
import { formatRelativeTime, formatDateTime } from '@/utils/format';
// Issue #4207: replaces two copy-pasted inline 4/2-decimal expressions.
import { formatAmount } from '@/utils/cost';
import type {
  InvocationChainItem,
  InvocationChainResponse,
  InvocationStatus,
} from '@/types/activity';

// ---------------------------------------------------------------------------
// Status glyph (compact version for chain view)
// ---------------------------------------------------------------------------

// Issue #4187: typed as `Record<InvocationStatus, …>` rather than
// `Record<string, …>`. It was the loose form, and had already silently fallen
// behind: `blocked` and `skipped` were missing, so the `?? STATUS_GLYPHS.no_op`
// fallback below rendered them as "no-op". A budget-stopped run would have shown
// the same way — a stopped run reported as "nothing ran". Typing it means the
// next status added to the union fails the build here instead.
const STATUS_GLYPHS: Record<InvocationStatus, { glyph: string; colorClass: string }> = {
  webhook_received: { glyph: '∘', colorClass: 'text-gray-500' },
  in_progress: { glyph: '●', colorClass: 'text-blue-600 dark:text-blue-400' },
  complete: { glyph: '✓', colorClass: 'text-green-600 dark:text-green-400' },
  failed: { glyph: '✗', colorClass: 'text-red-600 dark:text-red-400' },
  rejected: { glyph: '✗', colorClass: 'text-orange-600 dark:text-orange-400' },
  rate_limited: { glyph: '✗', colorClass: 'text-yellow-600 dark:text-yellow-400' },
  no_op: { glyph: '✗', colorClass: 'text-gray-500' },
  blocked: { glyph: '✗', colorClass: 'text-gray-500' },
  skipped: { glyph: '✗', colorClass: 'text-gray-500' },
  budget_stopped: { glyph: '⊘', colorClass: 'text-amber-600 dark:text-amber-400' },
  // Issue #3964. The typing did its job: adding `aborted` to the union failed the
  // build here, which is the only reason this map is not now rendering a
  // deliberately stopped run as "✗ No-op" via the fallback below. Glyph and colour
  // match `utils/status.ts` so the chain view and the board agree.
  aborted: { glyph: '■', colorClass: 'text-amber-600 dark:text-amber-400' },
};

// ---------------------------------------------------------------------------
// Tree node component
// ---------------------------------------------------------------------------

interface ChainNodeProps {
  node: InvocationChainItem;
  depth: number;
  highlightId?: string;
  onNodeClick?: (invocationId: string) => void;
}

function ChainNode({ node, depth, highlightId, onNodeClick }: ChainNodeProps) {
  // The chain API types `status` as a plain string, so the cast is what lets the
  // map stay exhaustively typed above. The `??` fallback still covers null and
  // any value the backend adds before the frontend knows about it.
  const statusConfig = STATUS_GLYPHS[node.status as InvocationStatus] ?? STATUS_GLYPHS.no_op;
  const isHighlighted = node.invocation_id === highlightId;

  return (
    <div data-testid={`chain-node-${node.invocation_id}`}>
      <button
        type="button"
        onClick={() => onNodeClick?.(node.invocation_id)}
        className={`w-full text-left flex items-center gap-2 py-2 px-3 rounded-md cursor-pointer transition-colors ${
          isHighlighted
            ? 'bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-700'
            : 'hover:bg-gray-50 dark:hover:bg-gray-700/50'
        }`}
        style={{ marginLeft: `${depth * 24}px` }}
        title={`View detail for ${node.invocation_id}`}
      >
        {/* Indent connector */}
        {depth > 0 && (
          <span className="text-gray-300 dark:text-gray-600 text-sm" aria-hidden="true">
            └─
          </span>
        )}

        {/* Status glyph */}
        <span className={`${statusConfig.colorClass} text-sm font-medium`} aria-hidden="true">
          {statusConfig.glyph}
        </span>

        {/* Topic / invocation info */}
        <span className="text-sm text-gray-900 dark:text-white truncate flex-1">
          {node.topic || <span className="italic text-gray-400">untitled</span>}
        </span>

        {/* Per-node cost badge — Issue #1653 */}
        {node.total_cost_usd != null && (
          <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded font-mono">
            {formatAmount(node.total_cost_usd)}
          </span>
        )}

        {/* Persona badge */}
        {node.persona && (
          <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-2 py-0.5 rounded">
            {node.persona}
          </span>
        )}

        {/* Date */}
        <span
          className="text-xs text-gray-400 dark:text-gray-500 whitespace-nowrap"
          title={formatDateTime(node.invoked_at)}
        >
          {formatRelativeTime(node.invoked_at)}
        </span>
      </button>

      {/* Render children recursively */}
      {node.children.map((child) => (
        <ChainNode
          key={child.invocation_id}
          node={child}
          depth={depth + 1}
          highlightId={highlightId}
          onNodeClick={onNodeClick}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main chain view component
// ---------------------------------------------------------------------------

export interface InvocationChainProps {
  /** The correlation_id to display the chain for. */
  correlationId: string;
  /** Whether to use admin endpoint (tenant-scoped). */
  isAdmin?: boolean;
  /** Optional tenant_id for admin view. */
  tenantId?: string;
  /** Invocation ID to highlight in the tree. */
  highlightInvocationId?: string;
  /** Callback when user closes the chain view. */
  onClose?: () => void;
  /** Callback when a chain node is clicked (for recursive detail navigation). Issue #1653. */
  onNodeClick?: (invocationId: string) => void;
  /**
   * Issue #3708: When true, include no_op and webhook_received events in the
   * chain view (maps to the "Show all events" toggle). Default false = only
   * real runs are shown.
   */
  includeNonTriggering?: boolean;
}

export default function InvocationChain({
  correlationId,
  isAdmin = false,
  tenantId,
  highlightInvocationId,
  onClose,
  onNodeClick,
  includeNonTriggering = false,
}: InvocationChainProps) {
  const {
    data,
    isLoading,
    error,
    refetch,
  } = useQuery<InvocationChainResponse>({
    queryKey: ['invocation-chain', correlationId, isAdmin, tenantId, includeNonTriggering],
    queryFn: () =>
      isAdmin
        ? getAdminInvocationChain(correlationId, tenantId, includeNonTriggering)
        : getMyInvocationChain(correlationId, includeNonTriggering),
    enabled: !!correlationId,
  });

  if (isLoading) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-4">
        <TableSkeleton rows={4} />
      </div>
    );
  }

  if (error) {
    return (
      <Alert variant="error" title="Failed to load chain">
        <div className="flex items-center justify-between">
          <span>
            {error instanceof Error ? error.message : 'An unexpected error occurred'}
          </span>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </div>
      </Alert>
    );
  }

  if (!data || data.items.length === 0) {
    return (
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 text-center">
        <p className="text-gray-500 dark:text-gray-400">No chain data available</p>
        {onClose && (
          <Button variant="outline" size="sm" onClick={onClose} className="mt-3">
            Close
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg shadow" data-testid="invocation-chain">
      {/* Chain header */}
      <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-white">
            Invocation Chain
          </h3>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            {data.is_human_rooted ? (
              <span>
                Started by{' '}
                <span className="font-medium text-gray-700 dark:text-gray-300">
                  {data.root_human_id ? 'you' : 'a user'}
                </span>
              </span>
            ) : (
              <span className="text-amber-600 dark:text-amber-400">Agent-initiated (no human root)</span>
            )}
            {' · '}
            <span>{data.total_count} invocation{data.total_count !== 1 ? 's' : ''}</span>
            {data.depth_capped && (
              <span className="text-amber-500 ml-1">(truncated at depth cap)</span>
            )}
          </p>
        </div>
        {onClose && (
          <Button variant="outline" size="sm" onClick={onClose}>
            Close
          </Button>
        )}
      </div>

      {/* Chain tree */}
      <div className="p-3">
        {data.items.map((node) => (
          <ChainNode
            key={node.invocation_id}
            node={node}
            depth={0}
            highlightId={highlightInvocationId}
            onNodeClick={onNodeClick}
          />
        ))}
      </div>

      {/* Chain cost totals — Issue #1653 */}
      {data.chain_total_cost_usd != null && (
        <div className="px-4 py-2 border-t border-gray-200 dark:border-gray-700 flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
          <span>
            Chain total: <span className="font-medium font-mono">
              {formatAmount(data.chain_total_cost_usd)}
            </span>
          </span>
          {data.chain_total_call_count != null && (
            <span>{data.chain_total_call_count} call{data.chain_total_call_count !== 1 ? 's' : ''}</span>
          )}
          {data.chain_total_tokens != null && (
            <span>{data.chain_total_tokens.toLocaleString()} tokens</span>
          )}
        </div>
      )}
    </div>
  );
}
