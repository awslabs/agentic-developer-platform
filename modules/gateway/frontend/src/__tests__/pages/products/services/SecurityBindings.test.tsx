import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SecurityBindings from '@/pages/products/services/SecurityBindings';
import { getSecurityBinding, getSecurityBindingOperation, listSecurityBindings, reconcileSecurityBindingOperation, startSecurityBindingOperation } from '@/services/l5Services';

vi.mock('@/services/l5Services', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/services/l5Services')>(),
  getSecurityBinding: vi.fn(), getSecurityBindingOperation: vi.fn(), listSecurityBindings: vi.fn(),
  reconcileSecurityBindingOperation: vi.fn(), startSecurityBindingOperation: vi.fn(),
}));

const connection = { tenant_id: 'tenant-one', project_id: 'project-one', source_connection_id: 'connection-one', record_type: 'connection', record_id: 'connection-one', record_revision: '7' };
const configuration = { value: { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, revision: 4, settings: { connection_ref: connection, region: 'us-east-1', capabilities: ['threat_model', 'code_review'] } }, etag: '"configuration-four"' } as never;
const binding = { scope: { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '5' }, binding_id: 'binding-one', configuration_id: 'config-one', revision: 2, generation: 2, state: 'ready', ownership: 'adp_created', connection_ref: connection, gaps: [], readiness: [{ kind: 'code_review_source_registration', registration: { service_role: 'arn:aws:iam::111122223333:role/adp-security' }, observed_at: '2026-10-31T09:40:00+00:00' }, { kind: 'threat_model', native_model_ref: { ...connection, record_type: 'threat_model', record_id: 'model-one', record_revision: '2026-10-01T00:00:00+00:00' }, observed_at: '2026-10-31T09:40:01+00:00' }] };
const operation = { operation_id: 'operation-one', action: 'cleanup', state: 'accepted', revision: 1, gaps: [], binding_id: 'binding-one' };

const mount = (current = configuration) => render(<SecurityBindings productId="product-one" projectId="project-one" configuration={current} />);

describe('Security binding lifecycle', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [binding] });
    vi.mocked(getSecurityBinding).mockResolvedValue({ value: binding, etag: '"binding-two"' });
    vi.mocked(getSecurityBindingOperation).mockResolvedValue(operation as never);
    vi.mocked(startSecurityBindingOperation).mockResolvedValue({ value: operation, etag: '"operation-one"' } as never);
  });

  it('requires configuration choices before provider setup', async () => {
    mount(null);
    expect(screen.getByText(/Save a Security configuration before managing bindings/)).toBeInTheDocument();
    expect(listSecurityBindings).not.toHaveBeenCalled();
  });

  it('links only a typed native reference with an explicit revision', async () => {
    mount();
    await userEvent.click(await screen.findByText('Connect another workspace'));
    await userEvent.selectOptions(screen.getByLabelText('Connection method'), 'link');
    expect(screen.getByRole('button', { name: 'Connect existing workspace' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Existing Security resource ID'), 'space-one');
    await userEvent.type(screen.getByLabelText('Resource revision'), 'revision-one');
    await userEvent.click(screen.getByRole('button', { name: 'Connect existing workspace' }));
    await waitFor(() => expect(startSecurityBindingOperation).toHaveBeenCalledWith('product-one', 'project-one', {
      action: 'link', configuration_revision: 4, native_resource_ref: { ...connection, record_type: 'security_space', record_id: 'space-one', record_revision: 'revision-one' },
    }, undefined, expect.any(String)));
  });

  it('uses the binding read ETag for lifecycle operations and confirms owned cleanup separately', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Saved workspace'), 'binding-one');
    await screen.findByText(/Workspace status: ready/);
    if (!screen.getByText('Workspace details and maintenance').closest('details')?.open) await userEvent.click(screen.getByText('Workspace details and maintenance'));
    await userEvent.click(screen.getByRole('button', { name: 'Delete owned resource (confirm)' }));
    expect(startSecurityBindingOperation).not.toHaveBeenCalled();
    vi.mocked(window.confirm).mockReturnValue(true);
    await userEvent.click(screen.getByRole('button', { name: 'Delete owned resource (confirm)' }));
    await waitFor(() => expect(startSecurityBindingOperation).toHaveBeenCalledWith('product-one', 'project-one', {
      action: 'cleanup', configuration_revision: 4, binding_id: 'binding-one', owned_cleanup_confirmed: true,
    }, '"binding-two"', expect.any(String)));
    expect(await screen.findByText(/cleanup operation operation-one/)).toBeInTheDocument();
    vi.mocked(window.confirm).mockRestore();
  });

  it('retries an unconfirmed create using the original receipt, never a new paid request', async () => {
    vi.mocked(startSecurityBindingOperation).mockRejectedValueOnce(new Error('Connection lost'));
    mount();
    await userEvent.click(await screen.findByText('Connect another workspace'));
    await userEvent.selectOptions(screen.getByLabelText('Connection method'), 'create');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    expect(await screen.findByRole('button', { name: 'Retry original receipt' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled();
    const original = vi.mocked(startSecurityBindingOperation).mock.calls[0];
    await userEvent.click(screen.getByRole('button', { name: 'Retry original receipt' }));
    await waitFor(() => expect(startSecurityBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startSecurityBindingOperation).mock.calls[1]).toEqual(original);
  });

  it.each(['selection', 'refresh'])('ignores a delayed %s read before deleting the currently selected binding', async (readKind) => {
    const secondBinding = { ...binding, binding_id: 'binding-two', revision: 3 };
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [binding, secondBinding] });
    let finishPrevious!: (value: Awaited<ReturnType<typeof getSecurityBinding>>) => void;
    const previous = new Promise<Awaited<ReturnType<typeof getSecurityBinding>>>(resolve => { finishPrevious = resolve; });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    mount();
    await waitFor(() => expect(screen.getByLabelText('Saved workspace').querySelector('option[value="binding-one"]')).not.toBeNull());
    if (readKind === 'refresh') {
      await userEvent.selectOptions(screen.getByLabelText('Saved workspace'), 'binding-one');
      await screen.findByText(/Binding binding-one.*revision 2/);
    }
    vi.mocked(getSecurityBinding).mockImplementation(async (_product, _project, bindingId) =>
      bindingId === 'binding-one' ? previous : { value: secondBinding, etag: '"3"' });
    if (readKind === 'refresh') await userEvent.click(screen.getByRole('button', { name: 'Refresh workspace' }));
    else await userEvent.selectOptions(screen.getByLabelText('Saved workspace'), 'binding-one');
    expect(screen.queryByRole('button', { name: 'Delete owned resource (confirm)' })).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Saved workspace'), 'binding-two');
    await screen.findByText(/Binding binding-two.*revision 3/);
    await userEvent.click(screen.getByText('Workspace details and maintenance'));
    await act(async () => { finishPrevious({ value: binding, etag: '"2"' }); });
    expect(screen.getByLabelText('Saved workspace')).toHaveValue('binding-two');
    await userEvent.click(screen.getByRole('button', { name: 'Delete owned resource (confirm)' }));
    expect(startSecurityBindingOperation).toHaveBeenCalledExactlyOnceWith('product-one', 'project-one', {
      action: 'cleanup', configuration_revision: 4, binding_id: 'binding-two', owned_cleanup_confirmed: true,
    }, '"3"', expect.any(String));
    confirm.mockRestore();
  });

  it('reconciles unknown operation rather than starting another provider create', async () => {
    const unknown = { ...operation, state: 'outcome_unknown' };
    vi.mocked(startSecurityBindingOperation).mockResolvedValue({ value: unknown, etag: '"operation-one"' } as never);
    vi.mocked(getSecurityBindingOperation).mockResolvedValue(unknown as never);
    vi.mocked(reconcileSecurityBindingOperation).mockResolvedValue({ value: { ...unknown, state: 'waiting' }, etag: '"operation-two"' } as never);
    mount();
    await userEvent.click(await screen.findByText('Connect another workspace'));
    await userEvent.selectOptions(screen.getByLabelText('Connection method'), 'create');
    await userEvent.click(screen.getByRole('button', { name: 'Create workspace' }));
    await screen.findByRole('button', { name: 'Reconcile original operation' });
    await userEvent.type(screen.getByLabelText('Reason to reconcile'), 'Check the existing native space');
    await userEvent.click(screen.getByRole('button', { name: 'Reconcile original operation' }));
    await waitFor(() => expect(reconcileSecurityBindingOperation).toHaveBeenCalledWith('product-one', 'project-one', 'operation-one', 'Check the existing native space'));
    expect(startSecurityBindingOperation).toHaveBeenCalledTimes(1);
  });
  it('does not offer an uncertain-retry button or replay a binding edit rejected as stale', async () => {
    vi.mocked(startSecurityBindingOperation).mockRejectedValueOnce({ status: 412 });
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Saved workspace'), 'binding-one');
    await screen.findByText(/Workspace status: ready/);
    if (!screen.getByText('Workspace details and maintenance').closest('details')?.open) await userEvent.click(screen.getByText('Workspace details and maintenance'));
    await userEvent.click(screen.getByRole('button', { name: 'Verify workspace' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Binding changed elsewhere (412)');
    expect(screen.queryByRole('button', { name: 'Retry original receipt' })).not.toBeInTheDocument();
    await userEvent.click(await screen.findByText('Connect another workspace'));
    await userEvent.selectOptions(screen.getByLabelText('Connection method'), 'create');
    expect(screen.getByRole('button', { name: 'Create workspace' })).toBeDisabled();
    expect(startSecurityBindingOperation).toHaveBeenCalledTimes(1);
    vi.mocked(getSecurityBinding).mockResolvedValue({ value: { ...binding, revision: 3 }, etag: '"binding-three"' });
    await userEvent.click(screen.getByRole('button', { name: 'Refresh workspace' }));
    await screen.findByText(/Binding binding-one.*revision 3/);
    expect(startSecurityBindingOperation).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: 'Verify workspace' }));
    await waitFor(() => expect(startSecurityBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startSecurityBindingOperation).mock.calls[0][3]).toBe('"binding-two"');
    expect(vi.mocked(startSecurityBindingOperation).mock.calls[1][3]).toBe('"binding-three"');
  });

  it('hides foreign binding choices and signals the workspace to clear access', async () => {
    const onAccessDenied = vi.fn();
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [{ ...binding, scope: { ...binding.scope, tenant_id: 'tenant-other' } }] });
    render(<SecurityBindings productId="product-one" projectId="project-one" configuration={configuration} onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalled());
    expect(screen.queryByRole('option', { name: /Workspace binding-/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Workspace status:/)).not.toBeInTheDocument();
  });

  it('selects an existing model with binding scope and retains its exact request after an uncertain response', async () => {
    vi.mocked(startSecurityBindingOperation).mockRejectedValueOnce({ status: 503 });
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Saved workspace'), 'binding-one');
    await screen.findByText(/Workspace status: ready/);
    if (!screen.getByText('Workspace details and maintenance').closest('details')?.open) await userEvent.click(screen.getByText('Workspace details and maintenance'));
    expect(screen.getByText(/may initialize the first threat model/)).toBeInTheDocument();
    await userEvent.click(screen.getByText('Select an existing threat model'));
    expect(screen.getByRole('button', { name: 'Select and verify existing threat model' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Existing threat model ID'), 'model-existing');
    await userEvent.type(screen.getByLabelText('Threat model revision'), '2026-10-01T00:00:00+00:00');
    await userEvent.click(screen.getByRole('button', { name: 'Select and verify existing threat model' }));
    expect(startSecurityBindingOperation).toHaveBeenCalledExactlyOnceWith('product-one', 'project-one', {
      action: 'check', configuration_revision: 4, binding_id: 'binding-one',
      native_model_ref: { ...connection, record_type: 'threat_model', record_id: 'model-existing', record_revision: '2026-10-01T00:00:00+00:00' },
    }, '"binding-two"', expect.any(String));
    expect(screen.getByLabelText('Existing threat model ID')).toBeDisabled();
    await userEvent.click(await screen.findByRole('button', { name: 'Retry original receipt' }));
    await waitFor(() => expect(startSecurityBindingOperation).toHaveBeenCalledTimes(2));
    expect(vi.mocked(startSecurityBindingOperation).mock.calls[1]).toEqual(vi.mocked(startSecurityBindingOperation).mock.calls[0]);
  });

  it('clears model selection when a different binding is selected', async () => {
    const other = { ...binding, binding_id: 'binding-two' };
    vi.mocked(listSecurityBindings).mockResolvedValue({ items: [binding, other] });
    vi.mocked(getSecurityBinding).mockImplementation(async (_product, _project, id) => ({ value: id === 'binding-one' ? binding : other, etag: '"binding-two"' }));
    mount();
    await userEvent.selectOptions(await screen.findByLabelText('Saved workspace'), 'binding-one');
    await screen.findByText(/Workspace status: ready/);
    if (!screen.getByText('Workspace details and maintenance').closest('details')?.open) await userEvent.click(screen.getByText('Workspace details and maintenance'));
    await userEvent.click(screen.getByText('Select an existing threat model'));
    await userEvent.type(screen.getByLabelText('Existing threat model ID'), 'model-for-first-binding');
    await userEvent.selectOptions(screen.getByLabelText('Saved workspace'), 'binding-two');
    await screen.findByText(/Workspace status: ready/);
    if (!screen.getByText('Workspace details and maintenance').closest('details')?.open) await userEvent.click(screen.getByText('Workspace details and maintenance'));
    expect(screen.getByLabelText('Existing threat model ID')).toHaveValue('');
    expect(startSecurityBindingOperation).not.toHaveBeenCalled();
  });

});
