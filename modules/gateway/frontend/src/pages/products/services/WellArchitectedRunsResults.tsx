import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ScheduleAvailability, ServiceSection } from './L5ServiceJourney';
import WellArchitectedBindingCheck from './WellArchitectedBindingCheck';
import { appendReadbackPage } from '@/services/l5ReadbackPages';
import {
  checkWellArchitectedConfiguration, compareWellArchitectedResults, getWellArchitectedBindingOperation, getWellArchitectedPublicationStatus, getWellArchitectedResult, getWellArchitectedRun,
  listWellArchitectedBindings, listWellArchitectedResults, listWellArchitectedRuns,
  startWellArchitectedBinding, submitWellArchitectedRun, type SecurityBinding,
  type SecurityBindingOperation, type SecurityRun, type Versioned,
  type WellArchitectedComparison, type WellArchitectedConfiguration, type WellArchitectedPublicationStatus, type WellArchitectedResult,
} from '@/services/l5Services';

interface Props { productId: string; projectId: string; tenantId: string;
  configuration: Versioned<WellArchitectedConfiguration>; eligible: boolean; onAccessDenied: () => void;
  setupContent: ReactNode; capabilitiesContent: ReactNode;
  scheduleContent?: (bindings: SecurityBinding[], bindingsCurrent: boolean) => ReactNode }
const statusOf = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;

export default function WellArchitectedRunsResults({ productId, projectId, tenantId, configuration, eligible, onAccessDenied,
  setupContent, capabilitiesContent, scheduleContent }: Props) {
  const [bindings, setBindings] = useState<SecurityBinding[]>([]);
  const [bindingsReadAt, setBindingsReadAt] = useState<Versioned<WellArchitectedConfiguration> | null>(null);
  const [runs, setRuns] = useState<SecurityRun[]>([]);
  const [results, setResults] = useState<WellArchitectedResult[]>([]);
  const [runCursor, setRunCursor] = useState<string>();
  const [resultCursor, setResultCursor] = useState<string>();
  const [paging, setPaging] = useState({ runs: false, results: false });
  const pageRequests = useRef<Partial<Record<keyof typeof paging, symbol>>>({});
  const readGeneration = useRef(0);
  const inspectionGeneration = useRef(0);
  const invalidateReadback = useCallback(() => {
    readGeneration.current += 1; inspectionGeneration.current += 1; pageRequests.current = {};
    setPaging({ runs: false, results: false }); setRunCursor(undefined); setResultCursor(undefined);
  }, []);
  const [operation, setOperation] = useState<SecurityBindingOperation | null>(null);
  const [run, setRun] = useState<Versioned<SecurityRun> | null>(null);
  const [result, setResult] = useState<Versioned<WellArchitectedResult> | null>(null);
  const [publication, setPublication] = useState<WellArchitectedPublicationStatus | null>(null);
  const [comparison, setComparison] = useState<WellArchitectedComparison | null>(null);
  const [baselineId, setBaselineId] = useState('');
  const [bindingId, setBindingId] = useState('');
  const [working, setBusy] = useState(false);
  const [bindingCheckBusy, setBindingCheckBusy] = useState(false);
  const busy = working || bindingCheckBusy;
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [check, setCheck] = useState<{ result: { ready: boolean; gaps: string[] };
    configuration: Versioned<WellArchitectedConfiguration>; binding?: SecurityBinding } | null>(null);
  const checkGeneration = useRef(0);
  const invalidateCheck = useCallback(() => { checkGeneration.current += 1; setCheck(null); }, []);
  const scoped = useCallback((item: { scope: { tenant_id: string; project_id: string; product_id: string; relationship_revision: string } }) =>
    item.scope.tenant_id === tenantId && item.scope.product_id === productId && item.scope.project_id === projectId
    && item.scope.relationship_revision === configuration.value.scope.relationship_revision,
  [tenantId, productId, projectId, configuration.value.scope.relationship_revision]);
  const deny = useCallback(() => {
    invalidateCheck(); invalidateReadback(); setBusy(false); setBindingsReadAt(null);
    setBindings([]); setRuns([]); setResults([]); setOperation(null); setRun(null); setResult(null); setPublication(null); setComparison(null); setBaselineId('');
    onAccessDenied();
  }, [onAccessDenied, invalidateCheck, invalidateReadback]);
  const handleError = useCallback((failure: unknown) => {
    if (statusOf(failure) === 403 || statusOf(failure) === 404) { deny(); return; }
    setError(`Review request failed${typeof statusOf(failure) === 'number' ? ` (HTTP ${statusOf(failure)})` : ''}; no success is implied.`);
  }, [deny]);

  const load = useCallback(async () => {
    invalidateCheck(); invalidateReadback();
    setBindingsReadAt(null);
    const request = readGeneration.current;
    setBindings([]); setRuns([]); setResults([]);
    setBusy(true); setError(''); setRun(null); setResult(null); setPublication(null); setComparison(null); setBaselineId('');
    try {
      const bindingPage = await listWellArchitectedBindings(productId, projectId);
      if (request !== readGeneration.current) return;
      const runPage = await listWellArchitectedRuns(productId, projectId);
      if (request !== readGeneration.current) return;
      if ([...bindingPage.items, ...runPage.items].some(item => !scoped(item))) { deny(); return; }
      if (!runPage.items.length) {
        setBindings(bindingPage.items); setBindingsReadAt(configuration); setRunCursor(runPage.next_cursor); return request;
      }
      const resultPage = await listWellArchitectedResults(productId, projectId);
      if (request !== readGeneration.current) return;
      if (resultPage.items.some(item => !scoped(item) || (item.service_id && item.service_id !== 'well_architected'))) { deny(); return; }
      setBindings(bindingPage.items); setBindingsReadAt(configuration); setRuns(runPage.items); setResults(resultPage.items);
      setRunCursor(runPage.next_cursor); setResultCursor(resultPage.next_cursor);
      return request;
    } catch (failure) { if (request === readGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current) setBusy(false); }
  }, [productId, projectId, scoped, deny, handleError, invalidateCheck, invalidateReadback, configuration]);
  useEffect(() => { void load(); return () => { checkGeneration.current += 1; readGeneration.current += 1; }; }, [load, configuration]);
  const refreshBindings = useCallback(async () => { await load(); }, [load]);

  async function loadMore(kind: keyof typeof paging) {
    const cursor = kind === 'runs' ? runCursor : resultCursor;
    if (!cursor || pageRequests.current[kind]) return;
    const request = readGeneration.current;
    const token = Symbol(kind); pageRequests.current[kind] = token;
    setPaging(current => ({ ...current, [kind]: true })); setError('');
    try {
      if (kind === 'runs') {
        const page = await listWellArchitectedRuns(productId, projectId, cursor);
        if (request !== readGeneration.current || pageRequests.current[kind] !== token) return;
        if (page.items.some(item => !scoped(item))) { deny(); return; }
        setRuns(current => appendReadbackPage(current, page.items, item => item.run_id)); setRunCursor(page.next_cursor);
      } else {
        const page = await listWellArchitectedResults(productId, projectId, cursor);
        if (request !== readGeneration.current || pageRequests.current[kind] !== token) return;
        if (page.items.some(item => !scoped(item) || (item.service_id && item.service_id !== 'well_architected'))) { deny(); return; }
        setResults(current => appendReadbackPage(current, page.items, item => item.result_id)); setResultCursor(page.next_cursor);
      }
    } catch (failure) {
      if (request === readGeneration.current && pageRequests.current[kind] === token) handleError(failure);
    } finally {
      if (pageRequests.current[kind] === token) {
        delete pageRequests.current[kind]; setPaging(current => ({ ...current, [kind]: false }));
      }
    }
  }

  const selectedBinding = bindings.find(item => item.binding_id === bindingId && item.state === 'ready' && !item.gaps?.length
    && item.configuration_id === configuration.value.configuration_id && scoped(item));
  const currentCheck = check?.configuration === configuration && check.binding === selectedBinding ? check.result : null;
  const canStart = Boolean(!busy && eligible && configuration.value.settings.enabled && selectedBinding && currentCheck?.ready && !currentCheck.gaps.length);

  async function checkReadiness() {
    invalidateCheck();
    const request = checkGeneration.current;
    setBusy(true); setError('');
    try {
      const result = await checkWellArchitectedConfiguration(productId, projectId);
      if (request === checkGeneration.current) setCheck({ result, configuration, binding: selectedBinding });
    } catch (failure) { if (request === checkGeneration.current) handleError(failure); }
    finally { if (request === checkGeneration.current) setBusy(false); }
  }

  async function setup() {
    invalidateCheck();
    const request = readGeneration.current;
    setBusy(true); setError(''); setMessage('');
    try {
      const receipt = await startWellArchitectedBinding(productId, projectId, configuration.value.revision);
      if (request !== readGeneration.current) return;
      setOperation(receipt.value);
      setMessage(`Setup ${receipt.value.state}; operation ${receipt.value.operation_id}. This is not proof that a native profile is ready.`);
    } catch (failure) { if (request === readGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current) setBusy(false); }
  }
  async function inspectOperation() {
    if (!operation) return;
    const request = readGeneration.current;
    setBusy(true); setError('');
    try {
      const current = await getWellArchitectedBindingOperation(productId, projectId, operation.operation_id);
      if (request !== readGeneration.current) return;
      if (current.operation_id !== operation.operation_id) { deny(); return; }
      setOperation(current);
      await load();
    } catch (failure) { if (request === readGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current) setBusy(false); }
  }
  async function submit() {
    if (!canStart || !selectedBinding) return;
    invalidateCheck();
    const request = readGeneration.current;
    setBusy(true); setError(''); setMessage('');
    try {
      const receipt = await submitWellArchitectedRun(productId, projectId, selectedBinding.binding_id,
        configuration.value.revision, configuration.value.settings.source_refs);
      if (request !== readGeneration.current) return;
      if (!scoped(receipt.value)) { deny(); return; }
      const loaded = await load();
      if (loaded !== readGeneration.current) return;
      setRun(receipt); setMessage(`Run ${receipt.value.run_id} ${receipt.value.state}; intent accepted, not provider success.`);
    } catch (failure) { if (request === readGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current) setBusy(false); }
  }
  async function inspectRun(runId: string) {
    const request = readGeneration.current;
    const inspection = ++inspectionGeneration.current;
    setBusy(true); setError('');
    try {
      const read = await getWellArchitectedRun(productId, projectId, runId);
      if (request !== readGeneration.current || inspection !== inspectionGeneration.current) return;
      if (!scoped(read.value) || read.value.run_id !== runId) { deny(); return; }
      setRun(read);
      setRuns(current => appendReadbackPage(current, [read.value], item => item.run_id));
    } catch (failure) { if (request === readGeneration.current && inspection === inspectionGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current && inspection === inspectionGeneration.current) setBusy(false); }
  }
  async function inspectResult(resultId: string) {
    const request = readGeneration.current;
    const inspection = ++inspectionGeneration.current;
    setBusy(true); setError(''); setResult(null); setPublication(null); setComparison(null); setBaselineId('');
    try {
      const read = await getWellArchitectedResult(productId, projectId, resultId);
      if (request !== readGeneration.current || inspection !== inspectionGeneration.current) return;
      if (!scoped(read.value) || read.value.result_id !== resultId || (read.value.service_id && read.value.service_id !== 'well_architected')) { deny(); return; }
      setResult(read); setResults(current => appendReadbackPage(current, [read.value], item => item.result_id));
      try {
        const status = await getWellArchitectedPublicationStatus(productId, projectId, resultId);
        if (request !== readGeneration.current || inspection !== inspectionGeneration.current) return;
        if (status.result_id !== resultId || status.revision !== read.value.revision) { deny(); return; }
        setPublication(status);
      } catch (failure) {
        if (request !== readGeneration.current || inspection !== inspectionGeneration.current) return;
        if (statusOf(failure) === 403 || statusOf(failure) === 404) { deny(); return; }
        setError('Publication status is unavailable. The retained review is shown below. Reopen this result to retry publication readback; publication success is unconfirmed.');
      }
    } catch (failure) { if (request === readGeneration.current && inspection === inspectionGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current && inspection === inspectionGeneration.current) setBusy(false); }
  }


  async function compare() {
    if (!result || !baselineId || baselineId === result.value.result_id
      || !results.some(item => item.result_id === baselineId && scoped(item))) return;
    const request = readGeneration.current;
    const inspection = ++inspectionGeneration.current;
    setBusy(true); setError(''); setComparison(null);
    try {
      const read = await compareWellArchitectedResults(productId, projectId, baselineId, result.value.result_id);
      if (request !== readGeneration.current || inspection !== inspectionGeneration.current) return;
      if (read.baseline_result_id !== baselineId || read.candidate_result_id !== result.value.result_id) { deny(); return; }
      setComparison(read);
    } catch (failure) { if (request === readGeneration.current && inspection === inspectionGeneration.current) handleError(failure); }
    finally { if (request === readGeneration.current && inspection === inspectionGeneration.current) setBusy(false); }
  }

  return <div className="space-y-4">
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    <ServiceSection service="well_architected" step="setup" description="Review inherited context, pin review inputs, and inspect binding setup.">
    {setupContent}
    <h4>Review binding</h4>
    <button type="button" onClick={() => void setup()} disabled={busy || !eligible || bindings.length > 0}>Set up review binding</button>
    {operation && <p>Setup operation {operation.operation_id}: {operation.state}; gaps: {operation.gaps.join(', ') || 'none'}.
      <button type="button" onClick={() => void inspectOperation()} disabled={busy}>Read setup receipt</button></p>}
    <p>Bindings: {bindings.map(item => `${item.binding_id}: ${item.state}`).join(', ') || 'none ready'}</p>
    <WellArchitectedBindingCheck productId={productId} projectId={projectId} configuration={configuration}
      bindings={bindings} disabled={working || !eligible || bindingsReadAt !== configuration}
      onInvalidate={invalidateCheck} onBusyChange={setBindingCheckBusy} onRefresh={refreshBindings} onAccessDenied={deny} />
    </ServiceSection>
    <ServiceSection service="well_architected" step="capabilities" description="Choose the pillars for a source-only architecture review.">
      {capabilitiesContent}
    </ServiceSection>
    <ServiceSection service="well_architected" step="runs" description="Check readiness, start a review, and follow its observed outcome.">
    {scheduleContent ? scheduleContent(bindings, bindingsReadAt === configuration) : <ScheduleAvailability service="well_architected" />}
    <button type="button" onClick={() => void load()} disabled={busy}>Refresh review status</button>
    <label>Ready binding <select value={bindingId} disabled={busy} onChange={event => { invalidateCheck(); setBindingId(event.target.value); }}>
      <option value="">Choose a ready binding</option>{bindings.filter(item => item.state === 'ready' && !item.gaps?.length && item.configuration_id === configuration.value.configuration_id)
        .map(item => <option key={item.binding_id} value={item.binding_id}>{item.binding_id}</option>)}
    </select></label>
    <button type="button" onClick={() => void checkReadiness()} disabled={busy}>Check review readiness</button>
    {currentCheck && <p role="status">Readiness: {currentCheck.ready ? 'ready' : 'not ready'}; gaps: {currentCheck.gaps.join(', ') || 'none'}</p>}
    <p>Select a binding and check current readiness before starting. The server verifies the saved setup and owner consent again.</p>
    <button type="button" onClick={() => void submit()} disabled={!canStart}>Start source-only review</button>
    <h4>Review runs</h4>{runs.map(item => <button key={item.run_id} type="button" onClick={() => void inspectRun(item.run_id)} disabled={busy}>
      {item.run_id}: {item.state} ({item.provider_state})
    </button>)}
    {runCursor && <button type="button" disabled={paging.runs} onClick={() => void loadMore('runs')}>Load more review runs</button>}
    {run && <div>
      <p>Run revision {run.value.revision}: {run.value.state}; provider: {run.value.provider_state}; monitoring: {run.value.monitoring?.state || 'unobserved'}.</p>
      {(['expired', 'cancelled', 'cancel_requested', 'outcome_unknown'].includes(run.value.state) || run.value.provider_state === 'unknown')
        && run.value.provider_state !== 'cancelled' && <p>Local state does not confirm AWS cancellation. Native outcome needs provider readback.</p>}
      {['overdue', 'degraded'].includes(run.value.monitoring?.state || '') && <p>Monitoring is stale or degraded; last successful check: {run.value.monitoring?.last_successful_poll_at || 'none'}. This is not a current provider observation.</p>}
      {run.value.monitoring?.last_poll_error && <p>Last provider check error: {run.value.monitoring.last_poll_error.code}; cancellation is not confirmed.</p>}
    </div>}
    {!runs.length && <p>No review runs are visible.</p>}
    </ServiceSection>
    <ServiceSection service="well_architected" step="results" description="Read recommendations, compare retained reviews, and inspect publication receipts.">
    <h4>Review results</h4>{results.map(item => <button key={item.result_id} type="button" onClick={() => void inspectResult(item.result_id)} disabled={busy}>
      {item.result_id}: coverage {Array.isArray(item.coverage) ? `${item.coverage.length} selected files` : item.coverage}
    </button>)}
    {resultCursor && <button type="button" disabled={paging.results} onClick={() => void loadMore('results')}>Load more review results</button>}
    {result && <div>
      <p>Result revision {result.value.revision}; coverage: {Array.isArray(result.value.coverage) ? `${result.value.coverage.length} selected source files` : result.value.coverage}; mode: {result.value.review_mode || result.value.coverage_mode || 'not reported'};
        pillars: {result.value.pillars?.join(', ') || 'not reported'}; limitations: {result.value.limitations?.join(', ') || 'none reported'}. Source-only evidence is not deployment verification.</p>
      <p>Independent source comparison: {result.value.recommendations?.map(item => item.comparison?.status || 'not_checked').join(', ') || result.value.comparison_state || 'not reported'}.</p>
      <ul>{result.value.recommendations?.map((item, index) => <li key={item.native_id ?? index}>
        <h5>{item.title || 'Untitled recommendation'}</h5>
        <p className="whitespace-pre-wrap">{item.description || 'No description reported.'}</p>
        <p>Pillar: {item.pillar || 'not reported'}.</p>
        <p>Native recommendation: {item.native_id || 'not reported'}; generation: {item.generation_id || 'not reported'}.</p>
        <p>Source comparison: {item.comparison?.status || 'not_checked'}{item.comparison?.explanation && `; ${item.comparison.explanation}`}.</p>
      </li>)}</ul>
      <label>Baseline review result <select value={baselineId} disabled={busy} onChange={event => { setBaselineId(event.target.value); setComparison(null); }}>
        <option value="">Choose a baseline</option>{results.filter(item => item.result_id !== result.value.result_id)
          .map(item => <option key={item.result_id} value={item.result_id}>{item.result_id}</option>)}
      </select></label>
      <button type="button" onClick={() => void compare()} disabled={busy || !baselineId}>Compare review results</button>
      {comparison && <p>Result comparison: {comparison.comparability}; reasons: {comparison.reasons.join(', ') || 'none'}.</p>}
      {publication && <><p>Publication debt: {publication.consumers.filter(item => !item.projection_verified_on_ack)
        .map(item => `${item.destination}: ${item.state}${item.last_error ? ` (${item.last_error})` : ''}`).join('; ') || 'none reported'}.</p>
      {publication.consumers.some(item => item.last_error === 'consumer_schema_unadmitted') && <p>The listed destination has not accepted the Well-Architected result schema. Ask the platform administrator to qualify that destination and verify its publication repair worker, then refresh this result for a verified publication receipt. The retained review remains available here.</p>}
      <p>Publication receipts: {publication.consumers.filter(item => item.projection_verified_on_ack)
        .map(item => `${item.destination}: ${item.receipt_id}`).join('; ') || 'none verified'}.</p></>}
    </div>}
    {!results.length && <p>No retained review results are visible.</p>}
    </ServiceSection>
  </div>;
}
