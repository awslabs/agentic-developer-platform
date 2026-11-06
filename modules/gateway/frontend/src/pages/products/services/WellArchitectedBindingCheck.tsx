import { useEffect, useRef, useState } from 'react';
import { productReceipt, type RecordReference } from '@/services/products';
import { getWellArchitectedBindingOperation, matchesSecurityScope,
  type SecurityBinding, type SecurityBindingOperation, type Versioned, type WellArchitectedConfiguration } from '@/services/l5Services';
import { checkWellArchitectedBinding, readWellArchitectedBinding, type WellArchitectedCheckRequest } from '@/services/wellArchitectedBindings';

interface Props {
  productId: string; projectId: string; configuration: Versioned<WellArchitectedConfiguration>;
  bindings: SecurityBinding[]; disabled: boolean; onInvalidate: () => void;
  onBusyChange: (busy: boolean) => void; onRefresh: () => Promise<void>; onAccessDenied: () => void;
}
interface Receipt {
  key: string; etag: string; scope: string; body: WellArchitectedCheckRequest;
  state: 'pending' | 'confirmed' | 'rejected'; operation?: SecurityBindingOperation;
}
const terminal = (operation?: SecurityBindingOperation) => operation && ['succeeded', 'blocked', 'failed', 'cancelled', 'abandoned'].includes(operation.state);
const active = (binding: SecurityBinding) => !['cleaned', 'cleanup_pending', 'unlinked'].includes(binding.state);
const referenceMatches = (a: RecordReference | undefined, b: RecordReference | undefined) => Boolean(a && b &&
  (['tenant_id', 'project_id', 'source_connection_id', 'record_type', 'record_id', 'record_revision'] as const).every(key => a[key] === b[key]));
const guidance: Record<string, string> = {
  delegation_denied: 'Open Owner consent and review control consent for this binding and its next generation, then read the current binding again.',
  delegation_scope_changed: 'Open Owner consent and review the saved sources, binding and control generation before checking again. Revoke outdated consent before explicitly granting the reviewed scope.',
  connection_not_selected: 'Select and save the AWS connection and region in Setup, then reload the configuration.',
  selected_connection_unavailable: 'Ask the connection owner to verify the selected AWS connection, then reload Setup.',
  provider_unqualified: 'Ask the service operator to restore the qualified Well-Architected provider, then read the binding again.',
  native_generation_active: 'A native review is still active. Inspect its original run and provider outcome before another binding check.',
  binding_not_active: 'This binding is being cleaned or is no longer linked. Read current binding status before choosing an active binding.',
  precondition_failed: 'The saved configuration or binding changed. Reload Setup and read the current binding before checking again.',
  precondition_required: 'Read the current binding to obtain its version before checking again.',
};

export default function WellArchitectedBindingCheck(props: Props) {
  const value = props.configuration.value;
  const owner = (value.scope as typeof value.scope & { owner_id?: string }).owner_id;
  const scope = JSON.stringify([value.scope.tenant_id, props.productId, props.projectId, 'well_architected',
    value.configuration_id, owner, value.scope.relationship_revision]);
  return <CheckPanel {...props} owner={owner} scope={scope} key={`${scope}:${value.revision}:${value.hash}`} />;
}

function CheckPanel({ productId, projectId, configuration, bindings, disabled, onInvalidate, onBusyChange, onRefresh,
  onAccessDenied, owner, scope }: Props & { owner?: string; scope: string }) {
  const [bindingId, setBindingId] = useState('');
  const [current, setCurrent] = useState<Versioned<SecurityBinding> | null>(null);
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [restored, setRestored] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState('');
  const epoch = useRef(0);
  const storageKey = owner?.trim() ? `adp:wa-binding-check:${scope}` : null;
  const blocked = disabled || busy || denied;
  const unresolved = receipt?.state === 'pending' || (receipt?.state === 'confirmed' && !terminal(receipt.operation));
  useEffect(() => () => { epoch.current += 1; onBusyChange(false); }, [onBusyChange]);

  function matches(binding: SecurityBinding) {
    return matchesSecurityScope(binding.scope, configuration.value.scope, productId, projectId)
      && binding.configuration_id === configuration.value.configuration_id
      && referenceMatches(binding.connection_ref, configuration.value.settings.connection_ref);
  }
  function validOperation(operation: SecurityBindingOperation, original: Receipt) {
    return operation.action === 'check' && operation.binding_id === original.body.binding_id
      && typeof operation.operation_id === 'string' && operation.operation_id.length > 0
      && Number.isInteger(operation.revision) && operation.revision > 0
      && ['accepted', 'running', 'waiting', 'succeeded', 'blocked', 'failed', 'outcome_unknown', 'cancelled', 'abandoned'].includes(operation.state)
      && Array.isArray(operation.gaps) && operation.gaps.every(gap => typeof gap === 'string')
      && (!original.operation || operation.operation_id === original.operation.operation_id);
  }
  function retain(value: Receipt) {
    if (!storageKey) throw new Error('Missing configuration owner.');
    // Keep prior receipts when a later explicit check replaces the current one.
    sessionStorage.setItem(`${storageKey}:${value.key}`, JSON.stringify(value));
    sessionStorage.setItem(storageKey, JSON.stringify(value));
    setReceipt(value);
  }
  function restore() {
    if (!storageKey) throw new Error('Missing configuration owner.');
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) { setRestored(true); return; }
    const saved = JSON.parse(raw) as Receipt;
    if (saved.scope !== scope || !/^[A-Za-z0-9_-]{16,128}$/.test(saved.key) || !/^"[1-9][0-9]*"$/.test(saved.etag)
      || !saved.body || Object.keys(saved.body).sort().join(',') !== 'action,binding_id,configuration_revision'
      || saved.body.action !== 'check' || typeof saved.body.binding_id !== 'string' || !saved.body.binding_id
      || !Number.isInteger(saved.body.configuration_revision) || saved.body.configuration_revision < 1
      || !['pending', 'confirmed', 'rejected'].includes(saved.state)
      || (saved.state === 'confirmed' && !saved.operation)
      || (saved.operation && !validOperation(saved.operation, saved))) throw new Error('Invalid original receipt.');
    setReceipt(saved); setRestored(true);
  }
  function fail(caught: unknown, fallback: string) {
    const failure = caught as { status?: number; detail?: { code?: string } } | null;
    const code = failure?.detail?.code;
    setCurrent(null); setReviewed(false);
    if ([401, 403, 404].includes(failure?.status ?? 0) && !(code && guidance[code])) {
      setDenied(true); setReceipt(null); setBindingId(''); onAccessDenied();
      setError('Binding access is unavailable for this account or project. Reload the saved configuration as its current owner.');
    } else setError(`${fallback}${code ? ` (${code})` : ''} ${code && guidance[code] ? guidance[code]
      : failure?.status === 412 ? guidance.precondition_failed : 'Inspect the retained original request and current binding before another action.'}`);
  }
  function begin() { const request = ++epoch.current; setBusy(true); onBusyChange(true); setError(''); return request; }
  function end(request: number) { if (request === epoch.current) { setBusy(false); onBusyChange(false); } }

  async function read() {
    if (blocked || !bindingId || !storageKey) return;
    const request = begin(); onInvalidate(); setCurrent(null); setReviewed(false);
    try {
      restore();
      const result = await readWellArchitectedBinding(productId, projectId, bindingId);
      if (request !== epoch.current) return;
      if (!matches(result.value) || result.value.binding_id !== bindingId) throw { status: 403 };
      if (result.etag !== `"${result.value.revision}"` || !Number.isInteger(result.value.revision) || result.value.revision < 1) throw { status: 412 };
      setCurrent(result);
    } catch (caught) { if (request === epoch.current) fail(caught, 'Current binding could not be read.'); }
    finally { end(request); }
  }
  async function submit(replay = false) {
    if (blocked || !restored || !storageKey || (!replay && (!current || !active(current.value) || unresolved))
      || (replay && (!reviewed || receipt?.state !== 'pending' || receipt.body.configuration_revision !== configuration.value.revision))) return;
    const original: Receipt = replay ? receipt! : { key: productReceipt(), scope, etag: current!.etag,
      body: { action: 'check', configuration_revision: configuration.value.revision, binding_id: current!.value.binding_id }, state: 'pending' };
    const request = begin(); onInvalidate(); setCurrent(null); setReviewed(false);
    let sent = false;
    try {
      retain(original); sent = true;
      const response = await checkWellArchitectedBinding(productId, projectId, original.body, original.etag, original.key);
      if (request !== epoch.current) return;
      if (!validOperation(response.value, original)) throw new Error('Original operation could not be verified.');
      retain({ ...original, state: 'confirmed', operation: response.value });
      await onRefresh();
    } catch (caught) {
      if (request !== epoch.current) return;
      const status = (caught as { status?: number } | null)?.status;
      // A rejected replay cannot prove that an earlier uncertain request was never accepted.
      if (sent && !replay && [400, 401, 403, 404, 409, 412, 422, 428].includes(status ?? 0)) {
        try { retain({ ...original, state: 'rejected' }); } catch { /* Keep original pending receipt. */ }
      }
      fail(caught, sent ? 'Binding check is not confirmed; the original request is retained.' : 'The request could not be retained locally; no check was sent.');
    } finally { end(request); }
  }
  async function inspect() {
    if (blocked || !receipt?.operation) return;
    const original = receipt;
    const request = begin(); onInvalidate(); setCurrent(null);
    try {
      const operation = await getWellArchitectedBindingOperation(productId, projectId, original.operation!.operation_id);
      if (request !== epoch.current) return;
      if (!validOperation(operation, original)) throw { status: 403 };
      retain({ ...original, state: 'confirmed', operation });
      await onRefresh();
    } catch (caught) { if (request === epoch.current) fail(caught, 'Original check status could not be read.'); }
    finally { end(request); }
  }

  return <section aria-label="Existing review binding check" className="space-y-2">
    <p>Check binding reads the provider profile under current owner authorization. Read its current version first; an accepted check does not establish readiness.</p>
    <p>Updating or resuming a binding is not available from this screen. This action checks its current status.</p>
    {!storageKey && <p>Reload the saved configuration with its current owner identity before checking a binding.</p>}
    <label>Existing review binding <select value={bindingId} disabled={blocked} onChange={event => { setBindingId(event.target.value); setCurrent(null); setReviewed(false); }}>
      <option value="">Choose an existing binding</option>{bindings.filter(item => active(item) && matches(item)).map(item =>
        <option key={item.binding_id} value={item.binding_id}>{item.binding_id}: {item.state}</option>)}
    </select></label>
    <button type="button" disabled={blocked || !bindingId || !storageKey} onClick={() => void read()}>Read current binding</button>
    <button type="button" disabled={blocked || !storageKey} onClick={() => { try { restore(); } catch (caught) { fail(caught, 'Original receipt could not be restored.'); } }}>Read retained check receipt</button>
    {current && <p>Binding {current.value.binding_id}, revision {current.value.revision}, generation {current.value.generation}: {current.value.state}; gaps: {current.value.gaps.join(', ') || 'none'}.</p>}
    <button type="button" disabled={blocked || !restored || !current || !active(current.value) || Boolean(unresolved)} onClick={() => void submit()}>Check binding</button>
    {receipt && <div>
      <p>Original check request {receipt.key}; binding {receipt.body.binding_id}; configuration revision {receipt.body.configuration_revision}; receipt: {receipt.state}.</p>
      {receipt.operation && <><p>Action: {receipt.operation.action}; operation {receipt.operation.operation_id}: {receipt.operation.state}; gaps: {receipt.operation.gaps.join(', ') || 'none'}.</p>
        {receipt.operation.gaps.filter(gap => guidance[gap]).map(gap => <p key={gap}>{guidance[gap]}</p>)}
        <button type="button" disabled={blocked} onClick={() => void inspect()}>Read original check status</button></>}
      {receipt.state === 'pending' && <><p>The outcome is uncertain. Keep the original request; a replay uses the same key, body and binding version. If Setup changed, have the service operator inspect this original key.</p>
        <label><input type="checkbox" checked={reviewed} disabled={blocked} onChange={event => setReviewed(event.target.checked)} />I reviewed the original check request</label>
        <button type="button" disabled={blocked || !reviewed || receipt.body.configuration_revision !== configuration.value.revision} onClick={() => void submit(true)}>Replay original check</button></>}
    </div>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
