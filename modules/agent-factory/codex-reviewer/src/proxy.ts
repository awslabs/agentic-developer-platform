import { defaultProvider } from "@aws-sdk/credential-provider-node";
import { Hash } from "@smithy/hash-node";
import { SignatureV4 } from "@smithy/signature-v4";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

export interface ProxyHandle {
  close(): Promise<void>;
  baseUrl: string;
}

export function isAllowedProxyRequest(method: string | undefined, rawUrl: string): boolean {
  if (method !== "POST") return false;
  let url: URL;
  try {
    url = new URL(rawUrl, "http://127.0.0.1");
  } catch {
    return false;
  }
  if (url.search || url.hash) return false;
  let path: string;
  try {
    path = decodeURIComponent(url.pathname);
  } catch {
    return false;
  }
  if (path.split("/").some((segment) => segment === "." || segment === "..")) {
    return false;
  }
  return /^\/openai\/v1\/responses(?:\/[A-Za-z0-9._~-]+)*$/.test(path);
}

async function bodyOf(request: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

export async function startGatewayProxy(options: {
  target: string;
  region: string;
  tenantId: string;
  invocationId: string;
  port?: number;
}): Promise<ProxyHandle> {
  const target = new URL(options.target.replace(/\/+$/, ""));
  const signer = new SignatureV4({
    credentials: defaultProvider(),
    region: options.region,
    service: "execute-api",
    sha256: Hash.bind(null, "sha256"),
  });

  const server = createServer(async (request: IncomingMessage, response: ServerResponse) => {
    try {
      if (!isAllowedProxyRequest(request.method, request.url ?? "/")) {
        response.statusCode = 403;
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ error: "gateway proxy route is not allowed" }));
        return;
      }
      const body = await bodyOf(request);
      const path = `${target.pathname.replace(/\/$/, "")}${request.url ?? "/"}`;
      const headers: Record<string, string> = {
        host: target.hostname,
        "content-type": request.headers["content-type"] ?? "application/json",
        "x-agent-orgid": options.tenantId,
        "x-agent-runid": options.invocationId,
      };
      const signed = await signer.sign({
        method: request.method ?? "POST",
        protocol: target.protocol,
        hostname: target.hostname,
        path,
        query: {},
        headers,
        body,
      });
      const upstream = await fetch(`${target.origin}${path}`, {
        method: request.method ?? "POST",
        headers: signed.headers as Record<string, string>,
        body: body.length > 0 ? new Uint8Array(body) : undefined,
        signal: AbortSignal.timeout(12 * 60 * 1000),
      });
      response.statusCode = upstream.status;
      for (const name of ["content-type", "cache-control", "x-request-id"]) {
        const value = upstream.headers.get(name);
        if (value) response.setHeader(name, value);
      }
      if (!upstream.body) {
        response.end();
        return;
      }
      const reader = upstream.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        response.write(Buffer.from(value));
      }
      response.end();
    } catch (error) {
      response.statusCode = 502;
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ error: `gateway proxy failed: ${(error as Error).message}` }));
    }
  });

  const port = options.port ?? 9090;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", () => resolve());
  });
  return {
    baseUrl: `http://127.0.0.1:${port}/openai/v1`,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => (error ? reject(error) : resolve()))),
  };
}
