import { NavLink } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';
import { GITLAB_PATH, handleGitlabSsoClick } from '@/services/gitlabSso';

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

// Compact line icons keep the current navigation readable in the Blueprint shell.
// The labels and route permissions remain the source of truth for each item.
const iconPaths: Record<string, string> = {
  '📊': 'M4 20v-7h4v7M10 20V4h4v16M16 20v-10h4v10',
  '🔄': 'M20 7v5h-5M4 17v-5h5M5.5 9A7 7 0 0 1 18 7l2 5M4 12l2 5a7 7 0 0 0 12.5-2',
  '📈': 'M3 19h18M4 15l5-5 4 3 7-8M16 5h4v4',
  '🏢': 'M4 20V5h11v15M15 10h5v10M3 20h18M7 8h2M11 8h2M7 12h2M11 12h2M7 16h2M11 16h2',
  '👥': 'M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6M22 20v-2a4 4 0 0 0-3-3.87M16 4.13a3 3 0 0 1 0 5.74',
  '📝': 'M5 3h10l4 4v14H5zM14 3v5h5M8 12h8M8 16h7',
  '🤖': 'M12 3v3M8 3h8M5 7h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2M8 13h.01M16 13h.01M8 17h8',
  '🔀': 'M4 5a2 2 0 1 0 0 4 2 2 0 0 0 0-4M20 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4M20 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4M6 7h6a5 5 0 0 1 5 5v7M6 7h6a5 5 0 0 0 5-2',
  '⏱️': 'M12 8v5l3 2M9 2h6M12 4a9 9 0 1 0 0 18 9 9 0 0 0 0-18',
  '📚': 'M4 4h7a3 3 0 0 1 3 3v14H7a3 3 0 0 0-3 1zM14 7a3 3 0 0 1 3-3h3v18a3 3 0 0 0-3-1h-3',
  '📋': 'M8 4h8M9 2h6v4H9zM6 4H4v18h16V4h-2M8 11h8M8 15h8M8 19h5',
  '💵': 'M3 6h18v14H3zM3 10h18M16 15h3M6 3h12',
  '🛩️': 'M3 12 21 3l-6 18-3-8-9-1zM12 13l9-10',
  '💬': 'M4 4h16v12H9l-5 4zM8 9h8M8 12h5',
  '⚙️': 'M4 6h16M4 12h16M4 18h16M9 6a2 2 0 1 0 0 .01M16 12a2 2 0 1 0 0 .01M8 18a2 2 0 1 0 0 .01',
  '🔗': 'M9 15l6-6M7 10l-2 2a4 4 0 0 0 6 6l2-2M17 14l2-2a4 4 0 0 0-6-6l-2 2',
  '🔑': 'M14 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M10 12H3v3h3v3h3l2-3',
  '🧠': 'M12 3v18M12 5a4 4 0 0 0-7 3 4 4 0 0 0 0 7 4 4 0 0 0 7 4M12 5a4 4 0 0 1 7 3 4 4 0 0 1 0 7 4 4 0 0 1-7 4',
  '🖥️': 'M3 4h18v14H3zM8 22h8M12 18v4',
  '🔍': 'M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16M17 17l4 4',
};

function NavIcon({ icon }: { icon: string }) {
  return (
    <svg className="blueprint-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={iconPaths[icon] ?? iconPaths['📊']} />
    </svg>
  );
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
      // Issue #4841: relabelled from "Organizations" to disambiguate from the structural
      // panel added below. This entry is an anchor into the system dashboard's
      // "Top Organizations (24h)" USAGE section; the new entry is where orgs, departments
      // and teams are actually created and managed. Two links both labelled
      // "Organizations" gave no way to tell which one did what.
      navItems.push({ to: '/admin/system#organizations', label: 'Org Usage', icon: '📊' });
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

  // Routing configuration has its own destination; budgets share /budget.
  if (canViewBudgets() && isPlatformAdmin()) {
    navItems.push({ to: '/model-access', label: 'Model access', icon: '🔀' });
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

  // Delivery Flows — the orchestration engine's entry point (Issue #4869).
  //
  // Feature-gated because `orchestration_engine` is a per-environment opt-in and
  // `ALL_FEATURES_ENABLED` has it fail-CLOSED (#4209): an environment not running
  // the engine must not advertise a menu item whose route redirects away.
  //
  // Deliberately UNGATED by permission, like /budget above. The endpoint behind it
  // requires USAGE_READ, which is exactly what a MEMBER has — and that permission
  // resolves to [] on the ID-token path (#4389), so a client-side check would hide
  // the page from the operators it exists for.
  if (features.orchestration_engine) {
    navItems.push({ to: '/flows', label: 'Delivery Flows', icon: '🔀' });
  }

  // Personal spend stays feature-gated for members. Administrators retain their
  // existing budget-management access when that personal feature is disabled.
  if (features.budget_spend || (canViewBudgets() && (isPlatformAdmin() || isOrgAdmin()))) {
    navItems.push({ to: '/budget', label: 'Budget & Spend', icon: '💵' });
  }

  // Superplane domain app (Issue #5037, EPIC #4910).
  //
  // Feature-gated because `superplane` is fail-CLOSED in `ALL_FEATURES_ENABLED`: an
  // environment not running the domain app must not advertise a menu item whose route
  // redirects straight back to the dashboard. Deliberately UNGATED by permission —
  // nothing tenant-scoped ships behind this flag yet, so there is no permission to
  // check, and inventing one here would hide the entry from the operators enabling it.
  if (features.superplane) {
    navItems.push({ to: '/superplane', label: 'Superplane', icon: '🛩️' });
  }

  // Agent Chat for all authenticated users (Issue #97)
  if (features.chat) {
    navItems.push({ to: '/chat', label: 'Agent Chat', icon: '🤖' });
  }

  // My Chats page for all authenticated users (Issue #179)
  if (features.chat) {
    navItems.push({ to: '/my-chats', label: 'My Chats', icon: '💬' });
  }

  // Setup page for all authenticated users
  // Label is "CLI Setup", not "Claude Code Setup": the page covers Codex too
  // (Issue #4159). Route is unchanged.
  navItems.push({ to: '/setup', label: 'CLI Setup', icon: '⚙️' });

  // Connections page — link external services (Issue #465)
  if (features.connections) {
    navItems.push({ to: '/settings/connections', label: 'Connections', icon: '🔗' });
  }

  // Credentials — user vault + connected AWS accounts (Issue #562)
  if (features.credentials) {
    navItems.push({ to: '/settings/credentials', label: 'Credentials', icon: '🔑' });
  }

  // Per-persona model preferences are personal by default; the page itself
  // offers server-authorized managed-service scopes when any exist (#5422).
  if (features.agent_models) {
    navItems.push({ to: '/settings/agent-models', label: 'Agent Models', icon: '🧠' });
  }

  // System Health (demoted proxy dashboard) for platform admins (Issue #3634)
  if (features.system_dashboard && isPlatformAdmin()) {
    navItems.push({ to: '/admin/system', label: 'System Health', icon: '🖥️' });
  }

  // Access Requests page (Issue #545; org-scoped in #4018)
  //
  // Org admins see the link too: they can review the join-my-org requests
  // targeting their own tenant. This check is COSMETIC only — it reads the
  // `custom:role` ID-token claim, which is a display hint and confers no
  // authority. The real control is server-side: every /admin/access-requests
  // route gates on Permission.USER_MANAGE resolved from tenant_memberships,
  // and new-tenant (class-A) requests stay platform-admin-only there. A member
  // who forges the claim to reveal this link still gets 403 from the API.
  if (isPlatformAdmin() || isOrgAdmin()) {
    navItems.push({ to: '/admin/access-requests', label: 'Access Requests', icon: '📋' });
  }

  // Indexing Status page for platform admins (Issue #1424)
  if (features.indexing && isPlatformAdmin()) {
    navItems.push({ to: '/admin/indexing', label: 'Indexing Status', icon: '🔍' });
  }

  // Legacy Tenant Org Links menu, controlled per deployment.
  if (features.tenant_org_links && isPlatformAdmin()) {
    navItems.push({ to: '/admin/tenant-links', label: 'Tenant Org Links', icon: '🏢' });
  }

  // Organizations structure panel — orgs, departments, teams (Issue #4841).
  //
  // Gated on ORG_READ, which is the permission the underlying list route enforces, so an
  // ORG ADMIN sees this link too: the server filters the list to their own org and the
  // dept/team write routes gate on ORG_UPDATE scoped to target_org_id. Creating an
  // organization is platform-admin-only (the identity router is `require_admin`), so that
  // button — not this link — carries the narrower gate. This check is COSMETIC: the server
  // is the boundary on every route the panel calls.
  if (canViewOrganizations()) {
    navItems.push({ to: '/admin/organizations', label: 'Organizations', icon: '🏢' });
  }

  return (
    <nav className="blueprint-nav flex flex-col gap-1" aria-label="Main navigation">
      {navItems.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          className={({ isActive }) => `blueprint-nav-link flex items-center gap-3 px-3 py-2 rounded-md transition-colors ${isActive ? 'blueprint-nav-active' : ''}`}
        >
          <NavIcon icon={item.icon} />
          <span>{item.label}</span>
        </NavLink>
      ))}
      {/* External: GitLab SSO (Issue #3775, Wave 2).
          `/gitlab/` is a server path with an authenticated handoff, not a client
          route: /api/auth/gitlab-sso mints an RS256 JWT and 302s to GitLab's JWT
          callback. A plain <a href> cannot carry the Bearer token (sessionStorage),
          so the click handler does the fetch and follows the redirect, falling back
          to plain /gitlab/ navigation on any failure. The handler now lives in
          services/gitlabSso.ts because the new UI needs the same behaviour (#5123).
          Feature-gated: fail-closed behind FEATURE_GITLAB_ENABLED (Issue #3773). */}
      {features.gitlab && (
        <a
          href={GITLAB_PATH}
          onClick={handleGitlabSsoClick}
          className="blueprint-nav-link flex items-center gap-3 px-3 py-2 rounded-md transition-colors"
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
