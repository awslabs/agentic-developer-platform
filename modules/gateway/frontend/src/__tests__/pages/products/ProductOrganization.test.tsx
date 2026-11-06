import { beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ProductOrganization from '@/pages/products/ProductOrganization';
import { addPortfolioMembership, closePortfolioMembership, listPortfolioMemberships, listPortfolios, type PortfolioMembership, type ProductProfile } from '@/services/products';

vi.mock('@/services/products', () => ({
  addPortfolioMembership: vi.fn(), closePortfolioMembership: vi.fn(),
  listPortfolioMemberships: vi.fn(), listPortfolios: vi.fn(),
}));

const profile = { tenant_id: 'tenant-a', product_id: 'product-a', project_id: 'project-a', revision: 4,
  product: { name: 'Planner', purpose: 'Plan', intended_users: ['Maintainers'], owner_ref: { kind: 'user', id: 'actor-a' } },
  configuration: {},
} as ProductProfile;
const portfolio = { id: 'portfolio-a', revision: '2', data: { name: 'Commerce' } };
const relationship: PortfolioMembership = {
  relationship_id: 'edge-a', relationship_revision: '1',
  portfolio_ref: { tenant_id: 'tenant-a', project_id: 'project-a', source_connection_id: 'adp.catalog',
    record_type: 'portfolio', record_id: 'portfolio-a', record_revision: '2' },
  product_ref: { tenant_id: 'tenant-a', project_id: 'project-a', source_connection_id: 'adp.profile',
    record_type: 'product', record_id: 'product-a', record_revision: '4' },
  valid_from: '2026-10-21T00:00:00Z',
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(listPortfolios).mockResolvedValue({ items: [portfolio], can_edit: true });
  vi.mocked(listPortfolioMemberships).mockResolvedValue({ items: [] });
});

it('shows an ungrouped Product and independently reads back dated membership after adding it', async () => {
  const user = userEvent.setup();
  vi.mocked(listPortfolioMemberships).mockResolvedValueOnce({ items: [] }).mockResolvedValue({ items: [relationship] });
  vi.mocked(addPortfolioMembership).mockResolvedValue({ id: 'edge-a', revision: '1', change_id: 'change-a' });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText('No visible Portfolio memberships for this Product.')).toBeInTheDocument();
  await user.selectOptions(screen.getByRole('combobox', { name: 'Add to Portfolio' }), 'portfolio-a');
  fireEvent.change(screen.getByLabelText('Membership starts on'), { target: { value: '2026-10-21' } });
  await user.click(screen.getByRole('button', { name: 'Add membership' }));
  await waitFor(() => expect(addPortfolioMembership).toHaveBeenCalledWith(relationship.portfolio_ref,
    relationship.product_ref, '2026-10-21T00:00:00Z'));
  expect(await screen.findByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  expect(listPortfolioMemberships).toHaveBeenCalledTimes(3);
});

it('uses stable catalog IDs to show overlapping memberships without requiring a Portfolio', async () => {
  const user = userEvent.setup();
  const shared = { id: 'portfolio-b', revision: '3', data: { name: 'Shared services' } };
  const sharedRelationship: PortfolioMembership = { ...relationship, relationship_id: 'edge-b',
    portfolio_ref: { ...relationship.portfolio_ref, record_id: shared.id, record_revision: shared.revision } };
  let linked = false;
  vi.mocked(listPortfolios).mockResolvedValue({ items: [portfolio, shared], can_edit: true });
  vi.mocked(listPortfolioMemberships).mockImplementation(async (id, _cursor, effectiveAt) => ({
    items: linked && (!effectiveAt || effectiveAt === relationship.valid_from)
      ? [id === portfolio.id ? relationship : sharedRelationship] : [],
  }));
  vi.mocked(addPortfolioMembership).mockImplementation(async (portfolioRef, productRef) => {
    expect(portfolioRef).toEqual(sharedRelationship.portfolio_ref);
    expect(productRef).toEqual(sharedRelationship.product_ref);
    linked = true;
    return { id: sharedRelationship.relationship_id, revision: sharedRelationship.relationship_revision, change_id: 'change-b' };
  });
  render(<ProductOrganization productId={profile.product_id} projectId={profile.project_id} profile={profile} />);
  expect(await screen.findByText('No visible Portfolio memberships for this Product.')).toBeInTheDocument();
  expect(addPortfolioMembership).not.toHaveBeenCalled();
  await user.selectOptions(screen.getByRole('combobox', { name: 'Add to Portfolio' }), shared.id);
  fireEvent.change(screen.getByLabelText('Membership starts on'), { target: { value: '2026-10-21' } });
  await user.click(screen.getByRole('button', { name: 'Add membership' }));
  expect(await screen.findByText('Shared services: 2026-10-21 to ongoing')).toBeInTheDocument();
  expect(screen.getByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  expect(addPortfolioMembership).toHaveBeenCalledTimes(1);
  expect(listPortfolioMemberships).toHaveBeenCalledWith(shared.id, undefined, relationship.valid_from);
});

it('confirms a future membership at its effective date without treating it as current or retrying the write', async () => {
  const user = userEvent.setup();
  const startsAt = '2099-01-01T00:00:00Z';
  const scheduled = { ...relationship, valid_from: startsAt };
  let persisted = false;
  vi.mocked(addPortfolioMembership).mockImplementation(async () => {
    persisted = true;
    return { id: 'edge-a', revision: '1', change_id: 'change-a' };
  });
  vi.mocked(listPortfolioMemberships).mockImplementation(async (_portfolioId, cursor, effectiveAt) => {
    if (!persisted || effectiveAt !== startsAt) return { items: [] };
    return cursor ? { items: [scheduled] } : { items: [], next_cursor: 'scheduled-page' };
  });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Add to Portfolio' }), 'portfolio-a');
  fireEvent.change(screen.getByLabelText('Membership starts on'), { target: { value: '2099-01-01' } });
  await user.click(screen.getByRole('button', { name: 'Add membership' }));
  expect(await screen.findByRole('status')).toHaveTextContent('Membership confirmed from 2099-01-01. It is not in the current membership list.');
  expect(listPortfolioMemberships).toHaveBeenCalledWith('portfolio-a', undefined, startsAt);
  expect(listPortfolioMemberships).toHaveBeenCalledWith('portfolio-a', 'scheduled-page', startsAt);
  expect(screen.queryByText('Commerce: 2099-01-01 to ongoing')).not.toBeInTheDocument();
  expect(screen.queryByText(/No visible Portfolio memberships/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Portfolio details changed elsewhere/)).not.toBeInTheDocument();
  expect(screen.getByLabelText('Membership starts on')).toHaveValue('');
  expect(screen.getByRole('combobox', { name: 'Add to Portfolio' })).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Add membership' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: 'Add membership' }));
  expect(addPortfolioMembership).toHaveBeenCalledTimes(1);
});

it.each(['missing', 'stale', 'revoked', 'unavailable', 'cursor-conflict', 'revoked-current'])(
  'does not confirm a scheduled membership when its readback is %s', async (failure) => {
    const user = userEvent.setup();
    let confirmedRead = false;
    vi.mocked(addPortfolioMembership).mockResolvedValue({ id: 'edge-a', revision: '1', change_id: 'change-a' });
    vi.mocked(listPortfolioMemberships).mockImplementation(async (_portfolioId, _cursor, effectiveAt) => {
      if (!effectiveAt) {
        if (failure === 'revoked-current' && confirmedRead) throw { status: 403 };
        return { items: [] };
      }
      if (failure === 'revoked') throw { status: 403 };
      if (failure === 'unavailable') throw { status: 503 };
      if (failure === 'cursor-conflict') throw { status: 400 };
      confirmedRead = true;
      return { items: failure === 'missing' ? [] : [{ ...relationship,
        relationship_revision: failure === 'stale' ? '2' : '1', valid_from: effectiveAt }] };
    });
    render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Add to Portfolio' }), 'portfolio-a');
    fireEvent.change(screen.getByLabelText('Membership starts on'), { target: { value: '2099-01-01' } });
    await user.click(screen.getByRole('button', { name: 'Add membership' }));
    const message = failure.startsWith('revoked') ? 'Portfolio access changed'
      : failure === 'unavailable' ? 'Portfolio coverage unavailable' : 'Portfolio details changed elsewhere';
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.queryByText(/Membership confirmed/)).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Commerce' })).not.toBeInTheDocument();
    expect(addPortfolioMembership).toHaveBeenCalledTimes(1);
  });

it('shows multiple authorized dated memberships and closes one by its relationship revision', async () => {
  const user = userEvent.setup();
  const second = { id: 'portfolio-b', revision: '1', data: { name: 'Shared services' } };
  const other: PortfolioMembership = { ...relationship, relationship_id: 'edge-b',
    portfolio_ref: { ...relationship.portfolio_ref, record_id: 'portfolio-b', record_revision: '1' },
    valid_from: '2026-10-22T00:00:00Z' };
  vi.mocked(listPortfolios).mockResolvedValue({ items: [portfolio, second], can_edit: true });
  vi.mocked(listPortfolioMemberships).mockImplementation(async (id) => ({ items: id === 'portfolio-a' ? [relationship] : [other] }));
  vi.mocked(closePortfolioMembership).mockResolvedValue({ id: 'edge-a', revision: '2', change_id: 'change-b' });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText('Shared services: 2026-10-22 to ongoing')).toBeInTheDocument();
  expect(screen.getByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  fireEvent.change(screen.getAllByLabelText('End membership on')[0], { target: { value: '2026-10-25' } });
  await user.click(screen.getByRole('button', { name: 'End Commerce membership' }));
  await waitFor(() => expect(closePortfolioMembership).toHaveBeenCalledWith('portfolio-a', relationship, '2026-10-25T00:00:00Z'));
});

it('offers no writes for a member without catalog editing rights, even if that member owns the Product', async () => {
  vi.mocked(listPortfolios).mockResolvedValue({ items: [portfolio], can_edit: false });
  vi.mocked(listPortfolioMemberships).mockResolvedValue({ items: [relationship] });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  expect(screen.getByText('Portfolio membership is read-only with your current access.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Add membership' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /End Commerce/ })).not.toBeInTheDocument();
});

it('does not expose a partial list or membership count after a catalog lookup fails', async () => {
  vi.mocked(listPortfolioMemberships).mockRejectedValue({ status: 503 });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText(/Portfolio coverage unavailable/)).toBeInTheDocument();
  expect(screen.queryByText('Commerce')).not.toBeInTheDocument();
  expect(screen.queryByText(/No visible Portfolio memberships/)).not.toBeInTheDocument();
});

it('requires a fresh authorized read after a stale membership revision', async () => {
  const user = userEvent.setup();
  vi.mocked(listPortfolioMemberships).mockResolvedValue({ items: [relationship] });
  vi.mocked(closePortfolioMembership).mockRejectedValue({ status: 412, code: 'stale_revision' });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('End membership on'), { target: { value: '2026-10-25' } });
  await user.click(screen.getByRole('button', { name: 'End Commerce membership' }));
  expect(await screen.findByText(/Portfolio details changed elsewhere/)).toBeInTheDocument();
  expect(screen.queryByText('Commerce: 2026-10-21 to ongoing')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'End Commerce membership' })).not.toBeInTheDocument();
  vi.mocked(listPortfolioMemberships).mockResolvedValue({ items: [{ ...relationship, relationship_revision: '2' }] });
  await user.click(screen.getByRole('button', { name: 'Refresh current Portfolios' }));
  expect(await screen.findByText('Commerce: 2026-10-21 to ongoing')).toBeInTheDocument();
  expect(closePortfolioMembership).toHaveBeenCalledTimes(1);
});

it('hides all names and counts when access is revoked partway through a catalog page', async () => {
  vi.mocked(listPortfolios).mockResolvedValueOnce({ items: [portfolio], can_edit: true, next_cursor: 'next' })
    .mockRejectedValueOnce({ status: 403, code: 'permission_denied' });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  expect(await screen.findByText(/Portfolio access changed/)).toBeInTheDocument();
  expect(screen.queryByText(/Commerce/)).not.toBeInTheDocument();
  expect(screen.queryByText(/No visible Portfolio memberships/)).not.toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Add to Portfolio' })).not.toBeInTheDocument();
  expect(listPortfolios).toHaveBeenCalledWith('next');
});

it('removes cached Portfolio choices when access is revoked during an attempted write', async () => {
  const user = userEvent.setup();
  vi.mocked(addPortfolioMembership).mockRejectedValue({ status: 403, code: 'permission_denied' });
  render(<ProductOrganization productId="product-a" projectId="project-a" profile={profile} />);
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Add to Portfolio' }), 'portfolio-a');
  fireEvent.change(screen.getByLabelText('Membership starts on'), { target: { value: '2026-10-21' } });
  await user.click(screen.getByRole('button', { name: 'Add membership' }));
  expect(await screen.findByText(/Portfolio access changed/)).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: 'Commerce' })).not.toBeInTheDocument();
  expect(screen.queryByText(/No visible Portfolio memberships/)).not.toBeInTheDocument();
});
