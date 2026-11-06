import { createHash } from 'node:crypto';
import type { HookCallback, Options } from '@anthropic-ai/claude-agent-sdk';

/** Stable input: body evidence is meaningful only if the probe itself is stable. */
export const PROBE_PROMPT = 'Reply with exactly ADP_PROBE_OK. Do not use tools.';
export const PROBE_SYSTEM_PROMPT =
  'You are the ADP model invocability probe. Follow the user instruction and do not use tools.';
export const PROBE_CWD = '/tmp';
export const PROBE_SDK_DATE = '2026-01-01';

export const PROBE_PROMPT_SHA256 = createHash('sha256').update(PROBE_PROMPT).digest('hex');

export const denyProbeToolUse: HookCallback = async (input) => ({
  hookSpecificOutput: {
    hookEventName: 'PreToolUse',
    permissionDecision: 'deny',
    permissionDecisionReason: `ADP invocability probes never execute tools (${input.hook_event_name === 'PreToolUse' ? input.tool_name : 'unknown'} denied)`,
  },
});

/**
 * Explicitly exercises the Claude Code tool-bearing request shape used by ADP.
 * Project settings are disabled so a checked-out repository cannot mutate a
 * paid probe. The one-turn prompt tells the model not to execute the tools.
 */
export function probeSdkOptions(args: {
  modelId: string;
  maxBudgetUsd: number;
  abortController: AbortController;
  env: NodeJS.ProcessEnv;
}): Options {
  return {
    model: args.modelId,
    cwd: PROBE_CWD,
    tools: { type: 'preset', preset: 'claude_code' },
    settingSources: [],
    systemPrompt: PROBE_SYSTEM_PROMPT,
    permissionMode: 'default',
    // Keep the production tool declarations in the provider request, but put
    // a hard boundary in front of execution. PreToolUse denials take effect
    // even if a permission configuration changes; canUseTool is a second
    // fail-closed path for any tool that enters the permission flow.
    canUseTool: async (_toolName, _input, options) => ({
      behavior: 'deny',
      message: 'ADP invocability probes never execute tools',
      toolUseID: options.toolUseID,
    }),
    hooks: {
      PreToolUse: [{ hooks: [denyProbeToolUse] }],
    },
    persistSession: false,
    // metadata.user_id contains the SDK session UUID. Pinning a probe-only
    // session makes the exact canonical body reproducible without weakening
    // the production harness shape.
    sessionId: '00000000-0000-4000-8000-000000000002',
    maxTurns: 1,
    maxBudgetUsd: args.maxBudgetUsd,
    effort: 'low',
    abortController: args.abortController,
    env: args.env as Record<string, string | undefined>,
  };
}

/**
 * Host markers Claude Code reads to classify its client. GITHUB_ACTIONS makes
 * the CLI report client type "github-action" instead of "sdk-typescript", and
 * every non-SDK client type switches the AskUserQuestion tool description to
 * the variant that documents option previews (CLI 2.1.283: no
 * CLAUDE_CODE_QUESTION_PREVIEW_FORMAT and a client type outside sdk-*,
 * claude-desktop, local-agent, remote => preview text appended). A tool
 * declaration is semantic and must stay in the digest, so the environment is
 * pinned instead of the body being normalized (#7993 CI run 39005333704).
 */
const HOST_CLIENT_MARKERS = new Set(['GITHUB_ACTIONS']);

/**
 * A shell that itself runs inside a Claude Code session (a developer terminal, a
 * CI step started from an agent) carries CLAUDECODE / CLAUDE_CODE_* settings such
 * as CLAUDE_CODE_ENTRYPOINT=cli, which the SDK reflects into the request's
 * billing header, or CLAUDE_CODE_SKIP_BEDROCK_AUTH; a GitHub Actions runner
 * carries GITHUB_ACTIONS. The probe pod carries none of them, so they are
 * dropped before the pinned probe settings are applied; a generator run anywhere
 * then emits the same body as the runtime image (#7993).
 */
export function inheritedProbeEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const inherited: NodeJS.ProcessEnv = {};
  for (const [key, value] of Object.entries(source)) {
    if (/^CLAUDE(CODE|_)/.test(key) || HOST_CLIENT_MARKERS.has(key)) continue;
    inherited[key] = value;
  }
  return inherited;
}

export function probeSdkEnvironment(args: {
  baseUrl: string;
  region: string;
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...inheritedProbeEnvironment(process.env),
    CLAUDE_CODE_USE_BEDROCK: '1',
    ANTHROPIC_BEDROCK_BASE_URL: args.baseUrl,
    AWS_REGION: args.region,
    AWS_DEFAULT_REGION: args.region,
    AWS_ACCESS_KEY_ID: args.accessKeyId,
    AWS_SECRET_ACCESS_KEY: args.secretAccessKey,
    AWS_SESSION_TOKEN: args.sessionToken,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    // Ask the SDK to pin its reminder date. Versions 0.3.220-0.3.283 still emit
    // the wall-clock date, so canonical-json.ts additionally normalizes only that
    // exact non-semantic reminder and records the rule in the manifest.
    CLAUDE_CODE_OVERRIDE_DATE: PROBE_SDK_DATE,
    // Prevent installation- or host-dependent bundled content from changing
    // the request while retaining the real Claude Code tool-bearing shape.
    CLAUDE_CODE_DISABLE_BUNDLED_SKILLS: '1',
    CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS: '1',
    // Minimize other host-dependent prompt content while retaining the actual
    // tool schemas and thinking request fields.
    CLAUDE_CODE_SIMPLE_SYSTEM_PROMPT: '1',
    // The SDK otherwise generates a fresh session component in metadata.user_id
    // for every request, making an exact request-body digest impossible to pin.
    // These are probe-only synthetic identifiers, not an authenticated identity.
    CLAUDE_CODE_ACCOUNT_UUID: '00000000-0000-4000-8000-000000000001',
    CLAUDE_CODE_SESSION_ID: '00000000-0000-4000-8000-000000000002',
    DISABLE_TELEMETRY: '1',
  };
  delete env.ANTHROPIC_BASE_URL;
  delete env.ANTHROPIC_API_KEY;
  delete env.ANTHROPIC_AUTH_TOKEN;
  return env;
}
