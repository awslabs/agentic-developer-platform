vi.mock('@/components/budget/BudgetEnforcementControl', () => ({ BudgetEnforcementControl: () => <div data-testid="budget-enforcement-control" /> }));
/**
 * BudgetManagement page tests — Issue #4687.
 *
 * Scoped to what #4687 added to this page: surfacing the #4669 `advisory` for a cap that
 * was just created, and routing the admin from it to the control that would actually
 * bound the person. The advisory is the mechanism by which a workspace-scoped cap stops
 * masquerading as a global one AFTER it has been authored, so the properties that matter
 * are (a) it is shown, (b) it never reads as a failure or undoes the create, and (c) the
 * redirect it offers exists only for the party permitted to take it (#4620 §4.2).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BudgetManagement } from '@/pages/BudgetManagement';
import { ToastProvider } from '@/contexts/ToastContext';

vi.mock('@/services/budget', () => ({
  getBudgetsWithUtilization: vi.fn(),
  deleteBudgetByEntity: vi.fn(),
}));

vi.mock('@/contexts/AuthContext', () => ({
  useAuthContext: () => ({ user: { orgId: 'org-001' } }),
}));

const mockIsPlatformAdmin = vi.fn();
vi.mock('@/hooks/usePermissions', () => ({
  usePermissions: () => ({ isPlatformAdmin: mockIsPlatformAdmin }),
}));

/**
 * The create modal, stubbed to a single button that fires `onSuccess` with an advisory.
 *
 * The form's own behavior is covered in BudgetFormModal.test.tsx; what this file needs is
 * control over the one input the page reacts to. Stubbing it also makes the contract
 * explicit: the page learns about the advisory from a SUCCESS callback, so a page that
 * treated it as an error would have to do so against a signal that says otherwise.
 */
const mockAdvisory = vi.fn();
vi.mock('@/components/budget/BudgetFormModal', () => ({
  BudgetFormModal: ({
    isOpen,
    onSuccess,
    isPlatformAdmin,
  }: {
    isOpen: boolean;
    onSuccess: (result?: { advisory: string | null }) => void;
    isPlatformAdmin?: boolean;
  }) =>
    isOpen ? (
      <div data-testid="budget-form-modal" data-platform-admin={String(!!isPlatformAdmin)}>
        <button type="button" onClick={() => onSuccess({ advisory: mockAdvisory() })}>
          stub-create
        </button>
      </div>
    ) : null,
}));

/**
 * The #4691 defaults panel, stubbed to a marker.
 *
 * Its own behavior — scope encoding, the ladder copy, the three read states — is
 * covered in DefaultPersonLimits.test.tsx. What this file needs is only whether the
 * page MOUNTS it, so a stub keeps these tests off the panel's network calls (it reads
 * three scopes on mount) and makes the gating assertions below unambiguous.
 */
vi.mock('@/components/budget/DefaultPersonLimits', () => ({
  DefaultPersonLimits: () => <div data-testid="default-person-limits" />,
}));

/**
 * The #4745 routing panel, stubbed for the same reason: its behaviour is covered in
 * BedrockAccountRouting.test.tsx, and unstubbed it fetches mappings, destinations and
 * organizations on mount.
 */
vi.mock('@/components/bedrock/BedrockAccountRouting', () => ({
  BedrockAccountRouting: () => <div data-testid="bedrock-account-routing" />,
}));

import { getBudgetsWithUtilization } from '@/services/budget';

const mockGetBudgets = getBudgetsWithUtilization as ReturnType<typeof vi.fn>;

const ADVISORY =
  "This person's agent spend currently accrues in 2 other workspaces, not here. " +
  'This cap governs only the spend that bills to this workspace, so it may never be reached.';

const renderPage = () =>
  render(
    <ToastProvider>
      <BudgetManagement />
    </ToastProvider>
  );

/** Open the create modal and complete a create that returns `advisory`. */
async function createWithAdvisory(
  user: ReturnType<typeof userEvent.setup>,
  advisory: string | null
) {
  mockAdvisory.mockReturnValue(advisory);
  await user.click(screen.getByRole('button', { name: /add budget/i }));
  await user.click(await screen.findByRole('button', { name: 'stub-create' }));
}

describe('BudgetManagement — create advisory (#4687)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsPlatformAdmin.mockReturnValue(false);
    mockGetBudgets.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20, hasMore: false });
  });

  it('shows the advisory for the cap that was just created', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await createWithAdvisory(user, ADVISORY);

    expect(await screen.findByText(ADVISORY)).toBeInTheDocument();
  });

  it('shows nothing when there is no advisory', async () => {
    // The common case. A notice on every create would train admins to dismiss it
    // unread, which costs the one create where it matters.
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await createWithAdvisory(user, null);

    await waitFor(() =>
      expect(screen.queryByTestId('budget-form-modal')).not.toBeInTheDocument()
    );
    expect(screen.queryByText(/may never be reached/i)).not.toBeInTheDocument();
  });

  it('closes the modal and refreshes the list — the create is not held open on it', async () => {
    // The non-negotiable from the issue: the advisory must not block or undo the create.
    // A cross-tenant read cannot be allowed to veto an in-tenant write, so the cap is
    // committed and the flow completes exactly as it would without an advisory.
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalledTimes(1));

    await createWithAdvisory(user, ADVISORY);

    await waitFor(() =>
      expect(screen.queryByTestId('budget-form-modal')).not.toBeInTheDocument()
    );
    expect(mockGetBudgets).toHaveBeenCalledTimes(2);
  });

  it('can be dismissed, leaving the cap in place', async () => {
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await createWithAdvisory(user, ADVISORY);
    await screen.findByText(ADVISORY);

    const callsBefore = mockGetBudgets.mock.calls.length;
    await user.click(screen.getByRole('button', { name: /dismiss/i }));

    await waitFor(() => expect(screen.queryByText(ADVISORY)).not.toBeInTheDocument());
    // Dismissing is a UI act with no side effect on the row it described.
    expect(mockGetBudgets).toHaveBeenCalledTimes(callsBefore);
  });

  it('offers a platform admin the redirect to a person limit', async () => {
    mockIsPlatformAdmin.mockReturnValue(true);

    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await createWithAdvisory(user, ADVISORY);

    const redirect = await screen.findByRole('button', { name: /set a person limit instead/i });
    await user.click(redirect);

    // Straight back into authoring, with the notice cleared so it cannot sit beside a
    // different attempt.
    expect(await screen.findByTestId('budget-form-modal')).toBeInTheDocument();
    expect(screen.queryByText(ADVISORY)).not.toBeInTheDocument();
  });

  it('offers an org admin no redirect, and names who can act', async () => {
    // #4620 §4.2: an org admin may never author a person's cross-workspace limit. An
    // affordance that 403s is worse than none, so this path explains instead.
    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await createWithAdvisory(user, ADVISORY);

    await screen.findByText(ADVISORY);
    expect(
      screen.queryByRole('button', { name: /set a person limit instead/i })
    ).not.toBeInTheDocument();
    expect(screen.getByText(/only be set\s+by a platform admin/i)).toBeInTheDocument();
  });

  it('passes the caller platform-admin flag down to the create form', async () => {
    // The form gates the person-limit option on it; the route's `require_platform_admin`
    // remains the actual boundary.
    mockIsPlatformAdmin.mockReturnValue(true);

    const user = userEvent.setup();
    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    await user.click(screen.getByRole('button', { name: /add budget/i }));

    expect(await screen.findByTestId('budget-form-modal')).toHaveAttribute(
      'data-platform-admin',
      'true'
    );
  });
});

/**
 * The #4691 gate.
 *
 * Default person limits govern a POPULATION's spend across every GitHub org, and the
 * ruling on #4620 §4.2 puts anything person-level in platform-admin hands only — an
 * org admin may not author them even for their own members, because the rules are
 * partition-free. The panel is hidden rather than shown-and-403ing: an affordance that
 * fails is worse than none, since the operator has no way to interpret the error.
 *
 * As everywhere else on this page, hiding it is NOT the boundary —
 * `require_platform_admin` on every `/budget/person-default/*` route is.
 */
describe('BudgetManagement — default person limits panel (#4691)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetBudgets.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20, hasMore: false });
  });

  it('mounts the defaults panel for a platform admin', async () => {
    mockIsPlatformAdmin.mockReturnValue(true);

    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    expect(screen.getByTestId('default-person-limits')).toBeInTheDocument();
  });

  it('hides the defaults panel from a non-platform-admin', async () => {
    mockIsPlatformAdmin.mockReturnValue(false);

    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    expect(screen.queryByTestId('default-person-limits')).not.toBeInTheDocument();
  });
});

/**
 * The Bedrock account-routing panel (#4745, #4692 · R4 §6.3).
 *
 * Same gate, same reasoning as the defaults panel above, and the stakes are if anything
 * higher: these rules decide whose AWS account is BILLED for a population's model calls,
 * and they are partition-free — an org id is a scope value, not a boundary, so an org
 * admin could name another tenant's destination. Hence `require_platform_admin` on every
 * `/admin/bedrock-routing/*` route (design ruling 4, §6.5), which is the actual boundary;
 * hiding the panel only keeps an org admin out of a 403 they could not interpret.
 */
describe('BudgetManagement — Bedrock account routing panel (#4745)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetBudgets.mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20, hasMore: false });
  });

  it('mounts the routing panel for a platform admin', async () => {
    mockIsPlatformAdmin.mockReturnValue(true);

    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    expect(screen.getByTestId('bedrock-account-routing')).toBeInTheDocument();
  });

  it('hides the routing panel from a non-platform-admin', async () => {
    mockIsPlatformAdmin.mockReturnValue(false);

    renderPage();
    await waitFor(() => expect(mockGetBudgets).toHaveBeenCalled());

    expect(screen.queryByTestId('bedrock-account-routing')).not.toBeInTheDocument();
  });
});

/**
 * Issue #4948 — legacy configs whose entity id resolves to nothing are FLAGGED, not
 * hidden.
 *
 * The pickers that could only offer Cognito group names have been storing team and
 * department budgets under ids enforcement can never match. Those rows still exist. The
 * tempting cleanup is to drop them from the list, and it is the wrong one: the row is a
 * control an operator believes is in force. Hiding it leaves them believing it while
 * removing the only screen where they could have found out otherwise — #4511 with the
 * evidence deleted. The server decides resolvability (`entity_unresolved`); this page's
 * job is to render the row and say so.
 */
describe('BudgetManagement — unresolved entity flagging (#4948)', () => {
  const legacyRow = {
    entityType: 'team',
    entityId: 'Backend',
    entityDisplayName: null,
    entityUnresolved: true,
    periodType: 'monthly',
    budgetAmountUsd: 500,
    currentUsageUsd: 0,
    utilizationPct: 0,
    enforcementMode: 'hard',
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockIsPlatformAdmin.mockReturnValue(false);
  });

  it('keeps an unresolvable config in the table', async () => {
    mockGetBudgets.mockResolvedValue({ items: [legacyRow], total: 1, page: 1, pageSize: 20, hasMore: false });

    renderPage();

    expect(await screen.findByText('Backend')).toBeInTheDocument();
  });

  it('marks it as not enforced', async () => {
    mockGetBudgets.mockResolvedValue({ items: [legacyRow], total: 1, page: 1, pageSize: 20, hasMore: false });

    renderPage();

    const badge = await screen.findByTestId('entity-unresolved-badge');
    // Names the consequence, not the mechanism: "no matching team" is what the operator
    // has to act on. A bare "invalid" would not tell them the cap is doing nothing.
    expect(badge).toHaveTextContent(/not enforced/i);
    expect(badge).toHaveTextContent(/team/i);
  });

  it('leaves a resolvable config unmarked', async () => {
    mockGetBudgets.mockResolvedValue({
      items: [{ ...legacyRow, entityId: 'team-001', entityDisplayName: 'Backend', entityUnresolved: false }],
      total: 1,
      page: 1,
      pageSize: 20,
      hasMore: false,
    });

    renderPage();

    await screen.findByText('team-001');
    expect(screen.queryByTestId('entity-unresolved-badge')).not.toBeInTheDocument();
  });

  it('never marks a person-scoped config, whose keys this issue does not touch', async () => {
    // `user` and `root_user` are keyed by the Cognito sub and the person anchor
    // (#4511/#4536/#4687) — namespaces the tenancy tables know nothing about. Resolving
    // them against `teams`/`departments` would flag every healthy person budget.
    mockGetBudgets.mockResolvedValue({
      items: [{ ...legacyRow, entityType: 'user', entityId: 'sub-abc-123', entityUnresolved: false }],
      total: 1,
      page: 1,
      pageSize: 20,
      hasMore: false,
    });

    renderPage();

    await screen.findByText('sub-abc-123');
    expect(screen.queryByTestId('entity-unresolved-badge')).not.toBeInTheDocument();
  });
});
