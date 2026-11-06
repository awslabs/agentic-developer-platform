import { beforeEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import DevOpsKnowledgeAbandon from '@/pages/products/services/DevOpsKnowledgeAbandon';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { request: vi.fn(), requestWithEtag: vi.fn() } }));
vi.mock('@/services/products', () => ({ productReceipt: vi.fn(() => 'saved-abandonment-key-0001') }));
const selection = { tenant_id: 'tenant', product_id: 'product', project_id: 'project', configuration_id: 'configuration', binding_id: 'binding', configuration_revision: 4, binding_generation: 3 };
const acknowledgement = { kind: 'abandoned', actor_id: 'current-owner', reason: 'The original source expired before recovery.', abandoned_at: '2026-10-31T12:00:00Z', event_hash: 'receipt-hash', native_effects_may_remain: true, native_cleanup_performed: false, native_ownership_released: false, native_authority_granted: false, native_call_performed: false, release_check_completed: false, response: { operation_id: 'original', state: 'abandoned' } };
let history: { operation_id: string; revision: number; state: string; selection: typeof selection; items: typeof acknowledgement[] };
const onAccessDenied = vi.fn();
const onChanged = vi.fn();
const path = '/v1/products/product/projects/project/services/devops/binding-operations/original';
beforeEach(() => {
  vi.resetAllMocks();
  history = { operation_id: 'original', revision: 5, state: 'outcome_unknown', selection, items: [] };
  vi.mocked(apiClient.requestWithEtag).mockImplementation(async () => ({ value: history, etag: `"${history.revision}"` }));
  vi.mocked(apiClient.request).mockImplementation(async () => { history = { ...history, revision: 6, state: 'abandoned', items: [acknowledgement] }; return {}; });
});
function mount() { return render(<DevOpsKnowledgeAbandon selection={selection} operationId="original" onAccessDenied={onAccessDenied} onChanged={onChanged} />); }
async function read() { await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Read abandonment status' })); }); }
function declaration() {
  fireEvent.change(screen.getByLabelText('Reason for abandoning this publication'), { target: { value: acknowledgement.reason } });
  fireEvent.click(screen.getByRole('checkbox'));
}
async function submit() { await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })); }); }

it('requires current readback, a genuine reason and explicit effects acknowledgement', async () => {
  mount();
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled();
  await read();
  fireEvent.change(screen.getByLabelText('Reason for abandoning this publication'), { target: { value: acknowledgement.reason } });
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled();
  fireEvent.click(screen.getByRole('checkbox')); await submit();
  expect(apiClient.request).toHaveBeenCalledOnce();
  expect(apiClient.request).toHaveBeenCalledWith(`${path}/abandon`, { method: 'POST', headers: { 'If-Match': '"5"', 'Idempotency-Key': 'saved-abandonment-key-0001' }, body: { reason: acknowledgement.reason, native_effects_may_remain: true } });
  expect(apiClient.requestWithEtag).toHaveBeenCalledTimes(2);
  expect(onChanged).toHaveBeenCalledOnce();
  expect(screen.getByRole('region', { name: 'Retained abandonment acknowledgement' })).toHaveTextContent(acknowledgement.reason);
  expect(screen.getByRole('region', { name: 'Retained abandonment acknowledgement' })).toHaveTextContent('No native cleanup was performed.');
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled();
});
it('permits historical config revisions and generations without source or native consent', async () => {
  history.selection = { ...selection, configuration_revision: 1, binding_generation: 1 };
  mount(); await read(); declaration(); await submit(); expect(apiClient.request).toHaveBeenCalledOnce();
});
it('reads before uncertain retry and retains the exact original request', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 503 });
  mount(); await read(); declaration(); await submit();
  expect(apiClient.requestWithEtag).toHaveBeenCalledTimes(2);
  const original = vi.mocked(apiClient.request).mock.calls[0];
  expect(screen.getByLabelText('Reason for abandoning this publication')).toBeDisabled();
  const retry = screen.getByRole('button', { name: 'Retry saved abandonment acknowledgement' });
  expect(retry).not.toBeDisabled();
  await act(async () => { fireEvent.click(retry); });
  expect(vi.mocked(apiClient.request).mock.calls[1]).toEqual(original);
  expect(screen.queryByRole('button', { name: 'Retry saved abandonment acknowledgement' })).not.toBeInTheDocument();
});
it('blocks uncertain retry until a failed status read is repaired', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 503 });
  mount(); await read(); declaration();
  vi.mocked(apiClient.requestWithEtag).mockRejectedValueOnce({ status: 503 }); await submit();
  expect(screen.getByRole('button', { name: 'Retry saved abandonment acknowledgement' })).toBeDisabled();
  await read(); expect(screen.getByRole('button', { name: 'Retry saved abandonment acknowledgement' })).not.toBeDisabled();
});
it('does not retry when lost-response readback already shows abandonment', async () => {
  vi.mocked(apiClient.request).mockImplementationOnce(async () => { history = { ...history, state: 'abandoned', revision: 6, items: [acknowledgement] }; throw { status: 503 }; });
  mount(); await read(); declaration(); await submit();
  expect(screen.getByRole('region', { name: 'Retained abandonment acknowledgement' })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry saved abandonment acknowledgement' })).not.toBeInTheDocument();
  expect(apiClient.request).toHaveBeenCalledOnce();
});
it.each(['tenant_id', 'product_id', 'project_id', 'configuration_id', 'binding_id'] as const)('refuses mismatched %s read', async field => {
  history.selection = { ...selection, [field]: 'another' }; mount(); await read();
  expect(onAccessDenied).toHaveBeenCalledOnce();
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled(); expect(apiClient.request).not.toHaveBeenCalled();
});
it.each(['abandoned', 'cancelled', 'succeeded'])('keeps terminal %s operation read-only', async state => {
  history.state = state; mount(); await read();
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled(); expect(apiClient.request).not.toHaveBeenCalled();
});
it('clears inputs and retained data after human owner refusal', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 403, detail: { next_action: 'A current human owner is required.' } });
  mount(); await read(); declaration(); await submit();
  expect(onAccessDenied).toHaveBeenCalledOnce(); expect(screen.getByRole('alert')).toHaveTextContent('A current human owner is required.');
  expect(screen.getByLabelText('Reason for abandoning this publication')).toHaveValue(''); expect(screen.getByRole('checkbox')).not.toBeChecked();
});
it('shows worker guidance and requires fresh read and acknowledgement after a conflict', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 409, detail: { next_action: 'Wait for the current worker lease to end, then read this operation again.' } });
  mount(); await read(); declaration(); await submit();
  expect(screen.getByRole('alert')).toHaveTextContent('Wait for the current worker lease');
  expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled(); await read(); expect(screen.getByRole('checkbox')).not.toBeChecked();
});
it('requires renewed acknowledgement after stale operation revision', async () => {
  vi.mocked(apiClient.request).mockRejectedValueOnce({ status: 412 }); mount(); await read(); declaration(); await submit();
  expect(screen.getByRole('alert')).toHaveTextContent('operation changed'); expect(screen.getByRole('button', { name: 'Acknowledge abandonment without cleanup' })).toBeDisabled();
});
it('ignores old read response after switching operation identity', async () => {
  const view = mount(); let resolve!: (value: { value: typeof history; etag: string }) => void;
  vi.mocked(apiClient.requestWithEtag).mockReturnValueOnce(new Promise(done => { resolve = done; }));
  fireEvent.click(screen.getByRole('button', { name: 'Read abandonment status' }));
  view.rerender(<DevOpsKnowledgeAbandon selection={selection} operationId="another" onAccessDenied={onAccessDenied} />);
  await act(async () => { resolve({ value: history, etag: '"5"' }); });
  await waitFor(() => expect(screen.queryByText(/Original operation:/)).not.toBeInTheDocument()); expect(apiClient.request).not.toHaveBeenCalled();
});

it('does not claim confirmed abandonment from a missing retained acknowledgement', async () => {
  vi.mocked(apiClient.request).mockImplementationOnce(async () => { history = { ...history, state: 'abandoned', revision: 6, items: [] }; return {}; });
  mount(); await read(); declaration(); await submit();
  expect(screen.getByRole('alert')).toHaveTextContent('acknowledgement is missing or inconsistent');
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});
