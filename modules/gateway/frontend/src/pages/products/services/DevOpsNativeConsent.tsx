import { useEffect, useRef, useState } from 'react';
import type { DevOpsBinding, DevOpsConfiguration, Versioned } from '@/services/l5Services';
import { expiryValid, listOwnerGrants, revokeOwnerConsent, sameConsentScope, type ConsentPreview, type OwnerGrant } from '@/services/l5OwnerConsent';
import { grantNativeConsent, nativeAccessError, nativeConsentMatches, previewNativeConsent, type NativeConsentRequest } from '@/services/devOpsNativeAccess';
import { productReceipt } from '@/services/products';

interface Props {
  productId: string; projectId: string; configuration: Versioned<DevOpsConfiguration>; binding: Versioned<DevOpsBinding>;
  onAttention?: (message: string) => void;
  disabled: boolean; onChange: () => void;
}

export default function DevOpsNativeConsent({ productId, projectId, configuration, binding, disabled, onChange, onAttention }: Props) {
  const [launcher, setLauncher] = useState(typeof configuration.value.settings.devops.launcher_role_arn === 'string' ? configuration.value.settings.devops.launcher_role_arn : '');
  const [operator, setOperator] = useState(typeof configuration.value.settings.devops.operator_role_arn === 'string' ? configuration.value.settings.devops.operator_role_arn : '');
  const [expiry, setExpiry] = useState(''); const [reviewed, setReviewed] = useState(false);
  const [preview, setPreview] = useState<Versioned<ConsentPreview> | null>(null);
  const [grants, setGrants] = useState<OwnerGrant[] | null>(null);
  const [pending, setPending] = useState<{ body: NativeConsentRequest; scope: ConsentPreview['scope']; key: string; etag: string } | null>(null);
  const [busy, setBusy] = useState(false); const [message, setMessage] = useState('');
  const alive = useRef(true);
  useEffect(() => { onAttention?.(pending ? 'Workspace sign-in has an unfinished access request. Review its original outcome before continuing.' : message); }, [pending, message, onAttention]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const validExpiry = expiryValid(expiry);
  const policy = { launcher_role_arn: launcher.trim(), operator_role_arn: operator.trim() };
  const active = grants?.some(grant => grant.active && !grant.revoked_at && Date.parse(grant.expires_at) > Date.now());
  function edit() { setPreview(null); setReviewed(false); setMessage(''); onChange(); }
  async function read() {
    setBusy(true); setMessage(''); onChange();
    try {
      const page = await listOwnerGrants(productId, projectId, 'devops');
      if (!alive.current) return;
      const current = page.items.filter(grant => grant.execution_scope?.purpose === 'devops_native_ui');
      if (current.some(grant => grant.tenant_id !== configuration.value.scope.tenant_id || grant.product_id !== productId || grant.project_id !== projectId)) throw new Error();
      setGrants(current);
      if (pending && current.some(grant => sameConsentScope(grant.execution_scope, pending.scope) && Date.parse(grant.expires_at) === Date.parse(pending.body.expires_at))) {
        setPending(null); setMessage('The original native UI grant is retained. Check native UI access before launching.');
      }
    } catch (error) { if (alive.current) { setGrants(null); setMessage(nativeAccessError(error)); } }
    finally { if (alive.current) setBusy(false); }
  }
  async function review() {
    if (disabled || busy || pending || !launcher.trim() || !operator.trim()) return;
    setBusy(true); setPreview(null); setReviewed(false); setMessage(''); onChange();
    try {
      const result = await previewNativeConsent(productId, projectId, binding.value.binding_id, policy);
      if (!alive.current) return;
      if (!nativeConsentMatches(result.value, configuration, binding, policy) || result.etag !== configuration.etag) throw new Error();
      setPreview(result);
    } catch (error) { if (alive.current) setMessage(nativeAccessError(error)); }
    finally { if (alive.current) setBusy(false); }
  }
  async function grant() {
    if (disabled || busy || active || (!pending && (!validExpiry || !preview || !reviewed))) return;
    const request = pending ?? { body: { binding_id: binding.value.binding_id, for_control: true as const, control_action: 'devops_native_ui' as const,
      native_ui_policy: policy, scope_digest: preview!.value.scope_digest, expires_at: expiry }, scope: preview!.value.scope,
      key: productReceipt(), etag: preview!.etag };
    setPending(request); setBusy(true); setMessage(''); onChange();
    try {
      const result = await grantNativeConsent(productId, projectId, request.body, request.etag, request.key);
      if (!alive.current) return;
      if (!sameConsentScope(result.value.execution_scope, request.scope) || Date.parse(result.value.expires_at) !== Date.parse(expiry)) throw new Error();
      setGrants(current => [result.value, ...(current ?? [])]); setPending(null); setPreview(null); setReviewed(false);
      setMessage('Native UI consent saved. Check native UI access, then choose Open AWS DevOps Agent.');
    } catch (error) { if (alive.current) {
      const status = (error as { status?: number })?.status;
      if (status && [400, 401, 403, 404, 409, 412, 422].includes(status)) { setPending(null); setPreview(null); setReviewed(false); }
      setMessage(`${nativeAccessError(error)} Read current native UI grants before making another request.`);
    } }
    finally { if (alive.current) setBusy(false); }
  }
  async function revoke(grant: OwnerGrant) {
    if (disabled || busy) return;
    setBusy(true); setMessage(''); onChange();
    try {
      const result = await revokeOwnerConsent(productId, projectId, 'devops', grant);
      if (alive.current) { setGrants(current => current?.map(item => item.delegation_id === grant.delegation_id ? result.value : item) ?? null); setMessage('Native UI consent revoked.'); }
    } catch (error) { if (alive.current) { setGrants(null); setMessage(`${nativeAccessError(error)} Read current native UI grants to confirm revocation.`); } }
    finally { if (alive.current) setBusy(false); }
  }
  return <section className="devops-setup-fields" aria-label="Workspace sign-in access" onKeyDown={event => { if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault(); }}>
    <h4>Workspace sign-in access</h4>
    <p>The Product owner can allow sign-in to this DevOps space for a chosen time. This access is separate from permission to run investigations.</p>
    {(!launcher || !operator) && <div className="devops-next-step"><strong>One-time SSO setup needed</strong><p>A platform administrator must enable the operator app for this space and provide its separate launcher and operator roles. Add those roles below, then review and grant sign-in access.</p></div>}
    <details><summary>Session permissions and expiry</summary>
    <p>Only the Product owner can grant access. This separate grant permits a human SSO launch into this space. It does not authorize agent investigations, setup, prevention or changes to IAM roles. Existing operator-app permissions determine actions inside the native app. Revocation blocks new ADP handoffs under this grant. Already-issued handoffs and native app sessions may remain valid until provider expiry. The 15-minute bound applies to launcher authority, not the duration of a native operator session.</p>    </details>

    <button type="button" disabled={disabled || busy} onClick={() => void read()}>Read native UI grants</button>
    {grants?.map(grant => <p key={grant.delegation_id}>Native UI grant {grant.delegation_id}: {grant.active ? 'active' : 'inactive'}; expires {grant.expires_at}.
      <button type="button" disabled={disabled || busy || Boolean(grant.revoked_at)} onClick={() => void revoke(grant)}>Revoke native UI grant</button></p>)}
    <div className="devops-form-grid"><label>Native UI launcher role ARN <input value={launcher} placeholder="Platform administrator setup required" disabled={disabled || busy || Boolean(pending)} onChange={event => { setLauncher(event.target.value); edit(); }} /></label>
    <label>Native UI operator role ARN <input value={operator} placeholder="Platform administrator setup required" disabled={disabled || busy || Boolean(pending)} onChange={event => { setOperator(event.target.value); edit(); }} /></label>
    </div>
    <button type="button" disabled={disabled || busy || Boolean(pending) || !launcher.trim() || !operator.trim()} onClick={() => void review()}>Review native UI scope</button>
    {preview && <div><p>Account {preview.value.scope.provider_account_id}; region {preview.value.scope.region}; space {binding.value.native_resource_ref?.record_id}; binding generation {preview.value.scope.binding_generation}; configuration revision {preview.value.scope.configuration_revision}.</p>
      <p>Allowed ADP action: launchNativeBrowser. Launcher {policy.launcher_role_arn}; operator {policy.operator_role_arn}.</p>
      <details><summary>Complete native UI scope</summary><pre>{JSON.stringify(preview.value.scope, null, 2)}</pre></details>
      <p>Choose an expiry more than 1 minute and no more than 24 hours from now. Consent is never extended automatically.</p>
      <label>Native UI consent expiry (UTC) <input placeholder="YYYY-MM-DDTHH:mm:ssZ" value={expiry} disabled={busy || Boolean(pending)} onChange={event => { setExpiry(event.target.value); setReviewed(false); }} /></label>
      <label><input type="checkbox" checked={reviewed} disabled={busy || Boolean(pending)} onChange={event => setReviewed(event.target.checked)} /> I reviewed this exact space, role policy and expiry.</label>
      <button type="button" disabled={disabled || busy || Boolean(pending) || Boolean(active) || !reviewed || !validExpiry} onClick={() => void grant()}>Grant native UI consent</button>
    </div>}
    {pending && <button type="button" disabled={disabled || busy} onClick={() => void grant()}>Retry original native UI consent</button>}
    {message && <p role="status">{message}</p>}
  </section>;
}
