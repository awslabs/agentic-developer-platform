import RunAccess from './RunAccess';
import { useCallback, useContext, useEffect, useRef, useState } from 'react';
import { ProductSourceNameContext } from './useProductSourceLabels';
import { ServiceDisclosure, useRequestedCapability } from './L5ServiceJourney';
import { getSecurityRun, listSecurityBindings, listSecurityRuns, mutateSecurityRun, matchesSecurityScope, type SecurityBinding, type SecurityConfiguration, type SecurityRun, type SecurityRunInput, type Versioned } from '@/services/l5Services';
import { productReceipt, type RecordReference } from '@/services/products';
import { SecurityBadge, securityCapabilityName, securityDate } from './SecurityPresentation';
import { isSecurityCapability, securityCapabilityReady } from '@/services/securityCapabilityReadiness';
import { checkSecurityReadiness, securityReadinessAction, type SecurityCheck } from '@/services/securityReadiness';
import { useSecurityAdmission } from './useSecurityAdmission';

interface Props { productId: string; projectId: string; configuration: Versioned<SecurityConfiguration> | null; onAccessDenied?: () => void }
type Capability = SecurityRunInput['capability'];

const activeStates = new Set(['admitted', 'materializing', 'queued', 'submitting', 'running', 'collecting', 'cancel_requested', 'outcome_unknown']);
const followingRun = (run: SecurityRun) => activeStates.has(run.state) || run.monitoring?.state === 'recovery_pending';
const sourceType = (capability: Capability | '') => capability === 'threat_model' ? 'document' : 'repository_commit';
const referenceKey = (reference: RecordReference) => JSON.stringify([reference.tenant_id, reference.project_id, reference.source_connection_id, reference.record_type, reference.record_id, reference.record_revision]);

export default function SecurityRuns(props: Props) {
  const config = props.configuration?.value;
  return <SecurityAssessments {...props} key={JSON.stringify([config?.scope.tenant_id, props.productId, props.projectId, config?.configuration_id, config?.scope.owner_id])} />;
}

function SecurityAssessments({ productId, projectId, configuration, onAccessDenied }: Props) {
  const sourceName = useContext(ProductSourceNameContext);
  const requested = useRequestedCapability();
  const appliedRequest = useRef<string | null>(null);
  const defaultSelection = useRef('');
  const [runs, setRuns] = useState<SecurityRun[]>([]);
  const [bindings, setBindings] = useState<SecurityBinding[]>([]);
  const [bindingsReadAt, setBindingsReadAt] = useState<Versioned<SecurityConfiguration> | null>(null);
  const [check, setCheck] = useState<{ result: SecurityCheck; configuration: Versioned<SecurityConfiguration>; binding: SecurityBinding; capability: Capability; expiresAt: number } | null>(null);
  const [checking, setChecking] = useState(false);
  const checkGeneration = useRef(0);
  const loadGeneration = useRef(0);
  const invalidateCheck = useCallback(() => { checkGeneration.current += 1; setCheck(null); setChecking(false); }, []);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [detail, setDetail] = useState<Versioned<SecurityRun> | null>(null);
  const [capability, setCapability] = useState<Capability | ''>('');
  const [bindingId, setBindingId] = useState('');
  const [sourceKeys, setSourceKeys] = useState<string[]>([]);
  useEffect(() => {
    if (requested && isSecurityCapability(requested) && configuration?.value.settings.capabilities.includes(requested) && appliedRequest.current !== requested) {
      appliedRequest.current = requested;
      setCapability(requested); setBindingId(''); setSourceKeys([]);
    }
  }, [requested, configuration]);
  const [prBase, setPrBase] = useState('');
  const [prHead, setPrHead] = useState('');
  const [reason, setReason] = useState('');
  const [actionBusy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [progressError, setProgressError] = useState('');
  const revoked = useRef(false);
  const admission = useSecurityAdmission({ productId, projectId, configuration,
    selection: JSON.stringify([capability, bindingId, sourceKeys, prBase, prHead]),
    onAccepted: accepted => {
      if (revoked.current) return;
      setSelectedRunId(accepted.value.run_id); setDetail(accepted); setReason('');
      setRuns(current => [accepted.value, ...current.filter(run => run.run_id !== accepted.value.run_id)]);
    },
    onDenied: () => { revoked.current = true; setRuns([]); setBindings([]); setDetail(null); setError('Assessment access is unavailable. Refresh the workspace and review current owner consent.'); onAccessDenied?.(); },
  });
  const { pending } = admission;
  const busy = actionBusy || admission.busy;

  const inScope = (scope: SecurityRun['scope']) => matchesSecurityScope(scope, configuration?.value.scope, productId, projectId);
  const refresh = useCallback(async () => {
    const request = ++loadGeneration.current;
    invalidateCheck(); setBindingsReadAt(null);
    if (revoked.current) return;
    if (!configuration) { setRuns([]); setBindings([]); setDetail(null); admission.clear(); return; }
    try {
      const [page, choices] = await Promise.all([listSecurityRuns(productId, projectId), listSecurityBindings(productId, projectId)]);
      if (revoked.current || request !== loadGeneration.current) return;
      if (page.items.some(run => !matchesSecurityScope(run.scope, configuration.value.scope, productId, projectId))) throw { status: 403 };
      if (choices.items.some(binding => !matchesSecurityScope(binding.scope, configuration.value.scope, productId, projectId))) throw { status: 403 };
      let selected: SecurityRun | null = null;
      if (selectedRunId) {
        const current = await getSecurityRun(productId, projectId, selectedRunId);
        if (revoked.current || request !== loadGeneration.current) return;
        if (!matchesSecurityScope(current.value.scope, configuration.value.scope, productId, projectId)) throw { status: 403 };
        selected = current.value;
        setDetail(current);
      }
      // Direct readback can precede the list index after admission. Keep its
      // verified run visible and active while the history list catches up.
      setRuns(selected ? [selected, ...page.items.filter(run => run.run_id !== selected!.run_id)] : page.items);
      setNextCursor(page.next_cursor ?? null);
      setBindings(choices.items);
      setBindingsReadAt(configuration);
      setError('');
      setProgressError('');
    } catch (readError) {
      if (revoked.current || request !== loadGeneration.current) return;
      setRuns([]); setBindings([]); setDetail(null); setNextCursor(null); setSelectedRunId('');
      if (typeof readError === 'object' && readError !== null && 'status' in readError && (readError.status === 403 || readError.status === 404)) { revoked.current = true; admission.clear(); onAccessDenied?.(); }
      setError('Assessment details are unavailable for this workspace.');
    }
  }, [productId, projectId, configuration, selectedRunId, onAccessDenied, invalidateCheck]);

  useEffect(() => { void refresh(); return () => { loadGeneration.current += 1; checkGeneration.current += 1; }; }, [refresh]);
  const followingProgress = Boolean(configuration && !revoked.current && (runs.some(followingRun)
    || detail && followingRun(detail.value)));
  useEffect(() => {
    if (!followingProgress || !configuration || busy || pending) return;
    let stopped = false;
    let timer: number;
    async function updateProgress() {
      const generation = loadGeneration.current;
      try {
        // Read progress only. Background updates must not renew a binding,
        // invalidate a readiness check, or submit another assessment.
        const [page, current] = await Promise.all([
          listSecurityRuns(productId, projectId),
          selectedRunId ? getSecurityRun(productId, projectId, selectedRunId) : Promise.resolve(null),
        ]);
        if (stopped || revoked.current || generation !== loadGeneration.current) return;
        if (page.items.some(run => !matchesSecurityScope(run.scope, configuration!.value.scope, productId, projectId))
          || current && (current.value.run_id !== selectedRunId || !matchesSecurityScope(current.value.scope, configuration!.value.scope, productId, projectId))) throw { status: 403 };
        if (current) setDetail(current);
        setRuns(previous => {
          const updated = current ? [current.value, ...page.items.filter(run => run.run_id !== current.value.run_id)] : page.items;
          return [...updated, ...previous.filter(run => !updated.some(next => next.run_id === run.run_id))];
        });
        setProgressError('');
      } catch (failure) {
        if (stopped || revoked.current || generation !== loadGeneration.current) return;
        if (typeof failure === 'object' && failure !== null && 'status' in failure && (failure.status === 403 || failure.status === 404)) {
          revoked.current = true; loadGeneration.current += 1; invalidateCheck(); admission.clear();
          setRuns([]); setBindings([]); setDetail(null); setNextCursor(null); setSelectedRunId('');
          setProgressError(''); setError('Assessment access is unavailable. Refresh the workspace to check your access.');
          onAccessDenied?.();
        } else {
          setProgressError('Progress could not be refreshed. Showing the last confirmed status; checking again automatically.');
        }
      } finally {
        if (!stopped && !revoked.current) timer = window.setTimeout(() => void updateProgress(), 15_000);
      }
    }
    timer = window.setTimeout(() => void updateProgress(), 15_000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [followingProgress, configuration, productId, projectId, selectedRunId, busy, pending, onAccessDenied, invalidateCheck]);
  useEffect(() => { invalidateCheck(); }, [configuration, bindingId, capability, invalidateCheck]);
  useEffect(() => {
    if (!check?.result.ready) return;
    const timer = window.setTimeout(() => setCheck(current => current === check
      ? { ...current, result: { ...current.result, ready: false, gaps: ['readiness_expired'] } } : current), Math.max(0, check.expiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [check]);

  async function inspect(runId: string) {
    setSelectedRunId(runId);
    setDetail(null);
    if (!runId) return;
    try {
      const current = await getSecurityRun(productId, projectId, runId);
      if (revoked.current) return;
      if (!inScope(current.value.scope)) throw { status: 403 };
      setDetail(current); setError('');
    } catch (readError) {
      invalidateCheck();
      if (revoked.current) return;
      setRuns([]); setDetail(null); setNextCursor(null); setSelectedRunId('');
      if (typeof readError === 'object' && readError !== null && 'status' in readError && (readError.status === 403 || readError.status === 404)) { revoked.current = true; admission.clear(); onAccessDenied?.(); }
      setError('This assessment is unavailable. Refresh to check its current scope.');
    }
  }

  const settings = configuration?.value.settings;
  const sources = (settings?.source_refs ?? []).filter(item => capability && item.record_type === sourceType(capability));
  const selectedSources = sources.filter(item => sourceKeys.includes(referenceKey(item)));
  const trigger = settings?.triggers.find(item => item.kind === 'manual' && item.enabled && item.capability === capability && item.binding_id === bindingId
    && item.source_refs.length === selectedSources.length
    && item.source_refs.every(source => selectedSources.some(selected => referenceKey(source) === referenceKey(selected))));
  const binding = bindingsReadAt === configuration && bindings.find(item => item.binding_id === bindingId && item.state === 'ready' && item.configuration_id === configuration?.value.configuration_id
);
  useEffect(() => {
    if (!configuration || bindingsReadAt !== configuration) return;
    const saved = settings?.triggers.filter(item => item.kind === 'manual' && item.enabled
      && (!capability || item.capability === capability)
      && bindings.some(candidate => candidate.binding_id === item.binding_id
        && candidate.configuration_id === configuration.value.configuration_id && candidate.state === 'ready')) ?? [];
    if (!capability && saved.length && isSecurityCapability(saved[0].capability)) { setCapability(saved[0].capability); return; }
    const key = JSON.stringify([configuration.value.configuration_id, capability, bindingId]);
    if (defaultSelection.current === key) return;
    const matches = saved.filter(item => !bindingId || item.binding_id === bindingId);
    if (matches.length !== 1) return;
    const chosen = matches[0];
    if (!chosen.binding_id) return;
    defaultSelection.current = JSON.stringify([configuration.value.configuration_id, capability, chosen.binding_id]);
    setBindingId(chosen.binding_id); setSourceKeys(chosen.source_refs.map(referenceKey));
  }, [configuration, bindingsReadAt, bindings, capability, bindingId, settings]);
  const currentCheck = check?.configuration === configuration && check.binding === binding && check.capability === capability ? check : null;
  const ready = Boolean(currentCheck?.result.ready && !currentCheck.result.gaps.length && currentCheck.expiresAt > Date.now() && !checking);
  const canSubmit = Boolean(configuration && settings?.enabled && capability && binding && selectedSources.length && trigger
    && (capability !== 'code_review' || (!prBase && !prHead || Boolean(prBase && prHead)))
    && securityCapabilityReady(binding, capability) && !checking && (!currentCheck || ready || currentCheck.result.gaps.includes('readiness_expired')) && !error && !pending && !busy && !admission.blocked && !revoked.current && !runs.some(followingRun));

  async function checkReadiness(): Promise<boolean> {
    if (!configuration || !binding || !capability || busy || checking || pending || revoked.current) return false;
    invalidateCheck(); const request = checkGeneration.current;
    const expiresAt = Date.now() + 60_000;
    setChecking(true); setError('');
    try {
      const result = await checkSecurityReadiness(productId, projectId, configuration, binding, capability);
      if (request !== checkGeneration.current || revoked.current) return false;
      setCheck({ result: result.ready && expiresAt <= Date.now() ? { ...result, ready: false, gaps: ['readiness_expired'] } : result,
        configuration, binding, capability, expiresAt });
      return result.ready && !result.gaps.length && expiresAt > Date.now();
    } catch (failure) {
      if (request !== checkGeneration.current || revoked.current) return false;
      const response = (failure && typeof failure === 'object' ? failure : {}) as { status?: number; detail?: { code?: unknown } };
      if (response.status === 403 || response.status === 404) {
        revoked.current = true; setBindings([]); setRuns([]); setDetail(null); admission.clear(); onAccessDenied?.();
      }
      setError(typeof response.detail?.code === 'string' ? securityReadinessAction(response.detail.code)
        : 'Security readiness could not be confirmed for this configuration and binding. Refresh assessment status and review owner consent in Setup, then check again.');
      return false;
    } finally { if (request === checkGeneration.current) setChecking(false); }
  }

  async function submit(input: SecurityRunInput, receipt = productReceipt()) {
    if (busy || revoked.current) return;
    invalidateCheck(); setError('');
    await admission.submit({ kind: 'submit', input, receipt });
  }

  async function changeRun(action: 'cancel' | 'reconcile' | 'retry') {
    if (!detail || !reason.trim() || busy || pending) return;
    if (action === 'retry') {
      invalidateCheck(); setError('');
      await admission.submit({ kind: 'retry', runId: detail.value.run_id, reason: reason.trim(), capability: detail.value.input?.capability, receipt: productReceipt() });
      return;
    }
    setBusy(true);
    try {
      const updated = await mutateSecurityRun(productId, projectId, detail.value.run_id, action, reason.trim());
      if (revoked.current) return;
      if (!inScope(updated.value.scope)) throw { status: 403 };
      setDetail(updated);
      setReason('');
      setError('');
    } catch (actionError) {
      invalidateCheck();
      if (revoked.current) return;
      if (typeof actionError === 'object' && actionError !== null && 'status' in actionError && (actionError.status === 403 || actionError.status === 404)) { revoked.current = true; setRuns([]); setDetail(null); onAccessDenied?.(); }
      setError('The action was not confirmed. Refresh this run before requesting another action.');
    }
    finally { setBusy(false); }
  }

  const monitoring = detail?.value.monitoring;
  const observation = monitoring?.latest_observation;
  const taskCounts = Array.isArray(observation?.tasks) ? observation.tasks.reduce((counts, task) => {
    const state = task.status.toUpperCase();
    if (state === 'IN_PROGRESS' || state === 'RUNNING') counts.running++;
    else if (state === 'COMPLETED' || state === 'SUCCEEDED') counts.completed++;
    else if (state === 'FAILED') counts.failed++;
    else if (state === 'STOPPED' || state === 'CANCELLED' || state === 'CANCELED') counts.stopped++;
    else counts.unknown++;
    return counts;
  }, { running: 0, completed: 0, failed: 0, stopped: 0, unknown: 0 }) : observation?.tasks;
  return <section aria-label="Security assessments" className="security-workspace security-panel">
    {(error || admission.error) && <p role="alert">{error || admission.error}</p>}
    {!configuration && <p>Save a Security configuration before assessing sources.</p>}
    {admission.preparation && <div role="status">
      <p>Preparing the full repository snapshot…</p>
      {admission.preparation.stage && <p>{admission.preparation.stage}</p>}
      <p>{admission.preparation.paused ? admission.preparation.explanation : 'Checking automatically with the original request. Server preparation continues if you leave this page.'}</p>
    </div>}
    {pending && <div>
      <p>The original {pending.kind === 'retry' ? 'assessment retry' : pending.input.capability.replace(/_/g, ' ')} request is retained. Continuing uses its original choices and receipt.</p>
      {!admission.durable && <p>This configuration has no verified owner identity, so its receipt is retained only while this page stays open. Refresh assessment status before starting another assessment after leaving.</p>}
      <button type="button" disabled={busy || !configuration} onClick={() => { invalidateCheck(); setError(''); void admission.submit(pending); }}>Retry original assessment receipt</button>
    </div>}
    {followingProgress && <p className="product-muted">Progress updates automatically every 15 seconds while an assessment is active.</p>}
    {progressError && <p role="status">{progressError}</p>}
    {configuration && <>
      <section className="security-start-run" aria-label="Start a new assessment">
      <div className="security-panel-heading"><div><h3>Start a new assessment</h3><p>Choose what to check. Your saved sources are selected below.</p></div><button type="button" disabled={busy || !configuration} onClick={() => void refresh()}>Refresh assessment status</button></div>
      <div className="security-form-grid"><label className="block">Assessment capability <select value={capability} disabled={checking} onChange={event => { if (event.target.value === capability) return; defaultSelection.current = ''; setCapability(event.target.value as Capability); setSourceKeys([]); setBindingId(''); }}>
        <option value="">Select capability</option>{settings?.capabilities.filter(isSecurityCapability).map(item => <option key={item} value={item}>{securityCapabilityName(item)}</option>)}
      </select></label>
      <label className="block">Security workspace <select value={bindingId} disabled={checking} onChange={event => { if (event.target.value === bindingId) return; defaultSelection.current = event.target.value ? '' : JSON.stringify([configuration.value.configuration_id, capability, '']); setBindingId(event.target.value); setSourceKeys([]); }}>
        <option value="">Select a saved workspace</option>{bindings.filter(item => item.state === 'ready' && item.configuration_id === configuration.value.configuration_id).map(item =>
          <option key={item.binding_id} value={item.binding_id}>Saved workspace · {settings?.region ?? item.binding_id.slice(0, 8)}</option>)}
      </select></label>
      </div>
      {capability && !bindings.some(item => item.configuration_id === configuration.value.configuration_id && securityCapabilityReady(item, capability))
        && <p>Workspace access needs a fresh check before this assessment can start.</p>}
      <fieldset><legend>Sources from Setup</legend>{sources.map(item => <label className="block" key={referenceKey(item)}>
        <input type="checkbox" disabled={checking} checked={sourceKeys.includes(referenceKey(item))} onChange={event => setSourceKeys(current => event.target.checked ? [...current, referenceKey(item)] : current.filter(key => key !== referenceKey(item)))} /> {sourceName(item)} (revision {item.record_revision})
      </label>)}{!sources.length && <p>No pinned sources match this capability.</p>}</fieldset>
      {capability === 'code_review' && <ServiceDisclosure title="Limit to a pull request" hint="Optional — leave empty to scan the full repository"><div className="security-form-grid">
        <label className="block">Pull request base revision (optional) <input disabled={checking} value={prBase} onChange={event => setPrBase(event.target.value)} /></label>
        <label className="block">Pull request head revision (optional) <input disabled={checking} value={prHead} onChange={event => setPrHead(event.target.value)} /></label>
      </div></ServiceDisclosure>}
      {capability && bindingId && sourceKeys.length > 0 && !trigger && <p className="security-note">These sources do not match a saved run. Choose the sources saved in Setup, or update the manual run settings in Setup.</p>}
      {runs.some(followingRun) && <p>An assessment is still active or its outcome is unknown. Inspect it before starting another paid assessment.</p>}
      <details><summary>Check access without starting</summary><button type="button" disabled={busy || checking || Boolean(pending) || !settings?.enabled || !binding || !capability} onClick={() => void checkReadiness()}>Check Security readiness</button></details>
      {currentCheck && <div role="status"><p>{ready ? 'Ready to start.' : 'Access needs attention before this assessment can start.'}</p>
        {currentCheck.result.gaps.map(code => <p key={code}>{securityReadinessAction(code)} <small>({code})</small></p>)}
      </div>}
      <div className="security-actions"><button type="button" className="product-primary" aria-label="Start Security assessment" disabled={!canSubmit} onClick={async () => {
        if (!canSubmit || !configuration || !capability || !binding || !trigger) return;
        if (!securityCapabilityReady(binding, capability)) { invalidateCheck(); setError('Binding observations expired. Choose Refresh run access to verify the saved workspace before starting.'); return; }
        if ((!ready || !currentCheck || currentCheck.expiresAt <= Date.now()) && !await checkReadiness()) return;
        void submit({ configuration_revision: configuration.value.revision, binding_id: binding.binding_id, capability, sources: trigger.source_refs,
          ...(capability === 'code_review' && prBase && prHead ? { pr_base: prBase, pr_head: prHead } : {}) });
      }}>{checking ? 'Checking access…' : busy ? 'Starting assessment…' : 'Start Security assessment'}</button>
      <RunAccess service="security" configuration={configuration} bindingId={bindingId} disabled={busy || checking || Boolean(pending) || runs.some(followingRun)} onReady={() => void refresh()} /></div>
      </section>
      <h3>Run history</h3>
      <div className="security-list" aria-label="Assessment history">
        {runs.map(run => <button type="button" className="security-list-row" key={run.run_id} aria-pressed={selectedRunId === run.run_id} onClick={() => void inspect(run.run_id)}>
          <span><strong>{securityCapabilityName(run.input?.capability)}</strong><small>{run.input?.sources.map(sourceName).join(', ') || `Run ${run.run_id.slice(0, 8)}`} · Last checked {securityDate(run.monitoring?.last_successful_poll_at)}</small></span><SecurityBadge value={run.state} />
        </button>)}
      </div>
      <ServiceDisclosure title="Find an assessment by reference">
      <label className="block">Visible assessments <select value={selectedRunId} onChange={event => void inspect(event.target.value)}>
        <option value="">Select an assessment</option>{runs.map(run => <option key={run.run_id} value={run.run_id}>{run.run_id} — {run.state}</option>)}
      </select></label>
      </ServiceDisclosure>
      {!runs.length && <p className="security-empty">No assessments are visible yet. Start one above.</p>}
      {nextCursor && <button type="button" disabled={busy} onClick={async () => {
        try {
          const page = await listSecurityRuns(productId, projectId, nextCursor);
          if (page.items.some(run => !inScope(run.scope))) throw { status: 403 };
          if (revoked.current) return;
          setRuns(current => [...current, ...page.items]); setNextCursor(page.next_cursor ?? null);
        } catch (readError) {
          invalidateCheck();
          if (typeof readError === 'object' && readError !== null && 'status' in readError && (readError.status === 403 || readError.status === 404)) { revoked.current = true; setRuns([]); setDetail(null); onAccessDenied?.(); }
          setError('More assessments are unavailable.');
        }
      }}>Load more assessments</button>}
      {detail && <div aria-label="Assessment detail" className="security-panel">
        <div className="security-panel-heading"><h3>{securityCapabilityName(detail.value.input?.capability)}</h3><SecurityBadge value={detail.value.state} /></div>
        {taskCounts ? <div className="security-metrics" aria-label="Observed task progress">
          <div className="security-metric"><strong>{taskCounts.completed}</strong><span>Tasks completed</span></div>
          <div className="security-metric"><strong>{taskCounts.running}</strong><span>Tasks running</span></div>
          <div className="security-metric"><strong>{taskCounts.failed}</strong><span>Tasks failed</span></div>
        </div> : <p className="security-note">Task progress has not been reported yet.</p>}
        {observation?.phases?.length ? <ol className="security-phases" aria-label="Assessment stages">{observation.phases.map(phase => <li key={phase.name}><span>{phase.name.replace(/_/g, ' ')}</span><SecurityBadge value={phase.status ?? phase.native_status ?? 'unknown'} /></li>)}</ol> : null}
        <p className="product-muted">Last confirmed check: {securityDate(monitoring?.last_successful_poll_at)} · Deadline: {securityDate(detail.value.deadline_at)}</p>
        {monitoring?.state === 'recovery_pending' && <p className="security-note">Checking the existing AWS assessment and recovering its results. Progress updates automatically.</p>}
        {monitoring?.progress_state === 'suspected_stalled' && <p className="security-note">No recent progress has been observed. Review the monitoring details before deciding whether to intervene.</p>}
        {monitoring?.last_poll_error && <div role="alert" className="rounded-md bg-amber-50 p-3 text-sm text-amber-950 dark:bg-amber-950/30 dark:text-amber-100">
          {monitoring.last_poll_error.message && <p>{monitoring.last_poll_error.message}</p>}
          <p>{monitoring.last_poll_error.action || monitoring.last_poll_error.next_action || 'The last run step could not finish. Refresh this assessment and inspect the failure details before retrying.'}</p>
          <details>
            <summary>Failure details</summary>
            <p>{monitoring.last_poll_error.code}{monitoring.last_poll_error.stage ? ` at ${monitoring.last_poll_error.stage}` : ''}{monitoring.last_poll_error.cause ? `: ${monitoring.last_poll_error.cause}` : ''}</p>
          </details>
        </div>}
        <a href="#l5-security-results">Review assessment results</a>
        <ServiceDisclosure title="Monitoring details" hint="Run identifiers, provider status and polling history"><div className="security-technical">
        <p>Assessment {detail.value.run_id}, revision {detail.value.revision}</p>
        <p>Local run state: {detail.value.state}</p>
        <p>Provider state: {detail.value.provider_state}</p>
        <p>Deadline: {detail.value.deadline_at}</p>
        <p>Monitoring freshness: {monitoring?.state ?? 'not started'}; poll attempts: {monitoring?.poll_attempts ?? 0}; consecutive read errors: {monitoring?.consecutive_poll_errors ?? 0}</p>
        <p>Progress evidence: {monitoring?.progress_state ?? 'unknown'}{monitoring?.progress_state === 'suspected_stalled' ? ' (advisory only; no automatic restart)' : ''}</p>
        <p>Native job status: {observation?.status ?? observation?.native_status ?? 'Unavailable'}. Progress coverage: {observation?.progress_coverage ?? 'unavailable'}.</p>
        <p>Provider phases: {observation?.phases?.map(phase => `${phase.name}: ${phase.status ?? phase.native_status ?? 'unavailable'}`).join('; ') || 'Unavailable'}</p>
        <p>Native tasks: {taskCounts ? `running ${taskCounts.running}, completed ${taskCounts.completed}, failed ${taskCounts.failed}, stopped ${taskCounts.stopped}, unknown ${taskCounts.unknown}` : 'Unavailable'}</p>
        <p>Last check attempted: {monitoring?.last_poll_attempt_at ?? 'Not yet checked'}; last successful check: {monitoring?.last_successful_poll_at ?? 'None'}; next check: {monitoring?.next_poll_at ?? 'Not scheduled'}</p>
        <p>Last observed progress: {monitoring?.last_progress_at ?? 'Unavailable'}</p>
        <p>Monitoring policy: poll every {detail.value.admission?.monitoring_policy.poll_interval_seconds ?? 'unavailable'} seconds, grace {detail.value.admission?.monitoring_policy.poll_grace_seconds ?? 'unavailable'} seconds; stall advisory after {detail.value.admission?.monitoring_policy.stall_after_seconds ?? 'disabled'} seconds.</p>
        </div></ServiceDisclosure>
        {(activeStates.has(detail.value.state) || ['failed', 'blocked', 'expired'].includes(detail.value.state)) && <ServiceDisclosure title="Manage this run" hint="Cancel, reconcile or request a checked retry">
        <label className="block">Reason for run action <input value={reason} onChange={event => setReason(event.target.value)} /></label>
        <div className="flex flex-wrap gap-2">
          {activeStates.has(detail.value.state) && <button type="button" disabled={busy || Boolean(pending) || !reason.trim()} onClick={() => void changeRun('cancel')}>Request cancellation</button>}
          {['outcome_unknown', 'expired', 'blocked'].includes(detail.value.state) && <button type="button" disabled={busy || Boolean(pending) || !reason.trim()} onClick={() => void changeRun('reconcile')}>Reconcile existing assessment</button>}
          {['failed', 'blocked', 'expired'].includes(detail.value.state) && <button type="button" disabled={busy || Boolean(pending) || !reason.trim()} onClick={() => void changeRun('retry')}>Request a checked retry</button>}
        </div></ServiceDisclosure>}
      </div>}

    </>}
  </section>;
}
