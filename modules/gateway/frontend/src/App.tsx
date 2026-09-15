import { Routes, Route, Navigate } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { MainLayout } from './layouts/MainLayout';
import { AuthLayout } from './layouts/AuthLayout';
import { ProtectedRoute } from './components/ProtectedRoute';
import { OnboardingGuard } from './components/OnboardingGuard';
import { FeatureGate } from './components/FeatureGate'; // Issue #3566
import { LoadingScreen } from './components/LoadingScreen';
import { ErrorBoundary } from './components/ErrorBoundary';
import { RoleBasedRedirect } from './components/RoleBasedRedirect';
import { DashboardRedirect } from './components/DashboardRedirect';
import { AdminGuard } from './components/AdminGuard';
// Issue #5079: imported EAGERLY, unlike every /next page below. It is the
// fallback for "a /next chunk failed to load", so it cannot itself be a chunk
// that might fail to load. It is a few lines of static markup.
import { NextUnavailable } from './components/next/NextUnavailable';
import { NextLoading } from './components/next/NextLoading';
// Issue #5079: the preview's own feature gate. Eager for the same reason — it is
// what decides whether any /next chunk is fetched at all.
import { NewUiGate } from './components/next/NewUiGate';

// Lazy load pages for code splitting
const Login = lazy(() => import('./pages/Login'));
const AuthCallback = lazy(() => import('./pages/AuthCallback'));
const PlatformDashboard = lazy(() => import('./pages/PlatformDashboard'));
const OrgDashboard = lazy(() => import('./pages/OrgDashboard'));
const DepartmentDashboard = lazy(() => import('./pages/DepartmentDashboard'));
const LogViewer = lazy(() => import('./pages/LogViewer'));
const ClaudeSetup = lazy(() => import('./pages/ClaudeSetup'));
const CliAuth = lazy(() => import('./pages/CliAuth')); // Web CLI login approval (login --web)
const AgentManagement = lazy(() => import('./pages/AgentManagement')); // Issue #119
const ModelAccess = lazy(() => import('./pages/ModelAccess'));
const RateLimitManagement = lazy(() => import('./pages/RateLimitManagement')); // Issue #185
const MyChats = lazy(() => import('./pages/MyChats')); // Issue #179
const AgentChat = lazy(() => import('./pages/AgentChat')); // Issue #97
const Connections = lazy(() => import('./pages/settings/Connections')); // Issue #465
const SettingsCredentials = lazy(() => import('./pages/settings/SettingsCredentials')); // Issue #562
const ConnectAws = lazy(() => import('./pages/settings/ConnectAws')); // Issue #562
const Welcome = lazy(() => import('./pages/onboarding/Welcome')); // Issue #545
const Pending = lazy(() => import('./pages/onboarding/Pending')); // Issue #545
const Denied = lazy(() => import('./pages/onboarding/Denied')); // Issue #545
const AccessRequests = lazy(() => import('./pages/admin/AccessRequests')); // Issue #545
const IndexingStatus = lazy(() => import('./pages/admin/IndexingStatus')); // Issue #1424
const TenantOrgLinks = lazy(() => import('./pages/admin/TenantOrgLinks')); // Issue #2954
const AdminOrganizations = lazy(() => import('./pages/admin/Organizations')); // Issue #4841
const AgentActivity = lazy(() => import('./pages/AgentActivity')); // Issue #1457
const AgentRunDashboard = lazy(() => import('./pages/AgentRunDashboard')); // Issue #3633
const BudgetSpend = lazy(() => import('./pages/BudgetSpend')); // Issue #4402
const Knowledge = lazy(() => import('./pages/Knowledge')); // Issue #1794
const GraphView = lazy(() => import('./pages/GraphView')); // Issue #4212
const FlowsList = lazy(() => import('./pages/FlowsList')); // Issue #4869
const NotFound = lazy(() => import('./pages/NotFound'));

// Opt-in new UI shell — Issue #5079 (NUI-01 of EPIC #5078).
//
// Lazily loaded like every other page, which is what keeps the preview isolated:
// an environment with `new_ui` off never fetches these chunks, and a failure to
// fetch them cannot affect the current UI's chunks.
const NextLayoutLazy = lazy(() =>
  import('./layouts/NextLayout').then((m) => ({ default: m.NextLayout })),
);
const NextHome = lazy(() => import('./pages/next/NextHome'));
// Issue #5080: the Administration journey's landing page. Whether an actor may be
// here is decided in NextLayout from the gated journey model, which redirects to
// the Use ADP home when no administration entry survives — so this chunk is only
// ever rendered for an actor with something in that journey.
const NextAdminHome = lazy(() => import('./pages/next/NextAdminHome'));
const NextNotFound = lazy(() => import('./pages/next/NextNotFound'));

function App() {
  return (
    <ErrorBoundary>
      <Suspense fallback={<LoadingScreen />}>
        <Routes>
          {/* Public routes */}
          <Route element={<AuthLayout />}>
            <Route path="/login" element={<Login />} />
          </Route>

          {/* OAuth callback route (must be outside ProtectedRoute) */}
          <Route path="/auth/callback" element={<AuthCallback />} />

          {/* Onboarding routes (authenticated but no tenant yet) — Issue #545 */}
          <Route
            element={
              <ProtectedRoute>
                <MainLayout />
              </ProtectedRoute>
            }
          >
            <Route path="/onboarding/welcome" element={<Welcome />} />
            <Route path="/onboarding/pending" element={<Pending />} />
            <Route path="/onboarding/denied" element={<Denied />} />
          </Route>

          {/* Protected routes with onboarding guard */}
          <Route
            element={
              <ProtectedRoute>
                <OnboardingGuard />
              </ProtectedRoute>
            }
          >
            <Route element={<MainLayout />}>
              <Route path="/" element={<RoleBasedRedirect />} />
              <Route path="/dashboard" element={<DashboardRedirect />} />
              <Route path="/admin/system" element={<FeatureGate feature="system_dashboard"><AdminGuard><PlatformDashboard /></AdminGuard></FeatureGate>} />
              <Route path="/org/:orgId" element={<OrgDashboard />} />
              <Route path="/org/:orgId/department/:deptId" element={<DepartmentDashboard />} />
              <Route path="/logs" element={<FeatureGate feature="logs"><LogViewer /></FeatureGate>} /> {/* Issue #3747 */}
              <Route path="/setup" element={<ClaudeSetup />} />
              <Route path="/cli-auth" element={<CliAuth />} /> {/* Web CLI login approval */}
              <Route path="/agents" element={<AgentManagement />} /> {/* Issue #119 */}
              <Route path="/budgets" element={<Navigate to="/budget?view=manage" replace />} />
              <Route path="/model-access" element={<ModelAccess />} /> {/* Issue #185 */}
              <Route path="/ratelimits" element={<RateLimitManagement />} /> {/* Issue #185 */}
              <Route path="/my-chats" element={<FeatureGate feature="chat"><MyChats /></FeatureGate>} /> {/* Issue #179 */}
              <Route path="/chat" element={<FeatureGate feature="chat"><AgentChat /></FeatureGate>} /> {/* Issue #97 */}
              <Route path="/settings/connections" element={<FeatureGate feature="connections"><Connections /></FeatureGate>} /> {/* Issue #465 */}
              <Route path="/settings/credentials" element={<FeatureGate feature="credentials"><SettingsCredentials /></FeatureGate>} /> {/* Issue #562 */}
              <Route path="/settings/credentials/aws/connect" element={<FeatureGate feature="credentials"><ConnectAws /></FeatureGate>} /> {/* Issue #562 */}
              <Route path="/admin/access-requests" element={<AccessRequests />} /> {/* Issue #545 */}
              <Route path="/admin/indexing" element={<FeatureGate feature="indexing"><IndexingStatus /></FeatureGate>} /> {/* Issue #1424 */}
              <Route path="/admin/tenant-links" element={<TenantOrgLinks />} /> {/* Issue #2954 */}
              {/* Issue #4841. No AdminGuard: an org admin legitimately reaches this panel to
                  manage their OWN org's departments and teams (those routes gate on
                  ORG_UPDATE scoped to target_org_id). The page itself hides the create-org
                  button, whose route is platform-admin-only. */}
              <Route path="/admin/organizations" element={<AdminOrganizations />} />
              <Route path="/activity" element={<AgentActivity />} /> {/* Issue #1457 */}
              <Route path="/runs" element={<AgentRunDashboard />} /> {/* Issue #3633 */}
              {/* Issue #4402. Reachable by a MEMBER: no permission guard, because the
                  endpoints behind it are scoped to the caller server-side and accept no
                  entity parameter. Gating on a permission members lack would ship the
                  screen invisible to exactly the people it was built for (#4389). */}
              <Route path="/budget" element={<BudgetSpend />} />
              <Route path="/knowledge" element={<FeatureGate feature="knowledge"><Knowledge /></FeatureGate>} /> {/* Issue #1794 */}
              {/* Issue #4212. `orchestration_engine` is fail-CLOSED in
                  ALL_FEATURES_ENABLED, so a pending or failed /features fetch hides
                  this route rather than revealing the new path. */}
              {/* Issue #4869. The list the nav entry points at; same gate as the
                  detail view below, so a flag-off environment has neither. React
                  Router v6 ranks routes by specificity rather than declaration
                  order, so `/flows` and `/flows/:flowId` do not compete. */}
              <Route path="/flows" element={<FeatureGate feature="orchestration_engine"><FlowsList /></FeatureGate>} />
              <Route path="/flows/:flowId" element={<FeatureGate feature="orchestration_engine"><GraphView /></FeatureGate>} />
            </Route>

            {/* Opt-in new UI — Issue #5079 (NUI-01 of EPIC #5078).

                A SIBLING of the MainLayout block above, inside the same
                ProtectedRoute + OnboardingGuard. That placement is the whole
                coexistence contract in one line: /next reuses the existing
                authentication and onboarding guards (so no second login, no
                token in a URL, expiry behaves identically), while rendering in
                its own layout instead of the current header and sidebar. Every
                existing path above is untouched and still serves its current-UI
                page.

                Three wrappers, outermost first, each for a distinct failure:

                1. NewUiGate — flag off (including a session that never read
                   /features, because `new_ui` is fail-closed) redirects to "/",
                   a working current-UI page. This is the documented rollback.
                   It is the preview's own gate rather than the shared
                   FeatureGate because it revalidates the flag on a bounded
                   interval: an operator's disable has to reach a tab that is
                   ALREADY inside the preview, not only tabs that reload.
                2. ErrorBoundary with the NextUnavailable fallback — a /next
                   chunk that fails to load, or any error thrown by a new-UI
                   page, renders a screen whose escape hatch is a hard link to
                   the current UI. It sits OUTSIDE the layout so that a failure
                   in NextLayout itself is still caught.
                3. Suspense — the loading state for the lazy chunks. It must be
                   INSIDE the boundary: a rejected lazy import surfaces through
                   Suspense and is caught by the boundary above it. */}
            <Route
              path="/next"
              element={
                <NewUiGate>
                  <ErrorBoundary fallback={<NextUnavailable />}>
                    <Suspense fallback={<NextLoading />}>
                      <NextLayoutLazy />
                    </Suspense>
                  </ErrorBoundary>
                </NewUiGate>
              }
            >
              <Route index element={<NextHome />} />
              {/* Issue #5080: the Administration journey. A real URL rather than
                  in-page state, so a deep link or a bookmark lands in the right
                  journey and browser Back moves between them. */}
              <Route path="admin" element={<NextAdminHome />} />
              {/* Scoped catch-all: an unknown /next path stays inside this
                  layout, so the persistent "Back to current UI" control is still
                  on screen. Without it the app-level 404 below would render with
                  no way back into the preview or out of it. */}
              <Route path="*" element={<NextNotFound />} />
            </Route>
          </Route>

          {/* 404 */}
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}

export default App;
