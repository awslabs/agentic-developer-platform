import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiClient } from '@/services/api';
import { advanceRunPreparation, beginRunPreparation, readRunPreparation, retainRunPreparation, verificationDraft, type RunPreparation } from '@/services/l5RunPreparation';
import { configuration as draftFixture, binding as bindingFixture, selection, resources, policy, consent, grant as grantFixture } from '../fixtures/devOpsActivation';
import { sameConsentScope, type OwnerGrant } from '@/services/l5OwnerConsent';
import type { DevOpsBinding, DevOpsConfiguration, Versioned } from '@/services/l5Services';
vi.mock('@/services/api', () => ({ apiClient: { get: vi.fn(), requestWithEtag: vi.fn() } }));
const clone = <T,>(value: T): T => structuredClone(value);
let current: Versioned<DevOpsConfiguration>;
let binding: Versioned<DevOpsBinding>;
let grants: OwnerGrant[];
let operation: { operation_id: string; binding_id: string; action: 'check'; state: string; revision: number; gaps: string[] };
let mutations: { path: string; method: string; body: unknown; key?: string }[];
let receipts: Map<string, unknown>;
let lose: string;
let runs: { state: string }[];
const version = <T extends { hash?: string; revision?: number }>(value: T) => ({ value: clone(value), etag: `"${value.hash ?? value.revision}"` });
function preview(control: boolean) {
  const scope = { ...clone(consent.scope), configuration_revision: current.value.revision, configuration_hash: current.value.hash,
    binding_generation: binding.value.generation + Number(control) };
  if (!control) { delete scope.purpose; delete scope.control_action; delete scope.resource_selection; scope.capabilities = ['investigation']; scope.operations = ['credential:session', 'investigation:run']; }
  return { value: { scope, scope_digest: 'a'.repeat(64), maximum_lifetime_seconds: 86400 }, etag: current.etag };
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); receipts = new Map(); mutations = []; lose = ''; grants = []; runs = [{ state: 'succeeded' }];
  const config = clone(draftFixture); config.settings.enabled = true;
  Object.assign(config.settings, { limits: { max_cost: '300', deadline_seconds: 1800, max_polls: 40, currency: 'USD' } });
  config.settings.devops.accounts = [{ account_id: selection.account_id, environments: [{ environment_ref: config.settings.source_refs[0], resource_refs: resources.resource_refs }] }];
  config.settings.devops.trigger_policies = [{ trigger_id: 'keep-this-trigger', kind: 'manual', enabled: true, capability: 'investigation', binding_id: 'binding', environment_ref: config.settings.source_refs[0], ...clone(resources), extra: 'keep' }];
  current = version(config);
  binding = version({ ...clone(bindingFixture), readiness: [{ kind: 'resource_access', observation: { resource_selection: selection, expires_at: new Date(Date.now() - 1000).toISOString() } }] } as DevOpsBinding);
  operation = { operation_id: 'check-original', binding_id: 'binding', action: 'check', state: 'running', revision: 1, gaps: [] };
  vi.mocked(apiClient.get).mockImplementation(async path => {
    if (path.endsWith('/runs')) return { items: clone(runs) } as never;
    if (path.endsWith('/delegations')) return { items: clone(grants) } as never;
    if (path.includes('/binding-operations/')) return clone(operation) as never;
    if (path.endsWith('/resources')) return clone(resources) as never;
    if (path.endsWith('/access-policy')) return { ...clone(policy), document: { ...clone(policy.document), binding_generation: binding.value.generation,
      binding_revision: binding.value.revision, configuration_revision: current.value.revision,
      resource_observation: { resource_selection: selection, checked_at: new Date().toISOString(), expires_at: new Date(Date.now() + 900_000).toISOString() } } } as never;
    throw new Error(`Unexpected GET ${path}`);
  });
  vi.mocked(apiClient.requestWithEtag).mockImplementation(async (path, options = {}) => {
    const method = options.method ?? 'GET';
    if (path.endsWith('/configuration') && method === 'GET') return clone(current) as never;
    if (path.endsWith('/bindings/binding')) return clone(binding) as never;
    if (path.includes('/delegation-scope')) return preview(!path.includes('for_control=false')) as never;
    const key = (options.headers as Record<string, string>)?.['Idempotency-Key'];
    mutations.push({ path, method, body: clone(options.body), key });
    if (key && receipts.has(key)) return clone(receipts.get(key)) as never;
    let result: unknown;
    if (path.endsWith('/configuration') && method === 'PUT') {
      current = version({ ...current.value, settings: clone(options.body) as DevOpsConfiguration['settings'], revision: current.value.revision + 1, hash: `hash-${current.value.revision + 1}` }); result = current;
    } else if (path.endsWith('/delegations')) {
      const body = options.body as { control_action?: string; expires_at: string };
      const scope = preview(body.control_action === 'devops_resource_verification').value.scope;
      const grant = { ...clone(grantFixture), delegation_id: `grant-${grants.length + 1}`, execution_scope: scope, capabilities: scope.capabilities, expires_at: body.expires_at };
      grants.push(grant); result = version(grant);
    } else if (path.endsWith('/binding-operations')) {
      binding = version({ ...binding.value, generation: binding.value.generation + 1, revision: binding.value.revision + 1 }); result = version(operation);
    } else if (path.includes('/delegations/') && method === 'DELETE') {
      const grant = grants.find(item => path.endsWith(`/${item.delegation_id}`))!;
      grant.revoked_at = new Date().toISOString(); grant.revision++; grant.active = false; result = version(grant);
    } else throw new Error(`Unexpected mutation ${method} ${path}`);
    if (key) receipts.set(key, clone(result));
    if (lose && path.endsWith(lose)) { lose = ''; throw new Error('Lost response'); }
    return clone(result) as never;
  });
});
const persist = (journal: RunPreparation) => retainRunPreparation(journal);
async function begin() { const journal = await beginRunPreparation('devops', clone(current), 'binding'); persist(journal); return journal; }
async function allow(journal: RunPreparation, progress: Awaited<ReturnType<typeof advanceRunPreparation>>) {
  if (progress.kind !== 'consent') throw new Error('Expected consent review');
  return advanceRunPreparation(journal, persist, { purpose: progress.purpose, preview: progress.preview, expiresAt: new Date(Date.now() + 3600_000).toISOString() });
}
describe('guided renewal of saved run access', () => {
  it('verifies the same workload, preserves settings, and requires explicit execution consent without starting a run', async () => {
    const original = clone(current);
    const journal = await begin();
    expect(journal.plan).toBe('devops-resources'); expect(mutations).toEqual([]);
    const review = await advanceRunPreparation(journal, persist);
    expect(review.kind).toBe('consent'); expect(current.value.settings.enabled).toBe(false);
    expect(grants).toHaveLength(0);
    expect((await allow(journal, review)).kind).toBe('checking');
    operation.state = 'succeeded';
    const execution = await advanceRunPreparation(journal, persist);
    expect(execution.kind).toBe('consent'); expect(current.value.settings.enabled).toBe(true);
    expect(current.value.settings.devops.trigger_policies).toEqual(original.value.settings.devops.trigger_policies);
    expect((current.value.settings as unknown as { limits: unknown }).limits).toEqual((original.value.settings as unknown as { limits: unknown }).limits);
    expect(current.value.settings.devops.knowledge_policy).toEqual(original.value.settings.devops.knowledge_policy);
    expect(grants[0].revoked_at).toBeTruthy();
    expect((await allow(journal, execution)).kind).toBe('done');
    expect(readRunPreparation('devops', current.value)).toBeNull();
    expect(mutations.filter(item => item.path.endsWith('/binding-operations'))).toHaveLength(1);
    expect(mutations.every(item => !item.path.endsWith('/runs'))).toBe(true);
    expect(mutations.find(item => item.path.endsWith('/binding-operations'))?.body).toEqual(expect.objectContaining({ action: 'check', binding_id: 'binding', resource_selection: selection }));
  });
  it('reconciles a lost draft save from exact readback without overwriting it again', async () => {
    const journal = await begin(); lose = '/configuration';
    await expect(advanceRunPreparation(journal, persist)).rejects.toThrow('Lost response');
    const restored = readRunPreparation('devops', current.value)!;
    expect((await advanceRunPreparation(restored, persist)).kind).toBe('consent');
    expect(mutations.filter(item => item.method === 'PUT')).toHaveLength(1);
  });
  it('reconciles a lost consent response and verifies once', async () => {
    const journal = await begin(); const review = await advanceRunPreparation(journal, persist); lose = '/delegations';
    await expect(allow(journal, review)).rejects.toThrow('Lost response');
    const restored = readRunPreparation('devops', current.value)!;
    expect((await advanceRunPreparation(restored, persist)).kind).toBe('checking');
    expect(grants).toHaveLength(1);
    expect(mutations.filter(item => item.path.endsWith('/delegations'))).toHaveLength(1);
  });
  it('replays the original verification receipt after the binding advanced and its response was lost', async () => {
    const journal = await begin(); const review = await advanceRunPreparation(journal, persist); lose = '/binding-operations';
    await expect(allow(journal, review)).rejects.toThrow('Lost response');
    const restored = readRunPreparation('devops', current.value)!;
    expect((await advanceRunPreparation(restored, persist)).kind).toBe('checking');
    const checks = mutations.filter(item => item.path.endsWith('/binding-operations'));
    expect(checks).toHaveLength(2); expect(checks[0]).toEqual(checks[1]); expect(binding.value.generation).toBe(3);
  });
  it('does not change settings while another run is active', async () => {
    runs = [{ state: 'running' }]; const journal = await begin();
    await expect(advanceRunPreparation(journal, persist)).rejects.toThrow(/existing run/); expect(mutations).toEqual([]);
  });
  it('does not replace changed setup or a changed target', async () => {
    const journal = await begin(); current.value.hash = 'another-owner-edit';
    await expect(advanceRunPreparation(journal, persist)).rejects.toThrow(/Setup changed/); expect(mutations).toEqual([]);
  });
  it('refuses to remove integrations or schedules as part of workload refresh', () => {
    current.value.settings.devops.integrations = [{ enabled: true }];
    expect(() => verificationDraft(current.value)).toThrow(/separate access review/);
  });
  it('reviews active access before replacing it and preserves native UI grants', async () => {
    grants = [{ ...clone(grantFixture), delegation_id: 'old-run-grant', execution_scope: { ...preview(false).value.scope }, expires_at: new Date(Date.now() + 60_000).toISOString() },
      { ...clone(grantFixture), delegation_id: 'native', execution_scope: { ...preview(false).value.scope, purpose: 'devops_native_ui' }, expires_at: new Date(Date.now() + 60_000).toISOString() }];
    const journal = await begin(); const review = await advanceRunPreparation(journal, persist);
    expect(review.kind).toBe('replace'); expect(mutations).toEqual([]);
    if (review.kind !== 'replace') throw new Error();
    expect(review.grants.map(item => item.delegation_id)).toEqual(['old-run-grant']);
    expect((await advanceRunPreparation(journal, persist, undefined, review.grants)).kind).toBe('consent');
    expect(grants[0].revoked_at).toBeTruthy(); expect(grants[1].revoked_at).toBeNull();
  });
  it('rejects a changed reviewed scope without granting it', async () => {
    const journal = await begin(); const review = await advanceRunPreparation(journal, persist);
    if (review.kind !== 'consent') throw new Error();
    const changed = clone(review); changed.preview.value.scope.provider_account_id = '999999999999';
    await expect(allow(journal, changed)).rejects.toThrow(/scope changed/); expect(grants).toEqual([]);
    expect(sameConsentScope(journal.selection, selection)).toBe(true);
  });
});
