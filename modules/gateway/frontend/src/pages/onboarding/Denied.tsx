import { useAuth } from '@/hooks/useAuth';
import { useAccessStatus, clearAccessStatusCache } from '@/hooks/useAccessStatus';

export default function Denied() {
  const { logout } = useAuth();
  const { decisionNote } = useAccessStatus();

  const handleSignOut = async () => {
    clearAccessStatusCache();
    await logout();
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-8 text-center shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-red-100 dark:bg-red-900/30">
          <svg
            className="h-8 w-8 text-red-600 dark:text-red-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </div>

        <h1 className="mt-4 text-xl font-semibold text-gray-900 dark:text-white">
          Access Not Approved
        </h1>
        <p className="mt-2 text-gray-600 dark:text-gray-400">
          Access to this platform was not approved. If you believe this is a mistake,
          contact the administrator who reviews onboarding requests.
        </p>

        {decisionNote && (
          <div className="mt-4 rounded-md bg-gray-50 p-3 text-left dark:bg-gray-700/50">
            <p className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Admin note:
            </p>
            <p className="mt-1 text-sm text-gray-700 dark:text-gray-300">
              {decisionNote}
            </p>
          </div>
        )}

        <div className="mt-6">
          <button
            onClick={handleSignOut}
            className="w-full rounded-md bg-gray-600 px-4 py-2 text-sm font-medium text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-gray-500 focus:ring-offset-2"
          >
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
