import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { ScheduleAvailability, ServiceSection } from './L5ServiceJourney';
import {
  checkTransformConfiguration, getTransformArtifact, getTransformResult, getTransformRun, listTransformBindings, listTransformResults, listTransformRuns,
  mutateTransformRun, readTransformPatch, submitTransformRun, type SecurityBinding, type SecurityScope,
  type TransformConfiguration, type TransformResult, type TransformRun, type Versioned,
} from '@/services/l5Services';

interface Props { productId: string; projectId: string; tenantId: string;
  configuration: Versioned<TransformConfiguration>; onAccessDenied: () => void }
const statusOf = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
const failure = (error: unknown) => statusOf(error) === 503 ? 'Unavailable (HTTP 503); no run was started.' :
  `Request failed${typeof statusOf(error) === 'number' ? ` (HTTP ${statusOf(error)})` : ''}; no success is implied.`;

export default function TransformRunsResults({ productId, projectId, tenantId, configuration, onAccessDenied }: Props) {
  const [bindings, setBindings] = useState<SecurityBinding[]>([]);
  const [runs, setRuns] = useState<TransformRun[]>([]);
  const [results, setResults] = useState<TransformResult[]>([]);
  const [runCursor, setRunCursor] = useState<string>();
  const [resultCursor, setResultCursor] = useState<string>();
  const [readiness, setReadiness] = useState<{ ready: boolean; gaps: string[] } | null>(null);
  const [executionError, setExecutionError] = useState('');
  const [run, setRun] = useState<Versioned<TransformRun> | null>(null);
  const [result, setResult] = useState<Versioned<TransformResult> | null>(null);
  const [patch, setPatch] = useState('');
  const [bindingId, setBindingId] = useState('');
  const [reason, setReason] = useState('');
  const [runError, setRunError] = useState('');
  const [resultError, setResultError] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const scopeMatches = useCallback((scope: SecurityScope) =>
    scope.tenant_id === tenantId && scope.product_id === productId && scope.project_id === projectId
    && scope.relationship_revision === configuration.value.scope.relationship_revision,
  [tenantId, productId, projectId, configuration.value.scope.relationship_revision]);
  const deny = useCallback(() => {
    setBindings([]); setRuns([]); setResults([]); setRun(null); setResult(null); setPatch('');
    setRunCursor(undefined); setResultCursor(undefined); setReadiness(null); setBindingId('');
    onAccessDenied();
  }, [onAccessDenied]);
  const handleError = useCallback((error: unknown, report: (value: string) => void) => {
    if (statusOf(error) === 403 || statusOf(error) === 404) { deny(); return; }
    report(failure(error));
  }, [deny]);

  const load = useCallback(async () => {
    setBusy(true);
    setRunError(''); setResultError(''); setExecutionError(''); setReadiness(null); setBindingId('');
    setRunCursor(undefined); setResultCursor(undefined);
    const [bindingResponse, runResponse, resultResponse, readinessResponse] = await Promise.allSettled([
      listTransformBindings(productId, projectId), listTransformRuns(productId, projectId), listTransformResults(productId, projectId),
      checkTransformConfiguration(productId, projectId),
    ]);
    for (const response of [readinessResponse, runResponse, resultResponse]) {
      if (response.status === 'rejected' && [403, 404].includes(Number(statusOf(response.reason)))) {
        deny(); setBusy(false); return;
      }
    }
    if (readinessResponse.status === 'fulfilled') setReadiness(readinessResponse.value);
    else setExecutionError(failure(readinessResponse.reason));
    if (bindingResponse.status === 'fulfilled' && bindingResponse.value.items.every(item => scopeMatches(item.scope))) {
      setBindings(bindingResponse.value.items);
    } else if (bindingResponse.status === 'fulfilled') { deny(); setBusy(false); return; }
    else {
      setBindings([]);
      setExecutionError([403, 404].includes(Number(statusOf(bindingResponse.reason)))
        ? 'Execution binding or delegation is unavailable. Configuration editing remains available.' : failure(bindingResponse.reason));
    }
    if (runResponse.status === 'fulfilled' && runResponse.value.items.every(item => scopeMatches(item.scope))) {
      setRuns(runResponse.value.items);
      setRunCursor(runResponse.value.next_cursor);
    } else if (runResponse.status === 'fulfilled') { deny(); setBusy(false); return; }
    else { setRuns([]); setRunError(failure(runResponse.reason)); }
    if (resultResponse.status === 'fulfilled' && resultResponse.value.items.every(item => scopeMatches(item.scope) && item.service_id === 'transform')) {
      setResults(resultResponse.value.items);
      setResultCursor(resultResponse.value.next_cursor);
    } else if (resultResponse.status === 'fulfilled') { deny(); setBusy(false); return; }
    else { setResults([]); setResultError(failure(resultResponse.reason)); }
    setBusy(false);
  }, [productId, projectId, scopeMatches, deny]);

  useEffect(() => { void load(); }, [load, configuration.etag]);

  async function loadMoreRuns() {
    if (!runCursor || busy) return;
    setBusy(true); setRunError('');
    try {
      const page = await listTransformRuns(productId, projectId, runCursor);
      if (!page.items.every(item => scopeMatches(item.scope))) { deny(); return; }
      setRuns(current => [...current.filter(item => !page.items.some(next => next.run_id === item.run_id)), ...page.items]);
      setRunCursor(page.next_cursor);
    } catch (error) { handleError(error, setRunError); }
    finally { setBusy(false); }
  }
  async function loadMoreResults() {
    if (!resultCursor || busy) return;
    setBusy(true); setResultError('');
    try {
      const page = await listTransformResults(productId, projectId, resultCursor);
      if (!page.items.every(item => scopeMatches(item.scope) && item.service_id === 'transform')) { deny(); return; }
      setResults(current => [...current.filter(item => !page.items.some(next => next.result_id === item.result_id)), ...page.items]);
      setResultCursor(page.next_cursor);
    } catch (error) { handleError(error, setResultError); }
    finally { setBusy(false); }
  }

  async function inspectRun(runId: string) {
    setBusy(true); setRunError('');
    try {
      const read = await getTransformRun(productId, projectId, runId);
      if (!scopeMatches(read.value.scope) || read.value.run_id !== runId) { deny(); return; }
      setRun(read);
    } catch (error) { handleError(error, setRunError); }
    finally { setBusy(false); }
  }
  async function inspectResult(resultId: string) {
    setBusy(true); setResultError(''); setPatch(''); setResult(null);
    try {
      const read = await getTransformResult(productId, projectId, resultId);
      if (!scopeMatches(read.value.scope) || read.value.result_id !== resultId || read.value.service_id !== 'transform') { deny(); return; }
      setResult(read);
    } catch (error) { handleError(error, setResultError); }
    finally { setBusy(false); }
  }
  async function inspectPatch() {
    const reference = result?.value.retained_artifacts?.original_patch;
    if (!reference || !result || reference.sha256 !== result.value.original_patch.sha256
      || reference.tenant_id !== tenantId || reference.project_id !== projectId) { setResultError('Original patch evidence is unavailable or mismatched.'); return; }
    setBusy(true); setResultError(''); setPatch('');
    try {
      const access = await getTransformArtifact(productId, projectId, reference.artifact_id);
      if (access.artifact.sha256 !== reference.sha256 || access.artifact.version !== reference.version
        || access.artifact.tenant_id !== tenantId || access.artifact.project_id !== projectId) throw new Error('Artifact metadata does not match the retained patch.');
      setPatch(await readTransformPatch(productId, projectId, reference.artifact_id, reference.sha256));
    } catch (error) { handleError(error, setResultError); }
    finally { setBusy(false); }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const binding = bindings.find(item => item.binding_id === bindingId && item.configuration_id === configuration.value.configuration_id
      && item.state === 'ready' && scopeMatches(item.scope));
    const source = configuration.value.settings.source_refs[0];
    const capability = configuration.value.settings.capabilities[0];
    const goal = configuration.value.settings.transform.goal_defaults;
    if (busy || !readiness?.ready || !configuration.value.settings.enabled || !binding || !source
      || source.record_type !== 'repository_commit' || source.tenant_id !== tenantId || source.project_id !== projectId
      || !capability || goal.kind !== capability) return;
    setBusy(true); setRunError(''); setMessage('');
    try {
      const accepted = await submitTransformRun(productId, projectId, {
        service_id: 'transform', configuration_revision: configuration.value.revision, binding_id: binding.binding_id,
        capability, sources: [source], transform_input: { goal },
      });
      if (!scopeMatches(accepted.value.scope)) { deny(); return; }
      setRun(accepted); setRuns(current => [accepted.value, ...current.filter(item => item.run_id !== accepted.value.run_id)]);
      setMessage('Run accepted. Admission is not a passed test; inspect the retained result for verification.');
    } catch (error) { handleError(error, setRunError); }
    finally { setBusy(false); }
  }
  async function mutate(action: 'cancel' | 'reconcile') {
    if (!run || !reason.trim() || busy) return;
    setBusy(true); setRunError(''); setMessage('');
    try {
      const accepted = await mutateTransformRun(productId, projectId, run.value.run_id, action, reason.trim());
      if (!scopeMatches(accepted.value.scope) || accepted.value.run_id !== run.value.run_id) { deny(); return; }
      setRun(accepted); setReason('');
      setMessage(`${action} accepted; refresh for the observed outcome.`);
    } catch (error) { handleError(error, setRunError); }
    finally { setBusy(false); }
  }

  return <div className="space-y-4" aria-label="Transform runs and evidence">
    <ServiceSection service="transform" step="runs" description="Start work from a saved configuration and follow its observed progress.">
    <ScheduleAvailability service="transform" />
    <p>Manual runs use an existing linked, qualified binding. New Transform binding setup: Not available yet. Saving executor references alone does not establish readiness.</p>
    <button type="button" disabled={busy} onClick={() => void load()}>Refresh Transform status</button>
    {message && <p role="status">{message}</p>}
    {runError && <p role="alert">Runs: {runError}</p>}
    {executionError && <p role="alert">Execution: {executionError}</p>}
    {readiness && <p>Current configuration readiness: {readiness.ready ? 'ready' : 'not ready'}; gaps: {readiness.gaps.join(', ') || 'none'}.</p>}
    {readiness?.gaps.some(gap => ['executor_unqualified', 'executor_qualification_stale'].includes(gap)) && <p>Ask the Transform service operator to qualify the saved executor and test policy for this existing binding, then choose Refresh Transform status. Saving the references again does not qualify the executor.</p>}
    <form onSubmit={event => void submit(event)}>
      <label>Ready Transform binding <select value={bindingId} onChange={event => setBindingId(event.target.value)}>
        <option value="">Choose a ready binding</option>
        {bindings.filter(item => item.configuration_id === configuration.value.configuration_id && item.state === 'ready').map(item =>
          <option value={item.binding_id} key={item.binding_id}>{item.binding_id}</option>)}
      </select></label>
      <button type="submit" disabled={busy || !bindingId || !readiness?.ready || !configuration.value.settings.enabled}>Start Transform run</button>
    </form>
    {!runError && !runs.length && <p>No Transform runs are visible.</p>}
    {runs.map(item => <button type="button" key={item.run_id} disabled={busy} onClick={() => void inspectRun(item.run_id)}>
      Run {item.run_id}: {item.state} (provider: {item.provider_state})
    </button>)}
    {runCursor && <button type="button" disabled={busy} onClick={() => void loadMoreRuns()}>Load more Transform runs</button>}
    {run && <div><p>Run {run.value.run_id}, revision {run.value.revision}, state {run.value.state}, provider {run.value.provider_state}.</p>
      <p>Progress: {run.value.monitoring?.progress_state ?? 'unknown'}; observation: {run.value.monitoring?.last_successful_poll_at ?? 'unavailable'}; next poll: {run.value.monitoring?.next_poll_at ?? 'unknown'}.</p>
      <p>Monitoring: {run.value.monitoring?.state ?? 'unknown'}; observation coverage: {run.value.monitoring?.latest_observation?.progress_coverage ?? 'unavailable'}.</p>
      <label>Reason for lifecycle action <input value={reason} onChange={event => setReason(event.target.value)} /></label>
      <button type="button" disabled={!reason.trim() || busy} onClick={() => void mutate('cancel')}>Cancel run</button>
      <button type="button" disabled={!reason.trim() || busy} onClick={() => void mutate('reconcile')}>Reconcile run</button>
    </div>}
    </ServiceSection>
    <ServiceSection service="transform" step="results" description="Inspect retained patches, independent checks, and limitations.">
    <p>Repository PR publication is unavailable: no separately authorized SCM handoff is installed. Inspecting a patch never pushes, merges or deploys.</p>
    {resultError && <p role="alert">Results: {resultError}</p>}
    {!resultError && !results.length && <p>No retained Transform results are visible.</p>}
    {results.map(item => <button type="button" key={item.result_id} disabled={busy} onClick={() => void inspectResult(item.result_id)}>
      Result {item.result_id}: {item.verification_status}
    </button>)}
    {resultCursor && <button type="button" disabled={busy} onClick={() => void loadMoreResults()}>Load more Transform results</button>}
    {result && <div><p>Result {result.value.result_id}, revision {result.value.revision}: verification {result.value.verification_status}; scope {result.value.scope_status}; dependency comparison {result.value.dependency_comparison?.status ?? 'unknown'}.</p>
      <p>Observed {result.value.observed_at}; retained until {result.value.retention_expires_at}; cost {result.value.cost_status}.</p>
      <p>Original patch SHA-256: {result.value.original_patch?.sha256 ?? 'unavailable'} ({result.value.original_patch?.bytes ?? 'unknown'} bytes).</p>
      <button type="button" onClick={() => void inspectPatch()} disabled={busy || !result.value.retained_artifacts?.original_patch}>Inspect original diff</button>
      <button type="button" disabled>Publish Transform PR (unavailable)</button>
      {patch && <pre aria-label="Original provider diff" className="overflow-auto whitespace-pre-wrap">{patch}</pre>}
      <h4>Independent checks before the patch</h4>
      {result.value.tests_before.map((item, index) => <p key={index}>{item.name ?? `Check ${index + 1}`}: {item.status}</p>)}
      {!result.value.tests_before.length && <p>No before-patch tests observed.</p>}
      <h4>Independent checks after the patch</h4>
      {result.value.tests_after.map((item, index) => <p key={index}>{item.name ?? `Check ${index + 1}`}: {item.status}</p>)}
      {!result.value.tests_after.length && <p>No after-patch tests observed.</p>}
      <h4>Limitations</h4><p>{result.value.limitations.join(', ') || 'None reported'}</p>
    </div>}
    </ServiceSection>
  </div>;
}
