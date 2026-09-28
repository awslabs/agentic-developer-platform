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

// Mock the admin service for entity fetching - return empty results to force manual input
vi.mock('@/services/admin', () => ({
  getDepartments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
  getTeams: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    pageSize: 100,
    hasMore: false,
  }),
}));

import { createBudget, updateBudget } from '@/services/budget';

const mockCreateBudget = createBudget as ReturnType<typeof vi.fn>;
const mockUpdateBudget = updateBudget as ReturnType<typeof vi.fn>;

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

      // In edit mode, entity type and ID are shown as static text
      expect(screen.getByText('team')).toBeInTheDocument();
      expect(screen.getByText('team-001')).toBeInTheDocument();
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
