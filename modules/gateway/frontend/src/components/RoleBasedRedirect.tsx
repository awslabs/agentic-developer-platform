import { Navigate } from 'react-router-dom';
import { useAuthContext } from '@/contexts/AuthContext';
import { AdminRole } from '@/types';

/**
 * Redirects users to the appropriate dashboard based on their role.
 * - Platform admins → /dashboard (PlatformDashboard)
 * - Org admins → /org/{orgId} (OrgDashboard)
 * - Dept admins → /org/{orgId}/department/{deptId}
 */
export function RoleBasedRedirect() {
  const { user } = useAuthContext();

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  if (user.role === AdminRole.PLATFORM_ADMIN) {
    return <Navigate to="/dashboard" replace />;
  }

  if (user.role === AdminRole.ORG_ADMIN && user.orgId) {
    return <Navigate to={`/org/${user.orgId}`} replace />;
  }

  if (user.role === AdminRole.DEPT_ADMIN && user.orgId && user.deptId) {
    return <Navigate to={`/org/${user.orgId}/department/${user.deptId}`} replace />;
  }

  // Fallback: org admin without orgId or unknown role
  return <Navigate to="/dashboard" replace />;
}
