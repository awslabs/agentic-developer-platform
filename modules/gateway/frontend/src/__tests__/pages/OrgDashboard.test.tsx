/**
 * OrgDashboard — absent-vs-empty rendering of the member approval policy (Issue #4929).
 *
 * Scoped to the one thing #4929 changes on this page. The approval-policy section used to
 * be gated on `orgDetail?.memberApprovalPolicy &&`, so an org whose response did not carry
 * the field made the entire "Organization Settings" heading and its toggle vanish — with no
 * error, no empty state, and a page that otherwise looked fully loaded. That is the #3675
 * symptom, and it is what an admin would have hit the moment #4847 repoints this read at the
 * canonical identity route, which has never sent `member_approval_policy`.
 *
 * The properties that matter here are (a) absence is stated rather than silently swallowed,
 * (b) a real policy still renders the working toggle, and (c) the section stays gated on
 * permission, so the note is not a new leak to callers who could never see the setting.
 *
 * The toggle's own behaviour (its PUT, its copy, its saving state) belongs to #2984 and is
 * not re-tested here; it is stubbed to a marker so these assertions are unambiguous and off
 * the network.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import OrgDashboard from '@/pages/OrgDashboard';
import { ToastProvider } from '@/contexts/ToastContext';

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useParams: () => ({ orgId: 'org-1' }) };
});

vi.mock('@/services/dashboard', () => ({ getOrgDashboard: vi.fn() }));
vi.mock('@/services/budget', () => ({ getUsageTimeSeries: vi.fn() }));
vi.mock('@/services/admin', () => ({
  getDepartments: vi.fn(),
  getUserRoles: vi.fn(),
  getOrganization: vi.fn(),
  getAvailableRoles: vi.fn(),
  assignUserRole: vi.fn(),
  removeUserRole: vi.fn(),
}));

const mockCanManageUsers = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({
    canManageUsers: mockCanManageUsers,
    canViewBudgets: () => false,
    canViewUsage: () => false,
    canAccessOrg: () => true,
  }),
}));

/** Stubbed: #2984 owns the toggle's behaviour. This file only asks whether it is mounted. */
vi.mock('@/components/org/ApprovalPolicyToggle', () => ({
  ApprovalPolicyToggle: ({ currentPolicy }: { currentPolicy: string }) => (
    <div data-testid="approval-policy-toggle" data-policy={currentPolicy} />
  ),
}));

import { getOrgDashboard } from '@/services/dashboard';
import { getDepartments, getUserRoles, getOrganization, getAvailableRoles } from '@/services/admin';

const mockGetDashboard = getOrgDashboard as ReturnType<typeof vi.fn>;
const mockGetOrganization = getOrganization as ReturnType<typeof vi.fn>;

const DASHBOARD = {
  orgId: 'org-1',
  orgName: 'Acme Corp',
  totalRequests24h: 10,
  totalTokens24h: 100,
  totalCost24h: 1.5,
  activeUsers24h: 2,
  errorRate24h: 0,
  topDepartments: [],
  topModels: [],
};

/** An org as the DEPRECATED route delivers it — `memberApprovalPolicy` present. */
const DEPRECATED_ORG = {
  id: 'org-1',
  name: 'Acme Corp',
  awsAccounts: ['123456789012'],
  roleMappings: { admin: 'arn:aws:iam::123456789012:role/Admin' },
  settings: {},
  memberApprovalPolicy: 'require_admin_approval',
  githubInstallationIds: [],
  createdAt: '2026-01-01T00:00:00Z',
};

/**
 * The same org as the CANONICAL identity route delivers it: no `memberApprovalPolicy` and
 * no `roleMappings`, because `OrganizationResponse` carries neither. Built by OMITTING the
 * keys rather than setting them to `undefined` — the field is genuinely not on the payload.
 */
const CANONICAL_ORG = {
  id: 'org-1',
  name: 'Acme Corp',
  awsAccounts: ['123456789012'],
  settings: {},
  githubInstallationIds: [],
  createdAt: '2026-01-01T00:00:00Z',
};

const renderPage = () =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ToastProvider>
        <MemoryRouter>
          <OrgDashboard />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );

describe('OrgDashboard — member approval policy absence (#4929)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCanManageUsers.mockReturnValue(true);
    mockGetDashboard.mockResolvedValue(DASHBOARD);
    (getDepartments as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [] });
    (getUserRoles as ReturnType<typeof vi.fn>).mockResolvedValue({ items: [] });
    (getAvailableRoles as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  });

  it('states that the policy was not provided when the response does not carry it', async () => {
    mockGetOrganization.mockResolvedValue(CANONICAL_ORG);

    renderPage();

    expect(await screen.findByText(/member approval policy not provided/i)).toBeInTheDocument();
  });

  it('keeps the Organization Settings section visible when the policy is absent', async () => {
    // The regression this issue exists to prevent: the whole section disappearing, so the
    // page reads as though the org simply has no settings rather than as a field we did not
    // receive. A missing heading is the invisible failure.
    mockGetOrganization.mockResolvedValue(CANONICAL_ORG);

    renderPage();

    expect(await screen.findByRole('heading', { name: /organization settings/i })).toBeInTheDocument();
    expect(screen.queryByTestId('approval-policy-toggle')).not.toBeInTheDocument();
  });

  it('renders the real toggle when the policy IS present', async () => {
    // Regression guard on the live path: every current read still uses the deprecated
    // route, which does send the field.
    mockGetOrganization.mockResolvedValue(DEPRECATED_ORG);

    renderPage();

    const toggle = await screen.findByTestId('approval-policy-toggle');
    expect(toggle).toHaveAttribute('data-policy', 'require_admin_approval');
    expect(screen.queryByText(/member approval policy not provided/i)).not.toBeInTheDocument();
  });

  it('shows neither the toggle nor the absence note to a caller who cannot manage users', async () => {
    // The note explains a missing setting; it must not become a way for a caller who could
    // never see or change the policy to learn it exists. Authz is unchanged by #4929.
    mockCanManageUsers.mockReturnValue(false);
    mockGetOrganization.mockResolvedValue(CANONICAL_ORG);

    renderPage();

    await waitFor(() => expect(mockGetDashboard).toHaveBeenCalled());
    expect(screen.queryByRole('heading', { name: /organization settings/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/member approval policy not provided/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-policy-toggle')).not.toBeInTheDocument();
  });
});
