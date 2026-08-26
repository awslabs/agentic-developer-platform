/**
 * SetupInstructions — how to point Claude Code (and Codex) at this gateway.
 *
 * Issue #4146 rewrote this end-to-end. The previous content documented an
 * AWS-SSO + `bg-auth.sh` flow that no longer works: it predated both the
 * Cognito helper and GitHub login, told users to write `~/.claude/config.json`
 * with an `apiBaseUrl` key that exists in no file under `cli/`, and shipped
 * unresolved `https://your-gateway-url/v1` placeholders.
 *
 * Two invariants worth preserving on edit:
 * - The base URL is resolved at runtime from the real origin. Never a placeholder.
 * - The settings snippets are `JSON.stringify`'d from objects, so the rendered
 *   text cannot drift from the shape asserted in tests (or from `cli/examples/`).
 */

import { Card, CardTitle, CopyButton } from '@/components/ui';
import { getGatewayBaseUrl } from '@/utils/gatewayUrl';

/** Anthropic-format settings — mirrors cli/examples/claude-settings-cognito.json */
export function buildAnthropicSettings(baseUrl: string) {
  return {
    env: {
      ANTHROPIC_BASE_URL: baseUrl,
    },
    apiKeyHelper: 'bash ~/bin/bg-cognito-auth.sh token',
    // 55 min — matches `cmd_token`'s refresh-on-expiry behaviour. NOT the
    // 300000 from the legacy cli/claude-settings.example.json (bg-auth.sh era).
    apiKeyHelperTtlMs: 3300000,
    permissions: { allow: ['WebSearch', 'WebFetch'] },
    model: 'global.anthropic.claude-opus-4-6-v1',
  };
}

/** Bedrock-format settings — mirrors cli/examples/claude-settings-bedrock-gateway.json */
export function buildBedrockSettings(baseUrl: string) {
  return {
    env: {
      AWS_REGION: 'us-east-1',
      CLAUDE_CODE_USE_BEDROCK: '1',
      CLAUDE_CODE_SKIP_BEDROCK_AUTH: '1',
      ANTHROPIC_BEDROCK_BASE_URL: baseUrl,
    },
    apiKeyHelper: 'bash ~/bin/bg-cognito-auth.sh token',
    apiKeyHelperTtlMs: 3300000,
    permissions: { allow: ['WebSearch', 'WebFetch'] },
    model: 'global.anthropic.claude-opus-4-6-v1',
  };
}

/**
 * Default port of the `serve` proxy — DEFAULT_PROXY_PORT in cli/bg-cognito-auth.sh.
 * It must match the port in the config.toml `base_url` below, so both come from here.
 */
export const CODEX_PROXY_PORT = 9191;

/**
 * Codex provider config — mirrors cli/README.md §"Using Codex: zero-touch auth
 * with serve", Step 3. Deliberately points at the local proxy, NOT at the gateway
 * directly: Codex has no apiKeyHelper hook, so a token put here goes stale in an
 * hour (Issue #4156).
 */
export function buildCodexConfigToml(port: number = CODEX_PROXY_PORT): string {
  return `model = "openai.gpt-5.6-sol"
model_provider = "adp-gateway"

[model_providers.adp-gateway]
name = "ADP Gateway (local auth proxy)"
base_url = "http://127.0.0.1:${port}/openai/v1"
wire_api = "responses"
env_key = "ADP_GATEWAY_DUMMY"`;
}

function CodeSnippet({ children, copyValue }: { children: string; copyValue?: string }) {
  return (
    <div className="relative mt-2">
      <pre className="p-3 pr-20 bg-gray-100 dark:bg-gray-800 rounded-lg text-sm overflow-x-auto">
        {children}
      </pre>
      <div className="absolute top-2 right-2">
        <CopyButton value={copyValue ?? children} />
      </div>
    </div>
  );
}

/** Step numbers are derived from array order — a reorder cannot mis-number them. */
function Step({ index, title, children }: { index: number; title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="font-semibold text-gray-900 dark:text-white flex items-center gap-2">
        <span className="flex items-center justify-center w-6 h-6 rounded-full bg-primary-100 dark:bg-primary-900 text-primary-700 dark:text-primary-300 text-sm">
          {index + 1}
        </span>
        {title}
      </h3>
      <div className="mt-2 ml-8 text-sm space-y-2">{children}</div>
    </div>
  );
}

const CODE = 'bg-gray-100 dark:bg-gray-700 px-1 rounded font-mono';

export function SetupInstructions() {
  const baseUrl = getGatewayBaseUrl();
  const anthropicSettings = JSON.stringify(buildAnthropicSettings(baseUrl), null, 2);
  const bedrockSettings = JSON.stringify(buildBedrockSettings(baseUrl), null, 2);
  const codexConfigToml = buildCodexConfigToml();

  const steps: { title: string; body: React.ReactNode }[] = [
    {
      title: 'Prerequisites',
      body: (
        <ul className="list-disc space-y-1">
          <li>
            Claude Code installed (
            <code className={CODE}>npm install -g @anthropic-ai/claude-code</code>)
          </li>
          <li>
            <code className={CODE}>curl</code> and <code className={CODE}>jq</code> available on
            your PATH
          </li>
          <li>Signed in to this dashboard with GitHub — you already are, or you could not see this page</li>
        </ul>
      ),
    },
    {
      title: 'Download the helper script',
      body: (
        <>
          <p>
            Grab <code className={CODE}>bg-cognito-auth.sh</code> from the Downloads section below,
            then put it on your PATH and make it executable:
          </p>
          <CodeSnippet>{`mkdir -p ~/bin
mv ~/Downloads/bg-cognito-auth.sh ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh`}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            This is the script Claude Code calls to mint a fresh token on every request.
          </p>
        </>
      ),
    },
    {
      title: 'Connect the CLI',
      body: (
        <p>
          Use the <strong>Connect CLI</strong> panel above: run the{' '}
          <code className={CODE}>import</code> command it shows and paste your refresh token when
          prompted. This seeds the CLI from the session this browser already established — you have
          no Cognito password to type, because signing in with GitHub never created one.
        </p>
      ),
    },
    {
      title: 'Configure Claude Code',
      body: (
        <>
          <p>
            Write this to <code className={CODE}>~/.claude/settings.json</code>. The base URL is
            this deployment's real gateway URL — already filled in for you, and it takes no{' '}
            <code className={CODE}>/v1</code> suffix (Claude Code appends the API path itself):
          </p>
          <CodeSnippet>{anthropicSettings}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            Prefer to speak the Bedrock API format instead? Use this variant — same helper, same
            base URL:
          </p>
          <details className="mt-1">
            <summary className="cursor-pointer text-primary-600 dark:text-primary-400">
              Bedrock-format settings.json
            </summary>
            <CodeSnippet>{bedrockSettings}</CodeSnippet>
          </details>
        </>
      ),
    },
    {
      title: 'Run Claude Code',
      body: (
        <>
          <CodeSnippet>claude</CodeSnippet>
          <p>
            Claude Code calls <code className={CODE}>bg-cognito-auth.sh token</code> automatically
            via <code className={CODE}>apiKeyHelper</code> and refreshes the token on its own — you
            should not need to run the helper by hand again.
          </p>
        </>
      ),
    },
    {
      title: 'Using Codex instead? Zero-touch auth with serve',
      body: (
        <>
          <p>
            Codex reads its credential from an env var once at launch and never asks again — so a
            manually exported token works for about an hour, then every request 401s until you
            restart it. <code className={CODE}>serve</code> closes that gap: it runs a small
            localhost proxy that injects a freshly-refreshed token into every request, so you
            authenticate once and never touch tokens again.
          </p>
          <p>
            Install <strong>both</strong> files from the Downloads section below —{' '}
            <code className={CODE}>bg-gateway-proxy.py</code> must sit next to{' '}
            <code className={CODE}>bg-cognito-auth.sh</code>, because{' '}
            <code className={CODE}>serve</code> looks for its sibling:
          </p>
          <CodeSnippet>{`mv ~/Downloads/bg-cognito-auth.sh ~/Downloads/bg-gateway-proxy.py ~/bin/
chmod +x ~/bin/bg-cognito-auth.sh`}</CodeSnippet>
          <p>
            Add this to <code className={CODE}>~/.codex/config.toml</code> (the helper deliberately
            does not write this file for you — it is yours):
          </p>
          <CodeSnippet>{codexConfigToml}</CodeSnippet>
          <p>Then start the proxy and, in another terminal, Codex:</p>
          <CodeSnippet>{`~/bin/bg-cognito-auth.sh serve          # foreground; Ctrl-C to stop
ADP_GATEWAY_DUMMY=unused codex         # in a second terminal`}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            Leave the proxy running as long as you like — refresh happens per request, behind the
            scenes. Codex requires <code className={CODE}>env_key</code> to name an existing env var
            but never validates its value; the proxy discards whatever arrives and injects the real
            token.
          </p>
        </>
      ),
    },
    {
      title: 'Verify it works',
      body: (
        <p>
          Ask Claude Code anything, then open the <strong>Log Viewer</strong> in this dashboard. Your
          request should appear there within a few seconds — that confirms traffic is flowing through
          the gateway and being metered against your account. If it does not, see Troubleshooting
          below.
        </p>
      ),
    },
  ];

  return (
    <Card>
      <CardTitle>Setup Instructions</CardTitle>
      <div className="mt-4 space-y-6 text-gray-700 dark:text-gray-300">
        {steps.map((step, index) => (
          <Step key={step.title} index={index} title={step.title}>
            {step.body}
          </Step>
        ))}
      </div>
    </Card>
  );
}
