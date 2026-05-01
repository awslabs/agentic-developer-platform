import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import Login from '@/pages/Login';

// Mock auth service
vi.mock('@/services/auth', () => ({
  buildLoginUrl: vi.fn(),
  buildGitHubLoginUrl: vi.fn(),
}));

// Mock cognito config
vi.mock('@/config/cognito', () => ({
  isCognitoConfigured: vi.fn(),
}));

import * as authService from '@/services/auth';
import { isCognitoConfigured } from '@/config/cognito';

function renderLogin() {
  return render(
    <BrowserRouter>
      <Login />
    </BrowserRouter>
  );
}

describe('Login Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(isCognitoConfigured).mockReturnValue(true);
  });

  it('renders both GitHub and Email sign-in buttons', () => {
    renderLogin();

    expect(screen.getByTestId('github-login-btn')).toBeInTheDocument();
    expect(screen.getByText('Sign in with GitHub')).toBeInTheDocument();
    expect(screen.getByTestId('email-login-btn')).toBeInTheDocument();
    expect(screen.getByText('Sign in with Email')).toBeInTheDocument();
  });

  it('renders the visual separator between auth methods', () => {
    renderLogin();

    expect(screen.getByText('or')).toBeInTheDocument();
  });

  it('redirects to GitHub login URL when GitHub button is clicked', async () => {
    const mockUrl = 'https://example.auth.us-east-1.amazoncognito.com/oauth2/authorize?identity_provider=GitHub';
    vi.mocked(authService.buildGitHubLoginUrl).mockResolvedValue(mockUrl);

    const locationHrefSpy = vi.spyOn(window, 'location', 'get');
    const mockLocation = { ...window.location, href: '' };
    locationHrefSpy.mockReturnValue(mockLocation as Location);

    renderLogin();

    fireEvent.click(screen.getByTestId('github-login-btn'));

    await waitFor(() => {
      expect(authService.buildGitHubLoginUrl).toHaveBeenCalled();
    });

    locationHrefSpy.mockRestore();
  });

  it('redirects to standard login URL when Email button is clicked', async () => {
    const mockUrl = 'https://example.auth.us-east-1.amazoncognito.com/oauth2/authorize';
    vi.mocked(authService.buildLoginUrl).mockResolvedValue(mockUrl);

    const locationHrefSpy = vi.spyOn(window, 'location', 'get');
    const mockLocation = { ...window.location, href: '' };
    locationHrefSpy.mockReturnValue(mockLocation as Location);

    renderLogin();

    fireEvent.click(screen.getByTestId('email-login-btn'));

    await waitFor(() => {
      expect(authService.buildLoginUrl).toHaveBeenCalled();
    });

    locationHrefSpy.mockRestore();
  });

  it('shows error when Cognito is not configured and GitHub is clicked', async () => {
    vi.mocked(isCognitoConfigured).mockReturnValue(false);

    renderLogin();

    fireEvent.click(screen.getByTestId('github-login-btn'));

    await waitFor(() => {
      expect(
        screen.getByText('Authentication is not configured. Please contact your administrator.')
      ).toBeInTheDocument();
    });
  });

  it('shows error when Cognito is not configured and Email is clicked', async () => {
    vi.mocked(isCognitoConfigured).mockReturnValue(false);

    renderLogin();

    fireEvent.click(screen.getByTestId('email-login-btn'));

    await waitFor(() => {
      expect(
        screen.getByText('Authentication is not configured. Please contact your administrator.')
      ).toBeInTheDocument();
    });
  });

  it('shows error when buildGitHubLoginUrl throws', async () => {
    vi.mocked(authService.buildGitHubLoginUrl).mockRejectedValue(
      new Error('PKCE generation failed')
    );

    renderLogin();

    fireEvent.click(screen.getByTestId('github-login-btn'));

    await waitFor(() => {
      expect(screen.getByText('PKCE generation failed')).toBeInTheDocument();
    });
  });

  it('shows spinner when redirecting', async () => {
    // Make the login URL never resolve to keep spinner visible
    vi.mocked(authService.buildGitHubLoginUrl).mockReturnValue(new Promise(() => {}));

    renderLogin();

    fireEvent.click(screen.getByTestId('github-login-btn'));

    await waitFor(() => {
      expect(screen.getByText('Redirecting to login...')).toBeInTheDocument();
    });
  });
});
