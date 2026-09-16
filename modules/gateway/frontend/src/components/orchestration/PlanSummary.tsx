import type { PolicyAction, PolicySummary } from '@/types/orchestration';

interface PlanSummaryProps {
  stories: number;
  waves: number;
  gates: number;
  evaluations: number;
  /**
   * The authorized execution policy, when the flow has one (#5128).
   *
   * Optional, and absence renders nothing — see `FlowGraph.execution_policy`. Every
   * flow accepted before policies existed has none permanently, so a placeholder
   * would describe a policy that authorizes nothing on a flow that runs normally.
   */
  policy?: PolicySummary | null;
}

/** Verbs an operator would use, not the wire vocabulary. */
const ACTION_LABELS: Record<PolicyAction, string> = {
  develop: 'write code',
  review: 'review',
  repair: 'fix failures',
  merge: 'merge',
  deploy: 'deploy',
  evaluate: 'conclude evaluations',
};

function actionList(actions: PolicyAction[]): string {
  return actions.map((action) => ACTION_LABELS[action] ?? action).join(', ');
}

/** The authorization's own deadline, in the reader's locale. */
function expiryLabel(expiresAt: string): string {
  const when = new Date(expiresAt);
  return Number.isNaN(when.getTime()) ? expiresAt : when.toLocaleDateString();
}

/**
 * Planned implementation work is distinct from its approval and evaluation steps.
 *
 * When a policy is in force this also answers "what did I authorize?" — the targets
 * it applies to, what agents may do unattended, what still waits for a person, and
 * the limits. That question is otherwise only answerable by reading the accepted
 * plan JSON, which means in practice it is not answered at all.
 *
 * **Autonomous and human-decision actions are rendered as the server split them**
 * (`summarize_policy`), never re-derived here. An action can be both permitted and
 * gated in the source document — "agents may prepare this, a person releases it" —
 * so a client that showed `allowed_actions` verbatim would show `merge` as
 * autonomous on exactly the policy that gated it.
 *
 * Nothing here is a control: it describes authority that was already granted
 * elsewhere. Approving, amending and revoking stay on their existing surfaces.
 */
export function PlanSummary({ stories, waves, gates, evaluations, policy }: PlanSummaryProps) {
  return (
    <div className="text-sm text-gray-700 dark:text-gray-300" data-testid="plan-summary">
      <p className="font-medium">
        {stories} {stories === 1 ? 'story' : 'stories'} across {waves} {waves === 1 ? 'wave' : 'waves'}
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        {gates} approval {gates === 1 ? 'gate' : 'gates'} · {evaluations} {evaluations === 1 ? 'evaluation' : 'evaluations'}
      </p>

      {policy && (
        <div
          className="mt-3 space-y-1 border-l-2 border-gray-200 pl-3 text-xs dark:border-gray-700"
          data-testid="plan-summary-policy"
        >
          <p className="font-medium text-gray-700 dark:text-gray-300">
            You authorized this delivery until {expiryLabel(policy.expires_at)}
          </p>

          <p data-testid="policy-targets">
            {/* Repositories are always present — the server requires at least one, so
                a policy naming none cannot be accepted. Environments are optional and
                empty is the safe default, which is stated rather than left blank:
                "no deployment targets" is a meaningful authorization, and an absent
                line would read as missing information instead. */}
            <span className="text-gray-500 dark:text-gray-400">Where: </span>
            {policy.repository_ids.join(', ')}
            {policy.environment_connection_ids.length > 0
              ? ` · deploys to ${policy.environment_connection_ids.join(', ')}`
              : ' · no deployment targets'}
          </p>

          <p data-testid="policy-autonomous">
            <span className="text-gray-500 dark:text-gray-400">Agents may, without asking: </span>
            {policy.autonomous_actions.length > 0 ? actionList(policy.autonomous_actions) : 'nothing — every action needs a person'}
          </p>

          {/* Rendered only when the policy actually gates something. An empty
              "waits for you: none" line reads as reassurance that a control exists,
              on the policy where none does. */}
          {policy.human_decisions.length > 0 && (
            <p data-testid="policy-human-decisions">
              <span className="text-gray-500 dark:text-gray-400">Always waits for a person: </span>
              {actionList(policy.human_decisions)}
            </p>
          )}

          {policy.machine_accepted_evaluations > 0 && (
            <p data-testid="policy-machine-acceptance">
              {policy.machine_accepted_evaluations}{' '}
              {policy.machine_accepted_evaluations === 1 ? 'evaluation' : 'evaluations'} may be concluded automatically.
              {/* Said explicitly because it is the single most likely thing for a
                  reader to assume wrongly, and the consequence of assuming it is
                  believing a human gate can be satisfied by a machine. */}
              {' '}Approval gates still require you.
            </p>
          )}

          {policy.user_credentials && (
            <div data-testid="policy-user-credentials">
              <p>User credentials retain their configured permissions for {actionList(policy.user_credentials.actions)}.</p>
              {policy.user_credentials.vault_credential_ids.length > 0 && (
                <p>Vault credentials: {policy.user_credentials.vault_credential_ids.join(', ')}</p>
              )}
              {policy.user_credentials.aws_role_arns.length > 0 && (
                <p>AWS roles: {policy.user_credentials.aws_role_arns.join(', ')}</p>
              )}
              <p>Cancellation or plan expiry stops new credential requests. Credentials already issued follow the provider’s expiry and revocation rules.</p>
              <p>These credentials may permit additional actions at the provider. ADP still requires the task’s approvals.</p>
            </div>
          )}

          <p data-testid="policy-limits">
            {/* `max_spend_usd` is rendered as the server sent it. It is a Decimal
                server-side, and formatting it through a JS float would reintroduce
                the rounding the cost model avoids. */}
            <span className="text-gray-500 dark:text-gray-400">Limits: </span>
            up to ${policy.limits.max_spend_usd} total · {policy.limits.max_concurrent_actions} at a time ·{' '}
            {policy.limits.max_attempts_per_node} {policy.limits.max_attempts_per_node === 1 ? 'attempt' : 'attempts'} per step
          </p>
        </div>
      )}
    </div>
  );
}
