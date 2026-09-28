import * as http from 'node:http';
import { resolveProviderUrl, startCaptureProxy } from './capture-proxy';
import { requestShapeSha256 } from './canonical-json';

async function upstream(handler: http.RequestListener): Promise<{
  origin: string;
  close(): Promise<void>;
}> {
  const server = http.createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('test server failed to bind');
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

const credentials = { accessKeyId: 'TEST', secretAccessKey: 'TEST', sessionToken: 'TEST' };

async function postRequestTarget(origin: string, requestTarget: string): Promise<number> {
  const destination = new URL(origin);
  return await new Promise<number>((resolve, reject) => {
    const request = http.request({
      hostname: destination.hostname,
      port: destination.port,
      method: 'POST',
      path: requestTarget,
      headers: { 'content-type': 'application/json' },
    }, response => {
      response.resume();
      response.once('end', () => resolve(response.statusCode ?? 0));
    });
    request.once('error', reject);
    request.end('{}');
  });
}

describe('Bedrock request capture proxy', () => {
  it.each(['x@evil.com', 'us-east-1/other', 'us-east-1.example', '', 'us-east-1\n'])('rejects malformed region %j before opening a proxy', async (region) => {
    await expect(startCaptureProxy({ modelId: 'selected-model', region, credentials })).rejects.toThrow('Probe region is malformed');
  });
  it('captures the canonical digest, re-signs, preserves the path/body, and captures request ID', async () => {
    const body = Buffer.from('{"messages":[{"role":"user","content":"probe"}],"max_tokens":8}');
    let receivedPath = '';
    let receivedBody = '';
    let authorization = '';
    const fake = await upstream(async (request, response) => {
      receivedPath = request.url ?? '';
      authorization = String(request.headers.authorization ?? '');
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      receivedBody = Buffer.concat(chunks).toString('utf8');
      response.writeHead(200, {
        'content-type': 'application/json',
        'x-amzn-requestid': 'provider-request-123',
      });
      response.end('{"ok":true}');
    });
    const proxy = await startCaptureProxy({
      modelId: 'us.anthropic.claude-sonnet-4-6',
      region: 'us-east-1',
      credentials,
      expectedRequestShapeSha256: requestShapeSha256(body),
      upstreamBaseUrl: fake.origin,
    });
    try {
      const path = '/model/us.anthropic.claude-sonnet-4-6/invoke-with-response-stream';
      const response = await fetch(`${proxy.baseUrl}${path}`, { method: 'POST', body });
      expect(response.status).toBe(200);
      const captured = await proxy.captured();
      expect(captured).toEqual(expect.objectContaining({
        path,
        providerRequestId: 'provider-request-123',
        providerStatus: 200,
        forwarded: true,
      }));
      expect(receivedPath).toBe(path);
      expect(receivedBody).toBe(body.toString('utf8'));
      expect(authorization).toContain('Credential=TEST/');
      expect(authorization).toContain('/bedrock/aws4_request');
    } finally {
      await proxy.close();
      await fake.close();
    }
  });

  it('does not follow a provider redirect to a second origin', async () => {
    let targetCalls = 0;
    const target = await upstream((_request, response) => {
      targetCalls++;
      response.writeHead(200).end('{}');
    });
    const redirector = await upstream((_request, response) => {
      response.writeHead(302, { location: `${target.origin}/relocated` }).end();
    });
    const proxy = await startCaptureProxy({
      modelId: 'selected-model',
      region: 'us-east-1',
      credentials,
      upstreamBaseUrl: redirector.origin,
    });
    try {
      const captured = proxy.captured().catch(error => error as Error);
      const response = await fetch(`${proxy.baseUrl}/model/selected-model/invoke`, {
        method: 'POST',
        body: '{}',
      });

      expect(response.status).toBe(502);
      expect(await captured).toHaveProperty('message');
      expect(targetCalls).toBe(0);
    } finally {
      await proxy.close();
      await redirector.close();
      await target.close();
    }
  });

  it.each([
    '//evil.example.com/model/selected-model/invoke',
    'http://evil.example.com/model/selected-model/invoke',
  ])('keeps the configured upstream for request target %s', async requestTarget => {
    let receivedPath = '';
    const fake = await upstream((request, response) => {
      receivedPath = request.url ?? '';
      response.writeHead(200).end('{}');
    });
    const proxy = await startCaptureProxy({
      modelId: 'selected-model',
      region: 'us-east-1',
      credentials,
      upstreamBaseUrl: fake.origin,
    });
    try {
      await expect(postRequestTarget(proxy.baseUrl, requestTarget)).resolves.toBe(200);
      expect(receivedPath).toBe('/model/selected-model/invoke');
    } finally {
      await proxy.close();
      await fake.close();
    }
  });

  it.each([
    '//evil.example.com/model/selected-model/invoke',
    'http://evil.example.com/model/selected-model/invoke',
  ])('rejects provider target %s when it resolves off the configured origin', path => {
    const configuredUpstream = new URL('https://bedrock-runtime.us-east-1.amazonaws.com');

    expect(() => resolveProviderUrl(path, configuredUpstream)).toThrow(
      'captured request path does not resolve to the Bedrock upstream',
    );
  });

  it('does not forward a body whose digest differs from the manifest', async () => {
    let upstreamCalls = 0;
    const onRequestRejected = jest.fn();
    const fake = await upstream((_request, response) => {
      upstreamCalls++;
      response.writeHead(500).end();
    });
    const proxy = await startCaptureProxy({
      modelId: 'model:1',
      region: 'us-east-1',
      credentials,
      expectedRequestShapeSha256: '0'.repeat(64),
      upstreamBaseUrl: fake.origin,
      onRequestRejected,
    });
    try {
      const response = await fetch(`${proxy.baseUrl}/model/model%3A1/invoke`, {
        method: 'POST',
        body: '{"actual":true}',
      });
      expect(response.status).toBe(409);
      expect((await proxy.captured()).forwarded).toBe(false);
      expect(upstreamCalls).toBe(0);
      expect(onRequestRejected).toHaveBeenCalledTimes(1);
    } finally {
      await proxy.close();
      await fake.close();
    }
  });

  it('rejects a different model before reading or forwarding it', async () => {
    const fake = await upstream((_request, response) => response.writeHead(500).end());
    const proxy = await startCaptureProxy({
      modelId: 'selected-model', region: 'us-east-1', credentials, upstreamBaseUrl: fake.origin,
    });
    try {
      const response = await fetch(`${proxy.baseUrl}/model/other-model/invoke`, { method: 'POST', body: '{}' });
      expect(response.status).toBe(403);
      const queryResponse = await fetch(`${proxy.baseUrl}/model/selected-model/invoke?variant=other`, {
        method: 'POST', body: '{}',
      });
      expect(queryResponse.status).toBe(403);
      expect(proxy.snapshot()).toBeNull();
    } finally {
      await proxy.close();
      await fake.close();
    }
  });
});
