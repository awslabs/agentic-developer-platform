/**
 * OAuth Callback Page
 *
 * Handles the OAuth 2.0 authorization code callback from Cognito.
 * Extracts the authorization code from URL params, exchanges it for tokens
 * using the PKCE verifier, and redirects to the dashboard.
 */

import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { Spinner } from '@/components/ui/Spinner';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { handleOAuthCallback, buildLoginUrl, consumePostLoginRedirect } from '@/services/auth';

export default function AuthCallback() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { setAuthState } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(true);
  const processingStarted = useRef(false);

  useEffect(() => {
    // React StrictMode and rerenders must not consume the nonce twice.
    if (processingStarted.current) return;
    processingStarted.current = true;
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

        // Check if this is a callback from the GitHub auth broker (Issue #520)
        const brokerSource = searchParams.get('source');
        if (brokerSource === 'github_broker') {
          const {
            storeTokens,
            parseIdTokenForUser,
            getBrokerState,
            exchangeBrokerCode,
          } = await import('@/services/auth');

          // Issue #4133: verify this callback belongs to a login THIS browser
          // started. Without it, an attacker-crafted callback URL drops their
          // session into the victim's browser (login CSRF / session fixation).
          // Single-use: getBrokerState clears as it reads.
          const storedState = getBrokerState();
          const returnedState = searchParams.get('state');
          const brokerCode = searchParams.get('code');

          // Scrub callback material on rejection as well as success.
          window.history.replaceState({}, '', '/auth/callback');
          // Every transport must belong to a login started in this browser.
          // getBrokerState consumes the nonce before any token exchange/storage.
          if (!storedState || !returnedState || storedState !== returnedState) {
            setError('This sign-in link did not come from a login started in this browser. Please try again.');
            setIsProcessing(false);
            return;
          }

          let tokens: Awaited<ReturnType<typeof exchangeBrokerCode>>;

          if (brokerCode) {
            tokens = await exchangeBrokerCode(brokerCode, storedState);
          } else {
            const idToken = searchParams.get('id_token');
            const accessToken = searchParams.get('access_token');

            if (!idToken || !accessToken) {
              setError('Invalid broker response — missing tokens. Please try again.');
              setIsProcessing(false);
              return;
            }

            tokens = {
              id_token: idToken,
              access_token: accessToken,
              refresh_token: searchParams.get('refresh_token') || '',
              expires_in: parseInt(searchParams.get('expires_in') || '3600', 10),
              token_type: 'Bearer',
            };
          }

          const user = parseIdTokenForUser(tokens.id_token);
          if (!user) {
            setError('Failed to parse user from token. Please try again.');
            setIsProcessing(false);
            return;
          }

          storeTokens(tokens);

          setAuthState({
            user,
            token: tokens.access_token,
            isAuthenticated: true,
            isLoading: false,
          });

          // Return to the deep link the user was originally headed to (e.g.
          // the CLI approval page), falling back to the dashboard.
          navigate(consumePostLoginRedirect() ?? '/', { replace: true });
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

        // Return to the original deep link, else the role-appropriate dashboard
        navigate(consumePostLoginRedirect() ?? '/', { replace: true });
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
