/**
 * BudgetFormModal Component Tests
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Tests for the budget form modal create, edit, and validation flows.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BudgetFormModal } from '@/components/budget/BudgetFormModal';
import { ToastProvider } from '@/contexts/ToastContext';
import { EntityType, PeriodType, EnforcementMode } from '@/types';

// Mock the budget service
vi.mock('@/services/budget', () => ({
  createBudget: vi.fn(),
  updateBudget: vi.fn(),
}));

// Mock the admin service for entity fetching - return empty results to force manual input.
// The names here are the ones EntitySelector actually imports; `getDepartments`/`getTeams`
// were not, so the fetch used to throw and reach manual input via the error path.
// Issue #4948: the entity pickers read the platform's tenancy tables, not Cognito
// groups. Empty pages here on purpose — this suite is about the person-scoped paths
// (#4687), and an entity list it never asserts on should not be able to fail it.
vi.mock('@/services/admin', () => ({
  getOrganizations: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getDepartments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getOrgTeams: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  // Issue #4687: the person picker, and the lookup that turns the member it emits into
  // a person anchor. `getMemberGithubUserId` is the ONLY source of the GitHub numeric
  // id the anchor is built from — mocked here so a test can assert the form asked for
  // it rather than derived a key from the member id it already had.
  getOrgUsers: vi.fn(),
  getMemberGithubUserId: vi.fn(),
}));

vi.mock('@/services/personCap', () => ({
  setPersonCapFor: vi.fn(),
}));

import { createBudget, updateBudget } from '@/services/budget';
import { getOrgUsers, getMemberGithubUserId, getOrganizations, getOrgTeams } from '@/services/admin';
import { setPersonCapFor } from '@/services/personCap';
import { PERSON_LIMIT_LABEL } from '@/utils/entityLabels';

const mockCreateBudget = createBudget as ReturnType<typeof vi.fn>;
const mockUpdateBudget = updateBudget as ReturnType<typeof vi.fn>;
const mockGetOrgUsers = getOrgUsers as ReturnType<typeof vi.fn>;
const mockGetMemberGithubUserId = getMemberGithubUserId as ReturnType<typeof vi.fn>;
const mockSetPersonCapFor = setPersonCapFor as ReturnType<typeof vi.fn>;
const mockGetOrganizations = getOrganizations as ReturnType<typeof vi.fn>;
const mockGetOrgTeams = getOrgTeams as ReturnType<typeof vi.fn>;

const renderComponent = (props: Partial<Parameters<typeof BudgetFormModal>[0]> = {}) => {
  const defaultProps = {
    isOpen: true,
    onClose: vi.fn(),
    onSuccess: vi.fn(),
    orgId: 'org-001',
  };

  return render(
    <ToastProvider>
      <BudgetFormModal {...defaultProps} {...props} />
    </ToastProvider>
  );
};

describe('BudgetFormModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockCreateBudget.mockResolvedValue({
      id: 'budget-new',
      entityType: EntityType.TEAM,
      entityId: 'team-001',
      periodType: PeriodType.MONTHLY,
      budgetAmountUsd: 500,
      enforcementMode: EnforcementMode.HARD,
      orgId: 'org-001',
      updatedAt: new Date().toISOString(),
    });
    mockUpdateBudget.mockResolvedValue({
      id: 'budget-001',
      entityType: EntityType.TEAM,
      entityId: 'team-001',
      periodType: PeriodType.MONTHLY,
      budgetAmountUsd: 1000,
      enforcementMode: EnforcementMode.SOFT,
      orgId: 'org-001',
      updatedAt: new Date().toISOString(),
    });
    // The picker's list, shared by both person-scoped kinds and the person limit.
    mockGetOrgUsers.mockResolvedValue({
      items: [
        {
          id: 'user-operator',
          email: 'operator@test.com',
          name: 'Operator',
          cognitoSub: '8a41f2c0-1b7d-4e5a-9c33-000000000001',
          role: 'member',
        },
      ],
      total: 1,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetMemberGithubUserId.mockResolvedValue('20402445');
    mockSetPersonCapFor.mockResolvedValue({
      person_anchor: 'github:20402445',
      period_type: 'monthly',
      cap_usd: '500.00',
      enforcement_mode: 'hard',
    });
  });

  describe('Modal Rendering', () => {
    it('renders create modal when no editData provided', async () => {
      renderComponent();

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check modal title using heading
      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: /create budget/i })).toBeInTheDocument();
    });

    it('renders edit modal when editData provided', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const dialog = screen.getByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: /edit budget/i })).toBeInTheDocument();
    });

    it('does not render when closed', () => {
      renderComponent({ isOpen: false });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  describe('Form Fields', () => {
    it('shows form labels in create mode', async () => {
      renderComponent();

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check for form element labels by text
      expect(screen.getByText('Entity Type')).toBeInTheDocument();
      expect(screen.getByText('Budget Amount (USD)')).toBeInTheDocument();
      expect(screen.getByText('Period Type')).toBeInTheDocument();
      expect(screen.getByText('Enforcement Mode')).toBeInTheDocument();
    });

    it('shows entity values as read-only text in edit mode', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // In edit mode, entity type and ID are shown as static text. Issue #4536:
      // the type renders as its friendly label rather than the raw wire value —
      // the read-only edit view is otherwise where `root_user` would leak.
      expect(screen.getByText('Team')).toBeInTheDocument();
      expect(screen.getByText('team-001')).toBeInTheDocument();
    });

    // Issue #4536: the concept must never be named by its schema value. This is
    // the surface that renders `entityType` directly, so it is the one that would
    // print `root_user` if the label lookup were dropped.
    it('names a cloud-agent budget in plain language when editing', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.ROOT_USER,
          entityId: 'user-operator',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // #4687 added the scope qualifier to the same label.
      expect(screen.getByText('User — cloud agents (this GitHub org)')).toBeInTheDocument();
      expect(screen.getByRole('dialog').textContent).not.toContain('root_user');
    });
  });

  describe('Edit Flow', () => {
    it('pre-fills form with edit data', async () => {
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check that amount is pre-filled
      expect(screen.getByDisplayValue('500')).toBeInTheDocument();
    });

    it('submits edit form with updated data', async () => {
      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({
        onSuccess,
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Update budget amount
      const amountInput = screen.getByDisplayValue('500');
      await user.clear(amountInput);
      await user.type(amountInput, '1000');

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(mockUpdateBudget).toHaveBeenCalledWith(
          'org-001',
          EntityType.TEAM,
          'team-001',
          expect.objectContaining({
            budget_amount_usd: 1000,
          })
        );
      });
    });

    it('calls onSuccess after successful edit', async () => {
      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({
        onSuccess,
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(onSuccess).toHaveBeenCalled();
      });
    });
  });

  describe('Modal Actions', () => {
    it('calls onClose when cancel button is clicked', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      renderComponent({ onClose });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const cancelButton = screen.getByRole('button', { name: /cancel/i });
      await user.click(cancelButton);

      expect(onClose).toHaveBeenCalled();
    });
  });

  // Issue #4687: authoring a cross-workspace person limit from this form. Two distinct
  // guarantees are pinned here — the option is platform-admin-only, and the anchor it
  // writes under comes from the server for the person who was picked, never from
  // anything the client could have inferred or a human could have typed (#4511).
  describe('Person limit (#4687)', () => {
    /** Select the person-limit kind, then the one member in the picker. */
    const chooseP = async (user: ReturnType<typeof userEvent.setup>) => {
      const selects = screen.getAllByRole('combobox');
      await user.selectOptions(selects[0], PERSON_LIMIT_LABEL);
      await waitFor(() =>
        expect(screen.getByRole('option', { name: /Operator/ })).toBeInTheDocument()
      );
      await user.selectOptions(screen.getAllByRole('combobox')[1], 'user-operator');
    };

    it('does not offer the person limit to a non-platform admin', async () => {
      // The ruling on #4620 §4.2: an org admin may never author a person's
      // cross-workspace limit, because it governs spend in tenants they have no
      // membership in. The route 403s them regardless; this keeps them out of it.
      renderComponent({ isPlatformAdmin: false });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      expect(
        screen.queryByRole('option', { name: PERSON_LIMIT_LABEL })
      ).not.toBeInTheDocument();
    });

    it('offers the person limit to a platform admin', async () => {
      renderComponent({ isPlatformAdmin: true });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      expect(screen.getByRole('option', { name: PERSON_LIMIT_LABEL })).toBeInTheDocument();
    });

    it('writes the cap under the anchor resolved for the person picked', async () => {
      const user = userEvent.setup();
      renderComponent({ isPlatformAdmin: true });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await chooseP(user);

      await user.type(screen.getByRole('spinbutton'), '500');
      await user.click(screen.getByRole('button', { name: /set person limit/i }));

      await waitFor(() => {
        // Asked the server for the picked member's GitHub id...
        expect(mockGetMemberGithubUserId).toHaveBeenCalledWith('user-operator');
        // ...and keyed the cap on it. `github:<numeric id>` is the cross-org,
        // partition-free join key; a `users.id` here would be a per-tenant value the
        // person-cap enforcement never reads.
        expect(mockSetPersonCapFor).toHaveBeenCalledWith('github:20402445', 'monthly', '500.00');
      });
      // Emphatically not the budget-config path: a person limit is not an entity type.
      expect(mockCreateBudget).not.toHaveBeenCalled();
    });

    it('never sends a client-derived key as the anchor', async () => {
      const user = userEvent.setup();
      renderComponent({ isPlatformAdmin: true });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await chooseP(user);

      await user.type(screen.getByRole('spinbutton'), '500');
      await user.click(screen.getByRole('button', { name: /set person limit/i }));

      await waitFor(() => expect(mockSetPersonCapFor).toHaveBeenCalled());
      const [anchor] = mockSetPersonCapFor.mock.calls[0];
      // The three things a client could have reached for instead of asking the server —
      // each yields a row that validates, displays a number, and governs nothing.
      expect(anchor).not.toContain('user-operator');
      expect(anchor).not.toContain('operator@test.com');
      expect(anchor).not.toContain('8a41f2c0-1b7d-4e5a-9c33-000000000001');
    });

    it('writes nothing for a member with no linked GitHub identity', async () => {
      // A legitimate permanent state for someone who signed up by email: there is no
      // cross-workspace key, so any cap authored would be inert. Declining to write is
      // the correct outcome, and it is explained rather than reported as a failure.
      mockGetMemberGithubUserId.mockResolvedValue(null);

      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({ isPlatformAdmin: true, onSuccess });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await chooseP(user);

      await user.type(screen.getByRole('spinbutton'), '500');
      await user.click(screen.getByRole('button', { name: /set person limit/i }));

      await waitFor(() =>
        expect(screen.getByText(/no linked GitHub identity/i)).toBeInTheDocument()
      );
      expect(mockSetPersonCapFor).not.toHaveBeenCalled();
      expect(onSuccess).not.toHaveBeenCalled();
    });

    it('offers no enforcement-mode choice, and says the limit is enforced', async () => {
      // The route writes `hard` on every PUT (#4630). Offering the control would let an
      // admin believe they authored a soft limit and get an enforcing one — the
      // screen/behavior disagreement #4620 exists to close.
      const user = userEvent.setup();
      renderComponent({ isPlatformAdmin: true });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[0], PERSON_LIMIT_LABEL);

      expect(screen.queryByText('Enforcement Mode')).not.toBeInTheDocument();
      expect(screen.getByTestId('person-limit-enforcement-note')).toBeInTheDocument();
    });
  });

  // Issue #4687 surfacing the #4669 advisory: a sentence about a cap that WAS created.
  describe('Create advisory (#4669 / #4687)', () => {
    it('hands the advisory to the caller without disturbing the create', async () => {
      mockCreateBudget.mockResolvedValue({
        id: 'budget-new',
        entityType: EntityType.ROOT_USER,
        entityId: 'user-operator',
        periodType: PeriodType.MONTHLY,
        budgetAmountUsd: 500,
        enforcementMode: EnforcementMode.HARD,
        orgId: 'org-001',
        updatedAt: new Date().toISOString(),
        advisory: "This person's agent spend currently accrues in 2 other workspaces.",
      });

      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({ onSuccess });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await user.type(screen.getByRole('textbox'), 'team-001');
      await user.type(screen.getByRole('spinbutton'), '500');
      await user.click(screen.getByRole('button', { name: /create budget/i }));

      await waitFor(() => {
        // Success, with the advisory as a payload rather than an error — the caller
        // decides how to show it, and the row is already committed by the time this
        // fires. Nothing here can undo or retract the create.
        expect(onSuccess).toHaveBeenCalledWith({
          advisory: "This person's agent spend currently accrues in 2 other workspaces.",
          // The subject rides along so the page's advisory redirect can preselect
          // the same person (review fix on #4688).
          entityId: expect.any(String),
        });
      });
    });

    it('reports no advisory as null rather than omitting it', async () => {
      // The common case: a cap whose spend does accrue here. An explicit null keeps the
      // caller from having to distinguish "no advisory" from "older response shape".
      const user = userEvent.setup();
      const onSuccess = vi.fn();
      renderComponent({ onSuccess });

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());
      await user.type(screen.getByRole('textbox'), 'team-001');
      await user.type(screen.getByRole('spinbutton'), '500');
      await user.click(screen.getByRole('button', { name: /create budget/i }));

      await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ advisory: null, entityId: expect.any(String) }));
    });
  });

  /**
   * Issue #4948. The entity picker lists every org the caller administers, so the write
   * partition is now a choice rather than a coincidence.
   *
   * `_check_entity_budget` matches BOTH `BudgetConfig.org_id` (filled from the request's
   * `attributed_org_id`) and `BudgetConfig.entity_id`. Before this issue the picker could
   * only offer the caller's own org, so the two always agreed by accident. Offering the
   * real list breaks that: a `sophos-it` team posted to the caller's own partition stores
   * a row that reads back as a configured cap and is never matched at request time. The
   * operator sees a budget; the spend never stops. Asserted at the wire, not just at the
   * callback, because the callback firing proves nothing about where the POST went.
   */
  describe('Write partition follows the picked org (#4948)', () => {
    beforeEach(() => {
      mockGetOrganizations.mockResolvedValue({
        items: [
          { id: 'org-001', name: 'Acme Corp' },
          { id: 'sophos-it', name: 'Sophos IT' },
        ],
        total: 2,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
      mockGetOrgTeams.mockResolvedValue({
        items: [{ id: 'team-sophos-1', name: 'Helpdesk', departmentId: 'dept-1' }],
        total: 1,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });
      mockCreateBudget.mockResolvedValue({
        entityType: EntityType.TEAM,
        entityId: 'team-sophos-1',
        periodType: PeriodType.MONTHLY,
        budgetAmountUsd: 500,
        enforcementMode: EnforcementMode.HARD,
        advisory: null,
      });
    });

    it('posts to the picked org, not the caller\'s own', async () => {
      const user = userEvent.setup();
      renderComponent();

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);

      await waitFor(() => expect(screen.getByRole('option', { name: /Sophos IT/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[1], 'sophos-it');

      await waitFor(() => expect(screen.getByRole('option', { name: /Helpdesk/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');

      await user.type(screen.getByPlaceholderText('e.g., 500.00'), '500');
      await user.click(screen.getByRole('button', { name: /create budget/i }));

      await waitFor(() => {
        expect(mockCreateBudget).toHaveBeenCalledWith(
          'sophos-it',
          expect.objectContaining({ entity_type: EntityType.TEAM, entity_id: 'team-sophos-1' }),
        );
      });
      expect(mockCreateBudget).not.toHaveBeenCalledWith('org-001', expect.anything());
    });

    it.each([EntityType.USER, EntityType.ROOT_USER])(
      'returns to the person roster org when switching from a foreign team to %s',
      async (entityType) => {
        const user = userEvent.setup();
        renderComponent();
        await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
        await screen.findByRole('option', { name: /Sophos IT/ });
        await user.selectOptions(screen.getAllByRole('combobox')[1], 'sophos-it');
        await screen.findByRole('option', { name: /Helpdesk/ });
        await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');

        await user.selectOptions(screen.getAllByRole('combobox')[0], entityType);
        const person = await screen.findByRole('option', { name: /Operator/ });
        await user.selectOptions(screen.getAllByRole('combobox')[1], person);
        await user.type(screen.getByPlaceholderText('e.g., 500.00'), '500');
        await user.click(screen.getByRole('button', { name: /create budget/i }));

        await waitFor(() => expect(mockCreateBudget).toHaveBeenCalledWith(
          'org-001',
          expect.objectContaining({
            entity_type: entityType,
            entity_id: entityType === EntityType.USER
              ? '8a41f2c0-1b7d-4e5a-9c33-000000000001' : 'user-operator',
          }),
        ));
      },
    );

    it('leaves a single-org admin posting to their own org', async () => {
      // Nobody who never touches the org picker should see any change in where their
      // budget lands.
      const user = userEvent.setup();
      renderComponent();

      await waitFor(() => expect(screen.getByRole('dialog')).toBeInTheDocument());

      await user.selectOptions(screen.getAllByRole('combobox')[0], EntityType.TEAM);
      await waitFor(() => expect(screen.getByRole('option', { name: /Helpdesk/ })).toBeInTheDocument());
      await user.selectOptions(screen.getAllByRole('combobox')[2], 'team-sophos-1');

      await user.type(screen.getByPlaceholderText('e.g., 500.00'), '500');
      await user.click(screen.getByRole('button', { name: /create budget/i }));

      await waitFor(() => {
        expect(mockCreateBudget).toHaveBeenCalledWith('org-001', expect.anything());
      });
    });
  });

  describe('Error Handling', () => {
    it('handles update failure gracefully', async () => {
      mockUpdateBudget.mockRejectedValue(new Error('Update failed'));

      const user = userEvent.setup();
      renderComponent({
        editData: {
          entityType: EntityType.TEAM,
          entityId: 'team-001',
          periodType: PeriodType.MONTHLY,
          budgetAmountUsd: 500,
          enforcementMode: EnforcementMode.HARD,
        },
      });

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Submit the form
      const submitButton = screen.getByRole('button', { name: /save changes/i });
      await user.click(submitButton);

      // Wait for the error to be handled
      await waitFor(() => {
        expect(mockUpdateBudget).toHaveBeenCalled();
      });
    });
  });
});
