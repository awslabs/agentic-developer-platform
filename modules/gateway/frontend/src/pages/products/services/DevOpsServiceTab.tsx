import { sameConsentScope } from '@/services/l5OwnerConsent';
import DevOpsSavedSetup, { DevOpsSavedReferences } from './DevOpsSavedSetup';
import DevOpsSetupPanel from './DevOpsSetupPanel';
import DevOpsAccessSummary from './DevOpsAccessSummary';
import { DevOpsBadge } from './DevOpsPresentation';
import L5OwnerConsent from './L5OwnerConsent';
import DevOpsActivationSetup from './DevOpsActivationSetup';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { getDevOpsConfiguration, getDevOpsOptions, matchesSecurityScope, saveDevOpsConfiguration, type DevOpsCapability, type DevOpsConfiguration, type DevOpsOptions, type DevOpsSettings, type Versioned } from '@/services/l5Services';
import type { RecordReference } from '@/services/products';
import DevOpsOperations, { type DevOpsOperationSections } from './DevOpsOperations';
import { ServiceJourney, ServiceSection } from './L5ServiceJourney';

interface Props { productId: string; projectId: string; tenantId: string }
const label = (reference: RecordReference) => `${reference.record_type} ${reference.record_id} (revision ${reference.record_revision})`;
const scoped = (reference: RecordReference, tenantId: string, projectId: string) => reference.tenant_id === tenantId && reference.project_id === projectId;
const denied = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error && (error.status === 403 || error.status === 404);

export default function DevOpsServiceTab(props: Props) {
  return <DevOpsConfigurationView key={`${props.tenantId}:${props.productId}:${props.projectId}`} {...props} />;
}

function DevOpsConfigurationView({ productId, projectId, tenantId }: Props) {
  const [panel, setPanel] = useState<'edit' | 'access' | 'workspace' | 'technical' | null>(null);
  const [accessRefresh, setAccessRefresh] = useState(0);
  const [activationNotice, setActivationNotice] = useState('');
  const [accessNotice, setAccessNotice] = useState('');
  const closePanel = () => { setPanel(null); setAccessRefresh(value => value + 1); };
  const [options, setOptions] = useState<DevOpsOptions | null>(null);
  const [configuration, setConfiguration] = useState<Versioned<DevOpsConfiguration> | null>(null);
  const [capabilities, setCapabilities] = useState<DevOpsCapability[]>([]);
  const [hosting, setHosting] = useState('');
  const [accessPolicy, setAccessPolicy] = useState('{}');
  const [region, setRegion] = useState('');
  const [spaceMode, setSpaceMode] = useState<'create' | 'adopt'>('create');
  const [existingSpace, setExistingSpace] = useState('');
  const [sources, setSources] = useState<string[]>([]);
  const [sourcesTouched, setSourcesTouched] = useState(false);
  const [accounts, setAccounts] = useState('[]');
  const [integrations, setIntegrations] = useState('[]');
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [stale, setStale] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const generation = useRef(0);
  const hadConfiguration = useRef(false);
  const onAccessDenied = useCallback(() => { setRevoked(true); setConfiguration(null); setOptions(null); }, []);

  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError('');
    try {
      const choices = await getDevOpsOptions(productId, projectId);
      let current: Versioned<DevOpsConfiguration> | null = null;
      try {
        current = await getDevOpsConfiguration(productId, projectId);
      } catch (readError) {
        if (!(denied(readError) && typeof readError === 'object' && readError !== null && 'status' in readError
          && readError.status === 404 && !hadConfiguration.current)) throw readError;
      }
      if (request !== generation.current) return;
      if ([...choices.connections, ...choices.sources].some(ref => !scoped(ref, tenantId, projectId))
        || (current && (!matchesSecurityScope(current.value.scope, current.value.scope, productId, projectId)
          || current.value.scope.tenant_id !== tenantId || current.value.settings.service_id !== 'devops'
          || !scoped(current.value.settings.profile_ref, tenantId, projectId)
          || current.value.settings.profile_ref.record_type !== 'product'
          || current.value.settings.profile_ref.record_id !== productId))) throw { status: 403 };
      hadConfiguration.current ||= Boolean(current);
      setOptions(choices);
      setConfiguration(current);
      setCapabilities(current?.value.settings.capabilities ?? (choices.capabilities.includes('investigation') ? ['investigation'] : []));
      const saved = current?.value.settings.devops;
      setHosting(String(choices.connections.findIndex(ref => sameConsentScope(ref, saved?.hosting_connection_ref))));
      setAccessPolicy(JSON.stringify(saved?.access_policy_ref ?? {}, null, 2));
      setRegion(saved?.region ?? '');
      setSpaceMode(saved?.space_mode ?? 'create');
      setExistingSpace(saved?.existing_space_id ?? '');
      setSources(choices.sources.flatMap((ref, index) => current?.value.settings.source_refs.some(source => sameConsentScope(source, ref)) ? [String(index)] : []));
      setSourcesTouched(false);
      setAccounts(JSON.stringify(saved?.accounts ?? [], null, 2));
      setIntegrations(JSON.stringify(saved?.integrations ?? [], null, 2));
      setReason('');
      setStale(false);
      setRevoked(false);
    } catch (readError) {
      if (request !== generation.current) return;
      setConfiguration(null);
      setOptions(null);
      if (denied(readError)) setRevoked(true);
      setError(denied(readError) ? 'DevOps access is unavailable for this project.' : 'DevOps configuration could not be loaded. Retry to recover.');
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [productId, projectId, tenantId]);

  useEffect(() => {
    void load();
    return () => { generation.current += 1; };
  }, [load]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!options || saving || loading || revoked || stale) return;
    const host = options.connections[Number(hosting)];
    let policy: RecordReference | undefined;
    try {
      policy = accessPolicy.trim() && accessPolicy.trim() !== '{}' ? JSON.parse(accessPolicy) as RecordReference : undefined;
      if (policy && ( !scoped(policy, tenantId, projectId) || !['record_type', 'record_id', 'record_revision', 'source_connection_id'].every(field =>
        typeof policy![field as keyof RecordReference] === 'string' && policy![field as keyof RecordReference]))) throw new Error();
    } catch {
      setError('Choose a revisioned access policy reference from this project.'); return;
    }
    if (!host || configuration?.value.settings.enabled && !policy || !region.trim() || !capabilities.length || (spaceMode === 'adopt' && !existingSpace.trim())) {
      setError('Choose capabilities, hosting connection, access policy, Region and space before saving.'); return;
    }
    const selectedSources = options.sources.filter((_, index) => sources.includes(String(index)));
    const overrides: DevOpsSettings['overrides'] = configuration?.value.settings.overrides.filter(item =>
      (item.field !== 'source_refs' || !sourcesTouched) && (item.field !== 'region' || region === (configuration.value.settings.region ?? configuration.value.settings.devops.region))
    ) ?? [];
    if (sourcesTouched) overrides.push({ field: 'source_refs', value: selectedSources, reason: reason.trim() });
    if (region !== (configuration?.value.settings.region ?? configuration?.value.settings.devops.region) && region) overrides.push({ field: 'region', value: region.trim(), reason: reason.trim() });
    if ((sourcesTouched || region !== (configuration?.value.settings.region ?? configuration?.value.settings.devops.region)) && overrides.some(item => !item.reason.trim())) { setError('Explain the explicit Region or environment override before saving.'); return; }
    let selectedAccounts: unknown[];
    let selectedIntegrations: unknown[];
    try {
      selectedAccounts = JSON.parse(accounts) as unknown[];
      selectedIntegrations = JSON.parse(integrations) as unknown[];
      if (!Array.isArray(selectedAccounts) || !Array.isArray(selectedIntegrations)) throw new Error();
    } catch {
      setError('Accounts and integrations must each be JSON arrays of explicit, scoped selections.'); return;
    }
    const devops: DevOpsSettings['devops'] = {
      ...(configuration?.value.settings.devops ?? {}), hosting_connection_ref: host, ...(policy ? { access_policy_ref: policy } : {}),
      region: region.trim(), space_mode: spaceMode, ...(spaceMode === 'adopt' ? { existing_space_id: existingSpace.trim() } : { existing_space_id: undefined }),
      accounts: selectedAccounts, integrations: selectedIntegrations, authentication_mode: 'scoped_iam_federation',
      permitted_native_actions: configuration?.value.settings.devops.permitted_native_actions ?? [],
      knowledge_policy: configuration?.value.settings.devops.knowledge_policy ?? { source_refs: [], visibility: 'only_me', publish_results: false, refresh: 'manual' },
      trigger_policies: configuration?.value.settings.devops.trigger_policies ?? [], prevention_enabled: configuration?.value.settings.devops.prevention_enabled ?? false,
      retention_days: configuration?.value.settings.devops.retention_days ?? 30,
      browser_launch_policy: configuration?.value.settings.devops.browser_launch_policy ?? 'disabled',
    };
    if (spaceMode === 'create') delete devops.existing_space_id;
    const choices: Partial<DevOpsSettings> = {
      service_id: 'devops', capabilities, enabled: configuration?.value.settings.enabled ?? false, triggers: configuration?.value.settings.triggers ?? [], devops,
      ...(sourcesTouched ? { source_refs: selectedSources } : {}), ...(region ? { region: region.trim() } : {}), overrides,
    };
    const request = generation.current;
    setSaving(true); setError(''); setMessage('');
    try {
      const saved = await saveDevOpsConfiguration(productId, projectId, choices, configuration?.etag);
      if (request !== generation.current) return;
      if (!matchesSecurityScope(saved.value.scope, saved.value.scope, productId, projectId) || saved.value.scope.tenant_id !== tenantId) throw { status: 403 };
      setConfiguration(saved);
      hadConfiguration.current = true;
      setSourcesTouched(false);
      setMessage('Setup saved. Run access is checked before starting an investigation.');
      setPanel(null);
    } catch (saveError) {
      if (request !== generation.current) return;
      if (denied(saveError)) { setRevoked(true); setOptions(null); setConfiguration(null); setError('DevOps access is unavailable. Recheck access before continuing.'); }
      else if (typeof saveError === 'object' && saveError !== null && 'status' in saveError && saveError.status === 412) {
        setStale(true); setError('DevOps configuration changed elsewhere. Your edits remain visible; refresh and review before saving again.');
      } else { setStale(true); setError('DevOps save could not be confirmed. Your edits remain visible; refresh and review the current revision before making another request.'); }
    } finally { if (request === generation.current) setSaving(false); }
  }

  const savedSettings = configuration?.value.settings;
  const savedDevOps = savedSettings?.devops;
  const hasEdits = savedSettings && savedDevOps ? Boolean(sourcesTouched
    || region !== savedDevOps.region || spaceMode !== savedDevOps.space_mode || existingSpace !== (savedDevOps.existing_space_id ?? '')
    || hosting !== String(options?.connections.findIndex(ref => sameConsentScope(ref, savedDevOps.hosting_connection_ref)))
    || !sameConsentScope(capabilities, savedSettings.capabilities)
    || accounts !== JSON.stringify(savedDevOps.accounts, null, 2) || integrations !== JSON.stringify(savedDevOps.integrations, null, 2)
    || accessPolicy !== JSON.stringify(savedDevOps.access_policy_ref ?? {}, null, 2))
    : Boolean(options && (sourcesTouched || hosting !== '-1' || region || existingSpace || spaceMode !== 'create'
      || accounts !== '[]' || integrations !== '[]' || accessPolicy !== '{}'
      || !sameConsentScope(capabilities, options.capabilities.includes('investigation') ? ['investigation'] : [])));

  const journey = (operations?: DevOpsOperationSections) => <ServiceJourney service="devops" compact>
    {loading && <p role="status">Loading DevOps configuration…</p>}
    {error && panel !== 'edit' && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {operations?.status}
    <ServiceSection service="devops" step="setup" description="Your workload, connection and workspace at a glance.">
      {stale && <p className="devops-next-step">Refresh the saved setup before continuing. Your local edits are kept until you choose to refresh.</p>}
      <div className="devops-setup-overview">
        <section className="devops-panel" aria-label="Workload">
          <div className="devops-panel-heading"><h3>Workload</h3><button type="button" onClick={() => setPanel('edit')} disabled={loading || saving || revoked || operations?.workspace?.busy}>Change setup</button></div>
          {configuration ? <DevOpsSavedSetup configuration={configuration.value} projectId={projectId} /> : <p>Choose the Product environment and AWS connection to begin.</p>}
          {(activationNotice || configuration && !configuration.value.settings.enabled) && <div className="devops-next-step">
            <p>{activationNotice || 'Workload setup is not complete.'}</p><button type="button" onClick={() => setPanel('edit')}>Complete workload setup</button>
          </div>}
        </section>
        <section className="devops-panel" aria-label="AWS connection">
          <div className="devops-panel-heading"><h3>AWS connection</h3><button type="button" onClick={() => setPanel('access')} disabled={!configuration || revoked}>Manage access</button></div>
          <dl className="devops-saved-settings"><div><dt>AWS account</dt><dd>{configuration?.value.settings.devops.accounts.map(account => (account as { account_id?: string }).account_id).filter(Boolean).join(', ') || operations?.workspace?.saved?.provider_identity?.account_id || 'Not configured'}</dd></div>
          <div><dt>Connection</dt><dd>{configuration && options?.connections.some(ref => sameConsentScope(ref, configuration.value.settings.devops.hosting_connection_ref)) ? 'Selected from Product connections' : 'Choose a current AWS connection'}</dd></div></dl>
          {configuration && <DevOpsAccessSummary configuration={configuration.value} binding={operations?.workspace?.saved} refreshKey={accessRefresh} />}
          {accessNotice && <p className="devops-next-step">{accessNotice}</p>}
        </section>
        <section className="devops-panel" aria-label="DevOps workspace">
          <div className="devops-panel-heading"><h3>DevOps workspace</h3><button type="button" className={operations?.workspace?.nativeReady ? 'product-primary' : ''} disabled={!configuration || operations?.workspace?.busy}
            onClick={() => operations?.workspace?.nativeReady ? operations.workspace.open() : setPanel('workspace')}>
            {operations?.workspace?.nativeReady ? 'Open workspace' : !operations?.workspace?.saved ? 'Connect workspace' : configuration?.value.settings.devops.browser_launch_policy === 'disabled' ? 'Set up sign-in' : 'Open workspace'}</button></div>
          <dl className="devops-saved-settings"><div><dt>Workspace</dt><dd>{operations?.workspace?.saved ? <><DevOpsBadge value="linked" /><small>{operations.workspace.saved.provider_identity?.region ?? configuration?.value.settings.devops.region} · {operations.workspace.saved.ownership === 'adp_created' ? 'Created by ADP' : 'Linked to ADP'}</small></> : 'Not linked'}</dd></div>
          <div><dt>ADP sign-in</dt><dd>{operations?.workspace?.nativeReady ? 'Access checked' : configuration?.value.settings.devops.browser_launch_policy === 'disabled' ? 'Not configured' : 'Check access before opening'}</dd></div></dl>
          <p className="product-muted">Workspace sign-in is separate from permission to run investigations.</p>
          {(operations?.workspace?.notice) && <p className="devops-next-step">{operations?.workspace?.notice}</p>}
        </section>
      </div>
      <div className="devops-setup-footer">
        <button type="button" className="product-text-button" onClick={() => setPanel('technical')}>Technical details</button>
        <button type="button" className="product-text-button" disabled={loading || saving || operations?.workspace?.busy} onClick={() => {
          if ((!hasEdits && !stale) || window.confirm('Refresh saved setup? Any unsaved setup edits will be discarded.')) { closePanel(); void load(); }
        }}>Refresh setup</button>
      </div>
    </ServiceSection>
    <DevOpsSetupPanel title="Edit DevOps setup" open={panel === 'edit'} onClose={closePanel} busy={saving}>
      <p className="product-muted">Change the saved environment and AWS connection. These changes apply after saving.</p>
      {error && <p role="alert">{error}</p>}
      {stale && <p>Setup changed. Close this panel and refresh setup before saving again.</p>}
      {options && !revoked && <form onSubmit={event => void save(event)} className="devops-edit-form">
        <div className="devops-form-grid">
          <label>AWS Region <input value={region} onChange={event => setRegion(event.target.value)} list="devops-regions" /></label>
          <datalist id="devops-regions">{options.regions.map(value => <option key={value} value={value} />)}</datalist>
          <fieldset><legend>Selected project environments</legend>{options.sources.map((ref, index) => <label key={index}><input type="checkbox" checked={sources.includes(String(index))} onChange={event => { setSourcesTouched(true); setSources(current => event.target.checked ? [...current, String(index)] : current.filter(item => item !== String(index))); }} /> {ref.record_id}</label>)}</fieldset>
          <label>Hosting AWS connection <select value={hosting} onChange={event => setHosting(event.target.value)}><option value="-1">Choose connection</option>{options.connections.map((ref, index) => <option key={index} value={index}>{label(ref)}</option>)}</select></label>
          <label>Workspace provisioning <select value={spaceMode} onChange={event => setSpaceMode(event.target.value as 'create' | 'adopt')}><option value="create">Create and manage with ADP</option><option value="adopt">Use an existing AWS workspace</option></select></label>
          {spaceMode === 'adopt' && <label>Existing workspace ID <input value={existingSpace} onChange={event => setExistingSpace(event.target.value)} /></label>}
        </div>
        {(sourcesTouched || region !== (configuration?.value.settings.region ?? configuration?.value.settings.devops.region)) && <label>Reason for explicit Region or environment override <input value={reason} onChange={event => setReason(event.target.value)} /></label>}
        <details><summary>Advanced configuration</summary>
          <p>These references are saved for this Product and project.</p>
          <label>Project access policy reference (revisioned JSON) <textarea value={accessPolicy} onChange={event => setAccessPolicy(event.target.value)} /></label>
          <label>Accounts and workload references (JSON array) <textarea value={accounts} onChange={event => setAccounts(event.target.value)} /></label>
          <label>Integration choices (JSON array) <textarea value={integrations} onChange={event => setIntegrations(event.target.value)} /></label>
          <label><input type="checkbox" checked={capabilities.includes('investigation')} onChange={event => setCapabilities(current => event.target.checked ? [...current.filter(item => item !== 'investigation'), 'investigation'] : current.filter(item => item !== 'investigation'))} /> Enable Investigation</label>
          {capabilities.some(capability => capability !== 'investigation') && <button type="button" onClick={() => setCapabilities(current => current.filter(capability => capability === 'investigation'))}>Remove unavailable selections from draft</button>}
        </details>
        <div className="devops-actions"><button className="product-primary" type="submit" disabled={loading || saving || stale || !capabilities.length}>Save changes</button><span className="product-muted">Closing keeps your draft until you refresh.</span></div>
      </form>}
      {configuration && <DevOpsActivationSetup productId={productId} projectId={projectId} configuration={configuration}
        disabled={loading || saving || stale || revoked} onAttention={setActivationNotice} onActivated={(current, confirmedRevision) => {
          setConfiguration(current); setStale(true);
          setMessage(confirmedRevision && confirmedRevision !== current.value.revision
            ? `Activation is confirmed in revision ${confirmedRevision}. Refresh and review current revision ${current.value.revision} before running Investigation.`
            : 'Investigation setup is saved. Refresh setup and review owner permission before starting a run.');
        }} />}
    </DevOpsSetupPanel>
    <DevOpsSetupPanel title="Manage investigation access" open={panel === 'access'} onClose={closePanel}>
      <L5OwnerConsent service="devops" productId={productId} projectId={projectId} configuration={configuration?.value ?? null} disabled={loading || saving || stale || revoked} onAttention={setAccessNotice} />
    </DevOpsSetupPanel>
    <DevOpsSetupPanel title="Manage DevOps workspace" open={panel === 'workspace'} onClose={closePanel}>
      {operations?.nativeAccess}
      {operations?.nativeConsent}
      <section className="devops-setup-fields"><h3>Workspace setup and recovery</h3>{operations?.setup}</section>
    </DevOpsSetupPanel>
    <DevOpsSetupPanel title="Technical details" open={panel === 'technical'} onClose={closePanel}>
      {configuration && <DevOpsSavedReferences configuration={configuration.value} productId={productId} projectId={projectId} />}
      {options && <p>Setup diagnostics: {options.unavailable_reasons.join(', ') || 'none reported'}</p>}
      {operations?.capabilities}
      <button type="button" onClick={() => setPanel('workspace')}>Review workspace setup receipts</button>
      <button type="button" onClick={() => setPanel('edit')}>Review workload setup receipts</button>
    </DevOpsSetupPanel>
    <ServiceSection service="devops" step="runs" description="Ask a question about the saved workload and follow the investigation.">
      {operations?.runs ?? <p>Complete Setup before starting an investigation.</p>}
    </ServiceSection>
    <ServiceSection service="devops" step="results" description="Read the investigation summary, evidence and recommended next steps.">
      {operations?.results ?? <p>Results will appear here after an investigation.</p>}
    </ServiceSection>
  </ServiceJourney>;

  return <section aria-label="DevOps configuration" className="devops-workspace space-y-4">
    <span className="sr-only">Product {productId} · Project {projectId}.</span>
    {configuration && !revoked
      ? <DevOpsOperations productId={productId} projectId={projectId} configuration={configuration} onAccessDenied={onAccessDenied} onConfigurationChanged={setConfiguration}>{journey}</DevOpsOperations>
      : journey()}
  </section>;
}
