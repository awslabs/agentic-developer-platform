import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import L5OwnerConsent from '@/pages/products/services/L5OwnerConsent';
import * as consent from '@/services/l5OwnerConsent';
import type { ConsentConfiguration, ConsentPreview, ConsentReceipt, ConsentScope, OwnerGrant } from '@/services/l5OwnerConsent';

vi.mock('@/services/l5OwnerConsent', async original => ({
  ...await original<typeof import('@/services/l5OwnerConsent')>(), listOwnerGrants: vi.fn(), listConsentBindings: vi.fn(),
  previewOwnerConsent: vi.fn(), grantOwnerConsent: vi.fn(), revokeOwnerConsent: vi.fn(),
}));
const now = Date.parse('2030-01-01T00:00:00Z');
const expiry = '2030-01-01T01:00:00Z';
const configuration: ConsentConfiguration = { configuration_id: 'configuration-example', revision: 4, hash: 'a'.repeat(64),
  scope: { tenant_id: 'tenant-example', product_id: 'product-example', project_id: 'project-example', owner_id: 'owner-example' },
  settings: { enabled: true, capabilities: ['code_review'] } };
const reference = { tenant_id: 'tenant-example', project_id: 'project-example', source_connection_id: 'connection-example',
  record_type: 'credential', record_id: 'connection-example', record_revision: 'connection-revision' };
const binding = { binding_id: 'binding-example', configuration_id: configuration.configuration_id, generation: 3,
  state: 'ready', scope: configuration.scope };
const scope: ConsentScope = { ...configuration.scope, service_id: 'l5-worker', configuration_id: configuration.configuration_id,
  configuration_revision: 4, configuration_hash: configuration.hash, created_by: 'owner-example', credential_id: 'connection-example',
  provider_account_id: '111122223333', provider_role_arn: 'arn:aws:iam::111122223333:role/example', region: 'us-east-1',
  binding_id: binding.binding_id, binding_generation: binding.generation, operations: ['credential:session', 'source:read'],
  capabilities: ['code_review'], sources: [reference, { ...reference, record_type: 'repository_commit', record_id: 'example/repo', record_revision: 'commit-example' }], visibility: 'only_me' };
const preview: { value: ConsentPreview; etag: string } = {
  value: { scope, scope_digest: 'b'.repeat(64), maximum_lifetime_seconds: 86400 }, etag: `"${configuration.hash}"`,
};
function recorded(receipt?: ConsentReceipt): OwnerGrant {
  const selected = receipt?.scope ?? scope;
  return { ...selected, delegation_id: 'grant-example', revision: 1, execution_scope: selected,
    expires_at: receipt?.body.expires_at ?? expiry, revoked_at: null, active: true };
}
function panel(config = configuration, service: consent.ConsentService = 'security') {
  return <L5OwnerConsent service={service} productId="product-example" projectId="project-example" configuration={config} />;
}
async function read() {
  fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' }));
  await screen.findByRole('combobox', { name: 'Consent purpose' });
}
async function review(mode = 'execution') {
  fireEvent.change(screen.getByRole('combobox', { name: 'Consent purpose' }), { target: { value: mode } });
  if (mode === 'execution' || mode === 'control') fireEvent.change(screen.getByRole('combobox', { name: 'Consent binding' }), { target: { value: binding.binding_id } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review consent scope' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
  await screen.findByLabelText('Reviewed owner consent');
}
function accept() {
  fireEvent.change(screen.getByRole('textbox', { name: 'Consent expiry (UTC)' }), { target: { value: expiry } });
  fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); vi.spyOn(Date, 'now').mockReturnValue(now);
  vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [] });
  vi.mocked(consent.listConsentBindings).mockResolvedValue({ items: [binding] });
  vi.mocked(consent.previewOwnerConsent).mockResolvedValue(preview);
  vi.mocked(consent.grantOwnerConsent).mockImplementation(async (_product, _project, _service, receipt) => ({ value: recorded(receipt), etag: '"1"' }));
});
afterEach(() => vi.restoreAllMocks());

describe('explicit owner consent', () => {
  it('does no work on mount and grants only after selection, scope review and explicit expiry', async () => {
    render(panel()); expect(consent.listOwnerGrants).not.toHaveBeenCalled(); expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
    await read(); expect(screen.getByRole('combobox', { name: 'Consent purpose' })).toHaveValue('');
    await review();
    expect(screen.getByRole('list', { name: 'Consent source revisions' })).toHaveTextContent('commit-example');
    expect(screen.getByRole('textbox', { name: 'Consent expiry (UTC)' })).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeDisabled();
    accept(); fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' }));
    await screen.findByText(/Owner consent recorded/);
    const receipt = vi.mocked(consent.grantOwnerConsent).mock.calls[0][3];
    expect(receipt).toMatchObject({ state: 'pending', etag: preview.etag, scope,
      body: { binding_id: binding.binding_id, for_control: false, scope_digest: preview.value.scope_digest, expires_at: '2030-01-01T01:00:00.000Z' } });
    expect(receipt.key).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
  });
  it('clears owner acceptance when expiry changes', async () => {
    render(panel()); await read(); await review(); accept();
    fireEvent.change(screen.getByRole('textbox', { name: 'Consent expiry (UTC)' }), { target: { value: '2030-01-01T02:00:00Z' } });
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeDisabled();
  });
  it('prevents a consent text input from submitting the surrounding setup form', async () => {
    const save = vi.fn(); render(<form onSubmit={save}>{panel()}</form>); await read(); await review();
    const input = screen.getByRole('textbox', { name: 'Consent expiry (UTC)' });
    expect(fireEvent.keyDown(input, { key: 'Enter' })).toBe(false); expect(save).not.toHaveBeenCalled();
  });
  it('requests the next binding generation explicitly for control consent', async () => {
    vi.mocked(consent.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value, scope: { ...scope, binding_generation: 4 } } });
    render(panel()); await read(); await review('control');
    expect(consent.previewOwnerConsent).toHaveBeenCalledWith('product-example', 'project-example', 'security', { binding_id: binding.binding_id, for_control: true });
    expect(screen.getByText(/generation: 4/)).toBeInTheDocument();
  });
  it('supports setup-only DevOps consent without enabling configuration or granting capabilities', async () => {
    const draft = { ...configuration, settings: { enabled: false, capabilities: ['investigation'], triggers: [], devops: { space_mode: 'create' } } };
    const setupScope = { ...scope, service_id: 'l5-devops-worker', binding_id: null, binding_generation: 1,
      purpose: 'devops_setup', control_action: 'devops_setup', capabilities: [], operations: ['binding:create', 'credential:session'], sources: [reference] };
    vi.mocked(consent.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value, scope: setupScope } });
    render(panel(draft, 'devops')); await read(); await review('setup'); accept();
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' })); await screen.findByText(/Owner consent recorded/);
    expect(consent.previewOwnerConsent).toHaveBeenCalledWith('product-example', 'project-example', 'devops', { for_control: true, control_action: 'devops_setup' });
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[0][3].body).toMatchObject({ for_control: true, control_action: 'devops_setup' });
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[0][3].body).not.toHaveProperty('binding_id');
  });
  it('does not offer new-space setup for a DevOps draft with native automation enabled', async () => {
    render(panel({ ...configuration, settings: { enabled: false, capabilities: ['investigation'], devops: { space_mode: 'create', prevention_enabled: true } } }, 'devops'));
    await read(); expect(screen.getByRole('option', { name: /Create one DevOps space/ })).toBeDisabled();
  });
  it('keeps separate DevOps native UI consent from blocking an execution grant', async () => {
    const execution = { ...scope, service_id: 'l5-devops-worker', capabilities: ['investigation'] };
    const native = { ...execution, purpose: 'devops_native_ui', control_action: 'devops_native_ui',
      capabilities: [], operations: ['credential:session', 'browser:launch'] };
    vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [{ ...recorded(), service_id: 'l5-devops-worker', execution_scope: native }] });
    vi.mocked(consent.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value, scope: execution } });
    render(panel({ ...configuration, settings: { enabled: true, capabilities: ['investigation'] } }, 'devops'));
    await read(); await review(); accept();
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' }));
    await screen.findByText(/Owner consent recorded/);
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[0][3].scope).toEqual(execution);
  });
  it('requires enabled saved configuration for ordinary execution consent', async () => {
    render(panel({ ...configuration, settings: { enabled: false } })); await read(); await review(); accept();
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeDisabled();
  });
  it.each([undefined, '', ' '])('refuses new consent when configuration owner is absent or blank (%s)', async ownerId => {
    render(panel({ ...configuration, scope: { ...configuration.scope, owner_id: ownerId } })); await read();
    fireEvent.change(screen.getByRole('combobox', { name: 'Consent purpose' }), { target: { value: 'execution' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Consent binding' }), { target: { value: binding.binding_id } });
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    await screen.findByRole('alert');
    expect(screen.queryByLabelText('Reviewed owner consent')).not.toBeInTheDocument();
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
    expect(screen.getByText(/missing its owner identity/)).toBeInTheDocument();
  });
  it.each(['finops', 'well_architected', 'transform'] as const)('uses the same reviewed grant path for %s', async service => {
    vi.mocked(consent.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value,
      scope: { ...scope, service_id: consent.consentWorkers[service] } } });
    render(panel(configuration, service)); await read(); await review(); accept();
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' })); await screen.findByText(/Owner consent recorded/);
    expect(consent.grantOwnerConsent).toHaveBeenCalledWith('product-example', 'project-example', service, expect.any(Object));
  });
  it.each(['configuration_revision', 'binding_generation', 'created_by'])('rejects a stale or foreign %s in the scope', async field => {
    vi.mocked(consent.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value, scope: { ...scope, [field]: 'foreign' } } });
    render(panel()); await read();
    fireEvent.change(screen.getByRole('combobox', { name: 'Consent purpose' }), { target: { value: 'execution' } });
    fireEvent.change(screen.getByRole('combobox', { name: 'Consent binding' }), { target: { value: binding.binding_id } });
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    await screen.findByRole('alert'); expect(screen.queryByLabelText('Reviewed owner consent')).not.toBeInTheDocument();
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
  });
  it('ignores a late scope response after the saved configuration changes', async () => {
    let resolve!: (value: typeof preview) => void;
    vi.mocked(consent.previewOwnerConsent).mockReturnValue(new Promise(done => { resolve = done; }));
    const view = render(panel()); await read();
    fireEvent.change(screen.getByRole('combobox', { name: 'Consent purpose' }), { target: { value: 'initial' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    view.rerender(panel({ ...configuration, revision: 5, hash: 'c'.repeat(64) }));
    await act(async () => resolve(preview));
    expect(screen.queryByLabelText('Reviewed owner consent')).not.toBeInTheDocument(); expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
  });
});

describe('uncertainty and revocation', () => {
  it('does not send a grant when its original request cannot be retained', async () => {
    render(panel()); await read(); await review(); accept();
    vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => { throw new Error('storage unavailable'); });
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' })); await screen.findByRole('alert');
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent('consent was not sent');
  });
  it('reconciles an uncertain grant from readback without sending it again', async () => {
    vi.mocked(consent.grantOwnerConsent).mockRejectedValueOnce(new Error('network'));
    render(panel()); await read(); await review(); accept();
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' })); await screen.findByRole('alert');
    const receipt = vi.mocked(consent.grantOwnerConsent).mock.calls[0][3];
    vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [recorded(receipt)] });
    fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' })); await screen.findByText(/Matching consent is recorded/);
    expect(consent.grantOwnerConsent).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Original consent request: confirmed')).toBeInTheDocument();
  });
  it('retains the original request across remount and requires review before exact replay', async () => {
    vi.mocked(consent.grantOwnerConsent).mockRejectedValueOnce(new Error('network'));
    const view = render(panel()); await read(); await review(); accept();
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' })); await screen.findByRole('alert');
    const original = vi.mocked(consent.grantOwnerConsent).mock.calls[0][3]; view.unmount(); render(panel());
    fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' })); await screen.findByText('Original consent request: pending');
    expect(screen.queryByRole('button', { name: 'Replay original consent request' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' })); await screen.findByLabelText('Reviewed owner consent');
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Replay original consent request' })); await screen.findByText(/Owner consent recorded/);
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[1][3]).toEqual(original);
  });
  it('never replaces an active grant automatically', async () => {
    vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [recorded()] });
    render(panel()); await read(); await review(); accept();
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeDisabled();
    expect(consent.revokeOwnerConsent).not.toHaveBeenCalled();
  });
  it('does not infer revocation success from a failed request', async () => {
    vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [recorded()] });
    vi.mocked(consent.revokeOwnerConsent).mockRejectedValue(new Error('network'));
    render(panel()); await read(); fireEvent.click(screen.getByText(/grant-example: active/));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Revoke grant grant-example' }));
    fireEvent.click(screen.getByRole('button', { name: 'Revoke selected consent' })); await screen.findByRole('alert');
    expect(screen.queryByText(/Consent revoked/)).not.toBeInTheDocument();
    await read(); expect(screen.getByText(/grant-example: active/)).toBeInTheDocument();
    expect(consent.revokeOwnerConsent).toHaveBeenCalledTimes(1);
  });
  it('requires explicit revocation and uses the current grant revision even when configuration is disabled', async () => {
    const grant = recorded(); vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [grant] });
    vi.mocked(consent.revokeOwnerConsent).mockResolvedValue({ value: { ...grant, revision: 2, active: false, revoked_at: '2030-01-01T00:00:01Z' }, etag: '"2"' });
    render(panel({ ...configuration, settings: { enabled: false } })); await read();
    fireEvent.click(screen.getByText(/grant-example: active/));
    expect(screen.getByRole('button', { name: 'Revoke selected consent' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Revoke grant grant-example' }));
    fireEvent.click(screen.getByRole('button', { name: 'Revoke selected consent' })); await screen.findByText(/Consent revoked/);
    expect(consent.revokeOwnerConsent).toHaveBeenCalledWith('product-example', 'project-example', 'security', grant);
  });
  it('keeps authenticated readback and explicit revocation available without the configuration owner field', async () => {
    const grant = recorded(); vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [grant] });
    vi.mocked(consent.revokeOwnerConsent).mockResolvedValue({ value: { ...grant, revision: 2, active: false, revoked_at: '2030-01-01T00:00:01Z' }, etag: '"2"' });
    render(panel({ ...configuration, scope: { ...configuration.scope, owner_id: undefined } })); await read();
    fireEvent.click(screen.getByText(/grant-example: active/));
    fireEvent.click(screen.getByRole('checkbox', { name: 'Revoke grant grant-example' }));
    fireEvent.click(screen.getByRole('button', { name: 'Revoke selected consent' })); await screen.findByText(/Consent revoked/);
    expect(consent.revokeOwnerConsent).toHaveBeenCalledWith('product-example', 'project-example', 'security', grant);
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
  });
  it('keeps revocation usable when binding readback fails', async () => {
    vi.mocked(consent.listOwnerGrants).mockResolvedValue({ items: [recorded()] });
    vi.mocked(consent.listConsentBindings).mockRejectedValue({ status: 503 });
    render(panel()); await read(); await screen.findByText(/Bindings could not be read/);
    fireEvent.click(screen.getByText(/grant-example: active/));
    expect(screen.getByRole('checkbox', { name: 'Revoke grant grant-example' })).toBeEnabled();
  });
  it('removes previously shown details after owner access is denied', async () => {
    render(panel()); await read(); await review();
    vi.mocked(consent.listOwnerGrants).mockRejectedValue({ status: 404 });
    fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' })); await screen.findByRole('alert');
    expect(screen.queryByText(/commit-example/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Recorded owner grants')).not.toBeInTheDocument();
  });
});

describe('guided Security access', () => {
  const guided = () => render(<L5OwnerConsent service="security" productId="product-example" projectId="project-example" configuration={configuration} guided />);
  it('selects a unique saved workspace and offers a duration while requiring explicit owner approval', async () => {
    guided();
    expect(consent.listOwnerGrants).not.toHaveBeenCalled();
    await read();
    await waitFor(() => expect(screen.getByLabelText('Consent binding')).toHaveValue(binding.binding_id));
    expect(screen.getByLabelText('Consent purpose')).toHaveValue('execution');
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    await screen.findByLabelText('Access duration');
    expect(screen.getByLabelText('Access duration')).toHaveValue('8');
    expect(screen.getByRole('button', { name: 'Grant reviewed consent' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.change(screen.getByLabelText('Access duration'), { target: { value: '1' } });
    expect(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' }));
    await screen.findByText(/Owner consent recorded/);
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[0][3].body).toMatchObject({ expires_at: '2030-01-01T01:00:00.000Z', binding_id: binding.binding_id, for_control: false });
  });
  it('never chooses among multiple workspaces', async () => {
    vi.mocked(consent.listConsentBindings).mockResolvedValue({ items: [binding, { ...binding, binding_id: 'another-workspace' }] });
    guided(); await read();
    await waitFor(() => expect(screen.getByLabelText('Consent purpose')).toHaveValue('execution'));
    expect(screen.getByLabelText('Consent binding')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Review consent scope' })).toBeDisabled();
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
  });
  it('starts with setup access when no workspace exists', async () => {
    vi.mocked(consent.listConsentBindings).mockResolvedValue({ items: [] });
    guided(); await read();
    await waitFor(() => expect(screen.getByLabelText('Consent purpose')).toHaveValue('initial'));
    expect(consent.grantOwnerConsent).not.toHaveBeenCalled();
  });
  it('keeps the original expiry and receipt after an uncertain grant', async () => {
    vi.mocked(consent.grantOwnerConsent).mockRejectedValueOnce(new Error('lost response'));
    guided(); await read();
    await waitFor(() => expect(screen.getByLabelText('Consent binding')).toHaveValue(binding.binding_id));
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    await screen.findByLabelText('Access duration');
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' }));
    await screen.findByRole('alert');
    const original = vi.mocked(consent.grantOwnerConsent).mock.calls[0][3];
    fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review consent scope' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
    await screen.findByText(`Original expiry: ${original.body.expires_at}`);
    expect(screen.queryByLabelText('Access duration')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Replay original consent request' }));
    await screen.findByText(/Owner consent recorded/);
    expect(vi.mocked(consent.grantOwnerConsent).mock.calls[1][3]).toEqual(original);
  });
});
