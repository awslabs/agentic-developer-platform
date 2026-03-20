/**
 * Login Page - Redirects to Cognito Hosted UI
 *
 * Instead of showing a credentials form, this page automatically redirects
 * users to the Cognito hosted UI for OAuth 2.0 authentication with PKCE.
 */

import { useEffect, useState } from 'react';
import { buildLoginUrl } from '@/services/auth';
import { isCognitoConfigured } from '@/config/cognito';
import { Spinner } from '@/components/ui/Spinner';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';

export default function Login() {
  const [error, setError] = useState<string | null>(null);
  const [isRedirecting, setIsRedirecting] = useState(true);

  useEffect(() => {
    async function redirectToCognito() {
      try {
        // Check if Cognito is configured
        if (!isCognitoConfigured()) {
          setError(
            'Authentication is not configured. Please contact your administrator.'
          );
          setIsRedirecting(false);
          return;
        }

        // Generate PKCE challenge and redirect to Cognito
        const loginUrl = await buildLoginUrl();
        window.location.href = loginUrl;
      } catch (err) {
        console.error('Failed to redirect to login:', err);
        setError(
          err instanceof Error
            ? err.message
            : 'Failed to initialize login. Please try again.'
        );
        setIsRedirecting(false);
      }
    }

    redirectToCognito();
  }, []);

  const handleRetry = async () => {
    setError(null);
    setIsRedirecting(true);
    try {
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

  if (isRedirecting && !error) {
    return (
      <div className="text-center">
        <Spinner size="lg" />
        <p className="mt-4 text-gray-600 dark:text-gray-300">
          Redirecting to login...
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-md mx-auto">
        <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-6">
          Sign In
        </h2>

        <Alert variant="error" className="mb-6">
          {error}
        </Alert>

        <Button onClick={handleRetry} className="w-full">
          Try Again
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

  return null;
}
