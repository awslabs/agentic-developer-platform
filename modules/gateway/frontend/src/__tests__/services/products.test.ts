import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { addPortfolioMembership, closePortfolioMembership, createProduct, createScope, getOnboarding, getProduct, inspectProductPipelines, linkScope, listInitiatives, listProductContributions, listPortfolioMemberships, listPortfolios, listProducts, listProductScopes, listProjectBindings, listProjects, startOnboarding, guideOnboarding, unlinkScope, updateProduct, updateProject } from '@/services/products';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), request: vi.fn() } }));

describe('Product API client', () => {
  beforeEach(() => vi.resetAllMocks());

  it('requests only a paged repository-scoped pipeline read', async () => {
    await inspectProductPipelines('product-1', 'project-1', { kind: 'github_installation', id: 'install-1' }, '35', '2');
    expect(apiClient.get).toHaveBeenCalledWith(
      '/v1/products/product-1/projects/project-1/sources?connection_kind=github_installation&connection_id=install-1&pipeline_repository_id=35&resource_cursor=2',
    );
    expect(apiClient.request).not.toHaveBeenCalled();
  });

  it('uses catalog authorization, pinned references and revision preconditions for Portfolio membership', async () => {
    const portfolio = { tenant_id: 'tenant-a', project_id: 'project-a', source_connection_id: 'adp.catalog',
      record_type: 'portfolio', record_id: 'portfolio-a', record_revision: '2' };
    const product = { ...portfolio, source_connection_id: 'adp.profile', record_type: 'product', record_id: 'product-a', record_revision: '4' };
    const membership = { relationship_id: 'edge-a', relationship_revision: '1', portfolio_ref: portfolio,
      product_ref: product, valid_from: '2026-10-21T00:00:00Z' };
    await listPortfolios('cursor+=');
    await listPortfolioMemberships('portfolio-a', 'next+=');
    await addPortfolioMembership(portfolio, product, '2026-10-21T00:00:00Z', 'add_receipt');
    await closePortfolioMembership('portfolio-a', membership, '2026-10-25T00:00:00Z', 'close_receipt');
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/v1/catalog/portfolios?cursor=cursor%2B%3D');
    expect(apiClient.get).toHaveBeenNthCalledWith(2, '/v1/catalog/portfolios/portfolio-a/memberships?cursor=next%2B%3D');
    expect(apiClient.request).toHaveBeenNthCalledWith(1, '/v1/catalog/portfolios/portfolio-a/memberships', {
      method: 'POST', headers: { 'Idempotency-Key': 'add_receipt' },
      body: { portfolio_ref: portfolio, product_ref: product, source_refs: [portfolio, product], valid_from: '2026-10-21T00:00:00Z' },
    });
    expect(apiClient.request).toHaveBeenNthCalledWith(2, '/v1/catalog/portfolios/portfolio-a/memberships/edge-a', {
      method: 'PATCH', headers: { 'Idempotency-Key': 'close_receipt', 'If-Match': '1' }, body: { valid_to: '2026-10-25T00:00:00Z' },
    });
  });

  it('keeps the effective date on every membership readback page', async () => {
    await listPortfolioMemberships('portfolio/a', undefined, '2099-01-01T00:00:00Z');
    await listPortfolioMemberships('portfolio/a', 'next+=', '2099-01-01T00:00:00Z');
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/v1/catalog/portfolios/portfolio%2Fa/memberships?effective_at=2099-01-01T00%3A00%3A00Z');
    expect(apiClient.get).toHaveBeenNthCalledWith(2, '/v1/catalog/portfolios/portfolio%2Fa/memberships?cursor=next%2B%3D&effective_at=2099-01-01T00%3A00%3A00Z');
    expect(apiClient.request).not.toHaveBeenCalled();
  });

  it('reads authorized Initiative and scoped Contribution pages without issuing creation requests', async () => {
    await listInitiatives('cursor+=');
    await listProductContributions('project-1', 'next+=');
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/v1/catalog/initiatives?cursor=cursor%2B%3D');
    expect(apiClient.get).toHaveBeenNthCalledWith(2, '/v1/catalog/projects/project-1/contributions?cursor=next%2B%3D');
    expect(apiClient.request).not.toHaveBeenCalled();
  });

  it('starts and reads the same scoped onboarding session through the gateway API', async () => {
    const session = { id: 'saved-session', revision: 1, progress: { completed: 2, applicable: 2, percentage: 100, maturity: 'saved' } };
    vi.mocked(apiClient.request).mockResolvedValueOnce(session);
    vi.mocked(apiClient.get).mockResolvedValueOnce(session);
    const started = await startOnboarding('product-1', 'project-1', 1, 'browser_receipt_12345');
    const read = await getOnboarding('product-1', 'project-1', started.id);
    expect(read).toEqual(started);
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products/product-1/projects/project-1/onboarding-sessions', {
      method: 'POST', headers: { 'Idempotency-Key': 'browser_receipt_12345', 'If-Profile-Revision': '1' }, body: {},
    });
    expect(apiClient.get).toHaveBeenCalledWith('/v1/products/product-1/projects/project-1/onboarding-sessions/saved-session');
  });

  it('requests guidance with a scoped session and leaves direct edits separate', async () => {
    vi.mocked(apiClient.request).mockResolvedValueOnce({ revision: 3, mode: 'deterministic', message: 'Next step', proposals: [] });
    expect(await guideOnboarding('product-1', 'project-1', 'session-1', 3, 'What next?', 'deterministic', 'guide_receipt_12345'))
      .toEqual({ revision: 3, mode: 'deterministic', message: 'Next step', proposals: [] });
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products/product-1/projects/project-1/onboarding-sessions/session-1/guidance', {
      method: 'POST', headers: { 'Idempotency-Key': 'guide_receipt_12345', 'If-Profile-Revision': '3' },
      body: { message: 'What next?', mode: 'deterministic' },
    });
  });

  it('opts into model assistance only when requested explicitly', async () => {
    vi.mocked(apiClient.request).mockResolvedValueOnce({ revision: 3, mode: 'model', message: 'Review suggestions', proposals: [] });
    await guideOnboarding('product-1', 'project-1', 'session-1', 3, 'Review this source', 'model', 'model_receipt_12345');
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products/product-1/projects/project-1/onboarding-sessions/session-1/guidance', {
      method: 'POST', headers: { 'Idempotency-Key': 'model_receipt_12345', 'If-Profile-Revision': '3' },
      body: { message: 'Review this source', mode: 'model' },
    });
  });

  it('uses canonical Project binding paths, revisions and receipts for management', async () => {
    await createScope('one', 3, 'Beta', 'create_new_project_01');
    await linkScope('one', 4, 'other', 'link_old_project_01');
    await unlinkScope('one', 5, 'other', 'unlink_old_project_01');
    await listProjects('next+=');
    await listProjectBindings('one', 'other');
    expect(apiClient.request).toHaveBeenNthCalledWith(1, '/v1/products/one/projects', {
      method: 'POST', headers: { 'Idempotency-Key': 'create_new_project_01', 'If-Profile-Revision': '3' }, body: { mode: 'create', name: 'Beta' },
    });
    expect(apiClient.request).toHaveBeenNthCalledWith(2, '/v1/products/one/projects', {
      method: 'POST', headers: { 'Idempotency-Key': 'link_old_project_01', 'If-Profile-Revision': '4' }, body: { mode: 'select', project_id: 'other' },
    });
    expect(apiClient.request).toHaveBeenNthCalledWith(3, '/v1/products/one/projects/other', {
      method: 'DELETE', headers: { 'Idempotency-Key': 'unlink_old_project_01', 'If-Profile-Revision': '5' },
    });
    expect(apiClient.get).toHaveBeenCalledWith('/v1/projects?cursor=next%2B%3D');
    expect(apiClient.get).toHaveBeenCalledWith('/v1/products/one/projects/other/project-bindings');
  });

  it('creates a default first scope with a unique idempotency receipt and no unrelated fields', async () => {
    await createProduct('Planner', 'Coordinate releases', ['Maintainers']);
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products', {
      method: 'POST',
      headers: { 'Idempotency-Key': expect.stringMatching(/^[a-f0-9]{32}$/) },
      body: { name: 'Planner', purpose: 'Coordinate releases', intended_users: ['Maintainers'], starting_point: 'idea', project: { mode: 'create' } },
    });
  });

  it('updates only the selected Project name with a revision and receipt', async () => {
    await updateProject('one', 'two', 8, 'Scoped name');
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products/one/projects/two/configuration', {
      method: 'PATCH',
      headers: { 'Idempotency-Key': expect.stringMatching(/^[a-f0-9]{32}$/), 'If-Profile-Revision': '8' },
      body: { name: 'Scoped name' },
    });
  });

  it('sends a revision precondition with each edit', async () => {
    await updateProduct('one', 'two', 7, { name: 'Planner', purpose: 'Updated', intended_users: ['Maintainers'] });
    expect(apiClient.request).toHaveBeenCalledWith('/v1/products/one/projects/two', {
      method: 'PATCH',
      headers: { 'Idempotency-Key': expect.stringMatching(/^[a-f0-9]{32}$/), 'If-Profile-Revision': '7' },
      body: { name: 'Planner', purpose: 'Updated', intended_users: ['Maintainers'] },
    });
  });

  it('loads the authorized scope IDs from the Product page containing the selected Product', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ items: [], next_cursor: 'next' })
      .mockResolvedValueOnce({ items: [{ id: 'target', project_ids: ['first', 'second'] }], next_cursor: null });
    expect(await listProductScopes('target')).toEqual(['first', 'second']);
    expect(apiClient.get).toHaveBeenNthCalledWith(2, '/v1/products?cursor=next');
  });

  it('reads a product independently and paginates only authorized results', async () => {
    await getProduct('product-1', 'project-1');
    await listProducts('cursor+=');
    expect(apiClient.get).toHaveBeenNthCalledWith(1, '/v1/products/product-1/projects/project-1');
    expect(apiClient.get).toHaveBeenNthCalledWith(2, '/v1/products?cursor=cursor%2B%3D');
  });
});
