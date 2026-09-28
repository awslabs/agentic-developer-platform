/**
 * ConnectCliPanel — seed the CLI from the browser session. Issue #4146.
 *
 * Signing in with GitHub never creates a Cognito password, so the password login
 * flow cannot work for those users. `adp import` (shipped in #4145, fronted by the
 * `adp` CLI in #4852) covers them: it takes the refresh token the SPA already holds
 * and writes a working ~/.bedrock-gateway/ config from it.
 *
 * Security shape — do not "simplify" these away:
 * - The token is NEVER interpolated into the copyable command. `import` reads it
 *   from stdin precisely because an argv-embedded credential lands in
 *   ~/.bash_history and in `ps` output.
 * - It is masked until an explicit Reveal click, so the page is safe to have open
 *   while screen-sharing.
 */

import { useState } from 'react';
import { Card, CardTitle, Button, Alert, CopyButton } from '@/components/ui';
import { getRefreshToken } from '@/services/auth';
import { getGatewayBaseUrl } from '@/utils/gatewayUrl';

export function ConnectCliPanel() {
  const [revealed, setRevealed] = useState(false);
  const refreshToken = getRefreshToken();
  const baseUrl = getGatewayBaseUrl();

  const importCommand = `adp import --gateway-url ${baseUrl}`;

  // AuthCallback guards only on id/access tokens and stores `refreshToken || ''`,
  // so a fully logged-in user can legitimately hold no refresh token. Showing the
  // command anyway would send them to a prompt they cannot satisfy.
  if (!refreshToken) {
    return (
      <Card>
        <CardTitle>Connect CLI</CardTitle>
        <Alert variant="warning" title="Sign out and sign in again to enable CLI setup">
          This browser session has no refresh token, so there is nothing to hand the CLI yet. Sign
          out and sign back in, then return to this page.
        </Alert>
      </Card>
    );
  }

  return (
    <Card>
      <CardTitle>Connect CLI</CardTitle>
      <div className="mt-4 space-y-4 text-sm text-gray-700 dark:text-gray-300">
        <p>
          Run this on your machine. It prompts for the refresh token below with hidden input — paste
          it at the prompt.
        </p>

        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
            Command
          </label>
          <div className="flex gap-2">
            <code
              data-testid="import-command"
              className="flex-1 bg-gray-100 dark:bg-gray-800 px-3 py-2 rounded font-mono break-all"
            >
              {importCommand}
            </code>
            <CopyButton value={importCommand} label="Copy" />
          </div>
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 dark:text-gray-400 mb-1">
            Refresh token
          </label>
          <div className="flex gap-2">
            <code
              data-testid="refresh-token"
              className="flex-1 bg-gray-100 dark:bg-gray-800 px-3 py-2 rounded font-mono break-all"
            >
              {revealed ? refreshToken : '••••••••••••••••••••••••••••••••'}
            </code>
            <Button variant="outline" size="sm" onClick={() => setRevealed((r) => !r)}>
              {revealed ? 'Hide' : 'Reveal'}
            </Button>
            <CopyButton value={refreshToken} label="Copy" />
          </div>
        </div>

        <Alert variant="warning" title="This is a long-lived credential">
          Treat the refresh token like a password: never paste it into a chat, a URL, or a shared
          terminal. It is deliberately kept out of the command above so it does not land in your
          shell history.
        </Alert>

        <p className="text-gray-500 dark:text-gray-400">
          The token lives in this browser tab only (<code className="font-mono">sessionStorage</code>
          ) and is cleared when the tab closes. If you need it again later, sign in again to get a
          fresh one.
        </p>
      </div>
    </Card>
  );
}
