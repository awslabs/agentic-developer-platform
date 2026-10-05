/**
 * AuthCallback — GitHub broker session handoff (Issue #4133)
 *
 * Covers the security properties of the broker→SPA leg:
 *  - a callback this browser did not initiate is rejected (login CSRF / fixation)
 *  - the exchange code is swapped for tokens over POST, not read from the URL
 *  - token material is scrubbed from the address bar after storage
 *  - both transports require a matching, single-use browser nonce
 */

import { StrictMode } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import AuthCallback from '@/pages/AuthCallback';
import { AdminRole } from '@/types';

const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

const mockSetAuthState = vi.fn();
vi.mock('@/hooks/useAuth', () => ({
  useAuth: () => ({ setAuthState: mockSetAuthState }),
}));

vi.mock('@/services/auth', () => ({
  handleOAuthCallback: vi.fn(),
  buildLoginUrl: vi.fn(),
  storeTokens: vi.fn(),
  parseIdTokenForUser: vi.fn(),
  getBrokerState: vi.fn(),
  exchangeBrokerCode: vi.fn(),
  consumePostLoginRedirect: vi.fn(),
}));

import * as authService from '@/services/auth';

const USER = {
  id: 'u1',
  email: 'dev@example.com',
  name: 'Dev',
  role: AdminRole.PLATFORM_ADMIN,
  permissions: [],
};

const BROKER_TOKENS = {
  id_token: 'idt',
  access_token: 'at',
  refresh_token: 'rt',
  expires_in: 3600,
  token_type: 'Bearer',
};

function renderAt(search: string) {
  return render(
    <MemoryRouter initialEntries={[`/auth/callback${search}`]}>
      <AuthCallback />
    </MemoryRouter>
  );
}

describe('AuthCallback — GitHub broker handoff (#4133)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(authService.parseIdTokenForUser).mockReturnValue(USER);
    vi.mocked(authService.exchangeBrokerCode).mockResolvedValue(BROKER_TOKENS);
    vi.spyOn(window.history, 'replaceState').mockImplementation(() => {});
  });

  describe('exchange-code transport', () => {
    it('exchanges the code for tokens and signs the user in', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt('?source=github_broker&code=the-code&state=nonce-abc');

      await waitFor(() => {
        expect(authService.exchangeBrokerCode).toHaveBeenCalledWith('the-code', 'nonce-abc');
      });
      expect(authService.storeTokens).toHaveBeenCalledWith(BROKER_TOKENS);
      expect(mockSetAuthState).toHaveBeenCalledWith({
        user: USER,
        token: 'at',
        isAuthenticated: true,
        isLoading: false,
      });
      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });

    it('returns to the stored deep link instead of the dashboard', async () => {
      // The user was headed somewhere specific (e.g. the CLI approval page
      // opened by `login --web`) when ProtectedRoute bounced them to /login.
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');
      vi.mocked(authService.consumePostLoginRedirect).mockReturnValueOnce('/cli-auth?code=ABCD-2345');

      renderAt('?source=github_broker&code=the-code&state=nonce-abc');

      await waitFor(() => {
        expect(mockNavigate).toHaveBeenCalledWith('/cli-auth?code=ABCD-2345', { replace: true });
      });
    });

    it('scrubs the code out of the address bar after storing tokens', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt('?source=github_broker&code=the-code&state=nonce-abc');

      await waitFor(() => {
        expect(window.history.replaceState).toHaveBeenCalledWith({}, '', '/auth/callback');
      });
    });

    it('rejects a callback whose state does not match the stored nonce', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('victim-nonce');

      renderAt('?source=github_broker&code=attacker-code&state=attacker-nonce');

      await waitFor(() => {
        expect(
          screen.getByText(/did not come from a login started in this browser/i)
        ).toBeInTheDocument();
      });
      expect(authService.exchangeBrokerCode).not.toHaveBeenCalled();
      expect(authService.storeTokens).not.toHaveBeenCalled();
      expect(mockSetAuthState).not.toHaveBeenCalled();
    });

    it('rejects a callback when no login was started in this browser', async () => {
      // The session-fixation case: victim never clicked "Sign in with GitHub",
      // so sessionStorage holds no nonce.
      vi.mocked(authService.getBrokerState).mockReturnValue(null);

      renderAt('?source=github_broker&code=attacker-code&state=attacker-nonce');

      await waitFor(() => {
        expect(
          screen.getByText(/did not come from a login started in this browser/i)
        ).toBeInTheDocument();
      });
      expect(authService.exchangeBrokerCode).not.toHaveBeenCalled();
      expect(mockSetAuthState).not.toHaveBeenCalled();
    });

    it('rejects a code callback carrying no state at all', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt('?source=github_broker&code=the-code');

      await waitFor(() => {
        expect(
          screen.getByText(/did not come from a login started in this browser/i)
        ).toBeInTheDocument();
      });
      expect(authService.exchangeBrokerCode).not.toHaveBeenCalled();
    });

    it('surfaces an exchange failure instead of signing the user in', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');
      vi.mocked(authService.exchangeBrokerCode).mockRejectedValue(
        new Error('invalid_code')
      );

      renderAt('?source=github_broker&code=used-code&state=nonce-abc');

      await waitFor(() => {
        expect(screen.getByText('invalid_code')).toBeInTheDocument();
      });
      expect(mockSetAuthState).not.toHaveBeenCalled();
    });
  });

  describe('legacy tokens-in-query transport with mandatory state', () => {
    it('accepts legacy tokens only with the browser nonce', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt(
        '?source=github_broker&id_token=idt&access_token=at&refresh_token=rt&expires_in=3600&state=nonce-abc'
      );

      await waitFor(() => {
        expect(authService.storeTokens).toHaveBeenCalledWith({
          id_token: 'idt',
          access_token: 'at',
          refresh_token: 'rt',
          expires_in: 3600,
          token_type: 'Bearer',
        });
      });
      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
      expect(authService.exchangeBrokerCode).not.toHaveBeenCalled();
    });

    it('still scrubs tokens from the address bar on the legacy path', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt('?source=github_broker&id_token=idt&access_token=at&state=nonce-abc');

      await waitFor(() => {
        expect(window.history.replaceState).toHaveBeenCalledWith({}, '', '/auth/callback');
      });
    });

    it('rejects a legacy callback whose state is present but wrong', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('victim-nonce');

      renderAt(
        '?source=github_broker&id_token=idt&access_token=at&state=attacker-nonce'
      );

      await waitFor(() => {
        expect(
          screen.getByText(/did not come from a login started in this browser/i)
        ).toBeInTheDocument();
      });
      expect(authService.storeTokens).not.toHaveBeenCalled();
    });

    it('errors when the legacy callback has no tokens', async () => {
      vi.mocked(authService.getBrokerState).mockReturnValue('nonce-abc');

      renderAt('?source=github_broker&state=nonce-abc');

      await waitFor(() => {
        expect(screen.getByText(/missing tokens/i)).toBeInTheDocument();
      });
      expect(authService.storeTokens).not.toHaveBeenCalled();
    });
  });

  it.each([null, 'victim-nonce'])('rejects a stateless legacy callback (stored=%s)', async stored => {
    vi.mocked(authService.getBrokerState).mockReturnValue(stored);
    renderAt('?source=github_broker&id_token=attacker&access_token=attacker');
    await screen.findByText(/did not come from a login started in this browser/i);
    expect(authService.storeTokens).not.toHaveBeenCalled();
    expect(mockSetAuthState).not.toHaveBeenCalled();
    expect(window.history.replaceState).toHaveBeenCalledWith({}, '', '/auth/callback');
  });

  it('processes a one-time nonce once under StrictMode', async () => {
    vi.mocked(authService.getBrokerState).mockReturnValueOnce('nonce-abc').mockReturnValue(null);
    render(<StrictMode><MemoryRouter initialEntries={['/auth/callback?source=github_broker&code=code&state=nonce-abc']}><AuthCallback /></MemoryRouter></StrictMode>);
    await waitFor(() => expect(mockSetAuthState).toHaveBeenCalledTimes(1));
    expect(authService.getBrokerState).toHaveBeenCalledTimes(1);
    expect(authService.exchangeBrokerCode).toHaveBeenCalledTimes(1);
  });

  describe('unchanged paths', () => {
    it('surfaces a broker error param without touching tokens', async () => {
      renderAt('?error=not_authorized');

      await waitFor(() => {
        expect(screen.getByText(/not_authorized/i)).toBeInTheDocument();
      });
      expect(authService.storeTokens).not.toHaveBeenCalled();
    });

    it('still runs the email/password PKCE code exchange', async () => {
      vi.mocked(authService.handleOAuthCallback).mockResolvedValue({
        user: USER,
        token: 'pkce-token',
        expiresAt: new Date().toISOString(),
      });

      renderAt('?code=cognito-code');

      await waitFor(() => {
        expect(authService.handleOAuthCallback).toHaveBeenCalledWith('cognito-code');
      });
      // The PKCE path must not be diverted through the broker exchange.
      expect(authService.exchangeBrokerCode).not.toHaveBeenCalled();
      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });

    it('errors when no code and no broker params are present', async () => {
      renderAt('');

      await waitFor(() => {
        expect(screen.getByText(/no authorization code received/i)).toBeInTheDocument();
      });
    });
  });
});
