import { WorkloadCancellation } from './WorkloadCancellation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Input } from '@/components/ui';
import { isSuperseded, ScopeGuard } from './client';
import type { Unavailable } from './contract';
import { fingerprint, receiptsForIntentPrefix, type ReceiptScope, type ReceiptStore, type StoredReceipt } from './operations';
import { WorkloadAction, WorkloadReceipt } from './ServingPanel';
import { getBatchCatalog, listBatchJobs, type BatchCatalog, type BatchInput, type BatchProfile, type ServingDeployment } from './workloads';

interface Props { workspaceId: string; scope: ReceiptScope; store: ReceiptStore }

export function BatchPanel(props: Props) {
  return <BatchWorkspace key={`${props.scope.orgId}:${props.workspaceId}`} {...props} />;
}

function BatchWorkspace(props: Props) {
  const [guard] = useState(() => new ScopeGuard());
  const [rows, setRows] = useState<ServingDeployment[]>([]);
  const [catalog, setCatalog] = useState<BatchCatalog | null>(null);
  const [problem, setProblem] = useState<Unavailable | null>(null);
  const [observed, setObserved] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [candidate, setCandidate] = useState<BatchInput | null>(null);
  const [stopping, setStopping] = useState<ServingDeployment | null>(null);
  const [receipts, setReceipts] = useState<Array<{ intent: string; receipt: StoredReceipt }>>([]);
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    const serial = ++latest.current;
    const [listed, available] = await Promise.all([listBatchJobs(guard, props.workspaceId), getBatchCatalog(guard, props.workspaceId)]);
    if (isSuperseded(listed) || serial !== latest.current) return;
    setCatalog(available.ok ? available.value : null);
    if (listed.ok) {
      setRows(listed.value.jobs); setTruncated(listed.value.truncated);
      setObserved(new Date().toLocaleTimeString()); setProblem(null);
    } else if ('unavailable' in listed) {
      setProblem(listed.unavailable);
      if (listed.unavailable.reason === 'not-permitted') { setRows([]); setObserved(null); setTruncated(false); }
    }
    try { setReceipts(receiptsForIntentPrefix(props.store, props.scope, `batch:${props.workspaceId}:`)); }
    catch { setProblem({ reason: 'unknown', detail: 'Saved batch requests could not be read. Recover their references before submitting again.' }); }
  }, [guard, props.workspaceId, props.scope, props.store]);
  useEffect(() => {
    void refresh(); const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); guard.supersede(); };
  }, [guard, refresh]);
  return <section aria-label="Batch jobs" className="mt-6 space-y-4">
    <h3 className="font-semibold">Batch jobs</h3>
    <p>Select a configured image and invocation, review the limits, then request approval.</p>
    <Button variant="secondary" onClick={() => void refresh()}>Refresh batch jobs</Button>
    {observed && <p>Status retrieved at {observed}. Workload outcome, logs, result references and observed cost are not reported yet.</p>}
    {problem && <Alert variant="warning" title="Batch status unavailable">{problem.detail}{observed ? ' Displayed status may be stale.' : ''}</Alert>}
    {!catalog?.canSubmit && <p>New batch submissions are unavailable. The server must confirm workspace permissions, batch profiles and operation transport.</p>}
    {observed && !problem && rows.length === 0 && <p>No batch jobs are listed. This does not prove cleanup of earlier requests.</p>}
    {truncated && <p>Showing the latest 100 jobs. Earlier operations remain available through saved request references.</p>}
    <ul className="space-y-3">{rows.map((row) => <li className="rounded border p-4 break-words" key={row.deploymentId}>
      <h4 className="font-semibold">{row.name}</h4>
      <p>Status: {row.status}; operation: {row.operationState}</p>
      {row.operationId && <p>Operation reference: {row.operationId}</p>}
      {row.cancellationRequested && <p>Cancellation requested. Cleanup: {row.cleanupStatus}.</p>}
      {row.status === 'CancelledBeforeDispatch' && <p>Cancelled before dispatch; no workload cleanup is required.</p>}
      {catalog?.canCancel && row.deploymentId && row.operationId && !['Deleting', 'Deleted', 'CancelledBeforeDispatch'].includes(row.status) &&
        ['accepted', 'running', 'unknown'].includes(row.operationState) && <WorkloadCancellation workspaceId={props.workspaceId} row={row} kind="batch" onProgress={() => void refresh()} />}
      {row.providerUid && <p>Recorded Job UID: {row.providerUid}</p>}
      <p>Observed cost: unknown. Cleanup: {row.cleanupStatus === 'confirmed' ? 'confirmed by the backend' : row.cleanupStatus}.</p>
      {catalog?.canReviewTeardown && row.deploymentId && row.operationId && !['Deleting', 'Deleted', 'CancelledBeforeDispatch'].includes(row.status) &&
        <Button variant="secondary" onClick={() => { setStopping(row); setCandidate(null); }}>Review stop for {row.name}</Button>}
    </li>)}</ul>
    {catalog?.canSubmit && <BatchForm profiles={catalog.profiles} onReview={(input) => { setCandidate(input); setStopping(null); }} />}
    {candidate && catalog?.canSubmit && <WorkloadAction key={fingerprint(candidate)} {...props} kind="batch" mayManage={catalog.canSubmit} input={candidate} onProgress={() => void refresh()} />}
    {stopping?.deploymentId && catalog?.canReviewTeardown && <WorkloadAction key={stopping.deploymentId} {...props} kind="batch" mayManage={catalog.canReviewTeardown} input={{ deploymentId: stopping.deploymentId }} onProgress={() => void refresh()} />}
    {receipts.filter(({ receipt }) => receipt.submissionStage === 'submitted').map(({ intent, receipt }) =>
      <WorkloadReceipt key={receipt.idempotencyKey} {...props} kind="batch" intent={intent} initial={receipt} />)}
    <p>Cancel pending operations to withdraw execution. Review stop after the original operation settles. Resources remain reserved until the backend confirms non-execution or cleanup.</p>
  </section>;
}

function BatchForm({ profiles, onReview }: { profiles: BatchProfile[]; onReview: (input: BatchInput) => void }) {
  const [name, setName] = useState('');
  const [profileId, setProfileId] = useState('');
  const profile = profiles.find((entry) => entry.profileId === profileId);
  return <form aria-label="New batch job" className="rounded border p-4 space-y-3" onSubmit={(event) => {
    event.preventDefault(); if (profile) onReview({ name: name.trim(), profile_id: profile.profileId, batch_options: profile.options });
  }}>
    <h4 className="font-semibold">New batch job</h4>
    <div className="grid gap-3 sm:grid-cols-2">
      <Input name="batch-name" label="Job name" required pattern="[a-z][a-z0-9-]{0,50}" maxLength={51} value={name} onChange={(event) => setName(event.target.value)} />
      <label>Batch profile<select required className="block w-full rounded border p-2" value={profile?.profileId ?? ''} onChange={(event) => setProfileId(event.target.value)}>
        <option value="">Select a batch profile</option>
        {profiles.map((entry) => <option key={entry.profileId} value={entry.profileId}>{entry.profileId}</option>)}
      </select></label>
    </div>
    {profile && <div className="space-y-2">
      <p className="break-all">Image: {profile.options.image}</p>
      <p>GPUs: {profile.options.gpu_count}; CPU: {profile.options.cpu}; memory: {profile.options.memory}</p>
      <p>Invocation arguments (in order):</p><pre className="whitespace-pre-wrap break-all">{JSON.stringify([...profile.options.command, ...profile.options.args], null, 2)}</pre>
      <p>Source and input data follow this installed image and invocation. Review the exact runtime and cost bounds before submitting.</p>
    </div>}
    <Button type="submit" variant="secondary" disabled={!profile}>Prepare batch review</Button>
  </form>;
}
