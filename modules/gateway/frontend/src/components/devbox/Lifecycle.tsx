/**
 * Lifecycle — Issue #8094.
 *
 * The provisioning timeline (requested → launching → running → installing Crew →
 * Crew ready) with the time each step was first observed, plus the live state of
 * the box: status / observed / desired, generation, lease countdown and the
 * retained resources the owner is paying for.
 */
import { useEffect, useState } from 'react';
import { CREW_TARGET_SENTENCE, type CrewTargetState, type DevBox, type DevBoxResource, type LifecycleEvent, type LifecycleStep } from '@/hooks/useDevBox';

const STEPS: Array<{ step: LifecycleStep; label: string }> = [
  { step: 'requested', label: 'Requested' },
  { step: 'launching', label: 'Launching' },
  { step: 'running', label: 'Running' },
  { step: 'installing_crew', label: 'Installing Crew' },
  { step: 'enrolling_host', label: 'Enrolling this host' },
  { step: 'starting_crew', label: 'Starting Crew' },
  { step: 'crew_ready', label: 'Crew ready' },
];

export function stepLabel(step: LifecycleStep, box: DevBox | null): string {
  if (step === 'installing_crew' && box?.crew_bootstrap === 'user-data') return 'Installing Crew (user-data bootstrap)';
  return STEPS.find(candidate => candidate.step === step)?.label ?? step;
}

export function leaseRemaining(leaseExpiresAt: string | null | undefined, now: number): string | null {
  if (!leaseExpiresAt) return null;
  const expires = Date.parse(leaseExpiresAt);
  if (Number.isNaN(expires)) return null;
  const remaining = expires - now;
  if (remaining <= 0) return 'Lease expired';
  const minutes = Math.floor(remaining / 60_000);
  const hours = Math.floor(minutes / 60);
  if (hours >= 1) return `Lease ends in ${hours} h ${minutes % 60} min`;
  return `Lease ends in ${Math.max(minutes, 1)} min`;
}

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString();
}

export default function Lifecycle({ box, timeline, resources, phase, crewTarget = 'unknown' }: {
  box: DevBox | null;
  timeline: LifecycleEvent[];
  resources: DevBoxResource[] | null;
  phase: string | null;
  crewTarget?: CrewTargetState;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!box?.lease_expires_at) return;
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, [box?.lease_expires_at]);

  const lease = leaseRemaining(box?.lease_expires_at, now);
  const reached = new Map(timeline.map(event => [event.step, event]));

  return <section aria-label="Dev Box lifecycle" className="mt-6 border-t border-gray-200 pt-4 dark:border-gray-700">
    <h3 className="font-medium text-gray-900 dark:text-gray-100">Lifecycle</h3>
    {timeline.length > 0 && <ol className="mt-2 space-y-1 text-sm text-gray-700 dark:text-gray-300" aria-label="Provisioning timeline">
      {STEPS.map(({ step }) => {
        const event = reached.get(step);
        return <li key={step} className={event ? '' : 'text-gray-500 dark:text-gray-400'}>
          <span aria-hidden="true">{event ? '✓' : '○'}</span>{' '}
          {stepLabel(step, box)}
          {event && <>{' — '}<time dateTime={event.at}>{formatTime(event.at)}</time>{event.detail ? ` (${event.detail})` : ''}</>}
        </li>;
      })}
    </ol>}
    {phase && <p role="status" className="mt-2 text-sm text-gray-700 dark:text-gray-300">{phase}</p>}
    {box && <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm text-gray-700 dark:text-gray-300 [&>dt]:text-gray-500 dark:[&>dt]:text-gray-400">
      <dt>Status</dt><dd>{box.status} (observed {box.observed_state}{box.desired_state ? `, wanted ${box.desired_state}` : ''})</dd>
      <dt>Generation</dt><dd>{box.machine_generation}</dd>
      {box.template && <><dt>Template</dt><dd>{box.template}{box.template_revision != null ? ` rev ${box.template_revision}` : ''}</dd></>}
      {box.crew_bootstrap && <><dt>Crew bootstrap</dt><dd>{box.crew_bootstrap}</dd></>}
      {typeof box.crew_ready === 'boolean' && <><dt>Crew ready</dt><dd>{box.crew_ready ? 'yes' : 'not yet'}</dd></>}
      {box.crew_bootstrap_receipt && <><dt>Bootstrap receipt</dt><dd data-testid="bootstrap-receipt">
        {box.crew_bootstrap_receipt.status ?? 'unknown'}{box.crew_bootstrap_receipt.profile_revision ? ` (profile ${box.crew_bootstrap_receipt.profile_revision})` : ''}
        {box.crew_bootstrap_receipt.error ? ` — ${box.crew_bootstrap_receipt.error}` : ''}</dd></>}
      {crewTarget !== 'unknown' && <><dt>Crew target</dt><dd data-testid="crew-target">{crewTarget} — {CREW_TARGET_SENTENCE[crewTarget]}</dd></>}
      {lease && <><dt>Lease</dt><dd>{lease}{box.lease_expires_at ? <> (<time dateTime={box.lease_expires_at}>{new Date(box.lease_expires_at).toLocaleString()}</time>)</> : null}</dd></>}
    </dl>}
    {resources && resources.length > 0 && <>
      <h4 className="mt-4 font-medium text-gray-900 dark:text-gray-100">Resources</h4>
      <ul className="mt-1 text-sm text-gray-700 dark:text-gray-300" aria-label="Dev Box resources">
        {resources.map(resource => <li key={resource.resource_id}>
          {resource.resource_kind}{resource.size_gb != null ? ` ${resource.size_gb} GB` : ''} — {resource.retention_state}
          {resource.deletion_state ? ` (${resource.deletion_state})` : ''}{resource.encrypted ? ', encrypted' : ''}; generation {resource.machine_generation}
        </li>)}
      </ul>
    </>}
  </section>;
}
