import { Link } from 'react-router-dom';
import { useRevalidatingFeaturesQuery } from '@/hooks/useFeatures';
import { useInstallationStatus } from '@/hooks/useInstallationStatus';

export function InstallationDeploymentStatus({ installationId }: { installationId: string }) {
  const { data, isPending, isError } = useRevalidatingFeaturesQuery();
  if (isPending || isError || data?.assistant_ui !== true || data.chat !== true) return null;
  return <AuthorizedDeploymentStatus installationId={installationId} />;
}

function AuthorizedDeploymentStatus({ installationId }: { installationId: string }) {
  const { data, isError, isFetchedAfterMount } = useInstallationStatus(installationId);
  if (isError || !isFetchedAfterMount || data?.installation_id !== installationId) return null;
  if (data.status !== 'partial' && data.status !== 'unavailable') return null;
  return (
    <section aria-label="Deployment status" className="mt-2 text-sm">
      <p>{data.status === 'partial' ? 'Deployment status: partial runtime observations.' : 'Deployment status: no runtime observation available.'}</p>
      {data.capabilities?.installed_record?.status === 'unavailable' && <p>Installed-release record unavailable.</p>}
      {data.observed_at && <p>Observed at <time dateTime={data.observed_at}>{data.observed_at}</time></p>}
      <Link to={`/assistant/installation/${encodeURIComponent(installationId)}`} className="text-primary-600 hover:underline dark:text-primary-400">
        Ask about deployment
      </Link>
    </section>
  );
}
