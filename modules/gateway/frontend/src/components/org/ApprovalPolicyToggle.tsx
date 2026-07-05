/**
 * ApprovalPolicyToggle — org-admin toggle for the member approval policy.
 *
 * Issue #2984: Auto-join default ON with org-admin toggle.
 *
 * Controls whether new GitHub org members are auto-approved or require
 * explicit admin approval before accessing the platform.
 */

import { useState } from 'react';
import { useToast } from '@/contexts/ToastContext';
import { apiClient } from '@/services/api';

export type ApprovalPolicy = 'auto_approve_org_members' | 'require_admin_approval';

interface ApprovalPolicyToggleProps {
  orgId: string;
  currentPolicy: ApprovalPolicy;
  /** Whether the current user can change this setting */
  canManage: boolean;
}

export function ApprovalPolicyToggle({ orgId, currentPolicy, canManage }: ApprovalPolicyToggleProps) {
  const [policy, setPolicy] = useState<ApprovalPolicy>(currentPolicy);
  const [isSaving, setIsSaving] = useState(false);
  const toast = useToast();

  const isAutoApprove = policy === 'auto_approve_org_members';

  const handleToggle = async () => {
    if (!canManage) return;

    const newPolicy: ApprovalPolicy = isAutoApprove
      ? 'require_admin_approval'
      : 'auto_approve_org_members';

    setIsSaving(true);
    try {
      await apiClient.put(`/admin/organizations/${orgId}`, {
        member_approval_policy: newPolicy,
      });
      setPolicy(newPolicy);
      toast.success(
        newPolicy === 'auto_approve_org_members'
          ? 'Auto-join enabled — org members will be approved automatically.'
          : 'Admin approval required — new members will need manual approval.',
      );
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to update approval policy';
      toast.error(message);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
      <div className="flex items-center justify-between">
        <div>
          <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100">
            Auto-join for org members
          </h4>
          <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
            {isAutoApprove
              ? 'GitHub org members are automatically approved when they sign in.'
              : 'New members require admin approval before accessing the platform.'}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={isAutoApprove}
          disabled={!canManage || isSaving}
          onClick={handleToggle}
          className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 ${
            isAutoApprove ? 'bg-primary-600' : 'bg-gray-200 dark:bg-gray-600'
          }`}
        >
          <span
            aria-hidden="true"
            className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
              isAutoApprove ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
      </div>
    </div>
  );
}
