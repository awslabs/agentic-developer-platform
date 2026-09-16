/**
 * CLI Setup page tests — Issues #4146, #4159.
 *
 * Covers the page-level composition: the title, the approval note, the three
 * setup sections, and the troubleshooting matrix. Content-level assertions for
 * the instructions themselves live in SetupInstructions.test.tsx.
 *
 * #4159 renamed the page to "CLI Setup" and moved the download cards + Connect
 * CLI panel inside SetupInstructions, so the page-level assertions here changed
 * shape but not substance — the same elements must still reach the user.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ClaudeSetup from '@/pages/ClaudeSetup';
import * as auth from '@/services/auth';

const mockUseAuth = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

const STUB_ORIGIN = 'https://d123abc.cloudfront.net';

/**
 * The Troubleshooting card, by walking up from its heading. Needed because some
 * commands legitimately appear both here and in the setup steps above, so
 * page-wide text queries cannot tell the two apart.
 */
function getTroubleshootingCard(): HTMLElement {
  // CardTitle renders an <h3> as a direct child of the Card's <div>, so the
  // nearest enclosing div IS the card — do not walk past it, or the query
  // widens back to the whole page.
  const card = screen.getByText('Troubleshooting').closest('div');
  if (!card) throw new Error('Troubleshooting card not found');
  return card as HTMLElement;
}

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

    expect(screen.getByRole('heading', { name: 'CLI Setup' })).toBeInTheDocument();
  });

  it('names both tools in the subtitle rather than Claude Code alone', () => {
    // The old title, "Claude Code Setup", made Codex users think the page was
    // not for them (Issue #4159).
    render(<ClaudeSetup />);

    expect(
      screen.queryByRole('heading', { name: 'Claude Code Setup' })
    ).not.toBeInTheDocument();
    expect(document.body.textContent ?? '').toContain('Use Claude Code or Codex on your machine');
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

  it('renders the setup sections and the download fallback', async () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('heading', { name: 'Set up your CLI' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Verify' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Download Helper Scripts' })).toBeInTheDocument();
    // Each tab's download fallback lists only its own files: Claude Code needs
    // one, Codex needs two (helper + `serve` proxy, Issue #4156).
    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(1);
    expect(screen.getAllByText('bg-cognito-auth.sh').length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole('tab', { name: 'Codex' }));
    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(2);
    expect(screen.getAllByText('bg-gateway-proxy.py').length).toBeGreaterThan(0);
  });

  it('offers both tool tabs, defaulting to Claude Code', () => {
    render(<ClaudeSetup />);

    expect(screen.getByRole('tab', { name: 'Claude Code' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByRole('tab', { name: 'Codex' })).toBeInTheDocument();
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

  // --- Issue #4859: troubleshooting must use the adp verbs ---------------------
  it('points troubleshooting at the adp verbs, not the raw helper script', () => {
    // The page told users to install `adp` and then, in troubleshooting, to run a
    // script name they may never have installed (it now lives under ~/.adp/bin).
    // Scoped to the Troubleshooting card: `adp login` also appears (correctly) in
    // the Sign in step, so a page-wide getByText would match more than one node.
    render(<ClaudeSetup />);
    const troubleshooting = within(getTroubleshootingCard());

    expect(troubleshooting.getByText('adp login')).toBeInTheDocument();
    expect(troubleshooting.getByText('adp import')).toBeInTheDocument();
  });

  it.each([
    ['bg-cognito-auth.sh login --web'],
    ['bg-cognito-auth.sh import'],
  ])('no longer tells the user to run %s', (needle) => {
    // Exact-text queries: the collapsed raw-script fallback inside
    // SetupInstructions legitimately still documents the ~/bin-prefixed script
    // flow, so this asserts the bare troubleshooting commands are gone rather
    // than banning the script name page-wide.
    render(<ClaudeSetup />);

    expect(screen.queryByText(needle)).not.toBeInTheDocument();
  });

  it('describes the 401 fix for both tools, not Claude Code only', () => {
    render(<ClaudeSetup />);

    expect(document.body.textContent ?? '').toContain(
      'This applies to both Claude Code and Codex'
    );
  });

  it('no longer references the deprecated bg-auth script', async () => {
    render(<ClaudeSetup />);

    // The old Troubleshooting card told users to re-run `bg-auth`, which is the
    // deprecated SigV4 helper and would not fix a 401 on this flow. Checked on
    // both tabs — inactive tab panels render nothing, so a single view could
    // miss a regression parked behind the other tab.
    for (const needle of [
      'bg-auth.sh',
      'bg-auth.ps1',
      'aws configure sso',
      'your-gateway-url',
      'apiBaseUrl',
    ]) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }

    await userEvent.click(screen.getByRole('tab', { name: 'Codex' }));

    for (const needle of [
      'bg-auth.sh',
      'bg-auth.ps1',
      'aws configure sso',
      'your-gateway-url',
      'apiBaseUrl',
    ]) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }
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
