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

  it('does not suffix the base URL with /v1', () => {
    render(<SetupInstructions />);
    const text = document.body.textContent ?? '';

    expect(text).not.toContain('/v1"');
    expect(text).not.toContain(`${STUB_ORIGIN}/api/v1`);
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

  it('mentions Codex sharing the same base URL', () => {
    render(<SetupInstructions />);

    expect(document.body.textContent ?? '').toContain('Codex');
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
