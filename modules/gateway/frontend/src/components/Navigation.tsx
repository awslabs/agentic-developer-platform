import { NavLink } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';

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
      {/* External: GitLab (full page navigation, not SPA route).
          Trailing slash is required: the CloudFront behavior pattern is
          /gitlab/* which does NOT match the bare /gitlab — that falls
          through to the S3/SPA default behavior and 404s.
          Feature-gated: fail-closed behind FEATURE_GITLAB_ENABLED (Issue #3773). */}
      {features.gitlab && (
        <a
          href="/gitlab/"
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
