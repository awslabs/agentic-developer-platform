import L5OwnerConsent from './L5OwnerConsent';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import TransformRunsResults from './TransformRunsResults';
import { CapabilitySummary, ProfileSetupSummary, ScheduleAvailability, ServiceJourney, ServiceSection, ServiceDisclosure } from './L5ServiceJourney';
import {
  getTransformConfiguration, getTransformConfigurationVersion, getTransformOptions, saveTransformConfiguration,
  type TransformConfiguration, type TransformOptions, type TransformSettings, type Versioned,
} from '@/services/l5Services';

interface Props { productId: string; projectId: string; tenantId: string }

const initialSettings = JSON.stringify({ region: '', read_paths: [], write_paths: [], excluded_paths: [],
  goal_defaults: { kind: 'refactor', summary: '' } }, null, 2);
type UpgradeTarget = { kind: 'dependency' | 'runtime'; name: string; version: string };
const targetRecord = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const validUpgradeTarget = (value: unknown): value is UpgradeTarget => {
  const target = targetRecord(value);
  return ['dependency', 'runtime'].includes(String(target.kind))
    && [target.name, target.version].every(item => typeof item === 'string' && item.trim().length > 0 && item.length <= 255);
};
const statusOf = (error: unknown) => typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
const readDraftSettings = (settings: string): Partial<TransformSettings> | null => {
  try {
    const value: unknown = JSON.parse(settings);
    return value && typeof value === 'object' && !Array.isArray(value) ? value as Partial<TransformSettings> : null;
  } catch { return null; }
};

export default function TransformServiceTab({ productId, projectId, tenantId }: Props) {
  const [options, setOptions] = useState<TransformOptions | null>(null);
  const [configuration, setConfiguration] = useState<Versioned<TransformConfiguration> | null>(null);
  const [history, setHistory] = useState<TransformConfiguration | null>(null);
  const [source, setSource] = useState('');
  const [connection, setConnection] = useState('');
  const [capability, setCapability] = useState<TransformOptions['capabilities'][number] | ''>('');
  const [settings, setSettings] = useState(initialSettings);
  const [enabled, setEnabled] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [conflicted, setConflicted] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const previouslyRead = useRef(false);

  const onRunAccessDenied = useCallback(() => {
    setRevoked(true); setOptions(null); setConfiguration(null); setHistory(null);
    setError('Transform access is unavailable for this workspace. Previously shown details were removed.');
  }, []);

  const validScope = useCallback((value: TransformConfiguration) =>
    value.scope.tenant_id === tenantId && value.scope.product_id === productId && value.scope.project_id === projectId
    && value.settings.service_id === 'transform'
    && value.settings.profile_ref?.tenant_id === tenantId && value.settings.profile_ref?.project_id === projectId
    && value.settings.profile_ref?.record_type === 'product' && value.settings.profile_ref?.record_id === productId,
  [tenantId, productId, projectId]);

  const load = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const choices = await getTransformOptions(productId, projectId);
      if (choices.service_id !== 'transform' || [...choices.connections, ...choices.sources].some(ref =>
        ref.tenant_id !== tenantId || ref.project_id !== projectId)) throw { status: 403 };
      let current: Versioned<TransformConfiguration> | null = null;
      try {
        current = await getTransformConfiguration(productId, projectId);
      } catch (readError) {
        if (statusOf(readError) !== 404 || previouslyRead.current) throw readError;
      }
      if (current && !validScope(current.value)) throw { status: 403 };
      if (current) previouslyRead.current = true;
      setOptions(choices);
      setConfiguration(current);
      setHistory(null);
      setSource(current ? String(choices.sources.findIndex(ref => JSON.stringify(ref) === JSON.stringify(current.value.settings.source_refs[0]))) : '');
      setConnection(current ? String(choices.connections.findIndex(ref => JSON.stringify(ref) === JSON.stringify(current.value.settings.transform.connection_ref))) : '');
      setCapability(current?.value.settings.capabilities[0] ?? choices.capabilities[0] ?? '');
      setSettings(current ? JSON.stringify(current.value.settings.transform, null, 2) : initialSettings);
      setEnabled(current?.value.settings.enabled ?? false);
      setReason('');
      setConflicted(false);
      setRevoked(false);
      setMessage('');
    } catch (loadError) {
      setOptions(null);
      setConfiguration(null);
      setHistory(null);
      if (statusOf(loadError) === 403 || statusOf(loadError) === 404) {
        setRevoked(true);
        setError('Transform access is unavailable for this workspace. Previously shown details were removed.');
      } else setError('Transform configuration could not be loaded. Try again.');
    } finally { setBusy(false); }
  }, [productId, projectId, tenantId, validScope]);

  useEffect(() => { void load(); }, [load]);

  async function showVersion(revision: number) {
    if (!configuration || busy) return;
    setBusy(true);
    setError('');
    try {
      const version = await getTransformConfigurationVersion(productId, projectId, revision);
      if (!validScope(version.value) || version.value.configuration_id !== configuration.value.configuration_id) throw { status: 403 };
      setHistory(version.value);
    } catch (readError) {
      if (statusOf(readError) === 403 || statusOf(readError) === 404) {
        setConfiguration(null); setOptions(null); setHistory(null); setRevoked(true);
        setError('Transform access is unavailable for this workspace. Previously shown details were removed.');
      } else setError('Configuration history could not be loaded.');
    } finally { setBusy(false); }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!options || busy || conflicted || !capability || source === '' || connection === '') return;
    const chosenSource = options.sources[Number(source)];
    const chosenConnection = options.connections[Number(connection)];
    if (!chosenSource || !chosenConnection) return;
    const previous = configuration?.value.settings;
    const sourceChanged = JSON.stringify([chosenSource]) !== JSON.stringify(previous?.source_refs ?? []);
    if (sourceChanged && !reason.trim()) {
      setError('Explain why this repository revision differs from the inherited Product source.');
      return;
    }
    let transform: TransformSettings;
    try {
      const parsed: unknown = JSON.parse(settings);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid settings');
      transform = { ...(parsed as TransformSettings), connection_ref: chosenConnection };
      if (capability === 'dependency_upgrade') {
        const target = transform.goal_defaults?.target;
        if (!validUpgradeTarget(target)) {
          setError('Choose an upgrade target type, name and version before saving.');
          return;
        }
        transform.goal_defaults = { ...transform.goal_defaults, target: { ...target, name: target.name.trim(), version: target.version.trim() } };
      }
    } catch {
      setError('Transform settings must be a JSON object with a valid region, paths and goal.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const existingSourceOverride = previous?.overrides.find(item => item.field === 'source_refs');
      const overrides = (previous?.overrides ?? []).filter(item => item.field !== 'source_refs');
      if (sourceChanged) overrides.push({ field: 'source_refs', reason: reason.trim(), value: [chosenSource] });
      else if (existingSourceOverride) overrides.push(existingSourceOverride);
      const saved = await saveTransformConfiguration(productId, projectId, {
        service_id: 'transform', capabilities: [capability], enabled,
        ...(sourceChanged || existingSourceOverride ? { source_refs: [chosenSource] } : {}), transform, overrides,
      }, configuration?.etag);
      if (!validScope(saved.value)) throw { status: 403 };
      previouslyRead.current = true;
      setConfiguration(saved);
      setHistory(null);
      setConflicted(false);
      setMessage('Configuration saved. Readiness is reported by the service; no run or publication was started.');
    } catch (saveError) {
      if (statusOf(saveError) === 412) {
        setConflicted(true); setConfiguration(null); setHistory(null);
        setError('Configuration changed elsewhere. Refresh before editing; stale changes will not be replayed.');
      } else if (statusOf(saveError) === 403 || statusOf(saveError) === 404) {
        setRevoked(true); setOptions(null); setConfiguration(null); setHistory(null);
        setError('Transform access is unavailable for this workspace. Previously shown details were removed.');
      } else setError(`Configuration was not saved${typeof statusOf(saveError) === 'number' ? ` (HTTP ${statusOf(saveError)})` : ''}.`);
    } finally { setBusy(false); }
  }

  const draftSettings = readDraftSettings(settings);
  const target = targetRecord(draftSettings?.goal_defaults?.target);
  const upgradeTargetMissing = capability === 'dependency_upgrade' && !validUpgradeTarget(target);
  function updateDraftSettings(change: Partial<TransformSettings>) {
    if (draftSettings) setSettings(JSON.stringify({ ...draftSettings, ...change }, null, 2));
  }

  function updateUpgradeTarget(change: Partial<UpgradeTarget>) {
    if (!draftSettings || capability !== 'dependency_upgrade') return;
    updateDraftSettings({ goal_defaults: { ...draftSettings.goal_defaults, kind: capability,
      summary: draftSettings.goal_defaults?.summary ?? '',
      target: { kind: target.kind ?? 'dependency', name: target.name ?? '', version: target.version ?? '', ...change } } });
  }

  return <ServiceJourney service="transform">
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {(revoked || conflicted || !options) && <button type="button" onClick={() => void load()} disabled={busy}>Refresh Transform access and configuration</button>}
    <form onSubmit={event => void save(event)} className="space-y-4">
    <ServiceSection service="transform" step="setup" description="Review the Product profile and choose authorized source and connection revisions.">
      <ProfileSetupSummary configuration={configuration?.value} productId={productId} projectId={projectId} />
      <p>New Transform binding setup: Not available yet. Manual runs require an existing linked binding with a qualified executor and test policy. Saving references does not create or qualify a binding.</p>
      {options && !revoked ? <>
      {options.unavailable_reasons.length > 0 && <p>Unavailable inputs: {options.unavailable_reasons.join(', ')}</p>}
      {configuration && <ServiceDisclosure title="Configuration history and status"><p>Revision {configuration.value.revision}: {configuration.value.state}; gaps: {configuration.value.gaps.join(', ') || 'none'}; handoff: {configuration.value.handoff_state}</p>
        {configuration.value.gaps.some(gap => ['executor_unqualified', 'executor_qualification_stale'].includes(gap)) && <p>Ask the Transform service operator to qualify the saved executor and test policy for this existing binding, then refresh configuration and run readiness. Saving the references again does not qualify the executor.</p>}
        <label>Configuration history <select aria-label="Configuration history" value={history?.revision ?? ''} onChange={event => event.target.value ? void showVersion(Number(event.target.value)) : setHistory(null)}>
          <option value="">Current revision</option>
          {Array.from({ length: configuration.value.revision }, (_, index) => index + 1).map(revision =>
            <option key={revision} value={revision}>Revision {revision}</option>)}
        </select></label>
        {history && <p role="status">Historical revision {history.revision}: {history.state}; gaps: {history.gaps.join(', ') || 'none'}. Read-only.</p>}
      </ServiceDisclosure>}
      <ServiceDisclosure title="Edit Transform setup" hint="Review or change the service settings" open={!configuration}>
        <label>Repository commit <select value={source} onChange={event => setSource(event.target.value)}>
          <option value="">Choose an authorized source revision</option>
          {options.sources.filter(ref => ref.record_type === 'repository_commit').map(ref => {
            const index = options.sources.indexOf(ref);
            return <option key={index} value={index}>{ref.record_id} ({ref.record_revision})</option>;
          })}
        </select></label>
        <label>Connection <select value={connection} onChange={event => setConnection(event.target.value)}>
          <option value="">Choose an authorized connection</option>
          {options.connections.map((ref, index) => <option key={index} value={index}>{ref.record_id} ({ref.record_revision})</option>)}
        </select></label>
        <label>Transform region <input value={typeof draftSettings?.region === 'string' ? draftSettings.region : ''} disabled={!draftSettings}
          onChange={event => updateDraftSettings({ region: event.target.value })} /></label>
        <label>Reason for explicit repository choice <input value={reason} onChange={event => setReason(event.target.value)} /></label>
        <p>Save setup and the goal, capability and enablement choices in Capabilities together before reviewing owner consent.</p>
        <button className="product-primary" type="submit" disabled={busy || conflicted || !capability || source === '' || connection === '' || upgradeTargetMissing}>Save Transform configuration</button>
      </ServiceDisclosure>
      </> : <p>{revoked ? 'Setup details are unavailable for this workspace.' : busy ? 'Loading authorized setup choices.' : 'Setup choices are unavailable. Refresh access and configuration to try again.'}</p>}
      <L5OwnerConsent service="transform" productId={productId} projectId={projectId} configuration={configuration?.value ?? null} disabled={busy || conflicted || revoked} />
    </ServiceSection>
    <ServiceSection service="transform" step="capabilities" description="Choose a supported transformation and its goal, then save the configuration in Setup.">
      {options && !revoked ? <>
        <CapabilitySummary service="transform" capabilities={options.capabilities} selected={capability ? [capability] : []} />
        {options.readiness?.map(item => <p key={item.capability}>{item.capability}: {item.status} — {item.gaps.map(gap => gap.code).join(', ') || 'no reported gaps'}</p>)}
        <label>Capability <select value={capability} onChange={event => {
          const selected = event.target.value as typeof capability;
          setCapability(selected);
          if (selected) updateDraftSettings({ goal_defaults: { kind: selected, summary: draftSettings?.goal_defaults?.summary ?? '',
            ...(selected === 'dependency_upgrade' ? { target: draftSettings?.goal_defaults?.target ?? { kind: 'dependency', name: '', version: '' } } : {}) } });
        }}>
          <option value="">Choose a selected Product capability</option>
          {options.capabilities.map(item => <option key={item} value={item}>{item.replace('_', ' ')}</option>)}
        </select></label>
        <label>Transformation goal <textarea value={typeof draftSettings?.goal_defaults?.summary === 'string' ? draftSettings.goal_defaults.summary : ''} disabled={!draftSettings || !capability}
          onChange={event => capability && updateDraftSettings({ goal_defaults: { ...draftSettings?.goal_defaults, kind: capability, summary: event.target.value } })} /></label>
        {capability === 'dependency_upgrade' && <fieldset disabled={!draftSettings || busy || conflicted}>
          <legend>Upgrade target</legend>
          <p>Choose the dependency or runtime to upgrade and its requested version. This target is required for dependency upgrades.</p>
          <label>Upgrade target type <select value={typeof target.kind === 'string' ? target.kind : ''} onChange={event => updateUpgradeTarget({ kind: event.target.value as UpgradeTarget['kind'] })}>
            <option value="" disabled>Choose target type</option><option value="dependency">Dependency</option><option value="runtime">Runtime</option>
          </select></label>
          <label>Upgrade target name <input maxLength={255} value={typeof target.name === 'string' ? target.name : ''} onChange={event => updateUpgradeTarget({ name: event.target.value })} /></label>
          <label>Requested target version <input maxLength={255} value={typeof target.version === 'string' ? target.version : ''} onChange={event => updateUpgradeTarget({ version: event.target.value })} /></label>
          {upgradeTargetMissing && <p>Choose a target type and enter its name and requested version before saving.</p>}
        </fieldset>}
        <details>
          <summary>Advanced Transform settings</summary>
          <label>Transform settings (JSON) <textarea value={settings} onChange={event => setSettings(event.target.value)} rows={12} className="block w-full rounded border p-2" /></label>
          <p>Specify scoped executor and test-policy references and read/write/excluded paths. The region and goal above share this draft. Missing inputs and unqualified executors remain visible.</p>
        </details>
        {!draftSettings && <p role="alert">Fix the JSON object in Advanced Transform settings to edit the region and goal.</p>}
        <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} /> Enable configuration</label>
        <p>Save these selections with Save Transform configuration in Setup.</p>
      </> : <p>Capability choices appear after authorized setup is loaded.</p>}
    </ServiceSection>
    </form>
    {configuration && options && !revoked
      ? <TransformRunsResults productId={productId} projectId={projectId} tenantId={tenantId} configuration={configuration} onAccessDenied={onRunAccessDenied} />
      : <>
        <ServiceSection service="transform" step="runs"><ScheduleAvailability service="transform" /><p>Save an authorized configuration to check readiness and view runs. Execution also requires an existing linked, qualified binding; new binding setup is not available yet.</p></ServiceSection>
        <ServiceSection service="transform" step="results"><p>Retained results appear after an authorized configuration is loaded.</p></ServiceSection>
      </>}
  </ServiceJourney>;
}
