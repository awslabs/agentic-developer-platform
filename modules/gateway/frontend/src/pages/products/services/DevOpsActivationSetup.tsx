import { useEffect, useRef, useState } from 'react';
import { getDevOpsBindingOperation, getDevOpsConfiguration, type Versioned } from '@/services/l5Services';
import { consentScopeMatches, devOpsSetupDraft, expiryValid, listOwnerGrants, sameConsentScope, type ConsentPreview, type OwnerGrant } from '@/services/l5OwnerConsent';
import { productReceipt } from '@/services/products';
import { investigationGapAction } from '@/services/devOpsReadiness';
import {
  activateInvestigation, activationMatches, bindingMatches, buildActivation, grantResourceConsent, listResourceBindings,
  previewResourceConsent, readResourceAccessPolicy, readResourceBinding, readVerifiedResources, readDevOpsConfigurationVersion,
  referenceValid, resourceGrantMatches, resourceSelection, validResourcePreview, validateResourceReadback, verifyResource, type AccessPolicyReadback, type ActivationReceipt, type ActivationSettings,
  type ResourceBinding, type ResourceConsentReceipt, type ResourceReadback, type SetupConfiguration, type SetupJournal,
} from '@/services/devOpsActivation';

interface Props {
  productId: string; projectId: string; configuration: Versioned<SetupConfiguration>;
  onAttention?: (message: string) => void;
  disabled?: boolean; onActivated: (configuration: Versioned<SetupConfiguration>, confirmedRevision?: number) => void;
}
const statusOf = (error: unknown) => (error as { status?: number } | null)?.status;
const rejected = (error: unknown) => [400, 401, 403, 404, 409, 412, 422, 428].includes(statusOf(error) ?? 0);
const denied = (error: unknown) => [401, 403, 404].includes(statusOf(error) ?? 0);
function explanation(error: unknown) {
  const detail = (error as { detail?: { code?: string; message?: string } } | null)?.detail;
  if (detail?.message) return `${detail.message}${detail.code ? ` (${detail.code})` : ''}`;
  if (detail?.code) return `Reason: ${detail.code}.`;
  return error instanceof Error ? error.message : '';
}
const active = (grant: OwnerGrant) => !grant.revoked_at && Date.parse(grant.expires_at) > Date.now()
  && grant.execution_scope?.purpose !== 'devops_native_ui';

export default function DevOpsActivationSetup(props: Props) {
  const config = props.configuration.value;
  return <section aria-label="Investigation activation setup" className="space-y-3" onKeyDown={event => {
    if (event.key === 'Enter' && event.target instanceof HTMLInputElement) event.preventDefault();
  }}>
    <h4>{config.settings.enabled ? "Workload setup saved" : "Verify resources and activate Investigation"}</h4>
    <p>{config.settings.enabled ? "The workload and logs are saved for investigations. Current run access is checked in Runs & schedules." : "Complete these steps after creating the DevOps workspace. They use the saved configuration; local setup edits are excluded."}</p>
    <ActivationPanel {...props} key={[config.scope.tenant_id, props.productId, props.projectId, config.configuration_id, config.scope.owner_id].join(':')} />
  </section>;
}

function ActivationPanel({ productId, projectId, configuration, disabled = false, onActivated, onAttention }: Props) {
  const config = configuration.value;
  const owner = config.scope.owner_id;
  const storageKey = owner?.trim() ? `adp:devops-activation:${JSON.stringify([config.scope.tenant_id, productId, projectId, config.configuration_id, owner])}` : null;
  const [bindings, setBindings] = useState<ResourceBinding[]>([]);
  const [bindingId, setBindingId] = useState('');
  const [binding, setBinding] = useState<Versioned<ResourceBinding> | null>(null);
  const [grants, setGrants] = useState<OwnerGrant[] | null>(null);
  const [role, setRole] = useState('');
  const [lambda, setLambda] = useState('');
  const [logs, setLogs] = useState('');
  const [expiry, setExpiry] = useState('');
  const [preview, setPreview] = useState<Versioned<ConsentPreview> | null>(null);
  const [consentReviewed, setConsentReviewed] = useState(false);
  const [verifyReviewed, setVerifyReviewed] = useState(false);
  const [journal, setJournal] = useState<SetupJournal>({});
  const [readBack, setReadBack] = useState(false);
  const [resources, setResources] = useState<ResourceReadback | null>(null);
  const [policy, setPolicy] = useState<AccessPolicyReadback | null>(null);
  const saved = config.settings as Partial<ActivationSettings>;
  const [classification, setClassification] = useState(saved.classification ?? 'internal');
  const [maxCost, setMaxCost] = useState(saved.limits?.max_cost ?? '300');
  const [deadline, setDeadline] = useState(String(saved.limits?.deadline_seconds ?? 3600));
  const [polls, setPolls] = useState(String(saved.limits?.max_polls ?? 60));
  const [activation, setActivation] = useState<ActivationSettings | null>(null);
  const [activationReviewed, setActivationReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [accessDenied, setAccessDenied] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const epoch = useRef(0);
  useEffect(() => {
    epoch.current += 1;
    setReadBack(false); setPreview(null); setBinding(null); setResources(null); setPolicy(null); setActivation(null);
    setConsentReviewed(false); setActivationReviewed(false); setVerifyReviewed(false); setBusy(false);
    return () => { epoch.current += 1; };
  }, [config.hash]);
  const blocked = disabled || busy;
  const pendingConsent = journal.consent?.state === 'pending' ? journal.consent : null;
  const pendingCheck = journal.check?.state === 'pending' ? journal.check : null;
  const pendingActivation = journal.activation?.state === 'pending' ? journal.activation : null;
  const pending = Boolean(pendingConsent || pendingCheck || pendingActivation);
  const verificationRunning = Boolean(journal.check?.operation && !['succeeded', 'blocked', 'failed'].includes(journal.check.operation.state));
  const setupEligible = devOpsSetupDraft(config.settings, config.scope);
  useEffect(() => { onAttention?.(error || (pending || verificationRunning ? 'Workload setup has an unfinished request. Review its original outcome before continuing.' : '')); }, [error, pending, verificationRunning, onAttention]);

  function retain(next: SetupJournal) {
    if (!storageKey) throw new Error('Reload a configuration with its current owner before retaining a request.');
    sessionStorage.setItem(storageKey, JSON.stringify(next));
    setJournal(next);
  }
  function restore(): SetupJournal {
    const raw = storageKey ? sessionStorage.getItem(storageKey) : null;
    if (!raw) return journal;
    const value = JSON.parse(raw) as SetupJournal;
    for (const receipt of [value.consent, value.check, value.activation].filter(item => item !== undefined)) {
      if (!receipt || !/^[A-Za-z0-9_-]{16,128}$/.test(receipt.key) || !/^"[^"\s]+"$/.test(receipt.etag)
        || !['pending', 'confirmed', 'rejected'].includes(receipt.state) || !receipt.body) throw new Error('The retained setup receipt is invalid.');
    }
    if (value.consent && (!consentScopeMatches(value.consent.scope, config) || value.consent.scope.created_by !== owner
      || value.consent.scope.configuration_id !== config.configuration_id || value.consent.scope.service_id !== 'l5-devops-worker'
      || value.consent.body.control_action !== 'devops_resource_verification' || value.consent.body.for_control !== true
      || value.consent.body.binding_id !== value.consent.scope.binding_id
      || !sameConsentScope(value.consent.body.resource_selection, value.consent.scope.resource_selection))) throw new Error('The retained consent scope changed.');
    if (value.check && (value.check.body.action !== 'check' || !value.consent
      || value.check.body.binding_id !== value.consent.body.binding_id
      || value.check.body.configuration_revision !== value.consent.scope.configuration_revision
      || !sameConsentScope(value.check.body.resource_selection, value.consent.body.resource_selection))) throw new Error('The retained verification scope changed.');
    if (value.activation && (value.activation.configuration_id !== config.configuration_id
      || value.activation.body.service_id !== 'devops' || value.activation.body.enabled !== true
      || !Number.isInteger(value.activation.previous_revision) || value.activation.previous_revision < 1
      || !referenceValid(value.activation.body.profile_ref, config, 'product')
      || value.activation.body.profile_ref.record_id !== productId)) throw new Error('The retained activation scope changed.');
    return value;
  }
  function fail(caught: unknown, action: string) {
    setPreview(null); setConsentReviewed(false); setVerifyReviewed(false); setActivation(null); setActivationReviewed(false); setReadBack(false);
    setResources(null); setPolicy(null); setBinding(null);
    if (denied(caught)) { setAccessDenied(true); setGrants(null); setBindings([]); }
    setError(`${action} ${denied(caught) ? 'Sign in as the current Product and configuration owner and read setup again.' : explanation(caught)}`);
  }
  async function task(action: () => Promise<void>, fallback: string) {
    const request = ++epoch.current;
    setBusy(true); setError(''); setMessage('');
    try { await action(); }
    catch (caught) { if (request === epoch.current) fail(caught, fallback); }
    finally { if (request === epoch.current) setBusy(false); }
  }
  function assertIdentity(current: Versioned<SetupConfiguration>) {
    if (!consentScopeMatches(current.value.scope, config) || current.value.scope.owner_id !== owner
      || current.value.configuration_id !== config.configuration_id || current.value.settings.service_id !== 'devops') throw { status: 403 };
    if (!Number.isInteger(current.value.revision) || current.value.revision < 1 || current.etag !== `"${current.value.hash}"`) {
      throw new Error('The configuration revision token did not match its readback. Read setup again before continuing.');
    }
  }
  function assertCurrent(current: Versioned<SetupConfiguration>) {
    assertIdentity(current);
    if (current.etag !== configuration.etag || current.value.hash !== config.hash || current.value.revision !== config.revision) {
      throw new Error('The saved configuration changed. Your local edits are retained; refresh the configuration and review the current revision.');
    }
  }
  function checkGrantScope(items: OwnerGrant[]) {
    if (items.some(item => !consentScopeMatches(item, config) || item.service_id !== 'l5-devops-worker'
      || (item.execution_scope && (item.execution_scope.created_by !== owner || !consentScopeMatches(item.execution_scope, config))))) throw { status: 403 };
  }
  async function readSetup() {
    if (blocked) return;
    await task(async () => {
      const request = epoch.current;
      let next = restore();
      const current = await getDevOpsConfiguration(productId, projectId);
      if (request !== epoch.current) return;
      assertIdentity(current);
      if (next.activation?.state === 'pending' && current.value.revision > next.activation.previous_revision + 1) {
        const revision = next.activation.previous_revision + 1;
        let historical: Versioned<SetupConfiguration>;
        try { historical = await readDevOpsConfigurationVersion(productId, projectId, revision); }
        catch (caught) {
          if (request !== epoch.current) return;
          throw new Error(`Original activation is unresolved: configuration revision ${revision} could not be read. Ask the Product owner or administrator to restore access to that revision and its sources, then read setup again. The current revision ${current.value.revision} must not be used to retry this request. ${explanation(caught)}`);
        }
        if (request !== epoch.current) return;
        if (!activationMatches(historical, config, next.activation)) {
          throw new Error(`Original activation is unresolved: configuration revision ${revision} does not match the retained settings. Ask the Product owner or administrator to review that revision and request ${next.activation.key}. A replacement request is blocked; do not retry using current revision ${current.value.revision}.`);
        }
        next = { ...next, activation: { ...next.activation, state: 'confirmed' } };
        retain(next); setMessage(`The original activation is confirmed in configuration revision ${revision}. Later edits produced current revision ${current.value.revision}; review those current settings before running Investigation.`);
        if (current.value.revision !== config.revision) onActivated(current, revision);
        return;
      }
      if (next.activation && (next.activation.state === 'pending' || current.value.revision !== config.revision)
        && activationMatches(current, config, next.activation)) {
        next = { ...next, activation: { ...next.activation, state: 'confirmed' } };
        retain(next); setMessage('The saved configuration confirms manual Investigation activation. Review execution consent for this new revision before starting a run.');
        // A reload can already have loaded the confirmed revision. Do not make
        // its current drafts stale again just because a receipt was recovered.
        if (current.value.revision !== config.revision) onActivated(current);
        return;
      }
      assertCurrent(current);
      const found = await listResourceBindings(productId, projectId);
      if (request !== epoch.current) return;
      const consent = await listOwnerGrants(productId, projectId, 'devops');
      if (request !== epoch.current) return;
      checkGrantScope(consent.items);
      if (next.consent?.state === 'pending') {
        const matching = consent.items.filter(item => resourceGrantMatches(item, next.consent!));
        if (matching.length === 1) next = { ...next, consent: { ...next.consent, state: 'confirmed' } };
      }
      if (found.items.some(item => !consentScopeMatches(item.scope, config) || item.configuration_id !== config.configuration_id)) throw { status: 403 };
      setBindings(found.items.filter(item => bindingMatches(item, config)));
      setGrants(consent.items); setAccessDenied(false); setReadBack(true); retain(next);
      setMessage('Saved setup and consent read. Select the existing space binding to continue.');
    }, 'Setup could not be read. No new action was started.');
  }
  async function previewConsent() {
    if (blocked || accessDenied || !readBack || grants === null || !setupEligible || !owner?.trim() || pendingCheck || pendingActivation || verificationRunning) return;
    await task(async () => {
      const request = epoch.current;
      const id = pendingConsent?.body.binding_id ?? bindingId;
      const current = await readResourceBinding(productId, projectId, id);
      if (request !== epoch.current) return;
      const selection = pendingConsent?.body.resource_selection ?? resourceSelection(config, current.value, role.trim(), lambda.trim(), logs.trim());
      const result = await previewResourceConsent(productId, projectId, { binding_id: id, for_control: true, control_action: 'devops_resource_verification', resource_selection: selection });
      if (request !== epoch.current) return;
      if (!validResourcePreview(result, config, current.value, selection)
        || (pendingConsent && (result.etag !== pendingConsent.etag || !sameConsentScope(result.value.scope, pendingConsent.scope)
          || result.value.scope_digest !== pendingConsent.body.scope_digest))) throw new Error('The resource consent scope changed. Read setup and review its current revision.');
      setBinding(current); setBindingId(id); setPreview(result); setConsentReviewed(false);
    }, 'Resource consent could not be reviewed.');
  }
  async function grant() {
    if (blocked || !preview || !binding || !consentReviewed || !grants || grants.some(active) || !expiryValid(pendingConsent?.body.expires_at ?? expiry)) return;
    await task(async () => {
      const request = epoch.current;
      const original: ResourceConsentReceipt = pendingConsent ?? { key: productReceipt(), etag: preview.etag, scope: preview.value.scope,
        body: { binding_id: binding.value.binding_id, for_control: true, control_action: 'devops_resource_verification',
          resource_selection: preview.value.scope.resource_selection as ResourceConsentReceipt['body']['resource_selection'],
          scope_digest: preview.value.scope_digest, expires_at: new Date(expiry).toISOString() }, state: 'pending' };
      const retained = pendingConsent ? journal : { ...journal, check: undefined };
      retain({ ...retained, consent: original }); setReadBack(false);
      try {
        const result = await grantResourceConsent(productId, projectId, original);
        if (request !== epoch.current) return;
        checkGrantScope([result.value]);
        if (!resourceGrantMatches(result.value, original) || result.etag !== `"${result.value.revision}"`) throw new Error('Consent readback did not match the reviewed scope.');
        retain({ ...retained, consent: { ...original, state: 'confirmed' } });
        setGrants([result.value]); setPreview(null); setConsentReviewed(false);
        setMessage('Resource consent recorded. Read setup again, then explicitly verify resource access.');
      } catch (caught) {
        if (request !== epoch.current) return;
        if (!pendingConsent && rejected(caught)) retain({ ...retained, consent: { ...original, state: 'rejected' } });
        throw caught;
      }
    }, 'Consent was not confirmed. Read setup to reconcile its original request before retrying.');
  }
  async function checkResource() {
    if (blocked || accessDenied || !readBack || !verifyReviewed || !journal.consent || pendingConsent || pendingActivation) return;
    await task(async () => {
      const request = epoch.current;
      let original = pendingCheck;
      if (!original) {
        if (journal.check?.operation && !['succeeded', 'blocked', 'failed'].includes(journal.check.operation.state)) throw new Error('Read the existing verification operation before starting another.');
        const consent = journal.consent!;
        if (!grants?.some(item => active(item) && resourceGrantMatches(item, consent))) throw new Error('Read setup and grant current resource-verification consent first.');
        const current = await readResourceBinding(productId, projectId, consent.body.binding_id);
        if (request !== epoch.current) return;
        if (!validResourcePreview({ value: { scope: consent.scope, scope_digest: consent.body.scope_digest, maximum_lifetime_seconds: 86400 }, etag: consent.etag }, config, current.value, consent.body.resource_selection)) {
          throw new Error('The binding generation or consent changed. Review new resource consent before another verification.');
        }
        original = { key: productReceipt(), etag: current.etag, body: { action: 'check', configuration_revision: config.revision,
          binding_id: current.value.binding_id, resource_selection: consent.body.resource_selection }, state: 'pending' };
      }
      retain({ ...journal, check: original }); setReadBack(false); setVerifyReviewed(false);
      try {
        const result = await verifyResource(productId, projectId, original);
        if (request !== epoch.current) return;
        if (!result.value.operation_id || result.value.action !== 'check' || result.value.binding_id !== original.body.binding_id) throw new Error('Verification receipt did not match this binding.');
        retain({ ...journal, check: { ...original, state: 'confirmed', operation: result.value } });
        setMessage('Verification request recorded. Read the original operation until its outcome is known.');
      } catch (caught) {
        if (request !== epoch.current) return;
        if (!pendingCheck && rejected(caught)) retain({ ...journal, check: { ...original, state: 'rejected' } });
        throw caught;
      }
    }, 'Verification was not confirmed. Read setup before retrying the same request; do not create a replacement operation.');
  }
  async function readOperation() {
    const check = journal.check;
    if (blocked || !check?.operation) return;
    await task(async () => {
      const request = epoch.current;
      const operation = await getDevOpsBindingOperation(productId, projectId, check.operation!.operation_id);
      if (request !== epoch.current) return;
      if (operation.operation_id !== check.operation!.operation_id || operation.action !== 'check' || operation.binding_id !== check.body.binding_id) throw new Error('The operation readback did not match its retained receipt.');
      retain({ ...journal, check: { ...check, operation } });
      setMessage(`Resource verification: ${operation.state}. ${operation.gaps.length ? operation.gaps.map(investigationGapAction).join(' ') : 'Read verified resources after success.'}`);
    }, 'The original verification operation could not be read. Keep its receipt and try the read again.');
  }
  async function readResources() {
    if (blocked || accessDenied || !readBack || pending) return;
    await task(async () => {
      const request = epoch.current;
      const id = bindingId || journal.check?.body.binding_id || journal.consent?.body.binding_id;
      if (!id) throw new Error('Select an existing binding.');
      const current = await readResourceBinding(productId, projectId, id);
      if (request !== epoch.current) return;
      if (!bindingMatches(current.value, config) || current.value.state !== 'ready' || current.value.gaps.length) throw new Error('Resolve the selected binding gaps and read its verification operation first.');
      const refs = await readVerifiedResources(productId, projectId, id);
      if (request !== epoch.current) return;
      const document = await readResourceAccessPolicy(productId, projectId, id);
      if (request !== epoch.current) return;
      validateResourceReadback(config, current.value, refs, document);
      setBinding(current); setBindingId(id); setResources(refs); setPolicy(document); setActivation(null); setActivationReviewed(false);
      setMessage('Verified workload references and access policy read. Set bounded limits and review activation before their evidence expires.');
    }, 'Verified resources are unavailable. Complete resource verification again if its readback expired.');
  }
  function reviewActivation() {
    if (blocked || !binding || !resources || !policy || pending) return;
    try {
      setActivation(buildActivation(config, binding.value, resources, policy, { classification, maxCost, deadline: Number(deadline), polls: Number(polls) }));
      setActivationReviewed(false); setError('');
    } catch (caught) { setActivation(null); setError(explanation(caught)); }
  }
  async function activate() {
    if (blocked || accessDenied || !readBack || !activationReviewed || (!activation && !pendingActivation)) return;
    await task(async () => {
      const request = epoch.current;
      const original: ActivationReceipt = pendingActivation ?? { key: productReceipt(), etag: configuration.etag, configuration_id: config.configuration_id,
        previous_revision: config.revision, body: activation!, state: 'pending' };
      if (original.etag !== configuration.etag || original.previous_revision !== config.revision) throw new Error('Read the original activation outcome against its saved revision before continuing.');
      retain({ ...journal, activation: original }); setReadBack(false);
      try {
        const result = await activateInvestigation(productId, projectId, original);
        if (request !== epoch.current) return;
        if (!activationMatches(result, config, original)) throw new Error('The activation response did not match the exact reviewed settings.');
        retain({ ...journal, activation: { ...original, state: 'confirmed' } });
        setActivation(null); setActivationReviewed(false);
        setMessage('Manual Investigation activated. Review execution consent for the new saved revision before starting a run.');
        onActivated(result);
      } catch (caught) {
        if (request !== epoch.current) return;
        if (!pendingActivation && rejected(caught)) retain({ ...journal, activation: { ...original, state: 'rejected' } });
        throw caught;
      }
    }, 'Activation was not confirmed. Read setup to compare the saved configuration with this retained request. Retry only with its original revision.');
  }
  const changeSelection = () => { setPreview(null); setConsentReviewed(false); setBinding(null); setResources(null); setPolicy(null); setActivation(null); };
  const changeLimits = () => { setActivation(null); setActivationReviewed(false); };
  return <div className="space-y-3">
    {!config.settings.enabled && <p>Verify the workload, activate investigations, then review execution consent in Owner consent.</p>}
    {!setupEligible && !config.settings.enabled && <p>Resource setup requires a saved disabled Investigation draft, one account and environment, a created or adopted space matching that saved selection, and native automation disabled.</p>}
    {!owner?.trim() && <p>Reload the configuration with its owner identity before making setup requests.</p>}
    <button type="button" disabled={blocked} onClick={() => void readSetup()}>Read resource setup and recover receipts</button>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {readBack && !accessDenied && (!config.settings.enabled || pending) && <>
      <label className="block">Resource verification binding <select value={bindingId} disabled={blocked || pending} onChange={event => { setBindingId(event.target.value); changeSelection(); }}>
        <option value="">Choose the created or adopted binding</option>{bindings.map(item => <option key={item.binding_id} value={item.binding_id}>{item.binding_id} — {item.state}</option>)}
      </select></label>
      {grants?.some(active) && <p>An owner grant is active. Review and explicitly revoke it in Owner consent before granting a different resource scope. Existing matching consent can be used for verification.</p>}
      <fieldset disabled={blocked || pending || !setupEligible}><legend>Selected Lambda workload</legend>
        <label className="block">Distinct resource IAM role ARN <input value={role} onChange={event => { setRole(event.target.value); changeSelection(); }} /></label>
        <label className="block">Lambda function ARN <input value={lambda} onChange={event => { setLambda(event.target.value); changeSelection(); }} /></label>
        <label className="block">CloudWatch log-group ARN ending in :* <input value={logs} onChange={event => { setLogs(event.target.value); changeSelection(); }} /></label>
      </fieldset>
      <button type="button" disabled={blocked || !setupEligible || !owner?.trim() || (!bindingId && !pendingConsent) || Boolean(pendingCheck || pendingActivation) || verificationRunning} onClick={() => void previewConsent()}>Review resource consent scope</button>
    </>}
    {preview && <div>
      <p>Consent permits resource verification and a credential session only. Verification assumes the selected resource role, reads IAM, Lambda and logs, and may create an AWS association in this DevOps space.</p>
      <details open><summary>Complete resource consent scope</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(preview.value.scope, null, 2)}</pre></details>
      <label className="block">Resource consent expiry (UTC) <input value={pendingConsent?.body.expires_at ?? expiry} disabled={blocked || Boolean(pendingConsent)} placeholder="YYYY-MM-DDTHH:mm:ssZ" onChange={event => setExpiry(event.target.value)} /></label>
      <p>Choose an expiry more than one minute and no more than 24 hours from now. Consent is never renewed automatically.</p>
      <label className="block"><input type="checkbox" checked={consentReviewed} disabled={blocked} onChange={event => setConsentReviewed(event.target.checked)} /> I reviewed the complete resource scope and its expiry.</label>
      <button type="button" disabled={blocked || !consentReviewed || !grants || grants.some(active) || !expiryValid(pendingConsent?.body.expires_at ?? expiry)} onClick={() => void grant()}>{pendingConsent ? 'Retry original resource consent' : 'Grant resource-verification consent'}</button>
    </div>}
    {journal.consent?.state === 'confirmed' && !accessDenied && <div>
      <p>Verification can create a native AWS association and read the selected role, Lambda and log group. It does not start Investigation.</p>
      <label className="block"><input type="checkbox" checked={verifyReviewed} disabled={blocked || !readBack} onChange={event => setVerifyReviewed(event.target.checked)} /> Verify the workload in the retained resource consent scope.</label>
      <button type="button" disabled={blocked || !readBack || !verifyReviewed || Boolean(pendingConsent || pendingActivation)} onClick={() => void checkResource()}>{pendingCheck ? 'Retry original resource verification' : 'Verify resource access'}</button>
    </div>}
    {journal.check && !accessDenied && <div>
      <p>Verification request {journal.check.key}: {journal.check.operation?.state ?? journal.check.state}.</p>
      {journal.check.operation && <><p>Verification: {journal.check.operation.state}.</p>
        {journal.check.operation.gaps.map(code => <p key={code}>{investigationGapAction(code)} <small>({code})</small></p>)}
        <button type="button" disabled={blocked} onClick={() => void readOperation()}>Read original resource verification</button></>}
    </div>}
    {readBack && !accessDenied && (!config.settings.enabled || pending) && <button type="button" disabled={blocked || pending || !setupEligible} onClick={() => void readResources()}>Read verified workload references</button>}
    {resources && policy && <div>
      <details><summary>Verified workload and access policy</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify({ ...resources, policy_ref: policy.policy_ref, expires_at: policy.document.resource_observation.expires_at }, null, 2)}</pre></details>
      <fieldset disabled={blocked || pending}><legend>Investigation settings</legend>
        <label className="block">Investigation classification <input value={classification} onChange={event => { setClassification(event.target.value); changeLimits(); }} /></label>
        <details><summary>Advanced run limits</summary>
        <label className="block">Cost estimate (USD) <input inputMode="decimal" value={maxCost} onChange={event => { setMaxCost(event.target.value); changeLimits(); }} /></label>
        <label className="block">Deadline in seconds (60–3600) <input inputMode="numeric" value={deadline} onChange={event => { setDeadline(event.target.value); changeLimits(); }} /></label>
        <label className="block">Maximum polls (1–60) <input inputMode="numeric" value={polls} onChange={event => { setPolls(event.target.value); changeLimits(); }} /></label>
        <p>One provider attempt and one concurrent run. No budget setup is required. The cost estimate is for planning, not a provider billing cap.</p>
        </details>
      </fieldset>
      <button type="button" disabled={blocked || pending} onClick={reviewActivation}>Review Investigation activation</button>
    </div>}
    {(activation || pendingActivation) && !accessDenied && <div>
      <p>Activation saves one manual Investigation trigger for the verified workload. It starts no paid run and enables no native schedule. Fresh goal evidence must confirm every native goal is disabled.</p>
      <details><summary>Complete activation settings</summary><pre className="overflow-auto whitespace-pre-wrap">{JSON.stringify(pendingActivation?.body ?? activation, null, 2)}</pre></details>
      <label className="block"><input type="checkbox" checked={activationReviewed} disabled={blocked || !readBack} onChange={event => setActivationReviewed(event.target.checked)} /> I reviewed these saved resources and run limits.</label>
      <button type="button" disabled={blocked || !readBack || !activationReviewed} onClick={() => void activate()}>{pendingActivation ? 'Retry original Investigation activation' : 'Activate manual Investigation'}</button>
    </div>}
    {pending && <p>An outcome is uncertain. Its original request and revision are retained in this owner’s session. Read setup before retrying; a retry keeps the exact body, expiry, request key and revision.</p>}
  </div>;
}
