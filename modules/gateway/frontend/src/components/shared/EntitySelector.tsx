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
  getOrgUsers,
  getCognitoTeams,
  getCognitoDepartments,
} from '@/services/admin';
import { EntityType } from '@/types';
import { formatEntityType, entityTypeHelpText } from '@/utils/entityLabels';

interface EntityOption {
  value: string;
  label: string;
  /** Rendered but unselectable — see the NULL-sub case in the user branch below. */
  disabled?: boolean;
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
  /**
   * Offer "User — cloud agents" (`root_user`) alongside "User — direct use".
   *
   * Issue #4536. Opt-in rather than always-on because this component is shared with
   * the rate-limit form, and nothing enforces a `root_user` rate limit — offering it
   * there would let an operator configure a limit that silently does nothing. Only
   * the budget form, whose create/update path resolves and persists the type, sets it.
   */
  allowCloudAgentScope?: boolean;
}

/** The entity types every caller offers, in narrowing order. */
const baseEntityTypes = [
  EntityType.ORGANIZATION,
  EntityType.DEPARTMENT,
  EntityType.TEAM,
  EntityType.USER,
];

export function EntitySelector({
  orgId,
  entityType,
  entityId,
  onEntityTypeChange,
  onEntityIdChange,
  disabled = false,
  allowCloudAgentScope = false,
}: EntitySelectorProps) {
  // Labels come from the shared map, so this dropdown cannot word the two
  // person-scoped kinds differently from the list that renders them (#4536).
  // Without the cloud-agent kind on offer (the rate-limit form), "User — direct
  // use" would imply a cloud-agents counterpart that doesn't exist there, so
  // that surface keeps the plain "User" label and no bucket help text.
  const entityTypeOptions = (
    allowCloudAgentScope ? [...baseEntityTypes, EntityType.ROOT_USER] : baseEntityTypes
  ).map((value) => ({
    value,
    label:
      !allowCloudAgentScope && value === EntityType.USER ? 'User' : formatEntityType(value),
  }));
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
            // Issue #4511: this MUST send the Cognito sub. Budgets, rate limits
            // and the usage ledger all key `user` entities by the sub, so an id
            // in any other namespace produces a record the engine can never
            // match — a cap that appears configured but never enforces and never
            // shows spend. The previous source (`getCognitoUsers`) supplied the
            // Cognito *username*, which is `GitHub_<github_id>` for anyone
            // onboarded through GitHub and only coincidentally equals the sub for
            // email-signup users, which is why this survived testing.
            const usersResponse = await getOrgUsers(orgId, { pageSize: 100 });
            if (cancelled) return;
            options = usersResponse.items.map((user) => {
              const displayName = user.name ? `${user.name} (${user.email})` : user.email;
              // Members who have never signed in have no sub, so no budget for
              // them could ever be enforced. Show them disabled rather than
              // omitting them, so the absence is explained rather than confusing.
              return user.cognitoSub
                ? { value: user.cognitoSub, label: displayName }
                : {
                    value: user.id,
                    label: `${displayName} — has not signed in yet`,
                    disabled: true,
                  };
            });
            break;
          }

          case EntityType.ROOT_USER: {
            // Issue #4536: the SAME person picker as direct-use above — one list of
            // people, so the two budget kinds cannot disagree about who exists. The
            // difference is which id is submitted: the cloud-agent ledger is keyed by
            // canonical `users.id` (#4300), not by Cognito sub, so sending the sub
            // here would recreate #4511 one ledger over — a cap that exists and
            // matches nothing.
            const usersResponse = await getOrgUsers(orgId, { pageSize: 100 });
            if (cancelled) return;
            options = usersResponse.items.map((user) => ({
              // Never disabled: the canonical id always exists, and agent spend is
              // attributed from the run's lineage rather than a signed-in session, so
              // a member who has not signed in yet still has an enforceable cloud
              // budget. That is the one asymmetry with direct-use.
              value: user.id,
              label: user.name ? `${user.name} (${user.email})` : user.email,
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
        // Which of a person's two spend buckets this cap governs. Without it the
        // labels alone don't tell you which dollars land where (#4536). Budget
        // form only — see the option-label note above.
        helperText={allowCloudAgentScope ? entityTypeHelpText(entityType) : undefined}
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
              // Issue #4511: the old copy suggested `user-123 or user email`,
              // neither of which can ever be a valid user key. Name the forms
              // the server actually accepts. #4536: the accepted input forms are
              // identical for both person-scoped kinds — the server resolves them
              // to whichever key that ledger uses — so one hint serves both.
              entityType === EntityType.USER || entityType === EntityType.ROOT_USER
                ? "Enter the user's Cognito sub, their ADP user ID, or their Cognito username (GitHub_<id>). Email is not accepted."
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
    case EntityType.ROOT_USER:
      // Issue #4511: a Cognito sub is a UUID. The old `user-123 or
      // user@example.com` hint named two forms that can never be valid keys.
      return 'e.g., 8a41f2c0-1b7d-4e5a-9c33-... or GitHub_20402445';
    default:
      return 'Enter entity ID';
  }
}
