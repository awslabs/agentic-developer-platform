import { WorkspaceSelector } from '@/components/WorkspaceSelector';
import { Outlet } from 'react-router-dom';
import { Navigation } from '@/components/Navigation';
import { MobileNav } from '@/components/MobileNav';
import { NoOrgBanner } from '@/components/NoOrgBanner';
import { TryNewUiLink } from '@/components/TryNewUiLink';
import { useAuth } from '@/hooks/useAuth';

/** Well-known org ID for the adp-default free-tier tenant. */
const ADP_DEFAULT_ORG_ID = '00000000-0000-4000-a000-000000000001';

export function MainLayout() {
  const { user, logout } = useAuth();
  const isNoOrg = user?.orgId === ADP_DEFAULT_ORG_ID;

  return (
    <div className="blueprint-ui min-h-screen">
      {/* Skip link for accessibility */}
      <a
        href="#main-content"
        className="skip-link focus:absolute focus:top-0 focus:left-0 focus:z-50 focus:p-4 focus:bg-primary-600 focus:text-white focus:opacity-100"
      >
        Skip to main content
      </a>

      {/* Header */}
      <header className="blueprint-header sticky top-0 z-30">
        <div className="px-4 sm:px-6">
          <div className="flex min-h-16 flex-wrap items-center justify-between gap-x-4 gap-y-2 py-2">
            {/* Logo and mobile menu */}
            <div className="flex min-w-0 flex-wrap items-center gap-4">
              <MobileNav />
              <h1 className="blueprint-brand text-xl font-bold">
                <span className="blueprint-brand-mark" aria-hidden="true">ADP</span>
                Agentic Developer Platform
              </h1>
            </div>

            {/* User menu */}
            <div className="flex min-w-0 flex-wrap items-center gap-4">
              {/* Issue #5079: opt-in entry into the /next preview. Renders null
                  unless the fail-closed `new_ui` flag is on, so this header is
                  byte-identical to before in every environment that has not
                  enabled the preview. */}
              <TryNewUiLink />
              {user && (
                <div className="flex min-w-0 max-w-full flex-wrap items-center gap-3">
                  {user.avatarUrl && (
                    <img
                      src={user.avatarUrl}
                      alt={user.githubLogin || user.name || 'User avatar'}
                      className="w-8 h-8 rounded-full"
                      data-testid="user-avatar"
                    />
                  )}
                  <span className="min-w-0 break-all text-sm text-gray-600 dark:text-gray-400">
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
          {user && <WorkspaceSelector />}
        </div>
      </header>

      <div className="blueprint-content">
        <div className="flex min-h-[calc(100vh-5rem)]">
          {/* Sidebar navigation (desktop only) */}
          <aside className="blueprint-sidebar hidden lg:block flex-shrink-0">
            <div className="sticky top-24 py-5 px-3">
              <Navigation />
            </div>
          </aside>

          {/* Main content */}
          <main id="main-content" className="blueprint-main flex-1 min-w-0">
            <div className="blueprint-main-inner">
              {/* Issue #2984: No-org banner for personal/free-tier tenant users */}
              {isNoOrg && <NoOrgBanner />}
              <Outlet />
            </div>
          </main>
        </div>
      </div>
    </div>
  );
}
