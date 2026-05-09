import { Outlet } from 'react-router-dom';
import { Navigation } from '@/components/Navigation';
import { MobileNav } from '@/components/MobileNav';
import { useAuth } from '@/hooks/useAuth';

export function MainLayout() {
  const { user, logout } = useAuth();

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      {/* Skip link for accessibility */}
      <a
        href="#main-content"
        className="skip-link focus:absolute focus:top-0 focus:left-0 focus:z-50 focus:p-4 focus:bg-primary-600 focus:text-white focus:opacity-100"
      >
        Skip to main content
      </a>

      {/* Header */}
      <header className="bg-white dark:bg-gray-800 shadow-sm sticky top-0 z-30">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between h-16">
            {/* Logo and mobile menu */}
            <div className="flex items-center gap-4">
              <MobileNav />
              <h1 className="text-xl font-bold text-gray-900 dark:text-white">
                Bedrock Gateway Admin
              </h1>
            </div>

            {/* User menu */}
            <div className="flex items-center gap-4">
              {user && (
                <div className="flex items-center gap-3">
                  {user.avatarUrl && (
                    <img
                      src={user.avatarUrl}
                      alt={user.githubLogin || user.name || 'User avatar'}
                      className="w-8 h-8 rounded-full"
                      data-testid="user-avatar"
                    />
                  )}
                  <span className="text-sm text-gray-600 dark:text-gray-400">
                    {user.githubLogin || user.name || user.email || user.id}
                  </span>
                  {user.role && (
                    <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-primary-100 text-primary-800 dark:bg-primary-900 dark:text-primary-100">
                      {user.role.replace('_', ' ')}
                    </span>
                  )}
                </div>
              )}
              <button
                onClick={logout}
                className="px-3 py-2 text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700 rounded-lg transition-colors"
              >
                Logout
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex gap-8">
          {/* Sidebar navigation (desktop only) */}
          <aside className="hidden lg:block w-64 flex-shrink-0">
            <div className="sticky top-24">
              <Navigation />
            </div>
          </aside>

          {/* Main content */}
          <main id="main-content" className="flex-1 min-w-0">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  );
}
