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
import { Alert } from '@/components/ui/Alert';
import { Button } from '@/components/ui/Button';
import { Table, type Column } from '@/components/ui/Table';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { useToast } from '@/contexts/ToastContext';
import { useAuthContext } from '@/contexts/AuthContext';
import { usePermissions } from '@/hooks/usePermissions';
import {
  type BudgetListItem,
  getBudgetsWithUtilization,
  deleteBudgetByEntity,
} from '@/services/budget';
import { EntityType } from '@/types';
import { BudgetFormModal } from '@/components/budget/BudgetFormModal';
// Issue #4691: the admin authoring surface for #4690's default-limit ladder.
import { DefaultPersonLimits } from '@/components/budget/DefaultPersonLimits';
import { DeleteConfirmationModal } from '@/components/ui/DeleteConfirmationModal';
// Issue #4207: was a byte-identical local copy of utils/format's formatCurrency.
import { formatCurrency } from '@/utils/format';
// Issue #4536: the friendly labels moved to a shared module so the create form, this
// list and the edit view cannot word the two person-scoped budget kinds differently —
// and so `root_user` reaches no screen.
import { formatEntityType, PERSON_LIMIT_LABEL, WORKSPACE_NOUN , PERSON_LIMIT_OPTION_VALUE } from '@/utils/entityLabels';

// Helper to get utilization badge color
function getUtilizationBadgeVariant(pct: number): 'success' | 'warning' | 'danger' {
  if (pct < 50) return 'success';
  if (pct < 80) return 'warning';
  return 'danger';
}

export function BudgetManagement() {
  const { user } = useAuthContext();
  const { isPlatformAdmin } = usePermissions();
  const toast = useToast();
  // Read once per render rather than passed as a call: the create modal needs the
  // answer, not the predicate. Gates the person-limit option only (#4687, #4620 §4.2)
  // — the route's `require_platform_admin` is the actual boundary.
  const callerIsPlatformAdmin = isPlatformAdmin();

  const [budgets, setBudgets] = useState<BudgetListItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>('');

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  // Preselection handed to the create modal by the advisory's redirect (review
  // fix on #4688): the button must land on the person-limit form with the person
  // already picked, or the operator re-authors the mis-aimed cap it warned about.
  const [createPreset, setCreatePreset] = useState<{ entityType: string; entityId: string } | null>(null);
  // The person the advisory described, so the redirect can preselect them.
  const [advisorySubjectId, setAdvisorySubjectId] = useState<string | null>(null);
  // Persistent evidence of a person-limit write (review fix on #4688): the budget
  // list below cannot contain it (different table), so a closing modal plus a
  // transient toast reads as a failed write. Dismissible, not a toast.
  const [personLimitConfirmation, setPersonLimitConfirmation] = useState<string | null>(null);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedBudget, setSelectedBudget] = useState<BudgetListItem | null>(null);
  /**
   * The #4669 advisory for the cap that was just created — Issue #4687.
   *
   * Page-level, not modal-level, because the create closes the modal: a notice owned by
   * a dialog that is going away would be rendered and instantly dismissed. It describes
   * a budget that **exists** — a cloud-agent cap authored in a workspace where this
   * person's agent spend does not accrue, so it may never be reached. Never an error,
   * and nothing here undoes the create.
   */
  const [createAdvisory, setCreateAdvisory] = useState<string | null>(null);

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
    ...[
      EntityType.ORGANIZATION,
      EntityType.DEPARTMENT,
      EntityType.TEAM,
      EntityType.USER,
      // Issue #4536: cloud-agent budgets are authorable now, so they must be
      // filterable too — otherwise the type that is hardest to find is the one a
      // person most needs to check.
      EntityType.ROOT_USER,
    ].map((value) => ({ value, label: formatEntityType(value) })),
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

      {/* Issue #4687: the cap was created. This says why it may never bind, and offers
          the control that would — which is the entire point of surfacing it at create
          time rather than leaving the admin to discover it from spend that never
          stopped. Dismissible, and dismissing it changes nothing about the cap. */}
      {personLimitConfirmation && (
        <Alert variant="success" title="Person limit set" onDismiss={() => setPersonLimitConfirmation(null)}>
          <p data-testid="person-limit-confirmation">
            The limit was written. It does not appear in the budget list below — person limits live outside any single {WORKSPACE_NOUN} — and it
            now governs this person's agent spend everywhere. The person sees it on their own Budget &amp; Spend page.
          </p>
        </Alert>
      )}

      {createAdvisory && (
        <Alert
          variant="warning"
          title="This cap may never be reached"
          onDismiss={() => setCreateAdvisory(null)}
        >
          <p>{createAdvisory}</p>
          {callerIsPlatformAdmin ? (
            <p className="mt-2">
              To bound this person's agent spend everywhere, set a{' '}
              {PERSON_LIMIT_LABEL.toLowerCase()} instead.{' '}
              <button
                type="button"
                className="underline font-medium hover:opacity-80"
                onClick={() => {
                  // Straight back into the create flow — landing on the person-limit
                  // form with the SAME person picked (review fix on #4688). The
                  // advisory is cleared as the form reopens; the cap it described is
                  // untouched either way.
                  setCreatePreset(advisorySubjectId ? { entityType: PERSON_LIMIT_OPTION_VALUE, entityId: advisorySubjectId } : { entityType: PERSON_LIMIT_OPTION_VALUE, entityId: '' });
                  setCreateAdvisory(null);
                  setShowCreateModal(true);
                }}
              >
                Set a person limit instead
              </button>
            </p>
          ) : (
            // The ruling on #4620 §4.2 forbids org admins from authoring somebody's
            // cross-workspace limit, so this path offers no button — an affordance that
            // 403s is worse than none. It names who can do it instead.
            <p className="mt-2">
              A limit that follows this person across GitHub orgs can only be set
              by a platform admin — ask one to set a {PERSON_LIMIT_LABEL.toLowerCase()}{' '}
              for them.
            </p>
          )}
        </Alert>
      )}

      {/* Issue #4691: authoring for the #4690 default-limit ladder — platform admins
          only, and above the budget list on purpose. These rules govern a POPULATION's
          spend across every GitHub org, so they are not rows in a list of per-entity
          budgets inside one org; putting them below it would file the broadest control
          on the page under the narrowest heading.

          The gate is the same `isPlatformAdmin()` the create form's person-limit option
          uses (#4687), and it is an affordance for the same reason: every route the
          panel calls enforces `require_platform_admin` server-side. Widening this gate
          would not widen the authority — it would only produce 403s an org admin has no
          way to interpret. */}
      {callerIsPlatformAdmin && <DefaultPersonLimits />}

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
        onSuccess={(result) => {
          setShowCreateModal(false);
          setCreatePreset(null);
          // The budget is already committed when this fires; the advisory only decides
          // whether a notice is shown next to the refreshed list.
          setCreateAdvisory(result?.advisory ?? null);
          setAdvisorySubjectId(result?.advisory ? (result?.entityId ?? null) : null);
          // A person-limit write is NOT in the list below (different table), so it
          // gets persistent on-page evidence instead of only a transient toast
          // (review fix on #4688). `advisory` null + entityId present = person path.
          if (result && result.advisory === null && result.entityId) {
            setPersonLimitConfirmation(result.entityId);
          }
          loadBudgets();
        }}
        orgId={user?.orgId || ''}
        isPlatformAdmin={callerIsPlatformAdmin}
        preset={createPreset}
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
