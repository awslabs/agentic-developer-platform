import { useState } from 'react';
import { Card, Table, Button, Modal, ModalFooter, Input, Textarea } from '@/components/ui';
import { formatDate } from '@/utils/format';
import type { Team } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface TeamManagementProps {
  teams: Team[];
  onCreateTeam: (data: { name: string; description?: string }) => Promise<void>;
  onUpdateTeam: (teamId: string, data: { name?: string; description?: string }) => Promise<void>;
  onDeleteTeam: (teamId: string) => Promise<void>;
  isLoading?: boolean;
  canManage?: boolean;
}

export function TeamManagement({
  teams,
  onCreateTeam,
  onUpdateTeam,
  onDeleteTeam,
  isLoading,
  canManage = false,
}: TeamManagementProps) {
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [editingTeam, setEditingTeam] = useState<Team | null>(null);
  const [deletingTeam, setDeletingTeam] = useState<Team | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Form state
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');

  const resetForm = () => {
    setName('');
    setDescription('');
  };

  const handleOpenEdit = (team: Team) => {
    setEditingTeam(team);
    setName(team.name);
    setDescription(team.description || '');
  };

  const handleCreate = async () => {
    setIsSaving(true);
    try {
      await onCreateTeam({ name, description: description || undefined });
      setIsCreateModalOpen(false);
      resetForm();
    } finally {
      setIsSaving(false);
    }
  };

  const handleUpdate = async () => {
    if (!editingTeam) return;
    setIsSaving(true);
    try {
      await onUpdateTeam(editingTeam.id, {
        name: name !== editingTeam.name ? name : undefined,
        description: description !== editingTeam.description ? description : undefined,
      });
      setEditingTeam(null);
      resetForm();
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!deletingTeam) return;
    setIsSaving(true);
    try {
      await onDeleteTeam(deletingTeam.id);
      setDeletingTeam(null);
    } finally {
      setIsSaving(false);
    }
  };

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
          <Button variant="ghost" size="sm" onClick={() => handleOpenEdit(team)}>
            Edit
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setDeletingTeam(team)}>
            Delete
          </Button>
        </div>
      ),
    });
  }

  return (
    <>
      <Card padding="none">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <h3 className="font-semibold text-gray-900 dark:text-white">Teams</h3>
          {canManage && (
            <Button size="sm" onClick={() => setIsCreateModalOpen(true)}>
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

      {/* Create Modal */}
      <Modal
        isOpen={isCreateModalOpen}
        onClose={() => {
          setIsCreateModalOpen(false);
          resetForm();
        }}
        title="Create Team"
      >
        <div className="space-y-4">
          <Input
            label="Team Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Enter team name"
            required
          />
          <Textarea
            label="Description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Enter team description (optional)"
            rows={3}
          />
        </div>
        <ModalFooter>
          <Button variant="secondary" onClick={() => setIsCreateModalOpen(false)}>
            Cancel
          </Button>
          <Button onClick={handleCreate} isLoading={isSaving} disabled={!name.trim()}>
            Create
          </Button>
        </ModalFooter>
      </Modal>

      {/* Edit Modal */}
      <Modal
        isOpen={!!editingTeam}
        onClose={() => {
          setEditingTeam(null);
          resetForm();
        }}
        title="Edit Team"
      >
        <div className="space-y-4">
          <Input
            label="Team Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Enter team name"
            required
          />
          <Textarea
            label="Description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="Enter team description (optional)"
            rows={3}
          />
        </div>
        <ModalFooter>
          <Button variant="secondary" onClick={() => setEditingTeam(null)}>
            Cancel
          </Button>
          <Button onClick={handleUpdate} isLoading={isSaving} disabled={!name.trim()}>
            Save Changes
          </Button>
        </ModalFooter>
      </Modal>

      {/* Delete Confirmation Modal */}
      <Modal
        isOpen={!!deletingTeam}
        onClose={() => setDeletingTeam(null)}
        title="Delete Team"
        size="sm"
      >
        <p className="text-gray-600 dark:text-gray-400">
          Are you sure you want to delete the team "{deletingTeam?.name}"? This action cannot be
          undone.
        </p>
        <ModalFooter>
          <Button variant="secondary" onClick={() => setDeletingTeam(null)}>
            Cancel
          </Button>
          <Button variant="danger" onClick={handleDelete} isLoading={isSaving}>
            Delete
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}
