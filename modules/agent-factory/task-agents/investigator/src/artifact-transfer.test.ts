import assert from 'node:assert/strict';
import { createHash, randomUUID } from 'node:crypto';
import { test } from 'node:test';
import { ArtifactTransfers } from './artifact-transfer.js';
import { loadFixture } from './fixtures.js';
import { ProtocolViolation, parseHostFrame, type ArtifactChunkFrame, type WireStartFrame } from './protocol.js';

function fixture(bytes: Buffer) {
  const start = loadFixture('valid', 'process-start-frame.json').body as unknown as WireStartFrame;
  const artifactId = `art_${randomUUID()}`;
  const digest = createHash('sha256').update(bytes).digest('hex');
  start.artifacts = [{ artifact_id: artifactId, content_type: 'text/plain', content_sha256: digest, byte_length: bytes.length }];
  const chunks: ArtifactChunkFrame[] = [];
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    chunks.push({ protocol_version: 1, type: 'artifact.chunk', request_id: randomUUID(), task_id: start.task_id,
      artifact_id: artifactId, content_type: 'text/plain', content_sha256: digest, sequence: chunks.length + 1,
      total_bytes: bytes.length, data_base64: bytes.subarray(offset, offset + 32768).toString('base64'), last: offset + 32768 >= bytes.length });
  }
  return { start, chunks };
}
function admit(receiver: ArtifactTransfers, frame: ArtifactChunkFrame): void {
  receiver.accept(parseHostFrame(JSON.stringify(frame)) as ArtifactChunkFrame);
}

for (const length of [32767, 32768, 32769, 262144]) {
  test(`reassembles ${length} verified bytes across fixed chunk boundary`, () => {
    const bytes = Buffer.from('x'.repeat(length));
    const { start, chunks } = fixture(bytes);
    const receiver = new ArtifactTransfers();
    for (const chunk of chunks) admit(receiver, chunk);
    assert.equal(receiver.start(start).artifacts?.[0]?.content, bytes.toString());
    assert.ok(chunks.every(chunk => Buffer.byteLength(JSON.stringify(chunk)) + 1 <= 65536));
  });
}

test('UTF-8 scalar split across chunks is decoded only after full digest verification', () => {
  const bytes = Buffer.from('x'.repeat(32767) + '€' + 'tail');
  const { start, chunks } = fixture(bytes);
  const receiver = new ArtifactTransfers();
  for (const chunk of chunks) admit(receiver, chunk);
  assert.equal(receiver.start(start).artifacts?.[0]?.content, bytes.toString());
});

test('rejects invalid UTF-8 even with a matching digest', () => {
  const { chunks } = fixture(Buffer.from([0xff]));
  assert.throws(() => admit(new ArtifactTransfers(), chunks[0]!), /valid UTF-8/);
});

test('rejects duplicate, reordered, changed-metadata and incomplete transfers', () => {
  const { start, chunks } = fixture(Buffer.from('x'.repeat(32769)));
  assert.throws(() => admit(new ArtifactTransfers(), chunks[1]!), /out of order/);
  const duplicate = new ArtifactTransfers();
  admit(duplicate, chunks[0]!);
  assert.throws(() => admit(duplicate, chunks[0]!), /duplicate or reordered/);
  assert.throws(() => duplicate.start(start), /incomplete/);
  assert.throws(() => admit(duplicate, { ...chunks[1]!, content_sha256: '0'.repeat(64) }), /metadata changed/);
});

test('rejects digest mismatch, wrong final marker and noncanonical base64', () => {
  const { chunks } = fixture(Buffer.from('f'));
  assert.throws(() => admit(new ArtifactTransfers(), { ...chunks[0]!, content_sha256: '0'.repeat(64) }), /digest mismatch/);
  assert.throws(() => admit(new ArtifactTransfers(), { ...chunks[0]!, last: false }), /last marker/);
  assert.throws(() => admit(new ArtifactTransfers(), { ...chunks[0]!, data_base64: 'Zh==' }), /canonical base64/);
});

test('rejects oversized declarations before allocating and fifth artifact before receiving it', () => {
  const { chunks } = fixture(Buffer.from('x'));
  assert.throws(() => admit(new ArtifactTransfers(), { ...chunks[0]!, total_bytes: 262145 }), /permitted range/);
  const receiver = new ArtifactTransfers();
  for (let index = 0; index < 4; index += 1) {
    const part = fixture(Buffer.from('x'.repeat(262144)));
    for (const chunk of part.chunks) admit(receiver, chunk);
  }
  assert.throws(() => admit(receiver, chunks[0]!), /fixed input limits/);
});

test('start refuses missing, unused and mismatched artifact references', () => {
  const { start, chunks } = fixture(Buffer.from('hello'));
  assert.throws(() => new ArtifactTransfers().start(start), /complete verified transfer/);
  const receiver = new ArtifactTransfers();
  admit(receiver, chunks[0]!);
  assert.throws(() => receiver.start({ ...start, artifacts: [] }), /unreferenced/);
  assert.throws(() => receiver.start({ ...start, artifacts: [{ ...start.artifacts![0]!, content_type: 'application/json' }] }), /complete verified transfer/);
});

test('instructions and followups that cannot fit fail with a typed protocol error', async () => {
  const { boundedModelMessages } = await import('./investigator.js');
  const start = loadFixture('valid', 'process-start-frame.json').body as unknown as import('./protocol.js').StartFrame;
  assert.throws(() => boundedModelMessages(start, new Map(), [{ role: 'user', content: 'x'.repeat(65536) }], 1024),
    (error: unknown) => error instanceof ProtocolViolation && /instructions and follow-up/.test(error.message));
});
