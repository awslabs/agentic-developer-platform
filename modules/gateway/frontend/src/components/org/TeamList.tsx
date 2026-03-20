import { Card, Table, Button } from '@/components/ui';
import { formatDate } from '@/utils/format';
import type { Team } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface TeamListProps {
  teams: Team[];
  onCreateTeam?: () => void;
  onEditTeam?: (team: Team) => void;
  onDeleteTeam?: (team: Team) => void;
  isLoading?: boolean;
  canManage?: boolean;
}

export function TeamList({
  teams,
  onCreateTeam,
  onEditTeam,
  onDeleteTeam,
  isLoading,
  canManage = false,
}: TeamListProps) {
  const columns: Column<Team>[] = [
    {
      key: 'name',
      header: 'Team',
      render: (team) => (
        <span className="font-medium text-gray-900 dark:text-white">{team.name}</span>
      ),
    },
    {
      key: 'description',
      header: 'Description',
      render: (team) => (
        <span className="text-gray-500 dark:text-gray-400">{team.description || '-'}</span>
      ),
    },
    {
      key: 'createdAt',
      header: 'Created',
      render: (team) => formatDate(team.createdAt),
    },
  ];

  if (canManage) {
    columns.push({
      key: 'actions',
      header: '',
      align: 'right',
      render: (team) => (
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => onEditTeam?.(team)}>
            Edit
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onDeleteTeam?.(team)}>
            Delete
          </Button>
        </div>
      ),
    });
  }

  return (
    <Card padding="none">
      <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <h3 className="font-semibold text-gray-900 dark:text-white">Teams</h3>
        {canManage && onCreateTeam && (
          <Button size="sm" onClick={onCreateTeam}>
            Add Team
          </Button>
        )}
      </div>
      <Table
        columns={columns}
        data={teams}
        keyExtractor={(team) => team.id}
        isLoading={isLoading}
        emptyMessage="No teams found"
      />
    </Card>
  );
}
