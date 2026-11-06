import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { Alert, Button, Card, CardTitle } from '@/components/ui';
import { decideNativeCredential, getNativeCredentialPreview, type NativeCredentialPreview } from '@/services/nativeCredentials';

export default function NativeCredentialApproval() {
  const { receiptId = '' } = useParams();
  const [preview, setPreview] = useState<NativeCredentialPreview | null>(null);
  const [phase, setPhase] = useState<'loading' | 'ready' | 'submitting' | 'unknown' | 'unavailable'>('loading');
  const [revision, setRevision] = useState(0);
  const [now, setNow] = useState(Date.now());
  const generation = useRef(0);
  const submitting = useRef(false);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    submitting.current = false;
    setPreview(null);
    setPhase('loading');
    getNativeCredentialPreview(receiptId, controller.signal).then((result) => {
      if (generation.current === current && !controller.signal.aborted) {
        setPreview(result);
        setPhase('ready');
      }
    }).catch(() => {
      if (generation.current === current && !controller.signal.aborted) setPhase('unavailable');
    });
    return () => { controller.abort(); generation.current = current + 1; };
  }, [receiptId, revision]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const decide = async (action: 'approve' | 'deny') => {
    if (submitting.current || !preview || preview.receipt_id !== receiptId || phase !== 'ready' || preview.status !== 'awaiting_approval' || Date.now() >= preview.expires_at * 1000) return;
    const current = generation.current;
    submitting.current = true;
    setPhase('submitting');
    try {
      const status = await decideNativeCredential(preview, action);
      if (current === generation.current) {
        setPreview({ ...preview, status });
        setPhase('ready');
      }
    } catch {
      // Never retry a decision automatically or mistake a lost reply for denial.
      if (current === generation.current) setPhase('unknown');
    }
  };

  const expired = preview && now >= preview.expires_at * 1000;
  const pending = preview?.receipt_id === receiptId && preview?.status === 'awaiting_approval' && !expired;
  return (
    <main className="max-w-3xl mx-auto my-8 px-4">
      <Card>
        <CardTitle>Use a credential on your computer</CardTitle>
        {phase === 'loading' && <p role="status">Loading the selected command…</p>}
        {phase === 'unavailable' && <Alert variant="error" title="This request is unavailable">Sign in with the same account that started the command. The request may have expired, or private credential delivery may not be enabled.</Alert>}
        {phase === 'unknown' && <Alert variant="warning" title="Approval outcome is not confirmed">Check this request's status before taking another action.</Alert>}
        {preview && preview.receipt_id === receiptId && <div className="mt-4 space-y-4">
          <p>Check that this request and recipient match the ones shown in your terminal. Approval applies only to this command and credential.</p>
          <dl className="space-y-3 text-sm">
            <div><dt className="font-semibold">Credential</dt><dd>{preview.selection.service} / {preview.selection.label} ({preview.selection.credential_id})</dd></div>
            <div><dt className="font-semibold">Account</dt><dd>{preview.selection.user_id} · {preview.selection.tenant_id}</dd></div>
            <div><dt className="font-semibold">Executable</dt><dd className="font-mono break-all">{preview.selection.argv[0]}</dd></div>
            <div><dt className="font-semibold">Exact argument vector</dt><dd><pre className="font-mono whitespace-pre-wrap break-all">{JSON.stringify(preview.selection.argv, null, 2)}</pre></dd></div>
            <div><dt className="font-semibold">Working folder</dt><dd className="font-mono break-all">{preview.selection.cwd}</dd></div>
            <div><dt className="font-semibold">Access duration</dt><dd>{preview.selection.use_seconds} seconds</dd></div>
            <div><dt className="font-semibold">Delivery</dt><dd>{preview.selection.operation === 'assume_role' ? 'Temporary AWS credentials in the selected command’s environment' : 'Private temporary file for the selected command'}</dd></div>
            <div><dt className="font-semibold">Recipient fingerprint</dt><dd className="font-mono break-all">{preview.offer.recipient_fingerprint}</dd></div>
            <div><dt className="font-semibold">Request</dt><dd className="font-mono break-all">{preview.receipt_id}</dd></div>
          </dl>
          <details><summary>Executable and delivery binding</summary><p className="font-mono text-xs break-all">Executable: {preview.offer.command_digest}<br />Delivery: {preview.offer.sink_digest}</p></details>
          <p>The command's output stays private. Ending the command does not revoke an AWS session before its expiry.</p>
          <p><Link to="/settings/credentials">Manage or revoke credential consent</Link>.</p>
          {(expired || preview.status === 'expired') && <Alert variant="warning" title="Request expired">This request cannot be approved. Return to your terminal to inspect its status.</Alert>}
          {preview.status === 'denied' && <Alert variant="info" title="Request denied">This request cannot issue credentials.</Alert>}
          {['approved', 'challenged', 'delivered', 'acknowledged'].includes(preview.status) && <Alert variant="success" title="Selection approved">Return to your terminal for delivery and command status.</Alert>}
          {preview.status === 'unknown' && <Alert variant="warning" title="Delivery outcome is unknown">Inspect the original request in your terminal. This page cannot reissue credentials.</Alert>}
          {pending && phase !== 'unknown' && <div className="flex gap-3 justify-end">
            <Button variant="outline" disabled={phase !== 'ready'} onClick={() => decide('deny')}>Deny</Button>
            <Button disabled={phase !== 'ready'} onClick={() => decide('approve')}>{phase === 'submitting' ? 'Submitting…' : 'Approve this command'}</Button>
          </div>}
        </div>}
        {(phase === 'unknown' || phase === 'unavailable') && <Button variant="outline" onClick={() => setRevision((value) => value + 1)}>Check status</Button>}
      </Card>
    </main>
  );
}
