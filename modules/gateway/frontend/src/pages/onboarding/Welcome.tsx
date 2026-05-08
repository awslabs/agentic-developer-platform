import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { submitAccessRequest } from '@/services/onboarding';
import { clearAccessStatusCache } from '@/hooks/useAccessStatus';

export default function Welcome() {
  const { user } = useAuth();
  const navigate = useNavigate();

  const [motivation, setMotivation] = useState('');
  const [formError, setFormError] = useState<string | null>(null);
  const [collisionError, setCollisionError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setCollisionError(null);

    if (!motivation.trim()) {
      setFormError('Please provide a reason for requesting access.');
      return;
    }

    setIsSubmitting(true);
    try {
      // Server derives tenant_id, provider, provider_user_id from the JWT.
      // We only supply the reason.
      const response = await submitAccessRequest({
        motivation: motivation.trim(),
      });

      const data = await response.json().catch(() => ({}));

      if (response.status === 200 && data.status === 'approved') {
        clearAccessStatusCache();
        navigate(data.redirect || '/dashboard', { replace: true });
      } else if (response.status === 200 && data.status === 'pending') {
        clearAccessStatusCache();
        navigate('/onboarding/pending', { state: { requestId: data.request_id }, replace: true });
      } else if (response.status === 200 && data.status === 'collision') {
        setCollisionError(data.reason || 'A workspace with your account name already exists.');
      } else if (response.status === 200 && data.status === 'unavailable') {
        setUnavailable(true);
      } else if (response.status === 400) {
        setFormError(data.detail?.hint || data.message || 'Request was rejected.');
      } else {
        setFormError('An unexpected error occurred. Please try again.');
      }
    } catch {
      setFormError('Network error. Please check your connection and try again.');
    } finally {
      setIsSubmitting(false);
    }
  };

  if (unavailable) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <div className="w-full max-w-md rounded-lg border border-yellow-200 bg-yellow-50 p-8 text-center dark:border-yellow-800 dark:bg-yellow-900/20">
          <h1 className="text-xl font-semibold text-yellow-800 dark:text-yellow-200">
            Onboarding Not Available
          </h1>
          <p className="mt-3 text-yellow-700 dark:text-yellow-300">
            Onboarding is not yet enabled in this environment. Contact your administrator.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-lg rounded-lg border border-gray-200 bg-white p-8 shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Welcome to ADP
        </h1>
        <p className="mt-2 text-gray-600 dark:text-gray-400">
          Request access to get started. An administrator will review your request.
        </p>

        {/* User info */}
        <div className="mt-6 flex items-center gap-4 rounded-md bg-gray-50 p-4 dark:bg-gray-700/50">
          {user?.avatarUrl && (
            <img
              src={user.avatarUrl}
              alt=""
              className="h-12 w-12 rounded-full"
            />
          )}
          <div>
            {user?.githubLogin && (
              <p className="font-medium text-gray-900 dark:text-white">
                {user.githubLogin}
              </p>
            )}
            {user?.email && (
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {user.email}
              </p>
            )}
          </div>
        </div>

        <form onSubmit={handleSubmit} className="mt-6 space-y-5">
          {/* Motivation — the only field the user fills in */}
          <div>
            <label
              htmlFor="motivation"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Why do you need access?
            </label>
            <textarea
              id="motivation"
              value={motivation}
              onChange={(e) => {
                setMotivation(e.target.value);
                setFormError(null);
              }}
              rows={3}
              maxLength={500}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
              placeholder="Describe your use case..."
              aria-invalid={!!formError && !motivation.trim()}
            />
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {motivation.length}/500
            </p>
          </div>

          {collisionError && (
            <div
              role="alert"
              className="rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800 dark:border-yellow-800 dark:bg-yellow-900/20 dark:text-yellow-200"
            >
              {collisionError}
            </div>
          )}

          {formError && (
            <p className="text-sm text-red-600 dark:text-red-400" role="alert">
              {formError}
            </p>
          )}

          <button
            type="submit"
            disabled={isSubmitting}
            className="w-full rounded-md bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isSubmitting ? 'Submitting...' : 'Request Access'}
          </button>
        </form>
      </div>
    </div>
  );
}
