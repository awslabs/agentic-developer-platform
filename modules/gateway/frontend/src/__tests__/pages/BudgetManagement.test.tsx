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
