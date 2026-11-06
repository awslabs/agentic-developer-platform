import { SecurityBadge, securityCapabilityName } from './SecurityPresentation';
import L5OwnerConsent from './L5OwnerConsent';
import { useCallback, useEffect, useRef, useState, useId, type FormEvent, type ReactNode } from 'react';
import SecurityEvidencePanel from './SecurityEvidencePanel';
import SecurityLimits, { limitsDraft, parseLimits } from './SecurityLimits';
import SecurityProfileRefresh from './SecurityProfileRefresh';
import SourceCapturePreflight from './SourceCapturePreflight';
import { ServiceSection, ServiceDisclosure } from './L5ServiceJourney';
import DevOpsSetupPanel from './DevOpsSetupPanel';
import RunAccess from './RunAccess';
import useProductSourceLabels, { ProductSourceNameContext } from './useProductSourceLabels';
import './securitySetup.css';
import type { RecordReference } from '@/services/products';
import { isSecurityCapability } from '@/services/securityCapabilityReadiness';
import { getSecurityConfiguration, getSecurityOptions, listSecurityBindings, saveSecurityConfiguration, securityReadinessSummary, matchesSecurityScope, type SecurityBinding, type SecurityConfiguration as Configuration, type SecurityOptions, type Versioned } from '@/services/l5Services';

interface Props { bindingsPanel?: ReactNode; assessmentsPanel?: ReactNode; resultsPanel?: ReactNode; productId: string; projectId: string; tenantId?: string; onRead?: (configuration: Versioned<Configuration> | null, options: SecurityOptions | null) => void; onAccessDenied?: () => void }

type Capability = SecurityOptions['capabilities'][number];
const referenceKey = (ref?: RecordReference) => ref ? JSON.stringify([ref.tenant_id, ref.project_id, ref.source_connection_id, ref.record_type, ref.record_id, ref.record_revision]) : '';
const sameReferences = (left: RecordReference[], right: RecordReference[]) =>
  JSON.stringify(left.map(referenceKey).sort()) === JSON.stringify(right.map(referenceKey).sort());

const failure = (error: unknown) => {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    if (error.status === 412) return 'This configuration changed elsewhere. Refresh it before making another edit.';
    if (error.status === 403 || error.status === 404) return 'Security information is unavailable for this workspace.';
  }
  return 'The Security configuration request could not be confirmed.';
};

type ScheduleRow = { triggerId: string; enabled: boolean; time: string; binding: string; sources: string[] };

const scheduleCapabilities: Capability[] = ['threat_model', 'code_review'];
const emptySchedule = (): ScheduleRow => ({ triggerId: '', enabled: false, time: '', binding: '', sources: [] });
// Schedules are plain daily UTC cron expressions; the saved trigger identity never changes.
const utcTimeOf = (expression?: string) => {
  const parts = (expression ?? '').trim().split(/\s+/);
  return parts.length === 5 && /^[0-9]{1,2}$/.test(parts[0]) && /^[0-9]{1,2}$/.test(parts[1])
    && parts.slice(2).join(' ') === '* * *' && Number(parts[0]) < 60 && Number(parts[1]) < 24
    ? `${parts[1].padStart(2, '0')}:${parts[0].padStart(2, '0')}` : '';
};
const cronOf = (time: string) => {
  const match = /^([01][0-9]|2[0-3]):([0-5][0-9])$/.exec(time.trim());
  return match ? `${Number(match[2])} ${Number(match[1])} * * *` : '';
};

export default function SecurityConfiguration({ productId, projectId, tenantId, onRead, onAccessDenied, bindingsPanel, assessmentsPanel, resultsPanel }: Props) {
  const [panel, setPanel] = useState<'workspace' | 'access' | 'advanced' | 'profile' | null>(null);
  const formId = `security-configuration-${useId()}`;
  const [options, setOptions] = useState<SecurityOptions | null>(null);
  const hadConfiguration = useRef(false);
  const [configuration, setConfiguration] = useState<Versioned<Configuration> | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [connection, setConnection] = useState('');
  const [region, setRegion] = useState('');
  const [sources, setSources] = useState<string[]>([]);
  const [sourceSelectionTouched, setSourceSelectionTouched] = useState(false);
  const [overrideReason, setOverrideReason] = useState('');
  const [bindings, setBindings] = useState<SecurityBinding[]>([]);
  const [manualEnabled, setManualEnabled] = useState(false);
  const [manualCapability, setManualCapability] = useState<Capability | ''>('');
  const [manualBinding, setManualBinding] = useState('');
  const [manualSources, setManualSources] = useState<string[]>([]);
  const [manualTriggerId, setManualTriggerId] = useState('');
  const [schedules, setSchedules] = useState<Record<string, ScheduleRow>>({});
  const [pollInterval, setPollInterval] = useState(600);
  const [pollGrace, setPollGrace] = useState(300);
  const [stallAfter, setStallAfter] = useState('');
  const [visibility, setVisibility] = useState<'only_me' | 'product_team'>('only_me');
  const [enabled, setEnabled] = useState(false);
  const [limits, setLimits] = useState(limitsDraft());
  const [limitsTouched, setLimitsTouched] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [conflicted, setConflicted] = useState(false);
  const [needFreshEdit, setNeedFreshEdit] = useState(false);
  const [editedSinceRefresh, setEditedSinceRefresh] = useState(false);
  const [evidenceChallenge, setEvidenceChallenge] = useState<string | undefined>();
  const [evidenceReceipt, setEvidenceReceipt] = useState<string | undefined>();
  const [evidenceUncertain, setEvidenceUncertain] = useState(false);
  const generation = useRef(0);

  const load = useCallback(async () => {
    const request = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const choices = await getSecurityOptions(productId, projectId);
      let current: Versioned<Configuration> | null = null;
      try {
        current = await getSecurityConfiguration(productId, projectId);
      } catch (readError) {
        if (!(typeof readError === 'object' && readError !== null && 'status' in readError && readError.status === 404 && !hadConfiguration.current)) throw readError;
      }
      if (current && (!matchesSecurityScope(current.value.scope, current.value.scope, productId, projectId)
        || (tenantId && current.value.scope.tenant_id !== tenantId)
        || (current.value.settings.profile_ref && (current.value.settings.profile_ref.tenant_id !== current.value.scope.tenant_id
          || current.value.settings.profile_ref.project_id !== projectId || current.value.settings.profile_ref.record_type !== 'product'
          || current.value.settings.profile_ref.record_id !== productId)))) throw { status: 403 };
      if ([...choices.connections, ...choices.sources].some(source => (tenantId ?? current?.value.scope.tenant_id) && source.tenant_id !== (tenantId ?? current?.value.scope.tenant_id)
        || source.project_id !== projectId)) throw { status: 403 };
      const bindingPage = current ? await listSecurityBindings(productId, projectId) : { items: [] };
      if (request !== generation.current) return;
      if (bindingPage.items.some(binding => !matchesSecurityScope(binding.scope, current?.value.scope, productId, projectId))) throw { status: 403 };
      if (current) hadConfiguration.current = true;
      setOptions({ ...choices, capabilities: choices.capabilities.filter(isSecurityCapability) });
      setConfiguration(current);
      onRead?.(current, choices);
      const manual = current?.value.settings.triggers.find(item => item.kind === 'manual');
      setManualEnabled(manual?.enabled ?? false);
      setManualCapability(isSecurityCapability(manual?.capability) ? manual.capability : '');
      setManualBinding(manual?.binding_id ?? '');
      setManualTriggerId(manual?.trigger_id ?? '');
      setManualSources(choices.sources.flatMap((source, index) => manual?.source_refs.some(item => referenceKey(item) === referenceKey(source)) ? [String(index)] : []));
      const scheduled = current?.value.settings.triggers.filter(item => item.kind === 'schedule') ?? [];
      setSchedules(Object.fromEntries(scheduleCapabilities.map(capability => {
        const trigger = scheduled.find(item => item.capability === capability);
        return [capability, trigger ? {
          triggerId: trigger.trigger_id, enabled: trigger.enabled, time: utcTimeOf(trigger.schedule),
          binding: trigger.binding_id ?? '',
          sources: choices.sources.flatMap((source, index) => trigger.source_refs.some(item => referenceKey(item) === referenceKey(source)) ? [String(index)] : []),
        } : emptySchedule()];
      })));
      setPollInterval(current?.value.settings.monitoring?.poll_interval_seconds ?? 600);
      setPollGrace(current?.value.settings.monitoring?.poll_grace_seconds ?? 300);
      setStallAfter(String(current?.value.settings.monitoring?.stall_after_seconds ?? ''));
      setBindings(bindingPage.items);
      setSources(choices.sources.flatMap((source, index) => current?.value.settings.source_refs.some(item => referenceKey(item) === referenceKey(source)) ? [String(index)] : []));
      setOverrideReason('');
      setSourceSelectionTouched(false);
      setCapabilities((current?.value.settings.capabilities ?? []).filter(isSecurityCapability));
      const selectedConnection = current?.value.settings.connection_ref;
      setConnection(selectedConnection ? String(choices.connections.findIndex(item => referenceKey(item) === referenceKey(selectedConnection))) : '');
      setRegion(current?.value.settings.region ?? '');
      setVisibility(current?.value.settings.visibility ?? 'only_me');
      setEnabled(current?.value.settings.enabled ?? false);
      setLimits(limitsDraft(current?.value.settings.limits));
      setLimitsTouched(false);
      setMessage('');
      setConflicted(false);
      setEditedSinceRefresh(false);
    } catch (readError) {
      if (request !== generation.current) return;
      setConflicted(true);
      setOptions(null);
      setConfiguration(null);
      setBindings([]);
      onRead?.(null, null);
      setError(failure(readError));
      if (typeof readError === 'object' && readError !== null && 'status' in readError && (readError.status === 403 || readError.status === 404)) onAccessDenied?.();
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }, [productId, projectId, tenantId, onRead, onAccessDenied]);

  useEffect(() => {
    setEvidenceChallenge(undefined);
    setEvidenceReceipt(undefined);
    setEvidenceUncertain(false);
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  const { sourceName, accountName } = useProductSourceLabels(productId, projectId, tenantId ?? configuration?.value.scope.tenant_id,
    [...(options?.connections ?? []), ...(options?.sources ?? [])], options);

  const handleEvidenceReconciled = useCallback(() => { setEvidenceUncertain(false); void load(); }, [load]);
  const handleEvidenceChallenge = useCallback<Parameters<typeof SecurityEvidencePanel>[0]['onChallenge']>(challenge => {
    setEvidenceChallenge(challenge?.id); setEvidenceReceipt(challenge?.receipt);
    if (challenge?.id && challenge.id !== evidenceChallenge) setEvidenceUncertain(false);
    if (challenge) { setRegion(challenge.region); setOverrideReason(challenge.reason); setEditedSinceRefresh(true); }
  }, [evidenceChallenge]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!options || busy || evidenceUncertain || !capabilities.length || conflicted || (needFreshEdit && !editedSinceRefresh)) return;
    const previous = configuration?.value.settings;
    let selectedLimits;
    try {
      selectedLimits = limitsTouched ? parseLimits(limits, tenantId ?? configuration?.value.scope.tenant_id ?? '', projectId) : undefined;
      if (selectedLimits && stallAfter.trim() && Number(stallAfter) > selectedLimits.deadline_seconds) throw new Error('Stall detection must not exceed the run deadline.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Review the assessment limits.'); return; }
    const chosenConnection = connection !== '' ? options.connections[Number(connection)] : undefined;
    const chosenSources = sources.map(index => options.sources[Number(index)]).filter(item => item !== undefined);
    const changedFields = {
      ...(chosenConnection && referenceKey(chosenConnection) !== referenceKey(previous?.connection_ref) ? { connection_ref: chosenConnection } : {}),
      ...(region.trim() && region.trim() !== previous?.region ? { region: region.trim() } : {}),
      ...(sourceSelectionTouched && !sameReferences(chosenSources, previous?.source_refs ?? []) ? { source_refs: chosenSources } : {}),
    };
    const selectedSources = manualSources.map(index => options.sources[Number(index)]).filter(item => item !== undefined);
    const configuredSources = 'source_refs' in changedFields ? chosenSources : previous?.source_refs ?? [];
    if (manualEnabled && (!enabled || !manualCapability || !manualBinding || !selectedSources.length
      || !selectedSources.every(item => configuredSources.some(source => referenceKey(source) === referenceKey(item))
        && item.record_type === (manualCapability === 'threat_model' ? 'document' : 'repository_commit'))
      || !bindings.some(binding => binding.binding_id === manualBinding && binding.state === 'ready'
        && binding.configuration_id === configuration?.value.configuration_id))) {
      setError('An enabled manual trigger needs an enabled configuration, ready binding, capability and matching pinned sources.');
      return;
    }
    const scheduleRows = scheduleCapabilities.map(capability => ({ capability, row: schedules[capability] ?? emptySchedule() }));
    if (scheduleRows.filter(({ row }) => row.enabled).some(({ capability, row }) => !enabled || !capabilities.includes(capability)
      || !cronOf(row.time) || !row.sources.length
      || !bindings.some(binding => binding.binding_id === row.binding && binding.state === 'ready'
        && binding.configuration_id === configuration?.value.configuration_id)
      || !row.sources.map(index => options.sources[Number(index)]).every(item => item !== undefined
        && configuredSources.some(source => referenceKey(source) === referenceKey(item))
        && item.record_type === (capability === 'threat_model' ? 'document' : 'repository_commit')))) {
      setError('An enabled nightly schedule needs an enabled configuration, its selected capability, a ready binding, a UTC time and matching pinned sources.');
      return;
    }
    if (Object.keys(changedFields).length && !overrideReason.trim()) {
      setError('Add a short reason for changing the AWS account, region or Product inputs, then save again.');
      return;
    }
    setBusy(true);
    const request = generation.current;
    onRead?.(null, null);
    setError('');
    setMessage('');
    try {
      const overrides = (previous?.overrides ?? []).filter(item => !(item.field in changedFields));
      for (const [field, value] of Object.entries(changedFields)) overrides.push({ field, reason: overrideReason.trim(), value });
      const existingManual = previous?.triggers.find(item => item.kind === 'manual');
      const triggerId = existingManual?.trigger_id ?? (manualTriggerId || crypto.randomUUID());
      const triggers = (previous?.triggers ?? []).filter(item => item.trigger_id !== existingManual?.trigger_id);
      if (manualEnabled) triggers.push({ trigger_id: triggerId, kind: 'manual', enabled: true, capability: manualCapability,
        binding_id: manualBinding, source_refs: selectedSources, debounce_seconds: 0 });
      else if (existingManual) triggers.push({ ...existingManual, enabled: false });
      const scheduleIds: Record<string, string> = {};
      for (const { capability, row } of scheduleRows) {
        const existing = (previous?.triggers ?? []).find(item => item.kind === 'schedule' && item.capability === capability);
        if (!row.enabled && !existing) continue;
        // Disabling keeps the original trigger identity, schedule and pinned
        // scope so retained receipts stay readable and re-enabling is explicit.
        const identifier = existing?.trigger_id || row.triggerId || crypto.randomUUID();
        const replaced = triggers.findIndex(item => item.trigger_id === identifier);
        if (replaced >= 0) triggers.splice(replaced, 1);
        triggers.push(row.enabled
          ? { trigger_id: identifier, kind: 'schedule', enabled: true, capability, binding_id: row.binding,
            source_refs: row.sources.map(index => options.sources[Number(index)]).filter(item => item !== undefined),
            debounce_seconds: 0, schedule: cronOf(row.time) }
          : { ...(existing ?? {}), trigger_id: identifier, kind: 'schedule', enabled: false, capability,
            source_refs: existing?.source_refs ?? [], debounce_seconds: existing?.debounce_seconds ?? 0 });
        scheduleIds[capability] = identifier;
      }
      const saved = await saveSecurityConfiguration(productId, projectId, {
        capabilities, enabled, visibility,
        exclusions: previous?.exclusions ?? [],
        overrides,
        triggers,
        monitoring: { poll_interval_seconds: pollInterval, poll_grace_seconds: pollGrace,
          ...(stallAfter.trim() ? { stall_after_seconds: Number(stallAfter) } : {}) },
        ...('source_refs' in changedFields ? { source_refs: chosenSources } : {}),
        ...(chosenConnection ? { connection_ref: chosenConnection } : {}),
        ...(region.trim() ? { region: region.trim() } : {}),
        ...(selectedLimits ? { limits: selectedLimits } : {}),

      }, configuration?.etag, evidenceReceipt, evidenceChallenge);
      if (request !== generation.current) return;
      if (!matchesSecurityScope(saved.value.scope, configuration?.value.scope ?? saved.value.scope, productId, projectId)
        || (tenantId && saved.value.scope.tenant_id !== tenantId)) throw { status: 403 };
      hadConfiguration.current = true;
      setConfiguration(saved);
      // A first save can inherit Product inputs and AWS settings server-side.
      // Show that authoritative setup immediately, including normalized values.
      setConnection(saved.value.settings.connection_ref ? String(options.connections.findIndex(item => referenceKey(item) === referenceKey(saved.value.settings.connection_ref))) : '');
      setRegion(saved.value.settings.region ?? '');
      setSources(options.sources.flatMap((source, index) => saved.value.settings.source_refs.some(item => referenceKey(item) === referenceKey(source)) ? [String(index)] : []));
      setCapabilities(saved.value.settings.capabilities.filter(isSecurityCapability));
      setEnabled(saved.value.settings.enabled);
      setVisibility(saved.value.settings.visibility);
      setLimits(limitsDraft(saved.value.settings.limits));
      setEvidenceChallenge(undefined);
      setEvidenceReceipt(undefined);
      setManualTriggerId(manualEnabled ? triggerId : '');
      setSchedules(current => Object.fromEntries(scheduleCapabilities.map(capability => [capability, {
        ...(current[capability] ?? emptySchedule()),
        triggerId: scheduleIds[capability] ?? current[capability]?.triggerId ?? '',
      }])));
      onRead?.(saved, options);
      setOverrideReason('');
      setSourceSelectionTouched(false);
      setNeedFreshEdit(false);
      setEditedSinceRefresh(false);
      setLimitsTouched(false);
      setMessage('Security setup saved. Open Runs & schedules to start an assessment.');
    } catch (saveError) {
      if (request !== generation.current) return;
      if (typeof saveError === 'object' && saveError !== null && 'status' in saveError && saveError.status === 412) {
        setConflicted(true);
        setNeedFreshEdit(true);
        setEditedSinceRefresh(false);
        onRead?.(null, null);
      }
      if (evidenceChallenge) {
        setEvidenceUncertain(true);
        setError('The acceptance save may have completed. Read request status and use Reconcile original save before continuing. Do not save again.');
      } else {
        setConflicted(true);
        onRead?.(null, null);
        setError(`${failure(saveError)} Reload Security configuration and review the saved revision before another change.`);
      }
      if (typeof saveError === 'object' && saveError !== null && 'status' in saveError && (saveError.status === 403 || saveError.status === 404)) onAccessDenied?.();
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }

  const markEdited = (event: FormEvent<HTMLElement>) => {
    if (event.target instanceof Element && event.target.closest('.run-access, .security-setup-diagnostics')) return;
    if (!(event.target instanceof HTMLInputElement && event.target.name === 'override-reason')) setEditedSinceRefresh(true);
  };
  const editingLocked = busy || conflicted || evidenceUncertain;
  const previous = configuration?.value.settings;
  const selectedConnection = connection !== '' ? options?.connections[Number(connection)] : undefined;
  const selectedSources = sources.map(index => options?.sources[Number(index)]).filter((item): item is RecordReference => Boolean(item));
  const needsReason = Boolean(selectedConnection && referenceKey(selectedConnection) !== referenceKey(previous?.connection_ref)
    || region.trim() && region.trim() !== previous?.region
    || sourceSelectionTouched && !sameReferences(selectedSources, previous?.source_refs ?? []));
  const savedBindings = bindings.filter(item => item.configuration_id === configuration?.value.configuration_id && item.state === 'ready');
  const savedBinding = savedBindings.length === 1 ? savedBindings[0] : null;
  const origin = (field: string) => previous?.overrides.some(item => item.field === field) ? 'Chosen for Security'
    : configuration?.value.inherited_fields.includes(field) ? 'From Product profile' : 'Saved for Security';
  const profileUrl = `/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}`;
  const saveDisabled = editingLocked || !capabilities.length || (needFreshEdit && !editedSinceRefresh);
  return <ProductSourceNameContext.Provider value={sourceName}><section aria-label="Security configuration" className="security-workspace space-y-5">
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      {needFreshEdit && !conflicted && !editedSinceRefresh && <p role="status">Latest configuration loaded. Review it and make a new edit before saving; the stale changes were not replayed.</p>}

    <ServiceSection service="security" step="setup" description="Choose what to check, review the Product inputs, and connect your AWS workspace.">
      <div className="security-setup-intro">
        <p>{editedSinceRefresh ? 'You have unsaved changes. Save setup before starting a run.'
          : !configuration ? 'Start with the three sections below. ADP uses the sources already connected to your Product.'
            : !previous?.enabled ? 'Your setup is saved. Turn on Security below and save when you are ready.'
              : !savedBindings.length ? 'Your choices are saved. Connect a Security workspace to finish setup.'
                : 'Your saved setup is shown below. Start an assessment from Runs & schedules; ADP checks access before it runs.'}</p>
        <button type="button" className="product-text-button" onClick={() => void load()} disabled={busy}>Reload setup</button>
      </div>
      {busy && !options && <p role="status">Loading Security setup…</p>}
      {options && <form id={formId} onSubmit={event => void save(event)} onChangeCapture={markEdited}
        onInvalidCapture={event => { if (event.target instanceof Element && event.target.closest('dialog')) setPanel('advanced'); }}>
        <fieldset disabled={editingLocked} className="security-setup-steps">
          <section className="security-setup-step" aria-labelledby="security-activities-title">
            <header><span className="security-step-number">1</span><div><h3 id="security-activities-title">What do you want to check?</h3><p>Choose one or both assessments.</p></div></header>
            <div className="security-assessment-choices">{scheduleCapabilities.map(capability => <label key={capability} className="security-assessment-choice">
              <input type="checkbox" aria-label={securityCapabilityName(capability)} checked={capabilities.includes(capability)} disabled={editingLocked || !options.capabilities.includes(capability)}
                onChange={event => setCapabilities(current => event.target.checked ? [...current, capability] : current.filter(item => item !== capability))} />
              <span><strong>{securityCapabilityName(capability)}</strong><small>{capability === 'threat_model' ? 'Find risks in your architecture. Uses a design document.' : 'Find vulnerabilities in your code. Uses a saved repository revision.'}</small>
                {!options.capabilities.includes(capability) && <small>Not enabled for this Product.</small>}</span>
            </label>)}</div>
            <p className="security-unavailable">Pen testing · Not available yet</p>
          </section>
          <section className="security-setup-step" aria-labelledby="security-inputs-title">
            <header><span className="security-step-number">2</span><div><h3 id="security-inputs-title">Which Product inputs should it use?</h3><p>Saved inputs are selected. Add missing repositories or documents in the Product profile.</p></div></header>
            <div className="security-product-context">
              <div><span>Product purpose</span><strong>{previous?.purpose || (configuration ? 'Add a purpose in the Product profile' : 'Read from the Product profile when you save')}</strong><small>{previous?.purpose ? origin('purpose') : configuration ? 'Needs Product input' : 'Not saved yet'}</small></div>
              <div><span>Data classification</span><strong>{previous?.classification || (configuration ? 'Set in the Product profile' : 'Read from the Product profile when you save')}</strong><small>{previous?.classification ? origin('classification') : configuration ? 'Needs Product input' : 'Not saved yet'}</small></div>
            </div>
            <div className="security-source-groups" role="group" aria-label="Source revisions for Security">{(['document', 'repository_commit'] as const).map(type => {
              const choices = options.sources.map((source, index) => ({ source, index })).filter(item => item.source.record_type === type);
              return <fieldset key={type}><legend>{type === 'document' ? 'Architecture documents' : 'Repositories'}</legend>
                <p>{type === 'document' ? 'For threat modeling. Choose a document that explains the system, its data and its architecture.' : 'For code scanning. ADP scans the complete saved revision; you can limit a run to a pull request later.'}</p>
                {choices.map(({ source, index }) => <label key={referenceKey(source)} className="security-source-choice">
                  <input type="checkbox" checked={sources.includes(String(index))} onChange={event => { setSourceSelectionTouched(true); setSources(current => event.target.checked ? [...current, String(index)] : current.filter(item => item !== String(index))); }} />
                  <span><strong>{sourceName(source)}</strong><small>{previous?.source_refs.some(saved => referenceKey(saved) === referenceKey(source)) ? origin('source_refs') : 'Available from Product'} · Revision {source.record_revision.slice(0, 12)}</small></span>
                </label>)}
                {!choices.length && <p className="security-missing-input">{type === 'document' ? 'No architecture document is available. Add and verify one in the Product profile.' : 'No repository is available. Connect and verify a repository in the Product profile.'}</p>}
              </fieldset>;
            })}</div>
            <div className="security-setup-links"><a href={profileUrl}>Open Product profile</a>
              {configuration && <button type="button" className="product-text-button" onClick={() => setPanel('profile')}>Review profile updates</button>}</div>
          </section>
          <section className="security-setup-step" aria-labelledby="security-location-title">
            <header><span className="security-step-number">3</span><div><h3 id="security-location-title">Where should Security run?</h3><p>Choose an AWS account connected to your Product. Each setup uses one account and region.</p></div></header>
            <div className="security-form-grid">
              <label>AWS account<select aria-label="AWS account" value={connection} onChange={event => setConnection(event.target.value)}>
                <option value="">Choose an AWS account</option>
                {options.connections.map((item, index) => <option key={referenceKey(item)} value={index}>{accountName(item)}</option>)}
              </select><small>{previous?.connection_ref ? origin('connection_ref') : 'Available through your Product’s AWS connections.'}</small></label>
              <label>AWS region{options.regions.length ? <select aria-label="AWS region" value={region} onChange={event => setRegion(event.target.value)}>
                <option value="">Choose an AWS region</option>{options.regions.map(item => <option key={item} value={item}>{item}</option>)}
                </select> : <input aria-label="AWS region" value={region} onChange={event => setRegion(event.target.value)} placeholder="For example, us-east-1" />}<small>{previous?.region ? origin('region') : 'Use the region for your Security workspace.'}</small></label>
              <label>Who can see results?<select value={visibility} onChange={event => setVisibility(event.target.value as typeof visibility)}><option value="only_me">Only me</option><option value="product_team">Product team</option></select></label>
            </div>
            {!options.connections.length && <p className="security-missing-input">No AWS account is available. Connect an AWS account in the Product profile before connecting the Security workspace.</p>}
            <label className="security-enable"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Turn on Security for this Product</label>
            <div className="security-workspace-connection"><div><strong>Security workspace</strong><p>{savedBindings.length ? 'An existing workspace is saved. ADP uses it for assessments.' : 'Save your choices, approve access, then create or connect a workspace.'}</p></div>
              <button type="button" disabled={!configuration || editingLocked || editedSinceRefresh} onClick={() => setPanel('workspace')}>{savedBindings.length ? 'Manage workspace' : 'Connect workspace'}</button>
              {!savedBinding && <button type="button" disabled={!configuration || editingLocked || editedSinceRefresh} onClick={() => setPanel('access')}>Approve workspace access</button>}
              {configuration && savedBinding && previous?.enabled && <RunAccess service="security" configuration={configuration} bindingId={savedBinding.binding_id} disabled={editingLocked || editedSinceRefresh} onReady={() => { void load(); }} />}
            </div>

          </section>
          {needsReason && <label className="security-change-reason">Why use these Security settings?<input aria-label="Why use these Security settings?" name="override-reason" value={overrideReason} onChange={event => setOverrideReason(event.target.value)} placeholder="For example, assess the production account using the reviewed design" /><small>Explain your connection, region or source change. Other services keep their existing settings.</small></label>}
        </fieldset>
        <footer className="security-setup-footer"><div className="security-actions">
          <button className="product-primary" type="submit" disabled={saveDisabled}>{busy ? 'Saving…' : 'Save setup'}</button>
          {configuration && !editedSinceRefresh && <a href="#l5-security-runs">Go to Runs & schedules →</a>}
        </div><button type="button" className="product-text-button" disabled={busy} onClick={() => setPanel('advanced')}>Advanced settings</button></footer>
        <DevOpsSetupPanel title="Advanced Security settings" open={panel === 'advanced'} onClose={() => setPanel(null)} busy={busy}>
          <p>Saved run limits and diagnostic tools. These are not needed for everyday setup changes.</p>
          <button type="button" disabled={!configuration || editingLocked || editedSinceRefresh} onClick={() => setPanel('access')}>Advanced access controls</button>
          <fieldset disabled={editingLocked}><SecurityLimits value={limits} onChange={value => { setLimits(value); setLimitsTouched(true); }} /></fieldset>
          <button type="submit" className="product-primary" disabled={saveDisabled}>Save advanced settings</button>
          <div className="security-setup-diagnostics" onKeyDown={event => { if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault(); }}><details><summary>Saved references and diagnostics</summary>
            <p>Configuration: {configuration?.value.state ?? 'Not saved'}. Missing requirements: {configuration?.value.gaps.join(', ') || 'None reported'}.</p>
            <p>AWS connection: {previous?.connection_ref?.record_id ?? 'Not saved'}</p>
            <ul>{previous?.source_refs.map(source => <li key={referenceKey(source)}>{source.record_type}: {source.record_id} · revision {source.record_revision}</li>)}</ul>
          </details>
          {configuration && <SourceCapturePreflight productId={productId} projectId={projectId} configurationId={configuration.value.configuration_id} configurationRevision={configuration.value.revision} disabled={editingLocked || editedSinceRefresh} />}
          <fieldset disabled={busy}><SecurityEvidencePanel productId={productId} projectId={projectId} tenantId={tenantId} onReconciled={handleEvidenceReconciled} onChallenge={handleEvidenceChallenge} /></fieldset></div>
        </DevOpsSetupPanel>
      </form>}
      <DevOpsSetupPanel title="Security workspace" open={panel === 'workspace'} onClose={() => setPanel(null)}>
        {bindingsPanel ?? <p>Save setup before connecting a Security workspace.</p>}
      </DevOpsSetupPanel>
      <DevOpsSetupPanel title="Security access" open={panel === 'access'} onClose={() => setPanel(null)}>
        <L5OwnerConsent service="security" guided productId={productId} projectId={projectId} configuration={configuration?.value ?? null} disabled={editingLocked || editedSinceRefresh} />
      </DevOpsSetupPanel>
      <DevOpsSetupPanel title="Product profile updates" open={panel === 'profile'} onClose={() => setPanel(null)} busy={busy}>
        {configuration && <SecurityProfileRefresh key={`${productId}:${projectId}:${configuration.value.hash}`} productId={productId} projectId={projectId}
          configuration={configuration} disabled={editingLocked} dirty={editedSinceRefresh} onBusy={value => { setBusy(value); if (value) onRead?.(null, null); }} onApplied={load}
          onUncertain={() => { setConflicted(true); onRead?.(null, null); }} onAccessDenied={() => { setOptions(null); setConfiguration(null); onRead?.(null, null); onAccessDenied?.(); }} />}
      </DevOpsSetupPanel>
    </ServiceSection>
    <ServiceSection service="security" step="runs" description="Start an assessment, follow progress, or manage daily schedules.">
      {assessmentsPanel ?? <p className="security-empty">Save configuration and prepare a binding before starting assessments. Run history will appear here.</p>}
      <div className="security-panel">
      <div className="security-panel-heading"><div><h3>Daily schedules</h3><p>Run assessments at a fixed time using the saved source revisions.</p></div></div>
      {options && <fieldset disabled={editingLocked} onChangeCapture={markEdited} className="space-y-4">
        <fieldset className="space-y-2"><legend>Scheduled assessments (daily UTC)</legend>
          <p className="security-note">Schedules require current owner consent, which expires within 24 hours. Renew it in Setup before it expires.</p>
          <details className="border rounded p-3">
            <summary>Schedule details and authorization</summary>
            <p>Each capability keeps one schedule with a stable identity. Manual assessments remain available separately. Disabling a schedule keeps its identity and retained receipts.</p>
            <p>Schedules use the selected source revisions until you change them. Saving does not start an assessment or grant execution consent. Expired or revoked consent blocks that occurrence with schedule_consent_required.</p>
            <p>Review execution consent in Setup for the selected binding and sources. Each new grant requires an explicit owner action. Advanced release verification only permits evidence sharing.</p>
            <p>Next-run and last-tick status are not available in this editor.</p>
          </details>
          <div className="security-form-grid">{scheduleCapabilities.map(capability => {
            const row = schedules[capability] ?? emptySchedule();
            const bound = bindings.find(item => item.binding_id === row.binding);
            return <div key={capability} aria-label={`Scheduled ${capability}`} className="security-panel">
              <div className="security-panel-heading"><h4>{securityCapabilityName(capability)}</h4><SecurityBadge value={row.enabled ? 'enabled' : 'disabled'} /></div>
              <p className="product-muted">{row.enabled && row.time ? `Daily at ${row.time} UTC` : 'Daily schedule off'}</p>
              {!capabilities.includes(capability) && <p>Select this capability above before enabling its schedule.</p>}
              <label className="block"><input form={formId} type="checkbox" aria-label={`Enable scheduled ${capability}`} checked={row.enabled}
                onChange={event => setSchedules(current => ({ ...current, [capability]: { ...row, enabled: event.target.checked } }))} /> Assess on a daily UTC schedule</label>
              <ServiceDisclosure title="Schedule settings" hint="Time, connection and source revisions" open={row.enabled}>
              <label className="block">Daily UTC time <input form={formId} type="time" aria-label={`Daily UTC time for ${capability}`} value={row.time}
                onChange={event => setSchedules(current => ({ ...current, [capability]: { ...row, time: event.target.value } }))} /></label>
              <label className="block">Schedule binding <select form={formId} aria-label={`Schedule binding for ${capability}`} value={row.binding}
                onChange={event => setSchedules(current => ({ ...current, [capability]: { ...row, binding: event.target.value } }))}>
                <option value="">Select a ready binding</option>
                {bindings.filter(item => item.state === 'ready' && item.configuration_id === configuration?.value.configuration_id).map(item =>
                  <option key={item.binding_id} value={item.binding_id}>Security connection · {item.binding_id.slice(0, 8)}</option>)}
              </select></label>
              <fieldset><legend>Pinned schedule sources</legend>
                {options.sources.map((item, index) => ({ item, index })).filter(({ item }) =>
                  item.record_type === (capability === 'threat_model' ? 'document' : 'repository_commit')
                  && (configuration?.value.settings.source_refs ?? []).some(source => referenceKey(source) === referenceKey(item)))
                  .map(({ item, index }) => <label className="block" key={`${item.record_id}:${item.record_revision}`}>
                    <input form={formId} type="checkbox" checked={row.sources.includes(String(index))}
                      onChange={event => setSchedules(current => ({ ...current, [capability]: { ...row,
                        sources: event.target.checked ? [...row.sources, String(index)] : row.sources.filter(source => source !== String(index)) } }))} /> {sourceName(item)} (revision {item.record_revision})
                  </label>)}
              </fieldset>
              <details><summary>Connection verification details</summary><p className="security-technical">Binding readiness: {bound ? (securityReadinessSummary(bound.readiness) || 'not yet verified') : 'select a ready binding'}</p><p className="security-technical">Schedule reference: {row.triggerId || 'assigned on first save'}</p></details>
              </ServiceDisclosure>
            </div>;
          })}</div>
        </fieldset>
        <ServiceDisclosure title="Manual assessment access" hint="Choose which saved sources can be run on demand">
        <fieldset className="space-y-2"><legend>Manual assessment trigger</legend>
          <label className="block"><input form={formId} type="checkbox" checked={manualEnabled} onChange={event => setManualEnabled(event.target.checked)} /> Allow manual assessments</label>
          <label className="block">Trigger capability <select form={formId} value={manualCapability} onChange={event => { setManualCapability(event.target.value as Capability); setManualSources([]); }}>
            <option value="">Select capability</option>{capabilities.map(item => <option key={item} value={item}>{securityCapabilityName(item)}</option>)}
          </select></label>
          <label className="block">Trigger binding <select form={formId} value={manualBinding} onChange={event => setManualBinding(event.target.value)}>
            <option value="">Select a ready binding</option>{bindings.filter(item => item.state === 'ready' && item.configuration_id === configuration?.value.configuration_id).map(item =>
              <option key={item.binding_id} value={item.binding_id}>Security connection · {item.binding_id.slice(0, 8)}</option>)}
          </select></label>
          <fieldset><legend>Trigger source revisions</legend>
            {options.sources.map((item, index) => ({ item, index })).filter(({ item }) =>
              item.record_type === (manualCapability === 'threat_model' ? 'document' : 'repository_commit')
              && (configuration?.value.settings.source_refs ?? []).some(source => referenceKey(source) === referenceKey(item)))
              .map(({ item, index }) => <label className="block" key={`${item.record_id}:${item.record_revision}`}>
                <input form={formId} type="checkbox" checked={manualSources.includes(String(index))} onChange={event => setManualSources(current => event.target.checked ? [...current, String(index)] : current.filter(source => source !== String(index)))} /> {item.record_id} (revision {item.record_revision})
              </label>)}
          </fieldset>
        </fieldset>
        </ServiceDisclosure>
        <ServiceDisclosure title="Monitoring preferences" hint="Advanced polling and stall detection">
        <fieldset className="space-y-2"><legend>Assessment monitoring policy</legend>
          <label className="block">Poll interval (seconds) <input form={formId} type="number" min={60} max={900} value={pollInterval} onChange={event => setPollInterval(Number(event.target.value))} /></label>
          <label className="block">Poll grace (seconds) <input form={formId} type="number" min={60} max={900} value={pollGrace} onChange={event => setPollGrace(Number(event.target.value))} /></label>
          <label className="block">Suspected stall after (seconds, optional) <input form={formId} type="number" min={Math.max(600, 2 * pollInterval)} max={86400} value={stallAfter} onChange={event => setStallAfter(event.target.value)} /></label>
          <p>A suspected stall is advisory and never starts another assessment.</p>
        </fieldset>
        </ServiceDisclosure>
        <div className="security-actions"><button form={formId} className="product-primary" type="submit" disabled={busy || evidenceUncertain || !capabilities.length || conflicted || (needFreshEdit && !editedSinceRefresh)}>Save run settings</button><span className="product-muted">Saves setup and schedule edits together.</span></div>
      </fieldset>}
      </div>
    </ServiceSection>
    <ServiceSection service="security" step="results" description="Review findings and mitigations from completed or partial assessments.">
      {resultsPanel ?? <p>Results appear here after an assessment produces output.</p>}
    </ServiceSection>
  </section></ProductSourceNameContext.Provider>;
}
