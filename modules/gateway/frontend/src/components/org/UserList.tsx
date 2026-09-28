import { useState } from 'react';
import { Card, Table, Button, Badge, Modal, ModalFooter, Select } from '@/components/ui';
import { formatDate } from '@/utils/format';
import { AdminRole, type UserRole } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface UserListProps {
  users: UserRole[];
  /**
   * Change a user's role. Issue #4019: this replaces the old context-free
   * `onAssignRole?: () => void` header button, which could not name a target
   * user and so could not express "change THIS user's role".
   *
   * A rejected promise keeps the dialog open so the caller's error toast is
   * actionable (the common failure is a 403 the admin can't fix by retrying,
   * but they still need the dialog's context to understand it). Reporting the
   * error is the caller's job — this component only manages dialog state.
   */
  onChangeRole?: (user: UserRole, newRole: string) => Promise<unknown> | void;
  /** Demote a user to `member` (never deletes their membership row). */
  onRemoveRole?: (user: UserRole) => Promise<unknown> | void;
  /** Roles the current caller may assign, from GET /admin/users/roles. */
  availableRoles?: string[];
  isLoading?: boolean;
  canManage?: boolean;
}

const FALLBACK_ROLES: string[] = [
  AdminRole.MEMBER,
  AdminRole.DEPT_ADMIN,
  AdminRole.ORG_ADMIN,
  AdminRole.PLATFORM_ADMIN,
];

export function UserList({
  users,
  onChangeRole,
  onRemoveRole,
  availableRoles,
  isLoading,
  canManage = false,
}: UserListProps) {
  const [changing, setChanging] = useState<UserRole | null>(null);
  const [removing, setRemoving] = useState<UserRole | null>(null);
  const [selectedRole, setSelectedRole] = useState<string>('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const roleOptions = (availableRoles?.length ? availableRoles : FALLBACK_ROLES).map((role) => ({
    value: role,
    label: formatRole(role),
  }));

  const getRoleBadgeVariant = (role: AdminRole): 'success' | 'info' | 'warning' => {
    switch (role) {
      case 'platform_admin':
        return 'success';
      case 'org_admin':
        return 'info';
      case 'dept_admin':
        return 'warning';
      default:
        return 'info';
    }
  };

  const openChangeRole = (user: UserRole) => {
    setSelectedRole(user.role ?? '');
    setChanging(user);
  };

  const closeModals = () => {
    if (isSubmitting) return;
    setChanging(null);
    setRemoving(null);
  };

  const submitChangeRole = async () => {
    if (!changing || !selectedRole) return;
    setIsSubmitting(true);
    try {
      await onChangeRole?.(changing, selectedRole);
      setChanging(null);
    } catch {
      // Left open on failure; the caller toasts the reason.
    } finally {
      setIsSubmitting(false);
    }
  };

  const submitRemoveRole = async () => {
    if (!removing) return;
    setIsSubmitting(true);
    try {
      await onRemoveRole?.(removing);
      setRemoving(null);
    } catch {
      // Left open on failure; the caller toasts the reason.
    } finally {
      setIsSubmitting(false);
    }
  };

  const columns: Column<UserRole>[] = [
    {
      key: 'userId',
      header: 'User ID',
      render: (user) => (
        <span className="font-medium text-gray-900 dark:text-white">{user.userId}</span>
      ),
    },
    {
      key: 'role',
      header: 'Role',
      render: (user) => (
        <Badge variant={getRoleBadgeVariant(user.role)}>{formatRole(user.role)}</Badge>
      ),
    },
    {
      key: 'deptId',
      header: 'Department',
      render: (user) => (
        <span className="text-gray-500 dark:text-gray-400">{user.deptId || '-'}</span>
      ),
    },
    {
      key: 'createdAt',
      header: 'Assigned',
      render: (user) => formatDate(user.createdAt),
    },
  ];

  if (canManage) {
    columns.push({
      key: 'actions',
      header: '',
      align: 'right',
      render: (user) => (
        <div className="flex items-center justify-end gap-2">
          {onChangeRole && (
            <Button variant="ghost" size="sm" onClick={() => openChangeRole(user)}>
              Change Role
            </Button>
          )}
          {onRemoveRole && user.role !== AdminRole.MEMBER && (
            <Button variant="ghost" size="sm" onClick={() => setRemoving(user)}>
              Remove
            </Button>
          )}
        </div>
      ),
    });
  }

  return (
    <>
      <Card padding="none">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <h3 className="font-semibold text-gray-900 dark:text-white">Admin Users</h3>
        </div>
        <Table
          columns={columns}
          data={users}
          keyExtractor={(user) => user.userId}
          isLoading={isLoading}
          emptyMessage="No admin users found"
        />
      </Card>

      <Modal isOpen={changing !== null} onClose={closeModals} title="Change Role" size="sm">
        <div className="space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Change the role for <span className="font-medium">{changing?.userId}</span>. This takes
            effect immediately for their permissions; their role badge updates the next time they
            sign in.
          </p>
          <Select
            label="Role"
            name="role"
            options={roleOptions}
            value={selectedRole}
            onChange={(e) => setSelectedRole(e.target.value)}
          />
        </div>
        <ModalFooter>
          <Button variant="ghost" onClick={closeModals} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button
            onClick={submitChangeRole}
            isLoading={isSubmitting}
            disabled={!selectedRole || selectedRole === changing?.role}
          >
            Change Role
          </Button>
        </ModalFooter>
      </Modal>

      <Modal isOpen={removing !== null} onClose={closeModals} title="Remove Role" size="sm">
        <p className="text-sm text-gray-600 dark:text-gray-400">
          Remove <span className="font-medium">{removing?.userId}</span>&apos;s{' '}
          {formatRole(removing?.role ?? '')} role? They will be demoted to Member and immediately
          lose the permissions that role granted.
        </p>
        <ModalFooter>
          <Button variant="ghost" onClick={closeModals} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={submitRemoveRole} isLoading={isSubmitting}>
            Remove Role
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}

function formatRole(role: string): string {
  return role.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}
