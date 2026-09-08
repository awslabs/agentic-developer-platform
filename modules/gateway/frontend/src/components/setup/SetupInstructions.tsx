/**
 * SetupInstructions — how to point Claude Code or Codex at this gateway.
 *
 * Issue #4146 rewrote the content end-to-end (the previous AWS-SSO +
 * `bg-auth.sh` flow no longer worked). Issue #4159 made it tabbed. This
 * revision restructures around two principles:
 *
 * 1. **Each tab is fully self-contained.** A Codex user never reads a Claude
 *    Code prerequisite, nothing references a section "above", and each tab
 *    numbers its steps 1..N. The previous split (common section + tabs that
 *    continue its numbering) interleaved unnumbered panels between numbered
 *    steps and told Codex users to re-`mv` a file an earlier step had already
 *    moved. Shared content is shared at the component level instead, so the
 *    tabs cannot drift apart.
 *
 * 2. **Sign-in is `login --web` — no credential ever passes through a human.**
 *    The CLI opens the browser, the user clicks Approve, done. The old
 *    "Reveal refresh token and paste it" panel remains only as a collapsed
 *    fallback for headless machines.
 *
 * Three invariants worth preserving on edit:
 * - The base URL is resolved at runtime from the real origin. Never a placeholder.
 * - The settings snippets are `JSON.stringify`'d from objects, so the rendered
 *   text cannot drift from the shape asserted in tests (or from `cli/examples/`).
 * - Step numbers are derived from array order — a reorder cannot mis-number them.
 */

import { Card, CardTitle, CopyButton, Tabs, TabsList, Tab, TabPanel } from '@/components/ui';
import { getGatewayBaseUrl } from '@/utils/gatewayUrl';
import { ScriptDownloadList } from '@/components/setup/ScriptDownload';
import { ConnectCliPanel } from '@/components/setup/ConnectCliPanel';

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

/** One curl per file, into ~/bin — no browser download, no mv from ~/Downloads. */
export function buildInstallCommand(baseUrl: string, files: string[]): string {
  const curls = files.map((file) => `curl -fsSL ${baseUrl}/cli/${file} -o ~/bin/${file}`);
  return ['mkdir -p ~/bin', ...curls, 'chmod +x ~/bin/bg-cognito-auth.sh'].join('\n');
}

/**
 * A paste-ready command that writes a config file, instead of "add this to
 * the file" — users should not need to know how to drive an editor to get set
 * up. The quoted 'EOF' heredoc delimiter keeps the shell from expanding
 * anything inside the content.
 */
export function buildFileWriteCommand(path: string, content: string): string {
  const dir = path.substring(0, path.lastIndexOf('/'));
  return `mkdir -p ${dir}\ncat > ${path} << 'EOF'\n${content}\nEOF`;
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

function SectionHeading({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h2 className="text-lg font-semibold text-gray-900 dark:text-white">{title}</h2>
      <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{children}</p>
    </div>
  );
}

const CODE = 'bg-gray-100 dark:bg-gray-700 px-1 rounded font-mono';

interface SetupStep {
  title: string;
  body: React.ReactNode;
}

/** Shared tool prerequisites — the helper script's own dependencies. */
function HelperPrereqItems() {
  return (
    <>
      <li>
        <code className={CODE}>curl</code>, <code className={CODE}>jq</code> and the{' '}
        <code className={CODE}>aws</code> CLI v2 available on your PATH
      </li>
      <li>Signed in to this dashboard — you already are, or you could not see this page</li>
    </>
  );
}

/**
 * The sign-in step, identical in both tabs (rendered from this one component
 * so the tabs cannot drift). Primary path: `login --web` — browser approval,
 * nothing displayed, nothing pasted. Fallbacks collapsed below it.
 */
function SignInStep({ baseUrl }: { baseUrl: string }) {
  const loginCommand = `~/bin/bg-cognito-auth.sh login --web --gateway-url ${baseUrl}`;
  return (
    <>
      <p>Run this, then click Approve in the browser tab it opens:</p>
      <CodeSnippet>{loginCommand}</CodeSnippet>
      <p className="text-gray-500 dark:text-gray-400">
        The approval page shows the same short code as your terminal — confirm they match and
        approve. Your machine receives a short-lived credential that refreshes itself in the
        background; you never see or copy a token. Re-approving takes one click whenever it fully
        expires (about a day of inactivity).
      </p>
      <details className="mt-1">
        <summary className="cursor-pointer text-primary-600 dark:text-primary-400">
          On a headless machine (SSH, no browser)? Use a pasted token instead
        </summary>
        <div className="mt-2 space-y-2">
          <p>
            Where no browser can open, seed the CLI from this browser session: run the{' '}
            <code className={CODE}>import</code> command below and paste the revealed refresh token
            when prompted.
          </p>
          <ConnectCliPanel />
        </div>
      </details>
      <details className="mt-1">
        <summary className="cursor-pointer text-primary-600 dark:text-primary-400">
          Have a Cognito password? (accounts not created via GitHub sign-in)
        </summary>
        <p className="mt-2">
          Use the interactive password flow instead:{' '}
          <code className={CODE}>~/bin/bg-cognito-auth.sh login --gateway-url {baseUrl}</code>
        </p>
      </details>
    </>
  );
}

/** The install step; Codex needs a second file, Claude Code does not. */
function InstallStep({ baseUrl, files, note }: { baseUrl: string; files: string[]; note?: React.ReactNode }) {
  return (
    <>
      <p>This fetches the helper straight from this gateway — nothing to download by hand:</p>
      <CodeSnippet>{buildInstallCommand(baseUrl, files)}</CodeSnippet>
      {note}
      <details className="mt-1">
        <summary className="cursor-pointer text-primary-600 dark:text-primary-400">
          Prefer downloading in the browser?
        </summary>
        <div className="mt-2">
          <ScriptDownloadList files={files} />
          <p className="mt-2 text-gray-500 dark:text-gray-400">
            Save the {files.length > 1 ? 'files' : 'file'} to <code className={CODE}>~/bin</code>{' '}
            and <code className={CODE}>chmod +x ~/bin/bg-cognito-auth.sh</code>.
          </p>
        </div>
      </details>
    </>
  );
}

export function SetupInstructions() {
  const baseUrl = getGatewayBaseUrl();
  const anthropicSettings = JSON.stringify(buildAnthropicSettings(baseUrl), null, 2);
  const bedrockSettings = JSON.stringify(buildBedrockSettings(baseUrl), null, 2);
  const codexConfigToml = buildCodexConfigToml();

  // --- Claude Code tab: one self-contained flow, numbered from 1 -------------
  const claudeCodeSteps: SetupStep[] = [
    {
      title: 'Prerequisites',
      body: (
        <ul className="list-disc space-y-1">
          <li>
            Claude Code installed (
            <code className={CODE}>npm install -g @anthropic-ai/claude-code</code>)
          </li>
          <HelperPrereqItems />
        </ul>
      ),
    },
    {
      title: 'Install the helper script',
      body: <InstallStep baseUrl={baseUrl} files={['bg-cognito-auth.sh']} />,
    },
    {
      title: 'Sign in',
      body: <SignInStep baseUrl={baseUrl} />,
    },
    {
      title: 'Configure Claude Code',
      body: (
        <>
          <p>
            Run this — it writes <code className={CODE}>~/.claude/settings.json</code> for you. The
            base URL is this deployment's real gateway URL, already filled in (it takes no{' '}
            <code className={CODE}>/v1</code> suffix — Claude Code appends the API path itself):
          </p>
          <CodeSnippet>{buildFileWriteCommand('~/.claude/settings.json', anthropicSettings)}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            This replaces an existing <code className={CODE}>settings.json</code> — if you already
            have one you care about, merge the JSON shown above into it instead.
          </p>
          <details className="mt-1">
            <summary className="cursor-pointer text-primary-600 dark:text-primary-400">
              Prefer the Bedrock API format? (same helper, same base URL)
            </summary>
            <CodeSnippet>{buildFileWriteCommand('~/.claude/settings.json', bedrockSettings)}</CodeSnippet>
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
  ];

  // --- Codex tab: one self-contained flow, numbered from 1 -------------------
  const codexSteps: SetupStep[] = [
    {
      title: 'Prerequisites',
      body: (
        <ul className="list-disc space-y-1">
          <li>Codex CLI installed</li>
          <li>
            <code className={CODE}>python3</code> (stdlib only — macOS and Linux ship it)
          </li>
          <HelperPrereqItems />
        </ul>
      ),
    },
    {
      title: 'Install the helper script and the local proxy',
      body: (
        <InstallStep
          baseUrl={baseUrl}
          files={['bg-cognito-auth.sh', 'bg-gateway-proxy.py']}
          note={
            <p className="text-gray-500 dark:text-gray-400">
              Codex needs both files: it reads its credential once at launch and never asks again,
              so <code className={CODE}>serve</code> runs a small localhost proxy (
              <code className={CODE}>bg-gateway-proxy.py</code>, which must sit next to the helper)
              that injects a freshly-refreshed token into every request.
            </p>
          }
        />
      ),
    },
    {
      title: 'Sign in',
      body: <SignInStep baseUrl={baseUrl} />,
    },
    {
      title: 'Configure Codex',
      body: (
        <>
          <p>
            Run this — it writes <code className={CODE}>~/.codex/config.toml</code> for you:
          </p>
          <CodeSnippet>{buildFileWriteCommand('~/.codex/config.toml', codexConfigToml)}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            This replaces an existing <code className={CODE}>config.toml</code> — if you already use
            Codex, merge these lines into yours instead, keeping the two{' '}
            <code className={CODE}>model</code> lines above any <code className={CODE}>[section]</code>{' '}
            header.
          </p>
          <p className="text-gray-500 dark:text-gray-400">
            Switching models with Codex's in-app picker just works — the proxy adds the{' '}
            <code className={CODE}>openai.</code> prefix the gateway expects if the picker writes a
            bare model name.
          </p>
        </>
      ),
    },
    {
      title: 'Start the proxy, then Codex',
      body: (
        <>
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
  ];

  const renderToolSteps = (steps: SetupStep[]) => (
    <div className="space-y-6 text-gray-700 dark:text-gray-300">
      {steps.map((step, index) => (
        <Step key={step.title} index={index} title={step.title}>
          {step.body}
        </Step>
      ))}
    </div>
  );

  return (
    <div className="space-y-8">
      <section className="space-y-4">
        <SectionHeading title="Set up your CLI">
          Pick your tool — each tab is the complete recipe, start to finish.
        </SectionHeading>
        <Card>
          <Tabs defaultValue="claude-code">
            <TabsList>
              <Tab value="claude-code">Claude Code</Tab>
              <Tab value="codex">Codex</Tab>
            </TabsList>
            <TabPanel value="claude-code">{renderToolSteps(claudeCodeSteps)}</TabPanel>
            <TabPanel value="codex">{renderToolSteps(codexSteps)}</TabPanel>
          </Tabs>
        </Card>
      </section>

      <section className="space-y-4">
        <SectionHeading title="Verify">
          One check, whichever tool you set up.
        </SectionHeading>
        <Card>
          <CardTitle>Verify it works</CardTitle>
          <p className="mt-2 text-sm text-gray-700 dark:text-gray-300">
            Ask Claude Code or Codex anything, then open the <strong>Log Viewer</strong> in this
            dashboard. Your request should appear there within a few seconds — that confirms traffic
            is flowing through the gateway and being metered against your account. If it does not,
            see Troubleshooting below.
          </p>
        </Card>
      </section>
    </div>
  );
}
