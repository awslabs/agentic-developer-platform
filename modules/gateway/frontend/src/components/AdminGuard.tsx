import { Navigate } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';

interface AdminGuardProps {
  children: React.ReactNode;
}

/**
 * Route guard that only allows platform admins through.
 * Non-admins are redirected to /runs.
 *
 * Issue #3634: used to protect /admin/system (demoted proxy dashboard).
 */
export function AdminGuard({ children }: AdminGuardProps) {
  const { isPlatformAdmin } = usePermissions();

  if (!isPlatformAdmin()) {
    return <Navigate to="/runs" replace />;
  }

  return <>{children}</>;
}
