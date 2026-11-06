import { createHash, timingSafeEqual } from 'node:crypto';
import { closeSync, constants, openSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { request as requestLocal, type IncomingMessage } from 'node:http';
import { createServer as createHttpsServer, type ServerOptions } from 'node:https';
import { isIPv4, Socket } from 'node:net';
import type { TLSSocket } from 'node:tls';
import { z } from 'zod';
import { createCrewDashboardCustody, mintCrewLink, type CrewCustodyClient } from './crew-dashboard-custody';

export function createCrewHostIngress(options: {
  privateIP: string; certificate: Buffer; key: Buffer; gatewayCA: Buffer; gatewayFingerprint: string; isCurrent: () => boolean;
  dashboardPort?: number; custodyPort?: number; runId?: string; generation?: number;
  tokenDirectory?: string;
}) {
  if (!isIPv4(options.privateIP) || !/^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/.test(options.privateIP) ||
      !options.certificate.length || !options.key.length || !options.gatewayCA.length ||
      !/^[0-9a-f]{64}$/.test(options.gatewayFingerprint) || typeof options.isCurrent !== 'function') {
    throw new Error('Crew ingress requires a private host and gateway mTLS');
  }
  const dashboardPort = options.dashboardPort ?? 5476;
  const custodyPort = options.custodyPort ?? 5477;
  const tls: ServerOptions = { cert: options.certificate, key: options.key, ca: options.gatewayCA,
    requestCert: true, rejectUnauthorized: true, minVersion: 'TLSv1.2' };
  const expectedFingerprint = Buffer.from(options.gatewayFingerprint, 'hex');
  const authorized = (socket: TLSSocket) => {
    const certificate = socket.getPeerCertificate(true).raw;
    return socket.authorized && certificate && timingSafeEqual(
      createHash('sha256').update(certificate).digest(), expectedFingerprint);
  };
  const validPath = (request: IncomingMessage) => typeof request.url === 'string' &&
    request.url.startsWith('/') && !request.url.startsWith('//') && !/[\r\n\\]/.test(request.url);
  const modelToken = z.object({ run_id: z.string().uuid(), lease_generation: z.number().int().positive(),
    token: z.string().min(20).max(2048), expires_at: z.number().int().positive() }).strict();
  const tokenDirectory = options.tokenDirectory ?? '/run/adp/crew-host/model-token';
  const acceptToken = (request: IncomingMessage, reply: import('node:http').ServerResponse) => {
    if (request.method !== 'POST' || !options.runId || !options.generation) { reply.writeHead(405).end(); return; }
    const parts: Buffer[] = [];
    let size = 0;
    request.on('data', (part: Buffer) => {
      size += part.length;
      if (size > 4096) request.destroy();
      else parts.push(part);
    });
    request.on('end', () => {
      try {
        const payload = modelToken.parse(JSON.parse(Buffer.concat(parts).toString('utf8')));
        const now = Math.floor(Date.now() / 1000);
        if (payload.run_id !== options.runId || payload.lease_generation !== options.generation ||
            payload.expires_at <= now || payload.expires_at > now + 60 || !options.isCurrent()) {
          reply.writeHead(403).end(); return;
        }
        const pending = join(tokenDirectory, `.token-${randomUUID()}`);
        const handle = openSync(pending, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
        try { writeFileSync(handle, JSON.stringify(payload)); } finally { closeSync(handle); }
        renameSync(pending, join(tokenDirectory, 'token.json'));
        reply.writeHead(204).end();
      } catch { reply.writeHead(403).end(); }
    });
  };
  const server = createHttpsServer(tls, (incoming, reply) => {
    if (!authorized(incoming.socket as TLSSocket) || !options.isCurrent() || !validPath(incoming)) {
      reply.writeHead(403).end();
      return;
    }
    if (incoming.url === '/model-token') { acceptToken(incoming, reply); return; }
    const mint = incoming.url === '/mint';
    if (incoming.url?.startsWith('/mint') && !mint) {
      reply.writeHead(404).end();
      return;
    }
    if (mint && incoming.method !== 'POST') {
      reply.writeHead(405).end();
      return;
    }
    const upstream = requestLocal({ host: '127.0.0.1', port: mint ? custodyPort : dashboardPort,
      path: incoming.url, method: incoming.method, headers: incoming.headers }, response => {
      reply.writeHead(response.statusCode ?? 502, response.headers);
      response.pipe(reply);
    });
    upstream.on('error', () => { if (!reply.headersSent) reply.writeHead(502); reply.end(); });
    incoming.pipe(upstream);
  });
  server.on('upgrade', (incoming, socket, head) => {
    if (!authorized(incoming.socket as TLSSocket) || !options.isCurrent() || !validPath(incoming) || (incoming.url === '/mint' || incoming.url === '/model-token')) {
      socket.destroy();
      return;
    }
    const upstream = new Socket();
    upstream.once('error', () => socket.destroy());
    socket.once('error', () => upstream.destroy());
    upstream.connect(dashboardPort, '127.0.0.1', () => {
      upstream.write(`${incoming.method} ${incoming.url} HTTP/${incoming.httpVersion}\r\n`);
      for (let index = 0; index < incoming.rawHeaders.length; index += 2) {
        upstream.write(`${incoming.rawHeaders[index]}: ${incoming.rawHeaders[index + 1]}\r\n`);
      }
      upstream.write('\r\n');
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
  });
  server.requestTimeout = 5_000;
  return {
    listen: (port = 8443, host = options.privateIP) => new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.off('error', reject); resolve(); });
    }),
    close: () => new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve())),
    address: () => server.address(),
  };
}

export async function startCrewHostDashboard(
  client: CrewCustodyClient, runId: string,
  options: { privateIP: string; certificate: Buffer; key: Buffer; gatewayCA: Buffer;
    gatewayFingerprint: string; isCurrent: () => boolean; dashboardPort?: number; listenPort?: number;
    generation?: number; tokenDirectory?: string },
  mint: (home: string) => Promise<string> = home => mintCrewLink(home, undefined, 'host'),
  bind: (ingress: ReturnType<typeof createCrewHostIngress>, port: number) => Promise<void> =
    (ingress, port) => ingress.listen(port),
  now: () => number = Date.now,
) {
  const custody = createCrewDashboardCustody(client, runId, options.privateIP, '/mnt/crew', mint, now);
  await custody.listen(0, '127.0.0.1');
  try {
    const ingress = createCrewHostIngress({ ...options, runId, custodyPort: (custody.address() as { port: number }).port });
    await bind(ingress, options.listenPort ?? 8443);
    return { address: ingress.address, close: async () => { await ingress.close(); await custody.close(); } };
  } catch (error) {
    await custody.close();
    throw error;
  }
}
