import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import DevOpsOperations from '@/pages/products/services/DevOpsOperations';
import L5OwnerConsent from '@/pages/products/services/L5OwnerConsent';
import * as consentApi from '@/services/l5OwnerConsent';
import * as operationsApi from '@/services/l5Services';
import type { ConsentPreview, ConsentScope } from '@/services/l5OwnerConsent';
import type { DevOpsConfiguration, Versioned } from '@/services/l5Services';
import { devOpsSetupStorageKey } from '@/services/devOpsSetupReceipt';
import { configuration as setupConfiguration } from '../../../fixtures/devOpsActivation';

vi.mock('@/services/l5OwnerConsent', async original => ({
  ...await original<typeof import('@/services/l5OwnerConsent')>(), listOwnerGrants: vi.fn(), listConsentBindings: vi.fn(),
  previewOwnerConsent: vi.fn(), grantOwnerConsent: vi.fn(),
}));
vi.mock('@/services/l5Services', async original => ({
  ...await original<typeof import('@/services/l5Services')>(), listDevOpsBindings: vi.fn(), listDevOpsRuns: vi.fn(),
  listDevOpsResults: vi.fn(), startDevOpsBindingOperation: vi.fn(), getDevOpsBindingOperation: vi.fn(),
}));

const saved: DevOpsConfiguration = { ...setupConfiguration, hash: 'a'.repeat(64), settings: {
  ...setupConfiguration.settings, devops: { ...setupConfiguration.settings.devops, space_mode: 'adopt', existing_space_id: 'saved-space' },
} };
const native = { ...saved.settings.devops.hosting_connection_ref, record_type: 'agent_space', record_id: 'saved-space', record_revision: 'saved-space' };
const scope: ConsentScope = { ...saved.scope, service_id: 'l5-devops-worker', configuration_id: saved.configuration_id,
  configuration_revision: saved.revision, configuration_hash: saved.hash, created_by: 'owner', credential_id: 'connection',
  provider_account_id: '123456789012', provider_role_arn: 'arn:aws:iam::123456789012:role/setup', region: 'us-east-1',
  binding_id: null, binding_generation: 1, operations: ['binding:link', 'credential:session'], capabilities: [],
  sources: [{ ...saved.settings.devops.hosting_connection_ref, record_type: 'credential' }], visibility: 'only_me',
  control_action: 'devops_setup', purpose: 'devops_setup', setup_action: 'link', native_resource_ref: native };
const preview: Versioned<ConsentPreview> = { value: { scope, scope_digest: 'b'.repeat(64), maximum_lifetime_seconds: 86400 }, etag: `"${saved.hash}"` };
const denied = vi.fn();
function journey(configuration = saved) {
  return <>
    <L5OwnerConsent service="devops" productId="product" projectId="project" configuration={configuration} />
    <DevOpsOperations productId="product" projectId="project" configuration={{ value: configuration, etag: `"${configuration.hash}"` }} onAccessDenied={denied} />
  </>;
}
async function selectConsent() {
  fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' }));
  await screen.findByRole('combobox', { name: 'Consent purpose' });
  fireEvent.change(screen.getByRole('combobox', { name: 'Consent purpose' }), { target: { value: 'setup' } });
  fireEvent.click(screen.getByRole('button', { name: 'Review consent scope' }));
}
async function dispatch() {
  const button = screen.getByRole('button', { name: 'Link existing DevOps space' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear(); vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2030-01-01T00:00:00Z'));
  vi.mocked(consentApi.listOwnerGrants).mockResolvedValue({ items: [] });
  vi.mocked(consentApi.listConsentBindings).mockResolvedValue({ items: [] });
  vi.mocked(consentApi.previewOwnerConsent).mockResolvedValue(preview);
  vi.mocked(consentApi.grantOwnerConsent).mockImplementation(async (_product, _project, _service, receipt) => ({ etag: '"1"', value: {
    ...receipt.scope, delegation_id: 'grant', revision: 1, execution_scope: receipt.scope,
    expires_at: receipt.body.expires_at, revoked_at: null, active: true,
  } }));
  vi.mocked(operationsApi.listDevOpsBindings).mockResolvedValue({ items: [] });
  vi.mocked(operationsApi.listDevOpsRuns).mockResolvedValue({ items: [] });
  vi.mocked(operationsApi.listDevOpsResults).mockResolvedValue({ items: [] });
  vi.mocked(operationsApi.startDevOpsBindingOperation).mockResolvedValue({ etag: '"1"', value: {
    operation_id: 'original-link', action: 'link', state: 'accepted', revision: 1, gaps: [],
  } });
});
afterEach(() => vi.restoreAllMocks());

describe('DevOps existing-space setup journey', () => {
  it('grants exact saved-space link consent and separately dispatches the same target without creation authority', async () => {
    render(journey()); await selectConsent();
    const reviewed = await screen.findByLabelText('Reviewed owner consent');
    expect(within(reviewed).getByText(/Link existing DevOps space: saved-space/)).toHaveTextContent('hosting connection: connection');
    expect(within(reviewed).getByText(/Permitted operations/)).toHaveTextContent('binding:link, credential:session');
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Consent expiry (UTC)'), { target: { value: '2030-01-01T01:00:00Z' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact scope/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Grant reviewed consent' }));
    await screen.findByText(/Owner consent recorded/);
    const receipt = vi.mocked(consentApi.grantOwnerConsent).mock.calls[0][3];
    expect(receipt.scope).toEqual(scope);
    expect(receipt.body).toMatchObject({ control_action: 'devops_setup', for_control: true });
    expect(receipt.scope.capabilities).toEqual([]);
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
    await dispatch();
    await screen.findByText(/Binding operation original-link: accepted. Refresh/);
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledExactlyOnceWith('product', 'project', {
      action: 'link', configuration_revision: saved.revision, native_resource_ref: native,
    }, undefined, expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/));
    expect(saved.settings.enabled).toBe(false);
  });

  it.each([
    ['space', { native_resource_ref: { ...native, record_id: 'another-space' } }],
    ['space revision', { native_resource_ref: { ...native, record_revision: 'another-space' } }],
    ['connection', { native_resource_ref: { ...native, source_connection_id: 'another-connection' } }],
    ['credential revision', { sources: [{ ...scope.sources[0], record_revision: 'another-revision' }] }],
    ['action', { setup_action: 'create' }],
    ['operations', { operations: ['binding:create', 'credential:session'] }],
    ['execution capability', { capabilities: ['investigation'] }],
    ['configuration revision', { configuration_revision: saved.revision + 1 }],
    ['owner', { created_by: 'another-owner' }],
    ['project', { project_id: 'another-project' }],
  ])('rejects a consent preview with changed %s', async (_label, changed) => {
    vi.mocked(consentApi.previewOwnerConsent).mockResolvedValue({ ...preview, value: { ...preview.value, scope: { ...scope, ...changed } } });
    render(journey()); await selectConsent();
    expect(await screen.findByRole('alert')).toHaveTextContent('saved scope or binding changed');
    expect(screen.queryByLabelText('Reviewed owner consent')).not.toBeInTheDocument();
    expect(consentApi.grantOwnerConsent).not.toHaveBeenCalled();
  });

  it.each(['', 'space/with/path', ' space ', 'a'.repeat(129)])('blocks invalid saved adoption ID %s', async id => {
    render(journey({ ...saved, settings: { ...saved.settings, devops: { ...saved.settings.devops, existing_space_id: id } } }));
    await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Read owner consent' }));
    expect(await screen.findByRole('option', { name: /Link the saved existing DevOps space/ })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
  });

  it('blocks a saved hosting connection from another project', async () => {
    render(journey({ ...saved, settings: { ...saved.settings, devops: { ...saved.settings.devops,
      hosting_connection_ref: { ...saved.settings.devops.hosting_connection_ref, project_id: 'another-project' },
    } } }));
    await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
  });

  it('replays an uncertain link with its original space, request and receipt', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'));
    render(journey()); await dispatch();
    const retry = await screen.findByRole('button', { name: 'Retry original receipt' });
    const original = vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[0];
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('Original existing space: saved-space');
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    fireEvent.click(retry);
    await waitFor(() => expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[1]).toEqual(original);
  });

  it('retains the original link when its response describes a different setup action', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockResolvedValueOnce({ etag: '"1"', value: {
      operation_id: 'wrong-create', action: 'create', state: 'accepted', revision: 1, gaps: [],
    } });
    render(journey()); await dispatch();
    await screen.findByRole('button', { name: 'Retry original receipt' });
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('Action: link');
    expect(screen.queryByText(/Binding operation wrong-create/)).not.toBeInTheDocument();
  });

  it('retains an uncertain link and blocks replay after the saved target changes', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'));
    const view = render(journey()); await dispatch();
    await screen.findByRole('button', { name: 'Retry original receipt' });
    const original = vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[0];
    view.rerender(journey({ ...saved, revision: saved.revision + 1, hash: 'c'.repeat(64),
      settings: { ...saved.settings, devops: { ...saved.settings.devops, existing_space_id: 'new-space' } } }));
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeDisabled();
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent(original[4]!);
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('Original existing space: saved-space');
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('Replay is blocked');
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
  });

  it('does not expose a late consent preview after the saved target changes', async () => {
    let resolve!: (value: Versioned<ConsentPreview>) => void;
    vi.mocked(consentApi.previewOwnerConsent).mockImplementation(() => new Promise(done => { resolve = done; }));
    const view = render(journey()); await selectConsent();
    view.rerender(journey({ ...saved, revision: saved.revision + 1, hash: 'c'.repeat(64),
      settings: { ...saved.settings, devops: { ...saved.settings.devops, existing_space_id: 'new-space' } } }));
    await act(async () => resolve(preview));
    expect(screen.queryByLabelText('Reviewed owner consent')).not.toBeInTheDocument();
    expect(consentApi.grantOwnerConsent).not.toHaveBeenCalled();
  });

  it('retains an in-flight link receipt when a new saved configuration arrives', async () => {
    let reject!: (reason: unknown) => void;
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockImplementation(() => new Promise((_done, failed) => { reject = failed; }));
    const view = render(journey()); await dispatch();
    const original = vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[0];
    view.rerender(journey({ ...saved, revision: saved.revision + 1, hash: 'c'.repeat(64) }));
    await act(async () => reject(new Error('Response lost')));
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent(original[4]!);
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('changed while the request was in flight');
  });

  it('does not expose an old owner’s in-flight receipt after ownership changes', async () => {
    let reject!: (reason: unknown) => void;
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockImplementation(() => new Promise((_done, failed) => { reject = failed; }));
    const view = render(journey()); await dispatch();
    view.rerender(journey({ ...saved, scope: { ...saved.scope, owner_id: 'new-owner' } }));
    await act(async () => reject(new Error('Response lost')));
    expect(screen.queryByLabelText('Original binding request')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
  });

  it.each(['create', 'adopt'] as const)('retains %s before dispatch and reload continues only the original request after readback', async mode => {
    const config = { ...saved, settings: { ...saved.settings, devops: { ...saved.settings.devops, space_mode: mode,
      existing_space_id: mode === 'adopt' ? 'saved-space' : undefined } } };
    const storageKey = devOpsSetupStorageKey(config, 'product', 'project')!;
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockImplementationOnce(async (_product, _project, request, _etag, receipt) => {
      expect(JSON.parse(sessionStorage.getItem(storageKey)!)).toMatchObject({ key: receipt, request, state: 'pending' });
      throw new Error('Response lost');
    });
    const view = render(journey(config));
    const start = screen.getByRole('button', { name: mode === 'adopt' ? 'Link existing DevOps space' : 'Create provider binding' });
    await waitFor(() => expect(start).toBeEnabled()); fireEvent.click(start);
    await screen.findByRole('button', { name: 'Retry original receipt' });
    const original = vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[0];
    view.unmount();
    let release!: (page: { items: [] }) => void;
    vi.mocked(operationsApi.listDevOpsBindings).mockImplementationOnce(() => new Promise(done => { release = done; }));
    render(journey(config));
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
    await act(async () => release({ items: [] }));
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeEnabled();
    expect(screen.getByRole('button', { name: mode === 'adopt' ? 'Link existing DevOps space' : 'Create provider binding' })).toBeDisabled();
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockResolvedValueOnce({ etag: '"1"', value: {
      operation_id: 'original-setup', action: mode === 'adopt' ? 'link' : 'create', state: 'accepted', revision: 1, gaps: [],
    } });
    fireEvent.click(screen.getByRole('button', { name: 'Retry original receipt' }));
    await waitFor(() => expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[1]).toEqual(original);
  });

  it('reloads a confirmed original operation and blocks a duplicate setup after refresh', async () => {
    const view = render(journey()); await dispatch();
    await screen.findByText(/Binding operation original-link: accepted. Refresh/);
    view.unmount(); render(journey());
    await screen.findByText(/Binding operation original-link: accepted./);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh status' })).toBeEnabled());
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
    vi.mocked(operationsApi.getDevOpsBindingOperation).mockResolvedValueOnce({ operation_id: 'original-link', action: 'link', state: 'succeeded', revision: 2, gaps: [] });
    fireEvent.click(screen.getByRole('button', { name: 'Check operation' }));
    await screen.findByText(/Binding operation original-link: succeeded./);
    expect(JSON.parse(sessionStorage.getItem(devOpsSetupStorageKey(saved, 'product', 'project')!)!).operation.state).toBe('succeeded');
  });

  it('reloads a stale pending target without allowing replay or replacement', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'));
    const view = render(journey()); await dispatch(); await screen.findByRole('button', { name: 'Retry original receipt' });
    view.unmount(); render(journey({ ...saved, revision: saved.revision + 1, hash: 'c'.repeat(64),
      settings: { ...saved.settings, devops: { ...saved.settings.devops, existing_space_id: 'changed-space' } } }));
    await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('saved-space');
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('Replay is blocked');
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
  });

  it('keeps an uncertain receipt pending after an authorization rejection during replay', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'))
      .mockRejectedValueOnce({ status: 403, detail: { code: 'delegation_denied' } });
    render(journey()); await dispatch();
    fireEvent.click(await screen.findByRole('button', { name: 'Retry original receipt' }));
    await waitFor(() => expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent('receipt: pending');
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(JSON.parse(sessionStorage.getItem(devOpsSetupStorageKey(saved, 'product', 'project')!)!).state).toBe('pending');
  });

  it('blocks new setup when its stored receipt is malformed instead of discarding it', async () => {
    const storageKey = devOpsSetupStorageKey(saved, 'product', 'project')!;
    sessionStorage.setItem(storageKey, '{');
    render(journey()); await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.getByRole('alert')).toHaveTextContent('malformed');
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(storageKey)).toBe('{');
  });

  it('does not dispatch when durable retention fails and requires an explicit reread to recover', async () => {
    const write = vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => { throw new Error('Storage unavailable'); });
    render(journey()); await dispatch();
    expect(await screen.findByRole('alert')).toHaveTextContent('no setup was sent');
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    write.mockRestore();
    fireEvent.click(screen.getByRole('button', { name: 'Read retained setup receipt' }));
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeEnabled();
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
  });

  it('blocks setup when the original receipt cannot be read from storage', async () => {
    vi.spyOn(sessionStorage, 'getItem').mockImplementation(() => { throw new Error('Storage unavailable'); });
    render(journey()); await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('could not be read');
    expect(operationsApi.startDevOpsBindingOperation).not.toHaveBeenCalled();
  });

  it('keeps a pending request blocked if storage loses its receipt during the same session', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'));
    render(journey()); await dispatch(); await screen.findByRole('button', { name: 'Retry original receipt' });
    const original = vi.mocked(operationsApi.startDevOpsBindingOperation).mock.calls[0];
    sessionStorage.removeItem(devOpsSetupStorageKey(saved, 'product', 'project')!);
    fireEvent.click(screen.getByRole('button', { name: 'Read retained setup receipt' }));
    expect(screen.getByLabelText('Original binding request')).toHaveTextContent(original[4]!);
    expect(screen.getByRole('button', { name: 'Link existing DevOps space' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Retry original receipt' })).toBeDisabled();
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
  });

  it('keeps stored setup receipts isolated from a different owner after reload', async () => {
    vi.mocked(operationsApi.startDevOpsBindingOperation).mockRejectedValueOnce(new Error('Response lost'));
    const view = render(journey()); await dispatch(); await screen.findByRole('button', { name: 'Retry original receipt' });
    const original = sessionStorage.getItem(devOpsSetupStorageKey(saved, 'product', 'project')!);
    view.unmount(); render(journey({ ...saved, scope: { ...saved.scope, owner_id: 'new-owner' } }));
    await screen.findByText(/No DevOps workspace is linked/);
    expect(screen.queryByLabelText('Original binding request')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
    expect(sessionStorage.getItem(devOpsSetupStorageKey(saved, 'product', 'project')!)).toBe(original);
    expect(operationsApi.startDevOpsBindingOperation).toHaveBeenCalledTimes(1);
  });
});
