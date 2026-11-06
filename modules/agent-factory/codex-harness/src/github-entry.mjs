#!/usr/bin/env node
import { ResponsesBridgeError } from './responses-proxy.js';
import { readModelResponse } from './model-response.js';
/** GitHub invocation adapter for the shared, isolated official Codex SDK. */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { z } from 'zod';
import { planningPersona, planningContract, parsePlanning, directPlanningSchemas, planningIssueContext, planningCorrection } from './planning.js';
import { PlanningProvider } from './planning-provider.js';
import { retryModelHttp } from './model-http.js';
import { runAdmittedSession } from './session.js';
import { githubTools, hostCommand } from './github-tools.js';
import { verifySnapshot, personaSchema } from './persona.js';
import { HARNESS_CONTRACT_REVISION } from './admission.js';

const execute = promisify(execFile);
const shared = async name => {
  const loaded = await import(new URL(`../../dist/${name}.js`, import.meta.url));
  return loaded.default ?? loaded;
};

const contextSchema = z.strictObject({
  version: z.literal(1), persona: z.string(), repository: z.string().regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/),
  repositoryId: z.string().regex(/^[1-9][0-9]*$/),
  issue: z.number().int().positive(), snapshot: z.object({ definition: z.string(), digest: z.string(), instructions: z.string(), skillSources: z.string() }).strict(),
  capabilities: z.array(z.enum(['artifacts.publish', 'repository.read', 'story.create', 'agents.delegate', 'actions.read', 'actions.write', 'repository.write', 'change.create', 'change.merge'])).min(1).max(9),
  deadlineMs: z.number().int().positive(), maxTurns: z.number().int().min(1).max(20),
  maxTools: z.number().int().min(0).max(32),
  maxOutputTokens: z.number().int().min(1).max(4096), harnessRevision: z.literal(HARNESS_CONTRACT_REVISION),
});

async function main() {
  if (!process.argv.includes('--embedded')) throw new Error('GitHub persona requires the shared worker');
  const persona = process.env.AGENT_TYPE;
  const { admitCodexPersonaModel, codexPersonaOperation } = await shared('codex-persona-policy');
  const { startCodexPersonaControls } = await shared('codex-persona-controls');
  const { createCodexPersonaReporter } = await shared('codex-persona-reporting');
  const operational = persona === 'agent-codex-operations' && process.env.ADP_ENGINE_OPERATIONS_STORY === 'true';
  const executeSession = async () => {
    const lifetime = AbortSignal.timeout(2700000);
    const initial = await admitCodexPersonaModel(persona, lifetime);
    const context = contextSchema.parse(initial.context);
    const snapshot = verifySnapshot(context.snapshot);
    const definition = personaSchema.parse(JSON.parse(snapshot.definition));
    if (context.persona !== persona || definition.key !== (operational ? 'gpt-operations-story' : `gpt-${persona.replace(/^agent-codex-/, '')}`) ||
        definition.completionPolicy !== 'report' || context.repository !== process.env.TARGET_REPO || context.issue !== Number(process.env.ISSUE_NUMBER)) {
      throw new Error('GitHub persona binding mismatch');
    }
    const remaining = context.deadlineMs - Date.now();
    if (remaining <= 0) throw new Error('GitHub persona expired');
    const deadline = AbortSignal.any([lifetime, AbortSignal.timeout(remaining)]);
    const reporter = operational ? {
      log: () => {}, progress: () => {},
      async finish(result) {
        if (Buffer.byteLength(result.response) > 12000) throw new Error('Operations report exceeds bound');
        writeFileSync(process.env.ADP_OPERATIONS_REPORT_FILE, JSON.stringify({ run_id: initial.runId,
          report: result.response }), { mode: 0o600 });
      },
      async fail() {},
    } : await createCodexPersonaReporter({ ...context, model: initial.model });
    let controls;
    try {
      controls = await startCodexPersonaControls(reporter.log, deadline);
      const signal = AbortSignal.any([deadline, controls.signal]);
      const storyBoard = operational
        ? await (await shared('operations-story-board')).restoreOperationsStoryBoard(controls.explain)
        : undefined;
      const storyContext = process.env.ADP_OPERATIONS_STORY_CONTEXT || '';
      if (operational && Buffer.byteLength(storyContext) > 8192) throw new Error('Operations context exceeds bound');
      const input = operational ? {
        number: context.issue, ...JSON.parse(storyContext), comments: [],
      } : JSON.parse((await execute('gh', ['issue', 'view', String(context.issue), '--repo', context.repository,
        '--json', 'number,title,body,comments,url,state'], { signal, timeout: 15000, maxBuffer: Infinity })).stdout);
      if (operational && (input.repository !== context.repository || input.issue !== context.issue ||
          typeof input.title !== 'string' || input.title.length > 300 || !Array.isArray(input.workflows) || input.workflows.length > 20)) {
        throw new Error('Operations story binding mismatch');
      }
      if (!operational) {
        const repositoryIdentity = JSON.parse(await hostCommand('gh', ['api', `repos/${context.repository}`], signal));
        if (String(repositoryIdentity.id) !== context.repositoryId) throw new Error('Repository identity changed');
      }
      const revision = (await hostCommand('git', ['rev-parse', 'HEAD'], signal)).trim();
      if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Repository revision unavailable');
      const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(key => [key, context.capabilities]));
      const journal = async (kind, request, execute, active, effectKey) => {
        const binding = { operation_id: randomUUID(), request_digest: createHash('sha256').update(JSON.stringify(request)).digest('hex'), kind, ...(effectKey ? { effect_key: effectKey } : {}) };
        const admission = await codexPersonaOperation({ ...binding, action: 'claim' }, active).catch(() => { throw new ResponsesBridgeError('operation_claim_failed'); });
        if (admission.status === 'confirmed') return JSON.parse(admission.result);
        if (admission.status !== 'admitted') throw new Error('Operation requires reconciliation');
        const result = await execute();
        const serialized = JSON.stringify(result);
        const receipt = await codexPersonaOperation({ ...binding, action: 'settle', result: serialized }, active).catch(() => { throw new ResponsesBridgeError('operation_settlement_failed'); });
        if (receipt.status !== 'confirmed' || receipt.result !== serialized) throw new Error('Operation settlement was not confirmed');
        return result;
      };
      const current = async (active, checkpoint = true) => {
        active.throwIfAborted();
        if (checkpoint) await controls.checkpoint();
        const fresh = await admitCodexPersonaModel(persona, active).catch(() => { throw new ResponsesBridgeError('model_authority_failed'); });
        if (fresh.model !== initial.model || fresh.snapshotDigest !== initial.snapshotDigest ||
            fresh.generation !== initial.generation || JSON.stringify(fresh.context) !== JSON.stringify(initial.context)) {
          throw new Error('GitHub persona authority changed');
        }
        return fresh;
      };
      const reportProgress = (text, detail) => { reporter.progress(text, detail); controls.explain(text, detail); };
      let analysisStarted = false;
      const tools = githubTools({ persona, repository: context.repository, revision,
        capabilities: context.capabilities, ...(storyBoard ? { storyProgress: storyBoard.update } : {}) },
        (name, args, work, active) => controls.operation(async () => {
          // PreToolUse already owns the active effect ticket. A nested pause
          // checkpoint here could park the effect that pause is waiting to drain.
          await current(active, false);
          return journal('tool', { name, args, revision }, work, active);
        }), reportProgress);
      const planner = operational ? undefined : planningPersona(persona);
      if (!planner && !operational) throw new Error('Unsupported planning persona');
      const refs = new Set(['issue', ...input.comments.map(comment => `follow_up_input.${comment.id}`)]);
      let previousArtifact, artifactComment, artifactBlock;
      for (const comment of input.comments) {
        const match = comment.body.match(/```json\n([\s\S]*?)\n```/);
        if (!match) continue;
        try {
          const document = JSON.parse(match[1]);
          if (document.planning_persona === planner) {
            previousArtifact = directPlanningSchemas[planner].parse(document.artifact);
            artifactComment = comment.id; artifactBlock = match[0];
          }
        } catch { /* Other issue comments are not planning documents. */ }
      }
      const provider = operational ? undefined : new PlanningProvider('github', context.repository, context.issue, {
        read: async path => { signal.throwIfAborted(); await controls.checkpoint(); return JSON.parse(await hostCommand('gh', ['api', path], signal)); },
        write: async (path, body) => JSON.parse(await hostCommand('gh', ['api', '--method', 'POST', path, '--input', '-'], signal, body)),
        effect: (key, request, work) => controls.operation(async () => { await current(signal, false); if (controls.pendingSteering()) throw new Error('Amendment pending before planning effect'); return journal('planning', request, work, signal, key); }),
        dispatch: async (issue, persona, reason) => JSON.parse(await hostCommand('adp-trigger', ['--persona', `agent-${persona}`, '--issue', String(issue), '--repo', context.repository, '--reason', reason], signal)),
      });
      const backlog = planner === 'pm' ? await provider.backlog() : undefined;
      const invoke = async correction => runAdmittedSession({ runId: initial.runId, snapshot,
        policy: { personaKey: definition.key, personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk',
          harnessRevision: context.harnessRevision, canonicalModel: initial.model, allowedEfforts: [definition.effort],
          capabilityLayers: layers, limits: { ...definition.limits, maxTurns: context.maxTurns }, deadlineMs: context.deadlineMs },
        repository: { provider: 'github', repositoryId: context.repositoryId, sourceRevision: revision },
        source: { kind: 'github', eventId: initial.runId }, prompt: JSON.stringify(operational
          ? { task: { title: input.title, workflows: input.workflows, issue: input.number }, correction, saved_story_board: storyBoard?.context,
            output_contract: 'Own diagnosis, repair publication, CI verification, merge and deployment recovery until runtime verification passes. Use repair tools and Actions tools; observe automatic deployment before dispatching. Continue fixing when tests or runtime fail. Report PRs, revisions, runs, evidence and actual blockers. Do not claim engine acceptance.' }
          : { task: planningIssueContext(input, artifactComment, artifactBlock),
            source_refs: [...refs], previous_artifact: previousArtifact, backlog, correction, output_contract: planningContract(planner, true) }),
        signal,
      }, {
        assertCurrent: current,
        planningCapabilities: context.capabilities.filter(capability => capability === "story.create" || capability === "agents.delegate"),
        ...(tools.definitions.length ? { toolBroker: { definitions: tools.definitions,
          repositoryCapabilities: operational ? context.capabilities.filter(capability => capability !== 'artifacts.publish') : ['repository.read'], execute: async (name, args, active) => {
            const result = await tools.execute(name, args, active);
            if (!result.isError) storyBoard?.observeToolResult(name, result.content);
            if (!result.isError) {
              const ref = `${operational ? 'actions' : 'repository'}.${createHash('sha256').update(JSON.stringify({revision, name, args})).digest('hex')}`;
              refs.add(ref);
              return { ...result, content: `Source ref: ${ref}\n${result.content}` };
            }
            return result;
          } } } : {}),
        takeSteering: () => controls.takeSteering().map(text => {
          const ref = `follow_up_input.control.${createHash('sha256').update(text).digest('hex')}`;
          refs.add(ref); return `Source ref: ${ref}\n${text}`;
        }),
        async progress(event) {
          // Repository tools report actual paths and outcomes at their execution
          // boundary. SDK start/finish notifications are not new model explanations.
          if (event.type === 'turn.started' && !analysisStarted) {
            analysisStarted = true;
            reportProgress('Starting the repository assessment.');
          }
        },
        async model(request, active) {
          return controls.operation(() => retryModelHttp(async () => {
            const fresh = await current(active, false);
            const port = Number(process.env.SIGV4_PROXY_PORT ?? '9090');
            if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid model proxy');
            return journal('model', { request, model: initial.model }, async () => {
            // Intentional local model proxy: fixed loopback host/path, validated numeric
            // operator-configured port, and no redirects or caller-selected destination.
            // nosemgrep: tmp.gitlab.nodejs_scan.javascript-ssrf-rule-node_ssrf
            const response = await fetch(`http://127.0.0.1:${port}/openai/v1/responses`, {
              method: 'POST', redirect: 'error', signal: active,
              headers: { 'content-type': 'application/json', 'X-Adp-Model-Evidence': fresh.evidenceId },
              body: JSON.stringify({ ...request, model: initial.model, stream: true, store: false }),
            });
            if (!response.ok) {
              // Settle each observed rejection before retrying with a new claim.
              // Do not retain provider bodies (which can contain sensitive data).
              await response.body?.cancel();
              return { httpStatus: response.status };
            }
            return { operationStatus: 'confirmed', response: await readModelResponse(response, active) };
            }, active);
          }, active));
        },
      });
      let result, planned, correction;
      if (operational) {
        result = await invoke();
        if (!result.response || result.response.length > 12000) throw new Error('Operations report is missing or exceeds bound');
      } else {
        for (let attempt = 0; attempt < 2; attempt++) {
          result = await invoke(correction);
          try { planned = parsePlanning(result.response, planner, refs, previousArtifact, true); break; }
          catch (error) { if (attempt === 1) throw error; correction = planningCorrection(error); reporter.progress('Correcting planning structure and citations.'); }
        }
      }
      await current(signal);
      if (controls.pendingSteering()) throw new Error('A new amendment arrived before planning effects; rerun with the updated scope');
      let effects;
      if (!operational && !planned.clarification && 'stories' in planned.artifact && planned.artifact.publish_stories) {
        reporter.progress('Creating linked stories and recording dependency relationships.');
        effects = await provider.publishStories(planned.artifact);
      }
      if (!operational) {
        if (!planned.clarification && 'schedule' in planned.artifact && planned.artifact.schedule.length) effects = await provider.schedule(planned.artifact.schedule);
        result.response = `${planned.artifact.summary}\n\n\`\`\`json\n${JSON.stringify({ planning_persona: planner, artifact: planned.artifact }, null, 2)}\n\`\`\`${planned.clarification ? `\n\nClarification needed: ${planned.clarification}` : ''}${effects ? `\n\nConfirmed effects: ${JSON.stringify(effects)}` : ''}`;
      }
      await current(signal);
      if (controls.pendingSteering()) throw new Error('A final amendment arrived before publication; report is incomplete');
      await controls.operation(async () => {
        if (controls.pendingSteering()) throw new Error('An amendment arrived at publication; report remains incomplete');
        await journal('report', { definition: snapshot.digest, response: result.response }, async () => {
          await reporter.finish(result, { definition: snapshot.digest, model: initial.model,
            modelPolicy: initial.snapshotDigest, harness: context.harnessRevision });
          return { published: true };
        }, signal);
      });
    } catch (error) { await reporter.fail(error); throw error; }
    finally { await controls?.close(); }
  };
  if (operational) await executeSession();
  else {
    const tokenLifecycle = await import(new URL('../../codex-reviewer/dist/token-lifecycle.js', import.meta.url));
    await tokenLifecycle.withGitHubTokenRenewal(executeSession);
  }
}
main().catch(error => {
  // Name the failure class so an operator can tell a binding mismatch from a model
  // or tool failure without a debugger. Only the error's own message is printed,
  // bounded and single-line; provider bodies and tokens never reach this path.
  const detail = String(error && error.message ? error.message : error).replace(/\s+/g, ' ').slice(0, 300);
  console.error(`GitHub Codex persona did not complete with verified authority and output: ${detail}`);
  process.exitCode = 1;
});
