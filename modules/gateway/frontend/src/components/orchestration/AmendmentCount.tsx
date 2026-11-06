import { useQuery } from '@tanstack/react-query';
import { usePermissions } from '@/hooks/usePermissions';
import { useFeatures } from '@/hooks/useFeatures';
import { Permission } from '@/types';
import { listAmendments } from '@/services/orchestration';

export function AmendmentCount({ flowId }: { flowId: string }) {
  const { hasPermission } = usePermissions();
  const features = useFeatures();
  const query = useQuery({
    queryKey: ['orchestration', 'amendments', flowId], queryFn: () => listAmendments(flowId),
    enabled: Boolean(features.orchestration_engine && hasPermission(Permission.PLAN_APPROVE)),
    refetchInterval: 30_000,
  });
  const count = query.data?.filter(draft => draft.state === 'pending').length;
  return count ? <span aria-label={`${count} pending amendments`}> ({count})</span> : null;
}
