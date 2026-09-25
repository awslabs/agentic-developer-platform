import test from "node:test";
import assert from "node:assert/strict";
import type { ThreadEvent, Thread } from "@openai/codex-sdk";
import { trace } from "@opentelemetry/api";
import { BasicTracerProvider, SimpleSpanProcessor, InMemorySpanExporter } from "@opentelemetry/sdk-trace-base";
import { snapshotPersona, sha256, type Persona, type Capability } from "./persona.js";
import { planVerifiedRun, HARNESS_CONTRACT_REVISION, type VerifiedRunPolicy } from "./admission.js";
import { runSdkTurn, type Progress, type TurnContext } from "./turn.js";

const definition: Persona = {
  schemaVersion: 1, key: "gpt-evidence-analyst", revision: "1", displayName: "Evidence analyst",
  instructions: "Use supplied evidence. Report uncertainties.", skills: [],
  requiredCapabilities: ["artifacts.publish"], optionalCapabilities: ["repository.read"],
  surfaces: ["task-api", "github", "gitlab", "delegation"], completionPolicy: "report", effort: "medium",
  limits: { maxTurns: 4, maxContextBytes: 8192, maxDurationMs: 60000 },
};
function policy(digest: string): VerifiedRunPolicy {
  const all: Capability[] = ["artifacts.publish", "repository.read"];
  return {
    personaKey: definition.key, personaDigest: digest, compatibilityClass: "codex-sdk",
    harnessRevision: HARNESS_CONTRACT_REVISION, canonicalModel: "registered-model-id",
    allowedEfforts: ["medium"], limits: { maxTurns: 2, maxContextBytes: 4096, maxDurationMs: 10000 },
    deadlineMs: 9000,
    capabilityLayers: { tenant: all, principal: all, run: all, surface: all, runtime: all },
  };
}
const source = { kind: "task-api", taskId: "task-1", generation: 1 } as const;

test("API-only dynamic persona needs no GitHub identity or repository", () => {
  const snapshot = snapshotPersona(JSON.stringify(definition), new Map());
  const planned = planVerifiedRun(snapshot, policy(snapshot.digest), source, undefined, [], 1000);
  assert.equal(planned.options.skipGitRepoCheck, true);
  assert.equal(planned.options.model, "registered-model-id");
  assert.deepEqual(planned.capabilities, ["artifacts.publish"]);
  assert.deepEqual(planned.unavailableOptionalCapabilities, ["repository.read"]);
  assert.equal(planned.limits.maxDurationMs, 8000);
  assert.equal(planned.limits.maxTurns, 2);
  assert.equal(planned.options.sandboxMode, "read-only");
});

test("definition is immutable and rejects arbitrary executable/configuration extensions", () => {
  const old = snapshotPersona(JSON.stringify(definition), new Map());
  const next = snapshotPersona(JSON.stringify({ ...definition, instructions: "New policy" }), new Map());
  assert.notEqual(old.digest, next.digest);
  assert.match(old.instructions, /supplied evidence/);
  assert.equal(Object.isFrozen(old), true);
  assert.throws(() => snapshotPersona(JSON.stringify({ ...definition, command: "sh -c unsafe" }), new Map()));
  assert.throws(() => snapshotPersona(JSON.stringify({ ...definition, requiredCapabilities: ["host.root"] }), new Map()));
  assert.throws(() => snapshotPersona(JSON.stringify({ ...definition, completionPolicy: "review-repair-merge" }), new Map()));
});

test("skill content is digest-bound and budgeted", () => {
  const skill = "Cite evidence for each finding.";
  const raw = JSON.stringify({ ...definition, skills: [{ id: "citations", sha256: sha256(skill) }] });
  assert.throws(() => snapshotPersona(raw, new Map()), /Missing or mismatched/);
  assert.throws(() => snapshotPersona(raw, new Map([["citations", "changed"]])), /Missing or mismatched/);
  assert.match(snapshotPersona(raw, new Map([["citations", skill]])).instructions, /Cite evidence/);
});

test("every capability layer is enforced and missing repository does not fabricate one", () => {
  const snapshot = snapshotPersona(JSON.stringify({ ...definition, requiredCapabilities: ["repository.read"], optionalCapabilities: [] }), new Map());
  const grant = policy(snapshot.digest);
  assert.throws(() => planVerifiedRun(snapshot, grant, source, undefined, [], 1000), /capabilities unavailable/);
  const repository = { provider: "gitlab", repositoryId: "project-1", sourceRevision: "commit-1" } as const;
  for (const layer of Object.keys(grant.capabilityLayers) as (keyof VerifiedRunPolicy["capabilityLayers"])[]) {
    const restricted = { ...grant, capabilityLayers: { ...grant.capabilityLayers, [layer]: [] } };
    assert.throws(() => planVerifiedRun(snapshot, restricted, source, repository, ["repository.read"], 1000));
  }
  assert.throws(() => planVerifiedRun(snapshot, grant, source, repository, [], 1000));
  assert.equal(planVerifiedRun(snapshot, grant, source, repository, ["repository.read"], 1000).options.skipGitRepoCheck, false);
});

test("stale snapshot, effort, expiry and generation fail preflight", () => {
  const snapshot = snapshotPersona(JSON.stringify(definition), new Map());
  const grant = policy(snapshot.digest);
  assert.throws(() => planVerifiedRun(snapshot, { ...grant, personaDigest: "stale" }, source, undefined, [], 1000));
  assert.throws(() => planVerifiedRun(snapshot, { ...grant, harnessRevision: "unknown-revision" }, source, undefined, [], 1000));
  assert.throws(() => planVerifiedRun(snapshot, { ...grant, allowedEfforts: ["high"] }, source, undefined, [], 1000));
  assert.throws(() => planVerifiedRun(snapshot, grant, source, undefined, [], 9000));
  assert.throws(() => planVerifiedRun(snapshot, grant, { ...source, generation: 0 }, undefined, [], 1000));
});

const usage = { input_tokens: 50, cached_input_tokens: 10, cache_write_input_tokens: 0, output_tokens: 8, reasoning_output_tokens: 3 };
const context = (): TurnContext => ({
  runId: "run-1", personaKey: definition.key, model: "registered-model-id", harnessRevision: "test-1",
  surface: "task-api", timeoutMs: 1000, maxInputBytes: 1024, maxOutputBytes: 1024, signal: new AbortController().signal,
});
function thread(events: ThreadEvent[]): Pick<Thread, "id" | "runStreamed"> {
  return { id: "thread-1", async runStreamed() { return { events: (async function* () { yield* events; })() }; } };
}

test("SDK completion preserves usage and exports only safe trace/progress attributes", async () => {
  const exporter = new InMemorySpanExporter();
  const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });
  trace.setGlobalTracerProvider(provider);
  const events: ThreadEvent[] = [
    { type: "turn.started" },
    { type: "item.started", item: { id: "x", type: "command_execution", command: "secret-inline-token", aggregated_output: "secret-output", exit_code: undefined, status: "in_progress" } },
    { type: "item.completed", item: { id: "answer", type: "agent_message", text: "sensitive final response" } },
    { type: "turn.completed", usage },
  ];
  const progress: Progress[] = [];
  const result = await runSdkTurn(thread(events), "secret prompt", context(), async event => { progress.push(event); });
  assert.equal(result.response, "sensitive final response");
  assert.deepEqual(result.usage, usage);
  assert.deepEqual(progress, [{ type: "turn.started" }, { type: "tool.started", tool: "command_execution" }]);
  await provider.forceFlush();
  const spans = exporter.getFinishedSpans();
  assert.equal(spans.length, 1);
  assert.equal(spans[0]?.attributes["adp.run.id"], "run-1");
  const exported = JSON.stringify(spans.map(s => ({ attributes: s.attributes, events: s.events, status: s.status })));
  assert.doesNotMatch(exported, /secret|sensitive final/);
  trace.disable();
  await provider.shutdown();
});

test("missing/failed terminal events and impossible usage never produce completion", async () => {
  for (const events of [
    [],
    [{ type: "error", message: "sensitive provider details" }],
    [{ type: "turn.completed", usage: { ...usage, cached_input_tokens: 100 } }],
    [{ type: "turn.completed", usage }, { type: "turn.started" }],
  ] as ThreadEvent[][]) {
    await assert.rejects(runSdkTurn(thread(events), "prompt", context(), async () => {}));
  }
});

test("aborted or oversized input never starts the SDK; output overflow aborts active work", async () => {
  let calls = 0;
  const inactive = { id: null, async runStreamed() { calls++; throw new Error("must not start"); } };
  await assert.rejects(runSdkTurn(inactive, "x", { ...context(), signal: AbortSignal.abort() }, async () => {}));
  await assert.rejects(runSdkTurn(inactive, "x".repeat(1025), context(), async () => {}));
  assert.equal(calls, 0);
  let signal: AbortSignal | undefined;
  const running: Pick<Thread, "id" | "runStreamed"> = { id: "thread-1", async runStreamed(_, options) {
    signal = options?.signal;
    return { events: (async function* (): AsyncGenerator<ThreadEvent> {
      yield { type: "item.completed", item: { id: "answer", type: "agent_message", text: "x".repeat(1025) } };
    })() };
  } };
  await assert.rejects(runSdkTurn(running, "prompt", context(), async () => {}), /output budget/);
  assert.equal(signal?.aborted, true);
});

test("all shipped persona candidates load through the same declarative parser", async () => {
  const { readdir, readFile } = await import("node:fs/promises");
  const folder = new URL("../personas/", import.meta.url);
  const files = (await readdir(folder)).filter(file => file.endsWith(".json"));
  assert.equal(files.length, 8);
  const names = new Set<string>();
  for (const file of files) {
    const snapshot = snapshotPersona(await readFile(new URL(file, folder), "utf8"), new Map());
    const parsed = JSON.parse(snapshot.definition) as Persona;
    assert.equal(names.has(parsed.key), false);
    names.add(parsed.key);
  }
});
