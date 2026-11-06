/**
 * Settings → Dev Box — Issues #8094, #8109, #8140.
 *
 * Owner view of the one canonical EC2 Crew box. ONE primary action, "Open my Dev Box": the
 * click synchronously opens the interstitial tab (`pages/settings/DevBoxOpen`, via
 * `services/devBoxOpen`), and THAT tab provisions or starts the box, enrolls this host
 * automatically when the Crew target is missing, waits for Crew, resolves the dashboard
 * destination and becomes the dashboard. A tab opened inside the click is never treated as a
 * pop-up; this page stays open and watches the box while the other tab works. The label
 * adapts to what that click will do:
 *   no box   → "Provision and open my Dev Box"
 *   stopped  → "Start and open my Dev Box"
 *   otherwise → "Open my Dev Box"
 *
 * Secondary controls derive from boxState(status, observed_state, desired_state):
 *   running        Stop / Replace (disabled: the API requires a stopped,
 *                  verified workspace) / Release
 *   stopped        Replace / Release
 *   transitioning  (requested launch, replace in flight, start/stop not yet observed) Refresh only
 *   failed         Release only (a setup whose launch EC2 refused stays `requested`: the failure
 *                  alert's Retry calls ensure again, which admits a fresh setup — #8423)
 * There is no separate Start, Enroll or Open dashboard button: the primary action covers them.
 * Failures render the named reason (operation error_class/progress, enrollment refusal, server
 * detail) with a Retry; never raw JSON.
 */
import { useEffect, useRef, useState } from 'react';
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import CrewHostEnrollment from '@/components/chat/CrewHostEnrollment';
import ConnectSsh from '@/components/devbox/ConnectSsh';
import Panel from '@/components/devbox/Panel';
import ContextBackups from '@/components/devbox/ContextBackups';
import Lifecycle from '@/components/devbox/Lifecycle';
import ReleaseDialog from '@/components/devbox/ReleaseDialog';
import WorkspaceSnapshots from '@/components/devbox/WorkspaceSnapshots';
import type { SnapshotSummary } from '@/components/devbox/WorkspaceSnapshots';
import { useAuth } from '@/hooks/useAuth';
import { useCrewRuntime } from '@/hooks/useCrewRuntime';
import { useFeatures } from '@/hooks/useFeatures';
import { CREW_TARGET_SENTENCE, boxState, costSummary, describeFailure, useDevBox, type WorkspaceRetention, hostReadinessSentence } from '@/hooks/useDevBox';
import { listCredentials } from '@/services/credentials';
import { openDevBoxTab } from '@/services/devBoxOpen';
import { getOAuthProviders, startOAuthConnection } from '@/services/oauthConnections';
import { AdminRole } from '@/types';

/** While the interstitial tab works, re-read the box this often, for at most this long. */
const WATCH_INTERVAL_MS = 5_000;
const WATCH_MAX_MS = 15 * 60_000;
type GithubState = 'loading' | 'unavailable' | 'not_connected' | 'connected' | 'paused' | 'reconnect';

export default function DevBoxPage() {
  const features = useFeatures();
  const runtime = useCrewRuntime();
  const { hasRole } = useAuth();
  const devBox = useDevBox({ autoLoad: true });
  const { box, cost, policy, resources, phase, failure, timeline, loading, busy, crewTarget } = devBox;
  const [releasing, setReleasing] = useState(false);
  const [snapshots, setSnapshots] = useState<SnapshotSummary[]>([]);
  const [selectedSnapshot, setSelectedSnapshot] = useState('');
  /** The interstitial href the browser refused to open (a blocker that ignores the click); shown as a link. */
  const [blockedHref, setBlockedHref] = useState<string | null>(null);
  /** Set while a tab opened from here is connecting; drives the watch below and the status line. */
  const [watchingSince, setWatchingSince] = useState<number | null>(null);
  const [github, setGithub] = useState<{ state: GithubState; login?: string; reconnect?: boolean }>({ state: 'loading' });
  const [githubBusy, setGithubBusy] = useState(false);
  const [githubError, setGithubError] = useState(false);
  const openedTab = useRef<Window | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([getOAuthProviders(), listCredentials()]).then(([response, credentials]) => {
      if (!active) return;
      const provider = response.providers.find(item => item.provider === 'github');
      const credential = credentials.find(item => item.service === 'github' && item.provider === 'agentcore-identity' && item.scope === 'user');
      const appGrant = (credential?.scopes as Record<string, unknown> | null)?.github_app_grant as Record<string, unknown> | undefined;
      if (!provider || provider.status !== 'registered' || provider.connection_status === 'unavailable') {
        setGithub({ state: 'unavailable' });
      } else if (!credential) {
        setGithub({ state: 'not_connected' });
      } else if (!credential.agent_consent || provider.connection_status === 'consent_paused') {
        setGithub({ state: 'paused', reconnect: appGrant?.schema_version === 1 });
      } else {
        if (appGrant?.schema_version === 1 && appGrant.repository_access === true && provider.connection_status === 'verified') {
          setGithub({ state: 'connected', login: provider.login });
        } else if (appGrant?.schema_version === 1 && appGrant.repository_access === false && provider.connection_status === 'verified') {
          setGithub({ state: 'not_connected', reconnect: true });
        } else {
          setGithub({ state: 'reconnect' });
        }
      }
    }).catch(() => { if (active) setGithub({ state: 'unavailable' }); });
    return () => { active = false; };
  }, []);

  const connectGithub = async () => {
    setGithubBusy(true);
    setGithubError(false);
    try {
      const started = await startOAuthConnection('github', false, github.reconnect || github.state === 'reconnect' || github.state === 'paused', true);
      if (!started.authorization_url.startsWith('https://') || !started.state || !started.session_uri) throw new Error('Consent could not be started.');
      window.sessionStorage.setItem('oauth_connection_pending', JSON.stringify({
        state: started.state, session_uri: started.session_uri, service: 'github', return_to: '/settings/dev-box',
      }));
      window.location.href = started.authorization_url;
    } catch {
      setGithubError(true);
      setGithubBusy(false);
    }
  };

  const handoffUrl = features.crew_dashboard_origin ? `${features.crew_dashboard_origin}/crew/handoff` : null;
  const state = boxState(box);
  const running = state === 'running';
  const stopped = state === 'stopped';
  const transitioning = state === 'transitioning';
  const isAdmin = hasRole(AdminRole.PLATFORM_ADMIN) || hasRole(AdminRole.ORG_ADMIN);
  const denied = !box && policy !== null && !policy.admission_allowed;
  const failureText = describeFailure(failure);
  const availableSnapshot = snapshots.some(snapshot => snapshot.state === 'present' && snapshot.snapshot_id === selectedSnapshot) ? selectedSnapshot : undefined;

  const primaryLabel = !box ? 'Provision and open my Dev Box' : stopped ? 'Start and open my Dev Box' : 'Open my Dev Box';
  const primaryAvailable = !loading && !denied && state !== 'failed' && (box !== null || policy?.admission_allowed === true);
  const primaryReason = !handoffUrl ? 'Crew dashboard origin is not configured.' : null;
  /**
   * Synchronous on purpose: `window.open` is allowed only inside the click. Re-clicking while
   * the tab from the previous click is still open just focuses it (one flow at a time).
   */
  const open = () => {
    if (!handoffUrl) return;
    if (openedTab.current && !openedTab.current.closed) { openedTab.current.focus(); return; }
    const { tab, href } = openDevBoxTab();
    openedTab.current = tab;
    setBlockedHref(tab ? null : href);
    setWatchingSince(Date.now());
  };
  const nextStep = running && crewTarget !== 'unknown'
    ? (crewTarget === 'lease_fenced' && hostReadinessSentence(devBox.box)) || CREW_TARGET_SENTENCE[crewTarget]
    : null;
  const watching = watchingSince !== null;
  const crewReady = running && crewTarget === 'ready';

  // The other tab does the work; this one keeps its view of the box current until Crew is
  // ready, the tab is gone, or the watch has run long enough.
  const { refresh } = devBox;
  useEffect(() => {
    if (watchingSince === null) return;
    const timer = setInterval(() => {
      const tabGone = openedTab.current !== null && openedTab.current.closed;
      if (tabGone || crewReady || Date.now() - watchingSince > WATCH_MAX_MS) { setWatchingSince(null); return; }
      void refresh({ quiet: true });
    }, WATCH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [crewReady, refresh, watchingSince]);

  const confirmRelease = (retention: WorkspaceRetention) => {
    setReleasing(false);
    void devBox.act('release', retention);
  };

  return <main className="mx-auto max-w-3xl px-4 py-8">
    <div className="mb-8 flex items-start justify-between gap-4">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Dev Box</h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Your EC2 box for Kiro Crew: one box per owner, running on your organisation's approved Crew template.
          One click starts it, enrolls this host and opens the Crew dashboard.
        </p>
      </div>
    </div>

    <div className="space-y-6">
    <Panel label="GitHub on this box" title="GitHub on this box" description="Your connected GitHub account, used by gh, git, Kiro Crew, Codex and Claude Code on the box.">
      <div className="mt-4 text-sm text-gray-700 dark:text-gray-300">
      {github.state === 'loading' && <p role="status">Checking GitHub connection…</p>}
      {github.state === 'unavailable' && <p>GitHub connection setup is unavailable. Ask a platform administrator to check the GitHub App.</p>}
      {github.state === 'connected' && <p>{github.login ? `Connected as @${github.login}` : 'Connected'}</p>}
      {github.state === 'paused' && <p>{github.reconnect ? 'Paused. Reconnect GitHub to approve access again.' : 'Paused. Resume access in Connections to use GitHub on this box.'}</p>}
      {github.state === 'reconnect' && <p>Reconnect to use GitHub on your box. Your previous connection cannot provide App-scoped tokens.</p>}
      </div>
      {(github.state === 'not_connected' || github.state === 'reconnect' || (github.state === 'paused' && github.reconnect)) &&
        <Button type="button" className="mt-3" disabled={githubBusy} onClick={() => void connectGithub()}>
          {github.state === 'reconnect' || github.state === 'paused' ? 'Reconnect GitHub for this box' : 'Use GitHub as you on this box'}
        </Button>}
      {githubError && <p role="alert" className="mt-2 text-sm text-red-700 dark:text-red-300">Could not start GitHub consent. Try again.</p>}
    </Panel>

    {loading && <p role="status" className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400"><Spinner size="sm" /> Checking your Dev Box…</p>}

    {failureText && <div><Alert variant="error" title="Dev Box request failed">
      <span data-testid="devbox-failure">{failureText}</span>
      {' '}
      {!busy && primaryAvailable && handoffUrl && <Button type="button" size="sm" variant="outline" onClick={open}>Retry</Button>}
    </Alert></div>}

    {!loading && denied && <section aria-label="Dev Box availability"
      className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800 dark:border-amber-700 dark:bg-amber-900/20 dark:text-amber-300">
      <h2 className="font-medium">Dev Box is not available</h2>
      <p data-testid="policy-denied" className="mt-1">{policy?.reason ?? 'Your organisation has not enabled Dev Box.'}</p>
    </section>}

    {primaryAvailable && <Panel label="Open my Dev Box" title="Open my Dev Box"
      description={box ? 'One click starts the box if needed, enrolls this host and opens the Crew dashboard in a new tab.'
        : 'No Dev Box yet. Provisioning launches a Crew-ready EC2 box and takes a few minutes.'}>
      <Button type="button" className="mt-4" disabled={busy || !handoffUrl} title={primaryReason ?? undefined}
        aria-describedby={primaryReason || nextStep ? 'open-my-dev-box-reason' : undefined} onClick={open}>{primaryLabel}</Button>
      {(primaryReason || nextStep) && <p id="open-my-dev-box-reason" className="mt-1 text-sm text-gray-600 dark:text-gray-300">{primaryReason ?? nextStep}</p>}
      {watching && !blockedHref && <p role="status" className="mt-2 text-sm" data-testid="connecting-in-tab">
        Connecting in a new tab; it becomes the Crew dashboard when the host is ready. This page stays open.</p>}
      {blockedHref && <p className="mt-2 text-sm">The browser blocked the new tab.{' '}
        <a href={blockedHref} target="_blank" className="underline">Open in new tab</a></p>}
      {/* The SSH route sits next to the launch button, not behind a toggle (owner, 2026-10-26): developers
          who work in a terminal, VS Code or Kiro over SSH should see how from the same place. */}
      {box && <ConnectSsh box={box} />}
    </Panel>}

    {!loading && (box || busy || timeline.length > 0) && <Panel label="Manage my Dev Box" title="Manage my Dev Box"
      description="Stop when you are done for the day; Replace moves the retained disk onto a fresh machine; Release gives the box back.">
      {box && <section aria-label="Dev Box actions" className="mt-4 flex flex-wrap gap-2">
        {running && <Button type="button" variant="outline" disabled={busy} onClick={() => void devBox.act('stop')}>Stop</Button>}
        {running && <Button type="button" variant="outline" disabled title="Stop the box first: replacement requires a stopped, verified workspace.">Replace</Button>}
        {stopped && <Button type="button" variant="outline" disabled={busy} onClick={() => void devBox.act('replace', undefined, availableSnapshot)}>Replace</Button>}
        {transitioning && <Button type="button" variant="secondary" disabled={busy} onClick={() => void devBox.refresh()}>Refresh</Button>}
        {(running || stopped || state === 'failed') && <Button type="button" variant="danger" disabled={busy} onClick={() => setReleasing(true)}>Release</Button>}
      </section>}
      {stopped && snapshots.some(snapshot => snapshot.state === 'present') && <label className="mt-3 block text-sm text-gray-700 dark:text-gray-300">
        Restore workspace during Replace
        <select className="ml-2 rounded border border-gray-300 bg-white p-1 dark:border-gray-600 dark:bg-gray-800" value={availableSnapshot ?? ''}
          disabled={busy} onChange={event => setSelectedSnapshot(event.target.value)}>
          <option value="">Keep current workspace (ordinary Replace)</option>
          {snapshots.filter(snapshot => snapshot.state === 'present').map(snapshot => <option key={snapshot.snapshot_id} value={snapshot.snapshot_id}>
            {snapshot.created_at && !Number.isNaN(Date.parse(snapshot.created_at))
              ? new Date(snapshot.created_at).toLocaleString() : 'Creation time unavailable'}
            {' — '}{snapshot.size_gb == null ? 'Size unavailable' : `${snapshot.size_gb} GB`}
          </option>)}
        </select>
      </label>}
      {transitioning && box && <p role="status" className="mt-3 text-sm text-gray-600 dark:text-gray-300" data-testid="transitioning">
        The controller is moving this box ({box.status}, observed {box.observed_state}{box.desired_state ? `, wanted ${box.desired_state}` : ''}). Actions return once the state is verified.
      </p>}
      <Lifecycle box={box} timeline={timeline} resources={resources} phase={phase} crewTarget={crewTarget} />
      <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">{costSummary(cost)}</p>
    </Panel>}

    <ContextBackups />
    <WorkspaceSnapshots desktopId={box?.desktop_id ?? null} onSnapshotsChange={setSnapshots} />

    {isAdmin && (box || policy) && <Panel label="Release values" title="Release values (read-only)" description="Visible to platform admins only.">
      <dl className="mt-4 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm text-gray-700 dark:text-gray-300">
        <dt>Crew bootstrap mode</dt><dd>{box?.crew_bootstrap ?? 'unknown until a box exists'}</dd>
        <dt>Template</dt><dd>{box?.template ?? '—'}{box?.template_revision != null ? ` (revision ${box.template_revision})` : ''}</dd>
        <dt>Admission</dt><dd>{box ? 'box reserved' : policy?.admission_allowed ? 'allowed' : policy?.reason ?? '—'}</dd>
      </dl>
      <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">Image revision, Crew profile revision and model pin are published to SSM by the release
        pipeline and are not exposed by the owner API; read them with the Dev Box runbook.</p>
    </Panel>}

    {runtime.hostAllowed && <details className="rounded-xl border border-dashed border-gray-300 p-4 text-sm dark:border-gray-600">
      <summary className="cursor-pointer font-medium text-gray-700 dark:text-gray-300">Advanced: enroll my Crew box manually</summary>
      <p className="mt-2 text-gray-500 dark:text-gray-400">Normally unnecessary: the button above enrolls the host automatically. Use this only if that failed.</p>
      <CrewHostEnrollment />
    </details>}
    </div>

    <ReleaseDialog isOpen={releasing} busy={busy} onClose={() => setReleasing(false)} onConfirm={confirmRelease} />
  </main>;
}
