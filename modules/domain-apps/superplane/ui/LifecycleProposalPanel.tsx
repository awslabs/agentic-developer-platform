import { useCallback, useEffect, useState } from 'react';

import { Alert, Button } from '@/components/ui';

import { ApprovalPanel } from './ApprovalPanel';
import {
  ScopeGuard, continueLifecycleProposal, getApproval, getOperation, isSuperseded,
  listLifecycleProposals, previewLifecycleProposal, recoverOperation, requestApproval,
  type LifecycleProposal,
} from './client';
import type { OperationApproval, Unavailable } from './contract';
import {
  claimPreviewIdentity, isTerminal, markSubmissionStage, readReceipt, receiptsForIntentPrefix, recordObservationExclusive,
  type ExclusiveSection, type ReceiptScope, type ReceiptStore, type StoredReceipt,
} from './operations';
import { useFreshnessClock } from './readiness';

interface Props {
  workspaceId: string;
  scope: ReceiptScope;
  store: ReceiptStore;
  mayManage: boolean;
  onProgress?: () => void;
  mintKey?: () => string;
  section?: ExclusiveSection;
}

export function LifecycleProposalPanel(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [proposals, setProposals] = useState<LifecycleProposal[]>([]);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const refresh = useCallback(async () => {
    const result = await listLifecycleProposals(guard, props.workspaceId);
    if (isSuperseded(result)) return;
    if (result.ok) { setProposals(result.value); setProblem(null); }
    else if ('unavailable' in result) setProblem(result.unavailable);
  }, [guard, props.workspaceId]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); guard.supersede(); };
  }, [guard, refresh]);
  return <section className="mt-6 space-y-4" aria-label="Workspace lifecycle plans">
    <h3 className="font-semibold">Workspace lifecycle plans</h3>
    <p>A completed operation can prepare the next plan. The workspace remains in provisioning until its readiness checks complete.</p>
    <Button variant="secondary" onClick={() => void refresh()}>Refresh lifecycle plans</Button>
    {problem && <Alert variant="warning" title="Lifecycle plans unavailable">{problem.detail}</Alert>}
    {!problem && proposals.length === 0 && <p>No next plan is available yet.</p>}
    {proposals.map((proposal) => <ProposalReview key={proposal.artifactId} {...props} proposal={proposal} />)}
    <PreviousContinuations {...props} currentArtifacts={proposals.map((proposal) => proposal.artifactId)} />
  </section>;
}

function PreviousContinuations({ currentArtifacts, ...props }: Props & { currentArtifacts: string[] }) {
  const [entries, setEntries] = useState<ReturnType<typeof receiptsForIntentPrefix>>([]);
  const [unreadable, setUnreadable] = useState(false);
  useEffect(() => {
    const refresh = () => {
      try {
        setEntries(receiptsForIntentPrefix(props.store, props.scope, `continue:${props.workspaceId}:`));
        setUnreadable(false);
      } catch { setUnreadable(true); }
    };
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, [props.store, props.scope, props.workspaceId]);
  const visibleIntents = new Set(currentArtifacts.map((artifact) => `continue:${props.workspaceId}:${artifact}`));
  return <>
    {unreadable && <Alert variant="warning" title="Saved continuation receipts unavailable">Browser storage could not be read. Recover the existing request reference before starting other work.</Alert>}
    {entries.filter(({ intent, receipt }) => !visibleIntents.has(intent) && receipt.submissionStage === 'submitted')
      .map(({ intent, receipt }) => <PreviousContinuation key={receipt.idempotencyKey} {...props} intent={intent} initial={receipt} />)}
  </>;
}

function PreviousContinuation({ initial, intent, store, scope, workspaceId, section, onProgress }: Props & { initial: StoredReceipt; intent: string }) {
  const [guard] = useState(() => new ScopeGuard());
  const [receipt, setReceipt] = useState(initial);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  useEffect(() => () => guard.supersede(), [guard]);
  const refresh = useCallback(async () => {
    const generation = guard.current();
    const result = receipt.operationId ? await getOperation(guard, receipt.operationId) : await recoverOperation(guard, receipt.idempotencyKey);
    if (isSuperseded(result)) return;
    if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
    if (result.value.idempotencyKey !== receipt.idempotencyKey || result.value.workspaceId !== workspaceId) {
      setProblem({ reason: 'unknown', detail: 'The operation response did not match this workspace and request.' }); return;
    }
    try {
      const saved = await recordObservationExclusive(store, scope, intent, result.value, section);
      if (!guard.isCurrent(generation)) return;
      if (saved) setReceipt(saved);
      setProblem(null);
      if (isTerminal(result.value.state) && !isTerminal(receipt.state)) onProgress?.();
    } catch {
      if (guard.isCurrent(generation)) setProblem({ reason: 'unknown', detail: 'The operation receipt could not be saved. Keep the request reference below.' });
    }
  }, [guard, receipt.idempotencyKey, receipt.operationId, receipt.state, workspaceId, store, scope, intent, section, onProgress]);
  useEffect(() => {
    if (isTerminal(receipt.state)) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [receipt.state, refresh]);
  return <article className="rounded border p-4" aria-label="Submitted lifecycle phase">
    <p role="status">{receipt.state === 'succeeded' ? 'This phase completed. Workspace readiness is checked separately.' : `Phase status: ${receipt.state}.`}</p>
    <p>Request reference: {receipt.idempotencyKey}</p>
    {receipt.operationId && <p>Operation: {receipt.operationId}</p>}
    <Button variant="secondary" onClick={() => void refresh()}>Refresh phase status</Button>
    {problem && <Alert variant="warning" title="Phase status unavailable">{problem.detail}</Alert>}
  </article>;
}

function ProposalReview({ proposal, workspaceId, scope, store, mayManage, onProgress,
  mintKey = () => crypto.randomUUID(), section }: Props & { proposal: LifecycleProposal }) {
  const [guard] = useState(() => new ScopeGuard());
  const intent = `continue:${workspaceId}:${proposal.artifactId}`;
  const [receipt, setReceipt] = useState<StoredReceipt | null>(() => readReceipt(store, scope, intent));
  const [review, setReview] = useState<LifecycleProposal | null>(null);
  const [approval, setApproval] = useState<OperationApproval | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [busy, setBusy] = useState(false);
  const now = useFreshnessClock();
  useEffect(() => () => guard.supersede(), [guard]);
  useEffect(() => {
    if (!receipt?.approvalId) return;
    const refresh = async () => {
      const result = await getApproval(guard, receipt.approvalId!);
      if (result.ok && result.value.approval_id === receipt.approvalId && result.value.workspace_id === workspaceId) setApproval(result.value);
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [guard, receipt?.approvalId, workspaceId]);
  useEffect(() => {
    if (!receipt || receipt.submissionStage !== 'submitted' || isTerminal(receipt.state)) return;
    let active = true;
    const refresh = async () => {
      const generation = guard.current();
      const result = receipt.operationId ? await getOperation(guard, receipt.operationId) : await recoverOperation(guard, receipt.idempotencyKey);
      if (active && result.ok && result.value.idempotencyKey === receipt.idempotencyKey && result.value.workspaceId === workspaceId) {
        try {
          const saved = await recordObservationExclusive(store, scope, intent, result.value, section);
          if (!active || !guard.isCurrent(generation)) return;
          if (saved) setReceipt(saved);
          if (isTerminal(result.value.state)) onProgress?.();
        } catch {
          if (active && guard.isCurrent(generation)) setProblem({ reason: 'unknown', detail: 'The operation receipt could not be saved. Keep this request reference to recover its status.' });
        }
      }
    };
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => { active = false; clearInterval(timer); };
  }, [guard, receipt?.idempotencyKey, receipt?.operationId, receipt?.state, receipt?.submissionStage, workspaceId, store, scope, intent, section, onProgress]);

  const preview = async () => {
    if (!mayManage || busy) return;
    setBusy(true);
    const generation = guard.current();
    try {
      const claim = await claimPreviewIdentity(store, scope, intent, {
        workspace_id: workspaceId, artifact_id: proposal.artifactId, request_revision: proposal.requestRevision,
        plan_file_sha256: proposal.planFileSha256, plan_json_sha256: proposal.planJsonSha256,
      }, mintKey, new Date().toISOString(), section);
      if (!guard.isCurrent(generation)) return;
      if (claim.kind === 'conflict') { setProblem({ reason: 'unknown', detail: claim.detail }); return; }
      setReceipt(claim.receipt);
      const result = await previewLifecycleProposal(guard, workspaceId, proposal.artifactId, claim.receipt.idempotencyKey);
      if (isSuperseded(result)) return;
      if (result.ok) {
        if (result.value.requestRevision !== proposal.requestRevision || result.value.sourceOperationId !== proposal.sourceOperationId ||
            result.value.planFileSha256 !== proposal.planFileSha256 || result.value.planJsonSha256 !== proposal.planJsonSha256) {
          setProblem({ reason: 'unknown', detail: 'The recorded plan changed. Refresh the lifecycle plans before reviewing it.' });
          return;
        }
        setReview(result.value); setProblem(null);
      } else if ('unavailable' in result) setProblem(result.unavailable);
    } catch {
      if (guard.isCurrent(generation)) setProblem({ reason: 'unknown', detail: 'The review identity could not be saved. No new request was submitted.' });
    } finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  const askApproval = async () => {
    if (!mayManage || busy || !review?.approvalRequest || !receipt || receipt.submissionStage === 'submitted') return;
    setBusy(true);
    const generation = guard.current();
    try {
      const saved = await markSubmissionStage(store, scope, intent, receipt.idempotencyKey, 'approval', undefined, section);
      if (!saved || !guard.isCurrent(generation)) return;
      setReceipt(saved);
      const result = await requestApproval(guard, review.approvalRequest);
      if (isSuperseded(result)) return;
      if (result.ok) {
        if (result.value.workspace_id !== workspaceId) { setProblem({ reason: 'unknown', detail: 'The approval response did not match this workspace.' }); return; }
        const updated = await markSubmissionStage(store, scope, intent, saved.idempotencyKey, 'approval', result.value.approval_id, section);
        if (!guard.isCurrent(generation)) return;
        setReceipt(updated); setApproval(result.value); setProblem(null);
      } else if ('unavailable' in result) setProblem(result.unavailable);
    } catch {
      if (guard.isCurrent(generation)) setProblem({ reason: 'unknown', detail: 'The approval receipt could not be saved. Review again using the same request reference.' });
    } finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  const currentApproval = approval?.result === 'allowed-once' && !approval.revoked && Date.parse(approval.expires_at) > now;
  const proceed = async () => {
    if (!mayManage || busy || !receipt || !review || !approval || !currentApproval || isTerminal(receipt.state)) return;
    setBusy(true);
    const generation = guard.current();
    try {
      const checked = await previewLifecycleProposal(guard, workspaceId, proposal.artifactId, receipt.idempotencyKey);
      if (isSuperseded(checked)) return;
      if (!checked.ok) { if ('unavailable' in checked) setProblem(checked.unavailable); return; }
      if (checked.value.revision !== review.revision || checked.value.planFileSha256 !== review.planFileSha256 || checked.value.planJsonSha256 !== review.planJsonSha256) {
        setProblem({ reason: 'unknown', detail: 'The reviewed plan changed. No continuation was submitted.' }); return;
      }
      const saved = await markSubmissionStage(store, scope, intent, receipt.idempotencyKey, 'submitted', approval.approval_id, section);
      if (!saved || !guard.isCurrent(generation)) return;
      setReceipt(saved);
      const result = await continueLifecycleProposal(guard, workspaceId, proposal.artifactId, saved.idempotencyKey, approval.approval_id);
      if (isSuperseded(result)) return;
      const observation = result.ok ? result.value : { idempotencyKey: saved.idempotencyKey, operationId: saved.operationId,
        workspaceId, state: 'unknown' as const };
      const updated = await recordObservationExclusive(store, scope, intent, observation, section);
      if (!guard.isCurrent(generation)) return;
      if (updated) setReceipt(updated);
      if (!result.ok && 'unavailable' in result) setProblem(result.unavailable);
      else { setProblem(null); if (isTerminal(observation.state)) onProgress?.(); }
    } catch {
      if (guard.isCurrent(generation)) setProblem({ reason: 'unknown', detail: 'The continuation receipt could not be saved. Keep the existing request reference; its outcome must be recovered before starting other work.' });
    } finally { if (guard.isCurrent(generation)) setBusy(false); }
  };
  const displayed = review ?? proposal;
  return <article className="rounded border p-4 space-y-3" aria-label="Recorded lifecycle plan">
    <h4 className="font-semibold">{displayed.phase.replace(/-/g, ' ')}</h4>
    <p>Account: {displayed.accountId}</p>
    {Object.entries(displayed.target).map(([key, value]) => <p key={key}>{key.replace(/_/g, ' ')}: {value}</p>)}
    <p>Plan reference: {displayed.artifactId}</p>
    {displayed.planFileSha256 && <p className="break-all">Saved plan SHA-256: {displayed.planFileSha256}</p>}
    {displayed.planJsonSha256 && <p className="break-all">Reviewed plan SHA-256: {displayed.planJsonSha256}</p>}
    <details><summary>Resource changes and cost estimate</summary>
      <pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify({ resources: displayed.inventory, estimate: displayed.estimate }, null, 2)}</pre>
    </details>
    {mayManage && <Button variant="secondary" disabled={busy} onClick={() => void preview()}>Review this lifecycle plan</Button>}
    {mayManage && review && !approval && receipt?.submissionStage !== 'submitted' && <Button disabled={busy} onClick={() => void askApproval()}>Request approval for this phase</Button>}
    {approval && <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />}
    {mayManage && review && approval && <Button disabled={busy || !currentApproval || !!receipt && isTerminal(receipt.state)} onClick={() => void proceed()}>Continue approved phase</Button>}
    {receipt && <p role="status">{receipt.state === 'succeeded' ? 'This phase completed. Workspace readiness is checked separately.' : `Phase status: ${receipt.state}.`}
      {' '}Request reference: {receipt.idempotencyKey}{receipt.operationId ? `; operation: ${receipt.operationId}` : ''}</p>}
    {problem && <Alert variant="warning" title="Lifecycle continuation unavailable">{problem.detail}</Alert>}
  </article>;
}
