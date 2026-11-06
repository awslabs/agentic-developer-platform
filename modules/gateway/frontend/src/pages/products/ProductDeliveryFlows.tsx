import { useAuth } from '@/hooks/useAuth';
import { useFeaturesQuery } from '@/hooks/useFeatures';
import FlowsList from '@/pages/FlowsList';

/** Use the menu's flow view, with scope fixed by the Product workspace. */
export default function ProductDeliveryFlows({ productId, projectId }: { productId: string; projectId?: string }) {
  const { user } = useAuth();
  const { data, isPending, isError } = useFeaturesQuery();
  if (isPending) return <p role="status">Loading delivery flows…</p>;
  if (isError) return <p role="alert">Could not check delivery flow availability. Reload to try again.</p>;
  if (!data?.orchestration_engine) return <p>Delivery flows are not enabled in this environment.</p>;
  return <div className="product-delivery-flows"><FlowsList productId={productId} projectId={projectId} readerKey={`${user?.orgId}:${user?.id}`} /></div>;
}
