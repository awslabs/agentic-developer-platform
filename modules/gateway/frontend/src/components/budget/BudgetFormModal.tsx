/**
 * Budget Form Modal
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Modal form for creating and editing budget configurations.
 */

import { useState, useEffect } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { EntitySelector } from '@/components/shared/EntitySelector';
import { useToast } from '@/contexts/ToastContext';
import { createBudget, updateBudget } from '@/services/budget';
import { EntityType, PeriodType, EnforcementMode } from '@/types';

interface BudgetFormData {
  entityType: string;
  entityId: string;
  periodType: string;
  budgetAmountUsd: number;
  enforcementMode: string;
}

interface BudgetFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  orgId: string;
  editData?: BudgetFormData;
}

const periodTypeOptions = [
  { value: PeriodType.DAILY, label: 'Daily' },
  { value: PeriodType.WEEKLY, label: 'Weekly' },
  { value: PeriodType.MONTHLY, label: 'Monthly' },
];

const enforcementModeOptions = [
  { value: EnforcementMode.HARD, label: 'Hard (Block requests when exceeded)' },
  { value: EnforcementMode.SOFT, label: 'Soft (Warn but allow requests)' },
];

export function BudgetFormModal({
  isOpen,
  onClose,
  onSuccess,
  orgId,
  editData,
}: BudgetFormModalProps) {
  const toast = useToast();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formData, setFormData] = useState<BudgetFormData>({
    entityType: EntityType.TEAM,
    entityId: '',
    periodType: PeriodType.MONTHLY,
    budgetAmountUsd: 0,
    enforcementMode: EnforcementMode.HARD,
  });

  const isEditMode = !!editData;

  // Reset form when modal opens/closes or editData changes
  useEffect(() => {
    if (editData) {
      setFormData(editData);
    } else {
      setFormData({
        entityType: EntityType.TEAM,
        entityId: '',
        periodType: PeriodType.MONTHLY,
        budgetAmountUsd: 0,
        enforcementMode: EnforcementMode.HARD,
      });
    }
  }, [editData, isOpen]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.entityId.trim()) {
      toast.error('Entity ID is required');
      return;
    }

    if (formData.budgetAmountUsd <= 0) {
      toast.error('Budget amount must be greater than 0');
      return;
    }

    setIsSubmitting(true);

    try {
      if (isEditMode) {
        // For edit, we use the PUT endpoint for budget config by entity
        await updateBudget(
          orgId,
          formData.entityType,
          formData.entityId,
          {
            budget_amount_usd: formData.budgetAmountUsd,
            enforcement_mode: formData.enforcementMode as EnforcementMode,
          }
        );
        toast.success('Budget updated successfully');
      } else {
        await createBudget(orgId, {
          entity_type: formData.entityType as EntityType,
          entity_id: formData.entityId,
          period_type: formData.periodType as PeriodType,
          budget_amount_usd: formData.budgetAmountUsd,
          enforcement_mode: formData.enforcementMode as EnforcementMode,
        });
        toast.success('Budget created successfully');
      }
      onSuccess();
    } catch (error: unknown) {
      const message =
        error instanceof Error ? error.message : `Failed to ${isEditMode ? 'update' : 'create'} budget`;
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleClose = () => {
    if (!isSubmitting) {
      onClose();
    }
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title={isEditMode ? 'Edit Budget' : 'Create Budget'}
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {isEditMode ? (
          <>
            {/* In edit mode, show read-only entity info */}
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Entity Type
              </label>
              <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                {formData.entityType}
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Entity ID
              </label>
              <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300">
                {formData.entityId}
              </div>
            </div>
          </>
        ) : (
          <EntitySelector
            orgId={orgId}
            entityType={formData.entityType}
            entityId={formData.entityId}
            onEntityTypeChange={(entityType) => setFormData((prev) => ({ ...prev, entityType, entityId: '' }))}
            onEntityIdChange={(entityId) => setFormData((prev) => ({ ...prev, entityId }))}
            disabled={false}
          />
        )}

        <Select
          label="Period Type"
          options={periodTypeOptions}
          value={formData.periodType}
          onChange={(e) => setFormData({ ...formData, periodType: e.target.value })}
          disabled={isEditMode}
          required
        />

        <Input
          label="Budget Amount (USD)"
          type="number"
          min="0.01"
          step="0.01"
          value={formData.budgetAmountUsd || ''}
          onChange={(e) =>
            setFormData({ ...formData, budgetAmountUsd: parseFloat(e.target.value) || 0 })
          }
          placeholder="e.g., 500.00"
          required
        />

        <Select
          label="Enforcement Mode"
          options={enforcementModeOptions}
          value={formData.enforcementMode}
          onChange={(e) => setFormData({ ...formData, enforcementMode: e.target.value })}
          required
        />

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={handleClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button type="submit" isLoading={isSubmitting}>
            {isEditMode ? 'Save Changes' : 'Create Budget'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
