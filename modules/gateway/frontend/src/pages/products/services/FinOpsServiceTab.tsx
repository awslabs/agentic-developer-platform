import L5OwnerConsent from './L5OwnerConsent';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { productReceipt, type RecordReference } from '@/services/products';
import {
  getFinOpsBindingOperation, getFinOpsConfiguration, getFinOpsOptions, getFinOpsPublication, getFinOpsResult,
  getFinOpsRun, listFinOpsBindings, listFinOpsResults, listFinOpsRuns, matchesSecurityScope,
  saveFinOpsConfiguration, startFinOpsBinding, submitFinOpsRun, getFinOpsNativeAnswer,
  type FinOpsBinding, type FinOpsConfiguration, type FinOpsResult, type FinOpsRun, type SecurityBindingOperation,
  type SecurityPublication, type Versioned,
} from '@/services/l5Services';

import { ServiceJourney, ServiceSection, ServiceDisclosure, ProfileSetupSummary, CapabilitySummary, ScheduleAvailability } from './L5ServiceJourney';
import FinOpsSetupFields from './FinOpsSetupFields';
import { checkFinOpsConfiguration, finOpsBindingReady, finOpsReadinessAction } from '@/services/finopsReadiness';

interface Props { productId: string; projectId: string; tenantId: string }
const statusOf = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
const errorCode = (error: unknown) => {
  const detail = (error && typeof error === 'object' ? error : {}) as { detail?: { code?: unknown } };
  return typeof detail.detail?.code === 'string' ? detail.detail.code : undefined;
};
const errorDetail = (error: unknown) => {
  const status = statusOf(error);
  if (typeof status !== 'number' || status < 400 || status > 599) return '';
  const detail = (error as { detail?: unknown }).detail;
  const code = typeof detail === 'object' && detail !== null && 'code' in detail ? detail.code : undefined;
  return ` (HTTP ${status}${typeof code === 'string' && /^[a-z][a-z0-9_]{0,127}$/.test(code) ? ` ${code}` : ''})`;
};
const scoped = (scope: { tenant_id: string; project_id: string }, tenantId: string, projectId: string) =>
  scope.tenant_id === tenantId && scope.project_id === projectId;
const editableChoices = (configuration: FinOpsConfiguration) =>
  Object.fromEntries(Object.entries(configuration.settings).filter(([field]) =>
    field !== 'profile_ref' && !configuration.inherited_fields?.includes(field)));

export default function FinOpsServiceTab({ productId, projectId, tenantId }: Props) {
  const [configuration, setConfiguration] = useState<Versioned<FinOpsConfiguration> | null>(null);
  const [choices, setChoices] = useState('{"service_id":"finops","capabilities":["cost_inquiry"],"enabled":false,"finops":{"agent_mode":"adopt"}}');
  const [unavailable, setUnavailable] = useState<string[]>([]);
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [admissionsEnabled, setAdmissionsEnabled] = useState<boolean | undefined>();
  const [connections, setConnections] = useState<RecordReference[]>([]);
  const [regions, setRegions] = useState<string[]>([]);
  const [bindings, setBindings] = useState<FinOpsBinding[]>([]);
  const [operation, setOperation] = useState<SecurityBindingOperation | null>(null);
  const [runs, setRuns] = useState<FinOpsRun[]>([]);
  const [runCursor, setRunCursor] = useState<string | undefined>();
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [run, setRun] = useState<Versioned<FinOpsRun> | null>(null);
  const [results, setResults] = useState<FinOpsResult[]>([]);
  const [resultCursor, setResultCursor] = useState<string | undefined>();
  const [loadingResults, setLoadingResults] = useState(false);
  const [result, setResult] = useState<Versioned<FinOpsResult> | null>(null);
  const [nativeAnswer, setNativeAnswer] = useState<string | null>(null);
  const [publication, setPublication] = useState<SecurityPublication | null>(null);
  const [bindingId, setBindingId] = useState('');
  const [nativeReference, setNativeReference] = useState('');
  const [question, setQuestion] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [granularity, setGranularity] = useState<'DAILY' | 'MONTHLY'>('MONTHLY');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const [stale, setStale] = useState(false);
  const [pendingReceipt, setPendingReceipt] = useState('');
  const [refreshing, setRefreshing] = useState(true);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<{ result: { ready: boolean; gaps: string[] }; scope: string;
    configuration: Versioned<FinOpsConfiguration>; binding?: FinOpsBinding } | null>(null);
  const checkGeneration = useRef(0);
  const currentScope = `${tenantId}:${productId}:${projectId}`;
  const scopeRef = useRef(currentScope);
  scopeRef.current = currentScope;
  const invalidateCheck = useCallback(() => { checkGeneration.current += 1; setCheck(null); setChecking(false); }, []);
  const generation = useRef(0);
  const resultRead = useRef(0);
  const hadConfiguration = useRef(false);

  const clear = useCallback(() => {
    invalidateCheck();
    setRefreshing(false);
    generation.current++;
    resultRead.current++;
    setConfiguration(null); setBindings([]); setOperation(null); setRuns([]); setRun(null);
    setResults([]); setResult(null); setNativeAnswer(null); setPublication(null); setChoices(''); setUnavailable([]); setCapabilities([]); setConnections([]); setRegions([]);
    setAdmissionsEnabled(undefined);
    setRunCursor(undefined); setResultCursor(undefined); setLoadingRuns(false); setLoadingResults(false);
  }, [invalidateCheck]);

  const refresh = useCallback(async () => {
    invalidateCheck(); setRefreshing(true);
    const request = ++generation.current;
    resultRead.current++;
    setRunCursor(undefined); setResultCursor(undefined); setLoadingRuns(false); setLoadingResults(false);
    try {
      const options = await getFinOpsOptions(productId, projectId);
      let current: Versioned<FinOpsConfiguration> | null = null;
      try {
        current = await getFinOpsConfiguration(productId, projectId);
      } catch (error) {
        if (statusOf(error) !== 404 || hadConfiguration.current) throw error;
      }
      if (current && (!matchesSecurityScope(current.value.scope, current.value.scope, productId, projectId)
        || current.value.scope.tenant_id !== tenantId || current.value.settings.service_id !== 'finops')) throw { status: 403 };
      const [bindingPage, runPage, resultPage] = current ? await Promise.all([
        listFinOpsBindings(productId, projectId), listFinOpsRuns(productId, projectId), listFinOpsResults(productId, projectId),
      ]) : [{ items: [] }, { items: [] }, { items: [] }];
      if ([...bindingPage.items, ...runPage.items, ...resultPage.items].some(item =>
        !scoped(item.scope, tenantId, projectId) || !matchesSecurityScope(item.scope, current?.value.scope, productId, projectId))) throw { status: 403 };
      if (request !== generation.current) return;
      hadConfiguration.current ||= Boolean(current);
      setCapabilities(options.capabilities ?? []);
      setAdmissionsEnabled(options.admissions_enabled);
      setUnavailable(options.unavailable_reasons); setConnections(options.connections ?? []); setRegions(options.regions ?? []);
      setConfiguration(current);
      if (current) setChoices(JSON.stringify(editableChoices(current.value), null, 2));
      setBindings(bindingPage.items); setRuns(runPage.items); setResults(resultPage.items);
      setRunCursor(runPage.next_cursor); setResultCursor(resultPage.next_cursor);
      setOperation(null); setRun(null); setResult(null); setNativeAnswer(null); setPublication(null);
      setBindingId(previous => bindingPage.items.some(item => item.binding_id === previous) ? previous : bindingPage.items[0]?.binding_id ?? '');
      setRevoked(false); setStale(false); setMessage('');
    } catch (error) {
      if (request !== generation.current) return;
      clear();
      if (statusOf(error) === 403 || statusOf(error) === 404) setRevoked(true);
      setMessage(statusOf(error) === 403 || statusOf(error) === 404
        ? `FinOps access is unavailable for this project. Previously shown details were removed.${errorDetail(error)}`
        : `FinOps state is unavailable. Refresh to retry the read.${errorDetail(error)}`);
    } finally { if (request === generation.current) setRefreshing(false); }
  }, [productId, projectId, tenantId, clear, invalidateCheck]);

  useEffect(() => { void refresh(); return () => { generation.current += 1; }; }, [refresh]);

  const selectedBinding = configuration ? bindings.find(item => item.binding_id === bindingId && finOpsBindingReady(item, configuration.value)) : undefined;
  const currentCheck = check?.configuration === configuration && check?.binding === selectedBinding && check?.scope === currentScope ? check?.result : null;
  const canSubmit = Boolean(admissionsEnabled === true && !busy && !checking && !refreshing && !stale && !revoked && configuration?.value.settings.enabled
    && selectedBinding && currentCheck?.ready && !currentCheck.gaps.length);

  async function checkReadiness() {
    if (!configuration || busy || refreshing || stale || revoked) return;
    invalidateCheck();
    const request = checkGeneration.current;
    setChecking(true); setMessage('');
    try {
      const result = await checkFinOpsConfiguration(productId, projectId);
      if (request !== checkGeneration.current || scopeRef.current !== currentScope) return;
      if (result.gaps.includes('finops_admissions_disabled')) setAdmissionsEnabled(false);
      setCheck({ result, configuration, binding: selectedBinding, scope: currentScope });
    } catch (error) {
      if (request !== checkGeneration.current || scopeRef.current !== currentScope) return;
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`FinOps readiness could not be confirmed. Refresh before trying again.${errorDetail(error)}`);
    } finally { if (request === checkGeneration.current) setChecking(false); }
  }

  async function loadMoreRuns() {
    if (!runCursor || loadingRuns || revoked) return;
    const request = generation.current;
    setLoadingRuns(true);
    try {
      const page = await listFinOpsRuns(productId, projectId, runCursor);
      if (request !== generation.current) return;
      if (page.items.some(item => !scoped(item.scope, tenantId, projectId)
        || !matchesSecurityScope(item.scope, configuration?.value.scope, productId, projectId))) throw { status: 403 };
      setRuns(current => [...current, ...page.items]); setRunCursor(page.next_cursor);
    } catch (error) {
      if (request !== generation.current) return;
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`More FinOps inquiries could not be read.${errorDetail(error)}`);
    } finally { if (request === generation.current) setLoadingRuns(false); }
  }

  async function loadMoreResults() {
    if (!resultCursor || loadingResults || revoked) return;
    const request = generation.current;
    setLoadingResults(true);
    try {
      const page = await listFinOpsResults(productId, projectId, resultCursor);
      if (request !== generation.current) return;
      if (page.items.some(item => !scoped(item.scope, tenantId, projectId)
        || !matchesSecurityScope(item.scope, configuration?.value.scope, productId, projectId))) throw { status: 403 };
      setResults(current => [...current, ...page.items]); setResultCursor(page.next_cursor);
    } catch (error) {
      if (request !== generation.current) return;
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`More FinOps results could not be read.${errorDetail(error)}`);
    } finally { if (request === generation.current) setLoadingResults(false); }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    let input: Record<string, unknown>;
    try {
      input = JSON.parse(choices);
      if (!input || Array.isArray(input) || input.service_id !== 'finops' || typeof input.finops !== 'object') throw new Error();
    } catch {
      setMessage('Enter a FinOps configuration JSON object with explicit scoped choices.'); return;
    }
    invalidateCheck(); setBusy(true); setMessage('');
    try {
      const saved = await saveFinOpsConfiguration(productId, projectId, input, configuration?.etag);
      if (!scoped(saved.value.scope, tenantId, projectId) || saved.value.scope.product_id !== productId) throw { status: 403 };
      hadConfiguration.current = true;
      setConfiguration(saved);
      setChoices(JSON.stringify(editableChoices(saved.value), null, 2));
      setMessage('Configuration saved. Setup and inquiry are separate actions.');
    } catch (error) {
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      else if (statusOf(error) === 412) setStale(true);
      setMessage(statusOf(error) === 412 ? `Configuration changed elsewhere. Refresh before editing; changes were not replayed.${errorDetail(error)}`
        : `Configuration was not confirmed. Refresh before attempting another write.${errorDetail(error)}`);
    } finally { setBusy(false); }
  }

  async function setup() {
    if (!configuration) return;
    const mode = configuration.value.settings.finops.agent_mode;
    let native_resource_ref: RecordReference | undefined;
    if (mode === 'adopt') {
      try {
        native_resource_ref = JSON.parse(nativeReference) as RecordReference;
        if (!native_resource_ref || !scoped(native_resource_ref, tenantId, projectId)
          || native_resource_ref.record_type !== 'finops_agent') throw new Error();
      } catch { setMessage('Adoption needs an explicit revisioned FinOps agent reference for this project.'); return; }
    } else { setMessage('Creating a FinOps agent is not available yet. Save a setup that uses an existing agent.'); return; }
    invalidateCheck(); setBusy(true);
    try {
      const accepted = await startFinOpsBinding(productId, projectId, {
        action: 'link', configuration_revision: configuration.value.revision,
        ...(native_resource_ref ? { native_resource_ref } : {}),
      });
      setOperation(accepted.value);
      setMessage(`Setup ${accepted.value.state}; operation ${accepted.value.operation_id}. Acceptance is not completion.`);
    } catch (error) {
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`Setup outcome is not confirmed. Refresh or read its original operation; do not start another setup automatically.${errorDetail(error)}`);
    } finally { setBusy(false); }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    if (!configuration || !bindingId || !question.trim() || !start || !end || start >= end) {
      setMessage('Select a ready binding, a question and a closed date range.'); return;
    }
    invalidateCheck();
    const receipt = productReceipt();
    setPendingReceipt(receipt); setBusy(true); setMessage('');
    try {
      const accepted = await submitFinOpsRun(productId, projectId, {
        service_id: 'finops', capability: 'cost_inquiry', configuration_revision: configuration.value.revision,
        binding_id: bindingId, question: question.trim(),
        query: { start, end, metric: 'UnblendedCost', granularity, group_by: ['SERVICE'], currency: 'USD' },
      }, receipt);
      if (!scoped(accepted.value.scope, tenantId, projectId) || accepted.value.scope.product_id !== productId) throw { status: 403 };
      setRun(accepted);
      setMessage(`Inquiry ${accepted.value.state}; run ${accepted.value.run_id}. Accepted does not mean answered.`);
    } catch (error) {
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      const code = errorCode(error);
      if (code === 'finops_admissions_disabled') setAdmissionsEnabled(false);
      const action = code && ['finops_admissions_disabled', 'native_unqualified', 'work_queue_unavailable', 'finops_authority_unavailable'].includes(code)
        ? `${finOpsReadinessAction(code)} ` : '';
      setMessage(`${action}Inquiry outcome is not confirmed. Keep the receipt; check existing runs before any explicit retry.${errorDetail(error)}`);
    } finally { setBusy(false); }
  }

  async function readRun(runId: string) {
    try {
      const current = await getFinOpsRun(productId, projectId, runId);
      if (!scoped(current.value.scope, tenantId, projectId) || current.value.scope.product_id !== productId) throw { status: 403 };
      setRun(current);
    } catch (error) {
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`FinOps run status is unavailable.${errorDetail(error)} ${statusOf(error) === 403 || statusOf(error) === 404
        ? 'Previously shown details were removed.' : 'The previous status is not a fresh read.'}`);
    }
  }

  async function readOperation(operationId: string) {
    invalidateCheck();
    try { setOperation(await getFinOpsBindingOperation(productId, projectId, operationId)); }
    catch (error) {
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`Setup status could not be read; no setup action was repeated.${errorDetail(error)}`);
    }
  }

  async function readResult(resultId: string) {
    const request = ++resultRead.current;
    setNativeAnswer(null); setResult(null);
    try {
      const current = await getFinOpsResult(productId, projectId, resultId);
      if (request !== resultRead.current) return;
      if (!scoped(current.value.scope, tenantId, projectId) || current.value.scope.product_id !== productId) throw { status: 403 };
      if (current.value.native.answer_artifact && ['answered', 'refused'].includes(current.value.native.status)) {
        const answer = await getFinOpsNativeAnswer(productId, projectId, resultId, current.value.revision);
        if (request !== resultRead.current) return;
        if (answer.result_id !== resultId || answer.run_id !== current.value.run_id
          || answer.revision !== current.value.revision
          || JSON.stringify(answer.artifact) !== JSON.stringify(current.value.native.answer_artifact)
          || !answer.text?.trim()) throw { status: 409 };
        setNativeAnswer(answer.text);
      }
      setResult(current); setPublication(null);
      if (current.value.publication_id) {
        try {
          const currentPublication = await getFinOpsPublication(productId, projectId, current.value.publication_id);
          if (request === resultRead.current) setPublication(currentPublication);
        } catch (error) {
          if (statusOf(error) === 403 || statusOf(error) === 404) throw error;
          if (request === resultRead.current) setMessage(`Publication status is unavailable; the original answer and evidence remain unchanged.${errorDetail(error)}`);
        }
      }
    } catch (error) {
      if (request !== resultRead.current) return;
      if (statusOf(error) === 403 || statusOf(error) === 404) { clear(); setRevoked(true); }
      setMessage(`FinOps result status is unavailable.${errorDetail(error)} ${statusOf(error) === 403 || statusOf(error) === 404
        ? 'Previously shown details were removed.' : 'The previous answer is not a fresh read.'}`);
    }
  }

  return <ServiceJourney service="finops"><section aria-label="FinOps service" className="space-y-4">
    <h3>FinOps cost inquiries</h3>
    <p>Ask cost questions about the billing accounts selected for this Product.</p>
    {message && <p role="alert">{message}</p>}
    <button type="button" onClick={() => void refresh()}>Refresh FinOps state</button>
    {revoked ? <p>FinOps details are unavailable for this scope.</p> : <>
      <ServiceSection service="finops" step="setup">
      <ProfileSetupSummary configuration={configuration?.value} productId={productId} projectId={projectId} />
      {unavailable.length > 0 && <p>Readiness: {unavailable.join(', ')}</p>}

      <ServiceDisclosure title="Edit FinOps setup" hint="Review or change the service settings" open={!configuration}>
      <form onSubmit={event => void save(event)}>
        <FinOpsSetupFields value={choices} onChange={setChoices} connections={connections} regions={regions} />
        <details><summary>Advanced settings</summary>
        <label htmlFor="finops-choices">FinOps configuration choices (JSON)</label>
        <textarea id="finops-choices" value={choices} onChange={event => setChoices(event.target.value)} rows={8} />
        <p>These are the same draft values as the setup fields above, including retained execution limits and policies.</p></details>
        <button disabled={busy || stale} type="submit">Save FinOps configuration</button>
      </form>
      </ServiceDisclosure>
      {configuration && <ServiceDisclosure title="Provider connection" hint="Connect an existing FinOps agent">
        <p>Configuration {configuration.value.state}; revision {configuration.value.revision}; ETag {configuration.etag}; setup {configuration.value.handoff_state}.</p>
        <p>Gaps: {configuration.value.gaps.join(', ') || 'none reported'}</p>
        {configuration.value.settings.finops.agent_mode === 'adopt' && <label>Revisioned scoped FinOps agent reference (JSON)
          <textarea value={nativeReference} onChange={event => setNativeReference(event.target.value)} />
        </label>}
        <button type="button" disabled={busy || stale || configuration.value.settings.finops.agent_mode !== 'adopt'} onClick={() => void setup()}>Start FinOps setup</button>
        {operation && <p>Setup {operation.state}; operation {operation.operation_id}; gaps {operation.gaps.join(', ') || 'none reported'}.
          <button type="button" onClick={() => void readOperation(operation.operation_id)}>Read setup status</button>
        </p>}
        {configuration.value.settings.finops.agent_mode !== 'adopt' && <p>Creating a new FinOps agent: Not available yet. Select an existing agent in Setup and save the configuration.</p>}
      </ServiceDisclosure>}
      <L5OwnerConsent service="finops" productId={productId} projectId={projectId} configuration={configuration?.value ?? null} disabled={busy || stale || refreshing || revoked} />
      </ServiceSection>
      <ServiceSection service="finops" step="capabilities">
        <CapabilitySummary service="finops" capabilities={capabilities} selected={Array.isArray(configuration?.value.settings.capabilities) ? configuration.value.settings.capabilities as string[] : []}
          unavailable={admissionsEnabled === true ? [] : ['cost_inquiry']} />
        <p>Cost inquiries require qualified billing access. Capability selection alone does not establish provider readiness.</p>
        {admissionsEnabled === true ? <p>Inquiry admission is enabled in this environment. Check current readiness before submitting; this does not confirm that the native executor is qualified.</p>
          : <p>{admissionsEnabled === false ? finOpsReadinessAction('finops_admissions_disabled')
            : 'Cost inquiries: Not available yet. This environment has not reported inquiry availability. Refresh FinOps state; if availability remains unreported, ask the platform administrator to complete the Gateway update.'}</p>}
      </ServiceSection>
      <ServiceSection service="finops" step="runs">
        <ScheduleAvailability service="finops" />
        {!configuration && <p>Complete and save Setup before starting an inquiry.</p>}
        {configuration && <>
        <form onSubmit={event => void submit(event)}>
          <label>Ready binding <select value={bindingId} disabled={busy || checking || refreshing} onChange={event => { invalidateCheck(); setBindingId(event.target.value); }}>
            <option value="">Choose binding</option>
            {bindings.filter(item => finOpsBindingReady(item, configuration.value)).map(item => <option key={item.binding_id} value={item.binding_id}>{item.binding_id} — {item.state}</option>)}
          </select></label>
          <button type="button" disabled={busy || checking || refreshing || stale} onClick={() => void checkReadiness()}>Check FinOps readiness</button>
          {currentCheck && <div role="status"><p>FinOps readiness: {admissionsEnabled === true && currentCheck.ready && !currentCheck.gaps.length ? 'ready' : 'not ready'}; gaps: {currentCheck.gaps.join(', ') || 'none'}.</p>
            {currentCheck.gaps.map(code => <p key={code}>{finOpsReadinessAction(code)} <small>({code})</small></p>)}
            {admissionsEnabled !== true && <p>A saved binding or readiness response does not enable inquiries while this environment's inquiry availability is off or unreported.</p>}
          </div>}
          <p>Select a matching billing binding and check current readiness before submitting. The server verifies billing scope and owner consent again.</p>
          {!configuration.value.settings.enabled && <p>This configuration is disabled. Its owner must enable the saved configuration and authorize execution before an inquiry can run.</p>}
          <label>Original question <textarea value={question} onChange={event => setQuestion(event.target.value)} /></label>
          <label>Start (inclusive) <input type="date" value={start} onChange={event => setStart(event.target.value)} /></label>
          <label>End (exclusive) <input type="date" value={end} onChange={event => setEnd(event.target.value)} /></label>
          <label>Granularity <select value={granularity} onChange={event => setGranularity(event.target.value as 'DAILY' | 'MONTHLY')}>
            <option value="MONTHLY">Monthly</option><option value="DAILY">Daily</option>
          </select></label>
          <button type="submit" disabled={!canSubmit}>Submit cost inquiry</button>
        </form>
        {pendingReceipt && <p>Original inquiry receipt: {pendingReceipt}. Never resubmit automatically.</p>}
        <h4>Inquiries</h4>
        {runs.map(item => <button type="button" key={item.run_id} onClick={() => void readRun(item.run_id)}>{item.run_id}: {item.state}</button>)}
        {runCursor && <button type="button" disabled={loadingRuns} onClick={() => void loadMoreRuns()}>Load more inquiries</button>}
        {run && <p>Run {run.value.run_id}: {run.value.state}; ETag {run.etag}. Read-only refresh does not submit another inquiry.
          <button type="button" onClick={() => void readRun(run.value.run_id)}>Read run status</button>
        </p>}
        </>}
      </ServiceSection>
      <ServiceSection service="finops" step="results">
        {!results.length && <p>No results are available yet. Outputs will appear here after a completed inquiry.</p>}
        {configuration && <>
        {results.map(item => <button type="button" key={item.result_id} onClick={() => void readResult(item.result_id)}>{item.result_id}: {item.native.status}; billing {item.oracle.measure.coverage}</button>)}
        {resultCursor && <button type="button" disabled={loadingResults} onClick={() => void loadMoreResults()}>Load more results</button>}
        {result && <div aria-label="FinOps answer and evidence">
          <p>Result {result.value.result_id}; native {result.value.native.status}; billing {result.value.oracle.measure.coverage}; attribution {result.value.attribution}; ETag {result.etag}.</p>
          <h3>Answer</h3>
          <pre>{nativeAnswer ?? 'Original answer content unavailable'}</pre>
          <dl className="product-summary-list">
            <div><dt>Billing period</dt><dd>{result.value.query.start} to {result.value.query.end} (end exclusive)</dd></div>
            <div><dt>Native reported cost</dt><dd>{result.value.native.measure.amount === null ? 'Not reported' : `${result.value.native.measure.amount} ${result.value.native.measure.currency ?? ''}`} · {result.value.native.measure.coverage} coverage</dd></div>
            <div><dt>Independent billing cost</dt><dd>{result.value.oracle.measure.amount === null ? 'Not reported' : `${result.value.oracle.measure.amount} ${result.value.oracle.measure.currency ?? ''}`} · {result.value.oracle.measure.coverage} coverage</dd></div>
            <div><dt>Comparison</dt><dd>{result.value.comparison.state}{result.value.comparison.reason ? ` · ${result.value.comparison.reason.replace(/_/g, ' ')}` : ''}</dd></div>
          </dl>
          <details><summary>Evidence and technical details</summary>
          <h5>Original scoped inquiry</h5><pre>{JSON.stringify(result.value.query, null, 2)}</pre>
          <h5>Native answer receipt</h5><pre>{JSON.stringify(result.value.native, null, 2)}</pre>
          <h5>Independent billing evidence</h5><pre>{JSON.stringify(result.value.oracle, null, 2)}</pre>
          <h5>Attribution evidence</h5><pre>{JSON.stringify(result.value.attribution_evidence, null, 2)}</pre>
          <h5>Comparison</h5><pre>{JSON.stringify(result.value.comparison, null, 2)}</pre>
          </details>
          <p>Publication: {publication?.state ?? (result.value.publication_id ? 'status unavailable' : 'not reported')}</p>
        </div>}
        </>}
      </ServiceSection>
    </>}
  </section></ServiceJourney>;
}
