import { setTimeout as sleep } from 'node:timers/promises';
import { z } from 'zod';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { GatewayHistoryStore } from './context/store/gateway-history';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { qualificationClaim, qualificationFixtures, qualificationManifest, qualificationPlan, QualificationObservation } from './qualification-contract';
import { QualificationDiagnostics } from './qualification-diagnostics';
import { ownerWrites, qualificationDataProbe } from './qualification-data';

async function pendingRequest(client: ChatDataClient, signal?: AbortSignal) {
  const bounded = AbortSignal.any([AbortSignal.timeout(20_000), ...(signal ? [signal] : [])]);
  for (let attempt = 0; attempt < 80; attempt++) {
    bounded.throwIfAborted();
    try { return qualificationPlan.parse(await client.qualificationRequest('pending', {}, bounded)); }
    catch (error) {
      if (!(error instanceof ChatDataError) || !['unavailable', 'denied'].includes(error.code)) throw error;
      await sleep(250, undefined, { signal: bounded });
    }
  }
  throw new ChatDataError('incomplete');
}

export async function executeSandboxQualification(client: ChatDataClient, signal?: AbortSignal): Promise<void> {
  if (!client.qualificationRequired()) throw new ChatDataError('scope_mismatch');
  const pending = await pendingRequest(client, signal);
  const binding = pending.binding;
  const now = Math.floor(Date.now() / 1000);
  if (binding.issued_at > now || binding.expires_at <= now || binding.expires_at - binding.issued_at > 120) throw new ChatDataError('expired');
  const claim = qualificationClaim.parse({ nonce: binding.nonce, evaluation_id: binding.evaluation_id, manifest_digest: binding.manifest_digest });
  const claimed = qualificationPlan.parse(await client.qualificationRequest('claim', claim, signal));
  if (canonicalJson(claimed) !== canonicalJson(pending)) throw new ChatDataError('scope_mismatch');
  const cancelled = new AbortController();
  const stopped = new AbortController();
  const execution = AbortSignal.any([cancelled.signal, AbortSignal.timeout(Math.max(1, binding.expires_at * 1000 - Date.now())), ...(signal ? [signal] : [])]);
  const diagnostics = new QualificationDiagnostics(binding);
  let fixtures = claimed.fixtures;
  const history = new GatewayHistoryStore(client);
  const observations: QualificationObservation[] = [];
  let failure: 'probe_unavailable' | 'deadline' | 'execution_error' | null = null;
  const monitor = (async () => {
    try {
      while (!stopped.signal.aborted) {
        const active = z.object({ nonce: z.literal(binding.nonce), active: z.literal(true), expires_at: z.literal(binding.expires_at) }).strict();
        active.parse(await client.qualificationRequest('active', claim, AbortSignal.any([execution, stopped.signal])));
        await sleep(500, undefined, { signal: AbortSignal.any([execution, stopped.signal]) });
      }
    } catch (error) { if (!stopped.signal.aborted) cancelled.abort(error); }
  })();
  try {
    const probes = binding.actor === 'a1' ? [...ownerWrites, ...qualificationManifest.probes.a1.filter(probe => !ownerWrites.includes(probe))] : qualificationManifest.probes[binding.actor];
    for (const probe of probes) {
      execution.throwIfAborted();
      const bounded = AbortSignal.any([execution, AbortSignal.timeout(5000)]);
      try {
        if (binding.actor === 'a1' && probe === 'session_read') {
          fixtures = qualificationFixtures.parse(await client.qualificationRequest('fixtures', claim, bounded));
          if (canonicalJson(fixtures.owner) !== canonicalJson(binding)) throw new ChatDataError('scope_mismatch');
        }
        let observation = await qualificationDataProbe(client, binding, probe, fixtures, bounded)
          ?? await diagnostics.execute(probe, bounded, fixtures);
        execution.throwIfAborted();
        if (!observation) {
          failure = 'probe_unavailable';
          observation = { probe, executed: false, outcome: 'error', exposure_digests: [] };
        }
        observations.push(observation);
      } catch {
        execution.throwIfAborted();
        observations.push({ probe, executed: true, outcome: bounded.aborted ? 'timeout' : 'error', exposure_digests: [] });
      }
    }
  } catch { failure = execution.aborted ? 'deadline' : 'execution_error'; }
  finally {
    stopped.abort();
    await monitor;
    try { await diagnostics.cleanup(failure === null && binding.actor === 'a1' && !execution.aborted); }
    catch { failure = 'execution_error'; }
  }
  signal?.throwIfAborted();
  const receipt = await client.qualificationRequest('result', {
    ...claim, source: 'sandbox_runtime', status: failure ? 'failed' : 'completed', observations, failure,
  }, signal);
  z.object({ nonce: z.literal(binding.nonce), result_digest: z.string().regex(/^[a-f0-9]{64}$/) }).strict().parse(receipt);
  if (failure) {
    await client.submitTurnResult({ outcome: 'failed' });
  } else {
    const accepted = await history.acceptedUserTurn(binding.session_id);
    const page = await history.readPage(binding.session_id, { limit: 1 });
    const message = await history.appendAssistant(binding.session_id, { idempotency_key: `qualification-${binding.nonce}`,
      expected_version: page.version, user_turn_id: accepted.message_id, content: 'Sandbox qualification diagnostics recorded.', tokens: 12 });
    await client.submitTurnResult({ outcome: 'completed', message_id: message.message_id });
  }
}
