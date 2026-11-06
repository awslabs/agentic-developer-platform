import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ProductSources from '@/pages/products/ProductSources';
import { bindProductSource, getProduct, inspectProductPipelines, inspectProductSource, listProductSources, listSourceBindings, verifySourceBinding, type ProductSource } from '@/services/products';
import { registerProfileCredential } from '@/services/credentials';
import * as jira from '@/services/jiraContext';

const { devlake } = vi.hoisted(() => ({ devlake: { enabled: false } }));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ hasRole: () => false }) }));
vi.mock('@/services/jiraContext', () => ({ jiraConnections: vi.fn(), jiraSources: vi.fn(), jiraResources: vi.fn() }));
vi.mock('@/hooks/useFeatures', async original => ({
  ...await original<typeof import('@/hooks/useFeatures')>(),
  useRevalidatingFeaturesQuery: () => ({ data: { devlake_connectors: devlake.enabled } }),
}));

vi.mock('@/services/credentials', async original => ({
  ...await original<typeof import('@/services/credentials')>(), registerProfileCredential: vi.fn(),
}));

vi.mock('@/services/products', () => ({
  bindProductSource: vi.fn(), getProduct: vi.fn(), inspectProductSource: vi.fn(),
  inspectProductPipelines: vi.fn(),
  listProductSources: vi.fn(), listSourceBindings: vi.fn(), verifySourceBinding: vi.fn(),
  productReceipt: () => 'i10_browser_receipt_123',
}));

const aws: ProductSource = {
  connection_ref: { kind: 'credential', id: 'connection-1' }, provider: 'aws',
  label: 'Test AWS', health: 'unverified', coverage: 'unavailable', capabilities: ['environment'],
};
const discovered: ProductSource = {
  ...aws, health: 'healthy', coverage: 'partial', resources: [{
    kind: 'environment', namespace: '123456789012', native_id: 'eu-west-1', name: '123456789012 · eu-west-1',
    source_revision: 'fixture-version', verified_environment: {
      account_id: '123456789012', role_ref: 'arn:aws:iam::123456789012:role/ADP-Agent-Role', regions: ['eu-west-1'],
    },
  }],
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [] });
  vi.mocked(jira.jiraSources).mockResolvedValue({ sources: [] });
  devlake.enabled = false;
  vi.mocked(listProductSources).mockResolvedValue({ items: [aws], next_cursor: null });
  vi.mocked(listSourceBindings).mockResolvedValue({ items: [] });
  vi.mocked(inspectProductSource).mockResolvedValue({ items: [discovered] });
  vi.mocked(getProduct).mockResolvedValue({ product_id: 'product-1', project_id: 'project-1', tenant_id: 'org-1', revision: 1,
    product: { name: 'Example', purpose: 'Plan', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-1' } }, configuration: {} });
  vi.mocked(bindProductSource).mockResolvedValue({ id: 'binding-1', project_id: 'project-1', status: 'healthy' });
});

const renderPicker = (section?: 'basic' | 'advanced') => render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={vi.fn()} section={section} /></MemoryRouter>);

describe('Product sources', () => {
  it('shows and searches org/repo while saving the immutable repository IDs', async () => {
    const source: ProductSource = { connection_ref: { kind: 'github_installation', id: 'github-1' }, provider: 'github', label: 'Example GitHub', health: 'healthy', coverage: 'complete', capabilities: ['repository'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [source], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...source, resources: [{ kind: 'repository', namespace: '27', native_id: '35', name: 'service', full_name: 'example/service', source_revision: 'version' }] }] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Example GitHub' }));
    expect(await screen.findByText('example/service')).toBeVisible();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sources' }), { target: { value: 'example/' } });
    const repository = screen.getByRole('group', { name: 'repository: example/service' });
    fireEvent.click(within(repository).getByRole('button', { name: 'Link repository to project' }));
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, expect.objectContaining({ namespace: '27', native_id: '35', display_name: 'example/service' }), expect.any(String)));
  });

  it('lists Jira beside other connections without a separate context section or credential form', async () => {
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [{ id: 'jira-1', label: 'Team Jira', site_id: 'site' }] });
    renderPicker();
    const jiraRow = await screen.findByRole('button', { name: 'Browse Team Jira' });
    expect(await screen.findByRole('button', { name: 'Browse Test AWS' })).toBeVisible();
    expect(jiraRow).toHaveAttribute('aria-expanded', 'false');
    expect(jiraRow.querySelector('svg')).not.toBeNull();
    expect(screen.queryByRole('heading', { name: 'Jira context' })).not.toBeInTheDocument();
    expect(screen.queryByText('Shared Jira access for this Product')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Shared connection')).not.toBeInTheDocument();
    expect(jira.jiraResources).not.toHaveBeenCalled();
  });

  it('shows the empty state only when all providers have loaded with no connections', async () => {
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    let finish!: (result: Awaited<ReturnType<typeof jira.jiraConnections>>) => void;
    vi.mocked(jira.jiraConnections).mockImplementation(() => new Promise(done => { finish = done; }));
    renderPicker();
    await waitFor(() => expect(screen.queryByText('Checking authorized connections…')).not.toBeInTheDocument());
    expect(screen.queryByText(/No connected sources yet/)).not.toBeInTheDocument();
    await act(async () => finish({ connections: [{ id: 'jira-1', label: 'Team Jira', site_id: 'site' }] }));
    expect(await screen.findByRole('button', { name: 'Browse Team Jira' })).toBeVisible();
    expect(screen.queryByText(/No connected sources yet/)).not.toBeInTheDocument();
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByText(/No connected sources yet/)).toBeVisible();
  });

  it('reports Jira discovery failure and retries it through Refresh sources', async () => {
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(jira.jiraConnections).mockRejectedValue(new Error('Unavailable'));
    renderPicker();
    expect(await screen.findByRole('alert')).toHaveTextContent('Jira connections could not be loaded');
    expect(screen.queryByText(/No connected sources yet/)).not.toBeInTheDocument();
    vi.mocked(jira.jiraConnections).mockResolvedValue({ connections: [{ id: 'jira-1', label: 'Team Jira', site_id: 'site' }] });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByRole('button', { name: 'Browse Team Jira' })).toBeVisible();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([undefined, 'basic'] as const)('keeps project context without advanced-only notices in the %s view', async (section) => {
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" projectName="Checkout" onSaved={vi.fn()} section={section}>
      <div>Embedded project content</div>
    </ProductSources></MemoryRouter>);
    await screen.findByRole('button', { name: /Browse Test AWS/ });
    expect(screen.getByRole('heading', { name: 'Connections' })).toBeInTheDocument();
    expect(screen.getByText('Checkout')).toBeInTheDocument();
    expect(screen.getByText('Embedded project content')).toBeInTheDocument();
    expect(screen.queryByText(/Pipeline selection unavailable/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Support selection unavailable/)).not.toBeInTheDocument();
  });

  it.each([
    ['sonarqube', 'quality_project', '/', 'team:api', 'advanced'],
    ['jenkins', 'ci_job', 'team', 'team/api', 'advanced'],
    ['argocd', 'deployment_application', 'team/ns', 'api', 'advanced'],
    ['gitlab', 'repository', '77', '901', 'basic'],
    ['pagerduty', 'incident_service', '/', 'P123', 'advanced'],
    ['linear', 'tracker_team', '/', 'team-a', 'basic'],
    ['slack', 'communication_channel', 'T123', 'C123', 'basic'],
  ] as const)('keeps %s native identity when linking a discovered page', async (provider, kind, namespace, nativeId, section) => {
    const connection: ProductSource = {
      connection_ref: { kind: 'credential', id: `${provider}-connection` }, provider,
      label: `Controlled ${provider}`, health: 'healthy', coverage: 'unavailable', capabilities: [kind],
    };
    const first = { ...connection, coverage: 'partial' as const, resource_cursor: 'next', resources: [
      { kind, namespace, native_id: `${nativeId}-other`, name: 'Same name', source_revision: 'old-revision' },
    ] };
    const second = { ...connection, coverage: 'complete' as const, resources: [
      { kind, namespace, native_id: nativeId, name: 'Same name', source_revision: 'current-revision' },
    ] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [connection], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValueOnce({ items: [first] }).mockResolvedValueOnce({ items: [second] });
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={vi.fn()} section={section} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: `Browse Controlled ${provider}` }));
    if (provider === 'pagerduty') expect(screen.queryByText(/Support selection unavailable/)).not.toBeInTheDocument();
    const linkLabel = `Link ${kind === 'ci_job' ? 'Jenkins job' : kind} to project`;
    await screen.findByRole('button', { name: linkLabel });
    fireEvent.click(screen.getByRole('button', { name: 'More resources' }));
    await waitFor(() => expect(screen.getAllByRole('button', { name: linkLabel })).toHaveLength(2));
    fireEvent.click(screen.getAllByRole('button', { name: linkLabel })[1]);
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, {
      kind, provider, namespace, native_id: nativeId, display_name: 'Same name', connection_ref: connection.connection_ref,
    }, 'i10_browser_receipt_123'));
    expect(inspectProductSource).toHaveBeenNthCalledWith(2, 'product-1', 'project-1', connection.connection_ref, 'next');
  });

  it('does not offer a selection when connector discovery is unavailable', async () => {
    const connection: ProductSource = { connection_ref: { kind: 'credential', id: 'sonar-connection' }, provider: 'sonarqube',
      label: 'Controlled SonarQube', health: 'unverified', coverage: 'unavailable', capabilities: ['quality_project'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [connection], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...connection, resources: [] }] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Controlled SonarQube' }));
    expect(await screen.findByText('No sources are available from this connection. Check its access or refresh to try again.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Link quality_project/ })).not.toBeInTheDocument();
    expect(bindProductSource).not.toHaveBeenCalled();
  });

  it('registers a connector, browses its served scopes, links and verifies before claiming Profile readiness', async () => {
    devlake.enabled = true;
    const connection: ProductSource = { connection_ref: { kind: 'credential', id: 'sonar-connection' },
      provider: 'sonarqube', label: 'Controlled SonarQube', health: 'unverified', coverage: 'unavailable',
      capabilities: ['quality_project'] };
    const resource = { kind: 'quality_project' as const, namespace: '/', native_id: 'team:api',
      name: 'API quality', source_revision: 'observation-sha256:fixture' };
    const selection = { kind: resource.kind, provider: 'sonarqube', namespace: '/', native_id: resource.native_id,
      connection_ref: connection.connection_ref };
    const unverified = { id: 'binding-1', project_id: 'project-1', status: 'unverified', selection };
    vi.mocked(listProductSources).mockResolvedValueOnce({ items: [], next_cursor: null })
      .mockResolvedValue({ items: [connection], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...connection, coverage: 'complete', resources: [resource] }] });
    vi.mocked(registerProfileCredential).mockResolvedValue({ id: connection.connection_ref.id } as never);
    vi.mocked(bindProductSource).mockResolvedValue(unverified);
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [] }).mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [unverified] }).mockResolvedValue({ items: [{ ...unverified, status: 'healthy' }] });
    vi.mocked(verifySourceBinding).mockResolvedValue({ operation_id: 'operation-1', status: 'succeeded', coverage: 'complete' });
    const onSaved = vi.fn();
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={onSaved} section="advanced" /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: 'Add connection' }));
    const dialog = screen.getByRole('dialog', { name: 'Add connection' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'SonarQube' }));
    fireEvent.change(within(dialog).getByLabelText('Connection label'), { target: { value: 'Controlled SonarQube' } });
    fireEvent.change(within(dialog).getByLabelText('Provider HTTPS endpoint'), { target: { value: 'https://sonar.example.test' } });
    fireEvent.change(within(dialog).getByLabelText('Read-only token'), { target: { value: 'fixture-only' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Register credential' }));
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Done — return to Product' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Controlled SonarQube' }));
    expect(inspectProductSource).toHaveBeenCalledWith('product-1', 'project-1', connection.connection_ref);
    fireEvent.click(await screen.findByRole('button', { name: 'Link quality_project to project' }));
    await screen.findByText('Linked quality_project to this project. Verify its native scope before using it in Profile.');
    expect(screen.queryByText('Native scope verified. Its Profile source is ready.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Linked source details'));
    fireEvent.click(screen.getByRole('button', { name: 'Verify sonarqube native scope' }));
    await screen.findByText('Native scope verified. Its Profile source is ready.');
    expect(verifySourceBinding).toHaveBeenCalledWith('product-1', 'project-1', 'binding-1', 1, 'i10_browser_receipt_123');
    expect(onSaved).toHaveBeenCalledTimes(3);
  });

  it('keeps a denied or failed connector verification out of Profile readiness', async () => {
    const connection: ProductSource = { connection_ref: { kind: 'credential', id: 'sonar-connection' },
      provider: 'sonarqube', label: 'Controlled SonarQube', health: 'unverified', coverage: 'unavailable',
      capabilities: ['quality_project'] };
    const binding = { id: 'binding-1', project_id: 'project-1', status: 'unverified',
      selection: { kind: 'quality_project', provider: 'sonarqube', namespace: '/', native_id: 'team:api' } };
    vi.mocked(listProductSources).mockResolvedValue({ items: [connection], next_cursor: null });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(inspectProductSource).mockRejectedValue(new Error('Go unavailable'));
    vi.mocked(verifySourceBinding).mockResolvedValue({ operation_id: 'operation-1', status: 'failed', coverage: 'unavailable' });
    const onSaved = vi.fn();
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={onSaved} section="advanced" /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: 'Browse Controlled SonarQube' }));
    expect(await screen.findByText('Source access changed or discovery is unavailable. Refresh choices or reconnect.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Link quality_project/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Linked source details'));
    fireEvent.click(screen.getByRole('button', { name: 'Verify sonarqube native scope' }));
    expect(await screen.findByText('Native scope could not be verified. Check access and refresh the binding before retrying.')).toBeInTheDocument();
    expect(screen.queryByText('Native scope verified. Its Profile source is ready.')).not.toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('recognizes a registered Jenkins job capability in the advanced section', async () => {
    const jenkins: ProductSource = {
      connection_ref: { kind: 'credential', id: 'jenkins-connection' }, provider: 'jenkins',
      label: 'Existing Jenkins', health: 'unverified', coverage: 'unavailable', capabilities: ['ci_job'],
    };
    vi.mocked(listProductSources).mockResolvedValue({ items: [jenkins], next_cursor: null });
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={vi.fn()} section="advanced" /></MemoryRouter>);
    await screen.findByRole('button', { name: /Browse Existing Jenkins/ });
    expect(screen.queryByText(/Pipeline selection unavailable/)).not.toBeInTheDocument();
  });

  it('shows one delivery home row and the planning caveat for two bound GitHub repositories', async () => {
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [
      { id: 'binding-one', project_id: 'project-1', status: 'healthy', selection: { kind: 'repository', provider: 'github', namespace: 'github', native_id: '101' } },
      { id: 'binding-two', project_id: 'project-1', status: 'healthy', selection: { kind: 'repository', provider: 'github', namespace: 'github', native_id: '102' } },
    ] });
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={vi.fn()} profile={{
      delivery_home_repository: { provider: 'github', repository: 'example/planning', repository_id: 101 },
      delivery_home_implied: false,
    }} /></MemoryRouter>);
    await screen.findByText('repository: 102 · healthy');
    const home = screen.getByLabelText('Delivery home');
    expect(within(home).getByText('Delivery home repository:').parentElement).toHaveTextContent('Delivery home repository: example/planning');
    expect(within(home).getByText('Changing the home moves future epics, not existing ones.')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Delivery home')).toHaveLength(1);
  });

  it.each(['connected', 'empty', 'unavailable'])('offers Add connection when existing connections are %s', async (state) => {
    if (state === 'empty') vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    if (state === 'unavailable') vi.mocked(listProductSources).mockRejectedValue(new Error('Unavailable'));
    renderPicker();
    fireEvent.click(screen.getByRole('button', { name: 'Add connection', exact: true }));
    const dialog = screen.getByRole('dialog', { name: 'Add connection' });
    expect(within(dialog).getByRole('button', { name: 'GitHub', exact: true })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'AWS account' })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Jira', exact: true })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Slack', exact: true })).toBeInTheDocument();
    expect(within(dialog).queryByRole('link')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close modal' }));
    expect(screen.getByRole('heading', { name: 'Existing connections' })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Checking authorized connections…')).not.toBeInTheDocument());
  });

  it.each(['basic', 'advanced'] as const)('opens Jira setup inside the Product from the %s section', async section => {
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={vi.fn()} section={section} /></MemoryRouter>);
    fireEvent.click(screen.getByRole('button', { name: 'Add connection', exact: true }));
    fireEvent.click(within(screen.getByRole('dialog', { name: 'Add connection' })).getByRole('button', { name: 'Jira', exact: true }));
    expect(screen.getByRole('dialog', { name: 'Connect Jira' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close setup' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add connection' })).toHaveFocus());
    await waitFor(() => expect(screen.queryByText('Checking authorized connections…')).not.toBeInTheDocument());
  });

  it('shows source discovery progress immediately and prevents repeated clicks', async () => {
    let resolve!: (result: { items: ProductSource[] }) => void;
    vi.mocked(inspectProductSource).mockReturnValue(new Promise(done => { resolve = done; }));
    renderPicker();
    const button = await screen.findByRole('button', { name: /Browse Test AWS/ });
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('Loading sources…')).toBeInTheDocument();
    fireEvent.click(button);
    expect(inspectProductSource).toHaveBeenCalledTimes(1);
    await act(async () => { resolve({ items: [discovered] }); });
    expect(button).toBeEnabled();
    expect(screen.queryByText('Loading sources…')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Link environment to project' })).toBeInTheDocument();
  });

  it('keeps selection progress and success beside the selected source', async () => {
    let resolve!: (result: Awaited<ReturnType<typeof bindProductSource>>) => void;
    vi.mocked(bindProductSource).mockReturnValue(new Promise(done => { resolve = done; }));
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [] }).mockResolvedValue({ items: [
      { id: 'binding-1', project_id: 'project-1', status: 'healthy' },
    ] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    const group = await screen.findByRole('group', { name: 'environment: 123456789012 · eu-west-1' });
    fireEvent.change(within(group).getByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(within(group).getByRole('button', { name: 'Link environment to project' }));
    expect(within(group).getByRole('status')).toHaveTextContent('Linking environment…');
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledTimes(1));
    await act(async () => { resolve({ id: 'binding-1', project_id: 'project-1', status: 'healthy' }); });
    expect(within(group).getByRole('status')).toHaveTextContent('Linked environment to this project.');
    expect(screen.queryByRole('button', { name: 'Check source selection' })).not.toBeInTheDocument();
  });

  it('shows a rejected selection beside its source and permits a corrected attempt', async () => {
    vi.mocked(bindProductSource).mockRejectedValue({ status: 409, detail: { code: 'connection_not_verified' } });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    const group = await screen.findByRole('group', { name: 'environment: 123456789012 · eu-west-1' });
    fireEvent.change(within(group).getByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(within(group).getByRole('button', { name: 'Link environment to project' }));
    await waitFor(() => expect(within(group).getByRole('status')).toHaveTextContent('Verify this connection before adding its sources.'));
    expect(screen.queryByRole('button', { name: 'Check source selection' })).not.toBeInTheDocument();
    expect(within(group).getByRole('button', { name: 'Link environment to project' })).toBeEnabled();
  });

  it('keeps a page error visible when the previously selected resource disappears', async () => {
    vi.mocked(inspectProductSource).mockResolvedValueOnce({ items: [{ ...discovered, resource_cursor: 'next' }] })
      .mockRejectedValueOnce(new Error('Connection unavailable'));
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [] }).mockResolvedValue({ items: [
      { id: 'binding-1', project_id: 'project-1', status: 'healthy' },
    ] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    fireEvent.change(await screen.findByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link environment to project' }));
    await screen.findByText('Linked environment to this project.');
    fireEvent.click(screen.getByRole('button', { name: 'More resources' }));
    expect(await screen.findByText('Source access changed. Refresh choices or reconnect.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Link environment to project' })).not.toBeInTheDocument();
  });

  it('connects a document using the chosen repository and connection without asking for IDs', async () => {
    const github: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'healthy', coverage: 'complete', capabilities: ['repository'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [github], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...github, resources: [
      { kind: 'repository', namespace: '27', native_id: '35', name: 'existing', source_revision: 'repo-rev' },
    ] }] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    fireEvent.click(await screen.findByText('Connect a financial document'));
    fireEvent.change(screen.getByLabelText('Document path in existing'), { target: { value: 'plans/financials.json' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect document from existing' }));
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, {
      kind: 'document', provider: 'github', namespace: '27', native_id: '35:plans/financials.json', display_name: 'plans/financials.json', connection_ref: github.connection_ref,
    }, 'i10_browser_receipt_123'));
    expect(screen.queryByLabelText(/repository id|account id/i)).not.toBeInTheDocument();
  });
  it('shows partial coverage and missing adapters without inventing native IDs', async () => {
    renderPicker('advanced');
    expect(await screen.findByText(/Pipeline selection unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Support selection unavailable/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Browse Test AWS/ }));
    expect(await screen.findByText(/Showing the sources this connection/)).toBeInTheDocument();
    expect(screen.getByText(/Verified account 123456789012/)).toHaveTextContent('ADP-Agent-Role');
    expect(screen.queryByText(/pipeline: /)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link environment to project' }));
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, expect.objectContaining({
      kind: 'environment', native_id: 'eu-west-1', namespace: '123456789012',
      environment: { name: 'Staging', environment_class: 'development', production: false },
    }), 'i10_browser_receipt_123'));
  });

  it('selects an already discovered repository using its native IDs without asking for them again', async () => {
    const github: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'healthy', coverage: 'complete', capabilities: ['repository'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [github], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...github, resources: [
      { kind: 'repository', namespace: '27', native_id: '35', name: 'existing', source_revision: 'repo-rev' },
    ] }] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    expect(await screen.findByText('existing')).toBeInTheDocument();
    expect(screen.queryByLabelText(/repository id|account id/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Link repository to project' }));
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, {
      kind: 'repository', provider: 'github', namespace: '27', native_id: '35', display_name: 'existing',
      connection_ref: github.connection_ref,
    }, 'i10_browser_receipt_123'));
    expect(inspectProductSource).toHaveBeenCalledWith('product-1', 'project-1', github.connection_ref);
  });

  it('loads subsequent connection and resource pages before offering their native IDs', async () => {
    const nextConnection: ProductSource = {
      connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'unverified', coverage: 'unavailable', capabilities: ['repository', 'pipeline'],
    };
    const firstPage: ProductSource = { ...nextConnection, coverage: 'partial', resource_cursor: '2', resources: [
      { kind: 'repository', namespace: '27', native_id: '101', name: 'first', source_revision: 'rev-1' },
    ] };
    const secondPage: ProductSource = { ...nextConnection, coverage: 'complete', resources: [
      { kind: 'repository', namespace: '27', native_id: '102', name: 'second', source_revision: 'rev-2' },
    ] };
    vi.mocked(listProductSources).mockResolvedValueOnce({ items: [aws], next_cursor: 'credential:connection-1' })
      .mockResolvedValueOnce({ items: [nextConnection], next_cursor: null });
    vi.mocked(inspectProductSource).mockImplementation(async (_productId, _projectId, _connection, cursor) => ({
      items: [cursor === '2' ? secondPage : firstPage],
    }));
    renderPicker();
    expect(await screen.findByRole('button', { name: 'More connections' })).toBeInTheDocument();
    expect(screen.queryByText(/Pipeline selection unavailable/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'More connections' }));
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    expect(await screen.findByText('first')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'More resources' }));
    expect(await screen.findByText('second')).toBeInTheDocument();
    expect(screen.getByText('first')).toBeInTheDocument();
    expect(inspectProductSource).toHaveBeenCalledWith('product-1', 'project-1', nextConnection.connection_ref, '2');
  });

  it('resumes an interrupted resource page without selecting another binding', async () => {
    const github: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'healthy', coverage: 'partial', capabilities: ['repository'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [github], next_cursor: null });
    const owned = { id: 'binding-owned', project_id: 'project-1', status: 'healthy',
      selection: { kind: 'repository', provider: 'github', namespace: '27', native_id: '35' } };
    const shared = { id: 'binding-shared', project_id: 'project-1', status: 'healthy',
      selection: { kind: 'repository', provider: 'github', namespace: '27', native_id: '36' } };
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [owned, shared] })
      .mockResolvedValue({ items: [{ ...owned, status: 'unlinked' }, shared] });
    let interrupted = true;
    vi.mocked(inspectProductSource).mockImplementation(async (_product, _project, _connection, cursor) => {
      if (cursor === '2' && interrupted) {
        interrupted = false;
        throw new Error('Interrupted page');
      }
      return { items: [{ ...github, resource_cursor: cursor ? undefined : '2', resources: [{
        kind: 'repository', namespace: '27', native_id: cursor ? '36' : '35',
        name: cursor ? 'second' : 'first', source_revision: cursor ? 'rev-2' : 'rev-1',
      }] }] };
    });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'More resources' }));
    expect(await screen.findByText(/Source access changed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Browse Existing GitHub/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'More resources' }));
    expect(await screen.findByText('second')).toBeInTheDocument();
    expect(screen.getAllByText('first')).toHaveLength(1);
    expect(inspectProductSource).toHaveBeenLastCalledWith('product-1', 'project-1', github.connection_ref, '2');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByText('repository: 35 · unlinked')).toBeInTheDocument();
    expect(screen.getByText('repository: 36 · healthy')).toBeInTheDocument();
    expect(bindProductSource).not.toHaveBeenCalled();
  });

  it('selects a discovered existing pipeline without asking for provider write authority', async () => {
    const connection: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'healthy', coverage: 'complete', capabilities: ['repository', 'pipeline'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [connection], next_cursor: null });
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [{ ...connection, resources: [
      { kind: 'repository', namespace: '27', native_id: '35', name: 'existing', source_revision: 'repo-rev' },
    ] }] });
    vi.mocked(inspectProductPipelines).mockResolvedValue({ items: [{ ...connection, resources: [
      { kind: 'pipeline', namespace: '27', native_id: '77', name: 'Existing build', source_revision: 'pipeline-rev', repository_id: '35' },
    ] }] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Browse workflows' }));
    expect(await screen.findByText('Existing build')).toBeInTheDocument();
    expect(inspectProductPipelines).toHaveBeenCalledWith('product-1', 'project-1', connection.connection_ref, '35', undefined);
    fireEvent.click(screen.getByRole('button', { name: 'Link to project' }));
    await waitFor(() => expect(bindProductSource).toHaveBeenCalledWith('product-1', 'project-1', 1, {
      kind: 'pipeline', provider: 'github', namespace: '27', native_id: '77', connection_ref: connection.connection_ref,
      repository_id: '35', source_revision: 'pipeline-rev', display_name: 'Existing build',
    }, 'i10_browser_receipt_123'));
  });

  it('discards a pipeline response after selecting a different connection', async () => {
    const github: ProductSource = { connection_ref: { kind: 'github_installation', id: 'connection-2' }, provider: 'github',
      label: 'Existing GitHub', health: 'healthy', coverage: 'complete', capabilities: ['repository', 'pipeline'] };
    vi.mocked(listProductSources).mockResolvedValue({ items: [github, aws], next_cursor: null });
    vi.mocked(inspectProductSource).mockImplementation(async (_product, _project, connection) => ({ items: connection.kind === 'credential'
      ? [discovered] : [{ ...github, resources: [{ kind: 'repository', namespace: '27', native_id: '35', name: 'existing', source_revision: 'repo-rev' }] }] }));
    let resolvePipelines!: (page: { items: ProductSource[] }) => void;
    vi.mocked(inspectProductPipelines).mockReturnValue(new Promise((resolve) => { resolvePipelines = resolve; }));
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Existing GitHub/ }));
    fireEvent.click(await screen.findByRole('button', { name: 'Browse workflows' }));
    fireEvent.click(screen.getByRole('button', { name: /Browse Test AWS/ }));
    await screen.findByText(/Verified account 123456789012/);
    await act(async () => resolvePipelines({ items: [{ ...github, resources: [
      { kind: 'pipeline', namespace: '27', native_id: '77', repository_id: '35', name: 'Existing build', source_revision: 'pipeline-rev' },
    ] }] }));
    expect(screen.queryByRole('button', { name: 'Link to project' })).not.toBeInTheDocument();
  });

  it('clears old source details on Project switch and revoked reload', async () => {
    vi.mocked(listProductSources).mockImplementation(async (_, projectId) => ({
      items: projectId === 'project-1' ? [aws] : [{ ...aws, label: 'Other Project AWS' }], next_cursor: null,
    }));
    vi.mocked(listSourceBindings).mockImplementation(async (_, projectId) => ({
      items: projectId === 'project-1' ? [] : [{ id: 'binding-2', project_id: 'project-2', status: 'healthy',
        selection: { kind: 'environment', provider: 'aws', namespace: '123456789012', native_id: 'eu-west-1' } }],
    }));
    const view = render(<MemoryRouter><ProductSources key="project-1" productId="product-1" projectId="project-1" onSaved={vi.fn()} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    await screen.findByText(/Verified account 123456789012/);
    view.rerender(<MemoryRouter><ProductSources key="project-2" productId="product-1" projectId="project-2" onSaved={vi.fn()} /></MemoryRouter>);
    expect(await screen.findByRole('button', { name: /Browse Other Project AWS/ })).toBeInTheDocument();
    expect(screen.queryByText(/Verified account 123456789012/)).not.toBeInTheDocument();
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(listSourceBindings).mockResolvedValue({ items: [{ id: 'binding-2', project_id: 'project-2', status: 'restricted' }] });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByText('Restricted source · restricted')).toBeInTheDocument();
    expect(screen.queryByText(/eu-west-1/)).not.toBeInTheDocument();
    expect(screen.queryByText(/ADP-Agent-Role/)).not.toBeInTheDocument();
  });

  it('leaves Product saving usable when a connection is unavailable', async () => {
    vi.mocked(inspectProductSource).mockResolvedValue({ items: [aws] });
    renderPicker();
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    expect(await screen.findByText(/Connection metadata unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Link environment to project' })).not.toBeInTheDocument();
    expect(bindProductSource).not.toHaveBeenCalled();
  });

  it.each(['healthy', 'failed'])('keeps a restricted refresh when an older submission readback is %s', async (outcome) => {
    let resolveReadback!: (value: Awaited<ReturnType<typeof listSourceBindings>>) => void;
    let rejectReadback!: (reason: Error) => void;
    const readback = new Promise<Awaited<ReturnType<typeof listSourceBindings>>>((resolve, reject) => {
      resolveReadback = resolve;
      rejectReadback = reject;
    });
    vi.mocked(listSourceBindings).mockResolvedValueOnce({ items: [] }).mockReturnValueOnce(readback)
      .mockResolvedValue({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'restricted' }] });
    const onSaved = vi.fn();
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={onSaved} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    await screen.findByText(/Verified account 123456789012/);
    fireEvent.change(screen.getByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link environment to project' }));
    await waitFor(() => expect(listSourceBindings).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    expect(await screen.findByText('Restricted source · restricted')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check source selection' })).toBeEnabled();
    await act(async () => {
      if (outcome === 'healthy') {
        resolveReadback({ items: [{ id: 'binding-1', project_id: 'project-1', status: 'healthy',
          selection: { kind: 'environment', provider: 'aws', namespace: '123456789012', native_id: 'eu-west-1' } }] });
      } else {
        rejectReadback(new Error('Old readback failed'));
      }
    });
    expect(screen.getByText('Restricted source · restricted')).toBeInTheDocument();
    expect(screen.queryByText(/eu-west-1|Selected environment|Source write outcome is unknown/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Check source selection' })).toBeEnabled();
    expect(onSaved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Check source selection' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(vi.mocked(bindProductSource).mock.calls[1]).toEqual(vi.mocked(bindProductSource).mock.calls[0]);
    expect(screen.getByText('Restricted source · restricted')).toBeInTheDocument();
    expect(screen.queryByText(/eu-west-1/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Check source selection' })).not.toBeInTheDocument();
  });

  it('does not start a selection whose Project revision arrives after refresh', async () => {
    const profile = await getProduct('product-1', 'project-1');
    let resolveProfile!: (value: typeof profile) => void;
    vi.mocked(getProduct).mockReturnValueOnce(new Promise((resolve) => { resolveProfile = resolve; }));
    const onSaved = vi.fn();
    render(<MemoryRouter><ProductSources productId="product-1" projectId="project-1" onSaved={onSaved} /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('button', { name: /Browse Test AWS/ }));
    await screen.findByText(/Verified account 123456789012/);
    fireEvent.change(screen.getByLabelText('Environment name'), { target: { value: 'Staging' } });
    fireEvent.click(screen.getByRole('button', { name: 'Link environment to project' }));
    await waitFor(() => expect(getProduct).toHaveBeenCalledTimes(2));
    vi.mocked(listProductSources).mockResolvedValue({ items: [], next_cursor: null });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh sources' }));
    await screen.findByText(/No connected sources yet/);
    await act(async () => { resolveProfile(profile); });
    expect(bindProductSource).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Check source selection' })).not.toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
  });
});
