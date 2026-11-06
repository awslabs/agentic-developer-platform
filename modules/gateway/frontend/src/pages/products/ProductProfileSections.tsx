import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Button, Modal } from '@/components/ui';
import { ProviderIcon } from '@/components/ui/ProviderIcon';
import { getSharedProductProfile, productReceipt, updateSharedProductProfile, type ProfileEnvironment, type ProfileResource, type ProfileSection, type SharedProductProfile, type SharedProfilePatch, type ProductProfile } from '@/services/products';
import ProductSources from './ProductSources';
import ProductGitlabHome from './ProductGitlabHome';
import { workspacePath } from './productWorkspaceNavigation';

const sectionNames = { details: 'Product details', repositories: 'Code repositories', environments: 'Environments', backlog: 'Backlog', knowledge: 'Knowledge' };
const environmentClasses = [['development', 'Dev'], ['test', 'Test'], ['staging', 'Staging'], ['production', 'Prod'], ['other', 'Other']] as const;
const providerName = (provider: string) => ({ github: 'GitHub', aws: 'AWS', 'atlassian-mcp': 'Jira', gitlab: 'GitLab' })[provider] ?? provider;

type Props = { productId: string; projectId: string; revision: number; projects: Record<string, string>; productProfile?: Pick<ProductProfile, 'delivery_home_repository' | 'delivery_home_implied'>; onSaved: () => void; children: ReactNode };

export default function ProductProfileSections({ productId, projectId, revision, projects, productProfile, onSaved, children }: Props) {
  const [profile, setProfile] = useState<SharedProductProfile | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [saving, setSaving] = useState(false);
  const [connections, setConnections] = useState(false);
  const [connectionProject, setConnectionProject] = useState('');
  const [connectionSection, setConnectionSection] = useState<'advanced' | undefined>();
  const [environment, setEnvironment] = useState<ProfileEnvironment | null>(null);
  const [repository, setRepository] = useState<ProfileResource | null>(null);
  const [query, setQuery] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [retryWrite, setRetryWrite] = useState(false);
  const pending = useRef<{ revision: number; patch: SharedProfilePatch; receipt: string } | null>(null);
  const request = useRef(0);
  const location = useLocation();

  const refresh = useCallback(async () => {
    const version = ++request.current;
    try {
      const latest = await getSharedProductProfile(productId);
      if (version !== request.current) return;
      setProfile(latest);
      setError('');
    } catch {
      if (version === request.current) setError('Could not load the product Profile. Try again.');
    }
  }, [productId]);
  useEffect(() => {
    const counter = request;
    void refresh(); return () => { counter.current++; };
  }, [refresh, revision]);
  useEffect(() => {
    if (profile?.base_project_id && (location.hash === "#product-connections" || new URLSearchParams(location.search).has("connection_setup"))) {
      setConnectionProject(profile.base_project_id); setConnections(true);
    }
  }, [location.hash, location.search, profile?.base_project_id]);

  async function save(patch: SharedProfilePatch) {
    if (!profile || saving) return;
    const write = pending.current ?? { revision: profile.revision, patch, receipt: productReceipt() };
    pending.current = write;
    setSaving(true); setMessage(''); setRetryWrite(false);
    try {
      await updateSharedProductProfile(productId, write.revision, write.patch, write.receipt);
      pending.current = null;
      setEnvironment(null); setRepository(null);
      setMessage('Product Profile saved.');
      onSaved();
      await refresh();
    } catch (failure) {
      const status = (failure as { status?: number }).status;
      if (status && status >= 400 && status < 500) {
        pending.current = null;
        setMessage(status === 412 ? 'The Profile changed. Your edits are kept; review them and save again.' : 'Could not save these changes. Check the names and selected resources, then try again.');
        await refresh();
      } else {
        setRetryWrite(true);
        setMessage('The save could not be confirmed. Check its status before making another change.');
      }
    } finally { setSaving(false); }
  }

  useEffect(() => {
    if (connections) document.getElementById("product-connection-manager")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [connections]);

  function openConnections(section?: 'advanced') {
    setConnectionProject(profile?.base_project_id ?? projectId);
    setConnectionSection(section);
    setConnections(true);
  }
  const disabled = saving || retryWrite || !profile?.can_edit;
  const applicable = (section: ProfileSection) => !profile?.not_applicable.includes(section);
  const toggleApplicable = (section: ProfileSection) => void save({ not_applicable: applicable(section)
    ? [...(profile?.not_applicable ?? []), section] : (profile?.not_applicable ?? []).filter(item => item !== section) });
  const applicability = (section: ProfileSection) => <button type="button" className="product-text-button" disabled={disabled} onClick={() => toggleApplicable(section)}>
    {applicable(section) ? 'Mark not applicable' : 'Include in Profile'}
  </button>;
  const resourceLine = (resource: ProfileResource, action?: ReactNode) => <li key={resource.id} className="product-profile-resource">
    <ProviderIcon provider={resource.provider === 'atlassian-mcp' ? 'jira' : resource.provider} />
    <div className="min-w-0 flex-1"><strong className="break-words">{resource.label}</strong><p className="product-muted">{providerName(resource.provider)}
      {resource.purpose && ` · ${resource.purpose === 'reference' ? 'Reference' : 'Product code'}`}{resource.default && ' · Default'}
    </p><span className="product-muted">Connection: {resource.bindings.some(binding => binding.status === 'healthy') ? 'healthy' : 'needs verification'}</span></div>{action}
  </li>;
  const resources = (profile?.resources ?? []).map(resource => {
    const home = productProfile?.delivery_home_repository;
    return home && resource.provider === home.provider && resource.kind === 'repository'
      && Number(resource.native_id) === home.repository_id && resource.label === `${resource.namespace}/${resource.native_id}`
      ? { ...resource, label: home.repository } : resource;
  });
  const repositories = resources.filter(r => r.kind === 'repository');
  const filtered = repositories.filter(r => `${r.label} ${r.provider}`.toLowerCase().includes(query.toLowerCase()));
  const targets = resources.filter(r => r.kind === 'environment' || r.kind === 'deployment_application');
  const backlog = resources.filter(r => r.kind === 'tracker' || r.kind === 'tracker_team');
  const documents = resources.filter(r => r.kind === 'document');

  return <>
    <section id="product-connections" className="blueprint-card product-card space-y-3" aria-label="Product Profile completion">
      <div className="product-section-heading"><div><h2>Profile</h2><p className="product-muted">Shared across this product’s projects. Describe what the product uses.</p></div>
        <Button variant="secondary" aria-expanded={connections} aria-controls="product-connection-manager" onClick={() => connections ? setConnections(false) : openConnections()}>Manage connections</Button></div>
      {error && <p role="alert">{error} <button type="button" className="product-text-button" onClick={() => void refresh()}>Retry</button></p>}
      {!profile && !error && <p role="status">Loading Profile…</p>}
      {profile && <><p>{profile.completion.completed} of {profile.completion.applicable} sections complete</p>
        <progress className="product-progress" aria-label="Profile completion" max={100} value={profile.completion.percentage}>{profile.completion.percentage}%</progress>
        <nav aria-label="Profile sections" className="product-profile-links">{profile.completion.sections.map(section => <a key={section.id} href={`#product-${section.id}`}>
          {sectionNames[section.id]}{section.status === 'complete' ? ' ✓' : section.status === 'not_applicable' ? ' · Not applicable' : section.status === 'unavailable' ? ' · Unavailable' : ' · Add details'}
        </a>)}</nav><p className="product-muted">Completion reflects saved information. Connection health is shown separately.</p></>}
      {message && <p role="status">{message}</p>}
      {retryWrite && <Button variant="secondary" disabled={saving} onClick={() => void save(pending.current!.patch)}>Check save status</Button>}
    </section>
    {connections && <section id="product-connection-manager" className="blueprint-card product-card" aria-label="Manage product connections">
      <div className="product-section-heading"><h2>Manage connections</h2><Button variant="secondary" onClick={() => { setConnections(false); void refresh(); onSaved(); }}>Close connections</Button></div>
      <div className="product-workspace space-y-4"><p className="product-muted">Resources linked by any project appear in the product Profile. New product defaults are linked through the Base project.</p>
        <label className="block">Project using these connections<select className="product-profile-input" value={connectionProject} onChange={event => setConnectionProject(event.target.value)}>
          {Object.entries(projects).map(([id, name]) => <option key={id} value={id}>{name}{id === profile?.base_project_id ? ' (Base)' : ''}</option>)}
        </select></label>
        {connections && connectionProject && <ProductSources key={connectionProject} productId={productId} projectId={connectionProject} section={connectionSection} profile={productProfile} onSaved={() => { void refresh(); onSaved(); }}>
          <details className="product-profile-more"><summary>GitLab home</summary><ProductGitlabHome productId={productId} /></details>
        </ProductSources>}
      </div>
    </section>}
    {children}
    <section id="product-repositories" className="blueprint-card product-card space-y-3" aria-label="Code repositories">
      <div className="product-section-heading"><div><h2>Code repositories</h2><p className="product-muted">Identify product code and reference repositories. Choose a default when useful.</p></div>
        <Button variant="secondary" disabled={disabled} onClick={() => openConnections()}>Add repository</Button></div>
      {repositories.length > 8 && <label className="block">Find a repository<input className="product-profile-input" value={query} onChange={event => { setQuery(event.target.value); setShowAll(false); }} placeholder="Search repositories" /></label>}
      <ul>{(showAll ? filtered : filtered.slice(0, 8)).map(resource => resourceLine(resource,
        <Button variant="secondary" disabled={disabled} onClick={() => setRepository({ ...resource })}>Edit</Button>))}</ul>
      {filtered.length > 8 && <button className="product-text-button" onClick={() => setShowAll(value => !value)}>{showAll ? 'Show fewer' : `Show all ${filtered.length} repositories`}</button>}
      {!repositories.length && <p className="product-muted">No accessible repositories linked yet.</p>}{applicability('repositories')}
    </section>
    <section id="product-environments" className="blueprint-card product-card space-y-3" aria-label="Environments">
      <div className="product-section-heading"><div><h2>Environments</h2><p className="product-muted">Name your Dev, Test, Prod or custom environments and map them to connected accounts.</p></div>
        <Button variant="secondary" disabled={disabled} onClick={() => setEnvironment({ id: crypto.randomUUID(), name: '', environment_class: 'development', resource_id: targets[0]?.id ?? '', deployment_target: '' })}>Add environment</Button></div>
      <ul>{profile?.environments.map(env => <li key={env.id} className="product-profile-resource"><ProviderIcon provider={targets.find(target => target.id === env.resource_id)?.provider ?? ''} />
        <div className="min-w-0 flex-1"><strong>{env.name}</strong><p className="product-muted">{environmentClasses.find(([value]) => value === env.environment_class)?.[1]} · {targets.find(target => target.id === env.resource_id)?.label}</p>
          {env.deployment_target && <p className="break-words">{env.deployment_target}</p>}</div><Button variant="secondary" disabled={disabled} onClick={() => setEnvironment({ ...env })}>Edit</Button></li>)}</ul>
      {!profile?.environments.length && <p className="product-muted">No accessible environments configured yet. Multiple environments can share an account.</p>}{applicability('environments')}
    </section>
    <section id="product-backlog" className="blueprint-card product-card space-y-3" aria-label="Backlog">
      <div className="product-section-heading"><div><h2>Backlog</h2><p className="product-muted">The projects or boards where this product’s work is planned.</p></div>
        <Button variant="secondary" disabled={disabled} onClick={() => openConnections()}>Add backlog</Button></div>
      <ul>{backlog.map(resource => resourceLine(resource))}</ul>{!backlog.length && <p className="product-muted">No accessible backlog linked yet.</p>}{applicability('backlog')}
    </section>
    <section id="product-knowledge" className="blueprint-card product-card space-y-3" aria-label="Product Knowledge">
      <div className="product-section-heading"><div><h2>Knowledge</h2><p className="product-muted">Shared documents and reference material for everyone on the product team.</p></div>
        <Link className="product-action-link" to={workspacePath(productId, projectId, 'knowledge')}>Open Knowledge</Link></div>
      <p>{profile?.knowledge_count == null ? 'Open Knowledge to view the shared collection.' : `${profile.knowledge_count} shared knowledge ${profile.knowledge_count === 1 ? 'source' : 'sources'}`}</p>
      <ul>{documents.map(resource => resourceLine(resource))}</ul>{applicability('knowledge')}
    </section>
    <Modal isOpen={!!repository} onClose={() => { if (!saving && !retryWrite) setRepository(null); }} title="Repository" size="sm">
      {repository && <form className="product-workspace space-y-4" onSubmit={event => { event.preventDefault(); void save({ repositories: { [repository.id]: { purpose: repository.purpose ?? 'product_code', label: repository.label } }, ...(repository.default ? { default_repository: repository.id } : profile?.resources.some(r => r.id === repository.id && r.default) ? { default_repository: null } : {}) }); }}>
        <label className="block">Name<input className="product-profile-input" required maxLength={200} value={repository.label} disabled={disabled} onChange={event => setRepository({ ...repository, label: event.target.value })} /></label>
        <label className="block">Purpose<select className="product-profile-input" value={repository.purpose} disabled={disabled} onChange={event => setRepository({ ...repository, purpose: event.target.value as 'product_code' | 'reference', default: event.target.value === 'reference' ? false : repository.default })}><option value="product_code">Product code</option><option value="reference">Reference</option></select></label>
        {repository.purpose !== 'reference' && <label className="flex gap-2"><input type="checkbox" checked={!!repository.default} disabled={disabled} onChange={event => setRepository({ ...repository, default: event.target.checked })} />Default code repository</label>}
        <Button type="submit" disabled={disabled}>Save repository</Button>
        {message && <p role="status">{message}</p>}
        {retryWrite && <Button variant="secondary" disabled={saving} onClick={() => void save(pending.current!.patch)}>Check save status</Button>}
      </form>}
    </Modal>
    <Modal isOpen={!!environment} onClose={() => { if (!saving && !retryWrite) setEnvironment(null); }} title="Environment" size="sm">
      {environment && <form className="product-workspace space-y-4" onSubmit={event => { event.preventDefault(); const { id, ...value } = environment; void save({ environments: { [id]: value } }); }}>
        <label className="block">Name<input className="product-profile-input" value={environment.name} required maxLength={200} disabled={disabled} placeholder="e.g. Dev, Test, Prod or Preview" onChange={event => setEnvironment({ ...environment, name: event.target.value })} /></label>
        <label className="block">Type<select className="product-profile-input" value={environment.environment_class} disabled={disabled} onChange={event => setEnvironment({ ...environment, environment_class: event.target.value as ProfileEnvironment['environment_class'] })}>{environmentClasses.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label className="block">Connected account or deployment application<select className="product-profile-input" value={environment.resource_id} required disabled={disabled} onChange={event => setEnvironment({ ...environment, resource_id: event.target.value })}><option value="">Select an account</option>{targets.map(target => <option key={target.id} value={target.id}>{target.label}</option>)}</select></label>
        <button type="button" className="product-text-button" disabled={disabled} onClick={() => { setEnvironment(null); openConnections('advanced'); }}>Connect an account</button>
        <label className="block">Deployment target (optional)<input className="product-profile-input" value={environment.deployment_target} maxLength={512} disabled={disabled} placeholder="e.g. application, cluster / namespace or stack" onChange={event => setEnvironment({ ...environment, deployment_target: event.target.value })} /></label>
        <div className="flex flex-wrap gap-3"><Button type="submit" disabled={disabled || !environment.resource_id}>Save environment</Button>
          {profile?.environments.some(env => env.id === environment.id) && <Button variant="secondary" disabled={disabled} onClick={() => void save({ environments: { [environment.id]: null } })}>Remove environment</Button>}</div>
        {message && <p role="status">{message}</p>}
        {retryWrite && <Button variant="secondary" disabled={saving} onClick={() => void save(pending.current!.patch)}>Check save status</Button>}
      </form>}
    </Modal>
  </>;
}
