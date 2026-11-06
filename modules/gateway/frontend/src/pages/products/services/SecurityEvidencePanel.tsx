import { useEffect, useRef, useState } from 'react';
import { readSecurityEvidence, securityEvidenceAction, type SecurityEvidenceView } from '@/services/securityEvidence';

interface Props {
  productId: string;
  projectId: string;
  tenantId?: string;
  onReconciled?: () => void;
  onChallenge: (challenge: { id: string; region: string; reason: string; receipt: string } | null) => void;
}

export default function SecurityEvidencePanel({ productId, projectId, tenantId, onChallenge, onReconciled }: Props) {
  const [requestId, setRequestId] = useState('');
  const [view, setView] = useState<SecurityEvidenceView | null>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [prepared, setPrepared] = useState(false);
  const [observerId, setObserverId] = useState('');
  const generation = useRef(0);
  const scopeKey = `${tenantId ?? ''}/${productId}/${projectId}`;
  const scope = useRef(scopeKey);
  scope.current = scopeKey;

  useEffect(() => { generation.current += 1; setView(null); setPrepared(false); setMessage(''); setBusy(false); }, [productId, projectId, tenantId]);

  async function action(kind: 'status' | 'preview' | 'endorse' | 'prepare' | 'observe' | 'mismatch' | 'revoke' | 'reconcile') {
    if (!requestId.trim() || busy) return;
    const started = generation.current;
    const current = () => generation.current === started && scope.current === scopeKey;
    setBusy(true);
    setMessage('');
    try {
      if (kind === 'status') {
        const result = await readSecurityEvidence(productId, projectId, requestId.trim());
        if (!current()) return;
        setMessage(`Request status: ${result.status.replace(/_/g, ' ')}. ${result.next_action ?? ''}`);
      } else if (kind === 'preview' || kind === 'prepare') {
        const result = await securityEvidenceAction(productId, projectId, requestId.trim(), kind,
          kind === 'preview' && observerId.trim() ? { observer_id: observerId.trim() } : undefined);
        if (!current()) return;
        const selected = result.request.selection;
        if (selected.product_id !== productId || selected.project_id !== projectId || (tenantId && selected.tenant_id !== tenantId)) throw Error('The request scope changed.');
        setView(result);
        setPrepared(kind === 'prepare');
      } else if (kind === 'endorse' && view?.maximum_expires_at) {
        await securityEvidenceAction(productId, projectId, requestId.trim(), 'endorse', {
          snapshot_sha256: view.snapshot_sha256, expires_at: view.maximum_expires_at, observer_id: view.observer_id,
        });
        if (!current()) return;
        setMessage('Owner consent recorded for this exact snapshot. The selected observer can prepare the check.');
      } else if ((kind === 'observe' || kind === 'mismatch') && view?.snapshot.run && prepared) {
        await securityEvidenceAction(productId, projectId, requestId.trim(), 'observe', {
          snapshot_sha256: view.snapshot_sha256, observation: kind === 'observe' ? 'displayed' : 'mismatch',
          configuration_revision: view.snapshot.configuration.revision,
          binding_revision: view.snapshot.binding.revision,
          run_id: view.snapshot.run.run_id, run_revision: view.snapshot.run.revision,
        });
        if (!current()) return;
        setPrepared(false);
        setMessage('Your observation is recorded. The release job will compare the independent clients.');
      } else if (kind === 'revoke' || kind === 'reconcile') {
        await securityEvidenceAction(productId, projectId, requestId.trim(), kind);
        if (!current()) return;
        onChallenge(null);
        setPrepared(false);
        if (kind === 'reconcile') onReconciled?.();
        setMessage(kind === 'revoke' ? 'Consent revoked. Retained history remains available.' : 'The original save receipt was reconciled without another save.');
      }
    } catch (error) {
      if (!current()) return;
      const detail = error as { message?: string };
      setMessage(detail.message || 'This request cannot proceed. Refresh its status and resolve the current owner, session or snapshot.');
      onChallenge(null);
      setPrepared(false);
    } finally { if (current()) setBusy(false); }
  }

  return <details className="border rounded p-3">
    <summary>Advanced release verification</summary>
    <p>Verify evidence for a release using your own ADP login. Owner approval here only permits sharing the selected evidence; it does not authorize Security assessments.</p>
    <label>Request ID <input value={requestId} onChange={event => { generation.current += 1; setBusy(false); setRequestId(event.target.value); setView(null); setPrepared(false); onChallenge(null); }} /></label>
    <label>Observer user ID (leave blank to perform the check yourself) <input value={observerId} onChange={event => { generation.current += 1; setBusy(false); setObserverId(event.target.value); setView(null); setPrepared(false); onChallenge(null); }} /></label>
    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={busy} onClick={() => void action('status')}>Read request status</button>
      <button type="button" disabled={busy} onClick={() => void action('preview')}>Preview owner consent</button>
      <button type="button" disabled={busy} onClick={() => void action('prepare')}>Prepare my check</button>
      <button type="button" disabled={busy} onClick={() => void action('reconcile')}>Reconcile original save</button>
      <button type="button" disabled={busy} onClick={() => void action('revoke')}>Revoke consent</button>
    </div>
    {message && <p role="status">{message}</p>}
    {view && <>
      <p>Release {view.request.release.run_id}, source {view.request.release.source_revision}. Expires {view.request.expires_at}.</p>
      <p>Selected observer: {view.observer_id ?? view.request.selection.observer_id}. Configuration {view.snapshot.configuration.configuration_id}, revision {view.snapshot.configuration.revision}.</p>
      {view.request.export_only && <p>This renews disclosure of the retained original observation. Do not repeat the save.</p>}
      <p>This shares the displayed configuration settings, binding and run details with the selected release.</p>
      {(['configuration', 'binding', 'run'] as const).map(name => <details key={name} open>
        <summary>{name}</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(view.snapshot[name], null, 2)}</pre>
      </details>)}
      {!prepared && view.maximum_expires_at && <button type="button" disabled={busy} onClick={() => void action('endorse')}>Approve this exact snapshot and expiry</button>}
      {prepared && view.request.selection.phase === 'readiness' && <p>Your readiness is recorded. The release job will issue the exact save request after its CLI check.</p>}
      {prepared && view.request.selection.phase === 'edit' && <>
        <p>Requested region: {view.request.selection.ui_region}. Reason: {view.request.selection.ui_reason}.</p>
        <button type="button" onClick={() => {
          onChallenge({ id: requestId.trim(), region: view.request.selection.ui_region, reason: view.request.selection.ui_reason, receipt: view.request.selection.ui_receipt });
          setMessage('Requested choices are in the configuration form. Review them and use Save Security configuration.');
        }}>Use requested Security choices</button>
      </>}
      {prepared && view.request.selection.phase === 'display' && <div className="flex gap-2">
        <button type="button" disabled={busy} onClick={() => void action('observe')}>I reviewed these configuration, binding and run details</button>
        <button type="button" disabled={busy} onClick={() => void action('mismatch')}>These details do not match</button>
      </div>}
    </>}
  </details>;
}
