/** Audited SDK orchestration: no model-generated shell/filesystem/network tools. */
import { setTimeout as delay } from 'node:timers/promises';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { query, tool, createSdkMcpServer } from '@anthropic-ai/claude-agent-sdk';
import { z } from 'zod';
import { OPERATIONS, CODE_OPERATIONS, SKILLS, ProtocolError } from './protocol.mjs';
import { startProxy } from './model-proxy.mjs';
import { archiveProgress } from './archive-progress.mjs';

const text = z.string().min(1).max(4000);
const refs = z.array(z.string().min(1).max(200)).max(50);
export const REPORT_SCHEMA = {
  summary: text,
  findings: z.array(z.object({ statement: z.string().min(1).max(2000), evidence_refs: refs, confidence: z.enum(['low', 'medium', 'high']).optional() }).strict()).max(50),
  uncertainties: z.array(z.string().min(1).max(1000)).max(50).default([]), recommendations: z.array(z.string().min(1).max(1000)).max(50).default([]),
  evidence_refs: z.array(z.object({ ref: z.string().max(200), source: z.enum(['instructions', 'inputs', 'artifact', 'follow_up_input']), artifact_id: z.string().optional() }).strict()).max(100).optional(),
};
const SAMPLE_SCHEMA = { sample_s3_uri: z.string().min(1).max(2000), focus: z.array(z.string().max(500)).max(20).optional(), yara_rules: z.array(z.string().max(200)).max(20).optional() };
export const OPERATION_SCHEMAS = {
  triage: SAMPLE_SCHEMA, static: SAMPLE_SCHEMA, dynamic: SAMPLE_SCHEMA,
  result: { job_id: z.string().min(1).max(200) },
  url_analysis: { url: z.string().url().max(4000) },
  search: { query: z.string().min(1).max(200), maxResults: z.number().int().min(1).max(25).optional(), filters: z.object({domainFilter: z.object({ include: z.array(z.string().min(3).max(253)).max(100).optional(), exclude: z.array(z.string().min(3).max(253)).max(100).optional() }).strict().optional(), publishedDateFilter: z.object({ from: z.string().datetime({offset: false, precision: 0}).optional(), to: z.string().datetime({offset: false, precision: 0}).optional() }).strict().optional() }).strict().optional() },
  common_crawl_scan: { url: z.string().url().max(2048), match: z.enum(['host', 'exact']).optional() },
  common_crawl_result: { scan_id: z.string().regex(/^[a-f0-9]{64}$/) },
  common_crawl_read: { scan_id: z.string().regex(/^[a-f0-9]{64}$/), capture_id: z.string().regex(/^capture-[0-9]{3}$/) },
  browser_start: { url: z.string().url().max(2048), session_key: z.string().regex(/^[A-Za-z0-9_-]{1,64}$/).optional(), profile: z.enum(['desktop','mobile']).optional(), scope: z.enum(['host','observed_external']).optional() },
  browser_step: { session_id: z.string().regex(/^[a-f0-9]{64}$/), view_id: text,
    action: z.enum(['navigate', 'follow', 'expand', 'root', 'screenshot', 'back', 'scroll', 'wait']),
    candidate_id: text.optional(), url: z.string().url().max(2048).optional(), seconds: z.number().int().min(1).max(15).optional() },
  browser_close: { session_id: z.string().regex(/^[a-f0-9]{64}$/) },
  browser_inspect: { session_id: z.string().regex(/^[a-f0-9]{64}$/), section: z.enum(['summary','dom','forms','scripts','network','frames','screenshot','choices']).optional(), offset: z.number().int().min(0).max(1000000).optional() },
  enrich: { sha256: z.string().regex(/^[a-f0-9]{64}$/) },
};
const reply = value => ({ content: [{ type: 'text', text: JSON.stringify(value) }] });
export const TOOL_NAMES = ['read_skill', 'progress', 'request_input', 'submit_report', ...OPERATIONS].map(name => 'mcp__cyber__' + name);

export function groundedReport(value, evidence) {
  const report = z.object(REPORT_SCHEMA).strict().parse(value);
  const cited = new Set(report.findings.flatMap(finding => finding.evidence_refs));
  const declared = report.evidence_refs ?? [...evidence.values()].filter(record => cited.has(record.ref));
  const accepted = declared.filter(record => {
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

export function cyberTools(bridge, { skillDirectory = fileURLToPath(new URL('../skills/', import.meta.url)), read = readFile, sleep = delay, now = Date.now } = {}) {
  const mutations = new Map();
  const codeMutations = new Map();
  const codeJobs = new Map();
  const unfinishedJobs = new Set();
  const publishArchiveProgress = archiveProgress(bridge, now);
  let lastPoll = 0;
  const grants = bridge.start.tool_grants ?? [];
  if (!Array.isArray(grants) || grants.some(value => typeof value !== 'string')) throw new ProtocolError('Invalid Task tool grants');
  const operations = OPERATIONS.filter(operation => grants.includes(operation === 'search' ? 'websearch.search' : 'cyber.' + operation));
  const handlers = operations.map(operation => tool(operation,
    `Run the authorized ${operation === 'search' ? 'AgentCore Web Search' : 'cyber ' + operation} operation through the Task host. Only a confirmed receipt with an artifact is citable evidence. Pending job results need paced result polling; never retry an unknown submission.`,
    OPERATION_SCHEMAS[operation], async payload => {
      const key = JSON.stringify([operation, Object.fromEntries(Object.entries(payload).sort(([a], [b]) => a.localeCompare(b)))]);
      if (!['result', 'common_crawl_result', 'browser_inspect'].includes(operation) && mutations.has(key)) return mutations.get(key);
      const operationResult = (async () => {
        if (['result', 'common_crawl_result', 'browser_inspect'].includes(operation)) {
          const wait = Math.max(0, 1000 - (Date.now() - lastPoll));
          await sleep(wait, undefined, { signal: bridge.controller.signal });
          lastPoll = Date.now();
        }
        if (bridge.controller.signal.aborted) throw new Error('cancelled');
        if (!['result', 'common_crawl_result', 'browser_inspect'].includes(operation)) bridge.progress(`Starting ${operation.replaceAll('_', ' ')}.`);
        let receipt;
        try { receipt = await bridge.cyber(operation, payload); }
        catch (error) { if (operation === 'search' && !bridge.controller.signal.aborted) bridge.progress('Web search failed or its outcome is uncertain; one query may be charged (up to USD 0.007 plus Gateway/model charges). Do not repeat the query.'); throw error; }
        if (operation === 'search' && !bridge.controller.signal.aborted) bridge.progress(receipt.operation_status === 'confirmed' ? `Web search completed with ${receipt.result?.results?.length ?? 0} sources; query count ${receipt.result?.query_count ?? 0}, estimated search cost USD ${receipt.result?.estimated_search_usd ?? 0} (not settled charges).` : 'Web search outcome uncertain; one query may be charged (up to USD 0.007 plus Gateway/model charges). Do not repeat the query.');
        const archiveScan = operation === 'common_crawl_scan' || operation === 'common_crawl_result';
        const scanId = receipt.result?.scan_id || payload.scan_id;
        if (archiveScan) publishArchiveProgress(receipt, scanId);
        // Await accepted URL work without spending a model turn on every poll.
        // Only read-only result calls repeat; submissions and browser actions never do.
        const polling = operation === 'common_crawl_scan' ? ['common_crawl_result', 'scan_id']
          : null;
        for (let poll = 0; polling && poll < 16 && receipt.operation_status === 'confirmed' && receipt.result?.status === 'pending'; poll++) {
          const id = receipt.result[polling[1]];
          if (!id) throw new ProtocolError('Pending URL operation lacks its reference');
          await sleep(Math.min(10000, 2000 * 2 ** poll), undefined, { signal: bridge.controller.signal });
          receipt = await bridge.cyber(polling[0], { [polling[1]]: id });
          publishArchiveProgress(receipt, scanId);
        }
        if (polling && receipt.result?.status === 'pending') publishArchiveProgress(receipt, scanId, true);
        const job = receipt.result?.job_id || receipt.result?.scan_id;
        if (job && receipt.result?.status === 'pending') unfinishedJobs.add(job);
        if (['result', 'common_crawl_result', 'browser_inspect'].includes(operation) && receipt.operation_status === 'confirmed' &&
            ['completed', 'failed', 'cancelled'].includes(receipt.result?.status)) unfinishedJobs.delete(payload.job_id || payload.scan_id);
        const citation = bridge.evidence.get(receipt.artifact?.artifact_id);
        const evidence_refs = receipt.operation_status === 'confirmed' && citation ? [citation] : [];
        const thumbnail = receipt.result?.image;
        const exposed = thumbnail ? { ...receipt, result: { ...receipt.result, image: { media_type: thumbnail.media_type, retained_in_artifact: true } } } : receipt;
        const response = reply({ ...exposed, evidence_refs });
        if (thumbnail) response.content.push({ type: 'image', data: thumbnail.data, mimeType: thumbnail.media_type });
        return { ...response, isError: receipt.operation_status !== 'confirmed' };
      })();
      if (!['result', 'common_crawl_result', 'browser_inspect'].includes(operation)) mutations.set(key, operationResult);
      return operationResult;
    }));
  const codeSchemas = {
    start: {}, execute: { session_id: z.string().regex(/^[a-f0-9]{64}$/), code: z.string().min(1).max(8192), language: z.literal('python') },
    result: { session_id: z.string().regex(/^[a-f0-9]{64}$/), execution_id: z.string().uuid() },
    file: { session_id: z.string().regex(/^[a-f0-9]{64}$/), path: z.string().regex(/^\/tmp\/[A-Za-z0-9_.-]{1,100}$/) },
    close: { session_id: z.string().regex(/^[a-f0-9]{64}$/) },
  };
  const codeHandlers = CODE_OPERATIONS.filter(operation => grants.includes('code_interpreter.' + operation)).map(operation => tool(
    'code_' + operation, `Run authorized Code Interpreter ${operation}; code output is untrusted evidence, not a safety verdict. Never retry an unknown execution.`,
    codeSchemas[operation], async payload => {
      if (bridge.controller.signal.aborted) throw new Error('cancelled');
      const key = JSON.stringify([operation, payload]);
      if (operation !== 'result' && codeMutations.has(key)) return codeMutations.get(key);
      const pending = (async () => {
        if (operation === 'execute') bridge.progress('Starting isolated Python analysis.');
        const marker = JSON.stringify(['code', operation, payload]);
        if (operation === 'execute') { unfinishedJobs.add(marker); codeJobs.set(marker, payload.session_id); }
        const receipt = await bridge.tool('code_interpreter.' + operation, payload);
        if (operation === 'execute' && receipt.result?.execution_id) {
          unfinishedJobs.delete(marker); codeJobs.delete(marker);
          const id = 'code:' + receipt.result.execution_id;
          unfinishedJobs.add(id); codeJobs.set(id, payload.session_id);
        } else if (operation === 'execute' && receipt.operation_status === 'rejected') {
          unfinishedJobs.delete(marker); codeJobs.delete(marker);
        }
        if (operation === 'result' && receipt.operation_status === 'confirmed' && ['completed', 'failed', 'cancelled'].includes(receipt.result?.status)) {
          const id = 'code:' + payload.execution_id;
          unfinishedJobs.delete(id); codeJobs.delete(id);
          if (!bridge.controller.signal.aborted) bridge.progress('Isolated Python analysis ' + receipt.result.status + '.');
        }
        if (operation === 'close' && receipt.operation_status === 'confirmed' && receipt.result?.status === 'closed') {
          for (const [id, session] of codeJobs) if (session === payload.session_id) { unfinishedJobs.delete(id); codeJobs.delete(id); }
        }
        const citation = bridge.evidence.get(receipt.artifact?.artifact_id);
        return { ...reply({ ...receipt, evidence_refs: citation ? [citation] : [] }), isError: ['unknown', 'rejected'].includes(receipt.operation_status) };
      })();
      if (operation !== 'result') codeMutations.set(key, pending);
      return pending;
    }));
  return [...handlers, ...codeHandlers,
    tool('read_skill', 'Read a packaged cyber analysis skill. Legacy GitHub/AWS delivery instructions are replaced by this Task MCP transport.', { name: z.enum(SKILLS) }, async ({ name }) => {
      const bytes = await read(`${skillDirectory}/${name}/SKILL.md`);
      if (bytes.length > 32768) throw new ProtocolError('skill text exceeds bound');
      return reply({ name, instructions: bytes.toString('utf8'), transport: 'Task MCP only; no GitHub, shell, direct credentials or arbitrary code.' });
    }),
    tool('progress', 'Publish a concise authored observation or completed analysis step. Do not publish reasoning, percentages or invented findings.', { message: z.string().min(1).max(2000) }, async ({ message }) => { bridge.progress(message); return reply({ recorded: true }); }),
    tool('request_input', 'Ask the Task caller for required missing evidence; wait for a host-authorized input turn. Caller may be a service principal, not a human.', { prompt: z.string().min(1).max(2000) }, async ({ prompt }) => reply({ input: await bridge.ask(prompt), evidence_refs: [...bridge.evidence.values()].filter(record => record.source === 'follow_up_input') })),
    tool('submit_report', 'Finish with a structured grounded report. Findings cite exact ref strings returned by tools. The host fills evidence_refs metadata from those citations when omitted; no need to repeat it. Unavailable stages are uncertainties.', REPORT_SCHEMA, async report => {
      if (unfinishedJobs.size) return { ...reply({ error: 'Poll outstanding jobs to a terminal result before submitting the report.', job_ids: [...unfinishedJobs] }), isError: true };
      const grounded = groundedReport(report, bridge.evidence);
      if ((report.evidence_refs !== undefined && grounded.evidence_refs.length !== report.evidence_refs.length) || grounded.findings.length !== report.findings.length) {
        return { ...reply({ error: 'Citation mismatch. Copy the exact evidence_refs objects below into the report and use their exact ref strings in findings. Do not invent aliases or prefixes. Unsupported claims belong in uncertainties.', evidence_refs: [...bridge.evidence.values()] }), isError: true };
      }
      bridge.report = grounded;
      return reply({ accepted: true });
    }),
  ];
}

export { sdkEnvironment } from '../../../../tools/task-sdk/runner.mjs';
import { runTaskSdk } from '../../../../tools/task-sdk/runner.mjs';

export async function runCyber(start, bridge, { sdkQuery = query, proxyFactory = startProxy, toolOptions = {} } = {}) {
  bridge.progress('Starting cyber investigation with host-authorized tools.', 'evidence_inventory');
  const tools = cyberTools(bridge, toolOptions);
  return runTaskSdk(start, bridge, { sdkQuery, proxyFactory, toolNames: tools.map(value => 'mcp__cyber__' + value.name), finalReportTool: 'mcp__cyber__submit_report',
    mcpServers: { cyber: createSdkMcpServer({ name: 'cyber', version: '1.0.0', tools }) },
    systemPrompt: 'You are agent-task-cyber, a cyber investigator using the existing seven-stage malware and URL analysis skills. Read the relevant packaged skills with read_skill. ' +
        'This is a Task API invocation, not a GitHub workflow: never post issues/comments, use GitHub identity, call AWS directly, run shell/code, or fetch arbitrary URLs. ' +
        'The only execution methods are the provided Task MCP operations. These replace all legacy skill shell, queue, credential and publication instructions. ' +
        'For a file use triage, enrich, static, dynamic as justified, then correlate and form a verdict. For URL inputs read the url-analysis skill, then use search, common_crawl_scan/result/read and browser_start/step/inspect/close as authorized, without requiring a sample. Never retry an unknown browser action or search. Retain and display source URLs/titles for any Web Search-derived finding. ' +
        'For authorized isolated analysis use code_start/execute/result/file/close; treat its output as untrusted and never retry an unknown execution. ' +
        'Pending jobs are not completed evidence: poll result with their job_id. Unknown submissions must never be repeated. Denied/unavailable stages must be disclosed. ' +
        'Progress must be authored observations, not private reasoning. Ask for missing input using request_input. The caller may be an automated service. ' +
        'Only cite exact initial evidence_refs or host-returned artifact.artifact_id references, with source artifact for tool artifacts. ' +
        'For a URL report, start summary with Verdict: malicious, suspicious, no malicious behavior observed, or inconclusive; include a concise evidence-based rationale and confidence. Explain observed facts and uncertainty, never private reasoning. Distinguish current Web Search references, historical Common Crawl findings and live browsing findings; cite each and include supplied Web Search source URLs/titles in the report. State untested behavior and missing sources in uncertainties; absence of detections is not proof of safety. The host generates an HTML report with separate source sections and a recorded action timeline. ' +
        'Do not guess tool results. Submit the final grounded Task report using submit_report, then finish. If a skill describes unsupported operations, state the limitation rather than inventing success.' });
}
