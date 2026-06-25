import { Routes, Route } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { MainLayout } from './layouts/MainLayout';
import { AuthLayout } from './layouts/AuthLayout';
import { ProtectedRoute } from './components/ProtectedRoute';
import { OnboardingGuard } from './components/OnboardingGuard';
import { LoadingScreen } from './components/LoadingScreen';
import { ErrorBoundary } from './components/ErrorBoundary';
import { RoleBasedRedirect } from './components/RoleBasedRedirect';

// Lazy load pages for code splitting
const Login = lazy(() => import('./pages/Login'));
const AuthCallback = lazy(() => import('./pages/AuthCallback'));
const PlatformDashboard = lazy(() => import('./pages/PlatformDashboard'));
const OrgDashboard = lazy(() => import('./pages/OrgDashboard'));
const DepartmentDashboard = lazy(() => import('./pages/DepartmentDashboard'));
const LogViewer = lazy(() => import('./pages/LogViewer'));
const ClaudeSetup = lazy(() => import('./pages/ClaudeSetup'));
const AgentManagement = lazy(() => import('./pages/AgentManagement')); // Issue #119
const BudgetManagement = lazy(() => import('./pages/BudgetManagement')); // Issue #185
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
const AgentActivity = lazy(() => import('./pages/AgentActivity')); // Issue #1457
const Knowledge = lazy(() => import('./pages/Knowledge')); // Issue #1794
const NotFound = lazy(() => import('./pages/NotFound'));

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
              <Route path="/dashboard" element={<PlatformDashboard />} />
              <Route path="/org/:orgId" element={<OrgDashboard />} />
              <Route path="/org/:orgId/department/:deptId" element={<DepartmentDashboard />} />
              <Route path="/logs" element={<LogViewer />} />
              <Route path="/setup" element={<ClaudeSetup />} />
              <Route path="/agents" element={<AgentManagement />} /> {/* Issue #119 */}
              <Route path="/budgets" element={<BudgetManagement />} /> {/* Issue #185 */}
              <Route path="/ratelimits" element={<RateLimitManagement />} /> {/* Issue #185 */}
              <Route path="/my-chats" element={<MyChats />} /> {/* Issue #179 */}
              <Route path="/chat" element={<AgentChat />} /> {/* Issue #97 */}
              <Route path="/settings/connections" element={<Connections />} /> {/* Issue #465 */}
              <Route path="/settings/credentials" element={<SettingsCredentials />} /> {/* Issue #562 */}
              <Route path="/settings/credentials/aws/connect" element={<ConnectAws />} /> {/* Issue #562 */}
              <Route path="/admin/access-requests" element={<AccessRequests />} /> {/* Issue #545 */}
              <Route path="/admin/indexing" element={<IndexingStatus />} /> {/* Issue #1424 */}
              <Route path="/activity" element={<AgentActivity />} /> {/* Issue #1457 */}
              <Route path="/knowledge" element={<Knowledge />} /> {/* Issue #1794 */}
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
