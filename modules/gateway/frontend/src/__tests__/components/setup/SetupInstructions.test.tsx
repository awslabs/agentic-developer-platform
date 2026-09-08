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
  buildInstallCommand,
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
  it('renders a settings snippet that parses and deep-equals the expected shape', () => {
    render(<SetupInstructions />);

    // The Anthropic-format snippet is the primary one, rendered eagerly on the
    // default (Claude Code) tab.
    const snippet = screen
      .getAllByText((_, el) => el?.tagName === 'PRE' && !!el.textContent?.includes('ANTHROPIC_BASE_URL'))
      .at(0);
    expect(snippet).toBeTruthy();

    const parsed = JSON.parse(snippet!.textContent!);
    expect(parsed).toEqual({
      env: { ANTHROPIC_BASE_URL: `${STUB_ORIGIN}/api` },
      apiKeyHelper: 'bash ~/bin/bg-cognito-auth.sh token',
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
    expect(text).toContain('ADP_GATEWAY_DUMMY=unused codex');
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
    // Claude Code tab: no proxy machinery, no python3 requirement.
    expect(document.body.textContent ?? '').not.toContain('bg-gateway-proxy.py');
    expect(document.body.textContent ?? '').not.toContain('python3');

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
      apiKeyHelper: 'bash ~/bin/bg-cognito-auth.sh token',
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
