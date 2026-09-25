/** Generic Claude Agent SDK lifecycle; personas supply skills, tools and prompt. */
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ProtocolError } from './protocol.mjs';

export function sdkEnvironment(proxy) {
  const env = {};
  for (const key of ['HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL']) if (process.env[key]) env[key] = process.env[key];
  return { ...env, ANTHROPIC_BASE_URL: proxy.url, ANTHROPIC_AUTH_TOKEN: proxy.token,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1', DISABLE_ERROR_REPORTING: '1' };
}

export async function runTaskSdk(start, bridge, { sdkQuery, proxyFactory, toolNames, mcpServers, systemPrompt }) {
  const maxTokens = start.limits?.max_output_tokens_per_turn;
  const maxTurns = start.limits?.max_turns;
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || !Number.isInteger(maxTurns) || maxTurns < 1) throw new ProtocolError('missing host limits');
  const proxy = await proxyFactory(bridge, { maxTokens });
  let session, home;
  try {
    home = await mkdtemp(join(tmpdir(), 'adp-task-sdk-'));
    session = sdkQuery({ prompt: JSON.stringify({ instructions: start.instructions, inputs: start.inputs || {}, acceptance_criteria: start.acceptance_criteria || [],
      artifacts: start.artifacts || [], evidence_refs: [...bridge.evidence.values()] }), options: {
      cwd: home, model: 'sonnet', maxTurns,
      env: { ...sdkEnvironment(proxy), HOME: home }, settingSources: [], persistSession: false,
      tools: [], allowedTools: toolNames,
      mcpServers,
      abortController: bridge.controller,
      canUseTool: async (name, input) => toolNames.includes(name) ? { behavior: 'allow', updatedInput: input } : { behavior: 'deny', message: 'Only the exact Task MCP operations are authorized.' },
      hooks: { PreToolUse: [{ matcher: '.*', hooks: [async input => ({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: toolNames.includes(input.tool_name) ? 'allow' : 'deny', permissionDecisionReason: 'Task MCP allowlist' } })] }] },
      systemPrompt,
    } });
    for await (const message of session) {
      // SDK transcripts/tool/thinking messages are never forwarded as public progress.
      if (message.type === 'result') {
        // An accepted report is complete even when the last permitted turn submitted it.
        if (bridge.report && message.subtype === 'error_max_turns') return bridge.report;
        if (message.subtype === 'error_max_turns') throw new ProtocolError('SDK model-turn limit reached before a grounded report was accepted');
        if (message.is_error || message.subtype !== 'success' || !bridge.report) throw new ProtocolError('SDK did not produce a successful grounded report');
        return bridge.report;
      }
    }
    throw new ProtocolError('SDK ended without a result');
  } finally {
    session?.close();
    await proxy.close();
    if (home) await rm(home, { recursive: true, force: true });
  }
}
