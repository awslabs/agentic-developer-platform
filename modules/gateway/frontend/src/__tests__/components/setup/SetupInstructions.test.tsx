/**
 * SetupInstructions tests — Issue #4146.
 *
 * The negative assertions are the anti-regression core of this file. The page
 * previously documented an AWS-SSO + bg-auth.sh flow with placeholder URLs, and a
 * user following it could not get set up at all. Content correctness here is the
 * feature, so the tests assert both what must be present and what must never
 * come back.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  SetupInstructions,
  buildAnthropicSettings,
  buildBedrockSettings,
  buildCodexConfigToml,
  CODEX_PROXY_PORT,
} from '@/components/setup/SetupInstructions';

const STUB_ORIGIN = 'https://d123abc.cloudfront.net';

function stubOrigin(origin: string) {
  Object.defineProperty(window, 'location', {
    writable: true,
    value: { ...window.location, origin },
  });
}

describe('SetupInstructions', () => {
  const realLocation = window.location;

  beforeEach(() => {
    stubOrigin(STUB_ORIGIN);
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
  it.each([
    ['bg-auth.sh', 'deprecated SigV4 helper'],
    ['bg-auth.ps1', 'never existed in the repo'],
    ['aws configure sso', 'AWS SSO is not part of this flow'],
    ['apiBaseUrl', 'not a key Claude Code reads'],
    ['config.json', 'the file is settings.json'],
    ['your-gateway-url', 'unresolved placeholder'],
    ['awsapps.com', 'SSO start URL placeholder'],
  ])('does not mention %s (%s)', (needle) => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').not.toContain(needle);
  });

  it('does not suffix the gateway base URL with /v1', () => {
    // Claude Code appends the API path itself. Scoped to the gateway origin: the
    // Codex proxy's own base_url legitimately ends in /openai/v1 (it is an
    // OpenAI-wire endpoint on localhost, not this gateway URL).
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).not.toContain(`${STUB_ORIGIN}/api/v1`);
    expect(text).not.toContain(`${STUB_ORIGIN}/v1`);
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

    // The Anthropic-format snippet is the primary one, rendered eagerly.
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
  it('documents the serve proxy flow for Codex', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('Codex');
    expect(text).toContain('serve');
    expect(text).toContain('bg-gateway-proxy.py');
    expect(text).toContain('~/.codex/config.toml');
  });

  it('renders the proxy base URL, wire API and dummy env key', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).toContain('http://127.0.0.1:9191/openai/v1');
    expect(text).toContain('wire_api = "responses"');
    expect(text).toContain('env_key = "ADP_GATEWAY_DUMMY"');
    expect(text).toContain('ADP_GATEWAY_DUMMY=unused codex');
  });

  it('does not present a manually exported token as the Codex path', () => {
    // The pre-#4155 instruction. It works for ~1h and then every request 401s
    // with no hook to refresh — which is exactly why `serve` exists.
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).not.toContain('export ADP_GATEWAY_TOKEN=$(');
    expect(text).not.toContain('ADP_GATEWAY_TOKEN');
  });

  it('does not point Codex at the gateway origin directly', () => {
    // Codex must talk to the loopback proxy; the proxy talks to the gateway.
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).not.toContain(`base_url = "${STUB_ORIGIN}`);
  });

  it('tells the user to verify via the Log Viewer', () => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').toContain('Log Viewer');
  });

  it('numbers steps sequentially from the array order', () => {
    render(<SetupInstructions />);

    // First and last step badges — derived, not hardcoded literals.
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.queryByText('8')).not.toBeInTheDocument();
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
