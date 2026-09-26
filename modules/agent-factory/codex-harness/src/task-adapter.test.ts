import test from "node:test";
import assert from "node:assert/strict";
import { snapshotPersona } from "./persona.js";
import { HARNESS_CONTRACT_REVISION } from "./admission.js";
import { taskHarness, parseTaskReport, taskToolName } from "./task-adapter.js";

function start() {
  const limits = { maxTurns: 2, maxContextBytes: 60000, maxDurationMs: 15000 };
  const snapshot = snapshotPersona(JSON.stringify({ schemaVersion: 1, key: "gpt-fixture", revision: "1", displayName: "Fixture",
    instructions: "Report evidence.", skills: [], requiredCapabilities: ["artifacts.publish"], optionalCapabilities: [],
    surfaces: ["task-api"], completionPolicy: "report", effort: "medium", limits }), new Map());
  const deadlineMs = Date.now() + 60000;
  return { persona: "agent-task-gpt-fixture", deadline_at: new Date(deadlineMs).toISOString(),
    model_binding: { model_id: "gpt-5-codex", transport: "openai_responses", invocability_verified: true },
    limits: { max_turns: 2, max_output_tokens_per_turn: 4096 },
    harness: { snapshot, policy: { personaKey: "gpt-fixture", personaDigest: snapshot.digest, compatibilityClass: "codex-sdk",
      harnessRevision: HARNESS_CONTRACT_REVISION, canonicalModel: "gpt-5-codex", allowedEfforts: ["medium"],
      capabilityLayers: Object.fromEntries(["tenant", "principal", "run", "surface", "runtime"].map(key => [key, ["artifacts.publish"]])), limits, deadlineMs } } };
}

test("Task bootstrap is pinned to host persona, model, deadline and operation limits", () => {
  assert.equal(taskHarness(start()).policy.personaKey, "gpt-fixture");
  for (const mutate of [
    (v: ReturnType<typeof start>) => { v.persona = "agent-task-other"; },
    (v: ReturnType<typeof start>) => { v.model_binding.model_id = "other"; },
    (v: ReturnType<typeof start>) => { v.model_binding.invocability_verified = false; },
    (v: ReturnType<typeof start>) => { v.harness.policy.deadlineMs += 1; },
    (v: ReturnType<typeof start>) => { v.limits.max_turns = 1; },
    (v: ReturnType<typeof start>) => { v.harness.snapshot = { ...v.harness.snapshot, instructions: "tampered" }; },
  ]) {
    const value = start(); mutate(value); assert.throws(() => taskHarness(value));
  }
});

test("Report citations must match host evidence, declared references and shared schema", () => {
  const citation = { ref: "instructions", source: "instructions" };
  const evidence = new Map([[citation.ref, citation]]);
  const report = { findings: [{ evidence_refs: [citation.ref] }], evidence_refs: [citation] };
  assert.deepEqual(parseTaskReport(JSON.stringify(report), evidence, () => {}), report);
  assert.throws(() => parseTaskReport(JSON.stringify(report), evidence, () => { throw new Error("schema"); }), /schema/);
  assert.throws(() => parseTaskReport(JSON.stringify({ ...report, evidence_refs: [] }), evidence, () => {}), /undeclared/);
  assert.throws(() => parseTaskReport(JSON.stringify({ ...report, evidence_refs: [{ ...citation, source: "artifact" }] }), evidence, () => {}), /unsupported/);
  assert.throws(() => parseTaskReport(JSON.stringify({ ...report, evidence_refs: [citation, citation] }), evidence, () => {}), /unsupported/);
});

test("Shared gateway bootstrap fixture is accepted by the SDK snapshot and Task adapter", async () => {
  const { readFile } = await import("node:fs/promises");
  const raw = await readFile(new URL("../../../../docs/task-api/contracts/v1/fixtures/valid/bootstrap-codex-response.json", import.meta.url), "utf8");
  const value = JSON.parse(raw);
  const { snapshot } = taskHarness(value);
  assert.deepEqual(snapshot, value.harness.snapshot);
});


test("Task tool names match the gateway v1 vector without delimiter collisions", async () => {
  const { taskToolName } = await import("./task-adapter.js");
  assert.equal(taskToolName("repository.read_change"), "adp_75ac58ef0e809a16d800561c0d85bc1bbbd842ae3416488c2b846848");
  assert.notEqual(taskToolName("a_b.c"), taskToolName("a.b_c"));
  assert.ok(taskToolName("a".repeat(48) + "." + "b".repeat(64)).length <= 64);
  assert.throws(() => taskToolName("repository.*"));
  assert.throws(() => taskToolName("https://foreign.invalid"));
});


test("repository capabilities require the provisioned host binding and admitted tool", () => {
  const base = start();
  for (const layer of Object.values(base.harness.policy.capabilityLayers)) layer.push("repository.read");
  const value = { ...base, harness: { ...base.harness, tools: [{ permission: "repository.read", capability: "repository.read",
    definition: { type: "function", name: taskToolName("repository.read"), description: "Read source", strict: false, parameters: { type: "object" } } }] },
    repository: { binding: { provider: "github", repositoryId: "456", sourceRevision: "b".repeat(40) }, capabilities: ["repository.read"] } };
  assert.equal(taskHarness(value).repository?.binding.repositoryId, "456");
  const taskTextOnly = { ...value, repository: undefined, inputs: { repository: value.repository } };
  assert.equal(taskHarness(taskTextOnly).repository, undefined);
  assert.throws(() => taskHarness({ ...value, repository: { ...value.repository, capabilities: ["repository.write"] } }));
  assert.throws(() => taskHarness({ ...value, repository: { ...value.repository, binding: { ...value.repository.binding, sourceRevision: "main" } } }));
});

test("report failure diagnostics identify the boundary without echoing model content", () => {
  const evidence = new Map();
  assert.throws(() => parseTaskReport("private invalid content", evidence, () => {}), error => {
    assert.equal((error as {code: string}).code, "invalid_json");
    assert.doesNotMatch(String(error), /private/); return true;
  });
  assert.throws(() => parseTaskReport("{}", evidence, () => { throw new Error("private invalid schema field"); }), error => {
    assert.equal((error as {code: string}).code, "invalid_schema");
    assert.doesNotMatch(String(error), /private/); return true;
  });
});

test("developer findings require execution artifacts instead of requirements as proof", () => {
  const instruction = {ref: "instructions", source: "instructions"};
  const artifact = {ref: "art-test", source: "artifact", artifact_id: "art-test"};
  const evidence = new Map([[instruction.ref, instruction], [artifact.ref, artifact]]);
  const report = {findings: [{evidence_refs: [instruction.ref]}], evidence_refs: [instruction, artifact]};
  assert.throws(() => parseTaskReport(JSON.stringify(report), evidence, () => {}, true), /execution evidence required/);
  report.findings[0]!.evidence_refs.push(artifact.ref);
  assert.deepEqual(parseTaskReport(JSON.stringify(report), evidence, () => {}, true), report);
});
