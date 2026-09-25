/** Audited SDK orchestration: no model-generated shell/filesystem/network tools. */
import { setTimeout as delay } from 'node:timers/promises';
import { readFile, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { query, tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';
import { OPERATIONS, SKILLS, ProtocolError } from './protocol.mjs';
import { startProxy } from './model-proxy.mjs';

const text = z.string().min(1).max(4000);
const refs = z.array(z.string().min(1).max(200)).max(50);
export const REPORT_SCHEMA = {
  summary: text,
  findings: z.array(z.object({ statement: z.string().min(1).max(2000), evidence_refs: refs, confidence: z.enum(['low', 'medium', 'high']).optional() }).strict()).max(50),
  uncertainties: z.array(z.string().min(1).max(1000)).max(50), recommendations: z.array(z.string().min(1).max(1000)).max(50),
  evidence_refs: z.array(z.object({ ref: z.string().max(200), source: z.enum(['instructions', 'inputs', 'artifact', 'follow_up_input']), artifact_id: z.string().optional() }).strict()).max(100),
};
const SAMPLE_SCHEMA = { sample_s3_uri: z.string().min(1).max(2000), focus: z.array(z.string().max(500)).max(20).optional(), yara_rules: z.array(z.string().max(200)).max(20).optional() };
export const OPERATION_SCHEMAS = {
  triage: SAMPLE_SCHEMA, static: SAMPLE_SCHEMA, dynamic: SAMPLE_SCHEMA,
  result: { job_id: z.string().min(1).max(200) },
  url_analysis: { url: z.string().url().max(4000) },
  enrich: { sha256: z.string().regex(/^[a-f0-9]{64}$/) },
};
const reply = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
export const TOOL_NAMES = ['read_skill', 'progress', 'request_input', 'submit_report', ...OPERATIONS].map(name => 'mcp__cyber__' + name);

export function groundedReport(value, evidence) {
  const report = z.object(REPORT_SCHEMA).strict().parse(value);
  const accepted = report.evidence_refs.filter(record => {
    const actual = evidence.get(record.ref);
    return actual && actual.source === record.source && actual.artifact_id === record.artifact_id;
  });
  const known = new Set(accepted.map(record => record.ref));
  const grounded = []; const unsupported = [];
  for (const finding of report.findings) {
    if (finding.evidence_refs.length && finding.evidence_refs.every(ref => known.has(ref))) grounded.push(finding);
    else {
      const detail = 'Unsupported finding: ' + finding.statement;
      for (let offset = 0; offset < detail.length; offset += 1000) unsupported.push(detail.slice(offset, offset + 1000));
    }
  }
  return { ...report, findings: grounded, evidence_refs: accepted,
    uncertainties: [...report.uncertainties, ...unsupported] };
}

export function cyberTools(bridge, { skillDirectory = fileURLToPath(new URL('../skills/', import.meta.url)), read = readFile } = {}) {
  const mutations = new Map();
  const unfinishedJobs = new Set();
  let lastPoll = 0;
  const handlers = OPERATIONS.map(operation => tool(operation,
    `Run the authorized cyber ${operation} operation through the Task host. Only a confirmed receipt with an artifact is citable evidence. Pending job results need paced result polling; never retry an unknown submission.`,
    OPERATION_SCHEMAS[operation], async payload => {
      const key = JSON.stringify([operation, Object.fromEntries(Object.entries(payload).sort(([a], [b]) => a.localeCompare(b)))]);
      if (operation !== 'result' && mutations.has(key)) return mutations.get(key);
      const operationResult = (async () => {
        if (operation === 'result') {
          const wait = Math.max(0, 1000 - (Date.now() - lastPoll));
          await delay(wait, undefined, { signal: bridge.controller.signal });
          lastPoll = Date.now();
        }
        if (bridge.controller.signal.aborted) throw new Error('cancelled');
        bridge.progress(`Requesting authorized ${operation} evidence.`);
        const receipt = await bridge.cyber(operation, payload);
        const job = receipt.result?.job_id;
        if (job && receipt.result?.status === 'pending') unfinishedJobs.add(job);
        if (operation === 'result' && receipt.operation_status === 'confirmed' &&
            ['completed', 'failed', 'cancelled'].includes(receipt.result?.status)) unfinishedJobs.delete(payload.job_id);
        return { ...reply(receipt), isError: receipt.operation_status !== 'confirmed' };
      })();
      if (operation !== 'result') mutations.set(key, operationResult);
      return operationResult;
    }));
  return [...handlers,
    tool('read_skill', 'Read a packaged cyber analysis skill. Legacy GitHub/AWS delivery instructions are replaced by this Task MCP transport.', { name: z.enum(SKILLS) }, async ({ name }) => {
      const bytes = await read(`${skillDirectory}/${name}/SKILL.md`);
      if (bytes.length > 32768) throw new ProtocolError('skill text exceeds bound');
      return reply({ name, instructions: bytes.toString('utf8'), transport: 'Task MCP only; no GitHub, shell, direct credentials or arbitrary code.' });
    }),
    tool('progress', 'Publish a concise authored observation or completed analysis step. Do not publish reasoning, percentages or invented findings.', { message: z.string().min(1).max(2000) }, async ({ message }) => { bridge.progress(message); return reply({ recorded: true }); }),
    tool('request_input', 'Ask the Task caller for required missing evidence; wait for a host-authorized input turn. Caller may be a service principal, not a human.', { prompt: z.string().min(1).max(2000) }, async ({ prompt }) => reply({ input: await bridge.ask(prompt), evidence_refs: [...bridge.evidence.values()].filter(record => record.source === 'follow_up_input') })),
    tool('submit_report', 'Finish with a structured grounded report. Cite only exact evidence references returned by the host or initial inputs; unavailable stages are uncertainties.', REPORT_SCHEMA, async report => { if (unfinishedJobs.size) return { ...reply({ error: 'Poll outstanding jobs to a terminal result before submitting the report.', job_ids: [...unfinishedJobs] }), isError: true }; bridge.report = groundedReport(report, bridge.evidence); return reply({ accepted: true }); }),
  ];
}

export function sdkEnvironment(proxy) {
  const env = {};
  for (const key of ['HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_ALL']) if (process.env[key]) env[key] = process.env[key];
  return { ...env, ANTHROPIC_BASE_URL: proxy.url, ANTHROPIC_AUTH_TOKEN: proxy.token,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1', DISABLE_ERROR_REPORTING: '1' };
}

export async function runCyber(start, bridge, { sdkQuery = query, proxyFactory = startProxy, toolOptions = {} } = {}) {
  const maxTokens = start.limits?.max_output_tokens_per_turn;
  const maxTurns = start.limits?.max_turns;
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || !Number.isInteger(maxTurns) || maxTurns < 1) throw new ProtocolError('missing host limits');
  const proxy = await proxyFactory(bridge, { maxTokens });
  let session, home;
  try {
    home = await mkdtemp(join(tmpdir(), 'adp-task-cyber-'));
    bridge.progress('Starting cyber investigation with host-authorized tools.', 'evidence_inventory');
    session = sdkQuery({ prompt: JSON.stringify({ instructions: start.instructions, inputs: start.inputs || {}, acceptance_criteria: start.acceptance_criteria || [],
      artifacts: start.artifacts || [], evidence_refs: [...bridge.evidence.values()] }), options: {
      cwd: home, model: 'sonnet', maxTurns,
      env: { ...sdkEnvironment(proxy), HOME: home }, settingSources: [], persistSession: false,
      tools: [], allowedTools: TOOL_NAMES,
      mcpServers: { cyber: createSdkMcpServer({ name: 'cyber', version: '1.0.0', tools: cyberTools(bridge, toolOptions) }) },
      abortController: bridge.controller,
      canUseTool: async (name, input) => TOOL_NAMES.includes(name) ? { behavior: 'allow', updatedInput: input } : { behavior: 'deny', message: 'Only the exact Task cyber MCP operations are authorized.' },
      hooks: { PreToolUse: [{ matcher: '.*', hooks: [async input => ({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: TOOL_NAMES.includes(input.tool_name) ? 'allow' : 'deny', permissionDecisionReason: 'Task cyber MCP allowlist' } })] }] },
      systemPrompt: 'You are agent-task-cyber, a cyber investigator using the existing seven-stage malware and URL analysis skills. Read the relevant packaged skills with read_skill. ' +
        'This is a Task API invocation, not a GitHub workflow: never post issues/comments, use GitHub identity, call AWS directly, run shell/code, or fetch arbitrary URLs. ' +
        'The only execution methods are the provided Task MCP operations. These replace all legacy skill shell, queue, credential and publication instructions. ' +
        'For a file use triage, enrich, static, dynamic as justified, then correlate and form a verdict. For URL inputs use url_analysis without requiring a sample. ' +
        'Pending jobs are not completed evidence: poll result with their job_id. Unknown submissions must never be repeated. Denied/unavailable stages must be disclosed. ' +
        'Progress must be authored observations, not private reasoning. Ask for missing input using request_input. The caller may be an automated service. ' +
        'Only cite exact initial evidence_refs or host-returned artifact.artifact_id references, with source artifact for tool artifacts. ' +
        'Do not guess tool results. Submit the final grounded Task report using submit_report, then finish. If a skill describes unsupported operations, state the limitation rather than inventing success.',
    } });
    for await (const message of session) {
      // SDK transcripts/tool/thinking messages are never forwarded as public progress.
      if (message.type === 'result') {
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
