import L5OwnerConsent from './L5OwnerConsent';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import {
  getWellArchitectedConfiguration, getWellArchitectedOptions,
  saveWellArchitectedConfiguration, validateWellArchitectedProviderSelection, type WellArchitectedProviderSelection,
  type WellArchitectedConfiguration, type WellArchitectedOptions,
  type WellArchitectedSettings, type Versioned,
} from '@/services/l5Services';
import type { RecordReference } from '@/services/products';
import WellArchitectedRunsResults from './WellArchitectedRunsResults';
import WellArchitectedScheduleEditor, { sameScheduleTriggers, type SaveWellArchitectedSchedules } from './WellArchitectedScheduleEditor';
import { providerAction, sameReference, scopedReference, validProviderInput, validProviderSelection } from './wellArchitectedProvider';
import { CapabilitySummary, ProfileSetupSummary, ServiceJourney, ServiceSection, ServiceDisclosure } from './L5ServiceJourney';
import SecurityLimits, { limitsDraft, parseLimits } from './SecurityLimits';

interface Props { productId: string; projectId: string; tenantId: string }
const pillars: WellArchitectedSettings['pillars'] = [
  'COST_OPTIMIZATION', 'SECURITY', 'RESILIENCE', 'PERFORMANCE', 'OPERATIONAL_EXCELLENCE',
];
const statusOf = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
const setupGuidance: Record<string, string> = {
  provider_unqualified: 'Review readiness has not been confirmed. Complete and save the source-only provider setup, review owner consent, then set up a binding and check review readiness.',
  capability_not_selected: 'Select Well-Architected in the Product capabilities, then refresh these setup choices.',
  repository_revision_unpinned: 'Pin the repository source to a commit in the Product, then refresh and select that revision.',
  source_coverage_incomplete: 'The authorized source list is incomplete. Refresh the Product sources before selecting review inputs.',
  connection_revision_unavailable: 'Verify an authorized AWS connection for this Product and project, then refresh and select its current revision.',
  source_options_unavailable: 'Add an authorized document or pinned repository source to the Product, then refresh these setup choices.',
  region_options_unavailable: 'Choose the AWS region for the selected connection and enter it in Provider region; suggested regions are unavailable.',
  catalog_mapping_unresolved: 'Ask the platform administrator to complete the Well-Architected capability mapping for this Product, then refresh these setup choices.',
};

export default function WellArchitectedServiceTab(props: Props) {
  // Scope changes remove all prior drafts and fence pending responses immediately.
  return <ScopedWellArchitectedServiceTab key={JSON.stringify([props.tenantId, props.productId, props.projectId])} {...props} />;
}

function ScopedWellArchitectedServiceTab({ productId, projectId, tenantId }: Props) {
  const [options, setOptions] = useState<WellArchitectedOptions | null>(null);
  const [configuration, setConfiguration] = useState<Versioned<WellArchitectedConfiguration> | null>(null);
  const [selectedSources, setSelectedSources] = useState<RecordReference[]>([]);
  const [selectedPillars, setSelectedPillars] = useState<WellArchitectedSettings['pillars']>([]);
  const [connection, setConnection] = useState('');
  const [region, setRegion] = useState('');
  const [reason, setReason] = useState('');
  const [classification, setClassification] = useState('');
  const [classificationTouched, setClassificationTouched] = useState(false);
  const [limits, setLimits] = useState(limitsDraft());
  const [limitsTouched, setLimitsTouched] = useState(false);
  const [executionRole, setExecutionRole] = useState('');
  const [sourceBucket, setSourceBucket] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [selection, setSelection] = useState<WellArchitectedProviderSelection | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const [validating, setValidating] = useState(false);
  const operation = useRef(0);
  const providerGeneration = useRef(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [conflicted, setConflicted] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const previouslyRead = useRef(false);

  const scoped = useCallback((ref: unknown) => scopedReference(ref, tenantId, projectId), [tenantId, projectId]);
  const validConfiguration = useCallback((value: WellArchitectedConfiguration) => {
    const settings = value.settings;
    const review = settings.well_architected;
    const role = review?.execution_role_ref;
    const store = review?.source_store_ref;
    return value.scope.tenant_id === tenantId && value.scope.product_id === productId && value.scope.project_id === projectId
      && settings.service_id === 'well_architected' && scoped(settings.profile_ref)
      && settings.profile_ref.record_type === 'product' && settings.profile_ref.record_id === productId
      && Array.isArray(settings.source_refs) && settings.source_refs.every(scoped)
      && (!settings.connection_ref || (scoped(settings.connection_ref) && settings.connection_ref.record_type === 'connection'))
      && (!settings.limits?.policy_ref || scoped(settings.limits.policy_ref))
      && review?.review_mode === 'source_only' && Array.isArray(review.goal_refs) && review.goal_refs.every(ref => scoped(ref) && ref.record_type === 'document')
      && (!role && !store || !!settings.connection_ref && !!role && !!store && validProviderInput({ connection_ref: settings.connection_ref,
        region: settings.region ?? '', execution_role_arn: role.record_id, source_bucket: store.record_id })
        && validProviderSelection({ provider_contacted: true, native_qualified: false, execution_role_ref: role, source_store_ref: store }, {
          connection_ref: settings.connection_ref, region: settings.region ?? '', execution_role_arn: role.record_id, source_bucket: store.record_id,
        }))
      && (settings.triggers ?? []).every(trigger => trigger.source_refs.every(scoped));
  }, [tenantId, productId, projectId, scoped]);
  const invalidateProvider = useCallback(() => {
    providerGeneration.current += 1;
    setSelection(null); setReviewed(false); setValidating(false); setMessage('');
  }, []);
  const deny = useCallback(() => {
    operation.current += 1;
    invalidateProvider();
    setOptions(null); setConfiguration(null); setSelectedSources([]); setSelectedPillars([]);
    setConnection(''); setRegion(''); setExecutionRole(''); setSourceBucket(''); setReason(''); setEnabled(false); setBusy(false);
    setClassification(''); setClassificationTouched(false); setLimits(limitsDraft()); setLimitsTouched(false);
    setRevoked(true); setError('Well-Architected access is unavailable. Previously shown details were removed.');
  }, [invalidateProvider]);

  const load = useCallback(async (preserveDraft = false, draftConnection?: RecordReference) => {
    const request = ++operation.current;
    invalidateProvider();
    setBusy(true); setError(''); setMessage('');
    try {
      const available = await getWellArchitectedOptions(productId, projectId);
      if (request !== operation.current) return;
      if ([...available.sources, ...available.connections].some(ref => !scoped(ref))
        || available.connections.some(ref => ref.record_type !== 'connection' || ref.record_id !== ref.source_connection_id)) throw { status: 403 };
      let current: Versioned<WellArchitectedConfiguration> | null = null;
      try { current = await getWellArchitectedConfiguration(productId, projectId); }
      catch (readError) { if (statusOf(readError) !== 404 || previouslyRead.current) throw readError; }
      if (request !== operation.current) return;
      if (current && !validConfiguration(current.value)) throw { status: 403 };
      if (current) previouslyRead.current = true;
      setOptions(available); setConfiguration(current);
      const selectedConnection = preserveDraft ? draftConnection : current?.value.settings.connection_ref;
      const index = available.connections.findIndex(ref => sameReference(ref, selectedConnection));
      setConnection(index >= 0 ? String(index) : '');
      if (!preserveDraft) {
        setSelectedSources(current?.value.settings.source_refs ?? []);
        setSelectedPillars(current?.value.settings.well_architected.pillars ?? []);
        setRegion(current?.value.settings.region ?? '');
        setClassification(current?.value.settings.classification ?? ''); setClassificationTouched(false);
        setLimits(limitsDraft(current?.value.settings.limits)); setLimitsTouched(false);
        setExecutionRole(current?.value.settings.well_architected.execution_role_ref?.record_id ?? '');
        setSourceBucket(current?.value.settings.well_architected.source_store_ref?.record_id ?? '');
        setEnabled(current?.value.settings.enabled ?? false); setReason('');
      } else setMessage('Latest saved configuration loaded. Your draft is preserved; review it and validate provider resources again before saving.');
      setRevoked(false); setConflicted(false);
    } catch (loadError) {
      if (request !== operation.current) return;
      if (statusOf(loadError) === 403 || statusOf(loadError) === 404) deny();
      else {
        setConflicted(true);
        setError(`${providerAction(loadError) ?? 'Well-Architected configuration could not be loaded.'} Refresh successfully before saving or starting work; your draft and retained results are preserved.`);
      }
    } finally { if (request === operation.current) setBusy(false); }
  }, [productId, projectId, scoped, validConfiguration, deny, invalidateProvider]);

  useEffect(() => {
    void load();
    return () => { operation.current += 1; providerGeneration.current += 1; };
  }, [load]);

  const connectionIndex = Number(connection);
  const chosenConnection = connection !== '' && Number.isInteger(connectionIndex) && connectionIndex >= 0 ? options?.connections[connectionIndex] : undefined;
  const providerInput = chosenConnection ? { connection_ref: chosenConnection, region: region.trim(),
    execution_role_arn: executionRole.trim(), source_bucket: sourceBucket.trim() } : undefined;
  const savedReview = configuration?.value.settings.well_architected;
  const providerEdited = executionRole.trim() !== (savedReview?.execution_role_ref?.record_id ?? '')
    || sourceBucket.trim() !== (savedReview?.source_store_ref?.record_id ?? '')
    || !!(savedReview?.execution_role_ref || savedReview?.source_store_ref) &&
      (!chosenConnection || !sameReference(chosenConnection, configuration?.value.settings.connection_ref) || region.trim() !== configuration?.value.settings.region);
  const newlyEnabled = enabled && !configuration?.value.settings.enabled;

  async function validateProvider() {
    if (busy || conflicted || validating || !providerInput) return;
    if (!validProviderInput(providerInput)) {
      setError('Enter an execution role ARN, a source bucket name and an AWS region before validating.'); return;
    }
    const generation = ++providerGeneration.current;
    setSelection(null); setReviewed(false); setValidating(true); setError(''); setMessage('');
    try {
      const result = await validateWellArchitectedProviderSelection(productId, projectId, providerInput);
      if (generation !== providerGeneration.current) return;
      if (!validProviderSelection(result, providerInput)) {
        setError('Provider validation returned references that do not match this selection. Nothing was accepted; refresh and validate again.'); return;
      }
      setSelection(result);
    } catch (validationError) {
      if (generation !== providerGeneration.current) return;
      if (statusOf(validationError) === 403 || statusOf(validationError) === 404) deny();
      else setError(providerAction(validationError) ?? 'Provider validation could not finish. Check the selected connection and resources, then retry validation.');
    } finally { if (generation === providerGeneration.current) setValidating(false); }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || conflicted || validating || !options || !options.capabilities.includes('architecture_review') || !selectedSources.length || !selectedPillars.length) return;
    if (!reason.trim()) { setError('Explain the explicit source, connection and region choices.'); return; }
    if (!chosenConnection || !scoped(chosenConnection) || !region.trim()) {
      setError('Select an authorized connection and an explicit region.'); return;
    }
    if (selectedSources.some(source => !options.sources.some(ref => sameReference(ref, source)))) {
      setError('Some draft sources are no longer authorized choices. Refresh and review the pinned sources before saving.'); return;
    }
    if ((providerEdited || newlyEnabled || selection) && (!selection || !reviewed)) {
      setError('Validate the provider resources and confirm their pinned references before saving these changes.'); return;
    }
    let selectedLimits;
    try { selectedLimits = limitsTouched ? parseLimits(limits, tenantId, projectId) : undefined; }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Review the assessment limits.'); return; }
    const classificationChanged = classificationTouched && classification.trim() !== (configuration?.value.settings.classification ?? '');
    const sourceOverride = { field: 'source_refs', reason: reason.trim(), value: selectedSources };
    await persistConfiguration({
        service_id: 'well_architected', capabilities: ['architecture_review'], enabled,
        source_refs: selectedSources, connection_ref: chosenConnection, region: region.trim(),
        ...(classificationChanged ? { classification: classification.trim() } : {}),
        ...(selectedLimits ? { limits: selectedLimits } : {}),
        overrides: [...(configuration?.value.settings.overrides ?? []).filter(item => !['source_refs', 'connection_ref', 'region', ...(classificationChanged ? ['classification'] : [])].includes(item.field)),
          sourceOverride, { field: 'connection_ref', reason: reason.trim(), value: chosenConnection },
          { field: 'region', reason: reason.trim(), value: region.trim() },
          ...(classificationChanged ? [{ field: 'classification', reason: reason.trim(), value: classification.trim() }] : [])],
        well_architected: { goal_refs: [], ...savedReview,
          ...(selection ? { execution_role_ref: selection.execution_role_ref, source_store_ref: selection.source_store_ref } : {}),
          review_mode: 'source_only', pillars: selectedPillars },
      }, 'review');
  }

  const saveSchedules: SaveWellArchitectedSchedules = async (triggers, expected) => {
    if (busy || conflicted || validating || !configuration || configuration !== expected || !options?.capabilities.includes('architecture_review')) return false;
    const settings = configuration.value.settings;
    return persistConfiguration({ service_id: 'well_architected', capabilities: ['architecture_review'], enabled: settings.enabled,
      source_refs: settings.source_refs, connection_ref: settings.connection_ref, region: settings.region,
      well_architected: settings.well_architected, triggers,
    }, 'schedule');
  };

  async function persistConfiguration(choices: Parameters<typeof saveWellArchitectedConfiguration>[2], kind: 'review' | 'schedule') {
    const request = ++operation.current;
    setBusy(true); setError(''); setMessage('');
    try {
      const saved = await saveWellArchitectedConfiguration(productId, projectId, choices, configuration?.etag);
      if (request !== operation.current) return false;
      if (!validConfiguration(saved.value)) throw { status: 403 };
      if (choices.triggers && !sameScheduleTriggers(saved.value.settings.triggers ?? [], choices.triggers)) throw new Error('Schedule readback differs from the saved choices.');
      previouslyRead.current = true;
      invalidateProvider();
      setConfiguration(saved);
      if (kind === 'review') {
        setEnabled(saved.value.settings.enabled);
        setClassification(saved.value.settings.classification ?? ''); setClassificationTouched(false);
        setLimits(limitsDraft(saved.value.settings.limits)); setLimitsTouched(false);
      }
      setMessage(kind === 'schedule' ? `Schedule changes saved in configuration revision ${saved.value.revision}. No review was started. Each occurrence still requires current readiness and owner consent.` : `Configuration revision ${saved.value.revision} saved; ${saved.value.settings.enabled ? 'enabled' : 'disabled'}. Check readiness before running a review. Saving may revalidate the explicit resources; no review or AWS discovery was started.`);
      return true;
    } catch (saveError) {
      if (request !== operation.current) return false;
      invalidateProvider();
      if (statusOf(saveError) === 412) {
        setConflicted(true);
        setError('Configuration changed elsewhere. Your draft is preserved. Refresh and review it before saving; stale choices were not replayed.');
      } else if (statusOf(saveError) === 403 || statusOf(saveError) === 404) deny();
      else {
        const status = statusOf(saveError);
        const uncertain = typeof status !== 'number' || status >= 500;
        if (uncertain) setConflicted(true);
        setError(uncertain ? 'The save outcome is unknown. Your draft is preserved. Refresh the saved configuration before making another save; this request will not be replayed.'
          : providerAction(saveError) ?? `Configuration was not saved (HTTP ${status}). Review the choices before trying again.`);
      }
      return false;
    } finally { if (request === operation.current) setBusy(false); }
  }

  const setupContent = <>
    <ProfileSetupSummary configuration={configuration?.value} productId={productId} projectId={projectId} />
    {options && !revoked ? <>
      {options.unavailable_reasons.length > 0 && <aside aria-label="Review setup guidance">
        <h3>Before starting a review</h3>
        <ul className="list-disc space-y-2 pl-5">{options.unavailable_reasons.map(reason => <li key={reason}>
          {setupGuidance[reason] ?? `Setup check ${reason.replace(/_/g, ' ')} needs attention. Refresh setup choices; if it persists, ask the platform administrator to inspect this check.`}
        </li>)}</ul>
      </aside>}
      {configuration && <ServiceDisclosure title="Configuration status">
        <p>Revision {configuration.value.revision}: {configuration.value.state}; gaps: {configuration.value.gaps.join(', ') || 'none'}; handoff: {configuration.value.handoff_state}</p>
        <p>Inherited: {configuration.value.inherited_fields.join(', ') || 'none'}; purpose: {configuration.value.settings.purpose || 'not provided'}; classification: {configuration.value.settings.classification || 'not provided'}</p>
      </ServiceDisclosure>}
      <ServiceDisclosure title="Edit review setup" hint="Review or change the service settings" open={!configuration}>
        <fieldset disabled={busy || conflicted}><legend>Source-only provider setup</legend>
          <p>Enter the separate Well-Architected execution role and the private archive bucket installed for this runtime. Validation reads only these resources through your selected connection.</p>
          <label>Execution role ARN <input value={executionRole} onChange={event => { invalidateProvider(); setExecutionRole(event.target.value); }} /></label>
          <label>Source bucket name <input value={sourceBucket} onChange={event => { invalidateProvider(); setSourceBucket(event.target.value); }} /></label>
          <button type="button" disabled={validating || !providerInput} onClick={() => void validateProvider()}>{validating ? 'Validating provider resources…' : 'Validate provider resources'}</button>
          {savedReview?.execution_role_ref && <p>Saved execution role: {savedReview.execution_role_ref.record_id} ({savedReview.execution_role_ref.record_revision})</p>}
          {savedReview?.source_store_ref && <p>Saved source store: {savedReview.source_store_ref.record_id} ({savedReview.source_store_ref.record_revision})</p>}
          {selection && <div aria-label="Validated provider resources">
            <p>Validated connection: {chosenConnection?.record_id} ({chosenConnection?.record_revision}); region: {region.trim()}.</p>
            <p>Execution role: {selection.execution_role_ref.record_id}; pinned revision: {selection.execution_role_ref.record_revision}.</p>
            <p>Source store: {selection.source_store_ref.record_id}; pinned revision: {selection.source_store_ref.record_revision}.</p>
            <p>Resource checks passed. Native provider qualification and review readiness still require setup.</p>
            <label><input type="checkbox" checked={reviewed} onChange={event => setReviewed(event.target.checked)} />I reviewed these pinned provider resources</label>
          </div>}
          <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} />Enable this review configuration when saved</label>
          <p>Enablement is saved only when you save the review draft. Existing enabled schedules may become eligible after enablement; every occurrence still requires current readiness and execution consent.</p>
          <p>Review the exact binding and source scope in Owner consent before granting access. A qualified provider, current consent, and a ready binding are required to run a review.</p>
        </fieldset>
        <fieldset disabled={busy || conflicted}><legend>Pinned review sources</legend>{options.sources.filter(ref => ['document', 'repository_commit'].includes(ref.record_type)).map(ref =>
          <label key={`${ref.source_connection_id}:${ref.record_type}:${ref.record_id}:${ref.record_revision}`} className="block">
            <input type="checkbox" checked={selectedSources.some(item => sameReference(item, ref))}
              onChange={event => { invalidateProvider(); setSelectedSources(current => event.target.checked ? [...current, ref] : current.filter(item => !sameReference(item, ref))); }} />
            {ref.record_type}: {ref.record_id} ({ref.record_revision})
          </label>)}</fieldset>
        <label>Authorized connection <select disabled={busy || conflicted} value={connection} onChange={event => { invalidateProvider(); setConnection(event.target.value); }}>
          <option value="">Select a connection</option>{options.connections.map((ref, index) => <option key={index} value={index}>{ref.record_id} ({ref.record_revision})</option>)}
        </select></label>
        <label>Provider region <input disabled={busy || conflicted} value={region} onChange={event => { invalidateProvider(); setRegion(event.target.value); }} /></label>
        <fieldset disabled={busy || conflicted}><legend>Review data and execution limits</legend>
          <p>Classification and assessment limits are required before a review can run. An incomplete draft keeps its missing inputs visible.</p>
          <label>Data classification <select value={classification} onChange={event => { setClassification(event.target.value); setClassificationTouched(true); }}>
            <option value="">Select classification</option>
            {['public', 'internal', 'confidential', 'restricted'].map(value => <option key={value} value={value}>{value}</option>)}
          </select></label>
          <SecurityLimits value={limits} onChange={value => { setLimits(value); setLimitsTouched(true); }} />
        </fieldset>
        <label>Reason for explicit choices <input disabled={busy || conflicted} value={reason} onChange={event => setReason(event.target.value)} /></label>
        <p>Save setup and the review pillars selected in Capabilities together before reviewing owner consent or setting up a binding. Schedule changes are saved separately in Runs &amp; schedules.</p>
        <button type="submit" disabled={busy || validating || conflicted || !options.capabilities.includes('architecture_review') || !selectedSources.length || !selectedPillars.length}>Save review draft</button>
      </ServiceDisclosure>
    </> : <p>{revoked ? 'Setup details are unavailable for this workspace.' : busy ? 'Loading authorized setup choices.' : 'Setup choices are unavailable. Refresh access to try again.'}</p>}
    <L5OwnerConsent service="well_architected" productId={productId} projectId={projectId} configuration={configuration?.value ?? null} disabled={busy || conflicted || revoked} />
  </>;
  const capabilitiesContent = <>
    <p>Review mode: source only. Reviews use selected documents and repository revisions.</p>
    <p>Cloud resource inspection: Not available yet.</p>
    {options && !revoked ? <>
        <CapabilitySummary service="well_architected" capabilities={options.capabilities} selected={options.capabilities.includes('architecture_review') ? ['architecture_review'] : []} />
        <fieldset disabled={busy || conflicted}><legend>Review pillars</legend>{pillars.map(pillar => <label key={pillar} className="block">
          <input type="checkbox" checked={selectedPillars.includes(pillar)} onChange={event => { invalidateProvider(); setSelectedPillars(current => event.target.checked ? [...current, pillar] : current.filter(item => item !== pillar)); }} />{pillar}
        </label>)}</fieldset>
        <p>Saved review goals: {configuration?.value.settings.well_architected.goal_refs.map(ref => `${ref.record_id} (${ref.record_revision})`).join(', ') || 'none selected'}.</p>
        <p>Save these selections with Save review draft in Setup.</p>
    </> : <p>Capability choices appear after authorized setup is loaded.</p>}
  </>;
  return <ServiceJourney service="well_architected">
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    <button type="button" onClick={() => void load(!revoked && !!options, chosenConnection)} disabled={busy}>Refresh Well-Architected access</button>
    <form onSubmit={event => void save(event)} className="space-y-4">
    {configuration && options && !revoked
      ? <WellArchitectedRunsResults productId={productId} projectId={projectId} tenantId={tenantId} configuration={configuration}
          eligible={!busy && !conflicted && options.capabilities.includes('architecture_review')} onAccessDenied={deny}
          setupContent={setupContent} capabilitiesContent={capabilitiesContent}
          scheduleContent={(bindings, bindingsCurrent) => <WellArchitectedScheduleEditor configuration={configuration} bindings={bindings}
            bindingsCurrent={bindingsCurrent} disabled={busy || conflicted || validating || !options.capabilities.includes('architecture_review')} onSave={saveSchedules} />} />
      : <>
        <ServiceSection service="well_architected" step="setup">{setupContent}</ServiceSection>
        <ServiceSection service="well_architected" step="capabilities">{capabilitiesContent}</ServiceSection>
        <ServiceSection service="well_architected" step="runs"><p>Daily UTC review schedules become available after saving a configuration and preparing a ready binding.</p><p>Save an authorized configuration to check readiness and view reviews.</p></ServiceSection>
        <ServiceSection service="well_architected" step="results"><p>Retained review results appear after an authorized configuration is loaded.</p></ServiceSection>
      </>}
    </form>
  </ServiceJourney>;
}
