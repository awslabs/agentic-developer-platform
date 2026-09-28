import { Card, Table, Button, Badge } from '@/components/ui';
import { formatDate } from '@/utils/format';
import type { UserRole, AdminRole } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface UserListProps {
  users: UserRole[];
  onAssignRole?: () => void;
  onRemoveRole?: (user: UserRole) => void;
  isLoading?: boolean;
  canManage?: boolean;
}

export function UserList({
  users,
  onAssignRole,
  onRemoveRole,
  isLoading,
  canManage = false,
}: UserListProps) {
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

  const formatRole = (role: string): string => {
    return role.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
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
        <Button variant="ghost" size="sm" onClick={() => onRemoveRole?.(user)}>
          Remove
        </Button>
      ),
    });
  }

  return (
    <Card padding="none">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h3 className="font-semibold text-gray-900 dark:text-white">Admin Users</h3>
        {canManage && onAssignRole && (
          <Button size="sm" onClick={onAssignRole}>
            Assign Role
          </Button>
        )}
      </div>
      <Table
        columns={columns}
        data={users}
        keyExtractor={(user) => user.userId}
        isLoading={isLoading}
        emptyMessage="No admin users found"
      />
    </Card>
  );
}
