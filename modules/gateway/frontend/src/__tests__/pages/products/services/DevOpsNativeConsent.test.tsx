import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DevOpsNativeConsent from '@/pages/products/services/DevOpsNativeConsent';
import { grantNativeConsent, previewNativeConsent } from '@/services/devOpsNativeAccess';
import { listOwnerGrants, revokeOwnerConsent, type ConsentPreview, type OwnerGrant } from '@/services/l5OwnerConsent';
import type { DevOpsBinding, DevOpsConfiguration, Versioned } from '@/services/l5Services';
import { configuration as setup, binding as selected } from '../../../fixtures/devOpsActivation';

vi.mock('@/services/devOpsNativeAccess', async original => ({
  ...await original<typeof import('@/services/devOpsNativeAccess')>(),
  grantNativeConsent: vi.fn(), previewNativeConsent: vi.fn(),
}));
vi.mock('@/services/l5OwnerConsent', async original => ({
  ...await original<typeof import('@/services/l5OwnerConsent')>(),
  listOwnerGrants: vi.fn(), revokeOwnerConsent: vi.fn(),
}));

const policy = { launcher_role_arn: 'arn:aws:iam::123456789012:role/launcher', operator_role_arn: 'arn:aws:iam::123456789012:role/operator' };
const configuration = { value: setup, etag: '"config-hash"' } as Versioned<DevOpsConfiguration>;
const binding = { value: selected, etag: '"4"' } as Versioned<DevOpsBinding>;
const preview: ConsentPreview = {
  scope_digest: 'a'.repeat(64), maximum_lifetime_seconds: 86400,
  scope: {
    purpose: 'devops_native_ui', control_action: 'devops_native_ui', tenant_id: 'tenant', product_id: 'product', project_id: 'project',
    service_id: 'l5-devops-worker', created_by: 'owner', credential_id: 'connection',
    configuration_id: 'config', configuration_revision: 3, configuration_hash: 'config-hash',
    provider_account_id: '123456789012', provider_role_arn: selected.provider_identity!.role_arn, region: 'us-east-1',
    binding_id: 'binding', binding_generation: 2, operations: ['credential:session'], capabilities: ['native_ui'],
    sources: [{ ...selected.connection_ref, record_type: 'credential' }], visibility: 'only_me',
    native_ui_policy: { ...policy, binding_revision: 4, native_space_ref: selected.native_resource_ref,
      setup_operation_id: 'original-setup', authentication_mode: 'scoped_iam_federation',
      browser_launch_policy: 'scoped_session', permitted_native_actions: ['launchNativeBrowser'] },
  },
};
function granted(expiry: string, scope = preview.scope): OwnerGrant {
  return { delegation_id: 'native-grant', revision: 1, tenant_id: 'tenant', product_id: 'product', project_id: 'project',
    service_id: 'l5-devops-worker', credential_id: 'connection', binding_generation: 2, operations: scope.operations,
    sources: scope.sources, capabilities: scope.capabilities, visibility: 'only_me', execution_scope: scope,
    expires_at: expiry, revoked_at: null, active: true };
}
async function mount(strict = false) {
  const element = <DevOpsNativeConsent productId="product" projectId="project" configuration={configuration} binding={binding} disabled={false} onChange={vi.fn()} />;
  const result = render(strict ? <StrictMode>{element}</StrictMode> : element);
  expect(screen.getByRole('heading', { name: 'Workspace sign-in access' })).toBeVisible();
  return result;
}
async function review(expiry = new Date(Date.now() + 300_000).toISOString()) {
  fireEvent.change(screen.getByLabelText('Native UI launcher role ARN'), { target: { value: policy.launcher_role_arn } });
  fireEvent.change(screen.getByLabelText('Native UI operator role ARN'), { target: { value: policy.operator_role_arn } });
  await userEvent.click(screen.getByRole('button', { name: 'Review native UI scope' }));
  await screen.findByLabelText('Native UI consent expiry (UTC)');
  fireEvent.change(screen.getByLabelText('Native UI consent expiry (UTC)'), { target: { value: expiry } });
  await userEvent.click(screen.getByRole('checkbox', { name: /I reviewed this exact space/ }));
  return expiry;
}
beforeEach(() => {
  vi.restoreAllMocks(); vi.resetAllMocks();
  vi.mocked(previewNativeConsent).mockResolvedValue({ value: structuredClone(preview), etag: configuration.etag });
  vi.mocked(listOwnerGrants).mockResolvedValue({ items: [] });
  vi.mocked(grantNativeConsent).mockImplementation(async (_product, _project, body) => ({ value: granted(body.expires_at), etag: '"1"' }));
});

describe('separate native UI owner consent', () => {
  it('does not request, grant or launch anything when opened', async () => {
    await mount();
    expect(previewNativeConsent).not.toHaveBeenCalled();
    expect(grantNativeConsent).not.toHaveBeenCalled();
    expect(listOwnerGrants).not.toHaveBeenCalled();
    expect(screen.getByText(/Already-issued handoffs and native app sessions/)).toBeInTheDocument();
  });

  it('reviews and explicitly grants the exact native scope in StrictMode with a short expiry', async () => {
    await mount(true);
    const expiry = await review();
    expect(grantNativeConsent).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Grant native UI consent' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Native UI consent saved');
    expect(previewNativeConsent).toHaveBeenCalledWith('product', 'project', 'binding', policy);
    expect(grantNativeConsent).toHaveBeenCalledWith('product', 'project', {
      binding_id: 'binding', for_control: true, control_action: 'devops_native_ui', native_ui_policy: policy,
      scope_digest: preview.scope_digest, expires_at: expiry,
    }, configuration.etag, expect.stringMatching(/^[A-Za-z0-9_-]{16,128}$/));
  });

  it.each(['scope', 'etag', 'capability', 'role'])('refuses a mismatched %s preview before grant', async mismatch => {
    const response = structuredClone(preview);
    if (mismatch === 'scope') response.scope.product_id = 'foreign';
    if (mismatch === 'capability') response.scope.capabilities.push('investigation');
    if (mismatch === 'role') response.scope.provider_role_arn = policy.launcher_role_arn;
    vi.mocked(previewNativeConsent).mockResolvedValue({ value: response, etag: mismatch === 'etag' ? '"changed"' : configuration.etag });
    await mount();
    fireEvent.change(screen.getByLabelText('Native UI launcher role ARN'), { target: { value: policy.launcher_role_arn } });
    fireEvent.change(screen.getByLabelText('Native UI operator role ARN'), { target: { value: policy.operator_role_arn } });
    await userEvent.click(screen.getByRole('button', { name: 'Review native UI scope' }));
    expect(await screen.findByRole('status')).toHaveTextContent('could not be confirmed');
    expect(screen.queryByRole('button', { name: 'Grant native UI consent' })).not.toBeInTheDocument();
    expect(grantNativeConsent).not.toHaveBeenCalled();
  });

  it('ignores ordinary execution grants when granting separate native access', async () => {
    const normal = granted(new Date(Date.now() + 3_600_000).toISOString(), { ...preview.scope, purpose: 'execution', capabilities: ['investigation'] });
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [normal] });
    await mount();
    await userEvent.click(screen.getByRole('button', { name: 'Read native UI grants' }));
    await review();
    expect(screen.getByRole('button', { name: 'Grant native UI consent' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: 'Revoke native UI grant' })).not.toBeInTheDocument();
  });

  it('reuses the original request after a lost response and an empty readback', async () => {
    vi.mocked(grantNativeConsent).mockRejectedValueOnce(new Error('response lost'));
    await mount(); await review();
    await userEvent.click(screen.getByRole('button', { name: 'Grant native UI consent' }));
    const original = vi.mocked(grantNativeConsent).mock.calls[0];
    await screen.findByRole('button', { name: 'Retry original native UI consent' });
    await userEvent.click(screen.getByRole('button', { name: 'Read native UI grants' }));
    await userEvent.click(screen.getByRole('button', { name: 'Retry original native UI consent' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Native UI consent saved');
    expect(vi.mocked(grantNativeConsent).mock.calls[1]).toEqual(original);
  });

  it('reconciles a lost response from current grants without issuing another grant', async () => {
    vi.mocked(grantNativeConsent).mockRejectedValueOnce(new Error('response lost'));
    await mount(); const expiry = await review();
    await userEvent.click(screen.getByRole('button', { name: 'Grant native UI consent' }));
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [granted(expiry)] });
    await userEvent.click(screen.getByRole('button', { name: 'Read native UI grants' }));
    expect(await screen.findByRole('status')).toHaveTextContent('original native UI grant is retained');
    expect(screen.queryByRole('button', { name: 'Retry original native UI consent' })).not.toBeInTheDocument();
    expect(grantNativeConsent).toHaveBeenCalledTimes(1);
  });

  it('permits exact outcome recovery after the original expiry without extending it', async () => {
    vi.mocked(grantNativeConsent).mockRejectedValueOnce(new Error('response lost'));
    const now = Date.now();
    await mount(); await review(new Date(now + 120_000).toISOString());
    await userEvent.click(screen.getByRole('button', { name: 'Grant native UI consent' }));
    const original = vi.mocked(grantNativeConsent).mock.calls[0];
    vi.spyOn(Date, 'now').mockReturnValue(now + 180_000);
    vi.mocked(grantNativeConsent).mockRejectedValueOnce({ status: 422, detail: { code: 'delegation_expiry_invalid' } });
    await userEvent.click(screen.getByRole('button', { name: 'Retry original native UI consent' }));
    expect(vi.mocked(grantNativeConsent).mock.calls[1]).toEqual(original);
    expect(screen.getByRole('button', { name: 'Review native UI scope' })).toBeEnabled();
    expect(screen.getByLabelText('Native UI launcher role ARN')).toBeEnabled();
  });

  it('does not update an unmounted consent panel after a delayed preview', async () => {
    let resolve!: (value: Versioned<ConsentPreview>) => void;
    vi.mocked(previewNativeConsent).mockReturnValue(new Promise(done => { resolve = done; }));
    const mounted = await mount();
    fireEvent.change(screen.getByLabelText('Native UI launcher role ARN'), { target: { value: policy.launcher_role_arn } });
    fireEvent.change(screen.getByLabelText('Native UI operator role ARN'), { target: { value: policy.operator_role_arn } });
    await userEvent.click(screen.getByRole('button', { name: 'Review native UI scope' }));
    mounted.unmount(); resolve({ value: preview, etag: configuration.etag });
    await waitFor(() => expect(grantNativeConsent).not.toHaveBeenCalled());
    expect(screen.queryByRole('button', { name: 'Grant native UI consent' })).not.toBeInTheDocument();
  });

  it('revokes only the explicit native grant and reports uncertain revocation for readback', async () => {
    const value = granted(new Date(Date.now() + 300_000).toISOString());
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [value] });
    vi.mocked(revokeOwnerConsent).mockRejectedValueOnce(new Error('lost response'));
    await mount();
    await userEvent.click(screen.getByRole('button', { name: 'Read native UI grants' }));
    await userEvent.click(screen.getByRole('button', { name: 'Revoke native UI grant' }));
    expect(revokeOwnerConsent).toHaveBeenCalledWith('product', 'project', 'devops', value);
    expect(await screen.findByRole('status')).toHaveTextContent('Read current native UI grants to confirm revocation');
  });
});
