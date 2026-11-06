import { Navigate, useParams } from 'react-router-dom';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import { useInstallationStatus } from '@/hooks/useInstallationStatus';
import AgentChat from './AgentChat';

export default function AssistantInstallationChat() {
  const { installationId = '' } = useParams();
  const { data, isPending, isError } = useRevalidatingFeaturesQuery();
  if (isPending) return <p role="status">Checking assistant availability…</p>;
  if (isError || data?.assistant_ui !== true || data.chat !== true) return <Navigate to="/" replace />;
  return <AuthorizedInstallationChat key={installationId} installationId={installationId} />;
}

function AuthorizedInstallationChat({ installationId }: { installationId: string }) {
  const { data, isPending, isError, isFetchedAfterMount } = useInstallationStatus(installationId);
  if (isError) return <p role="alert">Deployment chat is unavailable for this installation or your account.</p>;
  if (isPending || !isFetchedAfterMount) return <p role="status">Checking installation access…</p>;
  if (data?.installation_id !== installationId || (data.status !== 'partial' && data.status !== 'unavailable')) {
    return <p role="alert">Deployment chat is unavailable for this installation or your account.</p>;
  }
  return <AgentChat installationId={data.installation_id} />;
}
