import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DevOpsActivationSetup from '@/pages/products/services/DevOpsActivationSetup';
import { getDevOpsBindingOperation, getDevOpsConfiguration } from '@/services/l5Services';
import { listOwnerGrants } from '@/services/l5OwnerConsent';
import * as api from '@/services/devOpsActivation';
import { binding, configuration, consent, grant, limits, policy, resources, selection, storageKey } from '../../../fixtures/devOpsActivation';

vi.mock('@/services/l5Services', async importOriginal => ({ ...await importOriginal<typeof import('@/services/l5Services')>(),
  getDevOpsConfiguration: vi.fn(), getDevOpsBindingOperation: vi.fn() }));
vi.mock('@/services/l5OwnerConsent', async importOriginal => ({ ...await importOriginal<typeof import('@/services/l5OwnerConsent')>(), listOwnerGrants: vi.fn() }));
vi.mock('@/services/devOpsActivation', async importOriginal => ({ ...await importOriginal<typeof import('@/services/devOpsActivation')>(),
  listResourceBindings: vi.fn(), readResourceBinding: vi.fn(), previewResourceConsent: vi.fn(), grantResourceConsent: vi.fn(),
  verifyResource: vi.fn(), readVerifiedResources: vi.fn(), readResourceAccessPolicy: vi.fn(), activateInvestigation: vi.fn(), readDevOpsConfigurationVersion: vi.fn() }));

const current = { value: configuration, etag: '"config-hash"' };
const operation = { operation_id: 'operation', action: 'check' as const, binding_id: 'binding', revision: 1, state: 'succeeded', gaps: [] };
const renderSetup = () => {
  const onActivated = vi.fn();
  return { ...render(<DevOpsActivationSetup productId="product" projectId="project" configuration={current} onActivated={onActivated} />), onActivated };
};
const click = (name: string) => userEvent.click(screen.getByRole('button', { name }));
const read = () => click('Read resource setup and recover receipts');
async function prepareConsent() {
  await read();
  await userEvent.selectOptions(screen.getByLabelText('Resource verification binding'), 'binding');
  fireEvent.change(screen.getByLabelText('Distinct resource IAM role ARN'), { target: { value: selection.role_arn } });
  fireEvent.change(screen.getByLabelText('Lambda function ARN'), { target: { value: selection.resource_arn } });
  fireEvent.change(screen.getByLabelText('CloudWatch log-group ARN ending in :*'), { target: { value: selection.telemetry_arn } });
  await click('Review resource consent scope');
  fireEvent.change(screen.getByLabelText('Resource consent expiry (UTC)'), { target: { value: consent.body.expires_at } });
  await userEvent.click(screen.getByRole('checkbox', { name: 'I reviewed the complete resource scope and its expiry.' }));
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear();
  vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2030-01-01T00:01:00Z'));
  vi.mocked(getDevOpsConfiguration).mockResolvedValue(current);
  vi.mocked(listOwnerGrants).mockResolvedValue({ items: [] });
  vi.mocked(api.listResourceBindings).mockResolvedValue({ items: [binding] });
  vi.mocked(api.readResourceBinding).mockResolvedValue({ value: binding, etag: '"binding-4"' });
  vi.mocked(api.previewResourceConsent).mockResolvedValue({ value: { scope: consent.scope, scope_digest: consent.body.scope_digest, maximum_lifetime_seconds: 86400 }, etag: consent.etag });
  vi.mocked(api.grantResourceConsent).mockResolvedValue({ value: grant, etag: '"1"' });
  vi.mocked(api.verifyResource).mockResolvedValue({ value: operation, etag: '"1"' });
  vi.mocked(getDevOpsBindingOperation).mockResolvedValue(operation);
  vi.mocked(api.readVerifiedResources).mockResolvedValue(resources);
  vi.mocked(api.readResourceAccessPolicy).mockResolvedValue(policy);
});
afterEach(() => vi.restoreAllMocks());

describe('explicit DevOps resource and activation setup', () => {
  it('shows the denied provider step and the action needed to recover verification', async () => {
    vi.mocked(api.verifyResource).mockResolvedValue({ value: { ...operation, state: 'blocked',
      gaps: ['association_readback_denied'] }, etag: '"2"' });
    renderSetup(); await prepareConsent(); await click('Grant resource-verification consent');
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [grant] });
    await read();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Verify the workload in the retained resource consent scope.' }));
    await click('Verify resource access');
    expect(screen.getAllByText(/AWS denied reading the workload connection/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/aidevops:GetAssociation access to the selected agent space/).length).toBeGreaterThan(0);
    expect(api.verifyResource).toHaveBeenCalledOnce();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it('performs no reads, grants, provider operations or configuration changes on mount', () => {
    renderSetup();
    expect(getDevOpsConfiguration).not.toHaveBeenCalled();
    expect(api.listResourceBindings).not.toHaveBeenCalled();
    expect(api.grantResourceConsent).not.toHaveBeenCalled();
    expect(api.verifyResource).not.toHaveBeenCalled();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it('persists explicit scope before granting and keeps verification a separate action', async () => {
    renderSetup(); await prepareConsent();
    expect(api.grantResourceConsent).not.toHaveBeenCalled();
    vi.mocked(api.grantResourceConsent).mockImplementation(async (_, __, receipt) => {
      expect(JSON.parse(sessionStorage.getItem(storageKey)!).consent).toEqual(receipt);
      return { value: grant, etag: '"1"' };
    });
    await click('Grant resource-verification consent');
    expect(api.grantResourceConsent).toHaveBeenCalledWith('product', 'project', expect.objectContaining({ body: consent.body, scope: consent.scope }));
    expect(api.verifyResource).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Verify resource access' })).toBeDisabled();
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [grant] });
    await read();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Verify the workload in the retained resource consent scope.' }));
    await click('Verify resource access');
    expect(api.verifyResource).toHaveBeenCalledWith('product', 'project', expect.objectContaining({ etag: '"binding-4"', body: {
      action: 'check', configuration_revision: 3, binding_id: 'binding', resource_selection: selection } }));
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it('permits separate resource consent while native UI consent remains active', async () => {
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [{ ...grant, delegation_id: 'native-grant', execution_scope: {
      ...consent.scope, purpose: 'devops_native_ui', control_action: 'devops_native_ui', operations: ['credential:session', 'browser:launch'],
    } }] });
    renderSetup(); await prepareConsent();
    expect(screen.getByRole('button', { name: 'Grant resource-verification consent' })).toBeEnabled();
    await click('Grant resource-verification consent');
    expect(api.grantResourceConsent).toHaveBeenCalledOnce();
    expect(api.verifyResource).not.toHaveBeenCalled();
  });
  it('reconciles lost consent responses and reuses the original key, expiry and reviewed scope', async () => {
    vi.mocked(api.grantResourceConsent).mockRejectedValueOnce({ status: 503 });
    renderSetup(); await prepareConsent(); await click('Grant resource-verification consent');
    const original = vi.mocked(api.grantResourceConsent).mock.calls[0][2];
    expect(await screen.findByRole('alert')).toHaveTextContent('Consent was not confirmed');
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).consent.state).toBe('pending');
    await read(); await click('Review resource consent scope');
    expect(screen.getByLabelText('Resource consent expiry (UTC)')).toBeDisabled();
    await userEvent.click(screen.getByRole('checkbox', { name: 'I reviewed the complete resource scope and its expiry.' }));
    await click('Retry original resource consent');
    expect(vi.mocked(api.grantResourceConsent).mock.calls[1][2]).toEqual(original);
  });
  it('recovers an uncertain verification from storage and never replaces its original binding revision', async () => {
    const check = { key: 'original_verify_key', etag: '"original-binding-4"', state: 'pending',
      body: { action: 'check', configuration_revision: 3, binding_id: 'binding', resource_selection: selection } };
    sessionStorage.setItem(storageKey, JSON.stringify({ consent, check }));
    vi.mocked(listOwnerGrants).mockResolvedValue({ items: [grant] });
    vi.mocked(api.listResourceBindings).mockResolvedValue({ items: [{ ...binding, generation: 3, revision: 6 }] });
    renderSetup(); await read();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Verify the workload in the retained resource consent scope.' }));
    await click('Retry original resource verification');
    expect(api.verifyResource).toHaveBeenCalledWith('product', 'project', check);
    expect(api.readResourceBinding).not.toHaveBeenCalled();
  });
  it('requires verified references and reviewed bounds before activating and stores the exact activation request', async () => {
    const { onActivated } = renderSetup(); await read();
    await userEvent.selectOptions(screen.getByLabelText('Resource verification binding'), 'binding');
    await click('Read verified workload references');
    fireEvent.change(screen.getByLabelText('Investigation classification'), { target: { value: 'internal' } });
    fireEvent.change(screen.getByLabelText('Cost estimate (USD)'), { target: { value: '1.25' } });
    fireEvent.change(screen.getByLabelText('Deadline in seconds (60–3600)'), { target: { value: '300' } });
    fireEvent.change(screen.getByLabelText('Maximum polls (1–60)'), { target: { value: '10' } });
    await click('Review Investigation activation');
    expect(screen.getByRole('button', { name: 'Activate manual Investigation' })).toBeDisabled();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
    vi.mocked(api.activateInvestigation).mockImplementation(async (_, __, receipt) => {
      expect(JSON.parse(sessionStorage.getItem(storageKey)!).activation).toEqual(receipt);
      return { value: { ...configuration, hash: 'activated-hash', revision: 4, settings: receipt.body }, etag: '"activated-hash"' };
    });
    await userEvent.click(screen.getByRole('checkbox', { name: 'I reviewed these saved resources and run limits.' }));
    await click('Activate manual Investigation');
    expect(onActivated).toHaveBeenCalledTimes(1);
    expect(api.activateInvestigation).toHaveBeenCalledWith('product', 'project', expect.objectContaining({ etag: '"config-hash"', previous_revision: 3,
      body: expect.objectContaining({ enabled: true, limits: expect.objectContaining({ max_cost: '1.25', concurrency: 1 }) }) }));
    expect(api.verifyResource).not.toHaveBeenCalled();
  });
  it('confirms an activation lost across reload from exact configuration readback without another PUT', async () => {
    const body = api.buildActivation(configuration, binding, resources, policy, limits, Date.now());
    sessionStorage.setItem(storageKey, JSON.stringify({ activation: { key: 'original_activation_key', etag: current.etag, body,
      state: 'pending', configuration_id: 'config', previous_revision: 3 } }));
    vi.mocked(getDevOpsConfiguration).mockResolvedValue({ value: { ...configuration, revision: 4, hash: 'activated', settings: body }, etag: '"activated"' });
    const { onActivated } = renderSetup(); await read();
    expect(onActivated).toHaveBeenCalledTimes(1);
    expect(api.activateInvestigation).not.toHaveBeenCalled();
    expect(api.listResourceBindings).not.toHaveBeenCalled();
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).activation.state).toBe('confirmed');
  });
  it('does not mark an already loaded activation revision stale when its receipt is recovered again', async () => {
    const body = api.buildActivation(configuration, binding, resources, policy, limits, Date.now());
    sessionStorage.setItem(storageKey, JSON.stringify({ activation: { key: 'original_activation_key', etag: current.etag, body,
      state: 'pending', configuration_id: 'config', previous_revision: 3 } }));
    const activated = { value: { ...configuration, revision: 4, hash: 'activated', settings: body }, etag: '"activated"' };
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(activated);
    const onActivated = vi.fn();
    render(<DevOpsActivationSetup productId="product" projectId="project" configuration={activated} onActivated={onActivated} />);
    await read();
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).activation.state).toBe('confirmed');
    await read();
    expect(onActivated).not.toHaveBeenCalled();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it('reconciles the original activation from history when later configuration edits already exist', async () => {
    const body = api.buildActivation(configuration, binding, resources, policy, limits, Date.now());
    sessionStorage.setItem(storageKey, JSON.stringify({ activation: { key: 'original_activation_key', etag: current.etag, body,
      state: 'pending', configuration_id: 'config', previous_revision: 3 } }));
    const latest = { value: { ...configuration, revision: 6, hash: 'latest' }, etag: '"latest"' };
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(latest);
    vi.mocked(api.readDevOpsConfigurationVersion).mockResolvedValue({ value: { ...configuration, revision: 4, hash: 'activated', settings: body }, etag: '"activated"' });
    const { onActivated } = renderSetup(); await read();
    expect(api.readDevOpsConfigurationVersion).toHaveBeenCalledWith('product', 'project', 4);
    expect(onActivated).toHaveBeenCalledWith(latest, 4);
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).activation.state).toBe('confirmed');
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it.each(['changed', 'unavailable'])('keeps activation unresolved when historical evidence is %s', async kind => {
    const body = api.buildActivation(configuration, binding, resources, policy, limits, Date.now());
    sessionStorage.setItem(storageKey, JSON.stringify({ activation: { key: 'original_activation_key', etag: current.etag, body,
      state: 'pending', configuration_id: 'config', previous_revision: 3 } }));
    const latest = { value: { ...configuration, revision: 6, hash: 'latest' }, etag: '"latest"' };
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(latest);
    if (kind === 'unavailable') vi.mocked(api.readDevOpsConfigurationVersion).mockRejectedValue({ status: 403 });
    else vi.mocked(api.readDevOpsConfigurationVersion).mockResolvedValue({ value: { ...configuration, revision: 4, hash: 'different' }, etag: '"different"' });
    const onActivated = vi.fn();
    render(<DevOpsActivationSetup productId="product" projectId="project" configuration={latest} onActivated={onActivated} />);
    await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('Original activation is unresolved');
    expect(screen.getByRole('alert')).toHaveTextContent('revision 4');
    expect(JSON.parse(sessionStorage.getItem(storageKey)!).activation.state).toBe('pending');
    expect(onActivated).not.toHaveBeenCalled();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Retry original Investigation activation' })).not.toBeInTheDocument();
  });
  it.each(['resource scope', 'telemetry connection', 'policy scope', 'owner', 'generation', 'binding revision', 'native space'])('rejects changed %s before displaying verified references', async kind => {
    if (kind === 'resource scope') vi.mocked(api.readVerifiedResources).mockResolvedValue({ ...resources, resource_refs: [{ ...resources.resource_refs[0], tenant_id: 'other' }] });
    if (kind === 'telemetry connection') vi.mocked(api.readVerifiedResources).mockResolvedValue({ ...resources, telemetry_refs: [{ ...resources.telemetry_refs[0], source_connection_id: 'other' }] });
    if (kind === 'policy scope') vi.mocked(api.readResourceAccessPolicy).mockResolvedValue({ ...policy, policy_ref: { ...policy.policy_ref, project_id: 'other' } });
    if (kind === 'owner') vi.mocked(api.readResourceAccessPolicy).mockResolvedValue({ ...policy, document: { ...policy.document, scope: { ...policy.document.scope, owner_id: 'other' } } });
    if (kind === 'generation') vi.mocked(api.readResourceAccessPolicy).mockResolvedValue({ ...policy, document: { ...policy.document, binding_generation: 3 } });
    if (kind === 'binding revision') vi.mocked(api.readResourceAccessPolicy).mockResolvedValue({ ...policy, document: { ...policy.document, binding_revision: 6 } });
    if (kind === 'native space') vi.mocked(api.readResourceAccessPolicy).mockResolvedValue({ ...policy, document: { ...policy.document, native_space_id: 'other' } });
    renderSetup(); await read();
    await userEvent.selectOptions(screen.getByLabelText('Resource verification binding'), 'binding');
    await click('Read verified workload references');
    expect(await screen.findByRole('alert')).toHaveTextContent('Verified resources are unavailable');
    expect(screen.queryByText('Verified workload and access policy')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Investigation classification')).not.toBeInTheDocument();
    expect(api.activateInvestigation).not.toHaveBeenCalled();
  });
  it('blocks a changed or denied configuration and retains draft entries during readback', async () => {
    renderSetup(); await prepareConsent();
    vi.mocked(getDevOpsConfiguration).mockRejectedValueOnce({ status: 403 });
    await read();
    expect(await screen.findByRole('alert')).toHaveTextContent('current Product and configuration owner');
    expect(screen.queryByRole('button', { name: 'Grant resource-verification consent' })).not.toBeInTheDocument();
    await read();
    expect(screen.getByLabelText('Lambda function ARN')).toHaveValue(selection.resource_arn);
    vi.mocked(api.previewResourceConsent).mockResolvedValueOnce({ value: { scope: { ...consent.scope, created_by: 'other-owner' }, scope_digest: consent.body.scope_digest, maximum_lifetime_seconds: 86400 }, etag: consent.etag });
    await click('Review resource consent scope');
    expect(await screen.findByRole('alert')).toHaveTextContent('consent scope changed');
    expect(api.grantResourceConsent).not.toHaveBeenCalled();
  });
  it('does not restore another owner’s pending receipts', async () => {
    sessionStorage.setItem(storageKey, JSON.stringify({ consent: { ...consent, state: 'pending' } }));
    const changed = { ...current, value: { ...configuration, scope: { ...configuration.scope, owner_id: 'new-owner' } } };
    vi.mocked(getDevOpsConfiguration).mockResolvedValue(changed);
    render(<DevOpsActivationSetup productId="product" projectId="project" configuration={changed} onActivated={vi.fn()} />);
    await read();
    expect(screen.queryByText(/outcome is uncertain/)).not.toBeInTheDocument();
    expect(api.grantResourceConsent).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Read resource setup and recover receipts' })).toBeEnabled());
  });
});
