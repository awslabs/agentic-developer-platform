import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import NativeCredentialApproval from '@/pages/NativeCredentialApproval';
import { apiClient } from '@/services/api';

const first = 'c4ecf025-99a0-422e-9383-4c946d7f281c';
const second = '7db54ddb-8ed8-4cf7-a9a3-744534e00bb9';
function preview(id = first) {
  return {
    receipt_id: id, status: 'awaiting_approval', expires_at: Math.floor(Date.now() / 1000) + 60,
    snapshot: 'a'.repeat(64), approval_nonce: 'browser-session-bound-nonce',
    selection: { tenant_id: 'tenant', user_id: 'owner', session_id: 'native-session',
      receipt_id: id, request_id: 'request-' + id, execution_id: 'execution-' + id,
      credential_id: 'credential-' + id, service: 'aws', label: 'Selection ' + id,
      operation: 'assume_role', argv: ['/usr/bin/printf', '%s', 'first second', 'third'],
      cwd: '/home/alice/project', use_seconds: 900 },
    offer: { command_digest: 'b'.repeat(64), sink_digest: 'c'.repeat(64), recipient_fingerprint: 'd'.repeat(64) },
  };
}
function Navigation() {
  const navigate = useNavigate();
  return <button onClick={() => navigate('/credentials/native/' + second)}>Other request</button>;
}
function show() {
  return render(<MemoryRouter initialEntries={['/credentials/native/' + first]}><Navigation /><Routes>
    <Route path="/credentials/native/:receiptId" element={<NativeCredentialApproval />} />
  </Routes></MemoryRouter>);
}

describe('native approval request recovery and binding', () => {
  beforeEach(() => {
    vi.spyOn(apiClient, 'get').mockResolvedValue(preview());
    vi.spyOn(apiClient, 'post').mockResolvedValue({ receipt_id: first, status: 'approved' });
  });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it('renders exact argument boundaries for the immutable approved command', async () => {
    show();
    await screen.findByRole('button', { name: /^Approve/ });
    expect(screen.getByText(/"first second"/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Manage or revoke credential consent' })).toHaveAttribute('href', '/settings/credentials');
  });

  it('removes approval controls when an initially pending request expires', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2030-01-01T00:00:00Z'));
    vi.mocked(apiClient.get).mockResolvedValue({ ...preview(), expires_at: Math.floor(Date.now() / 1000) + 1 });
    show();
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByRole('button', { name: /^Approve/ })).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByRole('button', { name: /^Approve/ })).not.toBeInTheDocument();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('clears the previous selection while loading another receipt', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(preview()).mockImplementationOnce(() => new Promise(() => {}));
    show();
    await screen.findByRole('button', { name: /^Approve/ });
    fireEvent.click(screen.getByRole('button', { name: 'Other request' }));
    expect(screen.queryByText(new RegExp('Selection ' + first))).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Approve/ })).not.toBeInTheDocument();
  });

  it('refuses a decision after expiry even before the next display timer tick', async () => {
    show();
    const approve = await screen.findByRole('button', { name: /^Approve/ });
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 120000);
    fireEvent.click(approve);
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('ignores a previous receipt decision completing after navigation', async () => {
    let resolveDecision!: (value: unknown) => void;
    vi.mocked(apiClient.post).mockImplementationOnce(() => new Promise((resolve) => { resolveDecision = resolve; }));
    vi.mocked(apiClient.get).mockResolvedValueOnce(preview()).mockResolvedValueOnce(preview(second));
    show();
    fireEvent.click(await screen.findByRole('button', { name: /^Approve/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Other request' }));
    await screen.findByText(new RegExp('Selection ' + second));
    await act(async () => resolveDecision({ receipt_id: first, status: 'approved' }));
    expect(screen.getByText(new RegExp('Selection ' + second))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Approve/ })).toBeInTheDocument();
  });

  it('recovers an ambiguous decision by reading the original receipt without resubmission', async () => {
    vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('private-server-canary'));
    show();
    fireEvent.click(await screen.findByRole('button', { name: /^Approve/ }));
    const checkStatus = await screen.findByRole('button', { name: /Check status/ });
    expect(screen.queryByText(/private-server-canary/)).not.toBeInTheDocument();
    vi.mocked(apiClient.get).mockResolvedValueOnce({ ...preview(), status: 'approved', approval_nonce: '' });
    fireEvent.click(checkStatus);
    await waitFor(() => expect(apiClient.get).toHaveBeenLastCalledWith('/auth/native-credentials/approvals/' + first, expect.any(AbortSignal), true));
    expect(await screen.findByText('Selection approved')).toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });

  it('treats a decision response for another receipt as unconfirmed', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({ receipt_id: second, status: 'approved' });
    show();
    fireEvent.click(await screen.findByRole('button', { name: /^Approve/ }));
    expect(await screen.findByText(/outcome is not confirmed/)).toBeInTheDocument();
    expect(apiClient.post).toHaveBeenCalledTimes(1);
  });
});
