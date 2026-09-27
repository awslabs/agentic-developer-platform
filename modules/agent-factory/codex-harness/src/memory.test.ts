import assert from "node:assert/strict";
import test from "node:test";
import { DisabledMemoryProvider, RunMemory, type MemoryProvider, type MemoryRecord } from "./memory.js";

const scope = { tenantId: "tenant-a", namespaceId: "private-user-1-repo-2", personaKey: "gpt-developer" };
const query = { text: "test setup", maxRecords: 5, maxBytes: 4096 };
const record: MemoryRecord = { id: "lesson-1", scope, summary: "Use the repository's isolated test runner.",
  sourceRunId: "run-1", evidenceIds: ["test-receipt-1"], confidence: "verified", expiresAt: 2000 };
const signal = () => new AbortController().signal;
function backend(records: unknown): MemoryProvider {
  return { id: "fixture", revision: "1", async retrieve() { return records; },
    async save(_scope, value, operationKey) { return { operationKey, outcome: "stored", recordId: value.id }; } };
}

test("memory is explicitly disabled without introducing a storage dependency", async () => {
  const memory = new RunMemory(new DisabledMemoryProvider(), scope, () => 1000);
  assert.deepEqual(await memory.retrieve(query, signal()), []);
  assert.deepEqual(await memory.save(record, "run-1:learning-1", signal()), { operationKey: "run-1:learning-1", outcome: "disabled" });
});

test("a selected backend receives an immutable host scope; caller mutations cannot change it", async () => {
  const selectedScope = { ...scope };
  const provider = backend([record]);
  provider.retrieve = async (bound) => {
    assert.deepEqual(bound, scope);
    assert.equal(Object.isFrozen(bound), true);
    return [record];
  };
  const memory = new RunMemory(provider, selectedScope, () => 1000);
  selectedScope.tenantId = "tenant-b";
  assert.equal((await memory.retrieve(query, signal()))[0]?.id, record.id);
});

for (const field of ["tenantId", "namespaceId", "personaKey"] as const) {
  test(`backend cannot return memory across ${field}`, async () => {
    const other = { ...record, scope: { ...scope, [field]: "other" } };
    const memory = new RunMemory(backend([other]), scope, () => 1000);
    await assert.rejects(memory.retrieve(query, signal()), /scope mismatch/);
    await assert.rejects(memory.save(other, "run:lesson", signal()), /scope/);
  });
}

test("expired memory and excess context are excluded, duplicate identities are rejected", async () => {
  const memory = new RunMemory(backend([{ ...record, id: "old", expiresAt: 999 }, record]), scope, () => 1000);
  assert.deepEqual(await memory.retrieve(query, signal()), [record]);
  assert.deepEqual(await memory.retrieve({ ...query, maxBytes: 1 }, signal()), []);
  await assert.rejects(new RunMemory(backend([record, record]), scope, () => 1000).retrieve(query, signal()), /Duplicate/);
});

test("cancelled retrieval is discarded and an interrupted write has unknown outcome", async () => {
  const abort = new AbortController();
  const provider = backend([]);
  provider.retrieve = async () => { abort.abort(); return [record]; };
  const memory = new RunMemory(provider, scope, () => 1000);
  await assert.rejects(memory.retrieve(query, abort.signal));
  const writeAbort = new AbortController();
  provider.save = async (_scope, value, operationKey) => {
    writeAbort.abort();
    return { operationKey, outcome: "stored", recordId: value.id };
  };
  assert.deepEqual(await memory.save(record, "run:lesson", writeAbort.signal), { operationKey: "run:lesson", outcome: "unknown" });
});

test("a receipt for another write cannot acknowledge learning persistence", async () => {
  const provider = backend([]);
  provider.save = async () => ({ operationKey: "another-run", outcome: "stored", recordId: record.id });
  await assert.rejects(new RunMemory(provider, scope, () => 1000).save(record, "run:lesson", signal()), /identity mismatch/);
});
