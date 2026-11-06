import { useCrewRuntime } from '@/hooks/useCrewRuntime';
import OpenMyCrew from './OpenMyCrew';
import OpenMyCrewBox from './OpenMyCrewBox';

export default function CrewRuntimeAction({ handoffUrl }: { handoffUrl: string }) {
  const runtime = useCrewRuntime();
  if (runtime.loading) return null;
  if (runtime.allowed && runtime.choice === 'kirocrew-eks') return <OpenMyCrew handoffUrl={handoffUrl} />;
  // The EC2 action opens the interstitial tab, which reads the Crew origin itself (#8223 UX).
  if (runtime.hostAllowed && runtime.choice === 'kirocrew-ec2') return <OpenMyCrewBox />;
  return null;
}
