/**
 * Tests for TenantOrgLinks admin page.
 *
 * Issue #2954: Platform-admin can link/unlink GitHub orgs to a parent tenant.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import TenantOrgLinks from '@/pages/admin/TenantOrgLinks';

vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn(),
}));

vi.mock('@/services/tenantLinks', () => ({
  getLinkedOrgs: vi.fn(),
  linkOrgToTenant: vi.fn(),
  unlinkOrgFromTenant: vi.fn(),
}));

import { getOrganizations } from '@/services/admin';
import {
  getLinkedOrgs,
  linkOrgToTenant,
  unlinkOrgFromTenant,
} from '@/services/tenantLinks';

const mockGetOrganizations = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetLinkedOrgs = getLinkedOrgs as ReturnType<typeof vi.fn>;
const mockLinkOrg = linkOrgToTenant as ReturnType<typeof vi.fn>;
const mockUnlinkOrg = unlinkOrgFromTenant as ReturnType<typeof vi.fn>;

const mockTenants = {
  items: [
    { id: 'tenant-a', name: 'Sophos', awsAccounts: [], roleMappings: {}, settings: {}, createdAt: '2026-01-01' },
    { id: 'tenant-b', name: 'Acme', awsAccounts: [], roleMappings: {}, settings: {}, createdAt: '2026-01-02' },
  ],
  total: 2,
  page: 1,
  pageSize: 200,
  hasMore: false,
};

const mockLinkedOrgs = {
  tenantId: 'tenant-a',
  linkedOrgs: [
    { orgId: 'org-1', orgName: 'sophos-research', githubOrgId: '22222' },
  ],
};

function renderPage() {
  return render(
    <MemoryRouter>
      <TenantOrgLinks />
    </MemoryRouter>,
  );
}

describe('TenantOrgLinks Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetOrganizations.mockResolvedValue(mockTenants);
    mockGetLinkedOrgs.mockResolvedValue({ tenantId: 'tenant-a', linkedOrgs: [] });
    mockLinkOrg.mockResolvedValue({
      linked: true,
      tenantId: 'tenant-a',
      githubOrgId: '22222',
      orgName: 'sophos-research',
    });
    mockUnlinkOrg.mockResolvedValue({
      unlinked: true,
      tenantId: 'tenant-a',
      githubOrgId: '22222',
    });
  });

  it('renders the page title', async () => {
    renderPage();
    expect(screen.getByText('Tenant Org Links')).toBeInTheDocument();
  });

  it('shows the attach-forward-only warning', async () => {
    renderPage();
    expect(screen.getByText(/Attach-forward-only/)).toBeInTheDocument();
  });

  it('loads and displays tenants in the selector', async () => {
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByText('Sophos')).toBeInTheDocument();
    expect(screen.getByText('Acme')).toBeInTheDocument();
  });

  it('shows link form when a tenant is selected', async () => {
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    expect(screen.getByText('Link a GitHub Organization')).toBeInTheDocument();
    expect(screen.getByLabelText('GitHub Organization ID')).toBeInTheDocument();
  });

  it('fetches linked orgs when tenant is selected', async () => {
    mockGetLinkedOrgs.mockResolvedValue(mockLinkedOrgs);
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    await waitFor(() => {
      expect(mockGetLinkedOrgs).toHaveBeenCalledWith('tenant-a');
    });
    expect(screen.getByText('sophos-research')).toBeInTheDocument();
    expect(screen.getByText('22222')).toBeInTheDocument();
  });

  it('links an org when form is submitted', async () => {
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    const input = screen.getByLabelText('GitHub Organization ID');
    await userEvent.type(input, '22222');

    const linkButton = screen.getByText('Link Org');
    await userEvent.click(linkButton);

    await waitFor(() => {
      expect(mockLinkOrg).toHaveBeenCalledWith('tenant-a', '22222');
    });
  });

  it('shows unlink confirmation modal', async () => {
    mockGetLinkedOrgs.mockResolvedValue(mockLinkedOrgs);
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    await waitFor(() => {
      expect(screen.getByText('sophos-research')).toBeInTheDocument();
    });

    const unlinkButton = screen.getByText('Unlink');
    await userEvent.click(unlinkButton);

    expect(screen.getByText('Confirm Unlink')).toBeInTheDocument();
    expect(screen.getByText(/Are you sure you want to unlink/)).toBeInTheDocument();
  });

  it('unlinks an org when confirmed', async () => {
    mockGetLinkedOrgs.mockResolvedValue(mockLinkedOrgs);
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    await waitFor(() => {
      expect(screen.getByText('sophos-research')).toBeInTheDocument();
    });

    // Click "Unlink" in the table
    const unlinkButton = screen.getByText('Unlink');
    await userEvent.click(unlinkButton);

    // Click "Unlink" in the confirmation modal
    const confirmButtons = screen.getAllByText('Unlink');
    const modalUnlink = confirmButtons[confirmButtons.length - 1];
    await userEvent.click(modalUnlink);

    await waitFor(() => {
      expect(mockUnlinkOrg).toHaveBeenCalledWith('tenant-a', '22222');
    });
  });

  it('shows empty state when no orgs are linked', async () => {
    mockGetLinkedOrgs.mockResolvedValue({ tenantId: 'tenant-a', linkedOrgs: [] });
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    await waitFor(() => {
      expect(screen.getByText(/No organizations are currently linked/)).toBeInTheDocument();
    });
  });

  it('shows error when link fails', async () => {
    mockLinkOrg.mockRejectedValue(new Error('409: Organization already linked'));
    renderPage();
    await waitFor(() => {
      expect(mockGetOrganizations).toHaveBeenCalled();
    });

    const select = screen.getByLabelText('Parent Tenant');
    await userEvent.selectOptions(select, 'tenant-a');

    const input = screen.getByLabelText('GitHub Organization ID');
    await userEvent.type(input, '99999');

    const linkButton = screen.getByText('Link Org');
    await userEvent.click(linkButton);

    await waitFor(() => {
      expect(screen.getByText(/409: Organization already linked/)).toBeInTheDocument();
    });
  });
});
