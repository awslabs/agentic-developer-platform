import { useEffect, useRef, useState } from 'react';
import { advanceRunPreparation, beginRunPreparation, readRunPreparation, retainRunPreparation, type RunPreparation, type PreparationProgress } from '@/services/l5RunPreparation';
import type { DevOpsConfiguration, SecurityConfiguration, Versioned } from '@/services/l5Services';
import DevOpsSetupPanel from './DevOpsSetupPanel';
import { workloadName } from './DevOpsPresentation';
import './runAccess.css';

type Configuration = SecurityConfiguration | DevOpsConfiguration;
export default function RunAccess({ service, configuration, bindingId, disabled, onReady }: {
  service: 'security' | 'devops'; configuration: Versioned<Configuration>; bindingId: string; disabled?: boolean;
  onReady: (configuration: Versioned<Configuration>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [journal, setJournal] = useState<RunPreparation | null>(null);
  const journalRef = useRef(journal);
  const [progress, setProgress] = useState<PreparationProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [hours, setHours] = useState('8');
  const [reviewed, setReviewed] = useState(false);
  const generation = useRef(0);
  const identity = JSON.stringify([service, configuration.value.configuration_id, configuration.value.scope]);
  const latest = useRef({ onReady }); latest.current = { onReady };
  const persist = (value: RunPreparation) => { retainRunPreparation(value); journalRef.current = value; setJournal(value); };
  useEffect(() => {
    generation.current += 1; setProgress(null); setBusy(false); setOpen(false); setError(''); setJournal(null); journalRef.current = null;
    try { const saved = readRunPreparation(service, configuration.value); journalRef.current = saved; setJournal(saved); }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'The original access request could not be read.'); }
    return () => { generation.current += 1; };
  }, [identity]);
  async function start() {
    setOpen(true); setProgress(null); setReviewed(false);
    if (disabled || busy) return;
    // Setup and Runs keep their panels mounted. Read the shared receipt again
    // so changing sections cannot replace another panel's unfinished request.
    try {
      const retained = readRunPreparation(service, configuration.value);
      journalRef.current = retained; setJournal(retained); setError('');
      if (retained) return;
    } catch (failure) { showError(failure); return; }
    const epoch = generation.current;
    setBusy(true);
    try {
      const value = await beginRunPreparation(service, configuration, bindingId);
      if (epoch === generation.current) persist(value);
    } catch (failure) { if (epoch === generation.current) showError(failure); }
    finally { if (epoch === generation.current) setBusy(false); }
  }
  function showError(failure: unknown) {
    const detail = (failure as { detail?: { code?: string; message?: string } })?.detail;
    setError(failure instanceof Error ? failure.message : detail?.message ?? (detail?.code
      ? `Access could not be refreshed (${detail.code}). Review the saved setup and continue with this request.`
      : 'Access refresh was interrupted. Continue with the saved request to check its original outcome.'));
  }
  async function advance(authorize = false) {
    const current = journalRef.current;
    if (!current || busy || (authorize && (!reviewed || !['consent', 'replace'].includes(progress?.kind ?? '')))) return;
    const epoch = generation.current;
    setBusy(true); setError('');
    try {
      const result = await advanceRunPreparation(current, value => {
        // Retain the original workflow even if the owner leaves this screen.
        // Only its React state and completion callback belong to the mounted view.
        retainRunPreparation(value);
        if (epoch === generation.current) { journalRef.current = value; setJournal(value); }
      },
        authorize && progress?.kind === 'consent' ? { purpose: progress.purpose, preview: progress.preview, expiresAt: new Date(Date.now() + Number(hours) * 3600_000).toISOString() } : undefined,
        authorize && progress?.kind === 'replace' ? progress.grants : undefined);
      if (epoch !== generation.current) return;
      setProgress(result); setReviewed(false);
      if (result.kind === 'done') { setOpen(false); setJournal(null); journalRef.current = null; latest.current.onReady(result.configuration); }
    } catch (failure) { if (epoch === generation.current) showError(failure); }
    finally { if (epoch === generation.current) setBusy(false); }
  }
  useEffect(() => {
    if (progress?.kind !== 'checking' || busy || error) return;
    const timer = window.setTimeout(() => void advance(), 4000);
    return () => window.clearTimeout(timer);
  }, [progress, busy, error]);
  const config = journal?.original.value ?? configuration.value;
  const preview = progress?.kind === 'consent' ? progress.preview.value.scope : null;
  return <div className="run-access">
    <button type="button" className="product-text-button" disabled={disabled || busy || !bindingId} onClick={() => void start()}>
      {journal ? 'Continue access refresh' : 'Refresh run access'}
    </button>
    {error && !open && <p role="alert">{error}</p>}
    <DevOpsSetupPanel title="Refresh run access" open={open} onClose={() => setOpen(false)} busy={busy}>
      <p>Review access to the target saved in Setup. Only the Product and configuration owner can approve it.</p>
      <dl className="run-access-summary">
        <div><dt>Activity</dt><dd>{service === 'security' ? 'Security assessments' : 'DevOps investigation'}</dd></div>
        <div><dt>Saved target</dt><dd>{journal?.selection ? workloadName(journal.selection.resource_arn)
          : service === 'security' ? config.settings.source_refs.map(source => source.record_id).join(', ') : 'Workload and logs from Setup'}</dd></div>
        {preview && <div><dt>AWS account</dt><dd>{preview.provider_account_id} · {preview.region}</dd></div>}
      </dl>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">Checking the saved access request…</p>}
      {progress?.kind === 'checking' && <p role="status">Verifying the existing workspace and workload. This updates automatically; you can return here to continue.</p>}
      {progress?.kind === 'replace' && <>
        <h3>Replace existing access</h3>
        <p>The saved target has an active grant. Refreshing verification requires retiring that grant before reviewing new access.</p>
        <ul>{progress.grants.map(grant => <li key={grant.delegation_id}>Expires {new Date(grant.expires_at).toLocaleString()} · {grant.capabilities.join(', ') || 'Workload verification'}</li>)}</ul>
        <details><summary>Review grants being replaced</summary><pre>{JSON.stringify(progress.grants, null, 2)}</pre></details>
        <label><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)} /> Replace these grants for the saved target.</label>
        <button type="button" className="product-primary" disabled={busy || !reviewed} onClick={() => void advance(true)}>Replace access and continue</button>
      </>}
      {progress?.kind === 'consent' && <>
        <h3>{progress.purpose === 'controlGrant' ? 'Allow verification' : 'Allow runs'}</h3>
        <p>{progress.purpose === 'controlGrant' ? 'Allow ADP to verify access to this saved target.' : 'Allow the saved activities to use this target. You will start each run from the run screen.'}</p>
        <label>Access duration<select value={hours} disabled={busy} onChange={event => { setHours(event.target.value); setReviewed(false); }}>
          <option value="1">1 hour</option><option value="8">8 hours</option><option value="24">24 hours</option>
        </select></label>
        <details><summary>Review exact permissions</summary><pre>{JSON.stringify(preview, null, 2)}</pre></details>
        <label><input type="checkbox" checked={reviewed} disabled={busy} onChange={event => setReviewed(event.target.checked)} /> I authorize this scope for {hours} {hours === '1' ? 'hour' : 'hours'} as its owner.</label>
        <button type="button" className="product-primary" disabled={busy || !reviewed} onClick={() => void advance(true)}>{progress.purpose === 'controlGrant' ? 'Allow access and verify' : 'Allow runs'}</button>
      </>}
      {!progress && journal && <>
        <p>{journal.plan === 'devops-resources'
          ? 'ADP will pause new runs while it verifies the same workload and logs, restore your saved settings, and ask you to approve run access.'
          : journal.plan === 'security-check' ? 'ADP will check the existing workspace, source registration and selected threat model before you start.'
            : 'Review the permissions and duration for runs on this saved target.'}</p>
        <button type="button" className="product-primary" disabled={busy} onClick={() => void advance()}>Review access</button>
      </>}
      {error && journal && progress?.kind === 'checking' && <button type="button" disabled={busy} onClick={() => void advance()}>Continue saved request</button>}
      <a href={`#l5-${service}-setup`} onClick={() => setOpen(false)}>Review Setup</a>
    </DevOpsSetupPanel>
  </div>;
}
