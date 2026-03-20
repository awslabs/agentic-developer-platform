import { useState } from 'react';
import { Card, CardTitle, Table, Button, Modal, ModalFooter, Input, Select, Badge } from '@/components/ui';
import { formatCurrency } from '@/utils/format';
import { EntityType, PeriodType, EnforcementMode } from '@/types';
import type { Budget } from '@/types';
import type { Column } from '@/components/ui/Table';

export interface BudgetAllocationProps {
  budgets: Budget[];
  onCreateBudget: (data: {
    entityType: EntityType;
    entityId: string;
    periodType: PeriodType;
    budgetAmountUsd: number;
    enforcementMode: EnforcementMode;
  }) => Promise<void>;
  onUpdateBudget: (budget: Budget, data: { budgetAmountUsd?: number; enforcementMode?: EnforcementMode }) => Promise<void>;
  onDeleteBudget: (budgetId: string) => Promise<void>;
  entities: Array<{ id: string; name: string; type: EntityType }>;
  isLoading?: boolean;
  canManage?: boolean;
}

export function BudgetAllocation({
  budgets,
  onCreateBudget,
  onUpdateBudget,
  onDeleteBudget,
  entities,
  isLoading,
  canManage = false,
}: BudgetAllocationProps) {
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [editingBudget, setEditingBudget] = useState<Budget | null>(null);
  const [isSaving, setIsSaving] = useState(false);

  // Form state
  const [entityId, setEntityId] = useState('');
  const [entityType, setEntityType] = useState<EntityType>(EntityType.TEAM);
  const [periodType, setPeriodType] = useState<PeriodType>(PeriodType.MONTHLY);
  const [budgetAmount, setBudgetAmount] = useState('');
  const [enforcementMode, setEnforcementMode] = useState<EnforcementMode>(EnforcementMode.SOFT);

  const resetForm = () => {
    setEntityId('');
    setEntityType(EntityType.TEAM);
    setPeriodType(PeriodType.MONTHLY);
    setBudgetAmount('');
    setEnforcementMode(EnforcementMode.SOFT);
  };

  const handleCreate = async () => {
    setIsSaving(true);
    try {
      await onCreateBudget({
        entityType,
        entityId,
        periodType,
        budgetAmountUsd: parseFloat(budgetAmount),
        enforcementMode,
      });
      setIsCreateModalOpen(false);
      resetForm();
    } finally {
      setIsSaving(false);
    }
  };

  const handleUpdate = async () => {
    if (!editingBudget) return;
    setIsSaving(true);
    try {
      await onUpdateBudget(editingBudget, {
        budgetAmountUsd: parseFloat(budgetAmount),
        enforcementMode,
      });
      setEditingBudget(null);
      resetForm();
    } finally {
      setIsSaving(false);
    }
  };

  const openEdit = (budget: Budget) => {
    setEditingBudget(budget);
    setBudgetAmount(String(budget.budgetAmountUsd));
    setEnforcementMode(budget.enforcementMode);
  };

  const getEntityName = (type: EntityType, id: string): string => {
    const entity = entities.find((e) => e.type === type && e.id === id);
    return entity?.name || id;
  };

  const columns: Column<Budget>[] = [
    {
      key: 'entityId',
      header: 'Entity',
      render: (budget) => (
        <div>
          <span className="font-medium text-gray-900 dark:text-white">
            {getEntityName(budget.entityType, budget.entityId)}
          </span>
          <Badge size="sm" className="ml-2">
            {budget.entityType}
          </Badge>
        </div>
      ),
    },
    {
      key: 'budgetAmountUsd',
      header: 'Budget',
      align: 'right',
      render: (budget) => formatCurrency(budget.budgetAmountUsd),
    },
    {
      key: 'periodType',
      header: 'Period',
      render: (budget) => (
        <span className="capitalize">{budget.periodType}</span>
      ),
    },
    {
      key: 'enforcementMode',
      header: 'Enforcement',
      render: (budget) => (
        <Badge variant={budget.enforcementMode === 'hard' ? 'danger' : 'warning'}>
          {budget.enforcementMode === 'hard' ? 'Hard' : 'Soft'}
        </Badge>
      ),
    },
  ];

  if (canManage) {
    columns.push({
      key: 'actions',
      header: '',
      align: 'right',
      render: (budget) => (
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => openEdit(budget)}>
            Edit
          </Button>
          <Button variant="ghost" size="sm" onClick={() => onDeleteBudget(budget.id)}>
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
          <CardTitle>Budget Allocations</CardTitle>
          {canManage && (
            <Button size="sm" onClick={() => setIsCreateModalOpen(true)}>
              Add Budget
            </Button>
          )}
        </div>
        <Table
          columns={columns}
          data={budgets}
          keyExtractor={(budget) => budget.id}
          isLoading={isLoading}
          emptyMessage="No budgets configured"
        />
      </Card>

      {/* Create Modal */}
      <Modal
        isOpen={isCreateModalOpen}
        onClose={() => {
          setIsCreateModalOpen(false);
          resetForm();
        }}
        title="Create Budget"
      >
        <div className="space-y-4">
          <Select
            label="Entity Type"
            value={entityType}
            onChange={(e) => setEntityType(e.target.value as EntityType)}
            options={[
              { value: EntityType.TEAM, label: 'Team' },
              { value: EntityType.USER, label: 'User' },
            ]}
          />
          <Select
            label="Entity"
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            options={entities
              .filter((e) => e.type === entityType)
              .map((e) => ({ value: e.id, label: e.name }))}
            placeholder="Select entity"
          />
          <Select
            label="Period"
            value={periodType}
            onChange={(e) => setPeriodType(e.target.value as PeriodType)}
            options={[
              { value: PeriodType.DAILY, label: 'Daily' },
              { value: PeriodType.WEEKLY, label: 'Weekly' },
              { value: PeriodType.MONTHLY, label: 'Monthly' },
            ]}
          />
          <Input
            label="Budget Amount (USD)"
            type="number"
            min="0"
            step="0.01"
            value={budgetAmount}
            onChange={(e) => setBudgetAmount(e.target.value)}
            placeholder="100.00"
          />
          <Select
            label="Enforcement Mode"
            value={enforcementMode}
            onChange={(e) => setEnforcementMode(e.target.value as EnforcementMode)}
            options={[
              { value: EnforcementMode.SOFT, label: 'Soft (warn only)' },
              { value: EnforcementMode.HARD, label: 'Hard (block requests)' },
            ]}
          />
        </div>
        <ModalFooter>
          <Button variant="secondary" onClick={() => setIsCreateModalOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={handleCreate}
            isLoading={isSaving}
            disabled={!entityId || !budgetAmount}
          >
            Create
          </Button>
        </ModalFooter>
      </Modal>

      {/* Edit Modal */}
      <Modal
        isOpen={!!editingBudget}
        onClose={() => {
          setEditingBudget(null);
          resetForm();
        }}
        title="Edit Budget"
      >
        <div className="space-y-4">
          <Input
            label="Budget Amount (USD)"
            type="number"
            min="0"
            step="0.01"
            value={budgetAmount}
            onChange={(e) => setBudgetAmount(e.target.value)}
            placeholder="100.00"
          />
          <Select
            label="Enforcement Mode"
            value={enforcementMode}
            onChange={(e) => setEnforcementMode(e.target.value as EnforcementMode)}
            options={[
              { value: EnforcementMode.SOFT, label: 'Soft (warn only)' },
              { value: EnforcementMode.HARD, label: 'Hard (block requests)' },
            ]}
          />
        </div>
        <ModalFooter>
          <Button variant="secondary" onClick={() => setEditingBudget(null)}>
            Cancel
          </Button>
          <Button onClick={handleUpdate} isLoading={isSaving} disabled={!budgetAmount}>
            Save Changes
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}
