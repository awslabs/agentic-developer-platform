/**
 * NoOrgBanner — shown to users on a personal/free-tier tenant who have no
 * org connected. Displays different CTAs based on deployment App visibility.
 *
 * Issue #2984: No-org banner for onboarding guidance.
 *
 * - Private App deployment: "Ask your org owner to connect" + copyable connect link
 * - Public App deployment: "Connect your repos" CTA linking to the App install page
 */

interface NoOrgBannerProps {
  /** Whether the GitHub App is public (anyone can install). False = private (org-only). */
  isPublicApp?: boolean;
  /** The GitHub App install URL (for public-app CTA). */
  installUrl?: string;
}

export function NoOrgBanner({ isPublicApp = false, installUrl }: NoOrgBannerProps) {
  const connectUrl = `${window.location.origin}/settings/connections`;

  const handleCopyLink = () => {
    navigator.clipboard.writeText(connectUrl);
  };

  return (
    <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 px-4 py-4 dark:border-amber-800 dark:bg-amber-900/20">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 text-lg" aria-hidden="true">
          &#128279;
        </span>
        <div className="flex-1">
          {isPublicApp ? (
            <>
              <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
                Connect your repos to get started
              </p>
              <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
                Install the GitHub App on your account or organization to enable agent triggers.
              </p>
              {installUrl ? (
                <a
                  href={installUrl}
                  className="mt-2 inline-flex items-center rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 dark:bg-amber-700 dark:hover:bg-amber-600"
                >
                  Connect your repos
                </a>
              ) : (
                <a
                  href="/settings/connections"
                  className="mt-2 inline-flex items-center rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-700 dark:bg-amber-700 dark:hover:bg-amber-600"
                >
                  Go to Connections
                </a>
              )}
            </>
          ) : (
            <>
              <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
                Ask your org owner to connect this platform
              </p>
              <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">
                Your organization admin needs to install the GitHub App to enable agent features
                for your team.
              </p>
              <div className="mt-2 flex items-center gap-2">
                <code className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800 dark:bg-amber-900/40 dark:text-amber-200">
                  {connectUrl}
                </code>
                <button
                  onClick={handleCopyLink}
                  className="rounded px-2 py-0.5 text-xs font-medium text-amber-700 hover:bg-amber-100 dark:text-amber-300 dark:hover:bg-amber-900/40"
                  title="Copy link"
                >
                  Copy
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
