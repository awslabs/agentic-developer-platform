import JiraContextSources, { type JiraConnectionListState } from './JiraContextSources';
import ProductWorkflowPicker from './ProductWorkflowPicker';
import { Button } from '@/components/ui';
import ProductConnectionSetup from './ProductConnectionSetup';
import { isConnectionService } from '@/services/productConnectionSetup';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useSearchParams } from 'react-router-dom';
import { LineIcon } from '@/components/ui/LineIcon';
import { ProviderIcon } from '@/components/ui/ProviderIcon';
import { bindProductSource, confirmRepository, getProduct, inspectProductSource, listProductSources, listSourceBindings, previewRepository, productReceipt, repositoryOptions, startOnboarding, verifySourceBinding, type ProductProfile, type ProductSource, type RepositoryPlan, type SourceBinding, type SourceResource } from '@/services/products';

type Props = { productId: string; projectId: string; onSaved: () => void; section?: 'basic' | 'advanced'; projectName?: string; children?: ReactNode; profile?: Pick<ProductProfile, 'delivery_home_repository' | 'delivery_home_implied'> };

const connectionKey = (source: ProductSource) => `connection:${source.connection_ref.kind}:${source.connection_ref.id}`;
const resourceKey = (resource: SourceResource) => `resource:${resource.kind}:${resource.namespace}:${resource.native_id}`;
const connectorProviders = new Set(['sonarqube', 'jenkins', 'argocd', 'gitlab', 'pagerduty', 'linear', 'slack']);

function selectionFailure(error: unknown): string | undefined {
  if (!error || typeof error !== 'object') return;
  const failure = error as { status?: number; detail?: { code?: string } };
  switch (failure.status) {
    case 403: return 'You do not have permission to add this source. Ask the product owner to check your access.';
    case 404: return 'This connection or source is no longer available to you. Refresh sources or reconnect.';
    case 409: return failure.detail?.code === 'connection_not_verified'
      ? 'Verify this connection before adding its sources.'
      : 'This source changed or conflicts with an existing connection. Refresh sources and review the selection.';
    case 412: return 'The product changed while adding this source. Select it again to use the latest version.';
    case 422: return 'The source details could not be accepted. Check the selection and try again.';
    case 428: return 'Could not confirm the product version. Refresh sources and try again.';
  }
}

function providerLabel(provider: string) {
  switch (provider.toLowerCase()) {
    case 'github': return 'GitHub';
    case 'aws': return 'AWS';
    case 'sonarqube': return 'SonarQube';
    case 'jenkins': return 'Jenkins';
    case 'argocd': return 'Argo CD';
    case 'gitlab': return 'GitLab';
    case 'pagerduty': return 'PagerDuty';
    case 'linear': return 'Linear';
    case 'slack': return 'Slack';
    default: return provider;
  }
}

const sourceLabel = (source: ProductSource) => /^\d+$/.test(source.label) ? `${providerLabel(source.provider)} connection` : source.label;

export default function ProductSources({ productId, projectId, onSaved, section, profile, projectName, children }: Props) {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedService = searchParams.get('connection_setup');
  const scope = `${productId}:${projectId}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const loadVersion = useRef(0);
  const [sources, setSources] = useState<ProductSource[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<ProductSource | null>(null);
  const [pipelineRepository, setPipelineRepository] = useState<SourceResource | null>(null);
  const [resourceQuery, setResourceQuery] = useState('');
  const [resourcePage, setResourcePage] = useState(0);
  const [bindings, setBindings] = useState<SourceBinding[]>([]);
  const [coverage, setCoverage] = useState<'loading' | 'ready' | 'unavailable'>('loading');
  const [message, setMessage] = useState('');
  const [feedbackFor, setFeedbackFor] = useState<string | null>(null);
  const feedbackRef = useRef<HTMLDivElement>(null);
  const [addingConnection, setAddingConnection] = useState(isConnectionService(requestedService));
  const [jiraRevision, setJiraRevision] = useState(0);
  const [jiraList, setJiraList] = useState<JiraConnectionListState>({ status: 'loading', count: 0 });
  const addConnectionButton = useRef<HTMLButtonElement>(null);
  const workflowButton = useRef<HTMLButtonElement>(null);
  const [inspecting, setInspecting] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [environmentName, setEnvironmentName] = useState('');
  const [environmentClass, setEnvironmentClass] = useState<'development' | 'test' | 'staging' | 'production' | 'other'>('development');
  const [repositoryName, setRepositoryName] = useState('');
  const [documentPaths, setDocumentPaths] = useState<Record<string, string>>({});
  const [repositoryVisibility, setRepositoryVisibility] = useState<'private' | 'public' | 'internal'>('private');
  const [repositoryPreview, setRepositoryPreview] = useState<{ id: string; sessionId: string; revision: number; plan: RepositoryPlan; protections: Record<string, unknown>; receipt: string } | null>(null);
  const pending = useRef<{ resource: SourceResource; source: ProductSource; revision: number; receipt: string; environmentName: string; environmentClass: string } | null>(null);
  const verification = useRef<{ bindingId: string; revision: number; receipt: string } | null>(null);

  useEffect(() => {
    if (message) feedbackRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [message, feedbackFor]);

  useEffect(() => {
    if (isConnectionService(requestedService)) setAddingConnection(true);
  }, [requestedService]);

  const load = useCallback(async () => {
    const version = ++loadVersion.current;
    setJiraList({ status: 'loading', count: 0 });
    setJiraRevision(current => current + 1);
    setBusy(false);
    setCoverage('loading');
    setSelected(null);
    setPipelineRepository(null);
    setResourceQuery('');
    setResourcePage(0);
    setSources([]);
    setNextCursor(null);
    setBindings([]);
    setMessage('');
    setFeedbackFor(null);
    setInspecting(null);
    setRepositoryPreview(null);
    setDocumentPaths({});
    try {
      const [page, saved] = await Promise.all([listProductSources(productId, projectId), listSourceBindings(productId, projectId)]);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      setSources(page.items);
      setNextCursor(page.next_cursor);
      setBindings(saved.items);
      setCoverage('ready');
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) setCoverage('unavailable');
    }
  }, [productId, projectId, scope]);

  useEffect(() => {
    pending.current = null;
    verification.current = null;
    setRepositoryPreview(null);
    void load();
  }, [load]);

  async function moreConnections() {
    if (!nextCursor || busy) return;
    const version = loadVersion.current;
    setBusy(true);
    try {
      const page = await listProductSources(productId, projectId, nextCursor);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      setSources((previous) => [...previous, ...page.items]);
      setNextCursor(page.next_cursor);
    } catch {
      if (scopeRef.current === scope) void load();
    } finally {
      if (scopeRef.current === scope) setBusy(false);
    }
  }

  async function inspect(source: ProductSource) {
    if (busy || inspecting) return;
    const version = loadVersion.current;
    setSelected(null);
    setPipelineRepository(null);
    setResourceQuery('');
    setResourcePage(0);
    setFeedbackFor(connectionKey(source));
    setInspecting(connectionKey(source));
    setMessage('Loading sources…');
    setRepositoryPreview(null);
    try {
      const page = await inspectProductSource(productId, projectId, source.connection_ref);
      if (scopeRef.current === scope && loadVersion.current === version) {
        setSelected(page.items[0] ?? null);
        setMessage(page.items[0]?.resources?.length ? '' : 'No sources are available from this connection. Check its access or refresh to try again.');
      }
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) setMessage('Source access changed or discovery is unavailable. Refresh choices or reconnect.');
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setInspecting(null);
    }
  }

  async function moreResources() {
    if (!selected?.resource_cursor || busy) return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(connectionKey(selected));
    setMessage('Loading more sources…');
    try {
      const page = await inspectProductSource(productId, projectId, selected.connection_ref, selected.resource_cursor);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      const next = page.items[0];
      if (!next || next.connection_ref.id !== selected.connection_ref.id || next.coverage === 'denied' || next.coverage === 'unavailable') {
        setSelected(null);
        setMessage('Source access changed. Refresh choices or reconnect.');
        return;
      }
      setSelected((current) => current?.connection_ref.id === next.connection_ref.id
        ? { ...next, resources: [...(current.resources ?? []), ...(next.resources ?? [])] } : current);
      setMessage('');
    } catch {
      if (scopeRef.current === scope) {
        setSelected(null);
        setMessage('Source access changed. Refresh choices or reconnect.');
      }
    } finally {
      if (scopeRef.current === scope) setBusy(false);
    }
  }

  function inspectPipelines(repository: SourceResource) {
    if (!selected || busy || pending.current) return;
    setMessage('');
    setFeedbackFor(null);
    setPipelineRepository(repository);
  }

  async function choose(source: ProductSource, resource: SourceResource) {
    if (busy || pending.current || !source.resources?.some((item) => item.kind === resource.kind && item.native_id === resource.native_id
      && item.namespace === resource.namespace && item.source_revision === resource.source_revision)) return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(resourceKey(resource));
    setMessage(`Linking ${resource.kind === 'pipeline' ? 'workflow' : resource.kind}…`);
    try {
      const current = await getProduct(productId, projectId);
      if (scopeRef.current !== scope || loadVersion.current !== version || current.project_id !== projectId) return;
      const write = { resource, source, revision: current.revision, receipt: productReceipt(), environmentName: environmentName.trim(), environmentClass };
      pending.current = write;
      await submit(write);
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) setMessage('Could not confirm the latest Project revision. No source was selected.');
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  async function connectDocument(source: ProductSource, repository: SourceResource) {
    const path = documentPaths[repository.native_id]?.trim();
    if (!path || busy || pending.current || source.connection_ref.kind !== 'github_installation') return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(resourceKey(repository));
    setMessage('Connecting document…');
    try {
      const current = await getProduct(productId, projectId);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      const resource: SourceResource = { kind: 'document', name: path, namespace: repository.namespace,
        native_id: `${repository.native_id}:${path}`, repository_id: repository.native_id, source_revision: '' };
      const write = { resource, source, revision: current.revision, receipt: productReceipt(), environmentName: '', environmentClass };
      pending.current = write;
      await submit(write);
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) setMessage('Could not connect the document. Check the saved sources before retrying.');
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  async function submit(write: NonNullable<typeof pending.current>) {
    const version = loadVersion.current;
    let bindingSaved = false;
    setBusy(true);
    try {
      const { source, resource } = write;
      const saved = await bindProductSource(productId, projectId, write.revision, {
        kind: resource.kind, provider: source.provider, namespace: resource.namespace, native_id: resource.native_id,
        connection_ref: source.connection_ref, display_name: (resource.full_name || resource.name).slice(0, 200),
        ...(resource.kind === 'pipeline' ? { repository_id: resource.repository_id, source_revision: resource.source_revision } : {}),
        ...(resource.kind === 'environment' ? { environment: {
          name: write.environmentName, environment_class: write.environmentClass, production: write.environmentClass === 'production',
        } } : {}),
      }, write.receipt);
      bindingSaved = true;
      const readback = await listSourceBindings(productId, projectId);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      setBindings(readback.items);
      const linked = readback.items.find(binding => binding.id === saved.id && binding.project_id === projectId);
      if (linked) {
        pending.current = null;
        setMessage(connectorProviders.has(source.provider) && linked?.status !== 'healthy'
          ? `Linked ${resource.kind} to this project. Verify its native scope before using it in Profile.`
          : `Linked ${resource.kind === 'pipeline' ? 'workflow' : resource.kind} to this project.`);
        onSaved();
      } else {
        setMessage('Source write outcome is unknown. Check the saved Project before retrying the same selection.');
      }
    } catch (error) {
      if (scopeRef.current === scope && loadVersion.current === version) {
        const rejected = !bindingSaved && selectionFailure(error);
        if (rejected) pending.current = null;
        setMessage(rejected || 'Source write outcome is unknown. Check saved bindings before retrying with the same request.');
      }
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  async function verify(binding: SourceBinding) {
    if (busy || !binding.selection || !connectorProviders.has(binding.selection.provider)) return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(`binding:${binding.id}`);
    setMessage('Verifying the selected native scope…');
    try {
      const request = verification.current?.bindingId === binding.id
        ? verification.current
        : { bindingId: binding.id, revision: (await getProduct(productId, projectId)).revision, receipt: productReceipt() };
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      verification.current = request;
      const result = await verifySourceBinding(productId, projectId, binding.id, request.revision, request.receipt);
      const readback = await listSourceBindings(productId, projectId);
      if (scopeRef.current !== scope || loadVersion.current !== version) return;
      setBindings(readback.items);
      const current = readback.items.find(item => item.id === binding.id && item.project_id === projectId);
      if (result.status === 'succeeded' && result.coverage === 'complete' && current?.status === 'healthy'
        && binding.selection.connection_ref
        && current.selection?.provider === binding.selection.provider && current.selection.kind === binding.selection.kind
        && current.selection.native_id === binding.selection.native_id
        && current.selection.connection_ref?.kind === binding.selection.connection_ref.kind
        && current.selection.connection_ref?.id === binding.selection.connection_ref.id
        && current.selection.namespace === binding.selection.namespace) {
        verification.current = null;
        setMessage('Native scope verified. Its Profile source is ready.');
        onSaved();
      } else {
        if (!current?.selection || ['revoked', 'restricted', 'unlinked'].includes(current.status)) verification.current = null;
        setMessage('Native scope could not be verified. Check access and refresh the binding before retrying.');
      }
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) {
        setMessage('Verification outcome is unknown. Refresh the binding before retrying this operation.');
      }
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  const accepts = (kind: string) => !section || (section === 'basic' ? ['repository', 'tracker', 'document', 'tracker_team', 'communication_channel'] : ['environment', 'pipeline', 'ci_job', 'support', 'quality_project', 'deployment_application', 'incident_service']).includes(kind);
  const hasAdapter = (kind: string) => sources.some((source) => source.capabilities.includes(kind));

  async function prepareRepository(source: ProductSource, resource: SourceResource) {
    if (busy || source.connection_ref.kind !== 'github_installation' || !repositoryName.trim()) return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(resourceKey(resource));
    setMessage('Preparing repository preview…');
    setRepositoryPreview(null);
    try {
      const profile = await getProduct(productId, projectId);
      const session = await startOnboarding(productId, projectId, profile.revision);
      const options = await repositoryOptions(productId, projectId, session.id, source.connection_ref.id, resource.native_id);
      const plan: RepositoryPlan = { connection_ref: { kind: 'github_installation', id: source.connection_ref.id },
        owner_native_id: options.owner_native_id, name: repositoryName.trim(), visibility: repositoryVisibility,
        template_ref: options.template_ref, protections_ref: options.protections_ref, confirmed: false };
      const saved = await previewRepository(productId, projectId, session.id, profile.revision, plan, productReceipt());
      if (scopeRef.current === scope && loadVersion.current === version) {
        setRepositoryPreview({ id: saved.id, sessionId: session.id, revision: saved.revision, plan: saved.repository_plan,
          protections: options.protections, receipt: productReceipt() });
        setMessage('Repository preview is ready below.');
      }
    } catch {
      if (scopeRef.current === scope && loadVersion.current === version) setMessage('Repository template or protections changed or cannot be read. Nothing was created.');
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  async function createRepository() {
    if (busy || !repositoryPreview) return;
    const version = loadVersion.current;
    setBusy(true);
    setFeedbackFor(null);
    setMessage('Creating repository…');
    try {
      const outcome = await confirmRepository(productId, projectId, repositoryPreview.sessionId, repositoryPreview.revision,
        repositoryPreview.id, repositoryPreview.plan, repositoryPreview.receipt);
      if (scopeRef.current === scope && loadVersion.current === version) {
        setRepositoryPreview(null);
        setMessage(outcome.state === 'succeeded' && outcome.repository
          ? `Repository created with native ID ${outcome.repository.native_id}.`
          : 'Repository outcome is uncertain. Inspect the saved operation before trying again; do not start a new creation.');
        onSaved();
      }
    } catch (failure) {
      if (scopeRef.current === scope && loadVersion.current === version) {
        const status = (failure as { status?: number }).status;
        if (status === 409 || status === 412) {
          setRepositoryPreview(null);
          setMessage('Repository plan or source changed. Refresh and preview again; no new repository was created.');
        } else setMessage('Repository outcome is uncertain. Inspect saved operations before trying again.');
      }
    } finally {
      if (scopeRef.current === scope && loadVersion.current === version) setBusy(false);
    }
  }

  const isLinked = (source: ProductSource, resource: SourceResource) => bindings.some(binding => binding.project_id === projectId
    && !['unlinked', 'restricted', 'revoked'].includes(binding.status) && binding.selection?.kind === resource.kind
    && binding.selection.provider === source.provider && binding.selection.namespace === resource.namespace && binding.selection.native_id === resource.native_id);
  const linkedCount = (source: ProductSource) => bindings.filter(binding => binding.project_id === projectId
    && !['unlinked', 'restricted', 'revoked'].includes(binding.status)
    && binding.selection?.connection_ref?.kind === source.connection_ref.kind
    && binding.selection.connection_ref.id === source.connection_ref.id).length;
  const availableResources = (selected?.resources ?? []).filter(resource => accepts(resource.kind) || (section === 'advanced' && resource.kind === 'repository'));
  const filteredResources = availableResources.filter(resource => `${resource.full_name ?? ''} ${resource.name} ${resource.native_id}`.toLocaleLowerCase().includes(resourceQuery.trim().toLocaleLowerCase()));
  const currentResourcePage = Math.min(resourcePage, Math.max(0, Math.ceil(filteredResources.length / 10) - 1));
  const visibleResources = filteredResources.slice(currentResourcePage * 10, (currentResourcePage + 1) * 10);

  const feedback = (target: string | null) => feedbackFor === target && (message || pending.current)
    ? <div ref={feedbackRef} className="product-source-feedback">
      {message && <p role="status">{message}</p>}
      {pending.current && !busy && <button type="button" onClick={() => { setMessage('Checking source selection…'); void submit(pending.current!); }}>Check source selection</button>}
    </div> : null;

  return <section aria-label="Project sources" className="blueprint-card product-card product-sources space-y-3">
    <h2 className="text-xl font-semibold">{section === 'advanced' ? 'Environments & delivery' : 'Connections'}</h2>
    {projectName && <p className="product-muted">Repositories, workflows, environments and Jira sources for <strong>{projectName}</strong>. Change the Project above to view another project.</p>}
    <button ref={addConnectionButton} type="button" className="product-primary" onClick={() => setAddingConnection(true)}>Add connection</button>
    {addingConnection && <ProductConnectionSetup productId={productId} projectId={projectId} onSaved={onSaved}
      initialService={isConnectionService(requestedService) ? requestedService : undefined}
      onClose={() => {
        setAddingConnection(false);
        const next = new URLSearchParams(searchParams); next.delete('connection_setup'); setSearchParams(next, { replace: true });
        void load();
        window.setTimeout(() => addConnectionButton.current?.focus(), 0);
      }} />}
    {pipelineRepository && selected && <ProductWorkflowPicker key={`${scope}:${connectionKey(selected)}:${pipelineRepository.native_id}`}
      productId={productId} projectId={projectId} connection={selected} repository={pipelineRepository} busy={busy} pending={!!pending.current}
      feedback={feedback(feedbackFor)} isLinked={resource => isLinked(selected, resource)} onLink={(source, resource) => void choose(source, resource)}
      onClose={() => { setPipelineRepository(null); setFeedbackFor(null); window.setTimeout(() => workflowButton.current?.focus(), 0); }} />}
    <h3 className="font-semibold">Existing connections</h3>
    <p>Browse a connection, then link the sources this project uses.</p>
    {feedback(null)}
    {coverage === 'loading' && <p role="status">Checking authorized connections…</p>}
    {coverage === 'unavailable' && <p role="alert">Sources unavailable. Retry or check your connections; Product saving remains available.</p>}
    {coverage === 'ready' && !sources.length && !nextCursor && (section === 'advanced' || (jiraList.status === 'ready' && !jiraList.count)) && <p>No connected sources yet. Use Add connection to connect your first service.</p>}
    {coverage === 'ready' && sources.map((source) => <div className="product-connection" key={`${source.connection_ref.kind}:${source.connection_ref.id}`}>
      <button type="button" disabled={busy || inspecting !== null} aria-label={`Browse ${sourceLabel(source)}`} aria-busy={inspecting === connectionKey(source)} aria-expanded={selected?.connection_ref.kind === source.connection_ref.kind && selected.connection_ref.id === source.connection_ref.id}
        onClick={() => void inspect(source)}>
        <span className="product-connection-identity">
          <span className="product-provider-icon"><ProviderIcon provider={source.provider} /></span>
          <span className="product-connection-copy"><span>{sourceLabel(source)}</span><span className="product-provider-label">{providerLabel(source.provider)}{/^\d+$/.test(source.label) ? ` · Account ${source.label}` : ''}{linkedCount(source) > 0 ? ` · ${linkedCount(source)} linked` : ''}</span></span>
        </span>
        <span className="flex shrink-0 items-center gap-2 whitespace-nowrap">Browse<LineIcon className="h-4 w-4 shrink-0"><path d="m9 5 7 7-7 7" /></LineIcon></span>
      </button>
      {feedback(connectionKey(source))}
      {selected && connectionKey(selected) === connectionKey(source) && <div aria-label="Source coverage" className="product-source-results">
        <h4 className="font-semibold">Sources in {sourceLabel(selected)}</h4>
        {selected.coverage === 'partial' && <p className="product-muted">Showing the sources this connection can currently access.</p>}
        {(selected.coverage === 'unavailable' || selected.coverage === 'denied') && <p role="alert">Connection metadata unavailable. Verify or reconnect before selecting; Product saving remains available.</p>}
        {availableResources.length > 0 && <label>Search {selected.resource_cursor ? 'loaded ' : ''}sources<input type="search" placeholder="Source name" disabled={busy || !!pending.current} value={resourceQuery} onChange={event => { setResourceQuery(event.target.value); setResourcePage(0); }} /></label>}
        {availableResources.length > 0 && !filteredResources.length && <p>No loaded sources match your search.{selected.resource_cursor && ' Load more sources to continue searching.'}</p>}
        {visibleResources.map((resource) => <div role="group" aria-label={`${resource.kind}: ${resource.full_name || resource.name}`} className="product-source-resource" key={`${resource.kind}:${resource.namespace}:${resource.native_id}:${resource.source_revision}`}>
          <div className="product-resource-heading"><strong>{resource.full_name || resource.name}</strong><span className="product-muted">{resource.kind === 'ci_job' ? 'Jenkins job' : resource.kind}</span></div>
          {resource.verified_environment && <p>Verified account {resource.verified_environment.account_id} · role {resource.verified_environment.role_ref} · regions {resource.verified_environment.regions.join(', ')}</p>}
          {resource.kind === 'environment' && <>
            <label>Environment name <input value={environmentName} onChange={(event) => setEnvironmentName(event.target.value)} /></label>
            <label>Environment class <select value={environmentClass} onChange={(event) => setEnvironmentClass(event.target.value as typeof environmentClass)}>
              {(['development', 'test', 'staging', 'production', 'other'] as const).map((kind) => <option key={kind} value={kind}>{kind}</option>)}
            </select></label>
          </>}
          {accepts(resource.kind) && <button type="button" disabled={busy || !!pending.current || isLinked(selected, resource) || (resource.kind === 'environment' && !environmentName.trim())} onClick={() => void choose(selected, resource)}>{isLinked(selected, resource) ? 'Linked' : `Link ${resource.kind === 'ci_job' ? 'Jenkins job' : resource.kind} to project`}</button>}
          {resource.kind === 'repository' && selected.connection_ref.kind === 'github_installation' &&
            <button type="button" disabled={busy || !!pending.current} onClick={event => { workflowButton.current = event.currentTarget; inspectPipelines(resource); }}>Browse workflows</button>}
          {section !== 'advanced' && resource.kind === 'repository' && selected.connection_ref.kind === 'github_installation' &&
            <details><summary>Create a repository from this template</summary>
              <label>New repository name<input value={repositoryName} onChange={event => { setRepositoryName(event.target.value); setRepositoryPreview(null); }} /></label>
              <label>Visibility<select value={repositoryVisibility} onChange={event => { setRepositoryVisibility(event.target.value as typeof repositoryVisibility); setRepositoryPreview(null); }}>
                <option value="private">Private</option><option value="public">Public</option><option value="internal">Internal</option>
              </select></label>
              <button type="button" disabled={busy || !!pending.current || !repositoryName.trim()} onClick={() => void prepareRepository(selected, resource)}>Preview repository</button>
            </details>}
          {section !== 'advanced' && resource.kind === 'repository' && selected.connection_ref.kind === 'github_installation' &&
            <details><summary>Connect a financial document</summary>
              <p>Choose a JSON financial plan in this repository. Review its suggested amount and date in Budget & spend → Financial planning.</p>
              <label>Document path in {resource.name}<input placeholder="plans/financials.json" maxLength={400}
                value={documentPaths[resource.native_id] ?? ''} disabled={busy || !!pending.current}
                onChange={event => setDocumentPaths(current => ({ ...current, [resource.native_id]: event.target.value }))} /></label>
              <button type="button" disabled={busy || !!pending.current || !documentPaths[resource.native_id]?.trim()}
                onClick={() => void connectDocument(selected, resource)}>Connect document from {resource.name}</button>
            </details>}
          {feedback(resourceKey(resource))}
        </div>)}
        {filteredResources.length > 10 && <nav className="product-list-pagination" aria-label="Source pages">
          <span className="product-muted">{currentResourcePage * 10 + 1}–{Math.min((currentResourcePage + 1) * 10, filteredResources.length)} of {filteredResources.length} loaded sources</span>
          <Button variant="secondary" disabled={busy || !!pending.current || currentResourcePage === 0} onClick={() => setResourcePage(currentResourcePage - 1)}>Previous</Button>
          <Button variant="secondary" disabled={busy || !!pending.current || (currentResourcePage + 1) * 10 >= filteredResources.length} onClick={() => setResourcePage(currentResourcePage + 1)}>Next</Button>
        </nav>}
        {selected.resource_cursor && <button type="button" disabled={busy} onClick={() => void moreResources()}>More resources</button>}
      </div>}
    </div>)}
    {coverage === 'ready' && nextCursor && <button type="button" disabled={busy} onClick={() => void moreConnections()}>More connections</button>}
    {coverage === 'ready' && nextCursor && <p>Other connections may have additional adapters; load them to check coverage.</p>}
    {section !== 'advanced' && !addingConnection && <JiraContextSources key={`${scope}:${jiraRevision}`} productId={productId} projectId={projectId} onSaved={onSaved} list onListChange={setJiraList} />}
    {section === 'advanced' && coverage === 'ready' && !nextCursor && !hasAdapter('pipeline') && !hasAdapter('ci_job') && <p>Pipeline selection unavailable: connect a supported existing pipeline adapter when available. No pipeline will be created.</p>}
    {section === 'advanced' && coverage === 'ready' && !nextCursor && !hasAdapter('support') && !hasAdapter('incident_service') && <p>Support selection unavailable: connect a supported existing support adapter when available.</p>}
    {repositoryPreview && <div aria-label="Repository creation preview">
      <p>Owner ID {repositoryPreview.plan.owner_native_id} · {repositoryPreview.plan.name} · {repositoryPreview.plan.visibility}</p>
      <p>Template ID {repositoryPreview.plan.template_ref.record_id} at {repositoryPreview.plan.template_ref.record_revision}</p>
      <p>Protections revision {repositoryPreview.plan.protections_ref.record_revision}</p>
      <pre>{JSON.stringify(repositoryPreview.protections, null, 2)}</pre>
      <button type="button" disabled={busy} onClick={() => void createRepository()}>Confirm and create this repository</button>
    </div>}
    {bindings.length > 0 && <details><summary>Linked source details</summary>{bindings.map(binding => <div key={binding.id}>
      <p>{binding.selection ? `${binding.selection.kind}: ${binding.selection.native_id}` : 'Restricted source'} · {binding.status}</p>
      {binding.selection && connectorProviders.has(binding.selection.provider) && binding.status !== 'healthy' &&
        <button type="button" disabled={busy} onClick={() => void verify(binding)}>Verify {binding.selection.provider} native scope</button>}
      {feedback(`binding:${binding.id}`)}
    </div>)}</details>}

    {profile && <div aria-label="Delivery home">
      <p><strong>Delivery home repository:</strong> {profile.delivery_home_repository?.repository ?? 'Not set'}{profile.delivery_home_implied && ' (implied)'}</p>
      <p>Changing the home moves future epics, not existing ones.</p>
    </div>}

    {children}
    <button type="button" onClick={() => void load()}>Refresh sources</button>
  </section>;
}
