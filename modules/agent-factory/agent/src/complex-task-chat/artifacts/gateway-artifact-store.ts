import { createHash, randomUUID } from 'crypto';
import { constants } from 'fs';
import { mkdir, open, realpath, rename, unlink } from 'fs/promises';
import path from 'path';
import { z } from 'zod';
import { AgentTool } from '../context/types';
import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { ArtifactRef, ArtifactStore, CallerIdentity, TurnScope } from './port';

const MAX_BYTES = 8 * 1024 * 1024;
const artifactId = z.string().regex(/^art_[a-f0-9]{12,64}$/);
const filenameSchema = z.string().min(1).max(256).regex(/^[^/\\\x00-\x1f\x7f]+$/);
const contentTypeSchema = z.string().max(128).regex(/^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+(?:; charset=[A-Za-z0-9_.-]+)?$/);
const referenceSchema = z.object({
  id: artifactId,
  filename: filenameSchema,
  contentType: contentTypeSchema,
  sizeBytes: z.number().int().min(1).max(MAX_BYTES),
  checksum: z.string().regex(/^[a-f0-9]{64}$/),
  createdAt: z.iso.datetime({ offset: true }),
  source: z.enum(['agent', 'user']),
  supersedes: artifactId.optional(),
  url: z.string().max(2048),
  urlExpiresAt: z.iso.datetime({ offset: true }),
  scanStatus: z.enum(['clean', 'not_scanned']),
});
const pageSchema = z.object({
  status: z.enum(['ok', 'empty', 'partial']),
  entries: z.array(referenceSchema).max(100),
  next_cursor: z.string().min(1).max(2048).nullable(),
  observed_at: z.iso.datetime({ offset: true }),
  coverage: z.object({ source: z.literal('session_artifacts'), complete: z.boolean(), missing_source_ids: z.array(artifactId) }),
});
const filterSchema = z.object({
  contentType: contentTypeSchema.optional(),
  filename: z.string().max(256).optional(),
  limit: z.number().int().min(1).max(10_000).optional(),
}).strict();
const contentTypes: Record<string, string> = {
  '.pdf': 'application/pdf', '.csv': 'text/csv', '.json': 'application/json', '.html': 'text/html',
  '.md': 'text/markdown', '.txt': 'text/plain', '.png': 'image/png', '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.zip': 'application/zip',
  '.tar': 'application/x-tar', '.gz': 'application/gzip',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};

export class GatewayArtifactStore implements ArtifactStore {
  constructor(private readonly client: ChatDataClient, private readonly sessionId: string, private readonly workspaceRoot: string) {
    if (!workspaceRoot || !path.isAbsolute(workspaceRoot) || path.resolve(workspaceRoot) === path.parse(workspaceRoot).root || !sessionId) {
      throw new ChatDataError('invalid_request');
    }
  }

  #session(sessionId: string): void {
    if (sessionId !== this.sessionId) throw new ChatDataError('scope_mismatch');
  }

  async #workspacePath(filename: string, allowRoot = false): Promise<string> {
    if (typeof filename !== 'string' || !filename || filename.includes('\0')) throw new ChatDataError('invalid_request');
    const root = await realpath(this.workspaceRoot);
    const requested = path.resolve(root, filename);
    const resolved = await realpath(requested);
    const relative = path.relative(root, resolved);
    if ((!relative && !allowRoot) || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new ChatDataError('invalid_request');
    }
    return resolved;
  }

  async #destinationParent(filename: string) {
    if (typeof filename !== 'string' || !filename || filename.includes('\0')) throw new ChatDataError('invalid_request');
    const root = await realpath(this.workspaceRoot);
    const requested = path.resolve(root, filename);
    let ancestor = path.dirname(requested);
    const missing: string[] = [];
    let resolved: string;
    while (true) {
      try {
        resolved = await this.#workspacePath(ancestor, true);
        break;
      } catch (error) {
        const parent = path.dirname(ancestor);
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || parent === ancestor) throw error;
        missing.unshift(path.basename(ancestor));
        ancestor = parent;
      }
    }
    const directory = await open(resolved, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
    try {
      await this.#workspacePath(`/proc/self/fd/${directory.fd}`, true);
      return { directory, missing, filename: path.basename(requested) };
    } catch (error) {
      await directory.close();
      throw error;
    }
  }

  #reference(raw: unknown): ArtifactRef {
    const parsed = referenceSchema.safeParse(raw);
    if (!parsed.success) throw new ChatDataError('invalid_response');
    const { scanStatus, ...reference } = parsed.data;
    return { ...reference, url: this.client.artifactUrl(this.sessionId, reference.id) };
  }

  async publish(input: Parameters<ArtifactStore['publish']>[0]): Promise<ArtifactRef> {
    this.#session(input.sessionId);
    if (input.source !== undefined && input.source !== 'agent') throw new ChatDataError('invalid_request');
    if (input.ttl !== undefined) throw new ChatDataError('invalid_request');
    const filename = input.filename ?? path.basename(input.localPath);
    const contentType = input.contentType ?? contentTypes[path.extname(filename).toLowerCase()] ?? 'application/octet-stream';
    if (!filenameSchema.safeParse(filename).success || !contentTypeSchema.safeParse(contentType).success
      || (input.supersedes !== undefined && !artifactId.safeParse(input.supersedes).success)) {
      throw new ChatDataError('invalid_request');
    }
    const source = await this.#workspacePath(input.localPath);
    const file = await open(source, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    let content: Buffer;
    try {
      await this.#workspacePath(`/proc/self/fd/${file.fd}`);
      const info = await file.stat();
      if (!info.isFile() || info.size < 1 || info.size > MAX_BYTES) throw new ChatDataError('invalid_request');
      const buffer = Buffer.alloc(info.size + 1);
      let size = 0;
      while (size < buffer.length) {
        const { bytesRead } = await file.read(buffer, size, buffer.length - size, size);
        if (!bytesRead) break;
        size += bytesRead;
      }
      if (size !== info.size) throw new ChatDataError('invalid_request');
      content = buffer.subarray(0, size);
    } finally {
      await file.close();
    }
    const checksum = createHash('sha256').update(content).digest('hex');
    const metadata = { filename, content_type: contentType, content_sha256: checksum, ...(input.supersedes ? { supersedes: input.supersedes } : {}) };
    const key = createHash('sha256').update(JSON.stringify([this.sessionId, input.taskId ?? '', metadata])).digest('hex');
    const reference = this.#reference(await this.client.sessionRequest('artifact/create', this.sessionId, {
      ...metadata, idempotency_key: key, content_base64: content.toString('base64'),
    }));
    if (reference.checksum !== checksum || reference.sizeBytes !== content.length || reference.filename !== filename
      || reference.contentType !== contentType || reference.source !== 'agent' || reference.supersedes !== input.supersedes) {
      throw new ChatDataError('invalid_response');
    }
    return reference;
  }

  async fetch(id: string, destPath: string, sessionId: string, _identity?: CallerIdentity): Promise<void> {
    this.#session(sessionId);
    const destination = await this.#destinationParent(destPath);
    let directory = destination.directory;
    try {
      const content = await this.client.downloadArtifact(sessionId, id);
      for (const segment of destination.missing) {
        const parent = `/proc/self/fd/${directory.fd}`;
        await this.#workspacePath(parent, true);
        const childPath = path.join(parent, segment);
        await mkdir(childPath, { mode: 0o700 }).catch(error => { if (error.code !== 'EEXIST') throw error; });
        const child = await open(childPath, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
        try {
          await this.#workspacePath(`/proc/self/fd/${child.fd}`, true);
        } catch (error) {
          await child.close();
          throw error;
        }
        const previous = directory;
        directory = child;
        await previous.close();
      }
      const anchored = `/proc/self/fd/${directory.fd}`;
      await this.#workspacePath(anchored, true);
      const temporary = path.join(anchored, `.adp-artifact-${randomUUID()}`);
      const file = await open(temporary, 'wx', 0o600);
      try {
        await file.writeFile(content);
        await file.close();
        await rename(temporary, path.join(anchored, destination.filename));
      } finally {
        await file.close();
        await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
      }
    } finally {
      await directory.close();
    }
  }

  async listBySession(sessionId: string, filter: Parameters<ArtifactStore['listBySession']>[1] = {}, _identity?: CallerIdentity): Promise<ArtifactRef[]> {
    this.#session(sessionId);
    const parsed = filterSchema.safeParse(filter);
    if (!parsed.success) throw new ChatDataError('invalid_request');
    const limit = parsed.data.limit ?? 20;
    const request = {
      limit: Math.min(limit, 100),
      ...(parsed.data.contentType !== undefined ? { content_type: parsed.data.contentType } : {}),
      ...(parsed.data.filename !== undefined ? { filename: parsed.data.filename } : {}),
    };
    const references: ArtifactRef[] = [];
    const ids = new Set<string>();
    const cursors = new Set<string>();
    let cursor: string | null = null;
    for (let pageNumber = 0; pageNumber < 100; pageNumber++) {
      const parsedPage = pageSchema.safeParse(await this.client.sessionRequest('artifact/list', sessionId, { ...request, ...(cursor ? { cursor } : {}) }));
      if (!parsedPage.success) throw new ChatDataError('invalid_response');
      const page = parsedPage.data;
      if (page.coverage.missing_source_ids.length) throw new ChatDataError('incomplete');
      if (page.coverage.complete !== (page.next_cursor === null)
        || page.status !== (page.next_cursor ? 'partial' : page.entries.length ? 'ok' : 'empty')) {
        throw new ChatDataError('invalid_response');
      }
      for (const entry of page.entries) {
        if (ids.has(entry.id)) throw new ChatDataError('invalid_response');
        ids.add(entry.id);
        references.push(this.#reference(entry));
      }
      if (references.length >= limit || !page.next_cursor) return references.slice(0, limit);
      if (cursors.has(page.next_cursor)) throw new ChatDataError('invalid_response');
      cursors.add(page.next_cursor);
      cursor = page.next_cursor;
    }
    throw new ChatDataError('incomplete');
  }

  toolsForTurn(scope: TurnScope): AgentTool[] {
    this.#session(scope.sessionId);
    return [
      {
        name: 'publish_artifact',
        description: 'Publish a workspace file. Downloads require fresh gateway authorization; the reference is not a public object URL.',
        inputSchema: { path: z.string(), filename: z.string().optional(), contentType: z.string().optional(), supersedes: z.string().optional() },
        handler: async input => {
          const reference = await this.publish({
            sessionId: this.sessionId, taskId: scope.taskId, localPath: input.path as string,
            filename: input.filename as string | undefined, contentType: input.contentType as string | undefined,
            supersedes: input.supersedes as string | undefined,
          });
          scope.onPublish?.(reference);
          return { content: [{ type: 'text', text: JSON.stringify(reference) }] };
        },
      },
      {
        name: 'fetch_artifact', description: 'Download an authorized artifact into the workspace.',
        inputSchema: { id: z.string(), dest_path: z.string() },
        handler: async input => {
          await this.fetch(input.id as string, input.dest_path as string, this.sessionId);
          return { content: [{ type: 'text', text: `Downloaded ${input.id} to ${input.dest_path}` }] };
        },
      },
      {
        name: 'list_artifacts', description: 'List authorized artifacts in the current session.',
        inputSchema: { content_type: z.string().optional(), filename: z.string().optional(), limit: z.number().int().positive().optional() },
        handler: async input => {
          const references = await this.listBySession(this.sessionId, {
            contentType: input.content_type as string | undefined, filename: input.filename as string | undefined, limit: input.limit as number | undefined,
          });
          return { content: [{ type: 'text', text: references.length ? JSON.stringify(references) : 'No artifacts found.' }] };
        },
      },
    ];
  }
}
