/**
 * CliAuth page tests — web CLI login approval (login --web).
 *
 * The page's job: show the user_code from the URL so the human can match it
 * against their terminal, then POST the approve/deny decision. It must never
 * render a credential — the code it displays is a correlation check, not a
 * secret.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import CliAuth from '@/pages/CliAuth';
import { apiClient } from '@/services/api';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/cli-auth" element={<CliAuth />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('CliAuth', () => {
  beforeEach(() => {
    vi.spyOn(apiClient, 'post').mockResolvedValue({ status: 'approved' });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the code from the URL for the human to cross-check', () => {
    renderAt('/cli-auth?code=ABCD-2345');
    expect(screen.getByTestId('user-code')).toHaveTextContent('ABCD-2345');
    expect(screen.getByRole('button', { name: /approve/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /deny/i })).toBeInTheDocument();
  });

  it('uppercases a lowercased code from the URL', () => {
    renderAt('/cli-auth?code=abcd-2345');
    expect(screen.getByTestId('user-code')).toHaveTextContent('ABCD-2345');
  });

  it('approve posts the decision and tells the user to return to the terminal', async () => {
    renderAt('/cli-auth?code=ABCD-2345');
    await userEvent.click(screen.getByRole('button', { name: /approve/i }));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith('/auth/cli/approve', {
        user_code: 'ABCD-2345',
        action: 'approve',
      });
    });
    expect(await screen.findByText(/return to your terminal/i)).toBeInTheDocument();
    // The action buttons are gone — no double-submit.
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument();
  });

  it('deny posts the decision and confirms rejection', async () => {
    vi.mocked(apiClient.post).mockResolvedValue({ status: 'denied' });
    renderAt('/cli-auth?code=ABCD-2345');
    await userEvent.click(screen.getByRole('button', { name: /^deny$/i }));

    await waitFor(() => {
      expect(apiClient.post).toHaveBeenCalledWith('/auth/cli/approve', {
        user_code: 'ABCD-2345',
        action: 'deny',
      });
    });
    expect(await screen.findByText(/sign-in denied/i)).toBeInTheDocument();
  });

  it('explains an expired/unknown code instead of a raw error', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({ detail: { error: 'unknown_code' } });
    renderAt('/cli-auth?code=ABCD-2345');
    await userEvent.click(screen.getByRole('button', { name: /approve/i }));
    expect(await screen.findByText(/may have expired/i)).toBeInTheDocument();
  });

  it('tells password-account users to use the terminal login instead', async () => {
    vi.mocked(apiClient.post).mockRejectedValue({
      detail: { error: 'password_login_required', message: 'Use bg-cognito-auth.sh login instead.' },
    });
    renderAt('/cli-auth?code=ABCD-2345');
    await userEvent.click(screen.getByRole('button', { name: /approve/i }));
    expect(await screen.findByText(/login instead/i)).toBeInTheDocument();
  });

  it('handles a missing code without offering buttons', () => {
    renderAt('/cli-auth');
    expect(screen.getByText(/no sign-in code/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument();
  });
});
