import { NavLink } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';
import { getAccessToken } from '@/services/auth';

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

export function Navigation() {
  const {
    isPlatformAdmin,
    isOrgAdmin,
    isDeptAdmin,
    user,
    canViewOrganizations,
    canViewLogs,
    canViewMetrics,
    canViewPool,
    canViewBudgets,
    canViewRateLimits,
  } = usePermissions();
  const features = useFeatures();

  const navItems: NavItem[] = [];

  // Dashboard link — all users see this, points to /runs (Issue #3634)
  navItems.push({ to: '/runs', label: 'Dashboard', icon: '📊' });

  // Platform admin sees org/pool/metrics links (now under /admin/system — Issue #3634).
  // These are anchors INTO the system dashboard, so they share its feature gate.
  if (features.system_dashboard && isPlatformAdmin()) {
    if (canViewOrganizations()) {
      navItems.push({ to: '/admin/system#organizations', label: 'Organizations', icon: '🏢' });
    }
    if (canViewPool()) {
      navItems.push({ to: '/admin/system#pool', label: 'Pool Health', icon: '🔄' });
    }
    if (canViewMetrics()) {
      navItems.push({ to: '/admin/system#metrics', label: 'System Metrics', icon: '📈' });
    }
  }

  // Org admin sees their org dashboard
  if (isOrgAdmin() && user?.orgId) {
    navItems.push(
      { to: `/org/${user.orgId}`, label: 'My Organization', icon: '🏢' },
    );
  }

  // Dept admin sees their department
  if (isDeptAdmin() && user?.orgId && user?.deptId) {
    navItems.push(
      { to: `/org/${user.orgId}/department/${user.deptId}`, label: 'My Department', icon: '👥' },
    );
  }

  // Everyone with log access can see logs (feature-gated — Issue #3747)
  if (features.logs && canViewLogs()) {
    navItems.push({ to: '/logs', label: 'Logs', icon: '📝' });
  }

  // Agent management for org admins (Issue #119)
  if (isOrgAdmin()) {
    navItems.push({ to: '/agents', label: 'Agents', icon: '🤖' });
  }

  // Budget management for org admins (Issue #185)
  if (canViewBudgets() && (isPlatformAdmin() || isOrgAdmin())) {
    navItems.push({ to: '/budgets', label: 'Budgets', icon: '💰' });
  }

  // Rate limit management for org admins (Issue #185)
  if (canViewRateLimits() && (isPlatformAdmin() || isOrgAdmin())) {
    navItems.push({ to: '/ratelimits', label: 'Rate Limits', icon: '⏱️' });
  }

  // Knowledge management for all authenticated users (Issue #1794)
  if (features.knowledge) {
    navItems.push({ to: '/knowledge', label: 'Knowledge', icon: '📚' });
  }

  // Agent Activity for all authenticated users (Issue #1457)
  navItems.push({ to: '/activity', label: 'Agent Activity', icon: '📋' });

  // Agent Chat for all authenticated users (Issue #97)
  if (features.chat) {
    navItems.push({ to: '/chat', label: 'Agent Chat', icon: '🤖' });
  }

  // My Chats page for all authenticated users (Issue #179)
  if (features.chat) {
    navItems.push({ to: '/my-chats', label: 'My Chats', icon: '💬' });
  }

  // Setup page for all authenticated users
  navItems.push({ to: '/setup', label: 'Claude Code Setup', icon: '⚙️' });

  // Connections page — link external services (Issue #465)
  if (features.connections) {
    navItems.push({ to: '/settings/connections', label: 'Connections', icon: '🔗' });
  }

  // Credentials — user vault + connected AWS accounts (Issue #562)
  if (features.credentials) {
    navItems.push({ to: '/settings/credentials', label: 'Credentials', icon: '🔑' });
  }

  // System Health (demoted proxy dashboard) for platform admins (Issue #3634)
  if (features.system_dashboard && isPlatformAdmin()) {
    navItems.push({ to: '/admin/system', label: 'System Health', icon: '🖥️' });
  }

  // Access Requests page for platform admins (Issue #545)
  if (isPlatformAdmin()) {
    navItems.push({ to: '/admin/access-requests', label: 'Access Requests', icon: '📋' });
  }

  // Indexing Status page for platform admins (Issue #1424)
  if (features.indexing && isPlatformAdmin()) {
    navItems.push({ to: '/admin/indexing', label: 'Indexing Status', icon: '🔍' });
  }

  // Tenant Org Links page for platform admins (Issue #2954)
  if (isPlatformAdmin()) {
    navItems.push({ to: '/admin/tenant-links', label: 'Tenant Org Links', icon: '🏢' });
  }

  return (
    <nav className="flex flex-col gap-1" aria-label="Main navigation">
      {navItems.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          className={({ isActive }) =>
            `flex items-center gap-3 px-4 py-2 rounded-lg transition-colors ${
              isActive
                ? 'bg-primary-100 text-primary-700 dark:bg-primary-900 dark:text-primary-100'
                : 'text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'
            }`
          }
        >
          <span className="text-xl" aria-hidden="true">
            {item.icon}
          </span>
          <span>{item.label}</span>
        </NavLink>
      ))}
      {/* External: GitLab SSO (Issue #3775, Wave 2).
          Uses the /api/auth/gitlab-sso endpoint which mints an RS256 JWT and
          302-redirects to GitLab's JWT callback. A plain <a href> cannot carry
          the Bearer token (stored in sessionStorage), so we use a click handler
          that fetches with credentials and navigates to the redirect URL.
          Falls back to /gitlab/ direct navigation if SSO endpoint is unavailable.
          Feature-gated: fail-closed behind FEATURE_GITLAB_ENABLED (Issue #3773). */}
      {features.gitlab && (
        <a
          href="/gitlab/"
          onClick={(e) => {
            const token = getAccessToken();
            if (!token) return; // Let the default href navigate
            e.preventDefault();
            // Fetch the SSO endpoint with auth — redirect: manual lets us
            // read the Location header from the 302 response.
            fetch('/api/auth/gitlab-sso', {
              headers: { Authorization: `Bearer ${token}` },
              redirect: 'manual',
            }).then((res) => {
              // With redirect: manual, a 302 becomes type "opaqueredirect"
              // and we cannot read the Location header due to CORS.
              // Instead, re-fetch with redirect: follow — fetch will follow
              // the 302 to the GitLab callback and we get the final URL.
              if (res.type === 'opaqueredirect') {
                // Cannot read Location; re-request letting fetch follow
                return fetch('/api/auth/gitlab-sso', {
                  headers: { Authorization: `Bearer ${token}` },
                  redirect: 'follow',
                });
              }
              return res;
            }).then((res) => {
              if (res && res.redirected && res.url) {
                // fetch followed the 302 — navigate to the final URL
                window.location.href = res.url;
              } else if (res && res.ok) {
                // Unexpected 200 — may have followed redirect already
                window.location.href = '/gitlab/';
              } else {
                // SSO endpoint unavailable (404/503) — fall back
                window.location.href = '/gitlab/';
              }
            }).catch(() => {
              // Network error — fall back to direct navigation
              window.location.href = '/gitlab/';
            });
          }}
          className="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
        >
          <span className="text-xl" aria-hidden="true">
            🦊
          </span>
          <span>GitLab</span>
        </a>
      )}
    </nav>
  );
}
