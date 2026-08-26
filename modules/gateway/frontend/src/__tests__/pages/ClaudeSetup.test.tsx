/**
 * ClaudeSetup page tests — Issue #4146. First tests for this page.
 *
 * Covers the page-level composition: the approval note, the Connect CLI panel,
 * and the troubleshooting matrix. Content-level assertions for the instructions
 * themselves live in SetupInstructions.test.tsx.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ClaudeSetup from '@/pages/ClaudeSetup';
import * as auth from '@/services/auth';

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

const STUB_ORIGIN = 'https://d123abc.cloudfront.net';

describe('ClaudeSetup', () => {
  const realLocation = window.location;

  beforeEach(() => {
    Object.defineProperty(window, 'location', {
      writable: true,
      value: { ...window.location, origin: STUB_ORIGIN },
    });
    vi.spyOn(auth, 'getRefreshToken').mockReturnValue('refresh-token-value');
    mockUseAuth.mockReturnValue({
      isAuthenticated: true,
      user: { id: 'user-123', role: 'registered', orgId: 'org-1' },
    });
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { writable: true, value: realLocation });
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it('renders the page heading', () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('heading', { name: 'Claude Code Setup' })).toBeInTheDocument();
  });

  it('shows the approval note explaining the 409', () => {
    render(<ClaudeSetup />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('user_not_assigned_to_org');
    expect(text).toContain('409');
  });

  it('links to the request-access flow', () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('link', { name: /Settings → Connections/ })).toHaveAttribute(
      'href',
      '/settings/connections'
    );
  });

  it('renders the Connect CLI panel', () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('heading', { name: 'Connect CLI' })).toBeInTheDocument();
    expect(screen.getByTestId('import-command')).toBeInTheDocument();
  });

  it('renders the setup instructions and the download list', () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('heading', { name: 'Setup Instructions' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Download Helper Scripts' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument();
    // Named in more than one place (instructions + download card) — that's fine.
    expect(screen.getAllByText('bg-cognito-auth.sh').length).toBeGreaterThan(0);
  });

  // --- Troubleshooting matrix ------------------------------------------------
  it.each([
    ['401', /401 Unauthorized/],
    ['409', /409 Conflict/],
    ['429', /429 Too Many Requests/],
    ['402', /402 Payment Required/],
  ])('documents the %s failure mode', (_code, pattern) => {
    render(<ClaudeSetup />);

    expect(screen.getByText(pattern)).toBeInTheDocument();
  });

  it('no longer references the deprecated bg-auth script', () => {
    render(<ClaudeSetup />);
    const text = document.body.textContent ?? '';

    // The old Troubleshooting card told users to re-run `bg-auth`, which is the
    // deprecated SigV4 helper and would not fix a 401 on this flow.
    expect(text).not.toContain('bg-auth.sh');
    expect(text).not.toContain('bg-auth.ps1');
    expect(text).not.toContain('aws configure sso');
    expect(text).not.toContain('your-gateway-url');
    expect(text).not.toContain('apiBaseUrl');
  });

  it('shows the user access information when authenticated', () => {
    render(<ClaudeSetup />);

    expect(screen.getByText('user-123')).toBeInTheDocument();
    expect(screen.getByText('org-1')).toBeInTheDocument();
  });

  it('prompts re-sign-in instead of a broken command when no refresh token exists', () => {
    vi.spyOn(auth, 'getRefreshToken').mockReturnValue('');
    render(<ClaudeSetup />);

    expect(screen.getByText(/Sign out and sign in again/i)).toBeInTheDocument();
    expect(screen.queryByTestId('import-command')).not.toBeInTheDocument();
  });
});
