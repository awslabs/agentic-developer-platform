/**
 * Rate Limit Management Page
 *
 * Issue #185: Budget & Rate Limit Management UI for Org Admins
 * - Table view showing all rate limit configs
 * - Filter by entity type
 * - Add/Edit/Delete rate limits
 */

import { useState, useEffect, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Table, type Column } from '@/components/ui/Table';
import { Badge } from '@/components/ui/Badge';
import { Select } from '@/components/ui/Select';
import { useToast } from '@/contexts/ToastContext';
import { useAuthContext } from '@/contexts/AuthContext';
import {
  type RateLimitListItem,
  getRatelimits,
  deleteRatelimit,
} from '@/services/ratelimit';
import { EntityType } from '@/types';
import { RateLimitFormModal } from '@/components/ratelimit/RateLimitFormModal';
import { DeleteConfirmationModal } from '@/components/ui/DeleteConfirmationModal';

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

// Format number with commas
function formatNumber(num: number | null): string {
  if (num === null) return '-';
  return new Intl.NumberFormat('en-US').format(num);
}

export function RateLimitManagement() {
  const { user } = useAuthContext();
  const toast = useToast();

  const [ratelimits, setRatelimits] = useState<RateLimitListItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [entityTypeFilter, setEntityTypeFilter] = useState<string>('');

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedRateLimit, setSelectedRateLimit] = useState<RateLimitListItem | null>(null);

  const loadRatelimits = useCallback(async () => {
    if (!user?.orgId) return;

    setIsLoading(true);
    try {
      const response = await getRatelimits(user.orgId, {
        entityType: entityTypeFilter ? (entityTypeFilter as EntityType) : undefined,
        page,
        limit: 20,
      });
      setRatelimits(response.items);
      setTotal(response.total);
      setHasMore(response.hasMore);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to load rate limits';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [page, user?.orgId, entityTypeFilter, toast]);

  useEffect(() => {
    loadRatelimits();
  }, [loadRatelimits]);

  const handleDeleteRateLimit = async () => {
    if (!selectedRateLimit || !user?.orgId) return;

    try {
      await deleteRatelimit(
        user.orgId,
        selectedRateLimit.entityType,
        selectedRateLimit.entityId
      );
      toast.success('Rate limit deleted successfully');
      setShowDeleteModal(false);
      setSelectedRateLimit(null);
      loadRatelimits();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to delete rate limit';
      toast.error(message);
    }
  };

  const handleEdit = (ratelimit: RateLimitListItem) => {
    setSelectedRateLimit(ratelimit);
    setShowEditModal(true);
  };

  const handleDelete = (ratelimit: RateLimitListItem) => {
    setSelectedRateLimit(ratelimit);
    setShowDeleteModal(true);
  };

  const columns: Column<RateLimitListItem>[] = [
    {
      key: 'entityType',
      header: 'Entity Type',
      render: (item: RateLimitListItem) => (
        <Badge variant="default">{formatEntityType(item.entityType)}</Badge>
      ),
    },
    {
      key: 'entityId',
      header: 'Entity ID',
      render: (item: RateLimitListItem) => (
        <span className="font-mono text-sm">{item.entityId}</span>
      ),
    },
    {
      key: 'rpm',
      header: 'RPM',
      align: 'right',
      render: (item: RateLimitListItem) => formatNumber(item.rpm),
    },
    {
      key: 'tpm',
      header: 'TPM',
      align: 'right',
      render: (item: RateLimitListItem) => formatNumber(item.tpm),
    },
    {
      key: 'concurrentRequests',
      header: 'Concurrent',
      align: 'right',
      render: (item: RateLimitListItem) => formatNumber(item.concurrentRequests),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (item: RateLimitListItem) => (
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
            Rate Limit Management
          </h1>
          <p className="text-gray-600 dark:text-gray-400">
            Manage rate limit configurations for your organization
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>Add Rate Limit</Button>
      </div>

      <Card>
        <CardHeader>
          <div className="flex justify-between items-center">
            <CardTitle>Rate Limits ({total})</CardTitle>
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
          ) : ratelimits.length === 0 ? (
            <div className="text-center py-8 text-gray-500">
              <p>No rate limits configured yet.</p>
              <p className="text-sm mt-2">
                Create a rate limit to control API usage for entities.
              </p>
            </div>
          ) : (
            <>
              <Table
                data={ratelimits}
                columns={columns}
                keyExtractor={(item) => `${item.entityType}-${item.entityId}`}
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
      <RateLimitFormModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSuccess={() => {
          setShowCreateModal(false);
          loadRatelimits();
        }}
        orgId={user?.orgId || ''}
      />

      {/* Edit Modal */}
      {selectedRateLimit && (
        <RateLimitFormModal
          isOpen={showEditModal}
          onClose={() => {
            setShowEditModal(false);
            setSelectedRateLimit(null);
          }}
          onSuccess={() => {
            setShowEditModal(false);
            setSelectedRateLimit(null);
            loadRatelimits();
          }}
          orgId={user?.orgId || ''}
          editData={{
            entityType: selectedRateLimit.entityType,
            entityId: selectedRateLimit.entityId,
            rpm: selectedRateLimit.rpm,
            tpm: selectedRateLimit.tpm,
            concurrentRequests: selectedRateLimit.concurrentRequests,
          }}
        />
      )}

      {/* Delete Confirmation Modal */}
      <DeleteConfirmationModal
        isOpen={showDeleteModal}
        onClose={() => {
          setShowDeleteModal(false);
          setSelectedRateLimit(null);
        }}
        onConfirm={handleDeleteRateLimit}
        title="Delete Rate Limit"
        message={
          selectedRateLimit
            ? `Are you sure you want to delete the rate limit for ${formatEntityType(selectedRateLimit.entityType)} "${selectedRateLimit.entityId}"?`
            : ''
        }
      />
    </div>
  );
}

export default RateLimitManagement;
