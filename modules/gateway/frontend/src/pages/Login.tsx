/**
 * Login Page - Dual-path authentication
 *
 * Shows two sign-in options:
 * 1. "Sign in with GitHub" — redirects to Lambda auth broker (Issue #520)
 * 2. "Sign in with Email" — redirects to Cognito hosted UI (default provider selection)
 */

import { deploymentSetting } from '@/config/runtime';

import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  buildLoginUrl,
  buildGitHubLoginUrl,
  fetchLoginOptions,
  storePostLoginRedirect,
} from '@/services/auth';
import { isCognitoConfigured } from '@/config/cognito';
import { Spinner } from '@/components/ui/Spinner';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';

export default function Login() {
  const location = useLocation();
  const apiUrl = new URL(deploymentSetting('VITE_API_URL') || '/api', window.location.origin).href.replace(/\/$/, '');
  const shellQuote = (value: string) => "'" + value.replace(/'/g, "'\\''") + "'";
  const installCommand = `curl -fsSL ${shellQuote(`${apiUrl}/cli/install.sh`)} | sh -s -- --gateway-url ${shellQuote(apiUrl)}`;
  const [error, setError] = useState<string | null>(null);
  const [isRedirecting, setIsRedirecting] = useState(false);
  // Issue #2746: null = still loading (render enabled, no disabled-flash);
  // false = GitHub login not wired on this deployment (render disabled + hint).
  const [githubLoginEnabled, setGithubLoginEnabled] = useState<boolean | null>(
    null
  );

  // Issue #4017: surface the error code the auth broker redirects back with.
  // redirect_uri_mismatch is the ONLY observable signal that the GitHub App's
  // callback URL has drifted (GitHub exposes no API to read it), so it gets a
  // specific, actionable message instead of a generic failure.
  // Deep-link preservation: ProtectedRoute sends us the page the user was
  // trying to reach (e.g. /cli-auth?code=... from `login --web`). Both sign-in
  // paths leave the SPA for an external provider, so persist it in
  // sessionStorage for AuthCallback to restore — router state does not survive
  // the round-trip.
  useEffect(() => {
    const from = (location.state as { from?: { pathname?: string; search?: string } } | null)?.from;
    if (from?.pathname && from.pathname !== '/') {
      storePostLoginRedirect(`${from.pathname}${from.search ?? ''}`);
    }
  }, [location.state]);

  useEffect(() => {
    const brokerError = new URLSearchParams(window.location.search).get('error');
    if (!brokerError) return;
    setError(
      brokerError === 'workspace_refresh_required'
        ? 'Please sign in again to finish switching organizations.'
        : brokerError === 'redirect_uri_mismatch'
        ? 'GitHub rejected the sign-in because the App’s configured callback URL does not match this deployment. A platform administrator can fix this in Settings → Connections (“Re-validate config” shows the expected callback URL).'
        : `Sign-in failed: ${brokerError}`
    );
  }, []);

  useEffect(() => {
    let active = true;
    fetchLoginOptions()
      .then((opts) => {
        if (active) {
          setGithubLoginEnabled(opts.github_login_enabled);
        }
      })
      .catch(() => {
        // Fail-open: leave the button enabled (state stays null) if the fetch
        // rejects. fetchLoginOptions already fails open, so this is defensive.
      });
    return () => {
      active = false;
    };
  }, []);

  const handleGitHubLogin = async () => {
    setError(null);
    setIsRedirecting(true);
    try {
      const loginUrl = await buildGitHubLoginUrl();
      window.location.href = loginUrl;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : 'Failed to initialize GitHub login. Please try again.'
      );
      setIsRedirecting(false);
    }
  };

  const handleEmailLogin = async () => {
    setError(null);
    setIsRedirecting(true);
    try {
      if (!isCognitoConfigured()) {
        setError(
          'Authentication is not configured. Please contact your administrator.'
        );
        setIsRedirecting(false);
        return;
      }
      const loginUrl = await buildLoginUrl();
      window.location.href = loginUrl;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : 'Failed to initialize login. Please try again.'
      );
      setIsRedirecting(false);
    }
  };

  if (isRedirecting) {
    return (
      <div className="text-center">
        <Spinner size="lg" />
        <p className="mt-4 text-gray-600 dark:text-gray-300">
          Redirecting to login...
        </p>
      </div>
    );
  }

  return (
    <div className="blueprint-login">
      <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-6">
        Sign In
      </h2>

      {error && (
        <Alert variant="error" className="mb-6">
          {error}
        </Alert>
      )}

      {/* GitHub sign-in button */}
      <Button
        onClick={handleGitHubLogin}
        disabled={githubLoginEnabled === false}
        className="w-full flex items-center justify-center gap-3"
        data-testid="github-login-btn"
      >
        <svg
          className="w-5 h-5"
          viewBox="0 0 24 24"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            clipRule="evenodd"
            d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"
          />
        </svg>
        Sign in with GitHub
      </Button>

      {/* Issue #2746: explain why GitHub sign-in is disabled on an unwired deployment */}
      {githubLoginEnabled === false && (
        <p
          className="mt-2 text-sm text-gray-500 dark:text-gray-400"
          data-testid="github-login-disabled-hint"
        >
          GitHub sign-in isn't set up on this deployment yet. An administrator
          can enable it under Settings → Connections → Set up GitHub App.
        </p>
      )}

      {/* Visual separator */}
      <div className="relative my-6">
        <div className="absolute inset-0 flex items-center">
          <div className="w-full border-t border-gray-300 dark:border-gray-600" />
        </div>
        <div className="relative flex justify-center text-sm">
          <span className="px-2 bg-white dark:bg-gray-800 text-gray-500 dark:text-gray-400">
            or
          </span>
        </div>
      </div>

      {/* Email/password sign-in button */}
      <Button
        variant="secondary"
        onClick={handleEmailLogin}
        className="w-full"
        data-testid="email-login-btn"
      >
        Sign in with Email
      </Button>

      <details className="mt-6 text-sm text-gray-600 dark:text-gray-300">
        <summary className="cursor-pointer font-medium">Install the ADP CLI</summary>
        <p className="mt-3">Download without signing in. Run this command in your terminal:</p>
        <pre className="mt-2 overflow-x-auto rounded bg-gray-100 p-3 text-xs dark:bg-gray-800" data-testid="cli-install-command">{installCommand}</pre>
        <p className="mt-2">Developers: run <code>adp login</code>.</p>
        <p className="mt-2">First-time administrator: run <code>adp admin setup</code> and sign in with your Cognito username and password.</p>
      </details>

      <div className="mt-6 text-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Having trouble signing in?{' '}
          <a
            href="mailto:support@example.com"
            className="text-primary-600 hover:text-primary-500 dark:text-primary-400"
          >
            Contact support
          </a>
        </p>
      </div>
    </div>
  );
}
