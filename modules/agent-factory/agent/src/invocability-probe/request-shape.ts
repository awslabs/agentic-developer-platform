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

export function probeSdkEnvironment(args: {
  baseUrl: string;
  region: string;
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    CLAUDE_CODE_USE_BEDROCK: '1',
    ANTHROPIC_BEDROCK_BASE_URL: args.baseUrl,
    AWS_REGION: args.region,
    AWS_DEFAULT_REGION: args.region,
    AWS_ACCESS_KEY_ID: args.accessKeyId,
    AWS_SECRET_ACCESS_KEY: args.secretAccessKey,
    AWS_SESSION_TOKEN: args.sessionToken,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    // Ask the SDK to pin its reminder date. Version 0.3.220 still emits the
    // wall-clock date, so canonical-json.ts additionally normalizes only that
    // exact non-semantic reminder path and records the rule in the manifest.
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
