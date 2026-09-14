/**
 * The controls that turn a gate decision into an attributed act (issue #4213).
 *
 * Approve / Request changes on a gate node, and Resume on a stalled or halted
 * one. Three things about this component are load-bearing rather than stylistic:
 *
 * **1. Hidden without the permission, not disabled.** A disabled Approve button
 * advertises an authority the caller does not have and invites them to hunt for
 * the reason. Absence is the honest rendering. The button is a convenience over
 * the endpoint, never the thing that enforces anything: the backend re-checks
 * `PLAN_APPROVE` on every call, so hiding it is a UX decision and the 403 is the
 * control. Rendering it for an unauthorized caller would be a lie, not a hole.
 *
 * **2. Gated on the fail-closed `orchestration_engine` flag.** An environment
 * where the engine is off must not show promotion controls for a graph it is not
 * running. `useFeatures` is fail-open in general, but this flag defaults to
 * `false` while flags are pending or the fetch failed, so a slow `/features` call
 * cannot flash these buttons into existence.
 *
 * **3. Nothing here asserts who the actor is.** The request body carries only an
 * optional reason. `actor_kind` is derived server-side from the authenticated
 * session and the request model forbids extra fields — so there is deliberately
 * no code path in this component, and none available to it, that could claim
 * human attribution for a service caller.
 *
 * Which control renders is driven by the node's own state: a gate `awaiting_gate`
 * gets approve/reject, a `failed` or `halted` node gets resume. A node in any
 * other state gets nothing, because every other edge belongs to the engine and a
 * button that requests one would be refused with a 409 anyway.
 */

import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';
import { Permission } from '@/types';
import { approveGate, rejectGate, resumeNode } from '@/services/orchestration';
import type { GraphNode, GateDecisionResult, ResumeResult } from '@/types/orchestration';
import { Alert, Button, Spinner } from '@/components/ui';

export interface GateControlsProps {
  node: GraphNode;
  /** The flow this node belongs to, so a decision can refresh its graph. */
  flowId: string;
}

/** Which control set a node's state earns, or null for "none of them". */
type ControlMode = 'gate' | 'resume';

function controlModeFor(node: GraphNode): ControlMode | null {
  if ((node.kind === 'gate' || node.kind === 'eval') && node.state === 'awaiting_gate') return 'gate';
  // A stall lands the node in `failed`, so `failed` covers both "stuck" and
  // "broke" — both are resumable, and the engine's transition table is what
  // decides legality, not this component.
  if (node.state === 'failed' || node.state === 'halted' || node.state === 'rejected_at_gate' || node.state === 'awaiting_merge') return 'resume';
  return null;
}

export function GateControls({ node, flowId }: GateControlsProps) {
  const { hasPermission } = usePermissions();
  const features = useFeatures();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState('');
  const [feedback, setFeedback] = useState('');

  const mode = controlModeFor(node);

  // The two result shapes differ (a resume always produces a decision id; a gate
  // answer may not), so the union is declared rather than inferred from whichever
  // branch the compiler reaches first.
  const mutation = useMutation<GateDecisionResult | ResumeResult, Error, 'approve' | 'reject' | 'resume'>({
    mutationFn: (action) => {
      const trimmed = reason.trim() || undefined;
      if (action === 'approve') return approveGate(node.id, trimmed);
      if (action === 'reject') return rejectGate(node.id, trimmed);
      return resumeNode(node.id, trimmed);
    },
    onSuccess: (result, action) => {
      // The decision changed promotion state, so the graph this chip sits in is
      // now stale. Invalidating is what makes the new state visible without
      // waiting out the 30s poll.
      setReason('');
      setFeedback(action === 'reject'
        ? 'Changes requested. Work behind this gate is paused. No revision agent has been started.'
        : action === 'approve'
          ? 'Approval recorded. Eligible work can start on the next engine check.'
          : result.state === 'ready' && node.kind === 'gate'
            ? 'Review will reopen on the next engine check. This does not approve the gate.'
            : 'Retry requested. The engine will check this work again.');
      queryClient.invalidateQueries({ queryKey: ['orchestration', 'flow-graph', flowId] });
      queryClient.invalidateQueries({ queryKey: ['orchestration', 'flows'] });
    },
  });

  // Fail-closed flag first: an environment not running the engine shows nothing.
  if (!features.orchestration_engine) return null;
  // Then authority. Hidden, not disabled — see the header.
  if (!hasPermission(Permission.PLAN_APPROVE)) return null;
  if (mode === null) return feedback ? <p role="status" className="mt-2 text-sm">{feedback}</p> : null;

  const busy = mutation.isPending;

  return (
    <div className="mt-2 space-y-2" data-testid={`gate-controls-${node.node_ref}`}>
      {feedback && <p role="status" className="text-sm">{feedback}</p>}
      {mode === 'gate' && (
        <p className="text-xs text-gray-600 dark:text-gray-400">
          Add a note to request changes. This pauses work behind the gate; it does not start a revision agent.
        </p>
      )}
      {node.state === 'rejected_at_gate' && (
        <p className="text-xs text-gray-600 dark:text-gray-400">
          {node.kind === 'gate'
            ? 'Reopen review after updating the plan, or to reconsider this decision. Reopening does not approve or start the work.'
            : 'Retry this evaluation after addressing the feedback. Its new result will need review.'}
        </p>
      )}
      <label className="block">
        <span className="sr-only">Reason for this decision</span>
        <input
          type="text"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          disabled={busy}
          placeholder={mode === 'gate' ? 'Decision note (required to request changes)' : 'Reason (optional)'}
          data-testid="gate-controls-reason"
          className="w-full rounded border border-gray-300 px-2 py-1 text-xs dark:border-gray-600 dark:bg-gray-800"
        />
      </label>

      <div className="flex flex-wrap items-center gap-2">
        {mode === 'gate' ? (
          <>
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              onClick={() => mutation.mutate('approve')}
              data-testid="gate-approve"
            >
              {node.kind === 'eval' ? 'Accept evaluation' : 'Approve'}
            </Button>
            {/* "Request changes", not "Reject": the backend target is
                `rejected_at_gate`, which leaves the node live and re-openable.
                Labelling it "Reject" implies the work is dead. */}
            <Button
              size="sm"
              variant="secondary"
              disabled={busy || !reason.trim()}
              onClick={() => mutation.mutate('reject')}
              data-testid="gate-reject"
            >
              Request changes
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="secondary"
            disabled={busy}
            onClick={() => mutation.mutate('resume')}
            data-testid="node-resume"
          >
            {node.state === 'rejected_at_gate'
              ? node.kind === 'gate' ? 'Reopen review' : 'Retry evaluation'
              : node.state === 'halted' ? 'Override halt and resume' : node.state === 'awaiting_merge' ? 'Retry story' : 'Resume'}
          </Button>
        )}

        {busy && <Spinner size="sm" />}
      </div>

      {mutation.isError && (
        <Alert variant="error" title="That decision was not recorded">
          {mutation.error?.message ||
            'The decision was refused. Reload the graph to see its current state.'}
        </Alert>
      )}
    </div>
  );
}

export default GateControls;
