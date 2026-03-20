import { Link } from 'react-router-dom';
import { Card, Table, Button } from '@/components/ui';
import { formatCurrency, formatNumber, formatDate } from '@/utils/format';
import type { Department } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface DepartmentListProps {
  orgId: string;
  departments: Department[];
  topDepartments?: Array<{
    id: string;
    name: string;
    requestCount: number;
    tokenCount: number;
    costUsd: number;
  }>;
  onCreateDepartment?: () => void;
  onEditDepartment?: (dept: Department) => void;
  onDeleteDepartment?: (dept: Department) => void;
  isLoading?: boolean;
  canManage?: boolean;
}

export function DepartmentList({
  orgId,
  departments,
  topDepartments,
  onCreateDepartment,
  onEditDepartment,
  onDeleteDepartment,
  isLoading,
  canManage = false,
}: DepartmentListProps) {
  // Merge usage data with department info
  const departmentsWithUsage = departments.map((dept) => {
    const usage = topDepartments?.find((d) => d.id === dept.id);
    return {
      ...dept,
      requestCount: usage?.requestCount || 0,
      tokenCount: usage?.tokenCount || 0,
      costUsd: usage?.costUsd || 0,
    };
  });

  const columns: Column<typeof departmentsWithUsage[0]>[] = [
    {
      key: 'name',
      header: 'Department',
      render: (dept) => (
        <Link
          to={`/org/${orgId}/department/${dept.id}`}
          className="text-primary-600 hover:text-primary-500 font-medium"
        >
          {dept.name}
        </Link>
      ),
    },
    {
      key: 'description',
      header: 'Description',
      render: (dept) => (
        <span className="text-gray-500 dark:text-gray-400">
          {dept.description || '-'}
        </span>
      ),
    },
    {
      key: 'requestCount',
      header: 'Requests (24h)',
      align: 'right',
      render: (dept) => formatNumber(dept.requestCount),
    },
    {
      key: 'costUsd',
      header: 'Cost (24h)',
      align: 'right',
      render: (dept) => formatCurrency(dept.costUsd),
    },
    {
      key: 'createdAt',
      header: 'Created',
      render: (dept) => formatDate(dept.createdAt),
    },
  ];

  if (canManage) {
    columns.push({
      key: 'actions',
      header: '',
      align: 'right',
      render: (dept) => (
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => onEditDepartment?.(dept)}>
            Edit
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onDeleteDepartment?.(dept)}>
            Delete
          </Button>
        </div>
      ),
    });
  }

  return (
    <Card padding="none">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h3 className="font-semibold text-gray-900 dark:text-white">Departments</h3>
        {canManage && onCreateDepartment && (
          <Button size="sm" onClick={onCreateDepartment}>
            Add Department
          </Button>
        )}
      </div>
      <Table
        columns={columns}
        data={departmentsWithUsage}
        keyExtractor={(dept) => dept.id}
        isLoading={isLoading}
        emptyMessage="No departments found"
      />
    </Card>
  );
}
