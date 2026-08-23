/**
 * VerificationRow — one tri-state onboarding health check.
 *
 * Issue #4016: GitHub App onboarding reported success while login, webhooks, or
 * routing were broken. These rows are the user-visible half of the fix: they
 * report what was actually verified.
 *
 * Tri-state is load-bearing:
 *   true  → green, verified working
 *   false → red, verified broken (authoritative absence)
 *   null/undefined → amber, could not determine
 *
 * An unknown check must NEVER render red. A false-negative red sends operators
 * to "fix" something that was never broken, which is how the previous
 * fail-soft behaviour got its bad reputation in reverse.
 */

export type CheckState = boolean | null | undefined;

interface VerificationRowProps {
  /** Short label, e.g. "Agent credentials". */
  label: string;
  state: CheckState;
  /** Shown under the label when the check is false — what actually breaks. */
  brokenDetail: string;
  /** Shown under the label when the check is unknown — why we could not tell. */
  unknownDetail?: string;
}

export function VerificationRow({
  label,
  state,
  brokenDetail,
  unknownDetail = 'The check could not be completed, so this may or may not be working.',
}: VerificationRowProps) {
  const isOk = state === true;
  const isBroken = state === false;

  const icon = isOk ? '✓' : isBroken ? '✕' : '?';
  const iconClass = isOk
    ? 'text-green-600 dark:text-green-400'
    : isBroken
      ? 'text-red-600 dark:text-red-400'
      : 'text-amber-600 dark:text-amber-400';

  return (
    <li className="flex items-start gap-2 text-sm">
      <span className={`mt-0.5 font-semibold ${iconClass}`} aria-hidden="true">
        {icon}
      </span>
      <span className="sr-only">
        {isOk ? 'Working: ' : isBroken ? 'Broken: ' : 'Unknown: '}
      </span>
      <span>
        <span className="text-gray-800 dark:text-gray-200">{label}</span>
        {!isOk && (
          <span className="block text-gray-600 dark:text-gray-400">
            {isBroken ? brokenDetail : unknownDetail}
          </span>
        )}
      </span>
    </li>
  );
}

/**
 * Whether a set of checks contains anything worth surfacing.
 *
 * Deliberately treats unknown (null/undefined) as worth surfacing: "we could not
 * verify this" is exactly the information this issue exists to stop hiding.
 */
export function hasUnhealthyCheck(states: CheckState[]): boolean {
  return states.some((s) => s !== true);
}
