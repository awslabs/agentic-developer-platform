/**
 * CLI Setup page (route `/setup`, unchanged).
 *
 * Issue #4159 restructured this page. It was titled "Claude Code Setup" while
 * actually covering two tools, and the reading order was convoluted: Codex was
 * buried inside the Claude Code numbered flow and the download buttons sat below
 * the step that told users to go find them. It is now a common "connect your
 * machine" section, a Claude Code | Codex tab switcher, and a shared verify step
 * — all owned by `SetupInstructions`, which also hosts the download cards and
 * the Connect CLI panel so nothing forward-references a later section.
 */

import { SetupInstructions } from '@/components/setup/SetupInstructions';
import { Card, CardTitle, Alert } from '@/components/ui';
import { useAuth } from '@/hooks/useAuth';


export default function ClaudeSetup() {
  const { user, isAuthenticated } = useAuth();

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          CLI Setup
        </h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          Use Claude Code or Codex on your machine with the platform gateway as the backend.
        </p>
      </div>

      {/* Approval note (Issue #4146 / #4144): an approved-but-org-less user, or one
          whose CLI session outlived their org assignment, gets a 409 on every
          inference call. Make that self-explanatory rather than a support ticket. */}
      <Alert variant="warning" title="Waiting on approval?">
        Until a platform administrator approves your account and assigns it to an organization,
        inference calls are rejected with HTTP 409{' '}
        <code className="font-mono">user_not_assigned_to_org</code> — setup will look correct but
        every request will fail. You can request access from{' '}
        <a
          href="/settings/connections"
          className="underline text-primary-700 dark:text-primary-300"
        >
          Settings → Connections
        </a>
        .
      </Alert>

      {/* Sections 1-3: connect your machine → per-tool tabs → verify.
          Owns ScriptDownloadList and ConnectCliPanel (Issue #4159). */}
      <SetupInstructions />

      {/* Troubleshooting */}
      <Card>
        <CardTitle>Troubleshooting</CardTitle>
        <div className="mt-4 space-y-4 text-sm text-gray-700 dark:text-gray-300">
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              401 Unauthorized — token expired
            </h4>
            <p className="mt-1">
              Your stored refresh token is no longer valid, so the helper cannot mint a token.
              Return to the Connect CLI panel above and re-run{' '}
              <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded font-mono">
                bg-cognito-auth.sh import
              </code>{' '}
              with a freshly revealed token. This applies to both Claude Code and Codex — the same
              helper mints the token for each.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              409 Conflict — pending approval
            </h4>
            <p className="mt-1">
              A{' '}
              <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded font-mono">
                user_not_assigned_to_org
              </code>{' '}
              error means your account is not yet approved and assigned to an organization. Your
              setup is fine — it will start working once an administrator approves you. Nothing to
              reconfigure.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              429 Too Many Requests — rate limit
            </h4>
            <p className="mt-1">
              You've hit your rate limit. Wait a moment and try again, or contact your administrator
              to increase your limits.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              402 Payment Required — budget exceeded
            </h4>
            <p className="mt-1">
              Your requests are being blocked by a budget cap. Contact your organization or
              department administrator to review your budget allocation.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              Connection Issues
            </h4>
            <p className="mt-1">
              Ensure you can reach the Gateway URL from your network. If you're behind a
              corporate firewall, you may need to configure proxy settings.
            </p>
          </div>
        </div>
      </Card>

      {/* Info alert */}
      <Alert variant="info" title="About the platform">
        The platform provides a secure, managed way to access Amazon Bedrock from Claude Code.
        It handles authentication, rate limiting, cost tracking, and usage monitoring automatically.
      </Alert>

      {/* User info if authenticated — reference material, so it sits at the bottom
          rather than between the user and the first setup step (Issue #4159). */}
      {isAuthenticated && user && (
        <Card>
          <CardTitle>Your Access Information</CardTitle>
          <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">
                User ID
              </label>
              <p className="mt-1 font-mono text-sm text-gray-900 dark:text-white">
                {user.id}
              </p>
            </div>
            <div>
              <label className="text-sm font-medium text-gray-500 dark:text-gray-400">
                Role
              </label>
              <p className="mt-1 text-gray-900 dark:text-white capitalize">
                {user.role ? user.role.replace(/_/g, ' ') : '—'}
              </p>
            </div>
            {user.orgId && (
              <div>
                <label className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Organization
                </label>
                <p className="mt-1 font-mono text-sm text-gray-900 dark:text-white">
                  {user.orgId}
                </p>
              </div>
            )}
            {user.deptId && (
              <div>
                <label className="text-sm font-medium text-gray-500 dark:text-gray-400">
                  Department
                </label>
                <p className="mt-1 font-mono text-sm text-gray-900 dark:text-white">
                  {user.deptId}
                </p>
              </div>
            )}
          </div>
        </Card>
      )}

      {/* Support */}
      <Card>
        <CardTitle>Need Help?</CardTitle>
        <p className="mt-2 text-gray-600 dark:text-gray-400">
          If you're experiencing issues not covered above, contact your platform administrator
          or check the Log Viewer to see details about your API requests.
        </p>
      </Card>
    </div>
  );
}
