/**
 * SetupInstructions — how to point Claude Code or Codex at this gateway.
 *
 * Issue #4146 rewrote the content end-to-end (the previous AWS-SSO +
 * `bg-auth.sh` flow no longer worked). Issue #4159 made it tabbed. Issue #4852
 * put the `adp` CLI in front: the page now leads with
 * `install → login → status → setup → run`, four short verbs, and the raw-script
 * flow survives as a collapsed per-tab fallback.
 *
 * Principles, in the order they matter:
 *
 * 1. **The primary flow is `adp`.** Five copy-paste lines, none of them a file
 *    path or a heredoc. The scripts underneath are unchanged and still
 *    documented — under a `<details>`, for people who want to read what they run
 *    or who are pinned to a hand-installed helper.
 *
 * 2. **Each tab is fully self-contained.** A Codex user never reads a Claude
 *    Code prerequisite, nothing references a section "above", and each tab
 *    numbers its steps 1..N. Shared content is shared at the component level,
 *    so the tabs cannot drift apart.
 *
 * 3. **Sign-in is one browser approval — no credential ever passes through a
 *    human.** `adp login` opens the browser, the user clicks Approve, done. One
 *    login serves every tool, because they share one token store; the pasted-
 *    token panel remains only as a collapsed fallback for headless machines.
 *
 * Invariants worth preserving on edit:
 * - The base URL is resolved at runtime from the real origin. Never a placeholder.
 * - The settings snippets are `JSON.stringify`'d from objects, so the rendered
 *   text cannot drift from the shape asserted in tests (or from `cli/examples/`).
 * - Step numbers are derived from array order — a reorder cannot mis-number them.
 * - `CODEX_PROXY_PORT` is the single source of the proxy port shared with
 *   `cli/adp` and `cli/bg-cognito-auth.sh`.
 */

import { Card, CardTitle, CopyButton, Tabs, TabsList, Tab, TabPanel } from '@/components/ui';
import { getGatewayBaseUrl } from '@/utils/gatewayUrl';
import { ScriptDownloadList } from '@/components/setup/ScriptDownload';
import { ConnectCliPanel } from '@/components/setup/ConnectCliPanel';

/**
 * The exact value `adp claude setup` writes (Issue #4852 D4, finished in #4859).
 *
 * ABSOLUTE, and the install dir rather than a bare `adp`: Claude Code may invoke
 * the helper from a non-login shell where ~/.adp/bin is not on PATH. It must stay
 * in step with `adp_path()` in cli/adp + DEFAULT_INSTALL_DIR in cli/install.sh —
 * a snippet that disagrees with what the command writes is the bug #4859 fixed.
 */
export const ADP_API_KEY_HELPER = '~/.adp/bin/adp token';

/**
 * The hand-installed equivalent, for the raw-script fallback only: someone who
 * curled the core helper into ~/bin has no ~/.adp/bin, so the snippet above would
 * point at a path they do not have.
 */
export const SCRIPT_API_KEY_HELPER = 'bash ~/bin/bg-cognito-auth.sh token';

/** Anthropic-format settings — mirrors cli/examples/claude-settings-cognito.json */
export function buildAnthropicSettings(baseUrl: string) {
  return {
    env: {
      ANTHROPIC_BASE_URL: baseUrl,
    },
    apiKeyHelper: ADP_API_KEY_HELPER,
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
    apiKeyHelper: ADP_API_KEY_HELPER,
    apiKeyHelperTtlMs: 3300000,
    permissions: { allow: ['WebSearch', 'WebFetch'] },
    model: 'global.anthropic.claude-opus-4-6-v1',
  };
}

/**
 * Default port of the `serve` proxy — DEFAULT_PROXY_PORT in cli/bg-cognito-auth.sh
 * and CODEX_PROXY_PORT in cli/adp. It must match the port in the config.toml
 * `base_url` below, so all of them come from here.
 */
export const CODEX_PROXY_PORT = 9191;

/**
 * Codex provider config — mirrors cli/README.md §"Using Codex: zero-touch auth
 * with serve", Step 3. Deliberately points at the local proxy, NOT at the gateway
 * directly: Codex has no apiKeyHelper hook, so a token put here goes stale in an
 * hour (Issue #4156). `adp codex setup` writes this same block.
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

/**
 * The one-line install (Issue #4852).
 *
 * The URL is passed explicitly as well as fetched from, because the download
 * route serves a STATIC file that cannot be templated per-request — so the
 * script cannot know which deployment it came from unless we tell it. It
 * persists the value, which is what lets every later `adp` command take no
 * flags. `sh -s --` is what forwards arguments to a script arriving on stdin.
 */
export function buildAdpInstallCommand(baseUrl: string): string {
  return `curl -fsSL ${baseUrl}/cli/install.sh | sh -s -- --gateway-url ${baseUrl}`;
}

/** One curl per file, into ~/bin — the raw-script fallback's install step. */
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
const SUMMARY = 'cursor-pointer text-primary-600 dark:text-primary-400';

interface SetupStep {
  title: string;
  body: React.ReactNode;
}

/**
 * Shared prerequisites for the `adp` flow. Deliberately does NOT list the `aws`
 * CLI: gateway users hold no AWS credentials, and gateway-routed refresh (#4846)
 * is what makes that true.
 */
function AdpPrereqItems() {
  return (
    <>
      <li>
        <code className={CODE}>curl</code> and <code className={CODE}>jq</code> on your PATH
      </li>
      <li>Signed in to this dashboard — you already are, or you could not see this page</li>
    </>
  );
}

/** Step 2, identical in both tabs: the one-line install. */
function InstallAdpStep({ baseUrl }: { baseUrl: string }) {
  return (
    <>
      <p>
        One line. It installs <code className={CODE}>adp</code> into{' '}
        <code className={CODE}>~/.adp/bin</code>, remembers this gateway's URL so nothing later needs
        a flag, and adds itself to your PATH:
      </p>
      <CodeSnippet>{buildAdpInstallCommand(baseUrl)}</CodeSnippet>
      <p className="text-gray-500 dark:text-gray-400">
        Open a new terminal afterwards (or <code className={CODE}>source</code> your shell config) so
        the PATH change takes effect. Later, <code className={CODE}>adp update</code> pulls a newer
        version from this same gateway, and <code className={CODE}>adp update --rollback</code>{' '}
        undoes it.
      </p>
      <details className="mt-1">
        <summary className={SUMMARY}>Rather read it before you run it?</summary>
        <div className="mt-2 space-y-2">
          <p>Download, inspect, then run — the installer is a short POSIX shell script:</p>
          <CodeSnippet>{`curl -fsSL ${baseUrl}/cli/install.sh -o install.sh
less install.sh
sh install.sh --gateway-url ${baseUrl}`}</CodeSnippet>
        </div>
      </details>
    </>
  );
}

/**
 * Step 3, identical in both tabs (rendered from this one component so the tabs
 * cannot drift). One browser approval, shared by every tool.
 */
function SignInStep() {
  return (
    <>
      <p>Run this, then click Approve in the browser tab it opens:</p>
      <CodeSnippet>{'adp login'}</CodeSnippet>
      <p className="text-gray-500 dark:text-gray-400">
        The approval page shows the same short code as your terminal — confirm they match and
        approve. Your machine receives a short-lived credential that refreshes itself in the
        background; you never see or copy a token. Re-approving takes one click whenever it fully
        expires (about a day of inactivity).
      </p>
      <p>Confirm it worked:</p>
      <CodeSnippet>{'adp status'}</CodeSnippet>
      <p className="text-gray-500 dark:text-gray-400">
        That prints who you are signed in as, which gateway, and how long the current token is good
        for. <strong>One login covers every tool</strong> — if you set up a second tool later, you
        sign in once, not again.
      </p>
      <details className="mt-1">
        <summary className={SUMMARY}>
          On a headless machine (SSH, no browser)? Use a pasted token instead
        </summary>
        <div className="mt-2 space-y-2">
          <p>
            Where no browser can open, seed the CLI from this browser session: run{' '}
            <code className={CODE}>adp import</code> and paste the revealed refresh token when
            prompted.
          </p>
          <ConnectCliPanel />
        </div>
      </details>
      <details className="mt-1">
        <summary className={SUMMARY}>
          Have a Cognito password? (accounts not created via GitHub sign-in)
        </summary>
        <p className="mt-2">
          <code className={CODE}>adp login</code> uses browser approval. For the interactive password
          flow, call the underlying helper directly:{' '}
          <code className={CODE}>~/.adp/bin/bg-cognito-auth.sh login</code>
        </p>
      </details>
    </>
  );
}

/**
 * The pre-#4852 flow, kept per-tab and collapsed. Two audiences: people who want
 * to read the scripts they run, and anyone already set up this way who should not
 * be told their working install is wrong.
 *
 * `files`/`configPath`/`configBody` keep it tool-specific — the Claude Code tab
 * must not leak the Codex proxy into view, and vice versa.
 *
 * `configNote` exists because `configBody` is the same object the primary flow
 * shows, and that one names `adp`'s installed path. Where the two differ for a
 * hand-installed setup, this is where we say so.
 */
function RawScriptFallback({
  baseUrl,
  files,
  configPath,
  configBody,
  configNote,
  runCommand,
}: {
  baseUrl: string;
  files: string[];
  configPath: string;
  configBody: string;
  configNote?: React.ReactNode;
  runCommand: string;
}) {
  return (
    <details className="mt-2">
      <summary className={SUMMARY}>
        Prefer to run the scripts yourself, without <code className={CODE}>adp</code>?
      </summary>
      <div className="mt-2 space-y-3">
        <p className="text-gray-500 dark:text-gray-400">
          The same flow, unchanged — <code className={CODE}>adp</code> is a wrapper around these
          scripts, not a replacement for them. Use this if you want to read what you run, or if you
          already have a working hand-installed setup.
        </p>
        <p>Fetch the {files.length > 1 ? 'scripts' : 'script'} into ~/bin:</p>
        <CodeSnippet>{buildInstallCommand(baseUrl, files)}</CodeSnippet>
        <p>Sign in the same way:</p>
        <CodeSnippet>{`~/bin/bg-cognito-auth.sh login --web --gateway-url ${baseUrl}`}</CodeSnippet>
        <p>
          Write <code className={CODE}>{configPath}</code>:
        </p>
        <CodeSnippet>{buildFileWriteCommand(configPath, configBody)}</CodeSnippet>
        {configNote}
        <p className="text-gray-500 dark:text-gray-400">
          This replaces the file. If you already have one you care about, merge the content shown
          above into it instead — which is the difference <code className={CODE}>adp</code> makes:
          its setup verbs merge, and re-running them changes nothing.
        </p>
        <p>Then run it:</p>
        <CodeSnippet>{runCommand}</CodeSnippet>
        <div>
          <ScriptDownloadList files={files} />
          <p className="mt-2 text-gray-500 dark:text-gray-400">
            Prefer the browser? Save the {files.length > 1 ? 'files' : 'file'} to{' '}
            <code className={CODE}>~/bin</code> and{' '}
            <code className={CODE}>chmod +x ~/bin/bg-cognito-auth.sh</code>.
          </p>
        </div>
      </div>
    </details>
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
          <AdpPrereqItems />
        </ul>
      ),
    },
    {
      title: 'Install the adp CLI',
      body: <InstallAdpStep baseUrl={baseUrl} />,
    },
    {
      title: 'Sign in',
      body: <SignInStep />,
    },
    {
      title: 'Connect Claude Code',
      body: (
        <>
          <CodeSnippet>{'adp claude setup'}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            That merges the gateway settings into{' '}
            <code className={CODE}>~/.claude/settings.json</code> — your existing permissions, hooks
            and MCP servers are left alone. It sets{' '}
            <code className={CODE}>apiKeyHelper</code> so Claude Code fetches a fresh token by
            itself, and it is safe to re-run.
          </p>
          <details className="mt-1">
            <summary className={SUMMARY}>What it writes</summary>
            <div className="mt-2 space-y-2">
              <p>
                The Bedrock env block plus the token helper. Base URL is this deployment's real
                gateway URL (no <code className={CODE}>/v1</code> suffix — Claude Code appends the
                API path itself):
              </p>
              <CodeSnippet>{bedrockSettings}</CodeSnippet>
            </div>
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
            Claude Code calls <code className={CODE}>adp token</code> automatically via{' '}
            <code className={CODE}>apiKeyHelper</code> and refreshes on its own — you should not need
            to run anything by hand again.
          </p>
          <RawScriptFallback
            baseUrl={baseUrl}
            files={['bg-cognito-auth.sh']}
            configPath="~/.claude/settings.json"
            configBody={anthropicSettings}
            configNote={
              <p className="text-gray-500 dark:text-gray-400">
                Installing by hand means no <code className={CODE}>~/.adp/bin</code>, so point{' '}
                <code className={CODE}>apiKeyHelper</code> at the script you just fetched instead:{' '}
                <code className={CODE}>{SCRIPT_API_KEY_HELPER}</code>
              </p>
            }
            runCommand="claude"
          />
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
          <AdpPrereqItems />
        </ul>
      ),
    },
    {
      title: 'Install the adp CLI',
      body: <InstallAdpStep baseUrl={baseUrl} />,
    },
    {
      title: 'Sign in',
      body: <SignInStep />,
    },
    {
      title: 'Connect Codex',
      body: (
        <>
          <CodeSnippet>{'adp codex setup'}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            That merges a provider block into <code className={CODE}>~/.codex/config.toml</code> —
            any other providers, MCP servers and model settings you have are preserved, and it is
            safe to re-run.
          </p>
          <p className="text-gray-500 dark:text-gray-400">
            Switching models with Codex's in-app picker just works — the proxy adds the{' '}
            <code className={CODE}>openai.</code> prefix the gateway expects if the picker writes a
            bare model name.
          </p>
          <details className="mt-1">
            <summary className={SUMMARY}>What it writes</summary>
            <div className="mt-2 space-y-2">
              <p>
                A provider pointed at the local proxy, not at the gateway directly: Codex reads its
                credential once at launch and never asks again, so a token written here would go
                stale within the hour.
              </p>
              <CodeSnippet>{codexConfigToml}</CodeSnippet>
            </div>
          </details>
        </>
      ),
    },
    {
      title: 'Start the proxy, then Codex',
      body: (
        <>
          <CodeSnippet>{`adp serve                              # foreground; Ctrl-C to stop
ADP_GATEWAY_DUMMY=unused codex         # in a second terminal`}</CodeSnippet>
          <p className="text-gray-500 dark:text-gray-400">
            Leave the proxy running as long as you like — refresh happens per request, behind the
            scenes. It injects a freshly-refreshed token into every call. Codex requires{' '}
            <code className={CODE}>env_key</code> to name an existing env var but never validates its
            value; the proxy discards whatever arrives and injects the real token.
          </p>
          <RawScriptFallback
            baseUrl={baseUrl}
            files={['bg-cognito-auth.sh', 'bg-gateway-proxy.py']}
            configPath="~/.codex/config.toml"
            configBody={codexConfigToml}
            runCommand={`~/bin/bg-cognito-auth.sh serve          # foreground; Ctrl-C to stop
ADP_GATEWAY_DUMMY=unused codex         # in a second terminal`}
          />
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
