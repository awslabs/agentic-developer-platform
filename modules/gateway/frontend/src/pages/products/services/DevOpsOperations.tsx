import RunAccess from './RunAccess';
import { DevOpsBadge, devOpsDate, devOpsCapability } from './DevOpsPresentation';
import { ServiceDisclosure } from './L5ServiceJourney';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { devOpsSetupDraft, devOpsSetupTarget, sameConsentScope } from '@/services/l5OwnerConsent';
import { devOpsSetupStorageKey, readDevOpsSetupReceipt, retainDevOpsSetupReceipt, setupOperationMatches, type DevOpsSetupReceipt } from '@/services/devOpsSetupReceipt';
import { securityReadinessSummary } from '@/services/l5Services';
import { configureDevOpsPrevention, getDevOpsBinding, getDevOpsBindingOperation, getDevOpsPrevention, getDevOpsPublication, getDevOpsRun, launchDevOpsBrowser, listDevOpsBindings, listDevOpsEvents, listDevOpsFindings, listDevOpsResults, listDevOpsRuns, retryDevOpsPublication, startDevOpsBindingOperation, submitDevOpsRun, matchesSecurityScope, type BindingRequest, type DevOpsBinding, type DevOpsConfiguration, type DevOpsEvent, type DevOpsPrevention, type DevOpsResult, type DevOpsRun, type DevOpsRunInput, type SecurityBindingOperation, type SecurityFinding, type SecurityPublication, type Versioned } from '@/services/l5Services';
import { productReceipt, type RecordReference } from '@/services/products';
import DevOpsReleaseEvidence, { type DevOpsObservation } from './DevOpsReleaseEvidence';
import DevOpsKnowledgeEvidence from './DevOpsKnowledgeEvidence';
import DevOpsInvestigationInput from './DevOpsInvestigationInput';
import type { NativeArrival } from '@/services/devopsKnowledgeEvidence';
import DevOpsNativeConsent from './DevOpsNativeConsent';
import { checkNativeEligibility, nativeEligibilityMatches, nativeAccessError, type NativeEligibility } from '@/services/devOpsNativeAccess';
import { checkInvestigationReadiness, investigationGapAction, investigationReadbackMatches, type InvestigationReadiness } from '@/services/devOpsReadiness';
import { appendReadbackPage } from '@/services/l5ReadbackPages';
import DevOpsInvestigationReport from './DevOpsInvestigationReport';
import { reconcileDevOpsRun } from '@/services/l5Services';

export interface DevOpsOperationSections {
  status: ReactNode;
  setup: ReactNode;
  capabilities: ReactNode;
  nativeConsent?: ReactNode;
  nativeAccess?: ReactNode;
  workspace?: { saved: DevOpsBinding | null; nativeReady: boolean; busy: boolean; open: () => void; notice: string };
  runs: ReactNode;
  results: ReactNode;
}
interface Props {
  productId: string;
  projectId: string;
  configuration: Versioned<DevOpsConfiguration>;
  onAccessDenied: () => void;
  onConfigurationChanged?: (value: Versioned<DevOpsConfiguration>) => void;
  children?: (sections: DevOpsOperationSections) => ReactNode;
}
const denied = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error && (error.status === 403 || error.status === 404);
const stale = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error && error.status === 412;

export default function DevOpsOperations({ productId, projectId, configuration, onAccessDenied, onConfigurationChanged, children }: Props) {
  const [bindings, setBindings] = useState<DevOpsBinding[]>([]);
  const [bindingId, setBindingId] = useState('');
  const [detail, setDetail] = useState<Versioned<DevOpsBinding> | null>(null);
  const [operation, setOperation] = useState<SecurityBindingOperation | null>(null);
  const [runs, setRuns] = useState<DevOpsRun[]>([]);
  const [results, setResults] = useState<DevOpsResult[]>([]);
  const [events, setEvents] = useState<DevOpsEvent[]>([]);
  const [prevention, setPrevention] = useState<DevOpsPrevention | null>(null);
  const [preventionError, setPreventionError] = useState('');
  const [investigationCheck, setInvestigationCheck] = useState<{ value: InvestigationReadiness; configuration: Versioned<DevOpsConfiguration>; binding: Versioned<DevOpsBinding> } | null>(null);
  const [checkingInvestigation, setCheckingInvestigation] = useState(false);
  const [nativeCheck, setNativeCheck] = useState<{ value: NativeEligibility; configuration: Versioned<DevOpsConfiguration>; binding: Versioned<DevOpsBinding> } | null>(null);
  const [checkingNative, setCheckingNative] = useState(false);
  const [nativeError, setNativeError] = useState('');
  const [nativeConsentNotice, setNativeConsentNotice] = useState('');
  const nativeGeneration = useRef(0);
  const invalidateNative = useCallback(() => { nativeGeneration.current += 1; setNativeCheck(null); setCheckingNative(false); setNativeError(''); }, []);
  useEffect(() => { invalidateNative(); }, [configuration, invalidateNative]);
  useEffect(() => {
    if (!nativeCheck) return;
    const timer = window.setTimeout(() => setNativeCheck(current => current === nativeCheck ? null : current), Math.max(0, Date.parse(nativeCheck.value.valid_until) - Date.now()));
    return () => window.clearTimeout(timer);
  }, [nativeCheck]);
  const checkGeneration = useRef(0);
  const [findings, setFindings] = useState<SecurityFinding[]>([]);
  const [publication, setPublication] = useState<SecurityPublication | null>(null);
  const [selectedResult, setSelectedResult] = useState('');
  const [runCursor, setRunCursor] = useState<string>();
  const [resultCursor, setResultCursor] = useState<string>();
  const [findingCursor, setFindingCursor] = useState<string>();
  const [paging, setPaging] = useState({ runs: false, results: false, findings: false });
  const pageRequests = useRef<Partial<Record<keyof typeof paging, symbol>>>({});
  const invalidatePages = useCallback(() => {
    pageRequests.current = {};
    setPaging({ runs: false, results: false, findings: false });
  }, []);
  const capability = 'investigation' as const;
  const [evidence, setEvidence] = useState<DevOpsObservation | null>(null);
  const [nativeArrival, setNativeArrival] = useState<NativeArrival | null>(null);
  const [evidenceRun, setEvidenceRun] = useState<string | null>(null);
  const [evidenceLaunch, setEvidenceLaunch] = useState<string | null>(null);
  const [issuedLaunchUrl, setIssuedLaunchUrl] = useState<{ url: string; expiresAt: string } | null>(null);
  const evidenceAttempts = useRef(new Set<string>());
  const selectEvidence = useCallback((value: DevOpsObservation | null) => { setEvidence(value); setEvidenceRun(null); setEvidenceLaunch(null); setIssuedLaunchUrl(null); }, []);
  const [runInput, setRunInput] = useState('{"kind":"investigation","question":"","resource_refs":[],"telemetry_refs":[]}');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [progressError, setProgressError] = useState('');
  const [loading, setLoading] = useState(true);
  const [readbackFailed, setReadbackFailed] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState<{ kind: 'binding' | 'run'; receipt: string; request: BindingRequest | DevOpsRunInput; observationChallenge?: string; configurationKey?: string } | null>(null);
  const [setupReceipt, setSetupReceipt] = useState<DevOpsSetupReceipt | null>(null);
  const currentSetupReceipt = useRef<DevOpsSetupReceipt | null>(null);
  currentSetupReceipt.current = setupReceipt;
  const [setupRestored, setSetupRestored] = useState(false);
  const [setupStorageError, setSetupStorageError] = useState('');
  const generation = useRef(0);
  const selection = useRef('');

  const scope = configuration.value.scope;
  const settings = configuration.value.settings;
  // Eligibility to request setup only. The server independently checks the exact
  // owner grant, its expiry, saved scope and current connection evidence.
  const setupDraft = devOpsSetupDraft(settings, scope);
  const setupTarget = devOpsSetupTarget(settings, scope);
  const bindingConfigurationKey = JSON.stringify([configuration.value.configuration_id, configuration.value.revision,
    configuration.value.hash, scope, setupTarget]);
  const bindingOwnerKey = JSON.stringify([scope.tenant_id, productId, projectId, scope.owner_id, configuration.value.configuration_id]);
  const setupStorageKey = devOpsSetupStorageKey(configuration.value, productId, projectId);
  const currentBindingConfiguration = useRef(bindingConfigurationKey);
  const currentBindingOwner = useRef(bindingOwnerKey);
  currentBindingConfiguration.current = bindingConfigurationKey;
  currentBindingOwner.current = bindingOwnerKey;
  const readRetainedSetup = useCallback(() => {
    setSetupRestored(false); setSetupStorageError('');
    if (!setupStorageKey) {
      setSetupReceipt(null);
      setSetupStorageError('Reload a saved configuration with its current owner identity before requesting provider setup.');
      return;
    }
    try {
      const retained = readDevOpsSetupReceipt(setupStorageKey, bindingOwnerKey);
      const inMemory = currentSetupReceipt.current;
      if (inMemory?.ownerKey === bindingOwnerKey && inMemory.state !== 'rejected'
        && (!retained || retained.key !== inMemory.key || !sameConsentScope(retained.request, inMemory.request))) {
        throw new Error('Original setup receipt disappeared or changed.');
      }
      setSetupReceipt(retained); setSetupRestored(true);
      setUncertain(current => current?.kind === 'run' || (current?.kind === 'binding' && (current.request as BindingRequest).binding_id) ? current : retained?.state === 'pending'
        ? { kind: 'binding', receipt: retained.key, request: retained.request, configurationKey: retained.configurationKey } : null);
      setOperation(retained?.operation ?? null);
    } catch {
      setSetupStorageError('The original setup receipt could not be read or is malformed. New setup is blocked. Restore access to the original session receipt and read it again; do not clear it to start a replacement request.');
    }
  }, [setupStorageKey, bindingOwnerKey]);
  useEffect(() => {
    setUncertain(null); setOperation(null); setSetupReceipt(null); setBusy(false); readRetainedSetup();
  }, [readRetainedSetup]);
  const setupBlocked = !setupRestored || Boolean(setupStorageError) || (setupReceipt !== null && setupReceipt.state !== 'rejected');
  const staleBindingReceipt = uncertain?.kind === 'binding' && uncertain.configurationKey !== bindingConfigurationKey;
  const preventionConfigured = settings.capabilities.includes('prevention');
  const invalidateInvestigation = useCallback(() => { checkGeneration.current += 1; setInvestigationCheck(null); setCheckingInvestigation(false); }, []);
  useEffect(() => { invalidateInvestigation(); }, [configuration, invalidateInvestigation]);
  useEffect(() => {
    if (!investigationCheck?.value.ready) return;
    const timer = window.setTimeout(() => setInvestigationCheck(current => current === investigationCheck
      ? { ...current, value: { ...current.value, ready: false, gaps: ['readiness_expired'] } } : current),
    Math.max(0, Date.parse(investigationCheck.value.valid_until ?? '') - Date.now()));
    return () => window.clearTimeout(timer);
  }, [investigationCheck]);

  const matches = useCallback((value: { scope: DevOpsConfiguration['scope'] }) =>
    matchesSecurityScope(value.scope, scope, productId, projectId), [scope, productId, projectId]);
  const fail = useCallback((readError: unknown) => {
    generation.current += 1; invalidatePages(); setLoading(false); setBusy(false);
    setRunCursor(undefined); setResultCursor(undefined); setFindingCursor(undefined);
    setReadbackFailed(true);
    invalidateInvestigation(); invalidateNative(); setPreventionError('');
    setDetail(null); setPrevention(null); setEvents([]); setFindings([]); setPublication(null); setSelectedResult(''); setOperation(null);
    if (denied(readError)) { setBindings([]); setDetail(null); setRuns([]); setResults([]); onAccessDenied(); }
    setError(denied(readError) ? 'DevOps access is unavailable for this project.' : 'DevOps readback is unavailable. Refresh before acting.');
  }, [onAccessDenied, invalidateInvestigation, invalidateNative, invalidatePages]);
  const reportAccessDenied = useCallback(() => fail({ status: 403 }), [fail]);

  const refresh = useCallback(async () => {
    const request = ++generation.current;
    invalidatePages(); setRunCursor(undefined); setResultCursor(undefined); setFindingCursor(undefined);
    setLoading(true); setError('');
    invalidateInvestigation(); invalidateNative(); setPreventionError('');
    setReadbackFailed(true);
    setBindings([]); setRuns([]); setResults([]); setDetail(null); setPrevention(null); setEvents([]);
    setFindings([]); setPublication(null); setSelectedResult(''); setBindingId(''); selection.current = '';
    try {
      const [bindingPage, runPage, resultPage] = await Promise.all([
        listDevOpsBindings(productId, projectId), listDevOpsRuns(productId, projectId), listDevOpsResults(productId, projectId),
      ]);
      if (request !== generation.current) return;
      if ([...bindingPage.items, ...runPage.items, ...resultPage.items].some(item => !matches(item))) throw { status: 403 };
      setBindings(bindingPage.items); setRuns(runPage.items); setResults(resultPage.items);
      setRunCursor(runPage.next_cursor); setResultCursor(resultPage.next_cursor);
      setReadbackFailed(false);
      setDetail(null); setPrevention(null); setEvents([]); setFindings([]); setPublication(null);
      setBindingId(''); selection.current = '';
      const choices = bindingPage.items.filter(item => item.configuration_id === configuration.value.configuration_id
        && !['cleaned', 'cleanup_pending', 'unlinked'].includes(item.state));
      const triggerIds = new Set((settings.devops.trigger_policies as Record<string, unknown>[]).filter(item => item.kind === 'manual' && item.enabled
        && item.capability === 'investigation').map(item => item.binding_id));
      const preferred = choices.filter(item => triggerIds.has(item.binding_id));
      const chosen = preferred.length === 1 ? preferred[0] : choices.length === 1 ? choices[0] : null;
      if (chosen) {
        const current = await getDevOpsBinding(productId, projectId, chosen.binding_id);
        if (request !== generation.current) return;
        if (!matches(current.value) || current.value.binding_id !== chosen.binding_id
          || current.value.configuration_id !== configuration.value.configuration_id) throw { status: 403 };
        setDetail(current); setBindingId(chosen.binding_id); selection.current = chosen.binding_id;
      }
    } catch (readError) {
      if (request === generation.current) fail(readError);
    } finally { if (request === generation.current) setLoading(false); }
  }, [productId, projectId, matches, fail, invalidateInvestigation, invalidateNative, invalidatePages, configuration]);

  useEffect(() => { void refresh(); return () => { generation.current += 1; }; }, [refresh]);

  const following = runs.some(run => ['admitted', 'queued', 'submitting', 'running', 'collecting', 'outcome_unknown'].includes(run.state)
    || run.monitoring?.state === 'recovery_pending');
  useEffect(() => {
    if (!following || loading || busy || readbackFailed) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      const request = generation.current;
      try {
        const [runPage, resultPage] = await Promise.all([listDevOpsRuns(productId, projectId), listDevOpsResults(productId, projectId)]);
        if (stopped || request !== generation.current) return;
        if ([...runPage.items, ...resultPage.items].some(item => !matches(item))) throw { status: 403 };
        setRuns(previous => [...runPage.items.map(item => previous.find(old => old.run_id === item.run_id && old.revision > item.revision) ?? item), ...previous.filter(item => !runPage.items.some(next => next.run_id === item.run_id))]);
        setResults(previous => [...resultPage.items.map(item => previous.find(old => old.result_id === item.result_id && old.revision > item.revision) ?? item), ...previous.filter(item => !resultPage.items.some(next => next.result_id === item.result_id))]);
        setProgressError('');
      } catch (failure) {
        if (stopped || request !== generation.current) return;
        if (denied(failure) || (failure as { status?: number })?.status === 401) fail({ status: 403 });
        else setProgressError('Progress could not be refreshed. Showing the last confirmed status; checking again automatically.');
      } finally {
        if (!stopped) timer = setTimeout(() => void poll(), 15_000);
      }
    }
    timer = setTimeout(() => void poll(), 15_000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [following, loading, busy, readbackFailed, productId, projectId, matches, fail]);

  async function inspectBinding(id: string) {
    selection.current = id;
    const request = ++generation.current;
    invalidatePages();
    setReadbackFailed(true);
    invalidateInvestigation(); invalidateNative(); setPreventionError('');
    setBindingId(id); setDetail(null); setEvents([]); setPrevention(null); setError('');
    if (!id) { setReadbackFailed(false); return; }
    try {
      const current = await getDevOpsBinding(productId, projectId, id);
      if (request !== generation.current || id !== selection.current) return;
      if (!matches(current.value) || current.value.binding_id !== id || current.value.configuration_id !== configuration.value.configuration_id) throw { status: 403 };
      setDetail(current);
      setReadbackFailed(false);
      try {
        const currentEvents = await listDevOpsEvents(productId, projectId, id);
        if (request !== generation.current || id !== selection.current) return;
        if (currentEvents.items.some(item => item.binding_id !== id || !matches(item))) throw { status: 403 };
        setEvents(currentEvents.items);
      } catch (eventError) {
        if (denied(eventError)) throw eventError;
        if (request !== generation.current || id !== selection.current) return;
        setEvents([]); setProgressError('Workspace events could not be loaded. Run access is checked separately.');
      }
      if (preventionConfigured) {
        try {
          const currentPrevention = await getDevOpsPrevention(productId, projectId, id);
          if (request !== generation.current || id !== selection.current) return;
          if (currentPrevention.binding_id !== id || currentPrevention.configuration_revision !== configuration.value.revision) throw new Error('Prevention scope changed');
          setPrevention(currentPrevention);
        } catch {
          if (request !== generation.current || id !== selection.current) return;
          setPrevention(null);
          setPreventionError('Prevention status is unavailable for this saved configuration and consent. Review its separate owner authorization and refresh the binding before using prevention controls. Investigation requires its own readiness check below.');
        }
      }
    } catch (readError) { if (request === generation.current) { setDetail(null); fail(readError); } }
  }

  const currentInvestigation = investigationCheck?.configuration === configuration && investigationCheck.binding === detail ? investigationCheck.value : null;
  const ready = (capabilityName: string) => Boolean(!readbackFailed && !loading && !uncertain && detail && detail.value.state === 'ready' && !detail.value.gaps.length
    && (capabilityName === 'native_ui' ? !checkingNative && nativeCheck?.configuration === configuration && nativeCheck?.binding === detail && nativeEligibilityMatches(nativeCheck.value, configuration, detail)
      : capabilityName === 'investigation' ? !checkingInvestigation && currentInvestigation?.ready && Date.parse(currentInvestigation.valid_until ?? '') > Date.now()
      : detail.value.readiness.some(item =>
    item.capability === capabilityName && item.state === 'ready'
    && item.configuration_revision === configuration.value.revision && item.binding_generation === detail.value.generation
    && item.valid_until && Date.parse(item.valid_until) > Date.now())));

  async function checkInvestigation(): Promise<boolean> {
    if (!detail || busy || checkingInvestigation || readbackFailed || !settings.enabled || !settings.capabilities.includes('investigation')) return false;
    invalidateInvestigation(); const request = checkGeneration.current;
    setCheckingInvestigation(true); setError('');
    try {
      const value = await checkInvestigationReadiness(productId, projectId);
      if (request !== checkGeneration.current) return false;
      if (!investigationReadbackMatches(value, configuration, detail)) throw new Error('Investigation scope changed');
      setInvestigationCheck({ value, configuration, binding: detail });
      return value.ready && !value.gaps.length && Date.parse(value.valid_until ?? '') > Date.now();
    } catch (checkError) {
      const code = (checkError as { detail?: { code?: unknown } })?.detail?.code;
      if (request === checkGeneration.current) setError(typeof code === 'string'
        ? investigationGapAction(code)
        : 'Investigation readiness could not be confirmed for this configuration and binding. Refresh their current revisions, review owner consent and resource verification in Setup, then check again.');
      return false;
    } finally { if (request === checkGeneration.current) setCheckingInvestigation(false); }
  }

  async function startBinding(request: BindingRequest, receipt = productReceipt(), retry = false) {
    if (loading || busy || (readbackFailed && !retry) || (uncertain && !retry)) return;
    const setupRequest = !request.binding_id && ['create', 'link'].includes(request.action);
    if (setupRequest && (readbackFailed || !setupRestored || setupStorageError || !setupStorageKey || (!retry && setupBlocked))) return;
    if ((retry && staleBindingReceipt) || (!request.binding_id && (!setupDraft || !setupTarget
      || !sameConsentScope(request, { ...setupTarget, configuration_revision: configuration.value.revision })))) {
      setError('The saved setup selection changed. Read the original operation using its retained receipt before reviewing consent for another setup request.');
      return;
    }
    const generationAtStart = generation.current;
    const configurationAtStart = bindingConfigurationKey;
    const ownerAtStart = bindingOwnerKey;
    const retainOriginal = () => setUncertain({ kind: 'binding', receipt, request, configurationKey: configurationAtStart });
    let original: DevOpsSetupReceipt | null = null;
    let sent = false;
    invalidateInvestigation();
    setBusy(true); setError(''); setMessage('');
    try {
      if (setupRequest) {
        // Reread immediately before dispatch, including repeated clicks in the
        // same render. A retained uncertain/confirmed request cannot be replaced.
        const stored = readDevOpsSetupReceipt(setupStorageKey!, ownerAtStart);
        if (stored && stored.state !== 'rejected') {
          if (!retry || stored.state !== 'pending' || stored.key !== receipt || stored.configurationKey !== configurationAtStart
            || !sameConsentScope(stored.request, request)) throw new Error('Original setup request must be reconciled.');
          original = stored;
        } else {
          if (retry) throw new Error('Original setup receipt is unavailable.');
          original = { key: receipt, ownerKey: ownerAtStart, configurationKey: configurationAtStart, request, state: 'pending' };
        }
        retainDevOpsSetupReceipt(setupStorageKey!, original);
        setSetupReceipt(original);
        retainOriginal();
      }
      sent = true;
      const accepted = await startDevOpsBindingOperation(productId, projectId, request,
        request.binding_id ? detail?.etag : undefined, receipt);
      if (accepted.value.action !== request.action || !accepted.value.operation_id
        || (original && !setupOperationMatches(accepted.value, original))) throw new Error('Binding operation response does not match the request.');
      if (original) {
        const confirmed: DevOpsSetupReceipt = { ...original, state: 'confirmed', operation: accepted.value };
        retainDevOpsSetupReceipt(setupStorageKey!, confirmed);
        if (ownerAtStart === currentBindingOwner.current) setSetupReceipt(confirmed);
      }
      if (ownerAtStart !== currentBindingOwner.current) return;
      if (configurationAtStart !== currentBindingConfiguration.current) {
        setUncertain(null); setOperation(accepted.value); setReadbackFailed(true);
        setError('The saved setup selection changed while the request was in flight. Inspect the retained original operation before requesting another setup.'); return;
      }
      if (generationAtStart !== generation.current) return;
      setUncertain(null); setOperation(accepted.value); setReadbackFailed(true); setMessage(`Binding operation ${accepted.value.operation_id}: ${accepted.value.state}. Refresh to check readback.`);
    } catch (requestError) {
      if (ownerAtStart !== currentBindingOwner.current) return;
      if (setupRequest && !sent) {
        setSetupStorageError('The original setup request could not be retained or conflicts with a stored request, so no setup was sent. Read the retained setup receipt before continuing.');
        setSetupRestored(false); return;
      }
      if (configurationAtStart !== currentBindingConfiguration.current) {
        retainOriginal(); setError('The saved setup selection changed while the request was in flight. Its original receipt is retained; reconcile that operation before requesting another setup.'); return;
      }
      if (generationAtStart !== generation.current) return;
      const code = (requestError as { detail?: { code?: string } })?.detail?.code;
      if (!retry && code && ['delegation_denied', 'setup_consent_required'].includes(code)) {
        if (original) {
          try {
            const rejected: DevOpsSetupReceipt = { ...original, state: 'rejected' };
            retainDevOpsSetupReceipt(setupStorageKey!, rejected); setSetupReceipt(rejected);
          } catch {
            retainOriginal(); setSetupStorageError('The setup rejection could not be retained. Read the original setup receipt before continuing.'); return;
          }
        }
        setUncertain(null);
        setError('Provider setup needs current owner authorization for this exact saved configuration. Ask the Product owner to review and grant setup consent in Owner consent, then return to provider setup. No binding operation was admitted.');
      } else if (setupRequest) {
        retainOriginal(); setError('Binding operation not confirmed. Read current DevOps readback, then continue only with its retained original receipt.');
      } else if (denied(requestError)) fail(requestError);
      else if (stale(requestError)) { setReadbackFailed(true); setDetail(null); setError('Binding revision changed. Refresh and review before acting.'); }
      else { retainOriginal(); setError('Binding operation not confirmed. Retry only with its original receipt.'); }
    } finally { if (ownerAtStart === currentBindingOwner.current
      && (generationAtStart === generation.current || configurationAtStart !== currentBindingConfiguration.current)) setBusy(false); }
  }

  async function startRun(request: DevOpsRunInput, receipt = productReceipt(), retry = false) {
    if (loading || busy || (readbackFailed && !retry) || (uncertain && !retry)) return;
    const generationAtStart = generation.current;
    invalidateInvestigation();
    setBusy(true); setError(''); setMessage('');
    const observed = evidence?.subject === 'run_submission' ? evidence : null;
    try {
      if (observed && (retry || evidenceAttempts.current.has(observed.challenge_id) || evidenceRun || Date.parse(observed.expires_at) <= Date.now())) throw new Error('Read the original evidence before retrying');
      if (observed) evidenceAttempts.current.add(observed.challenge_id);
      const accepted = observed
        ? await submitDevOpsRun(productId, projectId, request, observed.receipt, observed.challenge_id)
        : await submitDevOpsRun(productId, projectId, request, receipt);
      if (generationAtStart !== generation.current) return;
      if (!matches(accepted.value)) throw { status: 403 };
      if (observed) setEvidenceRun(accepted.value.run_id);
      setUncertain(null); setMessage(`Run ${accepted.value.run_id}: ${accepted.value.state}. Refresh for provider readback.`);
      setRuns(current => [accepted.value, ...current]);
    } catch (requestError) {
      if (generationAtStart !== generation.current) return;
      const code = (requestError as { detail?: { code?: string } })?.detail?.code;
      if (code && ['delegation_denied', 'delegation_scope_changed', 'binding_or_delegation_unavailable', 'execution_policy_unavailable', 'selected_connection_unavailable', 'configuration_disabled', 'work_queue_unavailable', 'integration_not_ready'].includes(code)) {
        setUncertain(null);
        setError(`${investigationGapAction(code)} Your question and resource selection remain in this draft. Check readiness again before submitting.`);
      } else if (denied(requestError)) fail(requestError);
      else if (stale(requestError)) { setReadbackFailed(true); setDetail(null); setError('Run admission changed. Refresh configuration and binding before acting.'); }
      else { setUncertain({ kind: 'run', receipt: observed ? observed.receipt : receipt, request, ...(observed ? { observationChallenge: observed.challenge_id } : {}) }); setError(observed ? 'Read the original observation effect receipts before further action. Do not repeat an uncertain UI submission.' : 'Run submission not confirmed. Retry only with its original receipt.'); }
    } finally { if (generationAtStart === generation.current) setBusy(false); }
  }

  async function inspectSetupOperation() {
    if (!operation || busy) return;
    const original = operation;
    const ownerAtStart = bindingOwnerKey;
    setBusy(true); setError('');
    try {
      const current = await getDevOpsBindingOperation(productId, projectId, original.operation_id);
      if (ownerAtStart !== currentBindingOwner.current) return;
      if (current.operation_id !== original.operation_id || current.action !== original.action
        || (setupReceipt?.operation?.operation_id === original.operation_id && !setupOperationMatches(current, setupReceipt))) throw new Error('Operation scope changed.');
      if (setupReceipt?.operation?.operation_id === original.operation_id && setupStorageKey) {
        const updated: DevOpsSetupReceipt = { ...setupReceipt, state: 'confirmed', operation: current };
        retainDevOpsSetupReceipt(setupStorageKey, updated); setSetupReceipt(updated);
      }
      setOperation(current);
    } catch {
      if (ownerAtStart === currentBindingOwner.current) setError('The original operation could not be verified. Keep its receipt and read it again before another action.');
    } finally { if (ownerAtStart === currentBindingOwner.current) setBusy(false); }
  }

  async function submitRun() {
    if (!detail || !settings.enabled || !settings.capabilities.includes(capability) || capability !== 'investigation' || busy || checkingInvestigation || uncertain) return;
    try {
      const input = JSON.parse(runInput) as DevOpsRunInput['devops_input'];
      const refs: RecordReference[] = [
        ...((input.resource_refs as RecordReference[] | undefined) ?? []),
        ...((input.telemetry_refs as RecordReference[] | undefined) ?? []),
      ];
      if (input.kind !== capability || (capability === 'investigation' && (!String(input.question ?? '').trim() || !Array.isArray(input.resource_refs) || !input.resource_refs.length))
        || refs.some(ref => ref.tenant_id !== scope.tenant_id || ref.project_id !== projectId)) throw new Error();
      if (!ready(capability) && !await checkInvestigation()) return;
      await startRun({ service_id: 'devops', configuration_revision: configuration.value.revision, binding_id: detail.value.binding_id,
        capability, devops_input: input });
    } catch { setError('Choose a typed run input and scoped resource references for this capability.'); }
  }

  async function checkNative() {
    if (!detail || busy || checkingNative || readbackFailed) return;
    invalidateNative(); const request = nativeGeneration.current; const readGeneration = generation.current;
    setCheckingNative(true); setError('');
    try {
      const value = await checkNativeEligibility(productId, projectId, detail.value.binding_id, configuration.value.revision);
      if (request !== nativeGeneration.current || readGeneration !== generation.current) return;
      if (!nativeEligibilityMatches(value, configuration, detail)) throw new Error();
      setNativeCheck({ value, configuration, binding: detail });
    } catch (checkError) { if (request === nativeGeneration.current && readGeneration === generation.current) setNativeError(nativeAccessError(checkError)); }
    finally { if (request === nativeGeneration.current) setCheckingNative(false); }
  }

  async function launch() {
    if (!detail || !ready('native_ui') || busy) return;
    invalidateNative();
    setBusy(true); setError('');
    const request = generation.current;
    const observed = evidence?.subject === 'native_ui' ? evidence : null;
    try {
      if (observed && (evidenceAttempts.current.has(observed.challenge_id) || evidenceLaunch || Date.parse(observed.expires_at) <= Date.now())) throw new Error('Read the original launch receipt');
      if (observed) evidenceAttempts.current.add(observed.challenge_id);
      const response = observed
        ? await launchDevOpsBrowser(productId, projectId, detail.value.binding_id, configuration.value.revision, observed.challenge_id)
        : await launchDevOpsBrowser(productId, projectId, detail.value.binding_id, configuration.value.revision);
      if (request !== generation.current || detail.value.binding_id !== selection.current) return;
      if (response.binding_id !== detail.value.binding_id || !response.launch_url.startsWith('https://signin.aws.amazon.com/federation?')
        || Date.parse(response.expires_at) <= Date.now()) throw new Error();
      if (observed) {
        if (!response.observation_receipt_id) throw new Error('Launch receipt unavailable');
        setEvidenceLaunch(response.observation_receipt_id);
        setIssuedLaunchUrl({ url: response.launch_url, expiresAt: response.expires_at });
        setMessage('Your SSO launch is ready. Continue to the native UI, then return here to record your arrival.');
      } else window.location.assign(response.launch_url);
    } catch (launchError) { if (request === generation.current) { setNativeError(observed ? 'Read the original observation launch receipts before further action. Do not repeat an uncertain SSO launch.' : nativeAccessError(launchError)); } }
    finally { if (request === generation.current) setBusy(false); }
  }

  async function preventionAction(action: 'enable' | 'pause' | 'resume') {
    if (!detail || !prevention || busy || action !== 'pause') return;
    setBusy(true); setError('');
    invalidateInvestigation();
    const request = generation.current;
    try {
      const updated = await configureDevOpsPrevention(productId, projectId, detail.value.binding_id, configuration.value.revision, action, detail.etag);
      if (request === generation.current) { setPrevention(updated); setReadbackFailed(true); setMessage(`Prevention ${action} requested. Check observed schedule before treating it as active.`); }
    } catch (changeError) { if (request === generation.current) { if (denied(changeError)) fail(changeError); else { setReadbackFailed(true); setError(stale(changeError) ? 'Prevention revision changed. Refresh binding before another action.' : 'Prevention change unconfirmed. Refresh readback before another action.'); } } }
    finally { if (request === generation.current) setBusy(false); }
  }

  async function refreshRun(run: DevOpsRun) {
    const request = generation.current;
    try {
      const current = await getDevOpsRun(productId, projectId, run.run_id);
      if (request !== generation.current) return;
      if (!matches(current.value) || current.value.run_id !== run.run_id) throw { status: 403 };
      setRuns(existing => existing.map(item => item.run_id === run.run_id && current.value.revision >= item.revision ? current.value : item));
    } catch (readError) { if (request === generation.current) fail(readError); }
  }

  async function recoverRun(run: DevOpsRun) {
    if (busy || readbackFailed || !run.native_job_ref || run.origin !== 'adp_submitted'
      || run.input?.capability !== 'investigation' || !['outcome_unknown', 'expired'].includes(run.state)) return;
    const request = generation.current;
    setBusy(true); setError('');
    try {
      // The same displayed revision reuses its receipt after a lost response.
      const late = run.state === 'expired' || Date.parse(run.deadline_at) <= Date.now();
      const current = await reconcileDevOpsRun(productId, projectId, run.run_id, `devops-report-${run.run_id}-${run.revision}`,
        late ? { revision: run.revision, native_job_ref: run.native_job_ref } : undefined);
      if (request !== generation.current) return;
      if (!matches(current.value) || current.value.run_id !== run.run_id
        || !sameConsentScope(current.value.native_job_ref, run.native_job_ref)) throw { status: 403 };
      setRuns(existing => existing.map(item => item.run_id === run.run_id && current.value.revision >= item.revision ? current.value : item));
      setMessage('Checking the existing investigation and collecting its report. Progress updates automatically.');
    } catch (recoveryError) {
      if (request !== generation.current) return;
      if (denied(recoveryError)) fail(recoveryError);
      else setError('Report recovery was not confirmed. Refresh this run, then retry Collect existing report. Your original investigation is retained.');
    } finally { if (request === generation.current) setBusy(false); }
  }

  async function retryPublication() {
    if (!publication || !selectedResult || busy) return;
    const request = generation.current;
    const requested = publication;
    const resultId = selectedResult;
    try {
      const updated = await retryDevOpsPublication(productId, projectId, requested.publication_id, 'Retry scoped Context publication');
      if (request !== generation.current) return;
      if (updated.publication_id !== requested.publication_id || updated.result_id !== resultId) throw { status: 403 };
      setPublication(current => current?.publication_id === requested.publication_id && current.result_id === resultId ? updated : current);
    } catch (readError) { if (request === generation.current) fail(readError); }
  }

  async function inspectResult(result: DevOpsResult) {
    const request = ++generation.current;
    invalidatePages(); setFindingCursor(undefined);
    setSelectedResult(result.result_id); setFindings([]); setPublication(null); setError('');
    try {
      const found = await listDevOpsFindings(productId, projectId, result.result_id);
      if (request !== generation.current) return;
      if (found.items.some(item => item.source_refs.some(ref => ref.tenant_id !== scope.tenant_id || ref.project_id !== projectId))) throw { status: 403 };
      setFindings(found.items); setFindingCursor(found.next_cursor);
      if (result.publication_id) {
        try {
          const published = await getDevOpsPublication(productId, projectId, result.publication_id);
          if (request !== generation.current) return;
          if (published.publication_id !== result.publication_id || published.result_id !== result.result_id) throw { status: 403 };
          setPublication(published);
        } catch (readError) {
          if (request !== generation.current) return;
          if (denied(readError)) { fail(readError); return; }
          setError('Publication status is unavailable. Retained findings and report access remain below. Reopen this result to retry publication readback; publication success is unconfirmed.');
        }
      }
    } catch (readError) { if (request === generation.current) fail(readError); }
  }

  async function loadMore(kind: keyof typeof paging) {
    const cursor = kind === 'runs' ? runCursor : kind === 'results' ? resultCursor : findingCursor;
    if (!cursor || loading || pageRequests.current[kind] || (kind === 'findings' && !selectedResult)) return;
    const request = generation.current;
    const token = Symbol(kind);
    pageRequests.current[kind] = token;
    setPaging(current => ({ ...current, [kind]: true })); setError('');
    try {
      if (kind === 'runs') {
        const page = await listDevOpsRuns(productId, projectId, cursor);
        if (request !== generation.current || pageRequests.current[kind] !== token) return;
        if (page.items.some(item => !matches(item))) throw { status: 403 };
        setRuns(current => appendReadbackPage(current, page.items, item => item.run_id)); setRunCursor(page.next_cursor);
      } else if (kind === 'results') {
        const page = await listDevOpsResults(productId, projectId, cursor);
        if (request !== generation.current || pageRequests.current[kind] !== token) return;
        if (page.items.some(item => !matches(item))) throw { status: 403 };
        setResults(current => appendReadbackPage(current, page.items, item => item.result_id)); setResultCursor(page.next_cursor);
      } else {
        const page = await listDevOpsFindings(productId, projectId, selectedResult, cursor);
        if (request !== generation.current || pageRequests.current[kind] !== token) return;
        if (page.items.some(item => item.source_refs.some(ref => ref.tenant_id !== scope.tenant_id || ref.project_id !== projectId))) throw { status: 403 };
        setFindings(current => appendReadbackPage(current, page.items, item => item.finding_id)); setFindingCursor(page.next_cursor);
      }
    } catch (readError) { if (request === generation.current && pageRequests.current[kind] === token) fail(readError); }
    finally {
      if (pageRequests.current[kind] === token) {
        delete pageRequests.current[kind]; setPaging(current => ({ ...current, [kind]: false }));
      }
    }
  }

  // The list can display saved workspace identity without claiming fresh provider readiness.
  const workspaces = bindings.filter(item => item.configuration_id === configuration.value.configuration_id
    && !['cleaned', 'cleanup_pending', 'unlinked'].includes(item.state));
  const savedWorkspace = detail?.value ?? (workspaces.length === 1 ? workspaces[0] : null);
  const nativeAccess = <aside className="devops-native-access" aria-label="AWS DevOps Agent sign-in">
    <div className="devops-panel-heading"><div><h4>Open DevOps workspace</h4><p>Open the agent’s own application using your ADP sign-in.</p></div>
      <button className="product-primary" type="button" disabled={!ready('native_ui') || busy} onClick={() => void launch()}>Open AWS DevOps Agent</button></div>
    {workspaces.length === 1 && <button type="button" disabled={loading || busy || Boolean(uncertain)} onClick={() => void inspectBinding(workspaces[0].binding_id)}>Review workspace access</button>}
    {!detail && workspaces.length > 1 && <label>DevOps workspace <select value={bindingId} disabled={loading || busy || Boolean(uncertain)} onChange={event => void inspectBinding(event.target.value)}>
      <option value="">Choose a saved DevOps workspace</option>{workspaces.map((item, index) => <option key={item.binding_id} value={item.binding_id}>Workspace {index + 1} · {item.provider_identity?.region ?? 'saved environment'} · {item.state}</option>)}</select></label>}
    <div className="devops-actions"><button type="button" disabled={!detail || busy || checkingNative || readbackFailed || Boolean(uncertain)} onClick={() => void checkNative()}>{checkingNative ? 'Checking access…' : 'Check native UI access'}</button>
      <p className="product-muted">{ready('native_ui') ? 'SSO access confirmed. Opening rechecks your ADP session and permission for this space.' : settings.devops.browser_launch_policy === 'disabled' ? 'Workspace sign-in is not configured. A platform administrator must enable the AWS operator app and scoped SSO roles; the Product owner then grants sign-in access.' : 'Review this workspace and check access before opening it with your ADP sign-in.'}</p></div>
    {nativeError && <p role="alert">{nativeError} <a href="#l5-devops-setup">Review DevOps workspace access</a>.</p>}
    {issuedLaunchUrl && <button type="button" onClick={() => { if (evidence && Date.parse(evidence.expires_at) > Date.now() && Date.parse(issuedLaunchUrl.expiresAt) > Date.now()) window.open(issuedLaunchUrl.url, '_blank', 'noopener,noreferrer'); else setNativeError('The issued SSO launch expired. Review access again.'); }}>Continue with issued SSO launch</button>}
  </aside>;
  const status = <>
    {loading && <p role="status">Loading DevOps bindings, runs and results…</p>}
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {!children && nativeAccess}
  </>;
  const retryReceipt = (kind: 'binding' | 'run') => uncertain?.kind === kind && !uncertain.observationChallenge && <button type="button" disabled={busy || (kind === 'binding' && (staleBindingReceipt
    || (!(uncertain.request as BindingRequest).binding_id && (loading || readbackFailed || !setupRestored || Boolean(setupStorageError)))))} onClick={() => {
    const pending = uncertain;
    void (pending.kind === 'binding' ? startBinding(pending.request as BindingRequest, pending.receipt, true) : startRun(pending.request as DevOpsRunInput, pending.receipt, true));
  }}>Retry original receipt</button>;
  const setup = <>
    {!children && <div className="devops-panel-heading"><div><h3>DevOps workspace</h3><p>The AWS DevOps Agent space linked to this Product. Use your ADP sign-in when workspace access is enabled.</p></div></div>}
    {!children && savedWorkspace && <dl className="devops-saved-settings" aria-label="Saved DevOps workspace">
      <div><dt>Workspace ID</dt><dd>{savedWorkspace.native_resource_ref?.record_id ?? 'Not assigned yet'}<small>{savedWorkspace.ownership === 'adp_created' ? 'Created by ADP' : 'Linked to ADP'}</small></dd></div>
      <div><dt>AWS account and region</dt><dd>{savedWorkspace.provider_identity?.account_id ?? 'Not reported'}<small>{savedWorkspace.provider_identity?.region ?? settings.devops.region}</small></dd></div>
      <div><dt>Saved workspace status</dt><dd><DevOpsBadge value={savedWorkspace.state} /><small>Current run and sign-in access are checked separately.</small></dd></div>
    </dl>}
    <div className="devops-connection-setup">
    {!settings.enabled && <>
    <h4>Connect a DevOps workspace</h4>
    <p>Save an Investigation setup draft, then review its owner consent before creating or linking the space. Provider setup can incur costs.</p>
    {setupTarget?.action === 'link' && <p>Saved existing space: {setupTarget.native_resource_ref.record_id}; hosting connection: {setupTarget.native_resource_ref.source_connection_id}. Adoption reads and links this space, preserving customer ownership. It does not create a replacement space.</p>}
    {!setupDraft && <p role="status">Space setup is unavailable for this saved selection. Prepare the disabled Investigation setup draft described above, including an exact space ID and hosting connection for adoption. Existing bindings remain available for inspection.</p>}
    <details><summary>Owner setup and activation steps</summary>
      <ol><li>Use Owner consent above to review the saved creation or adoption scope and grant it with an explicit expiry.</li>
      <li>Request the selected setup here, then inspect the retained operation until it succeeds. If the response is lost, keep the original receipt and request; do not start another setup.</li>
      <li>Use Verify resources and activate Investigation in Setup to verify the selected Lambda and log group, then activate the space with bounded run limits. Activation requires fresh native evidence that its goals are disabled.</li>
      <li>Grant execution consent for the new configuration revision, then refresh configuration and binding readback here.</li></ol>
      <p>Use Owner consent above for setup and execution grants, and Verify resources and activate Investigation below for resource consent and activation. Setup consent does not authorize investigations or native UI access.</p>
    </details>
    </>}
    <p>{workspaces.length ? `${workspaces.length} saved workspace${workspaces.length === 1 ? '' : 's'}` : 'No DevOps workspace is linked to this configuration. Review the saved configuration and owner consent before setup.'}</p>
    {setupStorageError && setupDraft && <p role="alert">{setupStorageError}</p>}
    <button type="button" disabled={busy || !setupStorageKey} onClick={readRetainedSetup}>Read retained setup receipt</button>
    {!settings.enabled && <button type="button" disabled={loading || readbackFailed || busy || Boolean(uncertain) || !setupDraft || setupBlocked} onClick={() => {
      if (setupTarget) void startBinding({ ...setupTarget, configuration_revision: configuration.value.revision });
    }}>{settings.devops?.space_mode === 'adopt' ? 'Link existing DevOps space' : 'Create provider binding'}</button>}
    {setupReceipt && <div aria-label="Original binding request">
      <p>Original setup receipt: {setupReceipt.key}. Action: {setupReceipt.request.action}; saved configuration revision: {setupReceipt.request.configuration_revision}; receipt: {setupReceipt.state}.</p>
      {setupReceipt.request.native_resource_ref && <p>Original existing space: {setupReceipt.request.native_resource_ref.record_id}.</p>}
      {setupReceipt.state === 'pending' && <p>The original setup request is retained in this owner's session. Read current DevOps readback, then explicitly continue using the same receipt. Reloading does not submit it.</p>}
      {setupReceipt.configurationKey !== bindingConfigurationKey && <p>The saved configuration or space changed. Replay is blocked. Reconcile the original operation with this receipt before granting consent or requesting setup for another selection.</p>}
    </div>}
    {uncertain?.kind === 'binding' && !setupReceipt && <div aria-label="Original binding request">
      <p>Original setup receipt: {uncertain.receipt}. Action: {(uncertain.request as BindingRequest).action}; saved configuration revision: {uncertain.request.configuration_revision}.</p>
      {(uncertain.request as BindingRequest).native_resource_ref && <p>Original existing space: {(uncertain.request as BindingRequest).native_resource_ref!.record_id}.</p>}
      {staleBindingReceipt && <p>The saved configuration or space changed. Replay is blocked. Reconcile the original operation with this receipt before granting consent or requesting setup for another selection.</p>}
    </div>}
    {retryReceipt('binding')}
    </div>
    {operation && <div>Binding operation {operation.operation_id}: {operation.state}. {operation.gaps.join(', ')}
      <button type="button" disabled={busy} onClick={() => void inspectSetupOperation()}>Check operation</button>
    </div>}
    {workspaces.length > 1 && <label>Saved DevOps workspace <select value={bindingId} disabled={busy} onChange={event => void inspectBinding(event.target.value)}><option value="">Select workspace to inspect access</option>{workspaces.map((item, index) => <option key={item.binding_id} value={item.binding_id}>Workspace {index + 1} · {item.provider_identity?.region ?? settings.devops.region} · {item.state}</option>)}</select></label>}
    {detail && <div>
      <p>Binding {detail.value.binding_id}: {detail.value.state}; ownership: {detail.value.ownership}; generation {detail.value.generation}.</p>
      <p>Binding gaps: {detail.value.gaps.join(', ') || 'none reported'}</p>
      <ul>{detail.value.assets?.map((item, index) => <li key={index}>Asset {item.kind}: {item.status}{item.reason ? ` (${item.reason})` : ''}</li>)}</ul>
      <button type="button" disabled={readbackFailed || busy || Boolean(uncertain)} onClick={() => void startBinding({ action: 'check', configuration_revision: configuration.value.revision, binding_id: detail.value.binding_id })}>Check binding</button>
    </div>}
  </>;
  const capabilities = <>
    <h3>Capability readiness</h3>
    {detail ? <ul>{detail.value.readiness.map((item, index) => <li key={`${item.capability ?? item.kind ?? 'observation'}:${index}`}>{securityReadinessSummary([item])}; {item.capability ? ready(item.capability) ? 'current' : 'stale or unverified' : 'observation only; capability readiness is not confirmed'}</li>)}</ul>
      : <p>Select a provider binding in Setup to check which capabilities are ready.</p>}
  </>;
  const runControls = <>
    <section className="devops-panel" aria-label="New investigation">
    <div className="devops-panel-heading"><div><h3>Start an investigation</h3><p>Ask a question about your saved workload. ADP collects the findings here.</p></div><button type="button" disabled={loading || busy || Boolean(uncertain)} onClick={() => void refresh()}>Refresh status</button></div>
    {!settings.enabled && <p className="devops-next-step">Finish workload setup and execution consent in <a href="#l5-devops-setup">Setup</a> to enable investigations.</p>}
    {(!detail || bindings.length > 1) && <label>Investigation connection <select value={bindingId} disabled={loading || busy || Boolean(uncertain)} onChange={event => void inspectBinding(event.target.value)}>
      <option value="">Choose a saved DevOps connection</option>{bindings.map((item, index) => <option key={item.binding_id} value={item.binding_id}>
        Connection {index + 1} · {item.provider_identity?.region ?? 'saved environment'} · {item.state}
      </option>)}</select></label>}
    {retryReceipt('run')}
    {detail ? <div className="space-y-4">

      <DevOpsInvestigationInput configuration={configuration.value} bindingId={detail.value.binding_id}
        value={runInput} onChange={setRunInput} disabled={busy || checkingInvestigation || readbackFailed || Boolean(uncertain)} />
      <details>
        <summary>Advanced run settings</summary>
        <p>Inspect or edit the exact question, workload and telemetry references for this run.</p>
        <button type="button" disabled={busy || checkingInvestigation || readbackFailed || !settings.enabled} onClick={() => void checkInvestigation()}>Check Investigation readiness</button>
        <label>Typed run input (JSON) <textarea value={runInput} onChange={event => setRunInput(event.target.value)} /></label>
      </details>
      <div className="devops-actions">

        <button className="product-primary" type="button" disabled={!settings.enabled || !settings.capabilities.includes(capability) || readbackFailed || busy || checkingInvestigation || Boolean(error) || Boolean(currentInvestigation && !ready(capability) && !currentInvestigation.gaps.includes('readiness_expired')) || Boolean(uncertain)} aria-label="Start investigation" onClick={() => void submitRun()}>{checkingInvestigation ? 'Checking access…' : busy ? 'Starting investigation…' : 'Start investigation'}</button>
        <RunAccess service="devops" configuration={configuration} bindingId={bindingId} disabled={busy || checkingInvestigation || Boolean(uncertain) || following} onReady={value => { onConfigurationChanged?.(value as Versioned<DevOpsConfiguration>); void refresh(); }} />
      </div>
      {following && <p role="status">An investigation is already active. Follow its progress below before starting another.</p>}
      {currentInvestigation ? <div role="status" className="devops-readiness"><p>{ready('investigation') ? 'Ready to start. Access is checked again when you submit.' : 'Not ready to start. Resolve the issues below, then check again.'}</p>
        {currentInvestigation.gaps.map(code => <p key={code}>{investigationGapAction(code)} <small>({code})</small></p>)}
        {!currentInvestigation.gaps.length && !ready('investigation') && <p>This readiness check has expired. Check Investigation readiness again.</p>}
      </div> : <p className="product-muted">Starting checks access first. Progress and results appear below.</p>}
    </div> : <p>Choose a saved workspace above, or <a href="#l5-devops-setup">complete Setup</a> to connect one.</p>}
    </section>
    <div className="devops-panel-heading"><h3>Run history</h3>{following && <span className="devops-badge devops-badge--active">Updating automatically</span>}</div>
    {progressError && <p role="status">{progressError}</p>}
    {!runs.length && <p>No DevOps runs are available in the current readback.</p>}
    <ul className="devops-history">{runs.map(item => <li key={item.run_id} aria-label={`Run ${item.run_id}: ${item.state}; origin ${item.origin ?? 'ADP submitted'}`}>
      <div className="devops-panel-heading"><strong>{typeof item.input?.devops_input?.question === 'string' ? item.input.devops_input.question : devOpsCapability(item.input?.capability)}</strong><DevOpsBadge value={item.state} /></div>
      <p className="product-muted">{devOpsDate(item.admission?.admitted_at)} · {item.origin === 'native' ? 'Started in AWS' : 'Started in ADP'}</p>
      {['outcome_unknown', 'expired', 'blocked', 'failed'].includes(item.state) && item.monitoring?.last_poll_error && <p role="status" className="devops-next-step">
        {item.monitoring.last_poll_error.message ?? 'ADP could not finish reading this investigation.'}{' '}
        {item.monitoring.last_poll_error.action ?? item.monitoring.last_poll_error.next_action}
      </p>}
      {item.monitoring?.state === 'recovery_pending' && <p role="status">Collecting the existing investigation report…</p>}
      <div className="devops-actions"><button type="button" onClick={() => void refreshRun(item)}>Refresh run</button>
        {item.origin === 'adp_submitted' && item.input?.capability === 'investigation' && item.native_job_ref
          && ['outcome_unknown', 'expired'].includes(item.state) && <button type="button"
            disabled={busy || readbackFailed || item.monitoring?.state === 'recovery_pending'} onClick={() => void recoverRun(item)}>Collect existing report</button>}
      {item.state === 'expired' && item.origin === 'adp_submitted' && item.native_job_ref
        && item.input?.capability === 'investigation' && <p className="devops-muted">Collecting grants up to 15 minutes of read-only access to this existing report.</p>}
        {results.some(result => result.run_id === item.run_id) && <a href="#l5-devops-results">Review results</a>}</div>
      <details><summary>Run details</summary><p className="devops-technical">{item.run_id}</p></details>
    </li>)}</ul>
    {runCursor && <button type="button" disabled={paging.runs || loading} onClick={() => void loadMore('runs')}>Load more DevOps runs</button>}
    <div className="devops-secondary-actions">
      <aside className="devops-panel" aria-label="Scheduling availability"><h3>Schedules</h3><p>Scheduled investigations are not available yet. Start an investigation manually using the saved setup.</p></aside>
      {detail && preventionConfigured && <>
      <ServiceDisclosure title="Prevention schedule" hint="New schedules are not available yet">
      <p>New prevention schedules: Not available yet. Native goal controls exist, but activation of a prevention configuration is not supported yet. Existing schedule status remains visible; an authorized owner can request a pause.</p>
      {preventionError && <p role="status">{preventionError}</p>}
      {prevention && <div><p>Prevention: {prevention.status}; desired: {prevention.desired.enabled ? 'enabled' : 'disabled'}; observed: {prevention.observed ? 'read back' : 'not observed'}. {prevention.reason ?? ''}</p>
        {(['enable', 'pause', 'resume'] as const).map(action => <button type="button" key={action} disabled={readbackFailed || busy || action !== 'pause'} onClick={() => void preventionAction(action)}>{action} prevention</button>)}
      </div>}
      </ServiceDisclosure>
</>}
    </div>
  </>;
  const resultReadback = <>
    {!results.length && <p>No DevOps results are available in the current readback.</p>}
    <div className="devops-panel-heading"><h3>Investigation results</h3><span className="product-muted">{results.length} report{results.length === 1 ? '' : 's'}</span></div>
    <ul className="devops-history">{results.map(item => <li key={item.result_id} aria-label={`Result ${item.result_id}: ${item.coverage} coverage; origin ${item.origin ?? 'ADP submitted'}`}>
      <div className="devops-panel-heading"><strong>{(() => { const question = runs.find(run => run.run_id === item.run_id)?.input?.devops_input?.question; return typeof question === 'string' ? question : 'Investigation report'; })()}</strong><DevOpsBadge value={item.provider_outcome ?? 'unknown'} /></div>
      <p className="product-muted">{devOpsDate(item.observed_at)} · {item.coverage} coverage · {item.origin === 'native' ? 'Started in AWS' : 'Started in ADP'}</p>
      <div className="devops-actions"><button type="button" className="product-primary" aria-pressed={selectedResult === item.result_id} onClick={() => void inspectResult(item)}>View result</button></div>
      <details><summary>Result details</summary><p className="devops-technical">{item.result_id}</p></details>
    </li>)}</ul>
    {resultCursor && <button type="button" disabled={paging.results || loading} onClick={() => void loadMore('results')}>Load more DevOps results</button>}
    {selectedResult && <section className="devops-panel" aria-label={`Result ${selectedResult}${findings.length ? `: ${findings.length} findings` : ''}`}><h3>Investigation output</h3>
      {findings.length > 0 && <p className="product-muted">{findings.length} findings in the selected report.</p>}
      {results.filter(item => item.result_id === selectedResult && item.evidence_ref).map(item =>
        <DevOpsInvestigationReport key={`${scope.tenant_id}:${productId}:${projectId}:${item.result_id}:${item.revision}`}
          productId={productId} projectId={projectId} scope={scope} result={item} onAccessDenied={reportAccessDenied} />)}
      <ul>{findings.map(item => <li key={item.finding_id}>{item.claim}: {item.validation}</li>)}</ul>
      {findingCursor && <button type="button" disabled={paging.findings || loading} onClick={() => void loadMore('findings')}>Load more DevOps findings</button>}
      {publication && <p>Context publication {publication.publication_id}: {publication.state}.{' '}
        {['retry_wait', 'unavailable'].includes(publication.state) && <button type="button" disabled={busy} onClick={() => void retryPublication()}>Retry publication</button>}
      </p>}
    </section>}
    {detail && <ServiceDisclosure title="Native events and evidence" hint="Inspect event records and advanced verification">
      <p>Native-origin events: {events.length || 'none observed'}.</p>
      <ul>{events.map(item => <li key={item.event_id}>{item.event_id}: {item.state} (native event){item.result_ref ? `; result ${item.result_ref.record_id}` : ''}</li>)}</ul>
      <DevOpsReleaseEvidence productId={productId} projectId={projectId} bindingId={detail.value.binding_id} configurationId={configuration.value.configuration_id} revision={configuration.value.revision} runId={evidenceRun} launchReceipt={evidenceLaunch} onSelect={selectEvidence} onNativeArrival={setNativeArrival} />
      <DevOpsKnowledgeEvidence key={`${detail.value.binding_id}:${detail.value.generation}:${configuration.value.revision}`} selection={{ tenant_id: scope.tenant_id, product_id: productId, project_id: projectId, configuration_id: configuration.value.configuration_id, configuration_revision: configuration.value.revision, binding_id: detail.value.binding_id, binding_generation: detail.value.generation }} disabled={configuration.value.state === 'disabled'} nativeArrival={nativeArrival} onAccessDenied={onAccessDenied} />
    </ServiceDisclosure>}
  </>;
  const nativeConsent = detail && <DevOpsNativeConsent key={`${configuration.value.configuration_id}:${configuration.value.revision}:${configuration.value.hash}:${detail.value.binding_id}:${detail.value.revision}`}
        productId={productId} projectId={projectId} configuration={configuration} binding={detail} disabled={busy || readbackFailed} onChange={invalidateNative} onAttention={setNativeConsentNotice} />;
  const sections = { status, setup, capabilities, nativeConsent, nativeAccess,
    workspace: { saved: savedWorkspace, nativeReady: ready('native_ui'), busy: loading || busy,
      open: () => { void launch(); }, notice: nativeError || nativeConsentNotice || (uncertain?.kind === 'binding' || setupReceipt?.state === 'pending'
        ? 'Workspace setup has an unfinished request. Review it before starting another setup.' : '') },
    runs: runControls, results: resultReadback };
  if (children) return children(sections);
  return <section aria-label="DevOps operations" className="devops-workspace devops-panel space-y-4">
    <h2>DevOps operations</h2>
    {status}
    <section aria-label="DevOps setup">{setup}{nativeConsent}</section>
    <section aria-label="DevOps capabilities">{capabilities}</section>
    <section aria-label="DevOps runs and schedules">{runControls}</section>
    <section aria-label="DevOps results">{resultReadback}</section>
  </section>;
}
