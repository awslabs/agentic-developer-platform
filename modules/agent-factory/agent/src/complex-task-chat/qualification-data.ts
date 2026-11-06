import { createHash } from 'node:crypto';
import { z } from 'zod';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { GatewayHistoryStore } from './context/store/gateway-history';
import { GatewayMemoryProvider } from './memory/gateway-memory';
import { QualificationBinding, QualificationFixtures, QualificationObservation } from './qualification-contract';

export const ownerWrites = ['session_write', 'message_write', 'summary_write', 'artifact_write', 'memory_write'];
export const qualificationCanary = 'E45 synthetic isolation canary';

export async function qualificationDataProbe(client: ChatDataClient, binding: QualificationBinding, probe: string,
  fixtures: QualificationFixtures | undefined, signal: AbortSignal): Promise<QualificationObservation | undefined> {
  const scoped = client.withSignal(signal);
  const history = new GatewayHistoryStore(scoped);
  const session = binding.session_id;
  const content = qualificationCanary;
  const key = `e45-${binding.nonce}-${probe}`;
  const result = (outcome: QualificationObservation['outcome']): QualificationObservation => ({ probe, executed: true, outcome, exposure_digests: [] });
  const dataProbes = ['session_read', ...ownerWrites, 'message_read', 'summary_read', 'artifact_read', 'memory_read',
    'cursor_read', 'object_read', 'direct_url_read', 'forged_principal', 'forged_run', 'forged_session', 'forged_generation', 'forged_user_header', 'forged_tenant_header'];
  if (!dataProbes.includes(probe)) return undefined;
  if (binding.actor !== 'a1') {
    if (!fixtures) return undefined;
    try { await scoped.qualificationDataProbe(probe, fixtures, signal); return result('allowed'); }
    catch (error) {
      signal.throwIfAborted();
      if (error instanceof ChatDataError && error.status !== undefined && [401, 403, 404, 422].includes(error.status)) return result('denied');
      throw error;
    }
  }
  if (probe === 'session_write') {
    const read = z.object({ version: z.number().int().nonnegative() }).passthrough().parse(await scoped.sessionRequest('draft/read', session));
    const written = z.object({ version: z.number().int().positive() }).passthrough().parse(await scoped.sessionRequest('draft/write', session,
      { draft: { intent: content }, expected_version: read.version, idempotency_key: key }));
    if (written.version !== read.version + 1) throw new ChatDataError('invalid_response');
  } else if (probe === 'message_write') {
    const accepted = await history.acceptedUserTurn(session);
    const page = await history.readPage(session, { limit: 1 });
    await history.appendAssistant(session, { idempotency_key: key, expected_version: page.version, user_turn_id: accepted.message_id, content, tokens: 8 });
  } else if (probe === 'summary_write') {
    const accepted = await history.acceptedUserTurn(session);
    const page = await history.readPage(session, { limit: 1 });
    await history.appendSummary(session, { idempotency_key: key, expected_version: page.version, source_ids: [accepted.message_id], content, tokens: 8 });
  } else if (probe === 'artifact_write') {
    const checksum = createHash('sha256').update(content).digest('hex');
    z.object({ id: z.string().regex(/^art_[a-f0-9]{12,64}$/), checksum: z.literal(checksum) }).passthrough().parse(
      await scoped.sessionRequest('artifact/create', session, { idempotency_key: key, filename: 'e45.txt', content_type: 'text/plain',
        content_sha256: checksum, content_base64: Buffer.from(content).toString('base64') }));
  } else if (probe === 'memory_write') {
    z.object({ memory_id: z.string().regex(/^mem_[a-f0-9]{32}$/), version: z.literal(1) }).strict().parse(await scoped.runRequest('memory/write',
      { idempotency_key: key, expected_version: 0, content, kind: 'fact' }));
  } else {
    if (!fixtures) return undefined;
    if (probe === 'session_read') await history.readPage(session, { limit: 1 });
    else if (probe === 'message_read') {
      const messages = await history.getMessagesByIds(session, [fixtures.message_id]);
      if (messages.length !== 1 || messages[0].content !== content) throw new ChatDataError('invalid_response');
    } else if (probe === 'summary_read') {
      if ((await history.getSummaryById(session, fixtures.summary_id)).content !== content) throw new ChatDataError('invalid_response');
    } else if (probe === 'artifact_read') {
      const page = z.object({ entries: z.array(z.object({ id: z.string() }).passthrough()) }).passthrough().parse(
        await scoped.sessionRequest('artifact/list', session, { filename: 'e45.txt', limit: 1 }));
      if (!page.entries.some(entry => entry.id === fixtures.artifact_id)) throw new ChatDataError('incomplete');
    } else if (probe === 'memory_read') {
      if ((await new GatewayMemoryProvider(scoped).read(fixtures.memory_id)).content !== content) throw new ChatDataError('invalid_response');
    } else if (probe === 'cursor_read') await history.readPage(session, { limit: 1, cursor: fixtures.cursor });
    else if (probe === 'object_read') {
      if (!(await scoped.downloadArtifact(session, fixtures.artifact_id)).equals(Buffer.from(content))) throw new ChatDataError('invalid_response');
    } else if (probe === 'direct_url_read') await scoped.qualificationDataProbe(probe, fixtures, signal);
    else return undefined;
  }
  return result('allowed');
}
