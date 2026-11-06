import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ProductProfileSections from '@/pages/products/ProductProfileSections';
import { getSharedProductProfile, updateSharedProductProfile, type SharedProductProfile } from '@/services/products';

vi.mock('@/services/products', async original => ({ ...await original<typeof import('@/services/products')>(), getSharedProductProfile: vi.fn(), updateSharedProductProfile: vi.fn() }));
vi.mock('@/pages/products/ProductSources', () => ({ default: ({ projectId }: { projectId: string }) => <p>Connections for {projectId}</p> }));
vi.mock('@/pages/products/ProductGitlabHome', () => ({ default: () => null }));
const fixture: SharedProductProfile = {
  product_id: 'product', revision: 7, base_project_id: 'base', can_edit: true, not_applicable: [], knowledge_count: 2,
  resources: [
    { id: 'repo', kind: 'repository', provider: 'github', label: 'example/product', namespace: 'org', native_id: 'repo', purpose: 'product_code', default: false, bindings: [{ id: 'binding', project_id: 'base', status: 'degraded', generation: 1, revision: 2, relationship_revision: 1 }] },
    { id: 'account', kind: 'environment', provider: 'aws', label: 'Shared account', namespace: 'account', native_id: 'region', bindings: [] },
  ],
  environments: [{ id: 'dev', name: 'Dev', environment_class: 'development', resource_id: 'account', deployment_target: 'cluster/dev' }],
  completion: { completed: 4, applicable: 5, percentage: 80, sections: [{ id: 'repositories', status: 'complete' }, { id: 'backlog', status: 'missing' }, { id: 'knowledge', status: 'complete' }] },
};
const props = { productId: 'product', projectId: 'launch', revision: 7, projects: { base: 'Base', launch: 'Launch' }, onSaved: vi.fn(), children: <section id="product-details">Product details</section> };
const mount = () => render(<MemoryRouter><ProductProfileSections {...props} /></MemoryRouter>);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getSharedProductProfile).mockResolvedValue(structuredClone(fixture));
  vi.mocked(updateSharedProductProfile).mockResolvedValue({ revision: 8 } as never);
});

describe('Shared Product Profile', () => {
  it('uses product scope, retains completion with degraded health, and opens shared Knowledge and Base connections', async () => {
    mount();
    expect(await screen.findByText('4 of 5 sections complete')).toBeVisible();
    expect(screen.getByRole('progressbar', { name: 'Profile completion' })).toHaveValue(80);
    expect(screen.getByRole('link', { name: 'Backlog · Add details' })).toHaveAttribute('href', '#product-backlog');
    expect(screen.getByText('Connection: needs verification')).toBeVisible();
    expect(screen.getByRole('link', { name: 'Open Knowledge' })).toHaveAttribute('href', '/products/product/projects/launch/knowledge');
    expect(getSharedProductProfile).toHaveBeenCalledWith('product');
    await userEvent.click(screen.getByRole('button', { name: 'Manage connections' }));
    expect(screen.getByText('Connections for base')).toBeVisible();
  });

  it('saves repository purpose and default in one product-level write', async () => {
    mount();
    const repositories = await screen.findByRole('region', { name: 'Code repositories' });
    await screen.findByText('example/product');
    await userEvent.click(within(repositories).getByRole('button', { name: 'Edit' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'Default code repository' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save repository' }));
    await waitFor(() => expect(updateSharedProductProfile).toHaveBeenCalledWith('product', 7, { repositories: { repo: { purpose: 'product_code', label: 'example/product' } }, default_repository: 'repo' }, expect.any(String)));
  });

  it('adds a production environment on the existing Dev account with a separate target', async () => {
    mount(); await screen.findByText('4 of 5 sections complete');
    await userEvent.click(screen.getByRole('button', { name: 'Add environment' }));
    await userEvent.type(screen.getByRole('textbox', { name: 'Name' }), 'Prod');
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Type' }), 'production');
    await userEvent.type(screen.getByRole('textbox', { name: 'Deployment target (optional)' }), 'cluster/prod');
    await userEvent.click(screen.getByRole('button', { name: 'Save environment' }));
    await waitFor(() => expect(updateSharedProductProfile).toHaveBeenCalled());
    const patch = vi.mocked(updateSharedProductProfile).mock.calls[0][2];
    expect(Object.values(patch.environments!)).toEqual([{ name: 'Prod', environment_class: 'production', resource_id: 'account', deployment_target: 'cluster/prod' }]);
  });

  it('excludes optional sections without discarding saved resources', async () => {
    mount(); await screen.findByText('4 of 5 sections complete');
    await userEvent.click(within(screen.getByRole('region', { name: 'Backlog' })).getByRole('button', { name: 'Mark not applicable' }));
    expect(updateSharedProductProfile).toHaveBeenCalledWith('product', 7, { not_applicable: ['backlog'] }, expect.any(String));
  });

  it('retries an uncertain save with the original receipt and patch', async () => {
    vi.mocked(updateSharedProductProfile).mockRejectedValueOnce(new Error('response lost')).mockResolvedValueOnce({ revision: 8 } as never);
    mount(); await screen.findByText('4 of 5 sections complete');
    await userEvent.click(within(screen.getByRole('region', { name: 'Backlog' })).getByRole('button', { name: 'Mark not applicable' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Check save status' }));
    await waitFor(() => expect(updateSharedProductProfile).toHaveBeenCalledTimes(2));
    expect(vi.mocked(updateSharedProductProfile).mock.calls[1]).toEqual(vi.mocked(updateSharedProductProfile).mock.calls[0]);
  });
});
