/**
 * FreeTierBanner — shows free-tier status when user is on the adp-default tenant.
 *
 * Issue #466: adp-default fallback tenant for personal / unclaimed installations.
 */

interface FreeTierBannerProps {
  /** Current monthly spend in USD (tracked, not blocked in v1) */
  currentSpendUsd?: number;
  /** Monthly budget limit in USD */
  budgetLimitUsd?: number;
}

const ADP_DEFAULT_BUDGET_USD = 5;

export function FreeTierBanner({
  currentSpendUsd = 0,
  budgetLimitUsd = ADP_DEFAULT_BUDGET_USD,
}: FreeTierBannerProps) {
  return (
    <div className="mb-6 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 dark:border-blue-800 dark:bg-blue-900/20">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-lg" aria-hidden="true">
            🎁
          </span>
          <span className="text-sm font-medium text-blue-900 dark:text-blue-100">
            You&apos;re on the Free tier.
          </span>
          <span className="text-sm text-blue-700 dark:text-blue-300">
            ${currentSpendUsd.toFixed(2)} of ${budgetLimitUsd} used this month
          </span>
        </div>
        <a
          href="mailto:sales@adp.dev?subject=Upgrade%20from%20Free%20Tier"
          className="text-sm font-medium text-blue-700 underline hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-100"
        >
          Upgrade to a paid tenant →
        </a>
      </div>
    </div>
  );
}
