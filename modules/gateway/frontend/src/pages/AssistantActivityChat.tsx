import { useEffect, useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import { getMyInvocationDetail } from '@/services/activity';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import AgentChat from './AgentChat';

export default function AssistantActivityChat() {
  const { data, isPending, isError } = useRevalidatingFeaturesQuery();
  if (isPending) return <p role="status">Checking assistant availability…</p>;
  if (isError || data?.assistant_ui !== true || data.chat !== true) return <Navigate to="/" replace />;
  return <AuthorizedActivityChat />;
}

function AuthorizedActivityChat() {
  const [authorizedId, setAuthorizedId] = useState<string | null>(null);
  const { invocationId } = useParams();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setAuthorizedId(null);
    setFailed(false);
    if (!invocationId) {
      setFailed(true);
      return;
    }
    getMyInvocationDetail(invocationId).then((item) => {
      if (!active) return;
      if (item.invocation_id === invocationId) setAuthorizedId(item.invocation_id);
      else setFailed(true);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [invocationId]);

  if (failed) return <p role="alert">This activity is not available to your account.</p>;
  if (!authorizedId) return <p role="status">Checking activity access…</p>;
  return <AgentChat activityId={authorizedId} />;
}
