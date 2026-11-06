import { useEffect, useRef, useState } from 'react';
import { apiClient } from '@/services/api';
import { devOpsPath } from '@/services/l5Services';
import { productReceipt } from '@/services/products';

interface Selection {
  tenant_id: string; product_id: string; project_id: string; configuration_id: string; binding_id: string;
}
interface Acknowledgement {
  kind: string; actor_id: string; reason: string; abandoned_at: string; event_hash: string;
  native_effects_may_remain: boolean; native_cleanup_performed: boolean; native_ownership_released: boolean;
  native_authority_granted: boolean; native_call_performed: boolean; release_check_completed: boolean;
  response: { operation_id: string; state: string };
}
interface History {
  operation_id: string; revision: number; state: string; selection: Selection; items: Acknowledgement[];
}
interface SavedRequest { identity: string; etag: string; key: string; reason: string; readChecked: boolean }
interface Props {
  selection: Selection; operationId: string; onAccessDenied: () => void; onChanged?: () => void;
}
const identityFields = ['tenant_id', 'product_id', 'project_id', 'configuration_id', 'binding_id'] as const;
const openStates = new Set(['accepted', 'running', 'waiting', 'blocked', 'outcome_unknown', 'failed']);
function guidance(value: unknown) {
  const error = value as { status?: number; detail?: { code?: string; next_action?: string; message?: string } };
  if (typeof error?.detail?.next_action === 'string') return error.detail.next_action;
  if (typeof error?.detail?.message === 'string') return error.detail.message;
  if (error?.status === 401) return 'Sign in again and read this original operation before continuing.';
  if ([403, 404].includes(error?.status ?? 0)) return 'The current Product and configuration owner must sign in to read or abandon this operation.';
  if (error?.status === 412) return 'The operation changed. Read its current status and review its effects before acknowledging abandonment again.';
  if (error?.status === 422) return 'Enter a reason and explicitly acknowledge that native effects may remain.';
  return 'Abandonment was not confirmed. Read the original operation before retrying the saved acknowledgement.';
}

export default function DevOpsKnowledgeAbandon({ selection, operationId, onAccessDenied, onChanged }: Props) {
  const [page, setPage] = useState<{ value: History; etag: string } | null>(null);
  const [reason, setReason] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState<SavedRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const generation = useRef(0);
  const identity = JSON.stringify([selection.tenant_id, selection.product_id, selection.project_id, selection.configuration_id, selection.binding_id, operationId]);
  const path = `${devOpsPath(selection.product_id, selection.project_id)}/binding-operations/${encodeURIComponent(operationId)}`;
  useEffect(() => {
    generation.current += 1;
    setPage(null); setReason(''); setAcknowledged(false); setPending(null); setBusy(false); setError(''); setMessage('');
    return () => { generation.current += 1; };
  }, [identity]);

  function fail(value: unknown) {
    const status = (value as { status?: number })?.status;
    setError(guidance(value));
    if ([401, 403, 404].includes(status ?? 0)) {
      setPage(null); setReason(''); setAcknowledged(false); setPending(null); setMessage('');
      onAccessDenied();
    }
  }
  async function read(request: number) {
    const result = await apiClient.requestWithEtag<History>(`${path}/knowledge-evidence`);
    if (request !== generation.current) return;
    const current = result.value;
    if (current.operation_id !== operationId || !current.selection
      || !identityFields.every(key => current.selection[key] === selection[key])
      || !Number.isInteger(current.revision) || current.revision < 1 || result.etag !== `"${current.revision}"`
      || !Array.isArray(current.items)) throw { status: 403 };
    if (current.state === 'abandoned' && !current.items.some(item => item.kind === 'abandoned'
      && item.response?.operation_id === operationId && item.response.state === 'abandoned'
      && item.native_effects_may_remain === true && item.native_cleanup_performed === false
      && item.native_ownership_released === false && item.native_authority_granted === false
      && item.native_call_performed === false && item.release_check_completed === false)) {
      throw { status: 409, detail: { next_action: 'The retained abandonment acknowledgement is missing or inconsistent. Read this operation again before treating its outcome as confirmed.' } };
    }
    setPage(result);
    setPending(previous => openStates.has(current.state) && previous ? { ...previous, readChecked: true } : null);
    return result;
  }
  async function refresh() {
    if (busy) return;
    const request = generation.current;
    setBusy(true); setError('');
    try { await read(request); }
    catch (value) { if (request === generation.current) { setPage(null); fail(value); } }
    finally { if (request === generation.current) setBusy(false); }
  }
  async function send(saved: SavedRequest) {
    if (busy || !page || !openStates.has(page.value.state) || saved.identity !== identity) return;
    const request = generation.current;
    setBusy(true); setError(''); setMessage(''); setPending({ ...saved, readChecked: false });
    try {
      await apiClient.request(`${path}/abandon`, {
        method: 'POST', headers: { 'If-Match': saved.etag, 'Idempotency-Key': saved.key },
        body: { reason: saved.reason, native_effects_may_remain: true },
      });
      if (request !== generation.current) return;
      const confirmed = await read(request);
      if (request !== generation.current) return;
      if (confirmed?.value.state === 'abandoned') {
        setMessage('Abandonment is recorded. Native effects and ownership reservations remain; no cleanup or successful qualification is claimed.');
        onChanged?.();
      }
    } catch (value) {
      if (request !== generation.current) return;
      const status = (value as { status?: number })?.status;
      if (!status || status >= 500) {
        try {
          const confirmed = await read(request);
          if (request !== generation.current) return;
          if (confirmed?.value.state === 'abandoned') {
            setMessage('Abandonment is recorded. Native effects and ownership reservations remain; no cleanup or successful qualification is claimed.');
            onChanged?.();
            return;
          }
        } catch (readError) {
          if (request !== generation.current) return;
          setPage(null);
          if ([401, 403, 404].includes((readError as { status?: number })?.status ?? 0)) { fail(readError); return; }
        }
      } else {
        setPending(null); setAcknowledged(false);
        if (status === 412 || status === 409) setPage(null);
      }
      if (request === generation.current) fail(value);
    } finally { if (request === generation.current) setBusy(false); }
  }
  const history = page?.value.items.filter(item => item.kind === 'abandoned') ?? [];
  const available = Boolean(page && openStates.has(page.value.state) && !busy && !pending);
  return <details className="space-y-3">
    <summary>Abandon this knowledge publication</summary>
    <p>The current Product and configuration owner can stop this local operation after a native effect has started and the worker lease ends. Source or native consent expiry does not prevent this acknowledgement.</p>
    <p>Native changes may remain or their outcome may be unknown. Original evidence and ownership reservations stay retained. This action does not clean up native assets or mark knowledge use or release acceptance successful.</p>
    <button type="button" disabled={busy} onClick={() => void refresh()}>Read abandonment status</button>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {page && <p>Original operation: {page.value.state}. {openStates.has(page.value.state) ? 'The server checks current ownership, worker lease and retained effects before closing it.' : 'This operation is terminal; another abandonment request is unavailable.'}</p>}
    {history.map(item => <section key={item.event_hash} aria-label="Retained abandonment acknowledgement">
      <p>Abandoned by {item.actor_id} at {item.abandoned_at}.</p><p>Reason: {item.reason}</p>
      <p>{item.native_effects_may_remain ? 'Native effects may remain.' : 'Native effect status is unconfirmed.'} {item.native_cleanup_performed === false ? 'No native cleanup was performed.' : 'Native cleanup status is unconfirmed.'}</p>
      <p>{item.native_ownership_released === false ? 'Native ownership reservations remain retained.' : 'Native ownership release is unconfirmed.'} Release acceptance remains separate.</p>
    </section>)}
    <fieldset disabled={!available} className="space-y-2">
      <label className="block">Reason for abandoning this publication<textarea value={reason} maxLength={1000} onChange={event => setReason(event.target.value)} /></label>
      <label><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /> I acknowledge that native effects may remain and ownership reservations will stay retained.</label>
      <button type="button" disabled={!available || !reason.trim() || !acknowledged} onClick={() => {
        if (!available || !page || !reason.trim() || !acknowledged) return;
        void send({ identity, etag: page.etag, key: productReceipt(), reason: reason.trim(), readChecked: false });
      }}>Acknowledge abandonment without cleanup</button>
    </fieldset>
    {pending && <p>The original acknowledgement is awaiting confirmation. {pending.readChecked ? 'Readback was checked; retry uses the same saved reason, revision and request key.' : 'Read the original operation before retrying.'}
      <button type="button" disabled={busy || !page || !openStates.has(page.value.state) || !pending.readChecked} onClick={() => void send(pending)}>Retry saved abandonment acknowledgement</button>
    </p>}
  </details>;
}
