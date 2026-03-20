import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '@/hooks/useAuth';
import { LoadingScreen } from './LoadingScreen';
import type { Permission, AdminRole } from '@/types';

interface ProtectedRouteProps {
  children: React.ReactNode;
  requiredPermission?: Permission;
  requiredRole?: AdminRole;
  requireAny?: Permission[];
}

export function ProtectedRoute({
  children,
  requiredPermission,
  requiredRole,
  requireAny,
}: ProtectedRouteProps) {
  const { isAuthenticated, isLoading, hasPermission, hasRole } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return <LoadingScreen />;
  }

  if (!isAuthenticated) {
    // Redirect to login with return URL
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Check specific permission
  if (requiredPermission && !hasPermission(requiredPermission)) {
    return <Navigate to="/unauthorized" replace />;
  }

  // Check specific role
  if (requiredRole && !hasRole(requiredRole)) {
    return <Navigate to="/unauthorized" replace />;
  }

  // Check any of the permissions
  if (requireAny && requireAny.length > 0) {
    const hasAny = requireAny.some((perm) => hasPermission(perm));
    if (!hasAny) {
      return <Navigate to="/unauthorized" replace />;
    }
  }

  return <>{children}</>;
}
