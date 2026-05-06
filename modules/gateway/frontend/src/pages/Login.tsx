/**
 * Login Page - Dual-path authentication
 *
 * Shows two sign-in options:
 * 1. "Sign in with GitHub" — redirects to Lambda auth broker (Issue #520)
 * 2. "Sign in with Email" — redirects to Cognito hosted UI (default provider selection)
 */

import { useState } from 'react';
import { buildLoginUrl, buildGitHubLoginUrl } from '@/services/auth';
import { isCognitoConfigured } from '@/config/cognito';
import { Spinner } from '@/components/ui/Spinner';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';

export default function Login() {
  const [error, setError] = useState<string | null>(null);
  const [isRedirecting, setIsRedirecting] = useState(false);

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
    <div className="max-w-md mx-auto">
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
