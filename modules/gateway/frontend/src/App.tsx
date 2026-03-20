import { Routes, Route } from 'react-router-dom';
import { Suspense, lazy } from 'react';
import { MainLayout } from './layouts/MainLayout';
import { AuthLayout } from './layouts/AuthLayout';
import { ProtectedRoute } from './components/ProtectedRoute';
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

          {/* Protected routes */}
          <Route
            element={
              <ProtectedRoute>
                <MainLayout />
              </ProtectedRoute>
            }
          >
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
          </Route>

          {/* 404 */}
          <Route path="*" element={<NotFound />} />
        </Routes>
      </Suspense>
    </ErrorBoundary>
  );
}

export default App;
