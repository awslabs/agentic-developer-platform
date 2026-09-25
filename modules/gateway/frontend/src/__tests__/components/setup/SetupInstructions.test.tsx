/**
 * SetupInstructions tests — Issues #4146, #4156, #4159.
 *
 * The negative assertions are the anti-regression core of this file. The page
 * previously documented an AWS-SSO + bg-auth.sh flow with placeholder URLs, and a
 * user following it could not get set up at all. Content correctness here is the
 * feature, so the tests assert both what must be present and what must never
 * come back.
 *
 * #4159 made the layout tabbed (common section → Claude Code | Codex tabs →
 * verify). Every content assertion below survived that restructure unchanged;
 * the Codex ones just have to activate the Codex tab first, because an inactive
 * TabPanel renders nothing at all. The page-wide negative assertions are checked
 * on BOTH tabs, so a stale snippet cannot hide behind a tab.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  SetupInstructions,
  buildAnthropicSettings,
  buildBedrockSettings,
  buildCodexConfigToml,
  buildFileWriteCommand,
  buildInstallCommand,
  ADP_API_KEY_HELPER,
  SCRIPT_API_KEY_HELPER,
  CODEX_PROXY_PORT,
} from '@/components/setup/SetupInstructions';
import * as auth from '@/services/auth';

const STUB_ORIGIN = 'https://d123abc.cloudfront.net';

function stubOrigin(origin: string) {
  Object.defineProperty(window, 'location', {
    writable: true,
    value: { ...window.location, origin },
  });
}

/** Activate the Codex tab — its panel is not rendered until then. */
async function openCodexTab() {
  await userEvent.click(screen.getByRole('tab', { name: 'Codex' }));
}

describe('SetupInstructions', () => {
  const realLocation = window.location;

  beforeEach(() => {
    stubOrigin(STUB_ORIGIN);
    // The Connect CLI panel is part of the common section now; give it a token so
    // it renders its real body rather than the re-sign-in fallback.
    vi.spyOn(auth, 'getRefreshToken').mockReturnValue('refresh-token-value');
  });

  afterEach(() => {
    Object.defineProperty(window, 'location', { writable: true, value: realLocation });
    vi.restoreAllMocks();
  });

  it('documents the current Cognito helper flow', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('bg-cognito-auth.sh');
    expect(text).toContain('apiKeyHelper');
    expect(text).toContain('settings.json');
    expect(text).toContain('ANTHROPIC_BASE_URL');
  });

  // --- Anti-regression: the stale flow must never come back -------------------
  // Checked on both tabs — a tab panel that is not active renders nothing, so a
  // single-view assertion could miss a regression parked in the other tab.
  it.each([
    ['bg-auth.sh', 'deprecated SigV4 helper'],
    ['bg-auth.ps1', 'never existed in the repo'],
    ['aws configure sso', 'AWS SSO is not part of this flow'],
    ['apiBaseUrl', 'not a key Claude Code reads'],
    ['config.json', 'the file is settings.json'],
    ['your-gateway-url', 'unresolved placeholder'],
    ['awsapps.com', 'SSO start URL placeholder'],
  ])('does not mention %s (%s)', async (needle) => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').not.toContain(needle);

    await openCodexTab();
    expect(document.body.textContent ?? '').not.toContain(needle);
  });

  it('does not suffix the gateway base URL with /v1', async () => {
    // Claude Code appends the API path itself. Scoped to the gateway origin: the
    // Codex proxy's own base_url legitimately ends in /openai/v1 (it is an
    // OpenAI-wire endpoint on localhost, not this gateway URL).
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').not.toContain(`${STUB_ORIGIN}/api/v1`);
    expect(document.body.textContent ?? '').not.toContain(`${STUB_ORIGIN}/v1`);

    await openCodexTab();
    expect(document.body.textContent ?? '').not.toContain(`${STUB_ORIGIN}/api/v1`);
    expect(document.body.textContent ?? '').not.toContain(`${STUB_ORIGIN}/v1`);
  });

  // --- Real base URL ---------------------------------------------------------
  it('renders the real origin, not a placeholder', () => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').toContain(`${STUB_ORIGIN}/api`);
  });

  it('tracks the origin it is served from', () => {
    stubOrigin('https://gateway.example.internal');
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').toContain('https://gateway.example.internal/api');
  });

  // --- Rendered JSON must be valid and match cli/examples/ -------------------
  it('renders a write-command whose JSON payload parses and deep-equals the expected shape', () => {
    render(<SetupInstructions />);

    // The Anthropic-format snippet is the primary one, rendered eagerly on the
    // default (Claude Code) tab — wrapped in the paste-ready heredoc command.
    const snippet = screen
      .getAllByText((_, el) => el?.tagName === 'PRE' && !!el.textContent?.includes('ANTHROPIC_BASE_URL'))
      .at(0);
    expect(snippet).toBeTruthy();

    const text = snippet!.textContent!;
    expect(text.startsWith("mkdir -p ~/.claude\ncat > ~/.claude/settings.json << 'EOF'\n")).toBe(true);
    expect(text.endsWith('\nEOF')).toBe(true);

    const payload = text.split("<< 'EOF'\n")[1].replace(/\nEOF$/, '');
    const parsed = JSON.parse(payload);
    expect(parsed).toEqual({
      env: { ANTHROPIC_BASE_URL: `${STUB_ORIGIN}/api` },
      apiKeyHelper: '~/.adp/bin/adp token',
      apiKeyHelperTtlMs: 3300000,
      permissions: { allow: ['WebSearch', 'WebFetch'] },
      model: 'global.anthropic.claude-opus-4-6-v1',
    });
  });

  // --- Codex: the zero-touch `serve` flow (Issue #4156) -----------------------
  it('documents the serve proxy flow for Codex', async () => {
    render(<SetupInstructions />);
    await openCodexTab();
    const text = document.body.textContent ?? '';

    expect(text).toContain('Codex');
    expect(text).toContain('serve');
    expect(text).toContain('bg-gateway-proxy.py');
    expect(text).toContain('~/.codex/config.toml');
  });

  it('renders the proxy base URL, wire API and dummy env key', async () => {
    render(<SetupInstructions />);
    await openCodexTab();
    const text = document.body.textContent ?? '';

    expect(text).toContain('http://127.0.0.1:9191/openai/v1');
    expect(text).toContain('wire_api = "responses"');
    expect(text).toContain('env_key = "ADP_GATEWAY_DUMMY"');
  });

  // --- Codex: one-command launch (Issue #4863) --------------------------------
  //
  // These assert on the PRIMARY flow only — the copy-paste snippets a user sees
  // without expanding anything. The raw-script fallback still documents the
  // two-terminal form on purpose (someone running the scripts by hand has no
  // `adp` to run), so a whole-body assertion could not express "the primary flow
  // is one command" and would pass even if the old steps came back.
  const primarySnippets = () =>
    Array.from(document.querySelectorAll('pre'))
      .filter((node) => node.closest('details') === null)
      .map((node) => node.textContent ?? '')
      .join('\n');

  it('launches Codex with a single command', async () => {
    render(<SetupInstructions />);
    await openCodexTab();

    expect(primarySnippets()).toContain('adp codex');
  });

  it('does not make the user start a proxy or set a dummy var by hand', async () => {
    // The two-step dance #4863 removed: `adp serve` in one terminal, then
    // `ADP_GATEWAY_DUMMY=unused codex` in another. `adp codex` does both.
    render(<SetupInstructions />);
    await openCodexTab();
    const snippets = primarySnippets();

    expect(snippets).not.toContain('ADP_GATEWAY_DUMMY=unused codex');
    expect(snippets).not.toMatch(/^adp serve\b/m);
  });

  it('notes that bare codex needs the opt-in daemon, and adp claude is optional', async () => {
    // The asymmetry is documented, not hidden: Claude Code refreshes its own
    // token per request, so bare `claude` already works and `adp claude` is a
    // convenience. A user who thinks otherwise files a bug that is not one.
    render(<SetupInstructions />);
    await openCodexTab();
    const text = document.body.textContent ?? '';

    expect(text).toContain('adp daemon install');
    expect(text).toContain('adp claude');
    expect(text).toMatch(/optional/i);
  });

  it('does not present a manually exported token as the Codex path', async () => {
    // The pre-#4155 instruction. It works for ~1h and then every request 401s
    // with no hook to refresh — which is exactly why `serve` exists.
    render(<SetupInstructions />);
    await openCodexTab();
    const text = document.body.textContent ?? '';

    expect(text).not.toContain('export ADP_GATEWAY_TOKEN=$(');
    expect(text).not.toContain('ADP_GATEWAY_TOKEN');
  });

  it('does not point Codex at the gateway origin directly', async () => {
    // Codex must talk to the loopback proxy; the proxy talks to the gateway.
    render(<SetupInstructions />);
    await openCodexTab();

    expect(document.body.textContent ?? '').not.toContain(`base_url = "${STUB_ORIGIN}`);
  });

  it('tells the user to verify via the Log Viewer', () => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').toContain('Log Viewer');
  });

  // --- Structure: two self-contained tabs -------------------------------------
  it('renders two sections in order: set up your CLI, verify', () => {
    render(<SetupInstructions />);

    const headings = screen
      .getAllByRole('heading', { level: 2 })
      .map((h) => h.textContent?.trim());

    // 'Download Helper Scripts' renders inside the install step's collapsed
    // browser-download fallback, between the two section headings.
    expect(headings).toEqual(['Set up your CLI', 'Download Helper Scripts', 'Verify']);
  });

  it('installs via curl from this gateway — no browser download, no ~/Downloads mv', async () => {
    // The old flow's install step moved files from ~/Downloads, and the Codex
    // tab then re-moved a file an earlier step had already moved (which failed).
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain(
      `curl -fsSL ${STUB_ORIGIN}/api/cli/bg-cognito-auth.sh -o ~/bin/bg-cognito-auth.sh`
    );
    expect(document.body.textContent ?? '').not.toContain('~/Downloads');

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain(
      `curl -fsSL ${STUB_ORIGIN}/api/cli/bg-gateway-proxy.py -o ~/bin/bg-gateway-proxy.py`
    );
    expect(document.body.textContent ?? '').not.toContain('~/Downloads');
  });

  it('keeps the paste-ready write-commands in the raw-script fallback', async () => {
    // Still documented, but no longer the primary path: `adp <tool> setup` merges
    // instead of overwriting, so the heredoc lives under the fallback details
    // together with its overwrite caution.
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain("cat > ~/.claude/settings.json << 'EOF'");
    expect(document.body.textContent ?? '').toContain('This replaces the file');

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain("cat > ~/.codex/config.toml << 'EOF'");
    expect(document.body.textContent ?? '').toContain('This replaces the file');
  });

  it('keeps the browser-download cards available as a fallback', () => {
    render(<SetupInstructions />);
    expect(screen.getByRole('heading', { name: 'Download Helper Scripts' })).toBeInTheDocument();
  });

  it('signs in with login --web on both tabs — no token is displayed or pasted', async () => {
    render(<SetupInstructions />);
    const loginCommand = `~/bin/bg-cognito-auth.sh login --web --gateway-url ${STUB_ORIGIN}/api`;

    expect(document.body.textContent ?? '').toContain(loginCommand);

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain(loginCommand);
  });

  it('keeps the paste-a-token panel only as the headless fallback', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    // Present (inside the collapsed headless-machine details)…
    expect(text).toContain('headless machine');
    expect(text).toContain('import');
    // …and never as an unconditional numbered step.
    expect(screen.queryByRole('heading', { name: /^Connect the CLI$/ })).not.toBeInTheDocument();
  });

  it('keeps each tab self-contained — no cross-tool prerequisites', async () => {
    render(<SetupInstructions />);
    // Python is an ADP installer prerequisite shared by every tool.
    expect(document.body.textContent ?? '').not.toContain('bg-gateway-proxy.py');
    expect(document.body.textContent ?? '').toContain('python3');

    await openCodexTab();
    // Codex tab: no Claude Code install instruction.
    expect(document.body.textContent ?? '').not.toContain('@anthropic-ai/claude-code');
    expect(document.body.textContent ?? '').not.toContain('settings.json');
  });

  it('never references content by page position (above/below)', async () => {
    render(<SetupInstructions />);
    for (const needle of ['section above', 'section below', 'Downloads section']) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }
    await openCodexTab();
    for (const needle of ['section above', 'section below', 'Downloads section']) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }
  });

  it('defaults to the Claude Code tab', () => {
    render(<SetupInstructions />);

    expect(screen.getByRole('tab', { name: 'Claude Code' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    expect(screen.getByRole('tab', { name: 'Codex' })).toHaveAttribute('aria-selected', 'false');
    // Claude Code content is visible; Codex content is not rendered at all.
    expect(document.body.textContent ?? '').toContain('~/.claude/settings.json');
    expect(document.body.textContent ?? '').not.toContain('~/.codex/config.toml');
  });

  it('switches to the Codex tab and back', async () => {
    render(<SetupInstructions />);

    await openCodexTab();
    expect(screen.getByRole('tab', { name: 'Codex' })).toHaveAttribute('aria-selected', 'true');
    expect(document.body.textContent ?? '').toContain('~/.codex/config.toml');
    expect(document.body.textContent ?? '').not.toContain('~/.claude/settings.json');

    await userEvent.click(screen.getByRole('tab', { name: 'Claude Code' }));
    expect(document.body.textContent ?? '').toContain('~/.claude/settings.json');
    expect(document.body.textContent ?? '').not.toContain('~/.codex/config.toml');
  });

  // --- The adp flow is the primary path (Issue #4852) -------------------------
  it('leads with the one-line install carrying this gateway url', async () => {
    // The URL must be both fetched from AND passed in: the route serves a static
    // file, so the script cannot know which deployment it came from otherwise.
    render(<SetupInstructions />);
    const installLine = `curl -fsSL ${STUB_ORIGIN}/api/cli/install.sh | sh -s -- --gateway-url ${STUB_ORIGIN}/api`;

    expect(document.body.textContent ?? '').toContain(installLine);

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain(installLine);
  });

  it.each([
    ['claude-code', 'adp claude setup'],
    ['codex', 'adp codex setup'],
    ['hermes', 'adp hermes setup'],
    ['kimi', 'adp kimi doctor'],
  ])('presents the %s tab as install → login → status → connect', async (tab, connectionCommand) => {
    render(<SetupInstructions />);
    if (tab === 'codex') await openCodexTab();
    if (tab === 'hermes') await userEvent.click(screen.getByRole('tab', { name: 'Hermes' }));
    if (tab === 'kimi') await userEvent.click(screen.getByRole('tab', { name: 'Kimi Code' }));
    const text = document.body.textContent ?? '';

    expect(text).toContain(`curl -fsSL ${STUB_ORIGIN}/api/cli/install.sh | sh -s -- --gateway-url ${STUB_ORIGIN}/api`);
    expect(text).toContain('adp login');
    expect(text).toContain('adp status');
    expect(text).toContain(connectionCommand);
  });

  it('tells the user one login covers every tool', async () => {
    // The whole point of a single auth verb: adding a second tool is its setup
    // verb, not another sign-in.
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain('One login covers every tool');

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain('One login covers every tool');
  });

  it('does not present a per-tool login verb', async () => {
    // There is exactly one auth verb; `adp codex login` does not exist and would
    // imply the token store is per-tool.
    render(<SetupInstructions />);
    for (const needle of ['adp codex login', 'adp claude login']) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }
    await openCodexTab();
    for (const needle of ['adp codex login', 'adp claude login']) {
      expect(document.body.textContent ?? '').not.toContain(needle);
    }
  });

  it('says the setup verbs merge rather than overwrite', async () => {
    // The behavioural difference from the raw-script flow, and the reason a user
    // with an existing config can run these without fear.
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain('merges the gateway settings');
    expect(document.body.textContent ?? '').toContain('safe to re-run');

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain('merges a provider block');
    expect(document.body.textContent ?? '').toContain('safe to re-run');
  });

  it('documents self-update and its rollback', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('adp update');
    expect(text).toContain('adp update --rollback');
  });

  it('offers a read-before-you-run alternative to piping into sh', () => {
    // Piping a remote script into a shell is a reasonable thing to be wary of.
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain(`curl -fsSL ${STUB_ORIGIN}/api/cli/install.sh -o install.sh`);
    expect(text).toContain(`sh install.sh --gateway-url ${STUB_ORIGIN}/api`);
  });

  it('does not require the aws CLI', async () => {
    // Gateway users hold no AWS credentials — gateway-routed refresh (#4846) is
    // what makes that true, and the old prerequisite list was simply wrong.
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').not.toContain('aws');

    await openCodexTab();
    expect(document.body.textContent ?? '').not.toContain('aws');
  });

  it('keeps the raw-script flow available per tab', async () => {
    // Two audiences: people who want to read what they run, and anyone already
    // set up this way whose working install must not be called wrong.
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain('Prefer to run the scripts yourself');
    expect(document.body.textContent ?? '').toContain('bg-cognito-auth.sh');

    await openCodexTab();
    expect(document.body.textContent ?? '').toContain('Prefer to run the scripts yourself');
    expect(document.body.textContent ?? '').toContain('bg-gateway-proxy.py');
  });

  it('uses adp for the token helper and the proxy in the primary flow', async () => {
    render(<SetupInstructions />);
    expect(document.body.textContent ?? '').toContain('adp token');

    // #4863 replaced the standalone `adp serve` step with `adp codex`, which
    // starts the same proxy on demand. Still `adp`-driven — one command instead
    // of two terminals.
    await openCodexTab();
    expect(document.body.textContent ?? '').toContain('adp codex');
  });

  // --- Issue #4859: the settings snippet must agree with the command above it --
  it('renders the adp helper path in the settings snippet the primary flow shows', () => {
    // The bug: step 4 says to run `adp claude setup`, and the "What it writes"
    // snippet directly beneath it showed the raw-script helper — so the page
    // contradicted itself and a user comparing the two assumed a broken setup.
    render(<SetupInstructions />);
    const snippet = screen
      .getAllByText(
        (_, el) => el?.tagName === 'PRE' && !!el.textContent?.includes('ANTHROPIC_BEDROCK_BASE_URL')
      )
      .at(0);

    expect(snippet).toBeTruthy();
    expect(snippet!.textContent).toContain(ADP_API_KEY_HELPER);
    expect(snippet!.textContent).not.toContain('bg-cognito-auth.sh');
  });

  it('shows the script-based helper value only inside the raw-script fallback', () => {
    // The fallback renders the same settings object, so it must say which value a
    // hand-installed (~/bin, no ~/.adp) setup needs — otherwise the fallback
    // sends the user to a path they do not have.
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain(SCRIPT_API_KEY_HELPER);
    expect(text).toContain('Prefer to run the scripts yourself');
  });

  it('numbers each tab from 1 — a tab is one complete flow', async () => {
    render(<SetupInstructions />);

    // Claude Code: five steps → 1..5, nothing beyond.
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.queryByText('6')).not.toBeInTheDocument();

    // Codex: five steps → 1..5, nothing beyond.
    await openCodexTab();
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.queryByText('6')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: 'Hermes' }));
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.queryByText('6')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('tab', { name: 'Kimi Code' }));
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.queryByText('6')).not.toBeInTheDocument();
  });

  it('opens Hermes instructions with ADP launch commands and deployment selection', async () => {
    render(<SetupInstructions />);
    await userEvent.click(screen.getByRole('tab', { name: 'Hermes' }));

    expect(screen.getByRole('tab', { name: 'Hermes' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('adp hermes', { selector: 'pre' })).toBeInTheDocument();
    expect(screen.getByText('adp hermes --oneshot "Explain this repository"', { selector: 'pre' })).toBeInTheDocument();
    expect(screen.getByText('adp --deployment dev hermes', { selector: 'pre' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Hermes installation guide' })).toHaveAttribute(
      'href', 'https://github.com/NousResearch/hermes-agent'
    );
    expect(document.body.textContent ?? '').toContain('One login covers every tool');
    expect(document.body.textContent ?? '').not.toContain('~/.codex/config.toml');
    expect(document.body.textContent ?? '').not.toContain('ADP_GATEWAY_DUMMY');
  });

  it('opens Kimi instructions for the existing adapter without inventing a setup command', async () => {
    render(<SetupInstructions />);
    await userEvent.click(screen.getByRole('tab', { name: 'Kimi Code' }));

    expect(screen.getByRole('tab', { name: 'Kimi Code' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('adp kimi', { selector: 'pre' })).toBeInTheDocument();
    expect(screen.getByText('adp kimi --prompt "Explain this repository"', { selector: 'pre' })).toBeInTheDocument();
    expect(screen.getByText('adp --deployment dev kimi', { selector: 'pre' })).toBeInTheDocument();
    const text = document.body.textContent ?? '';
    expect(text).toContain('Kimi Code and the ADP Kimi adapter installed and configured');
    expect(text).toContain('adp kimi --version');
    expect(text).toContain('adp kimi doctor');
    expect(text).toContain('One login covers every tool');
    expect(text).not.toContain('adp kimi setup');
    expect(text).not.toContain('adp hermes setup');
  });
});

describe('buildFileWriteCommand', () => {
  it('creates the parent dir and writes via a quoted heredoc (no shell expansion)', () => {
    expect(buildFileWriteCommand('~/.claude/settings.json', '{ "a": 1 }')).toBe(
      "mkdir -p ~/.claude\ncat > ~/.claude/settings.json << 'EOF'\n{ \"a\": 1 }\nEOF"
    );
  });

  it('round-trips content containing $ and backticks untouched', () => {
    const content = 'value = "$HOME `whoami`"';
    expect(buildFileWriteCommand('~/.codex/config.toml', content)).toContain(content);
  });
});

describe('buildInstallCommand', () => {
  it('fetches each file from the gateway into ~/bin and marks the helper executable', () => {
    expect(buildInstallCommand('https://x/api', ['bg-cognito-auth.sh', 'bg-gateway-proxy.py'])).toBe(
      [
        'mkdir -p ~/bin',
        'curl -fsSL https://x/api/cli/bg-cognito-auth.sh -o ~/bin/bg-cognito-auth.sh',
        'curl -fsSL https://x/api/cli/bg-gateway-proxy.py -o ~/bin/bg-gateway-proxy.py',
        'chmod +x ~/bin/bg-cognito-auth.sh',
      ].join('\n')
    );
  });
});

describe('settings builders', () => {
  it('uses a 55-minute apiKeyHelper TTL, not the legacy 5-minute value', () => {
    // 300000 came from the bg-auth.sh-era cli/claude-settings.example.json and
    // would make Claude Code re-shell 11x more often than the token needs.
    expect(buildAnthropicSettings('https://x/api').apiKeyHelperTtlMs).toBe(3300000);
    expect(buildBedrockSettings('https://x/api').apiKeyHelperTtlMs).toBe(3300000);
  });

  it('builds the Bedrock variant with the gateway base URL and Bedrock env flags', () => {
    expect(buildBedrockSettings('https://x/api')).toEqual({
      env: {
        AWS_REGION: 'us-east-1',
        CLAUDE_CODE_USE_BEDROCK: '1',
        CLAUDE_CODE_SKIP_BEDROCK_AUTH: '1',
        ANTHROPIC_BEDROCK_BASE_URL: 'https://x/api',
      },
      apiKeyHelper: '~/.adp/bin/adp token',
      apiKeyHelperTtlMs: 3300000,
      permissions: { allow: ['WebSearch', 'WebFetch'] },
      model: 'global.anthropic.claude-opus-4-6-v1',
    });
  });

  it('points both formats at the same helper command', () => {
    expect(buildAnthropicSettings('https://x/api').apiKeyHelper).toBe(
      buildBedrockSettings('https://x/api').apiKeyHelper
    );
  });

  // --- Issue #4859: the snippet must match what `adp claude setup` writes ------
  it.each([
    ['buildAnthropicSettings', buildAnthropicSettings],
    ['buildBedrockSettings', buildBedrockSettings],
  ])('%s sets apiKeyHelper to the value adp claude setup writes', (_name, build) => {
    // The load-bearing assertion of #4859. cmd_claude_setup in cli/adp writes
    // "$(adp_path) token", and adp_path resolves inside DEFAULT_INSTALL_DIR
    // (~/.adp/bin, per cli/install.sh) — so a user comparing this snippet with
    // their real settings.json must see the same string.
    expect(build('https://x/api').apiKeyHelper).toBe(ADP_API_KEY_HELPER);
    expect(ADP_API_KEY_HELPER).toBe('~/.adp/bin/adp token');
  });

  it.each([
    ['buildAnthropicSettings', buildAnthropicSettings],
    ['buildBedrockSettings', buildBedrockSettings],
  ])('%s no longer names the raw helper script', (_name, build) => {
    expect(build('https://x/api').apiKeyHelper).not.toContain('bg-cognito-auth.sh');
  });

  it('uses an absolute helper path, not a bare adp', () => {
    // Claude Code may invoke apiKeyHelper from a non-login shell where
    // ~/.adp/bin is not on PATH, so a bare `adp token` would fail to resolve.
    expect(ADP_API_KEY_HELPER.startsWith('~/.adp/bin/')).toBe(true);
  });
});

describe('buildCodexConfigToml', () => {
  it('matches the config documented in cli/README.md', () => {
    expect(buildCodexConfigToml()).toBe(
      `model = "openai.gpt-5.6-sol"
model_provider = "adp-gateway"

[model_providers.adp-gateway]
name = "ADP Gateway (local auth proxy)"
base_url = "http://127.0.0.1:9191/openai/v1"
wire_api = "responses"
env_key = "ADP_GATEWAY_DUMMY"`
    );
  });

  it('defaults to the port bg-cognito-auth.sh serve binds', () => {
    expect(CODEX_PROXY_PORT).toBe(9191);
    expect(buildCodexConfigToml()).toContain(`127.0.0.1:${CODEX_PROXY_PORT}/`);
  });

  it('keeps base_url on loopback when the port is overridden', () => {
    // A credential-injecting listener must never be reachable off-host, and
    // --port is the only thing serve lets you change.
    expect(buildCodexConfigToml(9292)).toContain('base_url = "http://127.0.0.1:9292/openai/v1"');
    expect(buildCodexConfigToml(9292)).not.toContain('0.0.0.0');
  });
});
