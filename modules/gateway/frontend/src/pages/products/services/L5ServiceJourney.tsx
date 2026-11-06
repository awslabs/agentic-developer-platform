import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import './l5ServiceJourney.css';

export type L5Service = 'security' | 'devops' | 'finops' | 'transform' | 'well_architected';
export type ServiceStep = 'setup' | 'capabilities' | 'runs' | 'results';
const names: Record<L5Service, string> = { security: 'Security', devops: 'DevOps', finops: 'FinOps', transform: 'Transform', well_architected: 'Well-Architected' };
const steps: { id: ServiceStep; title: string; description: string }[] = [
  { id: 'setup', title: 'Setup', description: 'See what is already saved and what you need to add.' },
  { id: 'capabilities', title: 'Capabilities', description: 'Choose what the agent can do with this setup. Availability and run readiness are checked separately.' },
  { id: 'runs', title: 'Runs & schedules', description: 'Start work with the saved setup and follow its progress. Scheduled work uses the same setup and authorization checks.' },
  { id: 'results', title: 'Results', description: 'Review outputs and retained evidence from previous runs.' },
];

const SectionContext = createContext<ServiceStep | null>(null);
const CompactContext = createContext(false);
const JourneyNavigationContext = createContext<((hash: string) => void) | undefined>(undefined);
const RequestedCapabilityContext = createContext<string | null>(null);
export const useRequestedCapability = () => useContext(RequestedCapabilityContext);
export function CompactServiceJourney({ children, capability = null, onNavigate }: { children: ReactNode; capability?: string | null; onNavigate?: (hash: string) => void }) {
  return <CompactContext.Provider value><JourneyNavigationContext.Provider value={onNavigate}><RequestedCapabilityContext.Provider value={capability}>{children}</RequestedCapabilityContext.Provider></JourneyNavigationContext.Provider></CompactContext.Provider>;
}
const stepFromHash = (service: L5Service): ServiceStep => {
  const step = steps.find(item => window.location.hash === `#l5-${service}-${item.id}`)?.id ?? 'setup';
  return step === 'capabilities' ? service === 'security' ? 'setup' : service === 'devops' ? 'runs' : step : step;
};

export function ServiceJourney({ service, children, compact: compactOverride }: { service: L5Service; children: ReactNode; compact?: boolean }) {
  const inheritedCompact = useContext(CompactContext);
  const compact = compactOverride ?? inheritedCompact;
  const navigateSection = useContext(JourneyNavigationContext);
  const requested = useRequestedCapability();
  const capability = serviceCapabilities[service].find(item => item.id === requested && !item.unavailable);
  const [active, setActive] = useState<ServiceStep>(() => stepFromHash(service));
  const [validation, setValidation] = useState<{ field: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement; reveal: boolean } | null>(null);
  const invalidField = validation?.field;
  useEffect(() => {
    const navigate = () => setActive(stepFromHash(service));
    navigate();
    window.addEventListener('hashchange', navigate);
    return () => window.removeEventListener('hashchange', navigate);
  }, [service]);
  useEffect(() => {
    if (!validation?.reveal || !invalidField?.isConnected) return;
    // Native validation applies even when a form-associated input is in a hidden
    // section. Reveal it before focusing; keep the browser's constraint checks.
    let ancestor = invalidField.parentElement;
    while (ancestor) {
      if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
      ancestor = ancestor.parentElement;
    }
    const section = invalidField.closest('.l5-service-section');
    const step = steps.find(item => section?.id === `l5-${service}-${item.id}`);
    if (step && active !== step.id) { setActive(step.id); return; }
    invalidField.focus();
    setValidation(current => current === validation ? { ...current, reveal: false } : current);
  }, [validation, invalidField, active, service]);
  return <SectionContext.Provider value={active}><div className="l5-service-journey"
    onClickCapture={event => {
      if (!compact || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const target = event.target instanceof Element ? event.target.closest('a') : null;
      const hash = target?.getAttribute('href');
      const step = steps.find(item => hash === `#l5-${service}-${item.id}`);
      if (!step || !hash) return;
      event.preventDefault(); setActive(service === 'security' && step.id === 'capabilities' ? 'setup' : step.id);
      // Keep the navigation visible; a native fragment jump would put these
      // controls underneath ADP's fixed global header.
      if (navigateSection) navigateSection(hash);
      else window.history.replaceState(window.history.state, '', hash);
    }}
    onInputCapture={() => setValidation(null)}
    onInvalidCapture={event => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement)) return;
      event.preventDefault();
      setValidation(current => current?.reveal && current.field.isConnected ? current : { field: target, reveal: true });
    }}>
    {capability && <p className="product-muted">Selected activity: {capability.title}. Review its saved setup and run requirements below.</p>}
    <nav aria-label={`${names[service]} service sections`} className="l5-journey-nav">
      {steps.filter(step => step.id !== 'capabilities' || !compact && service !== 'security').map(step => <a key={step.id} href={`#l5-${service}-${step.id}`} aria-current={(active === step.id || compact && active === 'capabilities' && step.id === 'setup') ? 'location' : undefined}
        onClick={event => {
          if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
          event.preventDefault(); setActive(step.id);
        }}>{step.title}</a>)}
    </nav>
    {invalidField?.isConnected && <p role="alert">Check the highlighted field before saving. {invalidField.validationMessage}</p>}
    {children}
  </div></SectionContext.Provider>;
}

export function ServiceSection({ service, step, description, children }: { service: L5Service; step: ServiceStep; description?: string; children: ReactNode }) {
  const active = useContext(SectionContext);
  const compact = useContext(CompactContext);
  const definition = steps.find(item => item.id === step)!;
  const id = `l5-${service}-${step}`;
  const visible = active === null || active === step || compact && ['setup', 'capabilities'].includes(active) && ['setup', 'capabilities'].includes(step);
  return <section id={id} aria-labelledby={`${id}-title`} className="l5-service-section" hidden={!visible}>
    <header><h2 id={`${id}-title`}>{definition.title}</h2><p className="product-muted">{description ?? definition.description}</p></header>
    {children}
  </section>;
}

type Reference = { record_id?: string; record_revision?: string; record_type?: string };
interface SetupConfiguration {
  revision?: number;
  gaps?: string[];
  inherited_fields?: string[];
  settings: {
    purpose?: unknown; classification?: unknown; profile_ref?: unknown; source_refs?: unknown;
    region?: unknown; connection_ref?: unknown; overrides?: unknown;
    [key: string]: unknown;
  } | object;
}
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const textValue = (value: unknown) => typeof value === 'string' && value.trim() ? value : 'Not provided';
const referenceLabel = (value: unknown) => {
  const ref = object(value) as Reference;
  return ref.record_id ? `${ref.record_id}${ref.record_revision ? ` · revision ${ref.record_revision}` : ''}` : 'Not selected';
};

/** Render only the saved, scope-checked service configuration. Never fetch a broader profile here. */
export function ProfileSetupSummary({ configuration, productId, projectId }: { configuration?: SetupConfiguration | null; productId: string; projectId: string }) {
  if (!configuration) return <p>Save a service setup to see the Product information and source revisions it uses.</p>;
  const settings = object(configuration.settings);
  const profile = object(settings.profile_ref);
  const inherited = configuration.inherited_fields ?? [];
  const overrides = Array.isArray(settings.overrides) ? settings.overrides.map(object) : [];
  const sources = Array.isArray(settings.source_refs) ? settings.source_refs : [];
  const detail = object(settings.devops ?? settings.transform);
  const rows: { field: string; label: string; value: ReactNode }[] = [
    { field: 'purpose', label: 'Purpose', value: textValue(settings.purpose) },
    { field: 'classification', label: 'Data classification', value: textValue(settings.classification) },
    { field: 'source_refs', label: 'Sources', value: sources.length ? <ul>{sources.map((ref, index) => <li key={index}>{referenceLabel(ref)}</li>)}</ul> : 'No sources selected' },
    { field: 'connection_ref', label: 'Connection', value: referenceLabel(settings.connection_ref ?? detail.hosting_connection_ref ?? detail.connection_ref) },
    { field: 'region', label: 'Region', value: textValue(settings.region ?? detail.region) },
  ];
  const limits = object(settings.limits);
  if (typeof limits.max_cost === 'string' && limits.max_cost) rows.push({ field: 'limits', label: 'Run limit', value: `${limits.max_cost} ${typeof limits.currency === 'string' ? limits.currency : 'USD'} per run` });
  if (detail.space_mode === 'adopt') rows.push({ field: 'devops.space_mode', label: 'DevOps space', value: textValue(detail.existing_space_id) });
  if (detail.space_mode === 'create') rows.push({ field: 'devops.space_mode', label: 'DevOps space', value: 'Create a new space (saved choice)' });
  const origin = (field: string) => overrides.some(item => item.field === field) ? 'override' : inherited.includes(field) ? 'profile' : 'service';
  const hasValue = (row: typeof rows[number]) => !['Not provided', 'Not selected', 'No sources selected'].includes(String(row.value));
  const groups = [
    { id: 'profile', title: 'From Product profile', empty: 'No saved values were inherited from the Product profile.' },
    { id: 'service', title: 'Added for this service', empty: 'No additional service settings saved yet.' },
  ];
  const gaps = [...new Set(configuration.gaps ?? [])];
  const actions: Record<string, string> = {
    'investigation.needs_space': 'Create or connect a DevOps space in Provider connection.',
    classification: 'Set the data classification in the Product profile, then refresh this setup.',
    limits: 'Add run limits and an existing budget policy in the setup editor.',
    execution_delegation: 'Review and grant execution access in Owner consent.',
    connection_ref: 'Choose an AWS connection in the setup editor.',
    region: 'Choose an AWS region in the setup editor.',
    source_refs: 'Select the sources this service should use.',
    executor_binding_ref_missing: 'Add the qualified executor reference in advanced capability settings.',
    test_policy_ref_missing: 'Add the test policy reference in advanced capability settings.',
  };
  const displayValue = (row: typeof rows[number]) => row.field === 'source_refs'
    ? <ul>{sources.slice(0, 3).map((ref, index) => <li key={index}>{textValue(object(ref).record_id)}</li>)}{sources.length > 3 && <li>and {sources.length - 3} more — see saved references</li>}</ul> : row.field === 'connection_ref'
      ? String(row.value).split(' · revision ')[0] : row.value;
  return <div className="l5-profile-summary">
    <p className="product-muted">These are saved values. New Product profile changes need review before they are applied.</p>
    <div className="l5-setup-overview">{groups.map(group => <section key={group.id} aria-label={group.title}>
      <h3>{group.title}</h3>
      <dl>{rows.filter(row => hasValue(row) && (group.id === 'profile' ? origin(row.field) === 'profile' : origin(row.field) !== 'profile')).map(row =>
        <div key={row.field}><dt>{row.label}{origin(row.field) === 'override' && <small>Service override</small>}</dt><dd>{displayValue(row)}</dd></div>)}</dl>
      {!rows.some(row => hasValue(row) && (group.id === 'profile' ? origin(row.field) === 'profile' : origin(row.field) !== 'profile')) && <p className="product-muted">{group.empty}</p>}
    </section>)}</div>
    {gaps.length > 0 && <aside className="l5-setup-next" aria-label="Setup needs attention">
      <h3>To finish setup</h3>
      <ul>{gaps.map(gap => <li key={gap}>{actions[gap] ?? `Review ${gap.replace(/[._]/g, ' ')} in the service setup and readiness checks.`}</li>)}</ul>
    </aside>}
    <details className="l5-disclosure"><summary>Saved references and profile details</summary>
      <p>Configuration revision {configuration.revision ?? 'not reported'} · Product profile revision {typeof profile.record_revision === 'string' ? profile.record_revision : 'not reported'}.</p>
      <a href={`/products/${encodeURIComponent(productId)}/projects/${encodeURIComponent(projectId)}`}>View Product profile</a>
      {overrides.length > 0 && <ul>{overrides.map((override, index) => <li key={index}>{String(override.field).replace(/_/g, ' ')} override: {String(override.reason ?? 'No reason recorded')}</li>)}</ul>}
      <div className="l5-table-scroll"><table><caption className="sr-only">Values used by the saved service setup and where they came from</caption>
        <thead><tr><th scope="col">Setting</th><th scope="col">Saved value</th><th scope="col">Source</th></tr></thead>
        <tbody>{rows.filter(row => ['source_refs', 'connection_ref'].includes(row.field)).map(row => {
          const override = overrides.find(item => item.field === row.field);
          return <tr key={row.field}><th scope="row">{row.label}</th><td>{row.value}</td><td>{!hasValue(row) ? 'Not set' : override
            ? <>Service override{typeof override.reason === 'string' && override.reason ? `: ${override.reason}` : ''}</>
            : inherited.includes(row.field) ? 'Product profile' : 'Service setup'}</td></tr>;
        })}</tbody>
      </table></div>
      <p className="product-muted">This is the saved configuration, including pinned source revisions. Edits to the Product profile are not automatically applied here.</p>
    </details>
  </div>;
}

/** Details stay mounted so folding a panel cannot reset edits, receipts or pending work. */
export function ServiceDisclosure({ title, hint, open, children }: { title: string; hint?: string; open?: boolean; children: ReactNode }) {
  return <details className="l5-disclosure" open={open}><summary>{title}{hint && <span>{hint}</span>}</summary><div className="l5-disclosure-content">{children}</div></details>;
}

interface Capability { id: string; title: string; description: string; requirements: string; unavailable?: boolean }
// This catalog describes implemented contracts, not deployment readiness. Backend options
// decide Product eligibility; existing admission and binding checks still decide execution.
export const serviceCapabilities: Record<L5Service, Capability[]> = {
  security: [
    { id: 'threat_model', title: 'Threat modeling', description: 'Identify threats, assumptions and mitigations from architecture documents.', requirements: 'Pinned documents, registered source storage, a selected threat model and current execution authorization.' },
    { id: 'code_review', title: 'Code scanning', description: 'Review a repository revision and return findings with severity and code locations.', requirements: 'A pinned repository commit, registered source storage and current execution authorization.' },
    { id: 'pentesting', title: 'Pen testing', description: 'Active testing of a deployed application.', requirements: 'This capability has no supported execution path in ADP yet.', unavailable: true },
  ],
  devops: [
    { id: 'investigation', title: 'Investigation', description: 'Investigate a selected environment using its permitted resources and operational context.', requirements: 'A verified DevOps space, environment, bounded investigation goal and execution authorization.' },
    { id: 'release_readiness', title: 'Release readiness', description: 'Assess the operational readiness of a proposed release.', requirements: 'Release evaluation is implemented, but ADP cannot activate a release-readiness configuration yet.', unavailable: true },
    { id: 'release_verification', title: 'Release verification', description: 'Verify a release against a qualified test profile.', requirements: 'The native verification executor and test-profile reader are not installed in the runtime yet.', unavailable: true },
    { id: 'prevention', title: 'Prevention', description: 'Manage a native prevention goal for a verified DevOps space.', requirements: 'Native goal controls exist, but ADP cannot activate a new prevention configuration yet.', unavailable: true },
  ],
  finops: [
    { id: 'cost_inquiry', title: 'Cost inquiry', description: 'Ask a billing question and compare the answer with retained billing evidence.', requirements: 'Explicit account and billing scope, a qualified provider, a ready binding and execution authorization.' },
  ],
  transform: [
    { id: 'refactor', title: 'Refactor', description: 'Propose code changes for a specified refactoring goal and verify the result.', requirements: 'A pinned repository commit, permitted paths, an existing linked binding, a qualified executor and a test policy.' },
    { id: 'dependency_upgrade', title: 'Dependency upgrade', description: 'Update selected dependencies and report changes and verification results.', requirements: 'A pinned repository commit, a named dependency or runtime and target version, permitted paths, an existing linked binding, a qualified executor and a test policy.' },
  ],
  well_architected: [
    { id: 'architecture_review', title: 'Architecture review', description: 'Review selected documents and code against the chosen architecture pillars.', requirements: 'Pinned sources, selected pillars, source storage, an execution role and a qualified review provider.' },
    { id: 'resource_inspection', title: 'AWS resource inspection', description: 'Inspect deployed AWS resources as review inputs.', requirements: 'ADP currently supports source-only reviews.', unavailable: true },
  ],
};

export function CapabilitySummary({ service, capabilities, selected = [], unavailable = [] }: { service?: L5Service; capabilities: readonly string[]; selected?: readonly string[]; unavailable?: readonly string[] }) {
  const entries = service ? serviceCapabilities[service] : capabilities.map(id => ({ id, title: id.replace(/_/g, ' '), description: '', requirements: '' }));
  return <div className="l5-capability-grid">{entries.map((capability: Capability) => <article key={capability.id} className="l5-capability" aria-label={capability.title}>
    <h3>{capability.title}</h3>
    <p>{capability.description}</p>
    <p className="l5-capability-state">{capability.unavailable || unavailable.includes(capability.id) ? 'Not available yet'
      : !capabilities.includes(capability.id) ? 'Not enabled for this Product'
        : selected.includes(capability.id) ? 'Selected · run readiness checked below' : 'Setup required'}</p>
    <details><summary>Requirements</summary><p className="product-muted">{capability.requirements}</p></details>
  </article>)}</div>;
}

export function ScheduleAvailability({ service }: { service: L5Service }) {
  return <aside className="l5-schedule-availability" aria-label="Scheduling availability">
    <h3>Scheduling</h3>
    {service === 'finops' ? <p>Scheduled inquiries: Not available yet. FinOps currently supports manual inquiries only.</p>
      : service === 'security' ? <p>Daily UTC schedules use the selected source revisions. Each occurrence needs a current ready setup and execution authorization.</p>
      : service === 'transform' ? <p>Scheduled transformations: Not available yet. Use manual runs after completing setup.</p>
      : service === 'devops' ? <p>Scheduled investigations: Not available yet. Investigations currently use manual triggers. Existing prevention policies retain separate pause controls.</p>
      : <p>Daily UTC review schedules use the saved sources and a ready binding. Each occurrence requires current provider readiness and owner consent.</p>}
  </aside>;
}
