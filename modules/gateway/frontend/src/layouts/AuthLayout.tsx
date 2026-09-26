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
    <div className="blueprint-auth min-h-screen">
      <div className="blueprint-auth-shell">
        <aside className="blueprint-auth-panel">
          <div className="blueprint-auth-brand">
            <span className="blueprint-brand-mark" aria-hidden="true">ADP</span>
            <span>Agentic Developer Platform</span>
          </div>
          <div>
            <p className="blueprint-auth-kicker">SOFTWARE DELIVERY WITH AGENTS</p>
            <h1>Your software factory in the cloud.</h1>
            <p className="blueprint-auth-description">
              Plan, build, review, and verify software in one workspace.
            </p>
          </div>
          <p className="blueprint-auth-panel-footer">ADP · Developer Platform</p>
        </aside>

        <main className="blueprint-auth-main">
          <div className="blueprint-auth-content">
            <h1 className="blueprint-auth-mobile-brand">
              <span className="blueprint-brand-mark" aria-hidden="true">ADP</span>
              <span>Agentic Developer Platform</span>
            </h1>
            <div className="blueprint-auth-card">
              <Outlet />
            </div>
            <p className="blueprint-auth-footer">
              Secure access powered by AWS SSO
            </p>
          </div>
        </main>
      </div>
    </div>
  );
}
