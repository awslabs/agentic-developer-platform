import { beforeEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import DevOpsReleaseEvidence from '@/pages/products/services/DevOpsReleaseEvidence';
import { apiClient } from '@/services/api';

vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), post: vi.fn() } }));
const props = { productId: 'product', projectId: 'project', bindingId: 'binding', configurationId: 'configuration', revision: 1,
  runId: 'run', launchReceipt: 'issued', onSelect: vi.fn() };
const challenge = { service_id: 'devops', challenge_id: 'challenge', snapshot_sha256: 'a'.repeat(64), subject: 'run_submission',
  expires_at: '2100-01-01T00:00:00Z', observation_receipt: null,
  snapshot: { selection: { product_id: 'product', project_id: 'project', binding_id: 'binding', configuration_id: 'configuration', configuration_revision: 1 } } };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(apiClient.get).mockResolvedValue(challenge); vi.mocked(apiClient.post).mockResolvedValue({}); });
async function attach() {
  fireEvent.click(screen.getByText('Release qualification observation'));
  fireEvent.change(screen.getByLabelText('Delivery flow ID'), { target: { value: 'flow' } });
  fireEvent.change(screen.getByLabelText('Prepared observation ID'), { target: { value: 'challenge' } });
  fireEvent.change(screen.getByLabelText('Original UI run receipt'), { target: { value: 'original-ui-receipt-0001' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Attach prepared observation' })); });
}
it('requires an explicit human assertion after selecting the original scope', async () => {
  render(<DevOpsReleaseEvidence {...props} />); await attach();
  const record = await screen.findByRole('button', { name: 'Record that I submitted this run in ADP' });
  expect(apiClient.post).not.toHaveBeenCalled();
  await act(async () => { fireEvent.click(record); });
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/orchestration/flows/flow/l5-observations/challenge/observe', {
    snapshot_sha256: 'a'.repeat(64), observation: 'submitted', submitted_run_id: 'run',
  }));
});
it.each(['foreign', 'expired', 'recorded'])('refuses a %s observation before attaching an action', async boundary => {
  vi.mocked(apiClient.get).mockResolvedValue({ ...challenge,
    ...(boundary === 'foreign' ? { snapshot: { selection: { ...challenge.snapshot.selection, binding_id: 'foreign' } } }
      : boundary === 'expired' ? { expires_at: '2000-01-01T00:00:00Z' } : { observation_receipt: { id: 'already-recorded' } }),
  });
  render(<DevOpsReleaseEvidence {...props} />); await attach();
  await screen.findByText(/This observation is unavailable/);
  expect(props.onSelect).not.toHaveBeenCalledWith(expect.objectContaining({ challenge_id: 'challenge' }));
  expect(apiClient.post).not.toHaveBeenCalled();
});
it('requires an entered native hostname and the actual retained launch receipt', async () => {
  vi.mocked(apiClient.get).mockResolvedValue({ ...challenge, subject: 'native_ui' });
  render(<DevOpsReleaseEvidence {...props} />); await attach();
  const record = await screen.findByRole('button', { name: 'Record my native DevOps arrival' });
  expect(record).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Native hostname I saw after SSO'), { target: { value: 'selected.aidevops.global.app.aws' } });
  await act(async () => { fireEvent.click(record); });
  await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/orchestration/flows/flow/l5-observations/challenge/observe', {
    snapshot_sha256: 'a'.repeat(64), observation: 'displayed', displayed_native_hostname: 'selected.aidevops.global.app.aws', launch_receipt_id: 'issued',
  }));
});

it('passes a recorded native arrival to knowledge review only after scoped independent readback', async () => {
  const onNativeArrival = vi.fn();
  const arrived = { ...challenge, subject: 'native_ui', observation_receipt: { provenance: 'authenticated-human-observation', observed: { observation: 'displayed' } } };
  vi.mocked(apiClient.get).mockResolvedValueOnce({ ...challenge, subject: 'native_ui' }).mockResolvedValueOnce(arrived);
  render(<DevOpsReleaseEvidence {...props} onNativeArrival={onNativeArrival} />); await attach();
  fireEvent.change(screen.getByLabelText('Native hostname I saw after SSO'), { target: { value: 'selected.aidevops.global.app.aws' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record my native DevOps arrival' })); });
  expect(apiClient.get).toHaveBeenCalledTimes(2);
  expect(onNativeArrival).toHaveBeenLastCalledWith({ ...arrived, flowId: 'flow' });
});

it('does not forward a changed native arrival scope', async () => {
  const onNativeArrival = vi.fn();
  vi.mocked(apiClient.get).mockResolvedValueOnce({ ...challenge, subject: 'native_ui' }).mockResolvedValueOnce({ ...challenge, subject: 'native_ui', snapshot: { selection: { ...challenge.snapshot.selection, binding_id: 'foreign' } }, observation_receipt: { provenance: 'authenticated-human-observation', observed: { observation: 'displayed' } } });
  render(<DevOpsReleaseEvidence {...props} onNativeArrival={onNativeArrival} />); await attach();
  fireEvent.change(screen.getByLabelText('Native hostname I saw after SSO'), { target: { value: 'selected.aidevops.global.app.aws' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record my native DevOps arrival' })); });
  expect(onNativeArrival).toHaveBeenCalledExactlyOnceWith(null);
  expect(screen.getByRole('status')).toHaveTextContent('not confirmed');
});
