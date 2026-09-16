/**
 * Rate Limit Form Modal
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Modal form for creating and editing rate limit configurations.
 */

import { useState, useEffect } from 'react';
import { Modal, ModalFooter } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { EntitySelector } from '@/components/shared/EntitySelector';
import { useToast } from '@/contexts/ToastContext';
import { createRatelimit, updateRatelimit } from '@/services/ratelimit';
import { EntityType } from '@/types';

interface RateLimitFormData {
  entityType: string;
  entityId: string;
  rpm: number | null;
  tpm: number | null;
  concurrentRequests: number | null;
}

interface RateLimitFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  orgId: string;
  editData?: RateLimitFormData;
}

export function RateLimitFormModal({
  isOpen,
  onClose,
  onSuccess,
  orgId,
  editData,
}: RateLimitFormModalProps) {
  const toast = useToast();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formData, setFormData] = useState<RateLimitFormData>({
    entityType: EntityType.TEAM,
    entityId: '',
    rpm: null,
    tpm: null,
    concurrentRequests: null,
  });
  /**
   * The org partition this rate limit is WRITTEN to — Issue #4948.
   *
   * Not always `orgId`: the entity picker now lists every org the caller administers,
   * and a limit for another org's team must land in THAT org's partition. The limiter
   * loads rows by `(org_id, entity_type, entity_id)`, so posting a `sophos-it` team to
   * the caller's own partition would store a row that reads back as configured and is
   * never matched — the #4511 inert-config class one dimension over.
   *
   * Seeded from and reset to `orgId`, so a single-org admin's behaviour is unchanged.
   */
  const [scopeOrgId, setScopeOrgId] = useState(orgId);

  const isEditMode = !!editData;

  // Reset form when modal opens/closes or editData changes
  useEffect(() => {
    // Reopening the form must not inherit the org picked during the last create
    // (#4948) — that would silently author the next limit in a partition the
    // operator is no longer looking at.
    setScopeOrgId(orgId);
    if (editData) {
      setFormData(editData);
    } else {
      setFormData({
        entityType: EntityType.TEAM,
        entityId: '',
        rpm: null,
        tpm: null,
        concurrentRequests: null,
      });
    }
  }, [editData, isOpen, orgId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!formData.entityId.trim()) {
      toast.error('Entity ID is required');
      return;
    }

    // At least one limit must be set
    if (formData.rpm === null && formData.tpm === null && formData.concurrentRequests === null) {
      toast.error('At least one rate limit (RPM, TPM, or Concurrent) must be set');
      return;
    }

    setIsSubmitting(true);

    try {
      if (isEditMode) {
        await updateRatelimit(orgId, formData.entityType, formData.entityId, {
          rpm: formData.rpm,
          tpm: formData.tpm,
          concurrent_requests: formData.concurrentRequests,
        });
        toast.success('Rate limit updated successfully');
      } else {
        // `scopeOrgId`, NOT `orgId` (#4948): the partition must be the org the entity
        // belongs to, or the row matches nothing at request time.
        await createRatelimit(scopeOrgId, {
          entity_type: formData.entityType,
          entity_id: formData.entityId,
          rpm: formData.rpm,
          tpm: formData.tpm,
          concurrent_requests: formData.concurrentRequests,
        });
        toast.success('Rate limit created successfully');
      }
      onSuccess();
    } catch (error: unknown) {
      const message =
        error instanceof Error
          ? error.message
          : `Failed to ${isEditMode ? 'update' : 'create'} rate limit`;
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

  const parseIntOrNull = (value: string): number | null => {
    const parsed = parseInt(value, 10);
    return isNaN(parsed) ? null : parsed;
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={handleClose}
      title={isEditMode ? 'Edit Rate Limit' : 'Create Rate Limit'}
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
            // Issue #4948: redirect the WRITE to the org the operator picked. Passing
            // this is what makes the org picker appear at all — the component withholds
            // it from consumers that cannot honour it, so the affordance and the
            // correct partition ship together or not at all.
            onScopeOrgChange={setScopeOrgId}
            disabled={false}
          />
        )}

        <Input
          label="Requests Per Minute (RPM)"
          type="number"
          min="0"
          value={formData.rpm ?? ''}
          onChange={(e) => setFormData({ ...formData, rpm: parseIntOrNull(e.target.value) })}
          placeholder="e.g., 60"
        />

        <Input
          label="Tokens Per Minute (TPM)"
          type="number"
          min="0"
          value={formData.tpm ?? ''}
          onChange={(e) => setFormData({ ...formData, tpm: parseIntOrNull(e.target.value) })}
          placeholder="e.g., 100000"
        />

        <Input
          label="Concurrent Requests"
          type="number"
          min="0"
          value={formData.concurrentRequests ?? ''}
          onChange={(e) =>
            setFormData({ ...formData, concurrentRequests: parseIntOrNull(e.target.value) })
          }
          placeholder="e.g., 5"
        />

        <ModalFooter>
          <Button type="button" variant="secondary" onClick={handleClose} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button type="submit" isLoading={isSubmitting}>
            {isEditMode ? 'Save Changes' : 'Create Rate Limit'}
          </Button>
        </ModalFooter>
      </form>
    </Modal>
  );
}
