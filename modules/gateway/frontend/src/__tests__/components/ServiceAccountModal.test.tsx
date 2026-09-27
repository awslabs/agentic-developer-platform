import { beforeEach, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ServiceAccountModal } from '@/components/org/ServiceAccountModal';
import { createAgent, updateAgent, getAgentCredentials } from '@/services/agents';
import { loadIdentityHierarchy } from '@/services/organizationServiceIdentities';
vi.mock('@/services/agents', () => ({ createAgent: vi.fn(), updateAgent: vi.fn(), getAgentCredentials: vi.fn() }));
vi.mock('@/services/organizationServiceIdentities', () => ({ loadIdentityHierarchy: vi.fn() }));
const agent = { client_id: 'client', name: 'worker', org_id: 'selected', department_id: 'd1', team_id: 't1', status: 'active' as const, scopes: ['bedrockgw/invoke'], created_at: '' };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(loadIdentityHierarchy).mockResolvedValue({ departments: { d1: 'Engineering', d2: 'Operations' }, teams: { t1: { name: 'Cyber', departmentId: 'd1' }, t2: { name: 'Support', departmentId: 'd2' } } });
  vi.mocked(createAgent).mockResolvedValue(agent);
  vi.mocked(updateAgent).mockResolvedValue(agent);
  vi.mocked(getAgentCredentials).mockResolvedValue({ client_id: 'client', client_secret: 'test-secret', token_endpoint: 'https://example.com/token', scopes: ['bedrockgw/invoke'], example_curl: '' });
});
async function assignment() {
  await screen.findByRole('option', { name: 'Engineering' });
  await userEvent.selectOptions(screen.getByLabelText(/Department/), 'd1');
  await userEvent.selectOptions(screen.getByLabelText(/Team/), 't1');
}
it('requires a matching hierarchy and creates under the selected org with invoke-only scope', async () => {
  const saved = vi.fn();
  render(<ServiceAccountModal orgId="selected" onSaved={saved} onClose={vi.fn()} />);
  expect(screen.getByRole('button', { name: 'Create service account' })).toBeDisabled();
  await userEvent.type(screen.getByLabelText(/Name/), 'worker'); await assignment();
  expect(screen.queryByRole('option', { name: 'Support' })).not.toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText(/Department/), 'd2');
  expect(screen.getByLabelText(/Team/)).toHaveValue('');
  expect(screen.getByRole('button', { name: 'Create service account' })).toBeDisabled();
  await assignment();
  await userEvent.click(screen.getByRole('button', { name: 'Create service account' }));
  expect(await screen.findByLabelText('Client secret')).toHaveValue('test-secret');
  expect(createAgent).toHaveBeenCalledWith({ org_id: 'selected', name: 'worker', description: '', department_id: 'd1', team_id: 't1', scopes: ['bedrockgw/invoke'] });
  expect(getAgentCredentials).toHaveBeenCalledWith('client', 'selected');
  expect(saved).toHaveBeenCalledWith(expect.objectContaining({ id: 'client', departmentId: 'd1', teamId: 't1' }));
});
it('retries credentials without creating a second account', async () => {
  vi.mocked(getAgentCredentials).mockRejectedValueOnce(new Error('offline'));
  render(<ServiceAccountModal orgId="selected" onSaved={vi.fn()} onClose={vi.fn()} />);
  await userEvent.type(screen.getByLabelText(/Name/), 'worker'); await assignment();
  await userEvent.click(screen.getByRole('button', { name: 'Create service account' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('account was created');
  await userEvent.click(screen.getByRole('button', { name: 'Retry loading credentials' }));
  expect(await screen.findByLabelText('Client secret')).toHaveValue('test-secret');
  expect(createAgent).toHaveBeenCalledTimes(1);
});
it('updates only assignment fields in the selected org', async () => {
  const close = vi.fn();
  render(<ServiceAccountModal orgId="selected" identity={{ id: 'client', source: 'cognito', name: 'tao', status: 'active' }} onSaved={vi.fn()} onClose={close} />);
  await assignment(); await userEvent.click(screen.getByRole('button', { name: 'Save assignment' }));
  expect(updateAgent).toHaveBeenCalledWith('client', { department_id: 'd1', team_id: 't1' }, 'selected');
  expect(close).toHaveBeenCalled(); expect(getAgentCredentials).not.toHaveBeenCalled();
});
it('blocks saving when hierarchy loading fails and supports retry', async () => {
  vi.mocked(loadIdentityHierarchy).mockRejectedValueOnce(new Error('offline'));
  render(<ServiceAccountModal orgId="selected" onSaved={vi.fn()} onClose={vi.fn()} />);
  await screen.findByRole('alert');
  expect(screen.getByRole('button', { name: 'Create service account' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Retry hierarchy' }));
  await screen.findByRole('option', { name: 'Engineering' });
});
it('discards a completed creation after switching organizations', async () => {
  let resolve!: (value: typeof agent) => void;
  vi.mocked(createAgent).mockImplementation(() => new Promise(r => { resolve = r; }));
  const saved = vi.fn();
  const view = render(<ServiceAccountModal key="old" orgId="old" onSaved={saved} onClose={vi.fn()} />);
  await userEvent.type(screen.getByLabelText(/Name/), 'worker'); await assignment();
  await userEvent.click(screen.getByRole('button', { name: 'Create service account' }));
  view.rerender(<ServiceAccountModal key="new" orgId="new" onSaved={saved} onClose={vi.fn()} />);
  await act(async () => { resolve(agent); });
  expect(screen.getByLabelText('Organization')).toHaveValue('new');
  expect(saved).not.toHaveBeenCalled(); expect(getAgentCredentials).not.toHaveBeenCalled();
});
it('prevents duplicate creation after an uncertain response and requests roster reconciliation', async () => {
  vi.mocked(createAgent).mockRejectedValue(new Error('connection lost'));
  const close = vi.fn();
  render(<ServiceAccountModal orgId="selected" onSaved={vi.fn()} onClose={close} />);
  await userEvent.type(screen.getByLabelText(/Name/), 'worker'); await assignment();
  await userEvent.click(screen.getByRole('button', { name: 'Create service account' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Creation could not be confirmed');
  expect(screen.getByRole('button', { name: 'Create service account' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(close).toHaveBeenCalledWith(true);
  expect(createAgent).toHaveBeenCalledTimes(1);
});
