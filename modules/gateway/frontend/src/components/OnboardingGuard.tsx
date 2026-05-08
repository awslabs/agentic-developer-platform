import { useEffect } from 'react';
import { useNavigate, useLocation, Outlet } from 'react-router-dom';
import { useAccessStatus } from '@/hooks/useAccessStatus';
import { LoadingScreen } from './LoadingScreen';

/**
 * Route wrapper that intercepts authenticated users and redirects
 * them to the appropriate onboarding page based on their access status.
 * Users with status='registered' pass through to the normal app routes.
 */
export function OnboardingGuard() {
  const { status, isLoading } = useAccessStatus();
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (isLoading || !status) return;

    // Only redirect if user is on a non-onboarding protected route
    const isOnboardingRoute = location.pathname.startsWith('/onboarding/');
    if (isOnboardingRoute) return;

    if (status === 'new') {
      navigate('/onboarding/welcome', { replace: true });
    } else if (status === 'pending') {
      navigate('/onboarding/pending', { replace: true });
    } else if (status === 'denied') {
      navigate('/onboarding/denied', { replace: true });
    }
    // 'registered' falls through — user continues to the requested page
  }, [status, isLoading, navigate, location.pathname]);

  if (isLoading) {
    return <LoadingScreen />;
  }

  // If the user is not 'registered' and not already on an onboarding route,
  // show loading while redirect happens
  if (status && status !== 'registered' && !location.pathname.startsWith('/onboarding/')) {
    return <LoadingScreen />;
  }

  return <Outlet />;
}
