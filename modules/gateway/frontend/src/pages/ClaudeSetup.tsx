import { SetupInstructions } from '@/components/setup/SetupInstructions';
import { ScriptDownloadList } from '@/components/setup/ScriptDownload';
import { Card, CardTitle, Alert } from '@/components/ui';
import { useAuth } from '@/hooks/useAuth';


export default function ClaudeSetup() {
  const { user, isAuthenticated } = useAuth();

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Claude Code Setup
        </h1>
        <p className="text-gray-500 dark:text-gray-400 mt-1">
          Configure Claude Code to use the platform
        </p>
      </div>

      {/* User info if authenticated */}
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

      {/* Info alert */}
      <Alert variant="info" title="About the platform">
        The platform provides a secure, managed way to access Amazon Bedrock from Claude Code.
        It handles authentication, rate limiting, cost tracking, and usage monitoring automatically.
      </Alert>

      {/* Setup instructions */}
      <SetupInstructions />

      {/* Script downloads */}
      <ScriptDownloadList />

      {/* Troubleshooting */}
      <Card>
        <CardTitle>Troubleshooting</CardTitle>
        <div className="mt-4 space-y-4 text-sm text-gray-700 dark:text-gray-300">
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              Authentication Errors
            </h4>
            <p className="mt-1">
              If you see "401 Unauthorized" errors, your credentials may have expired.
              Run the <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">bg-auth</code> script
              again to refresh your session.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              Rate Limit Errors
            </h4>
            <p className="mt-1">
              If you see "429 Too Many Requests" errors, you've hit your rate limit.
              Wait a moment and try again, or contact your administrator to increase your limits.
            </p>
          </div>
          <div>
            <h4 className="font-semibold text-gray-900 dark:text-white">
              Budget Exceeded
            </h4>
            <p className="mt-1">
              If your requests are being blocked due to budget limits, contact your
              organization or department administrator to review your budget allocation.
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
