import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import ToolEntitlements from '@/pages/admin/ToolEntitlements';
import { getOrganizations, getOrgTeams } from '@/services/admin';
import {
  getToolAccess, getToolsCatalog, getToolsConsentDefault, getToolEntitlements, putToolEntitlement, putToolsConsentDefault,
} from '@/services/toolEntitlements';

const state = { allowed: true, platform: false, orgId: 'tenant-one' };
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    user: { orgId: state.orgId },
    hasPermission: () => state.allowed,
    isPlatformAdmin: () => state.platform,
  }),
}));
vi.mock('@/services/admin', () => ({ getOrganizations: vi.fn(), getOrgTeams: vi.fn() }));
vi.mock('@/services/toolEntitlements', () => ({
  getToolAccess: vi.fn(), getToolsCatalog: vi.fn(), getToolEntitlements: vi.fn(), putToolEntitlement: vi.fn(),
  getToolsConsentDefault: vi.fn(), putToolsConsentDefault: vi.fn(),
}));

const tenant = { team_id: null, tool_name: 'browser.start', enabled: true, max_cost_usd: '0.20', revision: 4 };
const denied = { team_id: 'team-x', tool_name: 'browser.start', enabled: false, max_cost_usd: null, revision: 2 };

function view() {
  return render(<MemoryRouter><ToolEntitlements /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  state.allowed = true;
  state.platform = false;
  state.orgId = 'tenant-one';
  vi.mocked(getOrganizations).mockResolvedValue({ items: [], hasMore: false, total: 0, page: 1, pageSize: 100 });
  vi.mocked(getOrgTeams).mockResolvedValue({ items: [
    { id: 'team-x', name: 'Team X' }, { id: 'team-y', name: 'Team Y' },
  ] as Awaited<ReturnType<typeof getOrgTeams>>['items'], hasMore: false, total: 2, page: 1, pageSize: 100 });
  vi.mocked(getToolsCatalog).mockResolvedValue({
    tools: [{ name: 'browser.start', description: 'Open browser', connector: 'browser', credential_kinds: ['GATEWAY_IAM_ROLE'] }],
    connectors: [{ name: 'browser', credential_kinds: ['GATEWAY_IAM_ROLE'], dynamic: false }],
  });
  vi.mocked(getToolEntitlements).mockImplementation(async (org, team) => ({
    tenant_id: org, team_id: team, entitlements: team === 'team-x' ? [tenant, denied] : [tenant],
  }));
  vi.mocked(putToolEntitlement).mockResolvedValue(denied);
  vi.mocked(getToolsConsentDefault).mockResolvedValue({
    tenant_id: 'tenant-one', consent_default: 'granted', tenant_override: null, platform_default: 'granted', choices: ['granted', 'explicit'],
  });
  vi.mocked(putToolsConsentDefault).mockImplementation(async (tenantId, value) => ({
    tenant_id: tenantId, consent_default: value ?? 'granted', tenant_override: value, platform_default: 'granted', choices: ['granted', 'explicit'],
  }));
  vi.mocked(getToolAccess).mockResolvedValue({
    schema_version: '1.0', tenant_id: 'tenant-one', user_id: 'user-one', team_id: 'team-x',
    tools: [{ name: 'browser.start', ready: false, reason: 'team_disabled', max_cost_usd: null },
      { name: 'browser.step', ready: true, reason: null, discovery_reason: 'catalogue_stale', max_cost_usd: '0.20' }],
  });
});

describe('tool entitlement admin page', () => {
  it('does not load settings for a caller without the permission', () => {
    state.allowed = false;
    view();
    expect(screen.getByText(/do not have permission/)).toBeInTheDocument();
    expect(getToolsCatalog).not.toHaveBeenCalled();
    expect(getToolEntitlements).not.toHaveBeenCalled();
  });

  it('shows the team X denial separately from team Y inheritance and writes the observed revision', async () => {
    const user = userEvent.setup();
    view();
    expect(await screen.findByRole('checkbox', { name: 'browser.start' })).toBeChecked();
    expect(getToolsCatalog).toHaveBeenCalledWith('tenant-one');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Scope' }), 'team-x');
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'browser.start' })).not.toBeChecked());
    expect(screen.getByText('Team override')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: 'browser.start' }));
    expect(putToolEntitlement).toHaveBeenCalledWith({
      tenant_id: 'tenant-one', team_id: 'team-x', tool_name: 'browser.start', enabled: true,
      max_cost_usd: null, expected_revision: 2,
    });
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Scope' })).not.toBeDisabled());
    await user.selectOptions(screen.getByRole('combobox', { name: 'Scope' }), 'team-y');
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'browser.start' })).toBeChecked());
    expect(screen.getByText('Inherited from tenant')).toBeInTheDocument();
    expect(getToolEntitlements).toHaveBeenCalledWith('tenant-one', 'team-y');
  });

  it('saves a team cost ceiling without replacing the tenant grant', async () => {
    const user = userEvent.setup();
    view();
    await screen.findByRole('checkbox', { name: 'browser.start' });
    await user.selectOptions(screen.getByRole('combobox', { name: 'Scope' }), 'team-y');
    await screen.findByText('Inherited from tenant');
    await user.clear(screen.getByRole('spinbutton', { name: 'browser.start cost limit' }));
    await user.type(screen.getByRole('spinbutton', { name: 'browser.start cost limit' }), '0.05');
    await user.click(screen.getByRole('button', { name: 'Save limit' }));
    expect(putToolEntitlement).toHaveBeenCalledWith({
      tenant_id: 'tenant-one', team_id: 'team-y', tool_name: 'browser.start', enabled: true,
      max_cost_usd: '0.05', expected_revision: 0,
    });
  });

  it('requires a platform admin to choose an organization and reloads after a stale write', async () => {
    state.platform = true;
    vi.mocked(getOrganizations).mockResolvedValue({
      items: [{ id: 'tenant-two', name: 'Tenant Two' }] as Awaited<ReturnType<typeof getOrganizations>>['items'],
      hasMore: false, total: 1, page: 1, pageSize: 100,
    });
    vi.mocked(putToolEntitlement).mockRejectedValueOnce({ status: 409, detail: 'Tool entitlement changed; reload and retry' });
    const user = userEvent.setup();
    view();
    expect(getToolsCatalog).not.toHaveBeenCalled();
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Organization' }), 'tenant-two');
    await screen.findByRole('checkbox', { name: 'browser.start' });
    expect(getToolsCatalog).toHaveBeenCalledWith('tenant-two');
    await user.click(screen.getByRole('checkbox', { name: 'browser.start' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('reload and retry');
    expect(getToolEntitlements).toHaveBeenCalledWith('tenant-two', null);
  });
});


it('checks the actual user team without changing tenant entitlement', async () => {
  const user = userEvent.setup();
  view();
  await screen.findByRole('checkbox', { name: 'browser.start' });
  await user.type(screen.getByRole('textbox', { name: 'User ID' }), 'user-one');
  await user.click(screen.getByRole('button', { name: 'Check access' }));
  expect(getToolAccess).toHaveBeenCalledWith('tenant-one', 'user-one');
  expect(await screen.findByText('browser.start: team disabled')).toBeInTheDocument();
  expect(screen.getByText('browser.step: Policy ready; hidden from discovery: catalogue stale')).toBeInTheDocument();
  expect(screen.getByText(/does not verify a live provider/)).toBeInTheDocument();
  expect(putToolEntitlement).not.toHaveBeenCalled();
});

it('shows the tenant consent posture and lets an administrator require explicit opt-in (#8387)', async () => {
  const user = userEvent.setup();
  view();
  const posture = await screen.findByRole('combobox', { name: 'Consent posture' });
  expect(posture).toHaveValue('platform');
  expect(screen.getByText('Effective: granted')).toBeInTheDocument();
  await user.selectOptions(posture, 'explicit');
  await waitFor(() => expect(putToolsConsentDefault).toHaveBeenCalledWith('tenant-one', 'explicit'));
  expect(await screen.findByText('Effective: explicit')).toBeInTheDocument();
  await user.selectOptions(posture, 'platform');
  await waitFor(() => expect(putToolsConsentDefault).toHaveBeenCalledWith('tenant-one', null));
  expect(putToolEntitlement).not.toHaveBeenCalled();
});
