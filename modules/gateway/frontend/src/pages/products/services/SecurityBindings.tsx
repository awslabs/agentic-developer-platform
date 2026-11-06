import { useCallback, useEffect, useRef, useState } from 'react';
import { productReceipt } from '@/services/products';
import { getSecurityBinding, getSecurityBindingOperation, listSecurityBindings, reconcileSecurityBindingOperation, startSecurityBindingOperation, securityReadinessSummary, matchesSecurityScope, type BindingAction, type BindingRequest, type SecurityBinding, type SecurityBindingOperation, type SecurityConfiguration, type Versioned } from '@/services/l5Services';

interface Props { productId: string; projectId: string; configuration: Versioned<SecurityConfiguration> | null; onAccessDenied?: () => void }

const controls: BindingAction[] = ['update', 'suspend', 'resume', 'unlink', 'cleanup'];
const sourceRegistrationGuidance = 'The source bucket and execution role registration could not be verified. Ask an authorized AWS operator to verify or register one approved source bucket and one approved execution role on this binding’s existing Security space, then run check on this binding. Check, update and resume do not register these resources.';

export default function SecurityBindings({ productId, projectId, configuration, onAccessDenied }: Props) {
  const [bindings, setBindings] = useState<SecurityBinding[]>([]);
  const [selected, setSelected] = useState('');
  const [detail, setDetail] = useState<Versioned<SecurityBinding> | null>(null);
  const [operation, setOperation] = useState<SecurityBindingOperation | null>(null);
  const [connectMode, setConnectMode] = useState<'create' | 'link' | ''>('');
  const [resourceId, setResourceId] = useState('');
  const [resourceRevision, setResourceRevision] = useState('');
  const [modelId, setModelId] = useState('');
  const [modelRevision, setModelRevision] = useState('');
  const [reconcileReason, setReconcileReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState<{ request: BindingRequest; etag?: string; receipt: string } | null>(null);
  const [error, setError] = useState('');
  const [stale, setStale] = useState(false);
  const selectedBinding = useRef('');
  const readGeneration = useRef(0);
  const revoked = useRef(false);
  const operationId = operation?.operation_id;

  const refresh = useCallback(async () => {
    if (revoked.current) return;
    const generation = ++readGeneration.current;
    setDetail(null);
    try {
      const page = await listSecurityBindings(productId, projectId);
      if (generation !== readGeneration.current) return;
      if (page.items.some(binding => !matchesSecurityScope(binding.scope, configuration?.value.scope, productId, projectId))) throw { status: 403 };
      const current = selected ? await getSecurityBinding(productId, projectId, selected) : null;
      if (generation !== readGeneration.current || selected !== selectedBinding.current) return;
      if (current && (current.value.binding_id !== selected || !matchesSecurityScope(current.value.scope, configuration?.value.scope, productId, projectId))) throw { status: 403 };
      const currentOperation = operationId ? await getSecurityBindingOperation(productId, projectId, operationId) : null;
      if (generation !== readGeneration.current || selected !== selectedBinding.current) return;
      setDetail(current);
      if (currentOperation) setOperation(currentOperation);
      setBindings(page.items);
      setError('');
      setStale(false);
    } catch (readError) {
      if (generation !== readGeneration.current) return;
      setBindings([]); setDetail(null); setOperation(null);
      if (typeof readError === 'object' && readError !== null && 'status' in readError && (readError.status === 403 || readError.status === 404)) onAccessDenied?.();
      setError('Bindings are unavailable for this workspace.');
    }
  }, [productId, projectId, selected, operationId, configuration, onAccessDenied]);

  useEffect(() => {
    if (configuration) void refresh();
    return () => { readGeneration.current += 1; };
  }, [configuration, refresh]);

  useEffect(() => {
    const available = bindings.filter(binding => binding.configuration_id === configuration?.value.configuration_id && !['cleaned', 'cleanup_pending', 'unlinked'].includes(binding.state));
    if (!selectedBinding.current && available.length === 1) {
      selectedBinding.current = available[0].binding_id;
      setSelected(available[0].binding_id);
    }
  }, [bindings, configuration]);

  function select(bindingId: string) {
    selectedBinding.current = bindingId;
    readGeneration.current++;
    setSelected(bindingId);
    setDetail(null);
    setModelId(''); setModelRevision('');
  }

  async function submit(request: BindingRequest, etag?: string, receipt = productReceipt()) {
    if (busy || stale || revoked.current) return;
    if (request.binding_id && (!detail || detail.value.binding_id !== selected || request.binding_id !== selectedBinding.current)) return;
    readGeneration.current++;
    setBusy(true);
    setError('');
    try {
      const accepted = await startSecurityBindingOperation(productId, projectId, request, etag, receipt);
      setUncertain(null);
      setOperation(accepted.value);
      setDetail(null);
      setBindings([]);
    } catch (requestError) {
      if (typeof requestError === 'object' && requestError !== null && 'status' in requestError && (requestError.status === 403 || requestError.status === 404)) {
        setUncertain(null); setBindings([]); setDetail(null); onAccessDenied?.();
        setError('Binding access is unavailable for this workspace.');
        return;
      }
      if (typeof requestError === 'object' && requestError !== null && 'status' in requestError && requestError.status === 412) {
        setUncertain(null);
        setDetail(null);
        setStale(true);
        setError('Binding changed elsewhere (412). Refresh and review its new revision before requesting another action.');
      } else {
        setUncertain({ request, etag, receipt });
        setError('Binding operation was not confirmed. Do not repeat a create with a new request; reconcile the original receipt.');
      }
    } finally { setBusy(false); }
  }

  async function control(action: BindingAction) {
    if (!detail || !configuration || detail.value.binding_id !== selectedBinding.current || detail.value.binding_id !== selected) return;
    if (action === 'cleanup' && (detail.value.ownership !== 'adp_created' || !window.confirm('Permanently delete this ADP-owned Security resource? Unlinking alone keeps it.'))) return;
    await submit({ action, configuration_revision: configuration.value.revision, binding_id: detail.value.binding_id,
      ...(action === 'cleanup' ? { owned_cleanup_confirmed: true as const } : {}) }, detail.etag);
  }

  async function selectModel() {
    if (!detail || !configuration || !modelId.trim() || !modelRevision.trim()
      || detail.value.binding_id !== selectedBinding.current || detail.value.binding_id !== selected) return;
    const reference = detail.value.connection_ref;
    if (!reference || reference.tenant_id !== configuration.value.scope.tenant_id || reference.project_id !== projectId) return;
    await submit({ action: 'check', configuration_revision: configuration.value.revision, binding_id: detail.value.binding_id,
      native_model_ref: { ...reference, record_type: 'threat_model', record_id: modelId.trim(), record_revision: modelRevision.trim() } }, detail.etag);
  }

  const readyForSetup = Boolean(configuration?.value.settings.connection_ref && configuration?.value.settings.region);
  const connection = configuration?.value.settings.connection_ref;
  const registration = detail?.value.readiness.find(item => item.kind === 'code_review_source_registration')?.registration;
  return <section aria-label="Security bindings" className="blueprint-card product-card space-y-4">
    <p>Use a saved Security workspace for assessments. To connect a new one, save setup and approve workspace access first.</p>
    {error && <p role="alert">{error}</p>}
    {uncertain && <button type="button" disabled={busy} onClick={() => void submit(uncertain.request, uncertain.etag, uncertain.receipt)}>Retry original receipt</button>}
    {!configuration && <p>Save a Security configuration before managing bindings.</p>}
    {configuration && !readyForSetup && <p>Choose an AWS connection and region in the configuration first.</p>}
    <button type="button" className="product-text-button" disabled={busy || !configuration} onClick={() => void refresh()}>Refresh workspace</button>
    {configuration && <>
      <details className="security-binding-advanced" open={!bindings.length}><summary>{bindings.length ? 'Connect another workspace' : 'Connect a workspace'}</summary>
        <label>Connection method<select value={connectMode} disabled={busy || !!uncertain} onChange={event => setConnectMode(event.target.value as typeof connectMode)}><option value="">Choose how to connect</option><option value="create">Create a workspace with ADP</option><option value="link">Use an existing AWS Security workspace</option></select></label>
        {connectMode === 'create' && <><p>ADP creates the Security workspace in the saved AWS account and region using your approved access.</p><button type="button" disabled={busy || stale || !!uncertain || (operation !== null && !['succeeded', 'failed', 'blocked'].includes(operation.state)) || !readyForSetup} onClick={() => void submit({ action: 'create', configuration_revision: configuration.value.revision })}>Create workspace</button></>}
        {connectMode === 'link' && <><p>Copy the exact workspace ID and revision from its AWS record.</p><div><label>Existing Security resource ID <input className="rounded border p-2" value={resourceId} onChange={event => setResourceId(event.target.value)} /></label></div>
        <div><label>Resource revision <input className="rounded border p-2" value={resourceRevision} onChange={event => setResourceRevision(event.target.value)} /></label></div>
        <button type="button" disabled={busy || stale || !!uncertain || !readyForSetup || !resourceId.trim() || !resourceRevision.trim()} onClick={() => connection && void submit({ action: 'link', configuration_revision: configuration.value.revision,
          native_resource_ref: { ...connection, record_type: 'security_space', record_id: resourceId.trim(), record_revision: resourceRevision.trim() } })}>Connect existing workspace</button></>}
      </details>
      <label className="block">Saved workspace <select className="mt-1 block rounded border p-2" value={selected} disabled={busy} onChange={event => select(event.target.value)}>
        <option value="">Choose a saved workspace</option>
        {bindings.map(binding => <option key={binding.binding_id} value={binding.binding_id}>Workspace {binding.binding_id.slice(0, 8)} · {binding.state}</option>)}
      </select></label>
      {!bindings.length && <p>No workspace is connected. Approve workspace access, then choose how to connect above.</p>}
      {detail && detail.value.binding_id === selected && <div aria-label="Binding detail" className="space-y-2">
        <p>Workspace status: {detail.value.state}</p>
        <p>Verify the workspace after changing setup or renewing access. This may initialize the first threat model in an ADP-created space; it does not start an assessment.</p>
        {detail.value.gaps.includes('source_readiness_unverified') && <p>{sourceRegistrationGuidance}</p>}
        <button className="product-primary" type="button" disabled={busy || stale || !!uncertain || !readyForSetup} onClick={() => void control('check')}>Verify workspace</button>
        <details className="security-binding-advanced"><summary>Workspace details and maintenance</summary>
        <p>Binding {detail.value.binding_id} · {detail.value.ownership} · revision {detail.value.revision}</p>
        <p>Readiness: {securityReadinessSummary(detail.value.readiness) || 'Not yet verified'}</p>
        <dl>
          <div><dt>Security space</dt><dd>{detail.value.native_resource_ref?.record_id ?? 'Not recorded'}</dd></div>
          <div><dt>Registered source bucket</dt><dd>{typeof registration?.bucket === 'string' ? registration.bucket : 'Not verified'}</dd></div>
          <div><dt>Registered execution role</dt><dd>{typeof registration?.service_role === 'string' ? registration.service_role : 'Not verified'}</dd></div>
        </dl>
        <p>Gaps: {detail.value.gaps.join(', ') || 'None reported'}</p>
        {detail.value.readiness.some(item => item.kind === 'threat_model_selection_required' || item.kind === 'threat_model_outcome_unknown')
          && <p>The existing threat model needs verification or an explicit selection. Select its exact ID and revision below; retain this binding and its operation history.</p>}
        {configuration.value.settings.capabilities?.includes('threat_model') && <details>
          <summary>Select an existing threat model</summary>
          <p>Enter the model ID and exact creation-time revision from its provider record. Selection verifies that the model belongs to this Security space and setup; it cannot create a model or start an assessment.</p>
          <label>Existing threat model ID <input value={modelId} disabled={busy || !!uncertain} onChange={event => setModelId(event.target.value)} /></label>
          <label>Threat model revision <input value={modelRevision} disabled={busy || !!uncertain} onChange={event => setModelRevision(event.target.value)} /></label>
          <button type="button" disabled={busy || stale || !!uncertain || !readyForSetup || !modelId.trim() || !modelRevision.trim()} onClick={() => void selectModel()}>Select and verify existing threat model</button>
        </details>}
        <p>Maintenance actions change this workspace. Update and resume can initialize a threat model under current authorization.</p>
        <div className="flex flex-wrap gap-2">{controls.filter(action => action !== 'cleanup' || detail.value.ownership === 'adp_created').map(action =>
          <button type="button" key={action} disabled={busy || stale || !!uncertain || !readyForSetup} onClick={() => void control(action)}>{action === 'cleanup' ? 'Delete owned resource (confirm)' : action}</button>)}</div></details>
      </div>}
    </>}
    {operation && <div aria-label="Binding operation" className="space-y-2">
      <p>{operation.action} operation {operation.operation_id}: {operation.state}. {operation.gaps.join(', ')}</p>
      {operation.gaps.includes('source_readiness_unverified') && <p>{sourceRegistrationGuidance}</p>}
      {operation.state === 'outcome_unknown' && <><label>Reason to reconcile <input value={reconcileReason} onChange={event => setReconcileReason(event.target.value)} /></label>
        <button type="button" disabled={busy || !reconcileReason.trim()} onClick={async () => {
          setBusy(true);
          try { setOperation((await reconcileSecurityBindingOperation(productId, projectId, operation.operation_id, reconcileReason.trim())).value); setError(''); }
          catch (requestError) {
            if (typeof requestError === 'object' && requestError !== null && 'status' in requestError && (requestError.status === 403 || requestError.status === 404)) {
              revoked.current = true; readGeneration.current++;
              setBindings([]); setDetail(null); setOperation(null); setUncertain(null);
              setResourceId(''); setResourceRevision(''); setReconcileReason('');
              onAccessDenied?.();
              setError('Binding access is unavailable for this workspace.');
            } else setError('Reconciliation was not confirmed. Refresh the original operation before trying again.');
          }
          finally { setBusy(false); }
        }}>Reconcile original operation</button></>}
    </div>}
  </section>;
}
