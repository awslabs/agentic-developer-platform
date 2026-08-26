/**
 * ConnectCliPanel tests — Issue #4146.
 *
 * The security properties are the point of this component, so they are asserted
 * directly: the refresh token must never appear in the copyable command, and must
 * stay masked until an explicit Reveal.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConnectCliPanel } from '@/components/setup/ConnectCliPanel';
import * as auth from '@/services/auth';

const TOKEN = 'eyJjdHkiOiJKV1QiLCJlbmMi.SUPER-SECRET-REFRESH-TOKEN.abc123';
const STUB_ORIGIN = 'https://d123abc.cloudfront.net';

describe('ConnectCliPanel', () => {
  const realLocation = window.location;

  beforeEach(() => {
    Object.defineProperty(window, 'location', {
      writable: true,
      value: { ...window.location, origin: STUB_ORIGIN },
    });
    vi.spyOn(auth, 'getRefreshToken').mockReturnValue(TOKEN);
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { writable: true, value: realLocation });
    vi.restoreAllMocks();
  });

  it('renders the import command against the real gateway URL', () => {
    render(<ConnectCliPanel />);

    expect(screen.getByTestId('import-command')).toHaveTextContent(
      `bg-cognito-auth.sh import --gateway-url ${STUB_ORIGIN}/api`
    );
  });

  it('never embeds the refresh token in the copyable command', () => {
    // An argv-embedded token lands in ~/.bash_history and in `ps` output.
    render(<ConnectCliPanel />);

    expect(screen.getByTestId('import-command').textContent).not.toContain(TOKEN);
    expect(screen.getByTestId('import-command').textContent).not.toContain('--refresh-token');
  });

  it('masks the refresh token until Reveal is clicked', () => {
    render(<ConnectCliPanel />);

    const field = screen.getByTestId('refresh-token');
    expect(field.textContent).not.toContain(TOKEN);
    expect(field.textContent).toMatch(/•+/);
  });

  it('shows the token after Reveal, and re-masks on Hide', () => {
    render(<ConnectCliPanel />);

    fireEvent.click(screen.getByRole('button', { name: 'Reveal' }));
    expect(screen.getByTestId('refresh-token')).toHaveTextContent(TOKEN);

    fireEvent.click(screen.getByRole('button', { name: 'Hide' }));
    expect(screen.getByTestId('refresh-token').textContent).not.toContain(TOKEN);
  });

  it('warns that the token is a long-lived credential', () => {
    render(<ConnectCliPanel />);

    expect(screen.getByText(/long-lived credential/i)).toBeInTheDocument();
  });

  it('notes the token is scoped to this browser tab', () => {
    render(<ConnectCliPanel />);

    expect(document.body.textContent ?? '').toContain('sessionStorage');
  });

  // --- Empty-token fallback --------------------------------------------------
  // AuthCallback stores `refreshToken || ''`, so a logged-in user can hold none.
  it.each([
    ['empty string', ''],
    ['null', null],
  ])('renders a re-sign-in message when the refresh token is %s', (_label, value) => {
    vi.spyOn(auth, 'getRefreshToken').mockReturnValue(value as string | null);
    render(<ConnectCliPanel />);

    expect(screen.getByText(/Sign out and sign in again/i)).toBeInTheDocument();
    expect(screen.queryByTestId('import-command')).not.toBeInTheDocument();
  });
});
