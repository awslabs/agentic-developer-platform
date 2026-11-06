import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { resolveCrewDashboardDestination } from '@/services/crewDashboard';

type CrewStatus = {
  state: 'starting' | 'restoring' | 'ready' | 'failed';
  target_kind: 'eks-pod' | 'ec2-instance';
  executor_generation: number;
};

function errorClass(error: unknown): string {
  if (typeof error !== 'object' || error === null || !('detail' in error)) return 'crew_unavailable';
  const detail = error.detail;
  const code = typeof detail === 'object' && detail !== null && 'error' in detail ? detail.error : undefined;
  return typeof code === 'string' && /^[a-z][a-z0-9_]{1,64}$/.test(code) ? code : 'crew_unavailable';
}

export default function OpenMyCrew({ handoffUrl }: { handoffUrl: string }) {
  const [phase, setPhase] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const reservedTab = useRef<Window | null>(null);

  useEffect(() => () => { request.current?.abort(); reservedTab.current?.close(); }, []);

  function openCrew() {
    if (request.current) return;
    const tab = window.open('', '_blank');
    if (!tab) {
      setFailure('Allow pop-ups to open your Crew.');
      return;
    }
    tab.opener = null;
    reservedTab.current = tab;
    const controller = new AbortController();
    request.current = controller;
    setFailure(null);
    setPhase('Starting your Crew…');
    void (async () => {
      const admitted = await apiClient.post<CrewStatus>('/crew/ensure', {}, controller.signal);
      if (!Number.isSafeInteger(admitted.executor_generation) || admitted.executor_generation < 1) throw new Error('crew_status_invalid');
      for (let attempt = 0; attempt < 120 && !controller.signal.aborted; attempt++) {
        const status = await apiClient.get<CrewStatus>('/crew/status', controller.signal);
        if (status.executor_generation !== admitted.executor_generation || status.target_kind !== admitted.target_kind) {
          throw new Error('crew_status_invalid');
        }
        if (status.state === 'ready') {
          setPhase('Opening');
          tab.location.href = await resolveCrewDashboardDestination(handoffUrl);
          reservedTab.current = null;
          setPhase(null);
          return;
        }
        if (status.state === 'failed') throw new Error('crew_restore_failed');
        if (status.state !== 'starting' && status.state !== 'restoring') throw new Error('crew_status_invalid');
        setPhase(status.state === 'restoring' ? 'Restoring Crew state…' : 'Starting your Crew…');
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
      if (!controller.signal.aborted) throw new Error('crew_start_timeout');
    })().catch(error => {
      if (!controller.signal.aborted) {
        tab.close();
        reservedTab.current = null;
        setFailure(errorClass(error) === 'crew_unavailable' && error instanceof Error ? error.message : errorClass(error));
        setPhase(null);
      }
    }).finally(() => { if (request.current === controller) request.current = null; });
  }

  return <div>
    <button type="button" onClick={openCrew} disabled={phase !== null}>Open my Crew</button>
    {phase && <span role="status">{phase}</span>}
    {failure && <span role="alert">{failure} <button type="button" onClick={openCrew}>Retry</button></span>}
  </div>;
}
