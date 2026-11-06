import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ProductPeople from '@/pages/products/ProductPeople';
import { getProductPeople, updateProductPeople, type ProductPeople as People } from '@/services/products';
import { addGitHubOrgMember, addTeamMember, getOrgTeams, getOrgUsers } from '@/services/admin';

vi.mock('@/services/products', async (original) => ({ ...await original<typeof import('@/services/products')>(), getProductPeople: vi.fn(), updateProductPeople: vi.fn() }));
vi.mock('@/services/admin', () => ({ addGitHubOrgMember: vi.fn(), addTeamMember: vi.fn(), getOrgTeams: vi.fn(), getOrgUsers: vi.fn() }));
const initial: People = {
  product_id: 'product', revision: 3, owner: { id: 'owner', name: 'Maya', github_username: 'maya' },
  team: { id: 'team', name: 'Platform' }, members: [{ id: 'owner', name: 'Maya', github_username: 'maya' }], next_offset: null,
  is_owner: true, permissions: { manage_team: true, manage_people: true, enroll_github: true },
};
const onPeople = vi.fn(), onSaved = vi.fn();
function mount() { return render(<MemoryRouter><ProductPeople productId="product" projectId="project" orgId="org" userId="actor" revision={3} onPeople={onPeople} onSaved={onSaved} /></MemoryRouter>); }
async function edit() { await userEvent.click(await screen.findByRole('button', { name: 'Manage people' })); await screen.findByRole('option', { name: 'Design' }); }

describe('Product people and access', () => {
  beforeEach(() => {
    vi.resetAllMocks(); sessionStorage.clear();
    vi.mocked(getProductPeople).mockResolvedValue(initial);
    vi.mocked(getOrgTeams).mockResolvedValue({ items: [{ id: 'team', name: 'Platform' }, { id: 'design', name: 'Design' }], hasMore: false } as Awaited<ReturnType<typeof getOrgTeams>>);
    vi.mocked(getOrgUsers).mockResolvedValue({ items: [{ id: 'owner', name: 'Maya', githubUsername: 'maya' }, { id: 'alex', name: 'Alex', githubUsername: null }], hasMore: false } as Awaited<ReturnType<typeof getOrgUsers>>);
  });

  it('displays people and shares one chosen team across delivery scopes', async () => {
    mount();
    expect(await screen.findByText('Platform')).toBeInTheDocument();
    expect(screen.getAllByText('Maya (@maya)')).toHaveLength(2);
    await edit();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Product team' }), 'design');
    vi.mocked(getProductPeople).mockResolvedValue({ ...initial, revision: 4, team: { id: 'design', name: 'Design' }, members: [] });
    await userEvent.click(screen.getByRole('button', { name: 'Save people' }));
    await waitFor(() => expect(updateProductPeople).toHaveBeenCalledWith('product', 'project', 3, { team_id: 'design' }, expect.any(String)));
    expect(await screen.findByText('Product people updated.')).toBeInTheDocument();
    expect(screen.getByText('Design')).toBeInTheDocument();
    expect(onSaved).toHaveBeenCalledOnce();
  });

  it('reuses ADP membership and GitHub enrollment without changing the primary team', async () => {
    mount(); await edit();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Organization member' }), 'alex');
    await userEvent.click(screen.getByRole('button', { name: 'Add to team' }));
    await waitFor(() => expect(addTeamMember).toHaveBeenCalledWith('org', 'team', { userId: 'alex', role: 'member', isPrimary: false }));
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Add using' }), 'github');
    await userEvent.type(screen.getByRole('textbox', { name: 'GitHub username' }), '@octocat');
    await userEvent.click(screen.getByRole('button', { name: 'Add to team' }));
    await waitFor(() => expect(addGitHubOrgMember).toHaveBeenCalledWith('org', { github_username: 'octocat', team_id: 'team', role: 'member' }));
    expect(screen.getByText(/other products shared with this team/)).toBeInTheDocument();
  });

  it('does not show management for members or GitHub enrollment for an organization administrator', async () => {
    vi.mocked(getProductPeople).mockResolvedValue({ ...initial, permissions: { manage_team: false, manage_people: false, enroll_github: false } });
    const view = mount();
    await screen.findByText('Platform');
    expect(screen.queryByRole('button', { name: 'Manage people' })).not.toBeInTheDocument();
    expect(getOrgUsers).not.toHaveBeenCalled();
    view.unmount();
    vi.mocked(getProductPeople).mockResolvedValue({ ...initial, permissions: { manage_team: true, manage_people: true, enroll_github: false } });
    mount(); await edit();
    expect(within(screen.getByRole('combobox', { name: 'Add using' })).queryByRole('option', { name: 'GitHub username' })).not.toBeInTheDocument();
  });

  it('retains the original receipt through an uncertain save and remount', async () => {
    vi.mocked(updateProductPeople).mockRejectedValueOnce(new TypeError('Response lost')).mockResolvedValue({} as never);
    const view = mount(); await edit();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Product team' }), 'design');
    await userEvent.click(screen.getByRole('button', { name: 'Save people' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not confirm the save');
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Save people', exact: true })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Product team' })).toBeDisabled();
    const call = vi.mocked(updateProductPeople).mock.calls[0];
    view.unmount(); mount();
    await userEvent.click(await screen.findByRole('button', { name: 'Check people save status' }));
    await waitFor(() => expect(updateProductPeople).toHaveBeenCalledTimes(2));
    expect(vi.mocked(updateProductPeople).mock.calls[1]).toEqual(call);
  });

  it('requires review after a concurrent change and hides people when access is revoked', async () => {
    vi.mocked(updateProductPeople).mockRejectedValue({ status: 412 });
    mount(); await edit();
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Product team' }), 'design');
    await userEvent.click(screen.getByRole('button', { name: 'Save people' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Product changed elsewhere');
    expect(screen.queryByRole('button', { name: 'Check people save status' })).not.toBeInTheDocument();
    vi.mocked(getProductPeople).mockRejectedValue({ status: 404 });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh people' }));
    await waitFor(() => expect(screen.queryByText('Platform')).not.toBeInTheDocument());
    expect(onPeople).toHaveBeenLastCalledWith(null);
  });
});
