#!/usr/bin/env node
import { ResponsesBridgeError } from './responses-proxy.js';
import { readModelResponse } from './model-response.js';
/** GitHub invocation adapter for the shared, isolated official Codex SDK. */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
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
  capabilities: z.array(z.enum(['artifacts.publish', 'repository.read', 'story.create', 'agents.delegate'])).min(1).max(4),
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
  const tokenLifecycle = await import(new URL('../../codex-reviewer/dist/token-lifecycle.js', import.meta.url));
  await tokenLifecycle.withGitHubTokenRenewal(async () => {
    const lifetime = AbortSignal.timeout(2700000);
    const initial = await admitCodexPersonaModel(persona, lifetime);
    const context = contextSchema.parse(initial.context);
    const snapshot = verifySnapshot(context.snapshot);
    const definition = personaSchema.parse(JSON.parse(snapshot.definition));
    if (context.persona !== persona || definition.key !== `gpt-${persona.replace(/^agent-codex-/, '')}` ||
        definition.completionPolicy !== 'report' || context.repository !== process.env.TARGET_REPO || context.issue !== Number(process.env.ISSUE_NUMBER)) {
      throw new Error('GitHub persona binding mismatch');
    }
    const remaining = context.deadlineMs - Date.now();
    if (remaining <= 0) throw new Error('GitHub persona expired');
    const deadline = AbortSignal.any([lifetime, AbortSignal.timeout(remaining)]);
    const reporter = await createCodexPersonaReporter({ ...context, model: initial.model });
    let controls;
    try {
      controls = await startCodexPersonaControls(reporter.log, deadline);
      const signal = AbortSignal.any([deadline, controls.signal]);
      const issue = await execute('gh', ['issue', 'view', String(context.issue), '--repo', context.repository,
        '--json', 'number,title,body,comments,url,state'], { signal, timeout: 15000, maxBuffer: Infinity });
      const input = JSON.parse(issue.stdout);
      const repositoryIdentity = JSON.parse(await hostCommand('gh', ['api', `repos/${context.repository}`], signal));
      if (String(repositoryIdentity.id) !== context.repositoryId) throw new Error('Repository identity changed');
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
      const tools = githubTools({ persona, repository: context.repository, revision, capabilities: context.capabilities },
        (name, args, work, active) => controls.operation(async () => {
          // PreToolUse already owns the active effect ticket. A nested pause
          // checkpoint here could park the effect that pause is waiting to drain.
          await current(active, false);
          return journal('tool', { name, args, revision }, work, active);
        }));
      const planner = planningPersona(persona);
      if (!planner) throw new Error('Unsupported planning persona');
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
      const provider = new PlanningProvider('github', context.repository, context.issue, {
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
        source: { kind: 'github', eventId: initial.runId }, prompt: JSON.stringify({ task: planningIssueContext(input, artifactComment, artifactBlock),
          source_refs: [...refs], previous_artifact: previousArtifact, backlog, correction, output_contract: planningContract(planner, true) }),
        signal,
      }, {
        assertCurrent: current,
        planningCapabilities: context.capabilities.filter(capability => capability === "story.create" || capability === "agents.delegate"),
        ...(tools.definitions.length ? { toolBroker: { definitions: tools.definitions,
          repositoryCapabilities: ['repository.read'], execute: async (name, args, active) => {
            const result = await tools.execute(name, args, active);
            if (!result.isError) {
              const ref = `repository.${createHash('sha256').update(JSON.stringify({revision, name, args})).digest('hex')}`;
              refs.add(ref);
              return { ...result, content: `Source ref: ${ref}\n${result.content}` };
            }
            return result;
          } } } : {}),
        takeSteering: () => controls.takeSteering().map(text => {
          const ref = `follow_up_input.control.${createHash('sha256').update(text).digest('hex')}`;
          refs.add(ref); return `Source ref: ${ref}\n${text}`;
        }),
        async progress() {
          const text = 'Working through the admitted task and its evidence.';
          reporter.progress(text);
          controls.explain(text);
        },
        async model(request, active) {
          return controls.operation(() => retryModelHttp(async () => {
            const fresh = await current(active, false);
            const port = Number(process.env.SIGV4_PROXY_PORT ?? '9090');
            if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid model proxy');
            return journal('model', { request, model: initial.model }, async () => {
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
      for (let attempt = 0; attempt < 2; attempt++) {
        result = await invoke(correction);
        try { planned = parsePlanning(result.response, planner, refs, previousArtifact, true); break; }
        catch (error) { if (attempt === 1) throw error; correction = planningCorrection(error); reporter.progress('Correcting planning structure and citations.'); }
      }
      await current(signal);
      if (controls.pendingSteering()) throw new Error('A new amendment arrived before planning effects; rerun with the updated scope');
      let effects;
      if (!planned.clarification && 'stories' in planned.artifact && planned.artifact.publish_stories) {
        reporter.progress('Creating linked stories and recording dependency relationships.');
        effects = await provider.publishStories(planned.artifact);
      }
      if (!planned.clarification && 'schedule' in planned.artifact && planned.artifact.schedule.length) effects = await provider.schedule(planned.artifact.schedule);
      result.response = `${planned.artifact.summary}\n\n\`\`\`json\n${JSON.stringify({ planning_persona: planner, artifact: planned.artifact }, null, 2)}\n\`\`\`${planned.clarification ? `\n\nClarification needed: ${planned.clarification}` : ''}${effects ? `\n\nConfirmed effects: ${JSON.stringify(effects)}` : ''}`;
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
  });
}
main().catch(() => { console.error('GitHub Codex persona did not complete with verified authority and output.'); process.exitCode = 1; });
