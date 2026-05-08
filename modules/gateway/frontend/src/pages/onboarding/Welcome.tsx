import { useState, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { submitAccessRequest } from '@/services/onboarding';
import { clearAccessStatusCache } from '@/hooks/useAccessStatus';

const TENANT_ID_REGEX = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/;
const RESERVED_NAMES = ['admin', 'platform', 'system', 'api', 'www', 'app', 'internal'];

function slugify(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9-]/g, '-');
}

function validateTenantId(value: string): string | null {
  if (!value) return 'Tenant ID is required';
  if (value.length < 3) return 'Tenant ID must be at least 3 characters';
  if (value.length > 64) return 'Tenant ID must be at most 64 characters';
  if (!TENANT_ID_REGEX.test(value)) {
    return 'Must start and end with a letter or number, and contain only lowercase letters, numbers, and hyphens';
  }
  if (RESERVED_NAMES.includes(value)) {
    return 'This name is reserved. Please choose another.';
  }
  return null;
}

export default function Welcome() {
  const { user } = useAuth();
  const navigate = useNavigate();

  const defaultTenantId = useMemo(
    () => (user?.githubLogin ? slugify(user.githubLogin) : ''),
    [user?.githubLogin]
  );

  const [tenantId, setTenantId] = useState(defaultTenantId);
  const [motivation, setMotivation] = useState('');
  const [tenantError, setTenantError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  const handleTenantIdChange = (value: string) => {
    setTenantId(value);
    setTenantError(null);
    setFormError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    setTenantError(null);

    // Client-side validation
    const validationError = validateTenantId(tenantId);
    if (validationError) {
      setTenantError(validationError);
      return;
    }
    if (!motivation.trim()) {
      setFormError('Please provide a reason for requesting access.');
      return;
    }

    setIsSubmitting(true);
    try {
      const response = await submitAccessRequest({
        proposed_tenant_id: tenantId,
        motivation: motivation.trim(),
      });

      if (response.status === 200) {
        // Auto-approved
        clearAccessStatusCache();
        const data = await response.json();
        navigate(data.redirect || '/dashboard', { replace: true });
      } else if (response.status === 202) {
        // Pending
        const data = await response.json();
        clearAccessStatusCache();
        navigate('/onboarding/pending', { state: { requestId: data.request_id }, replace: true });
      } else if (response.status === 400) {
        const data = await response.json();
        setTenantError(data.hint || 'Invalid tenant ID');
      } else if (response.status === 409) {
        setTenantError('This tenant name is already taken. Pick another.');
      } else if (response.status === 503) {
        setUnavailable(true);
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
          Request access to get started.
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
          {/* Tenant ID */}
          <div>
            <label
              htmlFor="tenant-id"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Workspace ID
            </label>
            <input
              id="tenant-id"
              type="text"
              value={tenantId}
              onChange={(e) => handleTenantIdChange(e.target.value)}
              className={`mt-1 block w-full rounded-md border px-3 py-2 text-sm shadow-sm focus:outline-none focus:ring-2 focus:ring-primary-500 dark:bg-gray-700 dark:text-white ${
                tenantError
                  ? 'border-red-300 dark:border-red-600'
                  : 'border-gray-300 dark:border-gray-600'
              }`}
              placeholder="my-workspace"
              aria-describedby={tenantError ? 'tenant-id-error' : undefined}
              aria-invalid={!!tenantError}
            />
            {tenantError && (
              <p id="tenant-id-error" className="mt-1 text-sm text-red-600 dark:text-red-400">
                {tenantError}
              </p>
            )}
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Lowercase letters, numbers, and hyphens. 3-64 characters.
            </p>
          </div>

          {/* Motivation */}
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
