/**
 * Settings → Dev Box → "Connecting to your Dev Box" interstitial (#8223 UX follow-up).
 *
 * This is the tab the Dev Box page (and the chat action) opens synchronously on the click,
 * so no pop-up blocker is involved; see `services/devBoxOpen`. It is also the tab that
 * becomes the Crew dashboard: it runs the one-click flow (`useDevBox().openBox`) and, once
 * the handoff ticket is minted, replaces its own location with the handoff URL. The flow
 * lives in exactly one place — this tab — so two tabs never race the enrollment.
 *
 * With the click's intent (nonce in the URL, consumed once) the flow may provision or start
 * the box and enroll this host. Without it (direct visit, reload, stale link) the page only
 * connects to a box that is already running with Crew ready; otherwise it names the state
 * and points back to the Dev Box page. Refusals render by name with Retry; nothing loops.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import Lifecycle from '@/components/devbox/Lifecycle';
import { useFeatures } from '@/hooks/useFeatures';
import { describeFailure, useDevBox } from '@/hooks/useDevBox';
import { consumeDevBoxOpenIntent } from '@/services/devBoxOpen';

const DEV_BOX_PAGE = '/settings/dev-box';

export default function DevBoxOpenPage() {
  const features = useFeatures();
  const [params] = useSearchParams();
  const devBox = useDevBox();
  const { box, phase, failure, timeline, resources, crewTarget, busy } = devBox;
  const [navigating, setNavigating] = useState(false);
  // The intent is consumed exactly once for the life of this page, so Retry keeps the mode
  // the click granted and a reload (or a direct visit) is connect-only.
  const ensure = useRef<boolean | null>(null);
  if (ensure.current === null) ensure.current = consumeDevBoxOpenIntent(params.get('intent'));
  const started = useRef(false);

  const handoffUrl = features.crew_dashboard_origin ? `${features.crew_dashboard_origin}/crew/handoff` : null;
  const { openBox } = devBox;
  const connect = useCallback(() => {
    if (!handoffUrl) return;
    void openBox(handoffUrl, {
      ensure: ensure.current === true,
      deliver: destination => { setNavigating(true); window.location.replace(destination); },
    });
  }, [handoffUrl, openBox]);

  useEffect(() => {
    if (started.current || !handoffUrl) return;
    started.current = true;
    connect();
  }, [connect, handoffUrl]);

  const failureText = describeFailure(failure);
  const title = box?.name ? `Connecting to your Dev Box “${box.name}”…` : 'Connecting to your Dev Box…';

  return <main className="mx-auto max-w-xl p-6">
    <h1 className="text-2xl font-semibold">{navigating ? 'Opening the Crew dashboard…' : title}</h1>
    {!handoffUrl && <Alert variant="error" title="Dev Box request failed">
      <span data-testid="devbox-open-failure">Crew dashboard origin is not configured.</span>{' '}
      <Link to={DEV_BOX_PAGE} className="underline">Back to Dev Box</Link>
    </Alert>}
    {(busy || navigating) && <p role="status" data-testid="devbox-open-status" className="mt-4 flex items-center gap-2">
      <Spinner size="sm" /> {navigating ? 'Handing this tab to Kiro Crew.' : phase}
    </p>}
    {!busy && !navigating && !failure && handoffUrl && <p role="status" className="mt-4 flex items-center gap-2"><Spinner size="sm" /> Checking your Dev Box…</p>}
    {failureText && <div className="mt-4"><Alert variant="error" title="Dev Box request failed">
      <span data-testid="devbox-open-failure">{failureText}</span>
      {' '}
      {!busy && handoffUrl && <Button type="button" size="sm" variant="outline" onClick={connect}>Retry</Button>}
      {' '}
      <Link to={DEV_BOX_PAGE} className="underline">Back to Dev Box</Link>
    </Alert></div>}
    {!ensure.current && handoffUrl && <p className="mt-2 text-sm text-gray-600 dark:text-gray-300" data-testid="connect-only-note">
      This tab only connects to a running Dev Box. To provision or start one, use <Link to={DEV_BOX_PAGE} className="underline">the Dev Box page</Link>.
    </p>}
    {(timeline.length > 0 || box) && <Lifecycle box={box} timeline={timeline} resources={resources} phase={null} crewTarget={crewTarget} />}
    <p className="mt-6 text-sm text-gray-600 dark:text-gray-300">
      ADP stays open in your other tab; this one becomes the Crew dashboard once the host is ready.
    </p>
  </main>;
}
