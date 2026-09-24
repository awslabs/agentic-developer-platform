import { useCallback, useEffect, useRef, useState } from 'react';

import { Alert, Button, Input } from '@/components/ui';

import { ApprovalPanel } from './ApprovalPanel';
import { getApproval, isSuperseded, requestApproval, ScopeGuard } from './client';
import type { OperationApproval, Unavailable } from './contract';
import {
  claimPreviewIdentity, fingerprint, markSubmissionStage,
  receiptsForIntentPrefix, recordObservationExclusive,
  type ReceiptScope, type ReceiptStore, type StoredReceipt,
} from './operations';
import { useFreshnessClock } from './readiness';
import {
  listDeployments, previewServing, submitServing,
  type ServingDeployment, type ServingInput, type ServingReview,
} from './workloads';

interface Props {
  workspaceId: string;
  scope: ReceiptScope;
  store: ReceiptStore;
  mayManage: boolean;
}

export function ServingPanel(props: Props) {
  // A scope-keyed child also protects direct callers which change workspace props.
  return <ServingWorkspace key={`${props.scope.orgId}:${props.workspaceId}`} {...props} />;
}

function ServingWorkspace(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [rows, setRows] = useState<ServingDeployment[]>([]);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [observed, setObserved] = useState<string | null>(null);
  const [candidate, setCandidate] = useState<ServingInput | null>(null);
  const [stopping, setStopping] = useState<ServingDeployment | null>(null);
  const [receipts, setReceipts] = useState<Array<{ intent: string; receipt: StoredReceipt }>>([]);
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++latest.current;
    const result = await listDeployments(guard, props.workspaceId);
    if (isSuperseded(result) || request !== latest.current) return;
    if (result.ok) { setRows(result.value); setProblem(null); setObserved(new Date().toLocaleTimeString()); }
    else if ('unavailable' in result) {
      setProblem(result.unavailable);
      if (result.unavailable.reason === 'not-permitted') { setRows([]); setObserved(null); }
    }
    try {
      setReceipts(receiptsForIntentPrefix(props.store, props.scope, `serving:${props.workspaceId}:`));
    } catch {
      setProblem({ reason: 'unknown', detail: 'Saved workload requests could not be read. Recover their request references before submitting again.' });
    }
  }, [guard, props.workspaceId, props.scope, props.store]);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); guard.supersede(); };
  }, [guard, refresh]);
  return <section className="mt-6 space-y-4" aria-label="Serving workloads">
    <h3 className="font-semibold">Serving workloads</h3>
    <p>Review a configured serving profile before requesting approval. Creation status does not establish endpoint health or resource cleanup.</p>
    <Button variant="secondary" onClick={() => void refresh()}>Refresh serving workloads</Button>
    {observed && <p>Status retrieved at {observed}. Endpoint health and observed cost are not reported by this view.</p>}
    {problem && <Alert variant="warning" title="Workload status unavailable">{problem.detail}{observed ? ' Displayed status may be stale.' : ''}</Alert>}
    {observed && !problem && rows.length === 0 && <p>No serving deployments are listed. An absent list entry does not prove cleanup of an earlier request.</p>}
    <ul className="space-y-3">
      {rows.map((row, index) => <li className="rounded border p-4 break-words" key={row.deploymentId ?? `${row.name}:${index}`}>
        <h4 className="font-semibold">{row.name}</h4>
        <p>Status: {row.status}; operation: {row.operationState}</p>
        {row.operationId && <p>Operation reference: {row.operationId}</p>}
        {row.providerUid && <p>Recorded resource: {row.providerUid}</p>}
        <p>Cleanup and observed cost: not reported</p>
        {props.mayManage && row.deploymentId && row.operationId && row.status !== 'Deleted' &&
          <Button variant="secondary" onClick={() => setStopping(row)}>Review stop for {row.name}</Button>}
      </li>)}
    </ul>
    {props.mayManage && <ServingForm onReview={(input) => { setCandidate(input); setStopping(null); }} />}
    {candidate && props.mayManage && <ServingAction key={fingerprint(candidate)} {...props}
      input={candidate} onProgress={() => void refresh()} />}
    {stopping?.deploymentId && props.mayManage && <ServingAction key={stopping.deploymentId} {...props}
      input={{ deploymentId: stopping.deploymentId }} onProgress={() => void refresh()} />}
    {receipts.filter(({ receipt }) => receipt.submissionStage === 'submitted').map(({ intent, receipt }) =>
      <p className="break-all" key={intent}>Saved workload request: {receipt.idempotencyKey}; operation reference: {receipt.operationId ?? 'awaiting response'}; last response: {receipt.state}. Cleanup requires a separate provider observation.</p>)}
    <p>Batch submission, workload logs and authenticated endpoint access are not available in this view yet.</p>
  </section>;
}

function ServingForm({ onReview }: { onReview: (input: ServingInput) => void }) {
  const [name, setName] = useState('');
  const [profile, setProfile] = useState('');
  const [model, setModel] = useState('');
  const [precision, setPrecision] = useState('fp16');
  const [framework, setFramework] = useState('vllm');
  const [gpus, setGpus] = useState('1');
  const [parallel, setParallel] = useState('1');
  const [length, setLength] = useState('');
  return <form className="rounded border p-4 space-y-3" aria-label="New serving deployment" onSubmit={(event) => {
    event.preventDefault();
    onReview({ name: name.trim(), profile_id: profile.trim(), model_name: model.trim(), precision,
      serving_framework: framework, replicas: 1, gpu_per_replica: Number(gpus),
      tensor_parallel_size: Number(parallel), max_model_len: length === '' ? null : Number(length) });
  }}>
    <h4 className="font-semibold">New serving deployment</h4>
    <p>Use a profile provided by your workspace administrator. The server checks that model options match its installed profile; approval includes its runtime and cost limits.</p>
    <div className="grid gap-3 sm:grid-cols-2">
      <Input name="serving-name" label="Deployment name" required pattern="[a-z][a-z0-9-]{0,49}[a-z0-9]" maxLength={51} value={name} onChange={(event) => setName(event.target.value)} />
      <Input name="serving-profile" label="Serving profile" required pattern="[a-z][a-z0-9-]{0,62}" value={profile} onChange={(event) => setProfile(event.target.value)} />
      <Input name="serving-model" label="Model name" required maxLength={500} value={model} onChange={(event) => setModel(event.target.value)} />
      <label>Precision<select className="block w-full rounded border p-2" value={precision} onChange={(event) => setPrecision(event.target.value)}>
        {['fp16', 'bf16', 'fp8', 'awq', 'int8'].map((value) => <option key={value}>{value}</option>)}
      </select></label>
      <label>Serving framework<select className="block w-full rounded border p-2" value={framework} onChange={(event) => setFramework(event.target.value)}>
        {['vllm', 'sglang'].map((value) => <option key={value}>{value}</option>)}
      </select></label>
      <Input name="serving-gpus" label="GPUs per replica" type="number" required min={1} max={8} value={gpus} onChange={(event) => setGpus(event.target.value)} />
      <Input name="serving-parallel" label="Tensor parallel size" type="number" required min={1} max={8} value={parallel} onChange={(event) => setParallel(event.target.value)} />
      <Input name="serving-length" label="Maximum model length (optional)" type="number" min={256} max={1048576} value={length} onChange={(event) => setLength(event.target.value)} />
    </div>
    <p>Replicas: 1</p>
    <Button type="submit" variant="secondary">Prepare serving review</Button>
  </form>;
}

function ServingAction({ input, onProgress, ...props }: Props & {
  input: ServingInput | { deploymentId: string }; onProgress: () => void;
}) {
  const [guard] = useState(() => new ScopeGuard());
  const [review, setReview] = useState<ServingReview | null>(null);
  const [receipt, setReceipt] = useState<StoredReceipt | null>(null);
  const [approval, setApproval] = useState<OperationApproval | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const teardown = 'deploymentId' in input;
  const intent = `serving:${props.workspaceId}:${teardown ? `stop:${input.deploymentId}` : `create:${input.name}`}`;
  const now = useFreshnessClock();
  useEffect(() => () => guard.supersede(), [guard]);
  const matching = (value: OperationApproval, plan: ServingReview) =>
    value.workspace_id === props.workspaceId && value.plan_digest === plan.revision &&
    value.action === (teardown ? 'teardown' : 'provision');
  const approved = !!approval && !!review && matching(approval, review) && approval.result === 'allowed-once' &&
    !approval.revoked && Date.parse(approval.expires_at) > now;

  const act = async (action: 'preview' | 'approval' | 'submit') => {
    if (!props.mayManage || locked.current) return;
    locked.current = true; setBusy(true);
    const generation = guard.current();
    try {
      if (action === 'preview') {
        const claim = await claimPreviewIdentity(props.store, props.scope, intent, input, () => crypto.randomUUID(), new Date().toISOString());
        if (!guard.isCurrent(generation)) return;
        if (claim.kind === 'conflict') { setProblem({ reason: 'unknown', detail: 'A different workload request remains unresolved. Recover the saved request before changing these inputs.' }); return; }
        setReceipt(claim.receipt);
        const result = await previewServing(guard, props.workspaceId, claim.receipt.idempotencyKey, input);
        if (isSuperseded(result)) return;
        if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
        setReview(result.value); setApproval(null); setProblem(null);
        if (claim.receipt.approvalId) {
          const previous = await getApproval(guard, claim.receipt.approvalId);
          if (previous.ok && previous.value.approval_id === claim.receipt.approvalId && matching(previous.value, result.value)) setApproval(previous.value);
        }
      } else if (action === 'approval' && review && receipt) {
        const saved = await markSubmissionStage(props.store, props.scope, intent, receipt.idempotencyKey, 'approval');
        if (!saved || !guard.isCurrent(generation)) return;
        const result = await requestApproval(guard, review.approvalRequest);
        if (isSuperseded(result)) return;
        if (!result.ok) { if ('unavailable' in result) setProblem(result.unavailable); return; }
        if (!matching(result.value, review)) { setProblem({ reason: 'unknown', detail: 'Approval does not match this workspace and reviewed plan.' }); return; }
        const updated = await markSubmissionStage(props.store, props.scope, intent, receipt.idempotencyKey, 'approval', result.value.approval_id);
        if (!guard.isCurrent(generation)) return;
        setReceipt(updated); setApproval(result.value); setProblem(null);
      } else if (action === 'submit' && approved && approval && review && receipt) {
        // Refresh approval at the mutation boundary, including retries after a lost reply.
        const current = await getApproval(guard, approval.approval_id);
        if (isSuperseded(current)) return;
        if (!current.ok) { if ('unavailable' in current) setProblem(current.unavailable); return; }
        if (current.value.approval_id !== approval.approval_id || !matching(current.value, review) ||
            current.value.result !== 'allowed-once' || current.value.revoked || Date.parse(current.value.expires_at) <= Date.now()) {
          setApproval(current.value); setProblem({ reason: 'not-permitted', detail: 'Approval is no longer valid for this plan.' }); return;
        }
        const checked = await previewServing(guard, props.workspaceId, receipt.idempotencyKey, input);
        if (isSuperseded(checked)) return;
        if (!checked.ok) { if ('unavailable' in checked) setProblem(checked.unavailable); return; }
        if (checked.value.revision !== review.revision || checked.value.deploymentId !== review.deploymentId) {
          setProblem({ reason: 'unknown', detail: 'The reviewed plan changed. Review and approve the current plan before submitting.' }); return;
        }
        const saved = await markSubmissionStage(props.store, props.scope, intent, receipt.idempotencyKey, 'submitted', approval.approval_id);
        if (!saved || !guard.isCurrent(generation)) return;
        setReceipt(saved);
        const result = await submitServing(guard, props.workspaceId, review, approval.approval_id, input);
        if (isSuperseded(result)) return;
        const updated = await recordObservationExclusive(props.store, props.scope, intent, {
          idempotencyKey: saved.idempotencyKey, workspaceId: props.workspaceId,
          operationId: result.ok ? result.value.operationId : saved.operationId,
          state: result.ok ? result.value.operationState : 'unknown',
        });
        if (!guard.isCurrent(generation)) return;
        setReceipt(updated);
        if (!result.ok && 'unavailable' in result) setProblem(result.unavailable);
        else setProblem(null);
        onProgress();
      }
    } catch {
      if (guard.isCurrent(generation)) {
        setProblem({ reason: 'unknown', detail: 'The request or its receipt could not be verified. Keep the saved request reference and recover its status before starting another workload.' });
      }
    } finally {
      locked.current = false;
      if (guard.isCurrent(generation)) setBusy(false);
    }
  };
  return <article className="rounded border p-4 space-y-3 break-words" aria-label={teardown ? 'Stop serving review' : 'Serving deployment review'}>
    <h4 className="font-semibold">{teardown ? 'Stop serving deployment' : 'Review serving deployment'}</h4>
    <Button variant="secondary" disabled={busy} onClick={() => void act('preview')}>Review {teardown ? 'stop' : 'serving'} plan</Button>
    {review && <>
      <p>Target: account {review.account}, region {review.region}, namespace {review.namespace}</p>
      <p className="break-all">Image: {review.image}</p>
      <p>Maximum resources: {review.resources}; maximum runtime: {review.runtimeSeconds} seconds</p>
      <p>Maximum additional cost: {review.maxCostMicros / 1_000_000} USD. Observed cost: unknown.</p>
      <p className="break-all">Plan reference: {review.revision}</p>
      {teardown && <p>Stopping requests governed cleanup of the original deployment. Existing resources and charges remain unresolved until provider verification completes.</p>}
      {!approval && <Button disabled={busy} onClick={() => void act('approval')}>Request workload approval</Button>}
    </>}
    {approval && <ApprovalPanel approval={approval} guard={guard} onChange={setApproval} />}
    {review && approval && <Button disabled={busy || !approved || receipt?.state === 'succeeded'} onClick={() => void act('submit')}>{teardown ? 'Submit approved stop' : 'Submit approved deployment'}</Button>}
    {receipt && <p role="status">Request reference: {receipt.idempotencyKey}; last response: {receipt.state}. {receipt.operationId ? `Operation: ${receipt.operationId}. ` : ''}Resource cleanup is checked separately.</p>}
    {problem && <Alert variant="warning" title="Workload request unavailable">{problem.detail}</Alert>}
  </article>;
}
