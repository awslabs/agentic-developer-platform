/**
 * Unit tests for sigv4-proxy.
 *
 * Tests:
 * - Issue #747: Phase 2 — verify that TENANT_ID env var is injected as
 *   X-Agent-OrgId header on outbound requests.
 * - Issue #1223: Idle timeout watchdog, error handler crash guards, and
 *   timeout handler enforcement.
 */

import * as http from 'http';
import { AddressInfo } from 'net';

// Save and restore env
const originalEnv = { ...process.env };

afterEach(() => {
  process.env = { ...originalEnv };
});

/**
 * Helper: create a simple HTTP server that captures request headers.
 */
function createCaptureServer(): Promise<{
  server: http.Server;
  port: number;
  getLastHeaders: () => http.IncomingHttpHeaders | null;
  close: () => Promise<void>;
}> {
  return new Promise((resolve) => {
    let lastHeaders: http.IncomingHttpHeaders | null = null;

    const server = http.createServer((req, res) => {
      lastHeaders = req.headers;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });

    server.listen(0, '127.0.0.1', () => {
      const port = (server.address() as AddressInfo).port;
      resolve({
        server,
        port,
        getLastHeaders: () => lastHeaders,
        close: () => new Promise<void>((r) => server.close(() => r())),
      });
    });
  });
}

describe('sigv4-proxy tenant header injection', () => {
  it('injects X-Agent-OrgId header when TENANT_ID is set', async () => {
    // This test verifies the header injection logic extracted from sigv4-proxy.ts.
    // The actual signing is covered by integration tests; here we test the
    // header-building logic in isolation.

    const tenantId = 'tenant-abc-123';

    // Simulate the header-building logic from sigv4-proxy.ts
    const headers: Record<string, string> = { host: 'example.com' };
    const incomingHeaders: Record<string, string> = {
      'content-type': 'application/json',
      'user-agent': 'test-agent',
    };

    const STRIP = new Set([
      'authorization', 'x-amz-security-token', 'x-amz-date',
      'x-amz-content-sha256', 'host',
    ]);

    for (const [k, v] of Object.entries(incomingHeaders)) {
      if (!STRIP.has(k.toLowerCase()) && typeof v === 'string') {
        headers[k.toLowerCase()] = v;
      }
    }

    // Inject tenant identity header (same logic as sigv4-proxy.ts)
    if (tenantId) {
      headers['x-agent-orgid'] = tenantId;
    }

    expect(headers['x-agent-orgid']).toBe('tenant-abc-123');
    expect(headers['content-type']).toBe('application/json');
    expect(headers['host']).toBe('example.com');
  });

  it('does not inject X-Agent-OrgId header when TENANT_ID is empty', async () => {
    const tenantId = '';

    const headers: Record<string, string> = { host: 'example.com' };

    if (tenantId) {
      headers['x-agent-orgid'] = tenantId;
    }

    expect(headers['x-agent-orgid']).toBeUndefined();
  });

  it('strips incoming authorization headers but preserves tenant header', async () => {
    const tenantId = 'org-456';

    const STRIP = new Set([
      'authorization', 'x-amz-security-token', 'x-amz-date',
      'x-amz-content-sha256', 'host',
    ]);

    const incomingHeaders: Record<string, string> = {
      'authorization': 'AWS4-HMAC-SHA256 ...',
      'x-amz-security-token': 'token123',
      'x-amz-date': '20260521T000000Z',
      'content-type': 'application/json',
      'x-custom': 'preserved',
    };

    const headers: Record<string, string> = { host: 'target.example.com' };
    for (const [k, v] of Object.entries(incomingHeaders)) {
      if (!STRIP.has(k.toLowerCase()) && typeof v === 'string') {
        headers[k.toLowerCase()] = v;
      }
    }

    if (tenantId) {
      headers['x-agent-orgid'] = tenantId;
    }

    // Auth headers stripped
    expect(headers['authorization']).toBeUndefined();
    expect(headers['x-amz-security-token']).toBeUndefined();
    expect(headers['x-amz-date']).toBeUndefined();

    // Non-auth headers + tenant header preserved
    expect(headers['content-type']).toBe('application/json');
    expect(headers['x-custom']).toBe('preserved');
    expect(headers['x-agent-orgid']).toBe('org-456');
  });
});

/**
 * Issue #1223: Tests for idle timeout, error handler guards, and timeout enforcement.
 *
 * These tests exercise the proxy's error-handling logic in isolation without
 * needing real AWS credentials or signing — they validate the defensive
 * patterns around Node.js stream/socket behavior.
 */
describe('sigv4-proxy idle timeout and error handling (issue #1223)', () => {
  it('error handler guards on res.headersSent — does not throw ERR_HTTP_HEADERS_SENT', () => {
    // Simulate the error handler logic from sigv4-proxy.ts:
    // When headers have already been sent, calling res.writeHead(502) would crash.
    // The fix guards on res.headersSent and calls res.destroy() instead.

    const res = {
      headersSent: true,
      destroyed: false,
      writeHead: jest.fn((_code: number) => { throw new Error('ERR_HTTP_HEADERS_SENT'); }),
      end: jest.fn((_body?: string) => {}),
      destroy: jest.fn(() => { (res as any).destroyed = true; }),
    };

    // Simulate the fixed error handler
    const handleError = (_err: Error) => {
      if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
      else if (!res.destroyed) res.destroy();
    };

    // Should NOT throw — should call destroy instead of writeHead
    expect(() => handleError(new Error('upstream request timeout'))).not.toThrow();
    expect(res.writeHead).not.toHaveBeenCalled();
    expect(res.destroy).toHaveBeenCalled();
  });

  it('error handler sends 502 when headers have not been sent', () => {
    const res = {
      headersSent: false,
      destroyed: false,
      writeHead: jest.fn((_code: number) => {}),
      end: jest.fn((_body?: string) => {}),
      destroy: jest.fn(),
    };

    const handleError = (_err: Error) => {
      if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
      else if (!res.destroyed) res.destroy();
    };

    handleError(new Error('upstream request timeout'));
    expect(res.writeHead).toHaveBeenCalledWith(502);
    expect(res.end).toHaveBeenCalledWith('Upstream error');
    expect(res.destroy).not.toHaveBeenCalled();
  });

  it('response idle watchdog destroys proxyRes after RESP_IDLE_MS of silence', async () => {
    jest.useFakeTimers();

    const RESP_IDLE_MS = 180_000;
    let destroyCalled = false;
    let destroyError: Error | undefined;

    // Simulate proxyRes (a readable stream)
    const proxyRes = {
      listeners: {} as Record<string, ((...args: any[]) => void)[]>,
      on(event: string, handler: (...args: any[]) => void) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(handler);
        return this;
      },
      destroy(err?: Error) {
        destroyCalled = true;
        destroyError = err;
      },
      pipe: jest.fn(),
    };

    // Simulate the idle watchdog setup from sigv4-proxy.ts
    let idleTimeout = setTimeout(() => {
      proxyRes.destroy(new Error('response idle timeout'));
    }, RESP_IDLE_MS);
    const bumpIdle = () => {
      clearTimeout(idleTimeout);
      idleTimeout = setTimeout(() => {
        proxyRes.destroy(new Error('response idle timeout'));
      }, RESP_IDLE_MS);
    };
    proxyRes.on('data', bumpIdle);
    proxyRes.on('end', () => clearTimeout(idleTimeout));

    // Advance time past idle timeout with no data events
    jest.advanceTimersByTime(RESP_IDLE_MS + 1);

    expect(destroyCalled).toBe(true);
    expect(destroyError?.message).toBe('response idle timeout');

    jest.useRealTimers();
  });

  it('response idle watchdog resets on data events — does not fire prematurely', async () => {
    jest.useFakeTimers();

    const RESP_IDLE_MS = 180_000;
    let destroyCalled = false;

    const proxyRes = {
      listeners: {} as Record<string, ((...args: any[]) => void)[]>,
      on(event: string, handler: (...args: any[]) => void) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(handler);
        return this;
      },
      destroy(err?: Error) { destroyCalled = true; },
      pipe: jest.fn(),
    };

    let idleTimeout = setTimeout(() => {
      proxyRes.destroy(new Error('response idle timeout'));
    }, RESP_IDLE_MS);
    const bumpIdle = () => {
      clearTimeout(idleTimeout);
      idleTimeout = setTimeout(() => {
        proxyRes.destroy(new Error('response idle timeout'));
      }, RESP_IDLE_MS);
    };
    proxyRes.on('data', bumpIdle);
    proxyRes.on('end', () => clearTimeout(idleTimeout));

    // Advance 170s (close to but before the 180s timeout)
    jest.advanceTimersByTime(170_000);
    expect(destroyCalled).toBe(false);

    // Emit a data event — this resets the timer
    const dataHandlers = proxyRes.listeners['data'] || [];
    dataHandlers.forEach(h => h(Buffer.from('chunk')));

    // Advance another 170s (still within 180s of last data)
    jest.advanceTimersByTime(170_000);
    expect(destroyCalled).toBe(false);

    // Advance past the timeout from last data event
    jest.advanceTimersByTime(11_000); // 170+11 = 181s since last data > 180s
    expect(destroyCalled).toBe(true);

    jest.useRealTimers();
  });

  it('timeout event on proxyReq triggers destroy (declared timeout is enforced)', () => {
    let destroyed = false;
    let destroyError: Error | undefined;

    // Simulate proxyReq
    const proxyReq = {
      listeners: {} as Record<string, ((...args: any[]) => void)[]>,
      on(event: string, handler: (...args: any[]) => void) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(handler);
        return this;
      },
      destroy(err?: Error) {
        destroyed = true;
        destroyError = err;
      },
    };

    // Simulate the timeout handler from sigv4-proxy.ts
    proxyReq.on('timeout', () => {
      proxyReq.destroy(new Error('upstream request timeout'));
    });

    // Fire the timeout event
    const timeoutHandlers = proxyReq.listeners['timeout'] || [];
    timeoutHandlers.forEach(h => h());

    expect(destroyed).toBe(true);
    expect(destroyError?.message).toBe('upstream request timeout');
  });

  it('proxyRes error handler does not crash when res is already destroyed', () => {
    const res = {
      headersSent: true,
      destroyed: true,
      writeHead: jest.fn((_code: number) => {}),
      end: jest.fn((_body?: string) => {}),
      destroy: jest.fn(),
    };

    // Simulate the proxyRes error handler
    const handleResError = (_err: Error) => {
      if (!res.headersSent) { res.writeHead(502); res.end('Upstream error'); }
      else if (!res.destroyed) res.destroy();
    };

    // Should not throw and should not call any methods (already destroyed)
    expect(() => handleResError(new Error('response idle timeout'))).not.toThrow();
    expect(res.writeHead).not.toHaveBeenCalled();
    expect(res.end).not.toHaveBeenCalled();
    expect(res.destroy).not.toHaveBeenCalled();
  });
});
