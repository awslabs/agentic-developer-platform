import { ServiceDisclosure } from './L5ServiceJourney';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { productReceipt } from '@/services/products';
import type { Versioned } from '@/services/l5Services';
import {
  consentScopeMatches, consentWorkers, devOpsSetupDraft, devOpsSetupTarget, expiryValid, grantOwnerConsent, listConsentBindings, listOwnerGrants,
  matchesConsentReceipt, previewOwnerConsent, revokeOwnerConsent, sameConsentScope, validConsentPreview,
  type ConsentBinding, type ConsentConfiguration, type ConsentPreview, type ConsentReceipt,
  type ConsentScope, type ConsentSelection, type ConsentService, type OwnerGrant,
} from '@/services/l5OwnerConsent';

interface Props {
  service: ConsentService; productId: string; projectId: string;
  configuration: ConsentConfiguration | null; disabled?: boolean; guided?: boolean; onAttention?: (message: string) => void;
}
const statusOf = (error: unknown) => (error as { status?: number } | null)?.status;
const denied = (error: unknown) => [401, 403, 404].includes(statusOf(error) ?? 0);
const errorMessage = (error: unknown) => {
  const detail = (error as { detail?: { code?: string; message?: string } } | null)?.detail;
  return typeof detail?.message === 'string' ? detail.message : 'Read the current consent and review its scope before trying again.';
};
function ScopeDetails({ scope }: { scope: ConsentScope }) {
  const nativeTarget = scope.setup_action === 'link' ? scope.native_resource_ref as { record_id: string; source_connection_id: string } : null;
  return <div className="space-y-2">
    <p>Owner: {scope.created_by}. Configuration {scope.configuration_id}, revision {scope.configuration_revision}.</p>
    <p>Account: {scope.provider_account_id}; region: {scope.region}; role: {scope.provider_role_arn}.</p>
    <p>Binding: {scope.binding_id ?? 'initial setup without an existing binding'}; generation: {scope.binding_generation}.</p>
    <p>Capabilities: {scope.capabilities.join(', ') || 'none — setup only'}. Visibility: {scope.visibility}.</p>
    <p>Permitted operations: {scope.operations.join(', ')}.</p>
    {nativeTarget && <p>Link existing DevOps space: {nativeTarget.record_id}; hosting connection: {nativeTarget.source_connection_id}. This reads and links the selected customer-owned space; it does not create a space or authorize execution.</p>}
    <ul aria-label="Consent source revisions">{scope.sources.map((source, index) => <li key={index}>
      {source.record_type}: {source.record_id}; revision {source.record_revision}; connection {source.source_connection_id}
    </li>)}</ul>
    <details><summary>Complete reviewed scope</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(scope, null, 2)}</pre></details>
  </div>;
}

function ConsentSection({ service, children, guided }: { service: ConsentService; children: ReactNode; guided?: boolean }) {
  if (guided) return <div className="security-guided-access">{children}</div>;
  return service === 'devops' ? <div className="devops-setup-fields"><h4>Owner consent</h4>{children}</div>
    : <ServiceDisclosure title="Owner consent" hint="Review access, scope and expiry">{children}</ServiceDisclosure>;
}

export default function L5OwnerConsent(props: Props) {
  const configuration = props.configuration;
  return <section aria-label="Owner consent" className="space-y-3" onKeyDown={event => {
    // This panel can live inside a configuration form. Enter in consent inputs must not save that form.
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
  }}>
    <ConsentSection service={props.service} guided={props.guided}>
    <p>Only the current Product and configuration owner can grant consent. Review the saved scope and choose an expiry; consent is never renewed automatically.</p>
    {configuration && configuration.scope.product_id === props.productId && configuration.scope.project_id === props.projectId
      ? <ConsentPanel {...props} configuration={configuration}
          key={[props.service, configuration.scope.tenant_id, props.productId, props.projectId, configuration.configuration_id,
            configuration.scope.owner_id, configuration.revision, configuration.hash].join(':')} />
      : <p>Save and load a service configuration before reviewing consent.</p>}
    </ConsentSection>
  </section>;
}

function ConsentPanel({ service, productId, projectId, configuration, disabled = false, guided = false, onAttention }: Props & { configuration: ConsentConfiguration }) {
  const [grants, setGrants] = useState<OwnerGrant[] | null>(null);
  const [bindings, setBindings] = useState<ConsentBinding[]>([]);
  const [mode, setMode] = useState('');
  const [bindingId, setBindingId] = useState('');
  const [preview, setPreview] = useState<Versioned<ConsentPreview> | null>(null);
  const [expiry, setExpiry] = useState('');
  const [hours, setHours] = useState('8');
  const [reviewed, setReviewed] = useState(false);
  const [receipt, setReceipt] = useState<ConsentReceipt | null>(null);
  const [busy, setBusy] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [revokeSelection, setRevokeSelection] = useState('');
  const [revokeUncertain, setRevokeUncertain] = useState('');
  const [readAfterRequest, setReadAfterRequest] = useState(false);
  const request = useRef(0);
  useEffect(() => () => { request.current += 1; }, []);
  const storageKey = typeof configuration.scope.owner_id === 'string' && configuration.scope.owner_id.trim().length > 0 ? `adp:l5-consent:${JSON.stringify([
    configuration.scope.tenant_id, productId, projectId, service, configuration.configuration_id, configuration.scope.owner_id,
  ])}` : null;
  const pending = receipt?.state === 'pending' ? receipt : null;
  useEffect(() => { onAttention?.(error || (pending || revokeUncertain ? 'An access request needs review. Read its original outcome before trying again.' : '')); }, [error, pending, revokeUncertain, onAttention]);
  const blocked = disabled || busy;
  const active = grants?.some(grant => !grant.revoked_at && Date.parse(grant.expires_at) > Date.now()
    && !(service === 'devops' && grant.execution_scope?.purpose === 'devops_native_ui')) ?? false;
  const selectedBinding = bindings.find(binding => binding.binding_id === bindingId);
  const setupEligible = service === 'devops' && devOpsSetupDraft(configuration.settings, configuration.scope);
  const setupTarget = service === 'devops' ? devOpsSetupTarget(configuration.settings, configuration.scope) : null;
  const selection: ConsentSelection = mode === 'setup' ? { for_control: true, control_action: 'devops_setup' }
    : mode === 'initial' ? { for_control: false }
    : { binding_id: bindingId, for_control: mode === 'control' };
  const requiresBinding = mode === 'execution' || mode === 'control';
  const canGrant = Boolean(storageKey && (configuration.settings.enabled
    || (setupEligible && (pending?.body.control_action === 'devops_setup' || mode === 'setup'))));

  function retain(value: ConsentReceipt) {
    // A storage failure prevents a new grant from being sent without its original request.
    if (!storageKey) throw new Error('Configuration owner is required to retain consent.');
    sessionStorage.setItem(storageKey, JSON.stringify(value));
    setReceipt(value);
  }
  function restore(): ConsentReceipt | null {
    if (!storageKey) return receipt;
    const raw = sessionStorage.getItem(storageKey);
    if (!raw) return receipt;
    const stored = JSON.parse(raw) as ConsentReceipt;
    if (!stored.scope || !consentScopeMatches(stored.scope, configuration)
      || stored.scope.service_id !== consentWorkers[service] || stored.scope.created_by !== configuration.scope.owner_id
      || stored.scope.configuration_id !== configuration.configuration_id || !/^[A-Za-z0-9_-]{16,128}$/.test(stored.key)
      || !stored.body || !/^[a-f0-9]{64}$/.test(stored.body.scope_digest)
      || Object.keys(stored.body).some(field => !['scope_digest', 'expires_at', 'binding_id', 'for_control', 'control_action'].includes(field))
      || (stored.body.control_action !== undefined && stored.body.control_action !== 'devops_setup')
      || !Number.isFinite(Date.parse(stored.body.expires_at)) || typeof stored.body.for_control !== 'boolean'
      || !['pending', 'confirmed', 'rejected', 'expired'].includes(stored.state)) throw new Error('Stored consent request is invalid.');
    return stored;
  }
  function validateGrant(grant: OwnerGrant) {
    if (!consentScopeMatches(grant, configuration) || grant.service_id !== consentWorkers[service]
      || !grant.delegation_id || !Number.isInteger(grant.revision) || grant.revision < 1
      || !Number.isFinite(Date.parse(grant.expires_at)) || !Array.isArray(grant.capabilities)
      || !Array.isArray(grant.operations) || !Array.isArray(grant.sources)
      || grant.sources.some(source => !source || source.tenant_id !== configuration.scope.tenant_id || source.project_id !== projectId)
      || (grant.execution_scope && (!consentScopeMatches(grant.execution_scope, configuration)
        || grant.execution_scope.service_id !== consentWorkers[service]
        || (configuration.scope.owner_id && grant.execution_scope.created_by !== configuration.scope.owner_id)))) throw { status: 403 };
  }
  function fail(caught: unknown, fallback: string) {
    setPreview(null); setReviewed(false); setGrants(null); setReadAfterRequest(false);
    if (denied(caught)) {
      setAccessDenied(true); setGrants(null); setBindings([]); setReceipt(null); setRevokeSelection('');
      setError('Owner consent is unavailable for this account or project. Sign in as its current Product and configuration owner, then read consent again.');
    } else setError(`${fallback} ${errorMessage(caught)}`);
  }
  async function readConsent() {
    if (blocked) return;
    const epoch = ++request.current;
    setBusy(true); setError(''); setMessage(''); setPreview(null); setReviewed(false); setRevokeSelection('');
    try {
      // Read grants independently of binding availability so revocation remains usable after setup breaks.
      const page = await listOwnerGrants(productId, projectId, service);
      if (epoch !== request.current) return;
      page.items.forEach(validateGrant);
      setGrants(page.items); setAccessDenied(false); setReadAfterRequest(true);
      let stored = restore();
      if (stored?.state === 'pending') {
        const matches = page.items.filter(grant => matchesConsentReceipt(grant, stored!));
        if (matches.length === 1) {
          stored = { ...stored, state: 'confirmed', delegation_id: matches[0].delegation_id };
          retain(stored); setMessage('Matching consent is recorded for the retained scope and expiry. Its current status is shown below; the original request key is retained.');
        }
      }
      setReceipt(stored);
      if (revokeUncertain && page.items.some(grant => grant.delegation_id === revokeUncertain && grant.revoked_at)) {
        setMessage('Revocation is confirmed by the current grant record.'); setRevokeUncertain('');
      }
      try {
        const found = await listConsentBindings(productId, projectId, service);
        if (epoch !== request.current) return;
        if (found.items.some(binding => !consentScopeMatches(binding.scope, configuration)
          || binding.configuration_id !== configuration.configuration_id || !Number.isInteger(binding.generation))) throw { status: 403 };
        const available = found.items.filter(binding => !['cleaned', 'cleanup_pending', 'unlinked'].includes(binding.state));
        setBindings(available);
        if (guided && !stored && !mode) {
          setMode(available.length === 0 ? 'initial' : 'execution');
          setBindingId(available.length === 1 ? available[0].binding_id : '');
        }
      } catch (caught) {
        if (epoch !== request.current) return;
        setBindings([]);
        if (denied(caught)) throw caught;
        setError('Bindings could not be read. Existing consent can still be inspected and revoked; read consent again before choosing a binding.');
      }
    } catch (caught) { if (epoch === request.current) fail(caught, 'Consent could not be read.'); }
    finally { if (epoch === request.current) setBusy(false); }
  }
  async function reviewScope() {
    if (blocked || accessDenied || grants === null || (pending && !readAfterRequest)
      || (!pending && (!mode || (requiresBinding && !selectedBinding) || (mode === 'setup' && !setupEligible)))) return;
    const epoch = ++request.current;
    const chosen = pending ? { binding_id: pending.body.binding_id, for_control: pending.body.for_control, control_action: pending.body.control_action } : selection;
    setBusy(true); setError(''); setPreview(null); setReviewed(false);
    try {
      const result = await previewOwnerConsent(productId, projectId, service, chosen);
      if (epoch !== request.current) return;
      const binding = bindings.find(item => item.binding_id === chosen.binding_id);
      if ((chosen.binding_id && !binding) || !validConsentPreview(result, configuration, service, chosen, binding)) throw { status: 412 };
      if (pending && (pending.etag !== result.etag || pending.body.scope_digest !== result.value.scope_digest
        || !sameConsentScope(pending.scope, result.value.scope))) throw { status: 412 };
      setPreview(result);
      if (guided && !pending) setExpiry(new Date(Date.now() + Number(hours) * 3600000).toISOString());
    } catch (caught) { if (epoch === request.current) fail(caught, 'The saved scope or binding changed. Reload the saved configuration and read consent again.'); }
    finally { if (epoch === request.current) setBusy(false); }
  }
  async function grant() {
    const expiresAt = pending?.body.expires_at ?? expiry;
    if (blocked || !preview || !reviewed || accessDenied || !canGrant || active || !expiryValid(expiresAt)) return;
    const epoch = ++request.current;
    const original: ConsentReceipt = pending ?? { key: productReceipt(), etag: preview.etag, scope: preview.value.scope,
      body: { ...selection, scope_digest: preview.value.scope_digest, expires_at: new Date(expiresAt).toISOString() }, state: 'pending' };
    setBusy(true); setError(''); setMessage(''); setReadAfterRequest(false);
    let sent = false;
    try {
      retain(original);
      sent = true;
      const response = await grantOwnerConsent(productId, projectId, service, original);
      if (epoch !== request.current) return;
      validateGrant(response.value);
      if (!matchesConsentReceipt(response.value, original) || response.etag !== `"${response.value.revision}"`) throw new Error();
      retain({ ...original, state: 'confirmed', delegation_id: response.value.delegation_id });
      setGrants(current => [response.value, ...(current ?? []).filter(item => item.delegation_id !== response.value.delegation_id)]);
      setPreview(null); setReviewed(false); setExpiry('');
      setMessage('Owner consent recorded for the reviewed scope and expiry. Setup, controls and runs remain separate actions.');
    } catch (caught) {
      if (epoch !== request.current) return;
      // A later rejection cannot prove an earlier uncertain submission never succeeded.
      if (sent && !pending && [400, 401, 403, 404, 409, 412, 422, 428].includes(statusOf(caught) ?? 0)) {
        retain({ ...original, state: 'rejected' });
      }
      fail(caught, sent ? 'Consent was not confirmed. Read consent to reconcile the original request; do not create a replacement grant.'
        : 'The request could not be retained locally, so consent was not sent.');
    } finally { if (epoch === request.current) setBusy(false); }
  }
  async function revoke(grant: OwnerGrant) {
    if (blocked || accessDenied || revokeSelection !== grant.delegation_id) return;
    const epoch = ++request.current;
    setBusy(true); setError(''); setMessage(''); setRevokeSelection(''); setPreview(null); setReviewed(false);
    try {
      setRevokeUncertain(grant.delegation_id);
      const response = await revokeOwnerConsent(productId, projectId, service, grant);
      if (epoch !== request.current) return;
      validateGrant(response.value);
      if (response.value.delegation_id !== grant.delegation_id || !response.value.revoked_at
        || response.etag !== `"${response.value.revision}"`) throw new Error();
      setGrants(current => current?.map(item => item.delegation_id === grant.delegation_id ? response.value : item) ?? null);
      setRevokeUncertain(''); setMessage('Consent revoked. Future operations must pass current authorization checks.');
    } catch (caught) { if (epoch === request.current) fail(caught, 'Revocation was not confirmed. Read consent before deciding whether another revocation request is needed.'); }
    finally { if (epoch === request.current) setBusy(false); }
  }

  return <div className="space-y-3">
    <p>Consent applies to saved configuration revision {configuration.revision}. Unsaved setup edits are excluded.</p>
    {!storageKey && <p>The saved configuration is missing its owner identity. Reload it before granting consent. Authenticated grant readback and revocation remain available.</p>}
    <button type="button" disabled={blocked} onClick={() => void readConsent()}>Read owner consent</button>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {!configuration.settings.enabled && <p>Execution consent requires an enabled saved configuration. Complete its prerequisites and explicitly enable it before granting execution consent.</p>}
    {service === 'devops' && !configuration.settings.enabled && <>
      <p>Setup consent requires a saved disabled Investigation draft with integrations, triggers, prevention and native actions disabled. Select either creation of one new space or adoption of the exact saved existing space. It permits one setup operation, without investigation or native UI authority.</p>
      {setupTarget?.action === 'link' && <p>Saved adoption target: {setupTarget.native_resource_ref.record_id}. Consent permits reading and linking this space through its saved hosting connection. The space remains customer-owned.</p>}
      {!setupEligible && <p>This saved configuration does not match an eligible creation or adoption setup selection.</p>}
      <p>Use Verify resources and activate Investigation below to review resource-verification consent and activate the verified space. Then grant execution consent here for the new saved revision.</p>
    </>}
    {grants !== null && !accessDenied && <>
      <div aria-label="Recorded owner grants">
        <h4>{guided ? 'Saved access' : 'Recorded grants'}</h4>{!grants.length && <p>No owner grants were returned.</p>}
        {grants.map(item => <details key={item.delegation_id}>
          <summary>{guided ? 'Access' : item.delegation_id}: {item.revoked_at ? 'revoked' : Date.parse(item.expires_at) <= Date.now() ? 'expired' : 'active'}; expires {item.expires_at}</summary>
          <p>Revision {item.revision}; revoked at: {item.revoked_at ?? 'not revoked'}.</p>
          <p>Capabilities: {item.capabilities.join(', ') || 'control scope'}; binding generation {item.binding_generation}.</p>
          <pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(item.execution_scope ?? { operations: item.operations, sources: item.sources }, null, 2)}</pre>
          {!item.revoked_at && <>
            <label><input type="checkbox" checked={revokeSelection === item.delegation_id} disabled={blocked}
              onChange={event => setRevokeSelection(event.target.checked ? item.delegation_id : '')} /> Revoke grant {item.delegation_id}</label>
            <button type="button" disabled={blocked || revokeSelection !== item.delegation_id} onClick={() => void revoke(item)}>Revoke selected consent</button>
          </>}
        </details>)}
      </div>
      {active && <p>An active grant already exists. Reuse its exact scope or explicitly revoke it before granting a different scope or expiry.</p>}
      {revokeUncertain && <p>Revocation outcome for {revokeUncertain} is uncertain. Read owner consent to inspect its current revision.</p>}
      {receipt && <details open={receipt.state === 'pending'}><summary>Original consent request: {receipt.state}</summary>
        <p>Receipt key: {receipt.key}; expiry: {receipt.body.expires_at}; delegation: {receipt.delegation_id ?? 'not confirmed'}.</p>
        <pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify({ etag: receipt.etag, body: receipt.body }, null, 2)}</pre>
      </details>}
      {pending ? <p>The original request is retained. Read consent first, then review and replay that same request only if its scope and expiry remain valid.</p> : <>
        <label>Consent purpose <select value={mode} disabled={blocked} onChange={event => {
          setMode(event.target.value); setBindingId(guided && bindings.length === 1 ? bindings[0].binding_id : ''); setPreview(null); setReviewed(false);
        }}><option value="">Choose a purpose</option>
          {service !== 'devops' && <option value="initial">{guided ? 'Create or connect a workspace and run assessments' : 'Initial binding generation — setup and execution'}</option>}
          {service === 'devops' && <option value="setup" disabled={!setupEligible}>{configuration.settings.devops?.space_mode === 'adopt'
            ? 'Link the saved existing DevOps space — setup only' : 'Create one DevOps space — setup only'}</option>}
          <option value="execution">{guided ? 'Run assessments in a saved workspace' : 'Execution on an existing binding'}</option>
          <option value="control">{guided ? 'Verify or update a saved workspace and run assessments' : 'Next binding generation — controls and execution'}</option>
        </select></label>
        {requiresBinding && <label>Consent binding <select value={bindingId} disabled={blocked} onChange={event => {
          setBindingId(event.target.value); setPreview(null); setReviewed(false);
        }}><option value="">Choose a binding</option>{bindings.map(binding => <option key={binding.binding_id} value={binding.binding_id}>
          {binding.binding_id} — generation {binding.generation}, {binding.state}
        </option>)}</select></label>}
      </>}
      {service === 'security' && (mode === 'control' || mode === 'initial') && <p>Security provider setup and binding controls may initialize the first threat model in an ADP-owned space. Consent does not perform that action itself.</p>}
      <button type="button" disabled={blocked || Boolean(pending && !readAfterRequest) || (!pending && (!mode || (requiresBinding && !selectedBinding) || (mode === 'setup' && !setupEligible)))} onClick={() => void reviewScope()}>Review consent scope</button>
      {preview && <div aria-label="Reviewed owner consent" className="space-y-2">
        {guided ? <><p>AWS account {preview.value.scope.provider_account_id} · {preview.value.scope.region}</p><p>Review the saved sources, permissions and exact target before approving.</p><details><summary>Review sources and permissions</summary><ScopeDetails scope={preview.value.scope} /><p>Scope digest: {preview.value.scope_digest}</p></details></> : <><ScopeDetails scope={preview.value.scope} /><p>Scope digest: {preview.value.scope_digest}</p></>}
        {pending ? <p>Original expiry: {pending.body.expires_at}</p> : guided ? <><label>Access duration<select value={hours} disabled={blocked} onChange={event => { setHours(event.target.value); setExpiry(new Date(Date.now() + Number(event.target.value) * 3600000).toISOString()); setReviewed(false); }}><option value="1">1 hour</option><option value="8">8 hours</option><option value="24">24 hours</option></select></label><p>Expires {expiry} (UTC)</p></> : <label>Consent expiry (UTC)
          <input value={expiry} placeholder="YYYY-MM-DDTHH:MM:SSZ" disabled={blocked} onChange={event => { setExpiry(event.target.value); setReviewed(false); }} />
        </label>}
        <p>{guided ? 'Access expires automatically. Renew it before running again or continuing daily schedules.' : 'Choose an explicit UTC expiry more than one minute and at most 24 hours from now. Daily schedules stop receiving authority when this consent expires.'}</p>
        <label><input type="checkbox" checked={reviewed} disabled={blocked} onChange={event => setReviewed(event.target.checked)} /> I reviewed this exact scope and expiry and authorize it as the owner.</label>
        <button type="button" disabled={blocked || active || !canGrant || !reviewed || !expiryValid(pending?.body.expires_at ?? expiry)} onClick={() => void grant()}>
          {pending ? 'Replay original consent request' : 'Grant reviewed consent'}
        </button>
      </div>}
      {pending && readAfterRequest && Date.parse(pending.body.expires_at) <= Date.now() && <button type="button" disabled={blocked} onClick={() => {
        retain({ ...pending, state: 'expired' }); setPreview(null); setReviewed(false); setMessage('The original request expiry has passed. Its receipt is retained; review a new scope before granting new consent.');
      }}>Close expired request</button>}
    </>}
  </div>;
}
