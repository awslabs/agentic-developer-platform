import test from "node:test";
import assert from "node:assert/strict";
import { requirementId, verifyRepositoryCompletion, type CompletionRequirements } from "./completion.js";

function fixture(policy: CompletionRequirements["policy"] = "validated-change") {
  const head = "a".repeat(40), source = "b".repeat(40), spec = "c".repeat(64), environment = "d".repeat(64);
  const requirements: CompletionRequirements = { runId: "run", policy, repository: { provider: "github", repositoryId: "org/repo", sourceRevision: source },
    criteria: ["Handle cancellation", "Preserve idempotency"], checks: [{ check: "integration", specificationDigest: spec, environmentDigest: environment }], assignedChange: 42 };
  const evidence = { runId: "run", provider: "github", repositoryId: "org/repo", sourceRevision: source, head, clean: true,
    publication: { number: 42, head, state: policy === "validated-change" ? "open" : "merged", draft: false, receipt: "publication-receipt" },
    requirements: requirements.criteria.map((criterion, index) => ({ id: requirementId(index, criterion), commit: head, status: "met", receipts: ["requirement-receipt"] })),
    validations: [{ check: "integration", commit: head, specificationDigest: spec, environmentDigest: environment, status: "passed", receipt: "validation-receipt" }],
    ...(policy === "review-repair-merge" ? { review: { commit: head, verdict: "approved", unresolvedFindings: 0, receipt: "review-receipt" },
      merge: { reviewedHead: head, commit: "e".repeat(40), protectionsSatisfied: true, receipt: "merge-receipt" } } : {}),
  };
  return { requirements, evidence };
}
const signal = new AbortController().signal;
for (const policy of ["validated-change", "review-repair-merge"] as const) {
  test(`${policy}: completion is derived from host receipts for the final commit`, async () => {
    const { requirements, evidence } = fixture(policy);
    let checks = 0;
    const result = await verifyRepositoryCompletion(requirements, { assertCurrent: async () => { checks++; }, readEvidence: async () => evidence }, signal);
    assert.equal(result.change, 42); assert.equal(result.head, evidence.head); assert.equal(checks, 2);
    assert.ok(result.receipts.includes("validation-receipt"));
  });
}
for (const mutation of ["dirty", "moved", "draft", "foreign", "wrong-pr", "missing-requirement", "unmet", "stale-requirement", "stale-test", "failed-test", "wrong-environment", "wrong-spec", "duplicate-test"] as const) {
  test(`developer refuses ${mutation} completion evidence`, async () => {
    const { requirements, evidence } = fixture();
    switch (mutation) {
      case "dirty": evidence.clean = false; break;
      case "moved": evidence.head = "f".repeat(40); break;
      case "draft": evidence.publication.draft = true; break;
      case "foreign": evidence.repositoryId = "org/other"; break;
      case "wrong-pr": evidence.publication.number = 43; break;
      case "missing-requirement": evidence.requirements.pop(); break;
      case "unmet": evidence.requirements[0]!.status = "unmet"; break;
      case "stale-requirement": evidence.requirements[0]!.commit = "f".repeat(40); break;
      case "stale-test": evidence.validations[0]!.commit = "f".repeat(40); break;
      case "failed-test": evidence.validations[0]!.status = "failed"; break;
      case "wrong-environment": evidence.validations[0]!.environmentDigest = "f".repeat(64); break;
      case "wrong-spec": evidence.validations[0]!.specificationDigest = "f".repeat(64); break;
      case "duplicate-test": evidence.validations.push(evidence.validations[0]!); break;
    }
    await assert.rejects(verifyRepositoryCompletion(requirements, { assertCurrent: async () => {}, readEvidence: async () => evidence }, signal));
  });
}
for (const mutation of ["unmerged", "unreviewed", "unresolved", "stale-review", "stale-merge", "protections"] as const) {
  test(`reviewer refuses ${mutation} completion`, async () => {
    const { requirements, evidence } = fixture("review-repair-merge");
    switch (mutation) {
      case "unmerged": evidence.publication.state = "open"; break;
      case "unreviewed": evidence.review!.verdict = "changes_requested"; break;
      case "unresolved": evidence.review!.unresolvedFindings = 1; break;
      case "stale-review": evidence.review!.commit = "f".repeat(40); break;
      case "stale-merge": evidence.merge!.reviewedHead = "f".repeat(40); break;
      case "protections": evidence.merge!.protectionsSatisfied = false; break;
    }
    await assert.rejects(verifyRepositoryCompletion(requirements, { assertCurrent: async () => {}, readEvidence: async () => evidence }, signal));
  });
}
test("completion refuses authority revoked while reading evidence", async () => {
  const { requirements, evidence } = fixture();
  let checks = 0;
  await assert.rejects(verifyRepositoryCompletion(requirements, { assertCurrent: async () => { if (++checks === 2) throw new Error("revoked"); },
    readEvidence: async () => evidence }, signal), /revoked/);
});

test("executable persona cannot start inference without a host completion validator", async () => {
  const { readFile } = await import("node:fs/promises");
  const { z } = await import("zod");
  const { snapshotPersona } = await import("./persona.js");
  const { runAdmittedSession } = await import("./session.js");
  const { HARNESS_CONTRACT_REVISION } = await import("./admission.js");
  const snapshot = snapshotPersona(await readFile(new URL("../personas/developer.json", import.meta.url), "utf8"), new Map());
  const capabilities = ["repository.read", "repository.write", "tests.run", "branch.push", "change.create", "artifacts.publish"] as const;
  const layers = { tenant: capabilities, principal: capabilities, run: capabilities, surface: capabilities, runtime: capabilities };
  let calls = 0;
  await assert.rejects(runAdmittedSession({ runId: "run", snapshot, policy: {
    personaKey: "gpt-developer", personaDigest: snapshot.digest, compatibilityClass: "codex-sdk", harnessRevision: HARNESS_CONTRACT_REVISION,
    canonicalModel: "gpt-5-codex", allowedEfforts: ["medium"], capabilityLayers: layers,
    limits: { maxTurns: 4, maxContextBytes: 65536, maxDurationMs: 1000 }, deadlineMs: Date.now() + 10000,
  }, source: { kind: "task-api", taskId: "task", generation: 1 }, repository: { provider: "github", repositoryId: "org/repo", sourceRevision: "a".repeat(40) },
    prompt: "Implement", maxOutputTokens: 64, maxResponseBytes: 1024, signal,
  }, { assertCurrent: async () => {}, progress: async () => {}, model: async () => { calls++; throw new Error("inference should not run"); },
    toolBroker: { definitions: capabilities.map((capability, index) => ({ name: `tool_${index}`, capability, input: z.object({}), readOnly: false, description: "fixture" })),
      maxCalls: 4, repositoryCapabilities: capabilities, execute: async () => { throw new Error("effect should not run"); } },
  }), /executable capability broker/);
  assert.equal(calls, 0);
});


test("host planning effects are admitted without direct model tools and remain unavailable to Task", async () => {
  const { readFile } = await import("node:fs/promises");
  const { snapshotPersona } = await import("./persona.js");
  const { runAdmittedSession } = await import("./session.js");
  const { HARNESS_CONTRACT_REVISION } = await import("./admission.js");
  for (const [persona, capability] of [["architect", "story.create"], ["pm", "agents.delegate"]] as const) {
    const snapshot = snapshotPersona(await readFile(new URL(`../personas/${persona}.json`, import.meta.url), "utf8"), new Map());
    const definition = JSON.parse(snapshot.definition);
    const caps = ["artifacts.publish", capability] as const;
    const input = {runId: "run", snapshot, policy: {personaKey: definition.key, personaDigest: snapshot.digest,
      compatibilityClass: "codex-sdk" as const, harnessRevision: HARNESS_CONTRACT_REVISION, canonicalModel: "fixture", allowedEfforts: [definition.effort],
      capabilityLayers: {tenant: caps, principal: caps, run: caps, surface: caps, runtime: caps},
      limits: {maxTurns: 4, maxContextBytes: 65536, maxDurationMs: 1000}, deadlineMs: Date.now()+10000},
      source: {kind: "github" as const, eventId: "event"}, repository: {provider: "github" as const, repositoryId: "1", sourceRevision: "a".repeat(40)},
      prompt: "Plan", maxOutputTokens: 64, maxResponseBytes: 1024, signal};
    const host = {planningCapabilities: [capability], assertCurrent: async () => {throw new Error("reached authority check");},
      progress: async () => {}, model: async (): Promise<never> => {throw new Error("unexpected inference");}};
    await assert.rejects(runAdmittedSession(input, host), /reached authority check/);
    await assert.rejects(runAdmittedSession({...input, source: {kind: "task-api", taskId: "task", generation: 1}}, host), /Invalid host planning capabilities/);
  }
});
