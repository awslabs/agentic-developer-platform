import { useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { getAccessStatus } from '@/services/onboarding';
import { clearAccessStatusCache } from '@/hooks/useAccessStatus';

const POLL_INTERVAL_MS = 30_000; // 30 seconds

export default function Pending() {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const checkStatus = useCallback(async () => {
    try {
      const data = await getAccessStatus();
      if (data.status === 'registered') {
        clearAccessStatusCache();
        navigate('/dashboard', { replace: true });
      } else if (data.status === 'denied') {
        clearAccessStatusCache();
        navigate('/onboarding/denied', { replace: true });
      }
    } catch {
      // Ignore errors during polling — will retry on next interval
    }
  }, [navigate]);

  useEffect(() => {
    intervalRef.current = setInterval(checkStatus, POLL_INTERVAL_MS);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [checkStatus]);

  const handleRefresh = () => {
    checkStatus();
  };

  const handleSignOut = async () => {
    clearAccessStatusCache();
    await logout();
  };

  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-md rounded-lg border border-gray-200 bg-white p-8 text-center shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-blue-100 dark:bg-blue-900/30">
          <svg
            className="h-8 w-8 text-blue-600 dark:text-blue-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
        </div>

        <h1 className="mt-4 text-xl font-semibold text-gray-900 dark:text-white">
          Request Under Review
        </h1>
        <p className="mt-2 text-gray-600 dark:text-gray-400">
          Your access request has been submitted and is awaiting admin approval.
          Expected response: ~24 hours.
        </p>

        <div className="mt-6 space-y-3">
          <button
            onClick={handleRefresh}
            className="w-full rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
          >
            Check Status
          </button>
          <button
            onClick={handleSignOut}
            className="w-full rounded-md px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700 focus:outline-none dark:text-gray-400 dark:hover:text-gray-200"
          >
            Sign out
          </button>
        </div>

        <p className="mt-4 text-xs text-gray-400 dark:text-gray-500">
          This page checks for updates automatically every 30 seconds.
        </p>
      </div>
    </div>
  );
}
