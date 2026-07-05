/**
 * PostInstallPanel — "What's next" guidance panel shown on the Connections page
 * after a successful GitHub App installation.
 *
 * Issue #2984: Post-install success state for onboarding guidance.
 */

interface PostInstallPanelProps {
  /** Number of repos connected in this installation */
  repoCount?: number;
  /** The sign-in URL to share with team members */
  signInUrl?: string;
}

export function PostInstallPanel({ repoCount, signInUrl }: PostInstallPanelProps) {
  const displayUrl = signInUrl || window.location.origin;

  const handleCopyUrl = () => {
    navigator.clipboard.writeText(displayUrl);
  };

  return (
    <div className="rounded-lg border border-green-200 bg-green-50 p-6 dark:border-green-800 dark:bg-green-900/20">
      <div className="flex items-start gap-3">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-green-100 dark:bg-green-900/40">
          <svg
            className="h-5 w-5 text-green-600 dark:text-green-400"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        </div>
        <div className="flex-1">
          <h3 className="text-lg font-semibold text-green-900 dark:text-green-100">
            Org connected{repoCount != null ? ` — ${repoCount} repo${repoCount !== 1 ? 's' : ''}` : ''}
          </h3>
          <p className="mt-1 text-sm text-green-700 dark:text-green-300">
            Your GitHub organization is now linked. Here&apos;s what to do next:
          </p>

          <div className="mt-4 space-y-3">
            {/* Step 1: Share sign-in URL */}
            <div className="flex items-start gap-3 rounded-md bg-white/60 p-3 dark:bg-gray-800/40">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-green-200 text-xs font-bold text-green-800 dark:bg-green-800 dark:text-green-200">
                1
              </span>
              <div className="flex-1">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  Share the sign-in URL with your team
                </p>
                <div className="mt-1.5 flex items-center gap-2">
                  <code className="rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                    {displayUrl}
                  </code>
                  <button
                    onClick={handleCopyUrl}
                    className="rounded px-2 py-0.5 text-xs font-medium text-green-700 hover:bg-green-100 dark:text-green-300 dark:hover:bg-green-900/40"
                    title="Copy URL"
                  >
                    Copy
                  </button>
                </div>
              </div>
            </div>

            {/* Step 2: Mention agent */}
            <div className="flex items-start gap-3 rounded-md bg-white/60 p-3 dark:bg-gray-800/40">
              <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-green-200 text-xs font-bold text-green-800 dark:bg-green-800 dark:text-green-200">
                2
              </span>
              <div>
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                  Mention <code className="rounded bg-gray-100 px-1 text-xs dark:bg-gray-700">@agent-developer</code> on a connected repo
                </p>
                <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                  Open an issue or PR comment and mention the agent to start your first task.
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
