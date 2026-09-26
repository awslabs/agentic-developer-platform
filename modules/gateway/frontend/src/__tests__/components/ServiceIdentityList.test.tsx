import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ServiceIdentityList } from '@/components/org/ServiceIdentityList';
import { loadIdentityHierarchy, loadServiceIdentityPage } from '@/services/organizationServiceIdentities';

vi.mock('@/services/organizationServiceIdentities', async importOriginal => ({
  ...await importOriginal<typeof import('@/services/organizationServiceIdentities')>(),
  loadIdentityHierarchy: vi.fn(), loadServiceIdentityPage: vi.fn(),
}));

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(loadIdentityHierarchy).mockResolvedValue({ departments: { dept: 'Engineering' }, teams: { team: { name: 'Cyber', departmentId: 'dept' } } });
  vi.mocked(loadServiceIdentityPage).mockImplementation(async (_org, source) => ({ items: [{ id: source, source, name: `${source} worker`, departmentId: source === 'iam' ? undefined : 'dept', teamId: 'team', status: 'active' }], next: null }));
});

describe('Organization service accounts', () => {
  it('shows all account kinds with explicit markers and named hierarchy', async () => {
    render(<ServiceIdentityList orgId="sophos-labs" />);
    await screen.findByText('cognito worker');
    expect(await screen.findAllByText('Engineering')).toHaveLength(3);
    expect(screen.getAllByText('Service account')).toHaveLength(3);
    expect(screen.getAllByText('Cyber')).toHaveLength(3);
    for (const source of ['cognito', 'iam', 'legacy']) {
      expect(loadServiceIdentityPage).toHaveBeenCalledWith('sophos-labs', source, '1');
    }
    await userEvent.type(screen.getByRole('textbox', { name: 'Search service accounts' }), 'cognito');
    expect(screen.getByText('cognito worker')).toBeInTheDocument();
    expect(screen.queryByText('iam worker')).not.toBeInTheDocument();
  });

  it('shows missing assignments explicitly without hiding the account', async () => {
    vi.mocked(loadServiceIdentityPage).mockImplementation(async (_org, source) => ({ items: source === 'cognito' ? [{ id: 'unassigned', source, name: 'tao', status: 'disabled' }] : [], next: null }));
    render(<ServiceIdentityList orgId="sophos-labs" />);
    const row = (await screen.findByText('tao')).closest('tr')!;
    expect(within(row).getAllByText('Unassigned')).toHaveLength(2);
    expect(within(row).getByText('disabled')).toBeInTheDocument();
  });

  it('does not infer a missing Cognito department from its team', async () => {
    vi.mocked(loadServiceIdentityPage).mockImplementation(async (_org, source) => ({ items: source === 'cognito' ? [{ id: 'partial', source, name: 'partial assignment', teamId: 'team', status: 'active' }] : [], next: null }));
    render(<ServiceIdentityList orgId="org" />);
    const row = (await screen.findByText('partial assignment')).closest('tr')!;
    expect(within(row).getByText('Unassigned')).toBeInTheDocument();
    expect(await within(row).findByText('Cyber')).toBeInTheDocument();
  });

  it('keeps assignment IDs visible when hierarchy names cannot be loaded', async () => {
    vi.mocked(loadIdentityHierarchy).mockRejectedValue(new Error('Unavailable'));
    render(<ServiceIdentityList orgId="org" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Available assignment IDs are shown');
    await screen.findByText('cognito worker');
    expect(screen.getAllByText('team')).toHaveLength(3);
  });

  it('keeps successful sources visible when one fails and allows retry', async () => {
    vi.mocked(loadServiceIdentityPage).mockImplementation(async (_org, source, cursor) => {
      if (source === 'iam') throw new Error('Forbidden');
      return { items: [{ id: `${source}-${cursor}`, source, name: `${source} ${cursor}`, status: 'active' }], next: source === 'cognito' && cursor === '1' ? '2' : null };
    });
    render(<ServiceIdentityList orgId="org" />);
    expect(await screen.findByRole('alert')).toHaveTextContent('IAM agent accounts could not be loaded');
    expect(screen.getByText('cognito 1')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry / load more accounts' }));
    await screen.findByText('cognito 2');
    expect(loadServiceIdentityPage).toHaveBeenCalledWith('org', 'cognito', '2');
    expect(vi.mocked(loadServiceIdentityPage).mock.calls.filter(([, source]) => source === 'legacy')).toHaveLength(1);
  });

  it('never shows the previous organization after switching while requests are pending', async () => {
    let resolveOld!: (value: Awaited<ReturnType<typeof loadServiceIdentityPage>>) => void;
    vi.mocked(loadServiceIdentityPage).mockImplementation((org, source) => org === 'old' && source === 'cognito'
      ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ items: [{ id: source, source, name: `${org} ${source}`, status: 'active' }], next: null }));
    const view = render(<ServiceIdentityList key="old" orgId="old" />);
    await waitFor(() => expect(loadServiceIdentityPage).toHaveBeenCalledWith('old', 'cognito', '1'));
    view.rerender(<ServiceIdentityList key="new" orgId="new" />);
    await screen.findByText('new cognito');
    resolveOld({ items: [{ id: 'old', source: 'cognito', name: 'old identity', status: 'active' }], next: null });
    await waitFor(() => expect(screen.queryByText('old identity')).not.toBeInTheDocument());
    expect(screen.getByText('new cognito')).toBeInTheDocument();
  });
});

it('exposes management actions only to organization managers and supported accounts', async () => {
  const view = render(<ServiceIdentityList orgId="org" />);
  await screen.findByText('cognito worker');
  expect(screen.queryByRole('button', { name: 'Add service account' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Edit assignment' })).not.toBeInTheDocument();
  view.rerender(<ServiceIdentityList orgId="org" canManage />);
  expect(screen.getByRole('button', { name: 'Add service account' })).toBeInTheDocument();
  expect(screen.getAllByRole('button', { name: 'Edit assignment' })).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: 'Edit assignment' }));
  expect(screen.getByRole('dialog')).toHaveTextContent('cognito worker');
});
