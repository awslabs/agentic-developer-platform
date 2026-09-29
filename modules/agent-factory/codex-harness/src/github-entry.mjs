#!/usr/bin/env node
/** GitHub invocation adapter for the shared, isolated official Codex SDK. */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID, createHash } from 'node:crypto';
import { z } from 'zod';
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
  capabilities: z.array(z.enum(['artifacts.publish', 'repository.read'])).min(1).max(2),
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
    const initial = await admitCodexPersonaModel(persona, AbortSignal.timeout(10000));
    const context = contextSchema.parse(initial.context);
    const snapshot = verifySnapshot(context.snapshot);
    const definition = personaSchema.parse(JSON.parse(snapshot.definition));
    if (context.persona !== persona || definition.key !== `gpt-${persona.replace(/^agent-codex-/, '')}` ||
        definition.completionPolicy !== 'report' || context.repository !== process.env.TARGET_REPO || context.issue !== Number(process.env.ISSUE_NUMBER)) {
      throw new Error('GitHub persona binding mismatch');
    }
    const remaining = context.deadlineMs - Date.now();
    if (remaining <= 0) throw new Error('GitHub persona expired');
    const deadline = AbortSignal.timeout(Math.min(remaining, 2700000));
    const reporter = await createCodexPersonaReporter({ ...context, model: initial.model });
    let controls;
    try {
      controls = await startCodexPersonaControls(reporter.log, deadline);
      const signal = AbortSignal.any([deadline, controls.signal]);
      const issue = await execute('gh', ['issue', 'view', String(context.issue), '--repo', context.repository,
        '--json', 'number,title,body,comments,url,state'], { signal, timeout: 15000, maxBuffer: 32768 });
      const input = JSON.parse(issue.stdout);
      const repositoryIdentity = JSON.parse(await hostCommand('gh', ['api', `repos/${context.repository}`], signal));
      if (String(repositoryIdentity.id) !== context.repositoryId) throw new Error('Repository identity changed');
      const revision = (await hostCommand('git', ['rev-parse', 'HEAD'], signal)).trim();
      if (!/^[a-f0-9]{40}$/.test(revision)) throw new Error('Repository revision unavailable');
      const layers = Object.fromEntries(['tenant', 'principal', 'run', 'surface', 'runtime'].map(key => [key, context.capabilities]));
      let operations = 0;
      const journal = async (kind, request, execute, active) => {
        const binding = { operation_id: randomUUID(), request_digest: createHash('sha256').update(JSON.stringify(request)).digest('hex'), kind };
        const admission = await codexPersonaOperation({ ...binding, action: 'claim' }, active);
        if (admission.status !== 'admitted') throw new Error('Operation requires reconciliation');
        const result = await execute();
        const serialized = JSON.stringify(result);
        const receipt = await codexPersonaOperation({ ...binding, action: 'settle', result: serialized }, active);
        if (receipt.status !== 'confirmed' || receipt.result !== serialized) throw new Error('Operation settlement was not confirmed');
        return result;
      };
      const current = async (active, checkpoint = true) => {
        active.throwIfAborted();
        if (checkpoint) await controls.checkpoint();
        const fresh = await admitCodexPersonaModel(persona, active);
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
      const result = await runAdmittedSession({ runId: initial.runId, snapshot,
        policy: { personaKey: definition.key, personaDigest: snapshot.digest, compatibilityClass: 'codex-sdk',
          harnessRevision: context.harnessRevision, canonicalModel: initial.model, allowedEfforts: [definition.effort],
          capabilityLayers: layers, limits: { ...definition.limits, maxTurns: context.maxTurns }, deadlineMs: context.deadlineMs },
        repository: { provider: 'github', repositoryId: context.repositoryId, sourceRevision: revision },
        source: { kind: 'github', eventId: initial.runId }, prompt: JSON.stringify({ task: input,
          output: 'Produce a useful Markdown report for the issue. Cite supplied evidence and distinguish proposals from verified results. Read repository evidence with the admitted tools when available. Mutations are unavailable in this report invocation.' }),
        maxOutputTokens: context.maxOutputTokens, maxResponseBytes: 48000, signal,
      }, {
        assertCurrent: current,
        ...(tools.definitions.length ? { toolBroker: { definitions: tools.definitions,
          repositoryCapabilities: ['repository.read'], maxCalls: context.maxTools, execute: tools.execute } } : {}),
        takeSteering: () => controls.takeSteering(),
        async progress() {
          const text = 'Working through the admitted task and its evidence.';
          reporter.progress(text);
          controls.explain(text);
        },
        async model(request, active) {
          return controls.operation(async () => {
            if (++operations > context.maxTurns) throw new Error('GitHub model budget exhausted');
            const fresh = await current(active, false);
            const port = Number(process.env.SIGV4_PROXY_PORT ?? '9090');
            if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid model proxy');
            return journal('model', { request, model: initial.model }, async () => {
            const response = await fetch(`http://127.0.0.1:${port}/openai/v1/responses`, {
              method: 'POST', redirect: 'error', signal: active,
              headers: { 'content-type': 'application/json', 'X-Adp-Model-Evidence': fresh.evidenceId },
              body: JSON.stringify({ ...request, model: initial.model, stream: false, store: false }),
            });
            if (!response.ok || !response.body) throw new Error('GitHub model request failed; no automatic replay');
            const reader = response.body.getReader();
            const chunks = []; let bytes = 0;
            try {
              for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                if ((bytes += value.length) > 48000) throw new Error('Model response exceeds bound');
                chunks.push(value);
              }
            } finally { await reader.cancel(); }
            return { operationStatus: 'confirmed', response: JSON.parse(Buffer.concat(chunks).toString('utf8')) };
            }, active);
          });
        },
      });
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
