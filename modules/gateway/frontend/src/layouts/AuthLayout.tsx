import { Outlet, Navigate } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { LoadingScreen } from '@/components/LoadingScreen';

export function AuthLayout() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <LoadingScreen />;
  }

  // Redirect to role-appropriate dashboard if already authenticated
  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-primary-50 to-primary-100 dark:from-gray-900 dark:to-gray-800 p-4">
      <div className="max-w-md w-full">
        {/* Logo/header */}
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold text-gray-900 dark:text-white mb-2">
            Bedrock Gateway
          </h1>
          <p className="text-gray-600 dark:text-gray-400">
            Admin Console
          </p>
        </div>

        {/* Auth form content */}
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow-lg p-8">
          <Outlet />
        </div>

        {/* Footer */}
        <p className="text-center text-sm text-gray-500 dark:text-gray-400 mt-8">
          Secure access powered by AWS SSO
        </p>
      </div>
    </div>
  );
}
