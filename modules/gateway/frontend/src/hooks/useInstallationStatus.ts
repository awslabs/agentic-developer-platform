import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/hooks/useAuth';
import { getInstallationStatus } from '@/services/installationStatus';

export function useInstallationStatus(installationId: string) {
  const { user } = useAuth();
  return useQuery({
    queryKey: ['installation-status', user?.id, user?.orgId, installationId],
    queryFn: ({ signal }) => getInstallationStatus(installationId, signal),
    enabled: Boolean(user),
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always',
    refetchInterval: 15_000,
    retry: false,
  });
}
