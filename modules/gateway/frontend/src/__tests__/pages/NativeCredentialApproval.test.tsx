import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NativeCredentialApproval from '@/pages/NativeCredentialApproval';
import { apiClient } from '@/services/api';
import { validateNativePreview, type NativeCredentialPreview } from '@/services/nativeCredentials';

const receipt = 'c4ecf025-99a0-422e-9383-4c946d7f281c';
const other = '7db54ddb-8ed8-4cf7-a9a3-744534e00bb9';
function preview(id = receipt): NativeCredentialPreview {
  return {
    receipt_id: id, status: 'awaiting_approval', expires_at: Math.floor(Date.now() / 1000) + 60,
    snapshot: 'a'.repeat(64), approval_nonce: 'browser-session-bound-nonce',
    selection: { tenant_id: 'canonical-tenant', user_id: 'canonical-user', session_id: 'native-session',
      receipt_id: id, request_id: 'original-request', execution_id: 'original-execution',
      credential_id: 'selected-vault-entry', service: 'aws', label: 'Dev role', operation: 'assume_role',
      argv: ['/usr/bin/aws', 'sts', 'get-caller-identity', '--query', 'Account'], cwd: '/home/alice/project', use_seconds: 900 },
    offer: { command_digest: 'b'.repeat(64), sink_digest: 'c'.repeat(64), recipient_fingerprint: 'd'.repeat(64) },
  };
}

function Navigation() {
  const navigate = useNavigate();
  return <button onClick={() => navigate('/credentials/native/' + other)}>Other request</button>;
}
function show(id = receipt) {
  return render(<MemoryRouter initialEntries={['/credentials/native/' + id]}><Navigation /><Routes>
    <Route path="/credentials/native/:receiptId" element={<NativeCredentialApproval />} />
  </Routes></MemoryRouter>);
}

describe('trusted native credential selection', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(apiClient, 'get').mockResolvedValue(preview());
    vi.spyOn(apiClient, 'post').mockResolvedValue({ receipt_id: receipt, status: 'approved' });
  });

  it('shows exact stored selection and submits its snapshot and browser nonce once', async () => {
    const user = userEvent.setup();
    show();
    const approve = await screen.findByRole('button', { name: 'Approve this command' });
    expect(screen.getByText('/usr/bin/aws')).toBeInTheDocument();
    expect(screen.getByText('/home/alice/project')).toBeInTheDocument();
    expect(screen.getByText('900 seconds')).toBeInTheDocument();
    expect(screen.getByText('d'.repeat(64))).toBeInTheDocument();
    expect(screen.getByText(/"get-caller-identity"/)).toBeInTheDocument();
    await user.dblClick(approve);
    expect(apiClient.post).toHaveBeenCalledTimes(1);
    expect(apiClient.post).toHaveBeenCalledWith('/auth/native-credentials/approvals/' + receipt, {
      snapshot: 'a'.repeat(64), approval_nonce: 'browser-session-bound-nonce', action: 'approve',
    });
    expect(await screen.findByText('Selection approved')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve this command' })).not.toBeInTheDocument();
  });

  it('denies without invoking native challenge, issue or credential APIs', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ receipt_id: receipt, status: 'denied' });
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole('button', { name: 'Deny' }));
    expect(await screen.findByText('Request denied')).toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledWith('/auth/native-credentials/approvals/' + receipt, expect.objectContaining({ action: 'deny' }));
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it('leaves an ambiguous decision disabled until an explicit status read', async () => {
    vi.mocked(apiClient.post).mockRejectedValue(new Error('SECRET_CANARY server error'));
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole('button', { name: 'Approve this command' }));
    expect(await screen.findByText('Approval outcome is not confirmed')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve this command' })).not.toBeInTheDocument();
    expect(screen.queryByText(/SECRET_CANARY/)).not.toBeInTheDocument();
    vi.mocked(apiClient.get).mockResolvedValue({ ...preview(), status: 'approved', approval_nonce: '' });
    await user.click(screen.getByRole('button', { name: 'Check status' }));
    expect(await screen.findByText('Selection approved')).toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it.each(['expired', 'denied', 'unknown', 'delivered', 'acknowledged', 'challenged'] as const)('renders %s as read-only without a new nonce', async (status) => {
    vi.mocked(apiClient.get).mockResolvedValue({ ...preview(), status, approval_nonce: '' });
    show();
    await screen.findByText('d'.repeat(64));
    expect(screen.queryByRole('button', { name: 'Approve this command' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deny' })).not.toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('refuses approval if the stored snapshot has already expired locally', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ ...preview(), expires_at: 1 });
    show();
    expect(await screen.findByText('Request expired')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve this command' })).not.toBeInTheDocument();
  });

  it('does not let a late preview replace the newly selected request', async () => {
    let resolveOld!: (value: NativeCredentialPreview) => void;
    vi.mocked(apiClient.get).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; })).mockResolvedValueOnce(preview(other));
    const user = userEvent.setup();
    show();
    await user.click(screen.getByRole('button', { name: 'Other request' }));
    await screen.findByText(other);
    await act(async () => resolveOld(preview()));
    expect(screen.queryByText(receipt)).not.toBeInTheDocument();
    vi.mocked(apiClient.post).mockResolvedValue({ receipt_id: other, status: 'approved' });
    await user.click(screen.getByRole('button', { name: 'Approve this command' }));
    await waitFor(() => expect(apiClient.post).toHaveBeenCalledWith('/auth/native-credentials/approvals/' + other, expect.anything()));
  });

  it('rejects malformed or mismatched previews and never renders extra payloads', async () => {
    const valid = preview();
    const withSecret = { ...valid, credential: 'SECRET_CANARY', offer: { ...valid.offer, ciphertext: 'SECRET_CANARY' } };
    const projected = validateNativePreview(withSecret, receipt);
    expect(JSON.stringify(projected)).not.toContain('SECRET_CANARY');
    vi.mocked(apiClient.get).mockResolvedValue({ ...withSecret, receipt_id: other });
    show();
    expect(await screen.findByText('This request is unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/SECRET_CANARY/)).not.toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
