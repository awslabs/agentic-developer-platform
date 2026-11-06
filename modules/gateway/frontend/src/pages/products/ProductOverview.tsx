import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { createScope, getProduct, linkScope, listProductScopes, listProjectBindings, listProjects, productPath, unlinkScope, updateProduct, updateProject, productReceipt, type GovernedProject, type ProductProfile } from '@/services/products';
import { Button, Modal } from '@/components/ui';
import { useAuth } from '@/hooks/useAuth';
import ProductOnboarding from './ProductOnboarding';
import ProductProfileSections from './ProductProfileSections';
import ProductOrganization from './ProductOrganization';
import ProductPeople from './ProductPeople';
import ProductBudget from './ProductBudget';
import ProductBuild from './ProductBuild';
import ProductInsights from './ProductInsights';
import { ProductAgents, ProductLifecycle, agentNames } from './ProductLifecycle';
import { productWorkspaces, workspaceFromPath, workspacePath, workspaceService, type ProductWorkspace } from './productWorkspaceNavigation';
import ProductServices from './services/ProductServices';
import { CompactServiceJourney } from './services/L5ServiceJourney';
import Knowledge from '@/pages/Knowledge';
import type { ProductPeople as People } from '@/services/products';
import { isConnectionService } from '@/services/productConnectionSetup';
import './productWorkspace.css';
import { productDraftKey, readProductDraft, readProjectDraft, writeProjectDraft, readBindingWrite, writeBindingWrite, serverFieldErrors, validateProductFields, writeProductDraft, type FieldErrors, type PendingProductWrite, type PendingProjectWrite, type PendingBindingWrite } from './productDraft';

type Draft = { name: string; purpose: string; users: string };

const fromProfile = (profile: ProductProfile): Draft => ({
  name: profile.product.name,
  purpose: profile.product.purpose,
  users: profile.product.intended_users.join(', '),
});

const isDirty = (draft: Draft, baseline: ProductProfile) =>
  draft.name !== baseline.product.name ||
  draft.purpose !== baseline.product.purpose ||
  draft.users !== baseline.product.intended_users.join(', ');

export default function ProductOverview() {
  const { productId, projectId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const activeService = workspaceService(location.pathname);
  const tab = workspaceFromPath(location.pathname);
  const setTab = (value: ProductWorkspace) => { if (productId && projectId) navigate(workspacePath(productId, projectId, value)); };
  const { user } = useAuth();
  const key = productDraftKey(user?.id, user?.orgId, `${productId}:${projectId}`);
  const bindingKey = productDraftKey(user?.id, user?.orgId, `binding:${productId}`);
  const projectKey = productDraftKey(user?.id, user?.orgId, `project:${productId}:${projectId}`);
  const projectLoadedKey = useRef<string | null>(null);
  const projectNameRef = useRef('');
  const projectBaselineRef = useRef('');
  const projectRevisionRef = useRef(0);
  const currentKey = useRef(key);
  currentKey.current = key;
  const [baseProjectId, setBaseProjectId] = useState<string | null>(null);
  const [scopeNames, setScopeNames] = useState<Record<string, string>>({});
  const [scopes, setScopes] = useState<string[]>([]);
  const [scopeRefresh, setScopeRefresh] = useState(0);
  const [manageOpen, setManageOpen] = useState(false);
  const [creatingProject, setCreatingProject] = useState(false);
  const [scopesStatus, setScopesStatus] = useState<'loading' | 'ready' | 'unavailable'>('loading');
  const [organizationOpen, setOrganizationOpen] = useState(false);
  const [editingDetails, setEditingDetails] = useState(false);
  const [financialsVisited, setFinancialsVisited] = useState(false);
  const [financialsOpen, setFinancialsOpen] = useState(false);
  // Keep saved URLs working while giving the profile one canonical home.
  useEffect(() => {
    if (!productId || !projectId) return;
    const params = new URLSearchParams(location.search);
    const section = params.get('section');
    const legacySettings = location.pathname.endsWith('/settings');
    const legacyProjects = location.pathname.endsWith('/projects');
    const connectionSetup = isConnectionService(params.get('connection_setup'));
    if (section === 'financials' && legacySettings) {
      params.delete('section');
      navigate({ pathname: workspacePath(productId, projectId, 'budget'), search: params.toString(), hash: '#financial-planning' }, { replace: true });
      return;
    }
    if (legacySettings || legacyProjects || connectionSetup && tab !== 'profile') {
      if (legacySettings && (!section || section === 'information') && !connectionSetup) setEditingDetails(true);
      params.delete('section');
      const hash = legacyProjects ? '#product-projects' : connectionSetup || section === 'basic' || section === 'advanced' ? '#product-connections' : '#product-details';
      navigate({ pathname: workspacePath(productId, projectId, 'profile'), search: params.toString(), hash }, { replace: true });
    }
  }, [location.pathname, location.search, navigate, productId, projectId, tab]);
  useEffect(() => {
    if (tab === 'budget' && location.hash === '#financial-planning') {
      setFinancialsOpen(true); setFinancialsVisited(true);
    }
  }, [tab, location.hash]);
  const [people, setPeople] = useState<People | null>(null);
  const [candidates, setCandidates] = useState<GovernedProject[]>([]);
  const [bindingId, setBindingId] = useState<string | null>(null);
  const [candidateCursor, setCandidateCursor] = useState<string | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState('');
  const [newScopeName, setNewScopeName] = useState('');
  const [managementError, setManagementError] = useState('');
  const [bindingWrite, setBindingWrite] = useState<PendingBindingWrite | null>(() => readBindingWrite(bindingKey));
  const [bindingSaving, setBindingSaving] = useState(false);
  const [projectName, setProjectName] = useState('');
  const [projectBaseline, setProjectBaseline] = useState('');
  const [projectPending, setProjectPending] = useState<PendingProjectWrite | null>(null);
  const [projectSaving, setProjectSaving] = useState(false);
  const [projectStatus, setProjectStatus] = useState('');
  const [server, setServer] = useState<ProductProfile | null>(null);
  const [loadedKey, setLoadedKey] = useState<string | null>(null);
  const serverRef = useRef<ProductProfile | null>(null);
  const [baseline, setBaseline] = useState<ProductProfile | null>(null);
  const baselineRef = useRef<ProductProfile | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const draftRef = useRef<Draft | null>(null);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [pending, setPending] = useState<PendingProductWrite | null>(() => readProductDraft(key)?.pending ?? null);

  useEffect(() => {
    if (saving || pending) return;
    const field = (['name', 'purpose', 'users'] as const).find((field) => errors[field]);
    if (field) document.getElementById(`product-edit-${field}`)?.focus();
  }, [errors, pending, saving]);

  const changeDraft = (next: Draft) => {
    draftRef.current = next;
    setDraft(next);
    writeProductDraft(key, baselineRef.current && isDirty(next, baselineRef.current) ? next : null, baselineRef.current ?? undefined, pending ?? undefined);
    setErrors({});
    setStatus('');
  };

  const refresh = useCallback(async () => {
    if (!productId || !projectId) return;
    try {
      const latest = await getProduct(productId, projectId);
      if (currentKey.current !== key) return;
      if (serverRef.current && latest.revision < serverRef.current.revision) return;
      if (!baselineRef.current) {
        const restored = readProductDraft(key);
        if (restored?.baseline?.product_id === latest.product_id && restored.baseline.project_id === latest.project_id && isDirty(restored.fields, restored.baseline)) {
          baselineRef.current = restored.baseline;
          setBaseline(restored.baseline);
          draftRef.current = restored.fields;
          setDraft(restored.fields);
          setPending(restored.pending ?? null);
        }
      }
      const clean = !baselineRef.current || !draftRef.current || !isDirty(draftRef.current, baselineRef.current);
      if (latest.project && projectLoadedKey.current !== projectKey) {
        const restored = readProjectDraft(projectKey);
        projectNameRef.current = restored?.name ?? latest.project.name;
        projectBaselineRef.current = restored?.baseline ?? latest.project.name;
        projectRevisionRef.current = restored?.revision ?? latest.revision;
        setProjectName(projectNameRef.current);
        setProjectBaseline(projectBaselineRef.current);
        setProjectPending(restored?.pending ?? null);
        projectLoadedKey.current = projectKey;
      } else if (latest.project && projectNameRef.current === projectBaselineRef.current) {
        projectNameRef.current = latest.project.name;
        projectBaselineRef.current = latest.project.name;
        projectRevisionRef.current = latest.revision;
        setProjectName(latest.project.name);
        setProjectBaseline(latest.project.name);
      }
      serverRef.current = latest;
      setServer(latest);
      setLoadedKey(key);
      if (clean) {
        baselineRef.current = latest;
        setBaseline(latest);
        draftRef.current = fromProfile(latest);
        setDraft(draftRef.current);
      }
      setError('');
    } catch {
      if (currentKey.current !== key) return;
      setError(serverRef.current ? 'Could not refresh the latest saved information.' : 'Product unavailable or you do not have access.');
    }
  }, [productId, projectId, key, projectKey]);

  useEffect(() => {
    setScopes([]);
    setScopeNames({});
    setBaseProjectId(null);
    setScopesStatus('loading');
    if (!productId) return;
    let active = true;
    const load = async () => {
      try {
        const visible = await listProductScopes(productId, names => { if (active) setScopeNames(names); }, id => { if (active) setBaseProjectId(id); });
        if (active) { setScopes(visible); setScopesStatus('ready'); }
      } catch {
        if (active) setScopesStatus('unavailable');
      }
    };
    void load();
    return () => { active = false; };
  }, [productId, projectId, scopeRefresh]);

  useEffect(() => { setBindingWrite(readBindingWrite(bindingKey)); }, [bindingKey]);

  const isOwner = people?.is_owner ?? false;

  const loadCandidates = useCallback(async (cursor?: string) => {
    try {
      const page = await listProjects(cursor);
      setCandidates((previous) => cursor ? [...previous, ...page.items] : page.items);
      setCandidateCursor(page.next_cursor);
      setManagementError('');
    } catch {
      setManagementError('Could not load eligible Projects. Try again.');
    }
  }, []);

  useEffect(() => {
    if (manageOpen && isOwner) void loadCandidates();
  }, [manageOpen, isOwner, loadCandidates, scopeRefresh]);

  useEffect(() => {
    setBindingId(null);
    if (!manageOpen || !isOwner || !productId || !projectId) return;
    let active = true;
    void listProjectBindings(productId, projectId).then((history) => {
      const versions = history.items.flatMap((item) => item.relationship_versions).filter((version) => 'to_ref' in version
        ? version.to_ref.record_type === 'project' && version.to_ref.record_id === projectId && version.to_ref.project_id === projectId
        : version.project_id === projectId);
      const current = versions[versions.length - 1];
      if (active && current && !current.valid_to) setBindingId('relationship_id' in current ? current.relationship_id : current.binding_id);
    }).catch(() => { if (active) setBindingId(null); });
    return () => { active = false; };
  }, [manageOpen, isOwner, productId, projectId, scopeRefresh]);

  useEffect(() => {
    serverRef.current = null;
    baselineRef.current = null;
    draftRef.current = null;
    setServer(null);
    setPeople(null);
    projectLoadedKey.current = null;
    projectNameRef.current = '';
    projectBaselineRef.current = '';
    projectRevisionRef.current = 0;
    setProjectName('');
    setProjectBaseline('');
    setProjectPending(null);
    setProjectStatus('');
    setBaseline(null);
    setDraft(null);
    setLoadedKey(null);
    setPending(readProductDraft(key)?.pending ?? null);
    void refresh();
    const interval = window.setInterval(() => { void refresh(); }, 15_000);
    window.addEventListener('focus', refresh);
    return () => { window.clearInterval(interval); window.removeEventListener('focus', refresh); };
  }, [refresh, key]);

  useEffect(() => {
    if (loadedKey !== key || !location.hash) return;
    document.getElementById(location.hash.slice(1))?.scrollIntoView?.({ block: 'start' });
  }, [key, loadedKey, location.hash, tab]);

  async function saveProject(write: PendingProjectWrite) {
    if (!productId || !projectId || projectSaving) return;
    setProjectSaving(true);
    setProjectStatus('');
    try {
      const saved = await updateProject(productId, projectId, write.revision, write.name.trim(), write.receipt);
      const latest = await getProduct(productId, projectId);
      if (latest.revision < saved.revision || currentKey.current !== key) throw new Error('Project readback unavailable.');
      serverRef.current = latest;
      setServer(latest);
      if (latest.project?.name === write.name.trim()) {
        projectNameRef.current = latest.project.name;
        projectBaselineRef.current = latest.project.name;
        projectRevisionRef.current = latest.revision;
        setProjectName(latest.project.name);
        setProjectBaseline(latest.project.name);
        setProjectPending(null);
        writeProjectDraft(projectKey, null);
        setProjectStatus('Project saved.');
      } else {
        setProjectStatus('Another edit followed yours. Review the latest Project.');
      }
    } catch (failure) {
      if ((failure as { status?: number }).status === 412) {
        setProjectPending(null);
        writeProjectDraft(projectKey, { name: projectNameRef.current, baseline: projectBaselineRef.current, revision: projectRevisionRef.current });
        setProjectStatus('Project changed elsewhere. Review the latest revision before saving.');
        await refresh();
      } else {
        setProjectStatus("Couldn't confirm the Project save. Your draft and receipt are retained; check status before retrying.");
      }
    } finally {
      setProjectSaving(false);
    }
  }

  async function applyBinding(write: NonNullable<typeof bindingWrite>) {
    if (!productId || bindingSaving) return;
    setBindingSaving(true);
    setManagementError('');
    try {
      const changed = write.kind === 'create'
        ? await createScope(productId, write.revision, write.value, write.receipt)
        : write.kind === 'link'
          ? await linkScope(productId, write.revision, write.value, write.receipt)
          : await unlinkScope(productId, write.revision, write.value, write.receipt);
      const latest = await getProduct(productId, changed.project_id);
      if (latest.revision < changed.revision || currentKey.current !== key) throw new Error('Binding readback unavailable.');
      setBindingWrite(null);
      writeBindingWrite(bindingKey, null);
      setCreatingProject(false);
      setNewScopeName('');
      setSelectedCandidate('');
      setScopeRefresh((revision) => revision + 1);
      navigate(`${workspacePath(productId, latest.project_id, 'profile')}#product-projects`);
    } catch (failure) {
      if ((failure as { status?: number }).status === 412) {
        setBindingWrite(null);
        writeBindingWrite(bindingKey, null);
        setManagementError('Product changed elsewhere. Review its latest revision before trying again.');
        await refresh();
      } else if ((failure as { status?: number }).status === 409 || (failure as { status?: number }).status === 404) {
        setBindingWrite(null);
        writeBindingWrite(bindingKey, null);
        setManagementError('This Project cannot be changed here. Refresh your authorized Projects or contact an administrator.');
        await loadCandidates();
      } else {
        setManagementError("Couldn't confirm the binding change. Check status using the same receipt before retrying.");
      }
    } finally {
      setBindingSaving(false);
    }
  }

  const projectConflict = !!(server?.project && projectName !== projectBaseline && server.revision !== projectRevisionRef.current);
  const conflict = !!(loadedKey === key && server && baseline && draft && isDirty(draft, baseline) && server.revision !== baseline.revision);

  async function reconcile(write: PendingProductWrite) {
    if (saving || !productId || !projectId || !draft) return;
    setSaving(true);
    setStatus('');
    try {
      const saved = await updateProduct(productId, projectId, write.revision!, {
        name: write.fields.name.trim(),
        purpose: write.fields.purpose.trim(),
        intended_users: write.fields.users.split(',').map((user) => user.trim()),
      }, write.receipt);
      const latest = await getProduct(productId, projectId);
      if (latest.revision < saved.revision) throw new Error('Readback is older than the receipt.');
      serverRef.current = latest;
      setServer(latest);
      setLoadedKey(key);
      setPending(null);
      setError('');
      if (latest.revision === saved.revision) {
        baselineRef.current = latest;
        setBaseline(latest);
        changeDraft(fromProfile(latest));
        writeProductDraft(key, null);
        setStatus(`Saved revision ${saved.revision}`);
      } else {
        setStatus('Original edit confirmed, but another client saved newer changes. Review them before continuing.');
        writeProductDraft(key, write.fields, baselineRef.current ?? undefined);
      }
    } catch (failure) {
      if ((failure as { status?: number }).status === 412) {
        setPending(null);
        writeProductDraft(key, draft, baselineRef.current ?? undefined);
        setStatus('Your edits were not saved. Review the latest saved version before retrying.');
        await refresh();
      } else {
        const invalid = serverFieldErrors(failure);
        if (Object.keys(invalid).length) {
          setPending(null);
          writeProductDraft(key, draft, baselineRef.current ?? undefined);
          setErrors(invalid);
        }
        setStatus(Object.keys(invalid).length
          ? 'Check the highlighted fields. Your edits are still here.'
          : "We couldn't confirm the changes. Your details are kept here. When you're online, check save status.");
      }
    } finally {
      setSaving(false);
    }
  }

  function save(event: FormEvent, review = false) {
    event.preventDefault();
    if (loadedKey !== key || !draft || !server || !baseline || !productId || !projectId || saving || pending || (conflict && !review)) return;
    const invalid = validateProductFields(draft);
    if (Object.keys(invalid).length) {
      setErrors(invalid);
      setStatus('Check the highlighted fields. Your edits are still here.');
      return;
    }
    const write = { receipt: productReceipt(), fields: { ...draft }, revision: review ? server.revision : baseline.revision };
    setPending(write);
    writeProductDraft(key, draft, baseline, write);
    void reconcile(write);
  }

  return <main className="product-workspace">
    <Link className="product-breadcrumb" to="/products">Products / {server?.product.name ?? 'Workspace'}</Link>
    {error && <p role="alert">{error}</p>}
    {(!server || loadedKey !== key) && !error && <p role="status">Loading product…</p>}
    {server && draft && loadedKey === key && <>
      <header className="product-heading">
        <div className="flex items-center gap-4"><div className="product-symbol" aria-hidden="true">P</div><div>
          <p className="product-eyebrow">Product workspace</p><h1>{server.product.name}</h1>
          <p className="product-muted">{server.project?.name ?? 'Current project'} · {server.product.lifecycle_status ?? 'Saved'}</p>
        </div></div>
      <label className="product-project-switcher">Project
        <select className="mt-1 block w-full rounded border p-2" value={projectId} disabled={projectSaving || saving || !!pending} onChange={(event) => {
          if (productId && scopes.includes(event.target.value)) {
            const suffix = location.pathname.slice(productPath(productId, projectId!).length);
            navigate(`${productPath(productId, event.target.value)}${suffix}${location.hash}`);
          }
        }}>
          {(scopes.length ? scopes : [projectId!]).map((scope, index) => <option key={scope} value={scope}>{scope === projectId ? server.project?.name ?? 'Current project' : scopeNames[scope] ?? `Project ${index + 1}`}{scope === baseProjectId ? ' (Base)' : ''}</option>)}
        </select>
      </label>

      </header>
      <nav aria-label="Product workspace" className="product-tabs">
        {productWorkspaces.map(([value, label]) =>
          <button type="button" key={value} aria-current={tab === value ? 'page' : undefined} onClick={() => setTab(value)}>{label}</button>)}
      </nav>
      {tab === 'knowledge' && productId && <Knowledge productId={productId} />}
      <div hidden={tab !== 'budget'} className="space-y-5">
        {tab === 'budget' && productId && projectId && <ProductBudget key={key} productId={productId} projectId={projectId} />}
        <details id="financial-planning" className="blueprint-card product-card product-financial-planning" open={financialsOpen} onToggle={event => {
          setFinancialsOpen(event.currentTarget.open);
          if (event.currentTarget.open) setFinancialsVisited(true);
        }}><summary>Financial planning</summary>
          <p className="product-muted mt-3">Plan product costs and target dates. This plan is separate from the project's AI usage limit above.</p>
          {financialsVisited && productId && projectId && user?.id && user?.orgId && <ProductOnboarding key={key} productId={productId} projectId={projectId} revision={server.revision} userId={user.id} orgId={user.orgId} onSaved={refresh} />}
        </details>
      </div>
      {tab === 'build' && productId && projectId && <ProductBuild key={key} productId={productId} projectId={projectId} />}
      {tab === 'insights' && productId && projectId && <ProductInsights key={key} productId={productId} projectId={projectId} />}
      {!activeService && (tab === 'release' || tab === 'operate') && productId && projectId && <ProductLifecycle workspace={tab} productId={productId} projectId={projectId} />}
      {tab === 'agents' && !activeService && productId && projectId && <ProductAgents productId={productId} projectId={projectId} />}
      {activeService && productId && projectId && <section className="blueprint-card product-card product-agent-detail">
        <Link className="product-action-link" to={workspacePath(productId, projectId, tab)}>← Back to {tab === 'release' ? 'Release' : tab === 'operate' ? 'Operate' : 'Agents'}</Link>
        <h2>{agentNames[activeService]}</h2>
        <CompactServiceJourney capability={new URLSearchParams(location.search).get('capability')} onNavigate={hash => navigate({ pathname: location.pathname, search: location.search, hash })}><ProductServices key={`${server.tenant_id}:${productId}:${projectId}:${activeService}`} productId={productId} projectId={projectId} tenantId={server.tenant_id} activeService={activeService} compact /></CompactServiceJourney>
      </section>}
      <div hidden={tab !== 'profile'} className="product-profile-cards space-y-5">
        {productId && projectId && <ProductProfileSections key={productId} productId={productId} projectId={projectId} revision={server.revision} projects={scopeNames} productProfile={server} onSaved={refresh}>
        <section id="product-details" className="blueprint-card product-card space-y-5" aria-label="Product details and team">
          <div className="product-section-heading"><h2>Product details &amp; team</h2>
            <Button variant="secondary" aria-expanded={editingDetails} aria-controls="product-details-editor" onClick={() => setEditingDetails(value => !value)}>{editingDetails ? 'Close details editor' : 'Edit information'}</Button>
          </div>
          <div hidden={editingDetails}><p className="break-words">{server.product.purpose || 'Add a purpose for this product.'}</p>
            <p className="product-muted mt-2">Built for {server.product.intended_users.join(', ') || 'your users'}.</p>
            {baseline && isDirty(draft, baseline) && <p className="product-muted mt-2">You have unsaved product details. Open Edit information to continue.</p>}
          </div>

          <div id="product-details-editor" hidden={!editingDetails}>
            <form noValidate className="space-y-4" onSubmit={(event) => void save(event)}>
              <label className="block">Name<input id="product-edit-name" className="mt-1 block w-full rounded border p-2" value={draft.name} disabled={saving || !!pending} aria-invalid={!!errors.name} aria-describedby={errors.name ? "product-edit-name-error" : undefined} maxLength={200} onChange={(event) => changeDraft({ ...draft, name: event.target.value })} /></label>
              {errors.name && <p id="product-edit-name-error">{errors.name}</p>}
              <label className="block">Purpose<textarea id="product-edit-purpose" className="mt-1 block w-full rounded border p-2" value={draft.purpose} disabled={saving || !!pending} aria-invalid={!!errors.purpose} aria-describedby={errors.purpose ? "product-edit-purpose-error" : undefined} maxLength={10000} onChange={(event) => changeDraft({ ...draft, purpose: event.target.value })} /></label>
              {errors.purpose && <p id="product-edit-purpose-error">{errors.purpose}</p>}
              <label className="block">Intended users<input id="product-edit-users" className="mt-1 block w-full rounded border p-2" value={draft.users} disabled={saving || !!pending} aria-invalid={!!errors.users} aria-describedby={errors.users ? "product-edit-users-error" : undefined} onChange={(event) => changeDraft({ ...draft, users: event.target.value })} /></label>
              {errors.users && <p id="product-edit-users-error">{errors.users}</p>}
              {conflict && <div role="alert" className="space-y-2 rounded border p-3">
                <h3 className="font-semibold">Review conflicting changes</h3>
                <p>Another client saved revision {server.revision}. Your unsaved edits are still in the fields above.</p>
                <p>Latest saved: {server.product.name}; {server.product.purpose}; {server.product.intended_users.join(', ')}</p>
                <button type="button" className="mr-4 underline" disabled={saving || !!pending} onClick={() => {
                  baselineRef.current = server;
                  setBaseline(server);
                  changeDraft(fromProfile(server));
                }}>Use latest saved values</button>
                <button type="button" className="underline" disabled={saving || !!pending} onClick={(event) => void save(event, true)}>Replace saved fields with my edits</button>
              </div>}
              {status && <p role="status">{status}</p>}
              {pending && <button type="button" className="underline" disabled={saving} onClick={() => void reconcile(pending)}>Check save status</button>}
              <Button type="submit" disabled={saving || !!pending || !baseline || !isDirty(draft, baseline) || conflict}>Save changes</Button>
            </form>
          </div>
          {productId && projectId && user?.id && user?.orgId && <ProductPeople embedded key={`${key}:people`} productId={productId} projectId={projectId} orgId={user.orgId} userId={user.id} revision={server.revision} onSaved={refresh} onPeople={setPeople} />}
          <details className="product-profile-more" onToggle={(event) => setOrganizationOpen(event.currentTarget.open)}>
            <summary>Organization and Portfolios</summary>
            {organizationOpen && productId && projectId && <ProductOrganization key={`${productId}:${projectId}`} productId={productId} projectId={projectId} profile={server} />}
          </details>
            <details className="product-profile-more">
              <summary>Product profile sharing</summary>
              <p className="product-muted">Profile sharing is separate from service availability. Open a service to review its saved setup and available capabilities.</p>
              {server.peer_availability ? (['context', 'l5'] as const).map(peer => <div key={peer}>
                <h3 className="font-semibold">{peer === 'context' ? 'Product Context' : 'Service profile sharing'}: {server.peer_availability![peer].status}</h3>
                <p className="product-muted">{server.peer_availability![peer].message}</p>
              </div>) : <p className="product-muted">Profile sharing status has not been reported. Your saved product remains available.</p>}
            </details>
          <details className="product-profile-more"><summary>CLI &amp; saved details</summary><p className="product-muted mt-3">Read this same profile from the CLI.</p><code className="product-command">adp product show --product {productId} --project {projectId} --json</code><p className="product-muted mt-3">Saved revision {server.revision}</p>{bindingId && <p className="product-muted break-all">Binding: {bindingId}</p>}</details>
        </section>
        </ProductProfileSections>}
      <section id="product-projects" className="blueprint-card product-card product-projects space-y-5" aria-label="Product projects">
        <div>
          <div className="product-section-heading"><div><h2>Projects</h2><p className="product-muted">Organize this product’s delivery flows, agents and budgets.</p></div>
            <Button disabled={!isOwner || bindingSaving || !!bindingWrite} onClick={() => setCreatingProject(true)}>New project</Button>
          </div>
          <p className="product-muted">The Base project is the default for work that is not assigned to another project.</p>
          {scopesStatus === 'loading' && <p role="status">Loading projects…</p>}
          {scopesStatus === 'unavailable' && <p role="alert">Could not load the project list. <button type="button" onClick={() => setScopeRefresh(value => value + 1)}>Retry</button></p>}
          {scopesStatus === 'ready' && <ul className="product-project-list">
            {scopes.map((id, index) => <li key={id}>
              <div><strong>{id === projectId ? server.project?.name ?? scopeNames[id] ?? 'Current project' : scopeNames[id] ?? `Project ${index + 1}`}</strong>
                {id === baseProjectId && <span className="product-status-chip">Base</span>}
                {id === projectId && <span className="product-muted">Current project</span>}
              </div>
              <Button variant="secondary" disabled={id === projectId || projectSaving || saving || !!pending || bindingSaving}
                onClick={() => productId && navigate(productPath(productId, id))}>{id === projectId ? 'Current' : 'Open project'}</Button>
            </li>)}
          </ul>}
          {!isOwner && <p className="product-muted">{people ? 'The product owner can create or link projects.' : 'Checking project permissions…'}</p>}
          {bindingSaving && <p role="status">Saving project…</p>}
          {bindingWrite && !bindingSaving && !creatingProject && <button type="button" className="product-text-button" onClick={() => void applyBinding(bindingWrite)}>Check project save status</button>}
          {managementError && !creatingProject && <p role="alert">{managementError}</p>}
        </div>
        {isOwner && <details className="product-profile-more" onToggle={(event) => setManageOpen(event.currentTarget.open)}>
          <summary>Link an existing project</summary>
          <p className="product-muted">Choose a project that is not already assigned to a product.</p>
            <form onSubmit={(event) => {
              event.preventDefault();
              if (!selectedCandidate || bindingWrite) return;
              const write = { kind: 'link' as const, value: selectedCandidate, revision: server.revision, receipt: productReceipt() };
              setBindingWrite(write);
              writeBindingWrite(bindingKey, write);
              void applyBinding(write);
            }}>
              <label className="block">Existing Project<select className="ml-2 rounded border p-2" value={selectedCandidate}
                onChange={(event) => setSelectedCandidate(event.target.value)}>
                <option value="">Select an eligible Project</option>
                {candidates.filter((candidate) => candidate.product_id === null).map((candidate) =>
                  <option key={candidate.id} value={candidate.id}>{candidate.name}</option>)}
              </select></label>
              <button className="underline" disabled={bindingSaving || !!bindingWrite || !selectedCandidate}>Link Project</button>
            </form>
            {candidateCursor && <button type="button" className="underline" onClick={() => void loadCandidates(candidateCursor)}>Load more Projects</button>}

        </details>}
        <details className="product-profile-more"><summary>Current project settings</summary>
      {server.project && projectLoadedKey.current === projectKey && <section aria-label="Project configuration" className="mt-4 space-y-4">
        <form className="space-y-3" onSubmit={(event) => {
          event.preventDefault();
          if (!projectName.trim() || projectName.trim().length > 200 || projectPending || projectSaving || projectConflict || projectName === projectBaseline) return;
          const write = { name: projectName, revision: server.revision, receipt: productReceipt() };
          setProjectPending(write);
          writeProjectDraft(projectKey, { name: projectName, baseline: projectBaseline, revision: projectRevisionRef.current, pending: write });
          void saveProject(write);
        }}>
          <label className="block">Project name<input className="mt-1 block w-full rounded border p-2" value={projectName} maxLength={200}
            disabled={!!projectPending || projectSaving} onChange={(event) => {
              projectNameRef.current = event.target.value;
              setProjectName(event.target.value);
              writeProjectDraft(projectKey, event.target.value !== projectBaseline
                ? { name: event.target.value, baseline: projectBaseline, revision: projectRevisionRef.current } : null);
            }} /></label>
          {projectConflict && <div role="alert">Project changed elsewhere. Your draft is retained.
            <button type="button" className="ml-2 underline" onClick={() => {
              projectBaselineRef.current = server.project!.name;
              projectRevisionRef.current = server.revision;
              setProjectBaseline(server.project!.name);
              writeProjectDraft(projectKey, { name: projectName, baseline: server.project!.name, revision: server.revision });
            }}>Review latest Project before saving my draft</button>
          </div>}
          {projectStatus && <p role="status">{projectStatus}</p>}
          {projectPending && <button type="button" className="underline" disabled={projectSaving} onClick={() => void saveProject(projectPending)}>Check Project save status</button>}
          <Button type="submit" disabled={projectSaving || !!projectPending || projectConflict || projectName === projectBaseline || !projectName.trim()}>Save Project</Button>
        </form>
      </section>}

          {isOwner && scopes.length > 1 && projectId !== baseProjectId && <button type="button" className="underline" disabled={bindingSaving || !!bindingWrite}
              onClick={() => {
                if (!projectId || !window.confirm('Unlink this Project? Saved configuration and assets will be kept.')) return;
                const write = { kind: 'unlink' as const, value: projectId, revision: server.revision, receipt: productReceipt() };
                setBindingWrite(write);
                writeBindingWrite(bindingKey, write);
                void applyBinding(write);
              }}>Unlink this Project</button>}
        </details>
      </section>
      </div>
      <Modal isOpen={creatingProject && tab === 'profile'} onClose={() => { if (!bindingSaving) setCreatingProject(false); }} title="New project" size="sm">
        <div className="product-workspace product-project-create space-y-4">
          <p>Create a project in {server.product.name}. It will have its own delivery flows, agent runs and budget.</p>
            <form onSubmit={(event) => {
              event.preventDefault();
              if (!isOwner || bindingSaving || !productId || !newScopeName.trim() || newScopeName.trim().length > 200 || bindingWrite) return;
              const write = { kind: 'create' as const, value: newScopeName.trim(), revision: server.revision, receipt: productReceipt() };
              setBindingWrite(write);
              writeBindingWrite(bindingKey, write);
              void applyBinding(write);
            }}>
              <label className="block">New project name<input className="mt-1 rounded border p-2" value={newScopeName} maxLength={200} autoFocus disabled={bindingSaving || !!bindingWrite}
                onChange={(event) => setNewScopeName(event.target.value)} /></label>
              <Button type="submit" disabled={bindingSaving || !!bindingWrite || !newScopeName.trim()}>Create project</Button>
            </form>

          {bindingSaving && <p role="status">Creating project…</p>}
          {managementError && <p role="alert">{managementError}</p>}
          {bindingWrite && !bindingSaving && <Button variant="secondary" onClick={() => void applyBinding(bindingWrite)}>Check project save status</Button>}
        </div>
      </Modal>
    </>}
  </main>;
}
