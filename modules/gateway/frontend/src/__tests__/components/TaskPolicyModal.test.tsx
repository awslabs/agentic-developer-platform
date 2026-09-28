import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, expect, it, vi } from 'vitest';
import { TaskPolicyModal } from '@/components/org/TaskPolicyModal';
import { AgentTaskBudget } from '@/components/org/AgentTaskBudget';
import * as api from '@/services/taskPolicies';
vi.mock('@/services/taskPolicies', () => ({ identityPolicy: vi.fn(), saveTaskPolicy: vi.fn(), previewReservation: vi.fn(), getTaskPolicyView: vi.fn() }));
const identity = { id: 'client', source: 'cognito' as const, name: 'Cyber worker', status: 'active' };
const policy: api.TaskPolicy = { tenant_id: 'org', canonical_principal_id: 'canonical', version: 2, status: 'active', allowed_personas: ['agent-task-cyber'], allowed_tools: ['cyber.browser_start'], task_scopes: ['submit', 'read'], model_policy_version: '2', limits: { max_duration_minutes: 60, max_turns: 8, max_output_tokens_per_turn: 4096, max_usd_per_task: '1' } };
const view: api.TaskPolicyView = { tenant_id: 'org', canonical_principal_id: 'canonical', policy, platform_limits: { ...policy.limits, max_usd_per_task: '20' }, platform_limit_setting: 'ADP_TASK_MAX_USD_PER_TASK', persona_tools: { 'agent-task-cyber': ['cyber.browser_start', 'cyber.browser_close'] }, models: [{ persona: 'agent-task-cyber', model: 'opus' }] };
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.identityPolicy).mockResolvedValue(structuredClone(view));
  vi.mocked(api.getTaskPolicyView).mockResolvedValue(structuredClone(view));
  vi.mocked(api.previewReservation).mockResolvedValue({ status: 'available', reservation_usd: '12.122880' });
  vi.mocked(api.saveTaskPolicy).mockResolvedValue({ ...policy, version: 3 });
});
it('shows the reservation blocker and saves the canonical version without changing grants', async () => {
  render(<TaskPolicyModal orgId="org" identity={identity} onClose={vi.fn()} />);
  expect(await screen.findByText(/reservation exceeds the Task budget/)).toBeInTheDocument();
  const input = screen.getByLabelText('Maximum spend per Task (USD)');
  await userEvent.clear(input); await userEvent.type(input, '15');
  expect(screen.queryByText(/reservation exceeds the Task budget/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Save Task policy' }));
  expect(api.saveTaskPolicy).toHaveBeenCalledWith('org', 'canonical', expect.objectContaining({ version: 2, limits: { ...policy.limits, max_usd_per_task: '15' }, allowed_tools: ['cyber.browser_start'], model_policy_version: '2' }));
  expect(await screen.findByText(/Task policy saved/)).toBeInTheDocument();
});
it('requires reload on concurrent edits and uncertain saves', async () => {
  vi.mocked(api.saveTaskPolicy).mockRejectedValue({ status: 409 });
  render(<TaskPolicyModal orgId="org" identity={identity} onClose={vi.fn()} />);
  await screen.findByLabelText('Maximum spend per Task (USD)');
  await userEvent.click(screen.getByRole('button', { name: 'Save Task policy' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('changed while you were editing');
  expect(screen.getByRole('button', { name: 'Save Task policy' })).toBeDisabled();
  await userEvent.click(screen.getByRole('button', { name: 'Reload stored policy' }));
  expect(await screen.findByRole('button', { name: 'Save Task policy' })).toBeEnabled();
});
it('never treats an unavailable preview as compatible', async () => {
  vi.mocked(api.previewReservation).mockRejectedValue(new Error('offline'));
  render(<AgentTaskBudget principal="canonical" persona="agent-task-cyber" model="opus" />);
  expect(await screen.findByText(/Reservation preview unavailable/)).toBeInTheDocument();
  expect(api.getTaskPolicyView).toHaveBeenCalledWith('canonical');
});
it('keeps personal and service policy scopes distinct', async () => {
  const rendered = render(<AgentTaskBudget persona="agent-task-cyber" model="opus" />);
  await screen.findByText(/This is your own Task policy/);
  rendered.rerender(<AgentTaskBudget principal="canonical" persona="agent-task-cyber" model="opus" />);
  await screen.findByText(/Administrators can edit this service account/);
  expect(api.getTaskPolicyView).toHaveBeenNthCalledWith(1, undefined);
  expect(api.getTaskPolicyView).toHaveBeenNthCalledWith(2, 'canonical');
});
it('discards a load after switching organizations', async () => {
  let resolve!: (value: api.TaskPolicyView) => void;
  vi.mocked(api.identityPolicy).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const rendered = render(<TaskPolicyModal key="old" orgId="old" identity={identity} onClose={vi.fn()} />);
  vi.mocked(api.identityPolicy).mockRejectedValue({ status: 403 });
  rendered.rerender(<TaskPolicyModal key="new" orgId="new" identity={identity} onClose={vi.fn()} />);
  await act(async () => resolve(view));
  expect(screen.queryByLabelText('Maximum spend per Task (USD)')).not.toBeInTheDocument();
});
it('requires explicit renewal of changed model authorization', async () => {
  vi.mocked(api.identityPolicy).mockResolvedValue({ ...view, models: [{ persona: 'agent-task-cyber', model: 'opus', revision: '3' }] });
  render(<TaskPolicyModal orgId="org" identity={identity} onClose={vi.fn()} />);
  expect(await screen.findByText(/This model selection is not authorized/)).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText(/Authorize the displayed model selections/));
  await userEvent.click(screen.getByRole('button', { name: 'Save Task policy' }));
  expect(api.saveTaskPolicy).toHaveBeenCalledWith('org', 'canonical', expect.objectContaining({ model_policy_versions: { 'agent-task-cyber': '3' } }));
});
