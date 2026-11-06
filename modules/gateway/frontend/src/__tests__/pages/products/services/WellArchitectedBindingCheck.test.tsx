import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import WellArchitectedBindingCheck from '@/pages/products/services/WellArchitectedBindingCheck';
import { getWellArchitectedBindingOperation, type SecurityBinding, type SecurityBindingOperation,
  type Versioned, type WellArchitectedConfiguration } from '@/services/l5Services';
import { checkWellArchitectedBinding, readWellArchitectedBinding } from '@/services/wellArchitectedBindings';

vi.mock('@/services/wellArchitectedBindings', () => ({ checkWellArchitectedBinding: vi.fn(), readWellArchitectedBinding: vi.fn() }));
vi.mock('@/services/l5Services', async original => ({ ...await original<typeof import('@/services/l5Services')>(), getWellArchitectedBindingOperation: vi.fn() }));

const scope = { tenant_id: 'tenant-one', product_id: 'product-one', project_id: 'project-one', relationship_revision: '1', owner_id: 'owner-one' };
const connection = { tenant_id: scope.tenant_id, project_id: scope.project_id, source_connection_id: 'aws-one', record_type: 'connection', record_id: 'aws-one', record_revision: '1' };
const configuration = { value: { scope, configuration_id: 'config-one', revision: 3, hash: 'hash-three',
  settings: { connection_ref: connection, enabled: true, source_refs: [], capabilities: ['architecture_review'] } }, etag: '"3"' } as Versioned<WellArchitectedConfiguration>;
const binding: SecurityBinding = { scope, binding_id: 'binding-one', configuration_id: 'config-one', revision: 7, generation: 2,
  state: 'blocked', ownership: 'adp_created', connection_ref: connection, readiness: [], gaps: ['provider_unqualified'] };
const operation: SecurityBindingOperation = { operation_id: 'check-one', action: 'check', binding_id: binding.binding_id, revision: 1, state: 'accepted', gaps: [] };
const props = { productId: scope.product_id, projectId: scope.project_id, configuration, bindings: [binding], disabled: false,
  onInvalidate: vi.fn(), onBusyChange: vi.fn(), onRefresh: vi.fn<() => Promise<void>>(), onAccessDenied: vi.fn() };

async function read() {
  await userEvent.selectOptions(screen.getByLabelText('Existing review binding'), binding.binding_id);
  await userEvent.click(screen.getByRole('button', { name: 'Read current binding' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Read current binding' })).toBeEnabled());
}
async function submit() {
  await userEvent.click(screen.getByRole('button', { name: 'Check binding' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Read current binding' })).toBeEnabled());
}

describe('Well-Architected existing binding check', () => {
  beforeEach(() => {
    vi.resetAllMocks(); sessionStorage.clear();
    vi.mocked(readWellArchitectedBinding).mockResolvedValue({ value: binding, etag: '"7"' });
    vi.mocked(checkWellArchitectedBinding).mockResolvedValue({ value: operation, etag: '"1"' });
    vi.mocked(getWellArchitectedBindingOperation).mockResolvedValue({ ...operation, state: 'blocked', gaps: ['provider_unqualified'] });
    props.onRefresh.mockResolvedValue();
  });

  it('requires explicit current ETag read and sends only check, then reads original action/state/gaps', async () => {
    render(<WellArchitectedBindingCheck {...props} />);
    expect(readWellArchitectedBinding).not.toHaveBeenCalled();
    expect(checkWellArchitectedBinding).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
    await read(); await submit();
    expect(checkWellArchitectedBinding).toHaveBeenCalledWith('product-one', 'project-one',
      { action: 'check', configuration_revision: 3, binding_id: 'binding-one' }, '"7"', expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/));
    expect(props.onInvalidate).toHaveBeenCalledTimes(2);
    expect(props.onRefresh).toHaveBeenCalledOnce();
    expect(screen.getByText(/Action: check; operation check-one: accepted/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Read original check status' }));
    await screen.findByText(/Action: check; operation check-one: blocked; gaps: provider_unqualified/);
    expect(getWellArchitectedBindingOperation).toHaveBeenCalledWith('product-one', 'project-one', 'check-one');
    expect(checkWellArchitectedBinding).toHaveBeenCalledOnce();
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
  });

  it('keeps the original uncertain request across remount and explicitly replays the same key/body/ETag', async () => {
    vi.mocked(checkWellArchitectedBinding).mockRejectedValueOnce(new Error('Network response lost'));
    const mounted = render(<WellArchitectedBindingCheck {...props} />);
    await read(); await submit();
    const original = vi.mocked(checkWellArchitectedBinding).mock.calls[0];
    expect(screen.getByRole('button', { name: 'Replay original check' })).toBeDisabled();
    mounted.unmount(); render(<WellArchitectedBindingCheck {...props} />);
    await userEvent.click(screen.getByRole('button', { name: 'Read retained check receipt' }));
    expect(screen.getByText(/receipt: pending/)).toBeInTheDocument();
    expect(checkWellArchitectedBinding).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByLabelText('I reviewed the original check request'));
    await userEvent.click(screen.getByRole('button', { name: 'Replay original check' }));
    await screen.findByText(/operation check-one: accepted/);
    expect(vi.mocked(checkWellArchitectedBinding).mock.calls[1]).toEqual(original);
  });

  it('blocks replacements and replay after saved revision changes but preserves the original key for inspection', async () => {
    vi.mocked(checkWellArchitectedBinding).mockRejectedValueOnce(new Error('Network response lost'));
    const { rerender } = render(<WellArchitectedBindingCheck {...props} />);
    await read(); await submit();
    rerender(<WellArchitectedBindingCheck {...props} configuration={{ ...configuration, value: { ...configuration.value, revision: 4 } }} />);
    await read();
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
    await userEvent.click(screen.getByLabelText('I reviewed the original check request'));
    expect(screen.getByRole('button', { name: 'Replay original check' })).toBeDisabled();
    expect(screen.getByText(/configuration revision 3; receipt: pending/)).toBeInTheDocument();
  });

  it('clears current binding eligibility after a failed refresh', async () => {
    render(<WellArchitectedBindingCheck {...props} />); await read();
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeEnabled();
    vi.mocked(readWellArchitectedBinding).mockRejectedValueOnce({ status: 503 });
    await userEvent.click(screen.getByRole('button', { name: 'Read current binding' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
    expect(checkWellArchitectedBinding).not.toHaveBeenCalled();
  });

  it.each([
    ['ETag', { value: binding, etag: '"6"' }, false],
    ['scope', { value: { ...binding, scope: { ...scope, tenant_id: 'different' } }, etag: '"7"' }, true],
    ['connection', { value: { ...binding, connection_ref: { ...connection, record_revision: '2' } }, etag: '"7"' }, true],
  ] as const)('rejects mismatched current binding %s', async (_label, response, denies) => {
    vi.mocked(readWellArchitectedBinding).mockResolvedValue(response);
    render(<WellArchitectedBindingCheck {...props} />);
    await userEvent.selectOptions(screen.getByLabelText('Existing review binding'), binding.binding_id);
    await userEvent.click(screen.getByRole('button', { name: 'Read current binding' }));
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
    expect(props.onAccessDenied).toHaveBeenCalledTimes(denies ? 1 : 0);
    expect(checkWellArchitectedBinding).not.toHaveBeenCalled();
  });

  it.each(['delegation_denied', 'delegation_scope_changed'])('preserves known consent rejection %s and gives the owner a specific next action', async code => {
    vi.mocked(checkWellArchitectedBinding).mockRejectedValueOnce({ status: 403, detail: { code } });
    render(<WellArchitectedBindingCheck {...props} />); await read(); await submit();
    expect(screen.getByRole('alert')).toHaveTextContent('Open Owner consent and review');
    expect(screen.getByText(/receipt: rejected/)).toBeInTheDocument();
    expect(props.onAccessDenied).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
  });

  it('sends no mutation if retaining the original request fails', async () => {
    render(<WellArchitectedBindingCheck {...props} />); await read();
    const storage = vi.spyOn(sessionStorage, 'setItem').mockImplementation(() => { throw new Error('Storage blocked'); });
    try {
      await submit();
      expect(checkWellArchitectedBinding).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent('no check was sent');
    } finally { storage.mockRestore(); }
  });

  it('keeps receipts out of another owner scope and ignores late response after unmount', async () => {
    let resolve!: (value: Versioned<SecurityBindingOperation>) => void;
    vi.mocked(checkWellArchitectedBinding).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const { rerender } = render(<WellArchitectedBindingCheck {...props} />); await read();
    await userEvent.click(screen.getByRole('button', { name: 'Check binding' }));
    rerender(<WellArchitectedBindingCheck {...props} configuration={{ ...configuration,
      value: { ...configuration.value, scope: { ...scope, owner_id: 'other-owner' } } } as Versioned<WellArchitectedConfiguration>} />);
    await act(async () => resolve({ value: operation, etag: '"1"' }));
    await userEvent.click(screen.getByRole('button', { name: 'Read retained check receipt' }));
    expect(screen.queryByText(/Original check request/)).not.toBeInTheDocument();
    expect(props.onRefresh).not.toHaveBeenCalled();
  });

  it('does not enable check without saved owner identity or for removed bindings', () => {
    render(<WellArchitectedBindingCheck {...props} bindings={[{ ...binding, state: 'unlinked' }]}
      configuration={{ ...configuration, value: { ...configuration.value, scope: { ...scope, owner_id: undefined } } } as Versioned<WellArchitectedConfiguration>} />);
    expect(screen.getByRole('button', { name: 'Check binding' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Read current binding' })).toBeDisabled();
    expect(screen.queryByRole('option', { name: /binding-one/ })).not.toBeInTheDocument();
  });
});
