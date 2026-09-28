/** Bounded, credential-free artifact transfer before the task starts. */
import { createHash } from 'node:crypto';
import { ProtocolViolation, type ArtifactChunkFrame, type InputArtifact, type StartFrame, type WireStartFrame } from './protocol.js';

export const ARTIFACT_CHUNK_BYTES = 32768;
interface Transfer {
  first: ArtifactChunkFrame;
  bytes: Buffer;
  offset: number;
  nextSequence: number;
  content?: string;
}

export class ArtifactTransfers {
  private readonly transfers = new Map<string, Transfer>();
  private taskId: string | undefined;
  private reserved = 0;
  private activeId: string | undefined;
  private started = false;

  accept(frame: ArtifactChunkFrame): void {
    if (this.started) throw new ProtocolViolation('artifact chunks must precede start');
    if (this.taskId !== undefined && this.taskId !== frame.task_id) throw new ProtocolViolation('artifact task identity changed');
    this.taskId = frame.task_id;
    let transfer = this.transfers.get(frame.artifact_id);
    if (transfer === undefined) {
      if (frame.sequence !== 1 || this.activeId !== undefined) throw new ProtocolViolation('artifact transfer is out of order');
      if (this.transfers.size >= 4 || this.reserved + frame.total_bytes > 1048576) throw new ProtocolViolation('artifact reservation exceeds fixed input limits');
      // Reserve the declared total before allocating or accepting any content.
      this.reserved += frame.total_bytes;
      transfer = { first: frame, bytes: Buffer.alloc(frame.total_bytes), offset: 0, nextSequence: 1 };
      this.transfers.set(frame.artifact_id, transfer);
      this.activeId = frame.artifact_id;
    }
    if (frame.sequence !== transfer.nextSequence || transfer.content !== undefined) throw new ProtocolViolation('duplicate or reordered artifact chunk');
    if (frame.total_bytes !== transfer.first.total_bytes || frame.content_sha256 !== transfer.first.content_sha256 || frame.content_type !== transfer.first.content_type) {
      throw new ProtocolViolation('artifact chunk metadata changed');
    }
    const chunk = Buffer.from(frame.data_base64, 'base64');
    if (chunk.toString('base64') !== frame.data_base64) throw new ProtocolViolation('artifact chunk is not canonical base64');
    const remaining = transfer.bytes.length - transfer.offset;
    if (chunk.length !== Math.min(ARTIFACT_CHUNK_BYTES, remaining) || frame.last !== (chunk.length === remaining)) {
      throw new ProtocolViolation('artifact chunk size or last marker is inconsistent');
    }
    chunk.copy(transfer.bytes, transfer.offset);
    transfer.offset += chunk.length;
    transfer.nextSequence += 1;
    if (frame.last) {
      if (createHash('sha256').update(transfer.bytes).digest('hex') !== frame.content_sha256) throw new ProtocolViolation('artifact content digest mismatch');
      try {
        transfer.content = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(transfer.bytes);
      } catch {
        throw new ProtocolViolation('artifact content is not valid UTF-8');
      }
      this.activeId = undefined;
    }
  }

  start(frame: WireStartFrame): StartFrame {
    if (this.started || (this.taskId !== undefined && this.taskId !== frame.task_id)) throw new ProtocolViolation('artifact start identity mismatch');
    if (this.activeId !== undefined) throw new ProtocolViolation('artifact transfer is incomplete');
    const used = new Set<string>();
    const seen = new Set<string>();
    const artifacts: InputArtifact[] = [];
    for (const artifact of frame.artifacts ?? []) {
      if (seen.has(artifact.artifact_id)) throw new ProtocolViolation('duplicate start artifact');
      seen.add(artifact.artifact_id);
      if ('content' in artifact) {
        artifacts.push(artifact);
        continue;
      }
      const transfer = this.transfers.get(artifact.artifact_id);
      if (transfer?.content === undefined || transfer.first.content_type !== artifact.content_type || transfer.first.content_sha256 !== artifact.content_sha256 || transfer.bytes.length !== artifact.byte_length) {
        throw new ProtocolViolation('start artifact does not match a complete verified transfer');
      }
      used.add(artifact.artifact_id);
      artifacts.push({ artifact_id: artifact.artifact_id, content_type: artifact.content_type, content: transfer.content });
    }
    if (used.size !== this.transfers.size) throw new ProtocolViolation('unreferenced artifact transfer');
    if (artifacts.reduce((sum, artifact) => sum + Buffer.byteLength(artifact.content, 'utf8'), 0) > 1048576) throw new ProtocolViolation('artifact aggregate exceeds input limit');
    this.started = true;
    this.transfers.clear();
    return { ...frame, ...(frame.artifacts === undefined ? {} : { artifacts }) } as StartFrame;
  }
}
