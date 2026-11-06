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
 * optional reason and reviewed revision hash. `actor_kind` is derived server-side from the authenticated
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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';
import { Permission } from '@/types';
import { approveGate, rejectGate, resumeNode, getGatePlanPreview, bindDraftProduct } from '@/services/orchestration';
import { DraftProductSelector } from './DraftProductSelector';
import { shortTime } from '@/utils/openGates';
import type { GraphNode, GateDecisionResult, ResumeResult } from '@/types/orchestration';
import { Alert, Button, Spinner } from '@/components/ui';

export interface GateControlsProps {
  node: GraphNode;
  /** The flow this node belongs to, so a decision can refresh its graph. */
  flowId: string;
}

/** Which control set a node's state earns, or null for "none of them". */
type ControlMode = 'gate' | 'resume';

function refusalMessage(error: unknown): string {
  const value = error as { message?: string; detail?: string | { message?: string } } | undefined;
  return (typeof value?.detail === 'string' ? value.detail : value?.detail?.message)
    || value?.message || 'The decision was refused. Reload the graph to see its current state.';
}

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
  const [dirtyProductRevision, setDirtyProductRevision] = useState<string | null>(null);

  const mode = controlModeFor(node);
  const preview = useQuery({
    queryKey: ['orchestration', 'gate-plan-preview', flowId, node.id],
    queryFn: () => getGatePlanPreview(flowId, node.id),
    enabled: Boolean(features.orchestration_engine && hasPermission(Permission.PLAN_APPROVE) && mode === 'gate'),
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const reviewed = preview.data;
  const reviewedRevision = reviewed ? `${reviewed.version}:${reviewed.plan_hash}` : null;
  const productDirty = dirtyProductRevision !== null && dirtyProductRevision === reviewedRevision;
  const nodeInPlan = reviewed?.plan_document.nodes.some(item =>
    item.address.split('/').slice(1).join('/') === `${node.epic_ref}/${node.wave_ref}/${node.node_ref}`);

  const productBinding = useMutation({
    mutationFn: ({ productId, projectId }: { productId: string; projectId?: string }) => {
      if (!reviewed || preview.isError) throw new Error('Reload the plan before saving its Product.');
      return bindDraftProduct(flowId, productId, reviewed, projectId);
    },
    onSuccess: async () => {
      setDirtyProductRevision(null);
      setFeedback('Product saved. Review the updated plan before approving execution.');
      mutation.reset();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['orchestration', 'gate-plan-preview', flowId, node.id] }),
        queryClient.invalidateQueries({ queryKey: ['orchestration', 'flow-graph', flowId] }),
        queryClient.invalidateQueries({ queryKey: ['orchestration', 'flows'] }),
      ]);
    },
  });

  // The two result shapes differ (a resume always produces a decision id; a gate
  // answer may not), so the union is declared rather than inferred from whichever
  // branch the compiler reaches first.
  const mutation = useMutation<GateDecisionResult | ResumeResult, Error, 'approve' | 'reject' | 'resume'>({
    mutationFn: (action) => {
      const trimmed = reason.trim() || undefined;
      if (action !== 'resume' && (!reviewed || !nodeInPlan || preview.isError)) {
        throw new Error('Load and review the current plan before deciding.');
      }
      if (action === 'approve' && reviewed?.execution?.ready === false) throw new Error('The next step is not executable. Resolve the listed configuration problems first.');
      if (action === 'approve') return approveGate(node.id, trimmed, reviewed!.plan_hash, reviewed!.execution);
      if (action === 'reject') return rejectGate(node.id, trimmed, reviewed!.plan_hash);
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
          // Gates chain: the engine may present the next one within a minute, and it
          // looks like this one. Say plainly that this decision is done and closed.
          ? `Approval recorded at ${shortTime(new Date().toISOString())}. This gate is closed. If the flow asks for another decision, that is the next gate in the chain, not this one.`
          : result.state === 'ready' && node.kind === 'gate'
            ? 'Review will reopen on the next engine check. This does not approve the gate.'
            : 'Retry requested. The engine will check this work again.');
      queryClient.invalidateQueries({ queryKey: ['orchestration', 'flow-graph', flowId] });
      queryClient.invalidateQueries({ queryKey: ['orchestration', 'flows'] });
      queryClient.invalidateQueries({ queryKey: ['orchestration', 'gate-plan-preview', flowId, node.id] });
    },
  });

  // Fail-closed flag first: an environment not running the engine shows nothing.
  if (!features.orchestration_engine) return null;
  // Then authority. Hidden, not disabled — see the header.
  if (!hasPermission(Permission.PLAN_APPROVE)) return null;
  if (mode === null) return feedback ? <p role="status" className="mt-2 text-sm">{feedback}</p> : null;

  const busy = mutation.isPending || productBinding.isPending;
  const decisionDisabled = busy || productDirty || preview.isFetching || preview.isError || !reviewed || !nodeInPlan;

  return (
    <div className="mt-2 space-y-2" data-testid={`gate-controls-${node.node_ref}`}>
      {mode === 'gate' && <div className="space-y-2 rounded border p-3 text-sm">
        {preview.isPending && <p>Loading the plan for review…</p>}
        {reviewed && <>
          <p className="font-medium">Review plan version {reviewed.version}: {reviewed.plan_document.title}</p>
          {reviewed.execution?.product?.can_bind_draft && <DraftProductSelector
            key={reviewedRevision} flowId={flowId} productId={reviewed.execution.product.product_id} projectId={reviewed.execution.product.project_id}
            disabled={busy || preview.isFetching || preview.isError} onChange={dirty => setDirtyProductRevision(dirty ? reviewedRevision : null)}
            onSave={(productId, projectId) => productBinding.mutate({ productId, projectId })} />}
          {productBinding.isError && <Alert variant="error" title="Product was not saved">{refusalMessage(productBinding.error)}</Alert>}
          <p>{reviewed.plan_document.nodes.length} planned steps. This decision applies to the reviewed plan version.</p>
          <p>{reviewed.plan_document.proposed_execution_policy ? 'Proposed execution authority — not yet granted'
            : reviewed.plan_document.execution_policy ? 'Recorded execution-policy bounds' : 'This plan has no execution-policy bounds.'}</p>
          {(reviewed.plan_document.proposed_execution_policy || reviewed.plan_document.execution_policy) &&
            <details><summary>Review execution permissions and limits</summary><p>Spend limits apply only when budget enforcement is enabled.</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(
              reviewed.plan_document.proposed_execution_policy || reviewed.plan_document.execution_policy, null, 2)}</pre></details>}
          {reviewed.execution?.evidence && <details open><summary>Review human evaluation evidence</summary>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(reviewed.execution.evidence, null, 2)}</pre>
          </details>}
          {reviewed.execution?.required && <div className="space-y-2">
            <p className="font-medium">Approving starts the following evaluation</p>
            {reviewed.execution.runs.map(run => <div key={run.node_id}>
              <p>{run.title}: {run.workflow}</p>
              <p>Account {run.target.account_id}, {run.target.region}, environment {run.target.resource_id}.</p>
              <p>Evidence: {run.criteria.join(', ')}. Final acceptance remains human.</p>
            </div>)}
            {reviewed.execution.window_request && <div><p>Approval also renews the expired execution window until {typeof reviewed.execution.window_request.expires_at === 'string' ? new Date(reviewed.execution.window_request.expires_at).toLocaleString() : 'the requested deadline'}. {typeof reviewed.execution.window_request.max_wall_clock_seconds === 'number' && <>Maximum elapsed time: {Math.round(reviewed.execution.window_request.max_wall_clock_seconds / 3600 * 10) / 10} hours.</>}</p><details><summary className="cursor-pointer py-1">Exact window renewal request</summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(reviewed.execution.window_request, null, 2)}</pre></details></div>}
          </div>}
          {reviewed.execution?.problems.map(problem => <Alert key={problem} variant="error" title="Next step needs configuration">{problem}</Alert>)}
          <details><summary>Full plan and dependencies</summary>
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(reviewed.plan_document, null, 2)}</pre>
          </details>
          <details><summary className="cursor-pointer py-1">Technical details and exact revision</summary><p className="break-all text-xs">Decision applies to revision {reviewed.plan_hash}.</p></details>
          {!nodeInPlan && <p>This gate is absent from the current plan. Reload the graph.</p>}
        </>}
        {preview.isError && <Alert variant="error" title="Plan preview unavailable">{refusalMessage(preview.error)}</Alert>}
        <Button size="sm" variant="secondary" disabled={busy || preview.isFetching}
          onClick={() => { mutation.reset(); productBinding.reset(); void preview.refetch(); }}>Reload plan preview</Button>
      </div>}
      {feedback && <p role="status" className="text-sm">{feedback}</p>}
      {mode === 'gate' && node.updated_at && (
        <p className="text-xs font-medium text-amber-900 dark:text-amber-200" data-testid="gate-waiting-since" title={node.updated_at}>
          Waiting for your decision since {shortTime(node.updated_at)}.
        </p>
      )}
      {mode === 'gate' && reviewed?.execution?.ready === false && <p role="status" className="text-sm text-amber-800 dark:text-amber-200">Approval is unavailable until the configuration problems above are resolved. Reload the preview after updating the configuration.</p>}
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
              disabled={decisionDisabled || reviewed?.execution?.ready === false}
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
              disabled={decisionDisabled || !reason.trim()}
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
          {refusalMessage(mutation.error)}
        </Alert>
      )}
    </div>
  );
}

export default GateControls;
