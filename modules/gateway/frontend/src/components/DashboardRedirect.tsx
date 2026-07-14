import { Navigate } from 'react-router-dom';
import { usePermissions } from '@/hooks/usePermissions';

/**
 * Redirect component for the legacy /dashboard path.
 * - Platform admins → /admin/system (preserved proxy dashboard)
 * - All other users → /runs (Agent Run Dashboard)
 *
 * Issue #3634: preserves bookmarks for admins who linked to /dashboard directly.
 */
export function DashboardRedirect() {
  const { isPlatformAdmin } = usePermissions();

  if (isPlatformAdmin()) {
    return <Navigate to="/admin/system" replace />;
  }

  return <Navigate to="/runs" replace />;
}
