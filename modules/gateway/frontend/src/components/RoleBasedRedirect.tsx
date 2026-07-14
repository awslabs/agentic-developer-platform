import { Navigate } from 'react-router-dom';
import { useAuthContext } from '@/contexts/AuthContext';

/**
 * Redirects all authenticated users to /runs (Agent Run Dashboard).
 * Unauthenticated users are sent to /login.
 *
 * Issue #3634: simplified from role-based routing to unconditional /runs redirect.
 */
export function RoleBasedRedirect() {
  const { user } = useAuthContext();

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return <Navigate to="/runs" replace />;
}
