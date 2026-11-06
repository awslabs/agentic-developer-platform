import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { enrollCrewHost } from '@/services/crewHostEnrollment';

type DesktopList = {
  contract_version: number;
  items: Array<{ desktop_id: string; profile: string; status: string }>;
};

/**
 * Manual host enrollment for the current Crew box — the "Advanced" fallback on Settings → Runtime.
 * Since #8140 the normal path is automatic: `useDevBox().openBox()` runs the same headless
 * `enrollCrewHost` when the running box has no Crew target. This thin view stays for the case
 * where an owner wants to enroll without opening the dashboard. Submission alone never means
 * the dashboard is ready.
 */
export default function CrewHostEnrollment() {
  const [phase, setPhase] = useState<'idle' | 'submitting' | 'pending' | 'unavailable'>('idle');
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => { request.current?.abort(); }, []);

  async function enroll() {
    if (request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setPhase('submitting');
    try {
      const desktops = await apiClient.get<DesktopList>('/cloud-desktops', controller.signal);
      if (desktops?.contract_version !== 1 || !Array.isArray(desktops.items)) throw new Error('Invalid desktop list');
      const current = desktops.items.filter(box => box?.profile === 'crew' && box.status !== 'released');
      if (current.length !== 1) throw new Error('No unique Crew box');
      if (!controller.signal.aborted) await enrollCrewHost(current[0].desktop_id, controller.signal);
      if (!controller.signal.aborted) setPhase('pending');
    } catch {
      if (!controller.signal.aborted) setPhase('unavailable');
    } finally {
      if (request.current === controller) request.current = null;
    }
  }

  return <section className="mt-6" aria-label="Crew box enrollment">
    <h2 className="font-semibold">Crew on your box</h2>
    <p>Enroll only the current owned Crew box. Submission does not make its dashboard ready.</p>
    <button type="button" onClick={() => void enroll()} disabled={phase === 'submitting' || phase === 'pending'}>
      Enroll my Crew box
    </button>
    {phase === 'submitting' && <p role="status">Encrypting enrollment for your box…</p>}
    {phase === 'pending' && <p role="status">Enrollment submitted. Waiting for verified host setup.</p>}
    {phase === 'unavailable' && <p role="alert">Enrollment unavailable. Check your owned Crew box and try again.</p>}
  </section>;
}
