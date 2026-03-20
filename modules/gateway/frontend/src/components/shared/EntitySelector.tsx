/**
 * Entity Selector Component
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Issue #226: Updated to use Cognito-backed endpoints as single source of truth.
 *
 * A shared component for selecting entity type and entity ID when creating/editing
 * budgets and rate limits. Now fetches entities from Cognito via the backend API.
 */

import { useState, useEffect } from 'react';
import { Select } from '@/components/ui/Select';
import { Input } from '@/components/ui/Input';
import {
  getCognitoUsers,
  getCognitoTeams,
  getCognitoDepartments,
} from '@/services/admin';
import { EntityType } from '@/types';

interface EntityOption {
  value: string;
  label: string;
}

interface EntitySelectorProps {
  orgId: string;
  entityType: string;
  entityId: string;
  onEntityTypeChange: (entityType: string) => void;
  onEntityIdChange: (entityId: string) => void;
  disabled?: boolean;
  /** List of department IDs for fetching teams (needed since teams require dept ID) */
  departmentIds?: string[];
}

const entityTypeOptions = [
  { value: EntityType.ORGANIZATION, label: 'Organization' },
  { value: EntityType.DEPARTMENT, label: 'Department' },
  { value: EntityType.TEAM, label: 'Team' },
  { value: EntityType.USER, label: 'User' },
];

export function EntitySelector({
  orgId,
  entityType,
  entityId,
  onEntityTypeChange,
  onEntityIdChange,
  disabled = false,
}: EntitySelectorProps) {
  const [entityOptions, setEntityOptions] = useState<EntityOption[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [useManualInput, setUseManualInput] = useState(false);

  // Fetch entities when entity type or org changes
  // Issue #226: Updated to use Cognito-backed endpoints
  useEffect(() => {
    if (!orgId) return;

    let cancelled = false;

    async function fetchEntities() {
      setIsLoading(true);
      setError(null);
      setEntityOptions([]);
      setUseManualInput(false);

      try {
        let options: EntityOption[] = [];

        switch (entityType) {
          case EntityType.ORGANIZATION:
            options = [{ value: orgId, label: `Current Organization (${orgId})` }];
            break;

          case EntityType.DEPARTMENT: {
            const deptResponse = await getCognitoDepartments(orgId);
            if (cancelled) return;
            options = deptResponse.items.map((dept) => ({
              value: dept.departmentId,
              label: dept.departmentId,
            }));
            break;
          }

          case EntityType.TEAM: {
            const teamsResponse = await getCognitoTeams(orgId, { pageSize: 100 });
            if (cancelled) return;
            options = teamsResponse.items.map((team) => ({
              value: team.groupName,
              label: team.description
                ? `${team.groupName} (${team.description})`
                : team.groupName,
            }));
            break;
          }

          case EntityType.USER: {
            const usersResponse = await getCognitoUsers(orgId, { pageSize: 100 });
            if (cancelled) return;
            options = usersResponse.items.map((user) => ({
              value: user.username,
              label: user.name
                ? `${user.name} (${user.email || user.username})`
                : user.email || user.username,
            }));
            break;
          }

          default:
            setUseManualInput(true);
            setIsLoading(false);
            return;
        }

        if (!cancelled) {
          setEntityOptions(options);
          setUseManualInput(options.length === 0);
        }
      } catch (err) {
        if (!cancelled) {
          console.warn('Failed to fetch entities, falling back to manual input:', err);
          setError('Failed to load entities. You can enter the ID manually.');
          setUseManualInput(true);
        }
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    }

    fetchEntities();

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entityType, orgId]);

  return (
    <div className="space-y-4">
      <Select
        label="Entity Type"
        options={entityTypeOptions}
        value={entityType}
        onChange={(e) => {
          onEntityTypeChange(e.target.value);
          onEntityIdChange(''); // Clear entity ID when type changes
        }}
        disabled={disabled}
        required
      />

      {isLoading ? (
        <div className="w-full">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            Entity ID
            <span className="text-red-500 ml-1">*</span>
          </label>
          <div className="w-full px-3 py-2 border rounded-lg border-gray-300 dark:border-gray-600 bg-gray-50 dark:bg-gray-700 text-gray-500 dark:text-gray-400">
            Loading entities...
          </div>
        </div>
      ) : useManualInput || entityOptions.length === 0 ? (
        <div>
          <Input
            label="Entity ID"
            value={entityId}
            onChange={(e) => onEntityIdChange(e.target.value)}
            placeholder={getPlaceholderForEntityType(entityType)}
            disabled={disabled}
            required
            error={error || undefined}
            helperText={
              entityType === EntityType.USER
                ? 'Enter the user ID (e.g., user-123 or user email)'
                : entityOptions.length === 0 && !error
                  ? 'No entities found. Enter the ID manually.'
                  : undefined
            }
          />
        </div>
      ) : (
        <Select
          label="Entity ID"
          options={entityOptions}
          value={entityId}
          onChange={(e) => onEntityIdChange(e.target.value)}
          placeholder="Select an entity..."
          disabled={disabled}
          required
        />
      )}

      {!useManualInput && entityOptions.length > 0 && (
        <button
          type="button"
          className="text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300 underline"
          onClick={() => setUseManualInput(true)}
        >
          Enter ID manually instead
        </button>
      )}

      {useManualInput && entityOptions.length > 0 && (
        <button
          type="button"
          className="text-sm text-primary-600 hover:text-primary-700 dark:text-primary-400 dark:hover:text-primary-300 underline"
          onClick={() => setUseManualInput(false)}
        >
          Select from list instead
        </button>
      )}
    </div>
  );
}

function getPlaceholderForEntityType(entityType: string): string {
  switch (entityType) {
    case EntityType.ORGANIZATION:
      return 'e.g., org-001';
    case EntityType.DEPARTMENT:
      return 'e.g., dept-001 or engineering';
    case EntityType.TEAM:
      return 'e.g., team-001 or platform-team';
    case EntityType.USER:
      return 'e.g., user-123 or user@example.com';
    default:
      return 'Enter entity ID';
  }
}
