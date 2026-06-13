import { NavLink } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';

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

  const navItems: NavItem[] = [];

  // Platform admin sees dashboard and all orgs
  if (isPlatformAdmin()) {
    navItems.push(
      { to: '/dashboard', label: 'Dashboard', icon: '📊' },
    );
    if (canViewOrganizations()) {
      navItems.push({ to: '/dashboard#organizations', label: 'Organizations', icon: '🏢' });
    }
    if (canViewPool()) {
      navItems.push({ to: '/dashboard#pool', label: 'Pool Health', icon: '🔄' });
    }
    if (canViewMetrics()) {
      navItems.push({ to: '/dashboard#metrics', label: 'System Metrics', icon: '📈' });
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

  // Everyone with log access can see logs
  if (canViewLogs()) {
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

  // Agent Chat for all authenticated users (Issue #97)
  navItems.push({ to: '/chat', label: 'Agent Chat', icon: '🤖' });

  // My Chats page for all authenticated users (Issue #179)
  navItems.push({ to: '/my-chats', label: 'My Chats', icon: '💬' });

  // Setup page for all authenticated users
  navItems.push({ to: '/setup', label: 'Claude Code Setup', icon: '⚙️' });

  // Connections page — link external services (Issue #465)
  navItems.push({ to: '/settings/connections', label: 'Connections', icon: '🔗' });

  // Credentials — user vault + connected AWS accounts (Issue #562)
  navItems.push({ to: '/settings/credentials', label: 'Credentials', icon: '🔑' });

  // Access Requests page for platform admins (Issue #545)
  if (isPlatformAdmin()) {
    navItems.push({ to: '/admin/access-requests', label: 'Access Requests', icon: '📋' });
  }

  // Indexing Status page for platform admins (Issue #1424)
  if (isPlatformAdmin()) {
    navItems.push({ to: '/admin/indexing', label: 'Indexing Status', icon: '🔍' });
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
    </nav>
  );
}
