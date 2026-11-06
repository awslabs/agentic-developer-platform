import { execFile as runFile } from 'node:child_process';
import type { KeyObject } from 'node:crypto';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { isIPv4 } from 'node:net';
import { promisify } from 'node:util';
import { z } from 'zod';
import { verifyEnvelope } from '../control-envelope';

const execFile = promisify(runFile);
const mintRequest = z.object({
  run_id: z.string().uuid(), lease_generation: z.number().int().positive(),
  user_id: z.string().min(1).max(128), request_id: z.string().uuid(),
}).strict();
const LINK_TTL_SECONDS = 300;

export const MINT_TIMEOUT_MS = 20_000;

export async function mintCrewLink(home: string, runner: typeof execFile = execFile,
  target: 'pod' | 'host' = 'pod'): Promise<string> {
  const binary = target === 'host' ? '/opt/adp/crew/current/venv/bin/kirocrew' : '/opt/kirocrew/venv/bin/kirocrew';
  const { stdout } = await runner(binary, ['token', '--ttl', '5m', '--port', '5476'], {
    cwd: home, env: { HOME: home, KIROCREW_HOME: home, PATH: `${binary.slice(0, -'/kirocrew'.length)}:/usr/bin:/bin` },
    // kirocrew's CLI start-up alone takes ~6.4 s on an m6i.xlarge host (dev 2026-10-19, #8223);
    // the previous 5 s killed every mint and the gateway saw 503 "unavailable".
    timeout: MINT_TIMEOUT_MS, maxBuffer: 4096,
  });
  const link = stdout.match(/https?:\/\/[^\s]+[?&]token=([A-Za-z0-9_.-]{16,4096})(?:[&#\s]|$)/);
  if (!link) throw new Error('Kiro Crew did not return a link token');
  return link[1];
}

export type CrewCustodyClient = {
  modelDecision(): Promise<{ runId: string; generation: number; tenantId: string; userId: string }>;
  dashboardVerificationKeys(): Promise<Map<string, KeyObject>>;
};

export function createCrewDashboardCustody(client: CrewCustodyClient,
  runId: string, podIP: string, home: string, mint: (home: string) => Promise<string> = mintCrewLink,
  now: () => number = Date.now) {
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(runId) ||
      !isIPv4(podIP) || !/^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/.test(podIP)) {
    throw new Error('Crew custody requires an admitted run and private pod IP');
  }
  const used = new Map<string, number>();
  const server = createServer(async (request: IncomingMessage, reply: ServerResponse) => {
    const respond = (status: number, payload: object) => {
      reply.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      reply.end(JSON.stringify(payload));
    };
    if (request.method !== 'POST' || request.url !== '/mint') { respond(404, { error: 'not_found' }); return; }
    if (request.headers['content-type'] !== 'application/json') { respond(415, { error: 'invalid_request' }); return; }
    let raw = Buffer.alloc(0);
    try {
      for await (const chunk of request) {
        raw = Buffer.concat([raw, chunk]);
        if (raw.length > 4096) { respond(413, { error: 'invalid_request' }); return; }
      }
      const body = mintRequest.safeParse(JSON.parse(raw.toString('utf8')));
      if (!body.success) { respond(400, { error: 'invalid_request' }); return; }
      const signature = request.headers['x-adp-crew-signature'];
      if (typeof signature !== 'string') { respond(401, { error: 'unauthorized' }); return; }
      const decision = await client.modelDecision();
      if (body.data.run_id !== runId || body.data.run_id !== decision.runId ||
          body.data.lease_generation !== decision.generation) { respond(409, { error: 'stale_lease' }); return; }
      const verified = verifyEnvelope(signature, await client.dashboardVerificationKeys(), {
        runId, generation: decision.generation, action: 'dashboard.mint',
        commandId: body.data.request_id, body: raw, audience: 'adp-crew-dashboard-mint', nowMs: now(),
      });
      if (!verified.ok || verified.envelope.principal !== body.data.user_id ||
          decision.userId !== body.data.user_id || verified.envelope.tenantId !== decision.tenantId) {
        respond(403, { error: 'forbidden' }); return;
      }
      for (const [id, expires] of used) if (expires <= now()) used.delete(id);
      if (used.has(body.data.request_id)) { respond(409, { error: 'replayed_request' }); return; }
      used.set(body.data.request_id, now() + 30_000);
      const expiresAt = now() + LINK_TTL_SECONDS * 1000;
      const linkToken = await mint(home);
      respond(200, { link_token: linkToken, expires_at: new Date(expiresAt).toISOString(),
        lease_generation: decision.generation });
    } catch {
      if (!reply.headersSent) respond(503, { error: 'unavailable' });
    }
  });
  server.requestTimeout = 5_000;
  return {
    listen: (port = 5477, host = podIP) => new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.off('error', reject); resolve(); });
    }),
    close: () => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
    address: () => server.address(),
  };
}
