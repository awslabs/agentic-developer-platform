import { apiClient } from './api';
import { productReceipt, type RecordReference } from './products';
import { type DevOpsConfiguration, type DevOpsBinding, type SecurityConfiguration, type SecurityBinding, type SecurityBindingOperation, type Versioned } from './l5Services';
import { consentPath, consentScopeMatches, devOpsSetupDraft, expiryValid, listOwnerGrants, matchesConsentReceipt, sameConsentScope, validConsentPreview,
  type ConsentPreview, type ConsentReceipt, type OwnerGrant } from './l5OwnerConsent';
import { bindingMatches, resourceSelection, validResourcePreview, validateResourceReadback, type ResourceSelection, type ResourceReadback, type AccessPolicyReadback } from './devOpsActivation';
import { securityCapabilityReady } from './securityCapabilityReadiness';

type Configuration = SecurityConfiguration | DevOpsConfiguration;
type Binding = SecurityBinding | DevOpsBinding;
type Service = 'security' | 'devops';
type Effect = { key: string; etag: string; body?: unknown; response?: Versioned<unknown> };
export interface RunPreparation {
  version: 1; service: Service; original: Versioned<Configuration>; binding: Versioned<Binding>;
  plan: 'security-check' | 'devops-resources' | 'execution'; selection?: ResourceSelection;
  effects: Partial<Record<'draft' | 'controlGrant' | 'check' | 'restore' | 'retire' | 'executionGrant', Effect>>;
  completed?: boolean; retiring?: OwnerGrant[];
}
export type PreparationProgress = { kind: 'replace'; grants: OwnerGrant[] } | { kind: 'consent'; preview: Versioned<ConsentPreview>; purpose: 'controlGrant' | 'executionGrant'; selection: Record<string, unknown> }
  | { kind: 'checking'; operation: SecurityBindingOperation } | { kind: 'done'; configuration: Versioned<Configuration> };
const requireValue: (condition: unknown, message: string) => asserts condition = (condition, message) => { if (!condition) throw new Error(message); };
const copy = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const noConcurrentWork = async (path: string) => {
  let cursor: string | undefined;
  const seen = new Set<string>();
  do {
    const page = await apiClient.get<{ items: { state: string; monitoring?: { state?: string } }[]; next_cursor?: string }>(`${path}/runs${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
    requireValue(page.items.every(run => ['succeeded', 'failed', 'cancelled', 'abandoned', 'blocked', 'expired'].includes(run.state) && run.monitoring?.state !== 'recovery_pending'),
      'An existing run still needs attention. Wait for it to finish or review its outcome before refreshing access.');
    cursor = page.next_cursor;
    requireValue(!cursor || !seen.has(cursor), 'Run history could not be fully checked. Refresh and try again.');
    if (cursor) seen.add(cursor);
  } while (cursor);
};
const active = (grant: OwnerGrant) => !grant.revoked_at && Date.parse(grant.expires_at) > Date.now() && grant.execution_scope?.purpose !== 'devops_native_ui';
export const runPreparationKey = (service: Service, configuration: Configuration) => `adp:run-preparation:${JSON.stringify([
  service, configuration.scope.tenant_id, configuration.scope.product_id, configuration.scope.project_id, configuration.configuration_id, configuration.scope.owner_id,
])}`;
export function readRunPreparation(service: Service, configuration: Configuration): RunPreparation | null {
  const raw = sessionStorage.getItem(runPreparationKey(service, configuration));
  if (!raw) return null;
  const saved = JSON.parse(raw) as RunPreparation;
  requireValue(saved.version === 1 && saved.service === service && saved.original?.value.configuration_id === configuration.configuration_id
    && sameConsentScope(saved.original.value.scope, configuration.scope) && saved.binding?.value.configuration_id === configuration.configuration_id
    && sameConsentScope(saved.binding.value.scope, configuration.scope) && ['security-check', 'devops-resources', 'execution'].includes(saved.plan)
    && saved.effects && Object.values(saved.effects).every(effect => effect && /^[A-Za-z0-9_-]{16,128}$/.test(effect.key) && typeof effect.etag === 'string'),
  'The retained access request could not be verified. Keep the original session and ask the Product owner to reconcile it.');
  return saved.completed ? null : saved;
}
export function retainRunPreparation(journal: RunPreparation) {
  sessionStorage.setItem(runPreparationKey(journal.service, journal.original.value), JSON.stringify(journal));
}

export function verificationDraft(configuration: DevOpsConfiguration): DevOpsConfiguration {
  const draft = copy(configuration);
  draft.settings.enabled = false;
  draft.settings.devops.trigger_policies = [];
  delete draft.settings.devops.access_policy_ref;
  const accounts = draft.settings.devops.accounts as { environments: { resource_refs?: RecordReference[] }[] }[];
  accounts.forEach(account => account.environments.forEach(environment => { environment.resource_refs = []; }));
  requireValue(devOpsSetupDraft(draft.settings, draft.scope), 'This setup needs a separate access review. Open Setup to review its integrations, schedules and workspace permissions.');
  return draft;
}
export async function beginRunPreparation(service: Service, configuration: Versioned<Configuration>, bindingId: string): Promise<RunPreparation> {
  requireValue(configuration.value.scope.owner_id && configuration.value.settings.enabled, 'Save an enabled configuration with its owner before refreshing run access.');
  const path = consentPath(configuration.value.scope.product_id, configuration.value.scope.project_id, service);
  const [current, binding] = await Promise.all([
    apiClient.requestWithEtag<Configuration>(`${path}/configuration`),
    apiClient.requestWithEtag<Binding>(`${path}/bindings/${encodeURIComponent(bindingId)}`),
  ]);
  requireValue(current.value.hash === configuration.value.hash && current.value.revision === configuration.value.revision && sameConsentScope(current.value.scope, configuration.value.scope) && current.etag === configuration.etag
    && binding.value.configuration_id === configuration.value.configuration_id && sameConsentScope(binding.value.scope, configuration.value.scope)
    && binding.value.binding_id === bindingId && binding.value.native_resource_ref && binding.value.state === 'ready',
  'The saved setup changed. Refresh this page before reviewing access.');
  const journal: RunPreparation = { version: 1, service, original: current, binding, plan: 'execution', effects: {} };
  if (service === 'security' && configuration.value.settings.capabilities.some(capability => !securityCapabilityReady(binding.value as SecurityBinding, capability))) {
    journal.plan = 'security-check';
    if ((configuration.value as SecurityConfiguration).settings.capabilities.includes('threat_model')) {
      requireValue(binding.value.readiness.some(item => item.kind === 'threat_model' && item.native_model_ref),
        'Choose the existing threat model in Setup before refreshing access.');
    }
  }
  if (service === 'devops') {
    const config = configuration.value as DevOpsConfiguration;
    const observed = (binding.value as DevOpsBinding).readiness.find(item => item.capability === 'resource_access' || item.kind === 'resource_access')?.observation as
      { resource_selection?: ResourceSelection; expires_at?: string } | undefined;
    // Stored observations select the same workload; they never provide fresh authority.
    if (!observed?.expires_at || !Number.isFinite(Date.parse(observed.expires_at)) || Date.parse(observed.expires_at) <= Date.now()) {
      const draft = verificationDraft(config);
      requireValue(observed?.resource_selection && bindingMatches(binding.value as DevOpsBinding, draft),
        'The saved workload verification is incomplete. Open Setup and verify the workload first.');
      const selected = observed.resource_selection;
      journal.selection = resourceSelection(draft, binding.value as DevOpsBinding, selected.role_arn, selected.resource_arn, selected.telemetry_arn);
      requireValue(sameConsentScope(journal.selection, selected), 'The saved workload identity changed. Review it in Setup.');
      const triggers = config.settings.devops.trigger_policies as Record<string, unknown>[];
      const accounts = config.settings.devops.accounts as { environments: { resource_refs?: RecordReference[] }[] }[];
      requireValue(triggers.length === 1 && triggers[0].kind === 'manual' && triggers[0].enabled === true && triggers[0].binding_id === bindingId
        && sameConsentScope((triggers[0].resource_refs as RecordReference[])?.map(ref => ref.record_id), [selected.resource_arn])
        && sameConsentScope((triggers[0].telemetry_refs as RecordReference[])?.map(ref => ref.record_id), [selected.telemetry_arn])
        && accounts.length === 1 && accounts[0].environments.length === 1
        && sameConsentScope(accounts[0].environments[0].resource_refs, triggers[0].resource_refs),
      'The saved setup has multiple workloads or schedules. Review their access together in Setup.');
      journal.plan = 'devops-resources';
    }
  }
  return journal;
}

/** Every write is retained before sending. Resuming follows the same receipts and
 * existing workspace; this workflow never creates a space, schedule or run. */
export async function advanceRunPreparation(journal: RunPreparation, persist: (journal: RunPreparation) => void,
  authorization?: { purpose: 'controlGrant' | 'executionGrant'; preview: Versioned<ConsentPreview>; expiresAt: string }, replacement?: OwnerGrant[]): Promise<PreparationProgress> {
  const original = journal.original.value;
  const { product_id: productId, project_id: projectId } = original.scope;
  const path = consentPath(productId, projectId, journal.service);
  const bindingId = journal.binding.value.binding_id;
  const bindingPath = `${path}/bindings/${encodeURIComponent(bindingId)}`;
  requireValue(!journal.completed && original.scope.owner_id, 'Refresh the saved setup before starting another access review.');
  const save = () => persist(copy(journal));
  const effect = async <T,>(name: keyof RunPreparation['effects'], endpoint: string, method: 'POST' | 'PUT' | 'DELETE', etag: string, body?: unknown): Promise<Versioned<T>> => {
    let retained = journal.effects[name];
    if (!retained) { retained = { key: productReceipt(), etag, ...(body === undefined ? {} : { body: copy(body) }) }; journal.effects[name] = retained; save(); }
    if (retained.response) return retained.response as Versioned<T>;
    // Pending effects always use their original body, expiry, precondition and receipt.
    const result = await apiClient.requestWithEtag<T>(endpoint, { method, headers: { 'If-Match': retained.etag, 'Idempotency-Key': retained.key }, body: retained.body });
    retained.response = result; save(); return result;
  };
  const readConfig = () => apiClient.requestWithEtag<Configuration>(`${path}/configuration`);
  let current = await readConfig();
  const allowed = [journal.original, journal.effects.draft?.response, journal.effects.restore?.response].filter(Boolean) as Versioned<Configuration>[];
  // A lost PUT response may be reconciled only against the exact next saved revision.
  for (const name of ['draft', 'restore'] as const) {
    const retained = journal.effects[name];
    if (retained && !retained.response) {
      const prior = name === 'draft' ? original : (journal.effects.draft?.response?.value as Configuration | undefined);
      if (prior && current.value.revision === prior.revision + 1 && sameSettings(current.value.settings, retained.body)
        && current.value.configuration_id === original.configuration_id && sameConsentScope(current.value.scope, original.scope)) {
        retained.response = current; save(); allowed.push(current);
      }
    }
  }
  requireValue(allowed.some(item => item.value.hash === current.value.hash && item.value.revision === current.value.revision)
    && current.value.configuration_id === original.configuration_id && sameConsentScope(current.value.scope, original.scope),
  'Setup changed during access review. Keep this request and review the current configuration before continuing.');
  let binding = await apiClient.requestWithEtag<Binding>(bindingPath);
  requireValue(binding.value.binding_id === bindingId && binding.value.configuration_id === original.configuration_id
    && sameConsentScope(binding.value.scope, original.scope) && sameConsentScope(binding.value.native_resource_ref, journal.binding.value.native_resource_ref)
    && sameConsentScope(binding.value.connection_ref, journal.binding.value.connection_ref), 'The saved workspace changed. Review the original access request in Setup.');

  const replaceActive = async (grants: OwnerGrant[], except?: string): Promise<PreparationProgress | null> => {
    const selected = grants.filter(grant => active(grant) && grant.delegation_id !== except);
    if (!selected.length) return null;
    requireValue(selected.every(grant => consentScopeMatches(grant, original) && grant.execution_scope?.created_by === original.scope.owner_id
      && grant.execution_scope?.configuration_id === original.configuration_id && grant.execution_scope?.binding_id === bindingId),
    'Another target has active access. Review its grant in Setup before refreshing this workload.');
    if (!journal.retiring && !replacement) return { kind: 'replace', grants: selected };
    if (!journal.retiring) {
      requireValue(sameConsentScope(selected, replacement), 'The existing access changed. Review the current grants before replacing them.');
      journal.retiring = copy(selected); save();
    }
    await noConcurrentWork(path);
    for (const grant of selected) {
      const reviewed = journal.retiring.find(item => item.delegation_id === grant.delegation_id);
      requireValue(reviewed && sameConsentScope(reviewed, grant), 'An active grant changed. Review it in Setup before continuing.');
      const revoked = await apiClient.requestWithEtag<OwnerGrant>(`${path}/delegations/${encodeURIComponent(grant.delegation_id)}`,
        { method: 'DELETE', headers: { 'If-Match': `"${grant.revision}"` } });
      requireValue(revoked.value.delegation_id === grant.delegation_id && revoked.value.revoked_at
        && sameConsentScope(revoked.value.execution_scope, grant.execution_scope), 'Access retirement was not confirmed. Continue this original request to read its outcome.');
    }
    return null;
  };

  const consent = async (purpose: 'controlGrant' | 'executionGrant', preview: Versioned<ConsentPreview>, selection: Record<string, unknown>): Promise<PreparationProgress | null> => {
    const grants = (await listOwnerGrants(productId, projectId, journal.service)).items;
    requireValue(grants.every(grant => consentScopeMatches(grant, original)), 'Access records belong to a different project. Reload the workspace.');
    const retained = journal.effects[purpose];
    if (retained) {
      const match = grants.find(grant => matchesConsentReceipt(grant, { scope: preview.value.scope, body: retained.body } as ConsentReceipt));
      if (match) { retained.response = { value: match, etag: `"${match.revision}"` }; save(); }
    }
    const ownId = (retained?.response?.value as OwnerGrant | undefined)?.delegation_id;
    const replace = await replaceActive(grants, ownId);
    if (replace) return replace;
    if (!retained && (!authorization || authorization.purpose !== purpose)) return { kind: 'consent', preview, purpose, selection };
    if (!retained) requireValue(authorization && expiryValid(authorization.expiresAt)
      && sameConsentScope(authorization.preview, preview), 'The reviewed scope changed or expired. Review access again.');
    const granted = await effect<OwnerGrant>(purpose, `${path}/delegations`, 'POST', preview.etag,
      { ...selection, scope_digest: preview.value.scope_digest, expires_at: authorization?.expiresAt });
    requireValue(sameConsentScope(granted.value.execution_scope, preview.value.scope) && active(granted.value)
      && Date.parse(granted.value.expires_at) === Date.parse((journal.effects[purpose]!.body as { expires_at: string }).expires_at),
    'The access grant is expired or its scope changed. Review the retained request in Setup.');
    return null;
  };

  if (journal.effects.check && !journal.effects.check.response) {
    // The provider may already have advanced the binding. Reconcile the original
    // request before requesting a new scope for that newer generation.
    await effect<SecurityBindingOperation>('check', `${path}/binding-operations`, 'POST', journal.effects.check.etag, journal.effects.check.body);
  }
  if (journal.plan !== 'execution' && !journal.effects.check?.response) {
    await noConcurrentWork(path);
    if (journal.plan === 'devops-resources') {
      if (!journal.effects.draft) {
        const grants = (await listOwnerGrants(productId, projectId, journal.service)).items;
        const replace = await replaceActive(grants);
        if (replace) return replace;
      }
      const draft = verificationDraft(original as DevOpsConfiguration);
      current = await effect<Configuration>('draft', `${path}/configuration`, 'PUT', journal.original.etag, draft.settings);
      requireValue(current.value.revision === original.revision + 1 && sameSettings(current.value.settings, draft.settings), 'The verification draft did not match the original workload. Review Setup.');
    }
    const selection = journal.plan === 'devops-resources'
      ? { binding_id: bindingId, for_control: true, control_action: 'devops_resource_verification', resource_selection: journal.selection! }
      : { binding_id: bindingId, for_control: true };
    const preview = await apiClient.requestWithEtag<ConsentPreview>(journal.plan === 'devops-resources' ? `${path}/delegation-scope`
      : `${path}/delegation-scope?for_control=true&binding_id=${encodeURIComponent(bindingId)}`,
    journal.plan === 'devops-resources' ? { method: 'POST', body: selection } : {});
    requireValue(journal.plan === 'devops-resources'
      ? validResourcePreview(preview, current.value as DevOpsConfiguration, binding.value as DevOpsBinding, journal.selection!)
      : validConsentPreview(preview, current.value, journal.service, { binding_id: bindingId, for_control: true }, binding.value), 'Access scope changed. Refresh the saved target before continuing.');
    const review = await consent('controlGrant', preview, selection);
    if (review) return review;
    const model = journal.binding.value.readiness.find(item => item.kind === 'threat_model')?.native_model_ref;
    const checked = await effect<SecurityBindingOperation>('check', `${path}/binding-operations`, 'POST', binding.etag,
      { action: 'check', configuration_revision: current.value.revision, binding_id: bindingId,
        ...(journal.selection ? { resource_selection: journal.selection } : {}), ...(journal.service === 'security' && model ? { native_model_ref: model } : {}) });
    requireValue(checked.value.action === 'check' && checked.value.binding_id === bindingId, 'The verification response did not match this workspace.');
  }
  if (journal.effects.check?.response) {
    const originalOperation = journal.effects.check.response.value as SecurityBindingOperation;
    const operation = await apiClient.get<SecurityBindingOperation>(`${path}/binding-operations/${encodeURIComponent(originalOperation.operation_id)}`);
    requireValue(operation.operation_id === originalOperation.operation_id && operation.binding_id === bindingId && operation.action === 'check', 'The original verification could not be matched.');
    if (['accepted', 'running', 'waiting'].includes(operation.state)) return { kind: 'checking', operation };
    requireValue(operation.state === 'succeeded', `Workload verification ${operation.state}. ${operation.gaps.join(', ') || 'Review the original operation in Setup before trying again.'}`);
    binding = await apiClient.requestWithEtag<Binding>(bindingPath);
    requireValue(binding.value.binding_id === bindingId && sameConsentScope(binding.value.scope, original.scope)
      && binding.value.generation === journal.binding.value.generation + 1
      && sameConsentScope(binding.value.native_resource_ref, journal.binding.value.native_resource_ref), 'Workspace verification changed its target. Review Setup.');
  }
  if (journal.plan === 'devops-resources') {
    const draft = journal.effects.draft!.response!.value as DevOpsConfiguration;
    if (!journal.effects.restore?.response) {
      const [resources, policy] = await Promise.all([
        apiClient.get<ResourceReadback>(`${bindingPath}/resources`), apiClient.get<AccessPolicyReadback>(`${bindingPath}/access-policy`),
      ]);
      validateResourceReadback(draft, binding.value as DevOpsBinding, resources, policy);
      requireValue(sameConsentScope(policy.document.resource_observation.resource_selection, journal.selection), 'The verified workload differs from the saved target.');
      const settings = copy((original as DevOpsConfiguration).settings);
      settings.devops.access_policy_ref = policy.policy_ref;
      (settings.devops.accounts as { environments: { resource_refs: RecordReference[] }[] }[])[0].environments[0].resource_refs = resources.resource_refs;
      settings.devops.trigger_policies[0] = { ...(settings.devops.trigger_policies[0] as Record<string, unknown>), resource_refs: resources.resource_refs, telemetry_refs: resources.telemetry_refs };
      current = await effect<Configuration>('restore', `${path}/configuration`, 'PUT', journal.effects.draft!.response!.etag, settings);
      requireValue(current.value.revision === draft.revision + 1 && sameSettings(current.value.settings, settings), 'The restored settings do not match the reviewed workload. Review Setup.');
    } else current = journal.effects.restore.response as Versioned<Configuration>;
    const own = journal.effects.controlGrant!.response!.value as OwnerGrant;
    const grants = (await listOwnerGrants(productId, projectId, journal.service)).items;
    const grant = grants.find(item => item.delegation_id === own.delegation_id);
    requireValue(grant && sameConsentScope(grant.execution_scope, own.execution_scope), 'The original verification grant could not be read.');
    if (!grant.revoked_at) {
      const revoked = await effect<OwnerGrant>('retire', `${path}/delegations/${encodeURIComponent(grant.delegation_id)}`, 'DELETE', `"${grant.revision}"`);
      requireValue(revoked.value.delegation_id === grant.delegation_id && revoked.value.revoked_at, 'Verification access retirement could not be confirmed.');
    }
  }
  if (journal.plan !== 'security-check') {
    const selection = { binding_id: bindingId, for_control: false };
    const preview = await apiClient.requestWithEtag<ConsentPreview>(`${path}/delegation-scope?for_control=false&binding_id=${encodeURIComponent(bindingId)}`);
    requireValue(validConsentPreview(preview, current.value, journal.service, selection, binding.value), 'Run access scope changed. Refresh the saved target.');
    const review = await consent('executionGrant', preview, selection);
    if (review) return review;
  }
  current = await readConfig();
  const expected = (journal.effects.restore?.response as Versioned<Configuration> | undefined) ?? journal.original;
  requireValue(current.value.hash === expected.value.hash && sameConsentScope(current.value.scope, original.scope), 'Setup changed before access review completed. Refresh Setup.');
  journal.completed = true; save();
  return { kind: 'done', configuration: current };
}
function sameSettings(actual: unknown, expected: unknown) {
  const normalize = (settings: unknown) => { const value = { ...(settings as Record<string, unknown>) }; delete value.monitoring; return value; };
  return sameConsentScope(normalize(actual), normalize(expected));
}
