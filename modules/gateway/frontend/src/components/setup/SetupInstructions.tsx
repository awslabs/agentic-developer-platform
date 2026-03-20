import { Card, CardTitle } from '@/components/ui';

export function SetupInstructions() {
  return (
    <Card>
      <CardTitle>Setup Instructions</CardTitle>
      <div className="mt-4 space-y-6 text-gray-700 dark:text-gray-300">
        {/* Step 1 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              1
            </span>
            Prerequisites
          </h3>
          <ul className="mt-2 ml-8 list-disc space-y-1 text-sm">
            <li>AWS CLI v2 installed and configured</li>
            <li>Access to AWS SSO (IAM Identity Center)</li>
            <li>Claude Code installed</li>
          </ul>
        </div>

        {/* Step 2 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              2
            </span>
            Download the Helper Script
          </h3>
          <p className="mt-2 ml-8 text-sm">
            Download the appropriate <code className="bg-gray-100 dark:bg-gray-700 px-1 rounded">bg-auth</code> script for your platform from the Downloads section below.
          </p>
        </div>

        {/* Step 3 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              3
            </span>
            Make the Script Executable (Linux/macOS)
          </h3>
          <pre className="mt-2 ml-8 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
            chmod +x bg-auth.sh
          </pre>
        </div>

        {/* Step 4 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              4
            </span>
            Configure AWS SSO
          </h3>
          <p className="mt-2 ml-8 text-sm mb-2">
            Configure AWS SSO if you haven't already:
          </p>
          <pre className="ml-8 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
{`aws configure sso
# Follow the prompts to configure:
# - SSO Start URL: https://your-org.awsapps.com/start
# - SSO Region: us-east-1
# - Account and Role selection`}
          </pre>
        </div>

        {/* Step 5 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              5
            </span>
            Run the Authentication Script
          </h3>
          <p className="mt-2 ml-8 text-sm mb-2">
            Linux/macOS:
          </p>
          <pre className="ml-8 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
{`./bg-auth.sh --profile your-sso-profile`}
          </pre>
          <p className="mt-3 ml-8 text-sm mb-2">
            Windows PowerShell:
          </p>
          <pre className="ml-8 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
{`.\\bg-auth.ps1 -Profile your-sso-profile`}
          </pre>
        </div>

        {/* Step 6 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              6
            </span>
            Configure Claude Code
          </h3>
          <p className="mt-2 ml-8 text-sm mb-2">
            Set the Bedrock Gateway as your API endpoint in Claude Code:
          </p>
          <pre className="ml-8 p-3 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
{`# Set environment variables
export ANTHROPIC_BASE_URL="https://your-gateway-url/v1"

# Or configure in ~/.claude/config.json
{
  "apiBaseUrl": "https://your-gateway-url/v1"
}`}
          </pre>
        </div>

        {/* Step 7 */}
        <div>
          <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
            <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
              7
            </span>
            Test the Connection
          </h3>
          <p className="mt-2 ml-8 text-sm">
            Run Claude Code and verify it connects through the Bedrock Gateway. You should see your requests logged in the Log Viewer.
          </p>
        </div>
      </div>
    </Card>
  );
}
