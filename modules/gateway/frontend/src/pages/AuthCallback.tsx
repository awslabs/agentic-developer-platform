/**
 * OAuth Callback Page
 *
 * Handles the OAuth 2.0 authorization code callback from Cognito.
 * Extracts the authorization code from URL params, exchanges it for tokens
 * using the PKCE verifier, and redirects to the dashboard.
 */

import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { Spinner } from '@/components/ui/Spinner';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { handleOAuthCallback, buildLoginUrl } from '@/services/auth';

export default function AuthCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { setAuthState } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(true);

  useEffect(() => {
    async function processCallback() {
      try {
        const errorParam = searchParams.get('error');
        const errorDescription = searchParams.get('error_description');

        // Handle OAuth error response
        if (errorParam) {
          setError(errorDescription || `Authentication error: ${errorParam}`);
          setIsProcessing(false);
          return;
        }

        // Check if this is a broker callback (tokens in query params)
        const brokerSource = searchParams.get('source');
        if (brokerSource === 'github_broker') {
          // Tokens come directly from the GitHub auth broker Lambda (Issue #520)
          const idToken = searchParams.get('id_token');
          const accessToken = searchParams.get('access_token');
          const refreshToken = searchParams.get('refresh_token');
          const expiresIn = searchParams.get('expires_in');

          if (!idToken || !accessToken) {
            setError('Invalid broker response — missing tokens. Please try again.');
            setIsProcessing(false);
            return;
          }

          // Store tokens using the existing auth service
          const { storeTokens, parseIdTokenForUser } = await import('@/services/auth');
          storeTokens({
            id_token: idToken,
            access_token: accessToken,
            refresh_token: refreshToken || '',
            expires_in: parseInt(expiresIn || '3600', 10),
            token_type: 'Bearer',
          });

          const user = parseIdTokenForUser(idToken);
          if (!user) {
            setError('Failed to parse user from token. Please try again.');
            setIsProcessing(false);
            return;
          }

          setAuthState({
            user,
            token: accessToken,
            isAuthenticated: true,
            isLoading: false,
          });

          navigate('/', { replace: true });
          return;
        }

        // Standard Cognito OAuth code exchange flow (email/password login)
        const code = searchParams.get('code');

        if (!code) {
          setError('No authorization code received. Please try logging in again.');
          setIsProcessing(false);
          return;
        }

        // Exchange code for tokens
        const loginResponse = await handleOAuthCallback(code);

        // Update auth state
        setAuthState({
          user: loginResponse.user,
          token: loginResponse.token,
          isAuthenticated: true,
          isLoading: false,
        });

        // Redirect to role-appropriate dashboard
        navigate('/', { replace: true });
      } catch (err) {
        console.error('OAuth callback error:', err);
        setError(
          err instanceof Error
            ? err.message
            : 'Authentication failed. Please try again.'
        );
        setIsProcessing(false);
      }
    }

    processCallback();
  }, [searchParams, navigate, setAuthState]);

  const handleRetryLogin = async () => {
    try {
      const loginUrl = await buildLoginUrl();
      window.location.href = loginUrl;
    } catch {
      setError('Failed to initiate login. Please refresh and try again.');
    }
  };

  if (isProcessing) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900">
        <div className="text-center">
          <Spinner size="lg" />
          <p className="mt-4 text-gray-600 dark:text-gray-300">
            Completing authentication...
          </p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-4">
        <div className="max-w-md w-full">
          <div className="bg-white dark:bg-gray-800 shadow rounded-lg p-6">
            <h1 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">
              Authentication Failed
            </h1>
            <Alert variant="error" className="mb-6">
              {error}
            </Alert>
            <div className="space-y-3">
              <Button onClick={handleRetryLogin} className="w-full">
                Try Again
              </Button>
              <Button
                variant="secondary"
                onClick={() => navigate('/')}
                className="w-full"
              >
                Go to Home
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return null;
}
