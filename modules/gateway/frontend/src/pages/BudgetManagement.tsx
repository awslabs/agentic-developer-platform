/**
 * Budget Management Page
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * - Table view showing all budget configs for the org
 * - Color-coded utilization: green (<50%), yellow (50-80%), red (>80%)
 * - Filter by entity type
 * - Add/Edit/Delete budgets
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Table, type Column } from '@/components/ui/Table';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { useToast } from '@/contexts/ToastContext';
import { useAuthContext } from '@/contexts/AuthContext';
import {
  type BudgetListItem,
  getBudgetsWithUtilization,
  deleteBudgetByEntity,
} from '@/services/budget';
import { EntityType } from '@/types';
import { BudgetFormModal } from '@/components/budget/BudgetFormModal';
import { DeleteConfirmationModal } from '@/components/ui/DeleteConfirmationModal';

// Helper to get utilization badge color
function getUtilizationBadgeVariant(pct: number): 'success' | 'warning' | 'danger' {
  if (pct < 50) return 'success';
  if (pct < 80) return 'warning';
  return 'danger';
}

// Format currency
function formatCurrency(amount: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amount);
}

// Format entity type for display
function formatEntityType(type: string): string {
  const map: Record<string, string> = {
    org: 'Organization',
    department: 'Department',
    team: 'Team',
    user: 'User',
  };
  return map[type] || type;
}

export function BudgetManagement() {
  const { user } = useAuthContext();
  const toast = useToast();

  const [budgets, setBudgets] = useState<BudgetListItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>('');

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedBudget, setSelectedBudget] = useState<BudgetListItem | null>(null);

  // Use ref to break useEffect/useCallback dependency cycle on toast (Defect #2 fix)
  const toastRef = useRef(toast);
  toastRef.current = toast;

  const loadBudgets = useCallback(async () => {
    if (!user?.orgId) return;

    setIsLoading(true);
    try {
      const response = await getBudgetsWithUtilization(user.orgId, {
        entityType: entityTypeFilter ? (entityTypeFilter as EntityType) : undefined,
        page,
        limit: 20,
      });
      setBudgets(response.items);
      setTotal(response.total);
      setHasMore(response.hasMore);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to load budgets';
      toastRef.current.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [page, user?.orgId, entityTypeFilter]);

  useEffect(() => {
    loadBudgets();
  }, [loadBudgets]);

  const handleDeleteBudget = async () => {
    if (!selectedBudget || !user?.orgId) return;

    try {
      await deleteBudgetByEntity(
        user.orgId,
        selectedBudget.entityType,
        selectedBudget.entityId,
        selectedBudget.periodType
      );
      toast.success('Budget deleted successfully');
      setShowDeleteModal(false);
      setSelectedBudget(null);
      loadBudgets();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to delete budget';
      toast.error(message);
    }
  };

  const handleEdit = (budget: BudgetListItem) => {
    setSelectedBudget(budget);
    setShowEditModal(true);
  };

  const handleDelete = (budget: BudgetListItem) => {
    setSelectedBudget(budget);
    setShowDeleteModal(true);
  };

  const columns: Column<BudgetListItem>[] = [
    {
      key: 'entityType',
      header: 'Entity Type',
      render: (item: BudgetListItem) => (
        <Badge variant="default">{formatEntityType(item.entityType)}</Badge>
      ),
    },
    {
      key: 'entityId',
      header: 'Entity ID',
      render: (item: BudgetListItem) => (
        <div>
          {item.entityDisplayName && (
            <span className="text-sm text-gray-900 dark:text-white">{item.entityDisplayName}</span>
          )}
          <span className="font-mono text-xs text-gray-500 block">{item.entityId}</span>
        </div>
      ),
    },
    {
      key: 'periodType',
      header: 'Period',
      render: (item: BudgetListItem) => (
        <span className="capitalize">{item.periodType}</span>
      ),
    },
    {
      key: 'budgetAmountUsd',
      header: 'Budget ($)',
      align: 'right',
      render: (item: BudgetListItem) => formatCurrency(item.budgetAmountUsd),
    },
    {
      key: 'currentUsageUsd',
      header: 'Current Usage ($)',
      align: 'right',
      render: (item: BudgetListItem) => formatCurrency(item.currentUsageUsd),
    },
    {
      key: 'utilizationPct',
      header: 'Utilization (%)',
      align: 'center',
      render: (item: BudgetListItem) => (
        <Badge variant={getUtilizationBadgeVariant(item.utilizationPct)}>
          {item.utilizationPct.toFixed(1)}%
        </Badge>
      ),
    },
    {
      key: 'enforcementMode',
      header: 'Mode',
      render: (item: BudgetListItem) => (
        <Badge variant={item.enforcementMode === 'hard' ? 'danger' : 'info'}>
          {item.enforcementMode}
        </Badge>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (item: BudgetListItem) => (
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => handleEdit(item)}>
            Edit
          </Button>
          <Button variant="danger" size="sm" onClick={() => handleDelete(item)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const entityTypeOptions = [
    { value: '', label: 'All Types' },
    { value: EntityType.ORGANIZATION, label: 'Organization' },
    { value: EntityType.DEPARTMENT, label: 'Department' },
    { value: EntityType.TEAM, label: 'Team' },
    { value: EntityType.USER, label: 'User' },
  ];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Budget Management
          </h1>
          <p className="text-gray-600 dark:text-gray-400">
            Manage budget configurations for your organization
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>Add Budget</Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex justify-between items-center">
            <CardTitle>Budgets ({total})</CardTitle>
            <div className="w-48">
              <Select
                options={entityTypeOptions}
                value={entityTypeFilter}
                onChange={(e) => {
                  setEntityTypeFilter(e.target.value);
                  setPage(1);
                }}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex justify-center py-8">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600" />
            </div>
          ) : budgets.length === 0 ? (
            <div className="text-center py-8 text-gray-500">
              <p>No budgets configured yet.</p>
              <p className="text-sm mt-2">
                Create a budget to set spending limits for entities.
              </p>
            </div>
          ) : (
            <>
              <Table
                data={budgets}
                columns={columns}
                keyExtractor={(item) =>
                  `${item.entityType}-${item.entityId}-${item.periodType}`
                }
              />
              {hasMore && (
                <div className="flex justify-center mt-4">
                  <Button variant="secondary" onClick={() => setPage(page + 1)}>
                    Load More
                  </Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Create Modal */}
      <BudgetFormModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSuccess={() => {
          setShowCreateModal(false);
          loadBudgets();
        }}
        orgId={user?.orgId || ''}
      />

      {/* Edit Modal */}
      {selectedBudget && (
        <BudgetFormModal
          isOpen={showEditModal}
          onClose={() => {
            setShowEditModal(false);
            setSelectedBudget(null);
          }}
          onSuccess={() => {
            setShowEditModal(false);
            setSelectedBudget(null);
            loadBudgets();
          }}
          orgId={user?.orgId || ''}
          editData={{
            entityType: selectedBudget.entityType,
            entityId: selectedBudget.entityId,
            periodType: selectedBudget.periodType,
            budgetAmountUsd: selectedBudget.budgetAmountUsd,
            enforcementMode: selectedBudget.enforcementMode,
          }}
        />
      )}

      {/* Delete Confirmation Modal */}
      <DeleteConfirmationModal
        isOpen={showDeleteModal}
        onClose={() => {
          setShowDeleteModal(false);
          setSelectedBudget(null);
        }}
        onConfirm={handleDeleteBudget}
        title="Delete Budget"
        message={
          selectedBudget
            ? `Are you sure you want to delete the ${selectedBudget.periodType} budget for ${formatEntityType(selectedBudget.entityType)} "${selectedBudget.entityId}"?`
            : ''
        }
      />
    </div>
  );
}

export default BudgetManagement;
