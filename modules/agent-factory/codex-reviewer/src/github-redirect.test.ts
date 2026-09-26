import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import test from "node:test";
import { GitHubClient } from "./github.js";

async function listen(server: Server): Promise<number> {
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("missing fixture port");
  return address.port;
}

for (const status of [301, 302, 303, 307, 308]) {
  test(`HTTP ${status} refuses off-origin redirect before native fetch reaches target`, async t => {
    let targetRequests = 0;
    const target = createServer((_request, response) => {
      targetRequests += 1;
      response.end(JSON.stringify({ number: 1 }));
    });
    const targetPort = await listen(target);
    const source = createServer((_request, response) => {
      response.writeHead(status, { location: `http://127.0.0.1:${targetPort}/forbidden` });
      response.end();
    });
    const sourcePort = await listen(source);
    const nativeFetch = globalThis.fetch;
    t.after(async () => {
      globalThis.fetch = nativeFetch;
      source.closeAllConnections(); target.closeAllConnections();
      await Promise.all([source, target].map(server => new Promise<void>(resolve => server.close(() => resolve()))));
    });
    globalThis.fetch = async (_input, init) => nativeFetch(`http://127.0.0.1:${sourcePort}/fixture`, init);
    const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
    await assert.rejects(() => client.getPullRequest(1));
    assert.equal(targetRequests, 0, "redirect target received a forbidden request");
  });
}

for (const location of ["https://api.github.com/repos/renamed/repo/pulls/1", "/repos/renamed/repo/pulls/1"]) {
  test(`same-origin repository rename follows validated location ${location}`, async t => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    const requests: string[] = [];
    globalThis.fetch = async (input, init) => {
      assert.equal(init?.redirect, "manual");
      requests.push(String(input));
      return requests.length === 1
        ? new Response(null, { status: 301, headers: { location } })
        : new Response(JSON.stringify({ number: 1 }));
    };
    const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
    assert.equal((await client.getPullRequest(1)).number, 1);
    assert.deepEqual(requests, ["https://api.github.com/repos/fixture/repository/pulls/1", "https://api.github.com/repos/renamed/repo/pulls/1"]);
  });
}

test("redirect loops are bounded", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let requests = 0;
  globalThis.fetch = async () => {
    requests += 1;
    return new Response(null, { status: 301, headers: { location: "/loop" } });
  };
  const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
  await assert.rejects(() => client.getPullRequest(1));
  assert.equal(requests, 4);
});

for (const location of ["https://api.github.com.evil.invalid/x", "//evil.invalid/x", "http://api.github.com/x", "https://user:password@api.github.com/x", "https://api.github.com:444/x", "http://[::1]/x"]) {
  test(`untrusted redirect ${location} never gets a second request`, async t => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let requests = 0;
    globalThis.fetch = async () => {
      requests += 1;
      return new Response(null, { status: 307, headers: { location } });
    };
    const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
    await assert.rejects(() => client.getPullRequest(1));
    assert.equal(requests, 1);
  });
}

for (const status of [301, 302, 303, 307, 308]) {
  test(`mutation redirect ${status} preserves exact body only when method is preserved`, async t => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    const requests: RequestInit[] = [];
    globalThis.fetch = async (_url, init) => {
      requests.push(init ?? {});
      return requests.length === 1
        ? new Response(null, { status, headers: { location: "/repos/renamed/repo/pulls/1/merge" } })
        : new Response(JSON.stringify({ merged: true, sha: "fixture-sha" }));
    };
    const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
    if (status === 307 || status === 308) {
      assert.equal(await client.merge(1, "reviewed-head"), "fixture-sha");
      assert.equal(requests.length, 2);
      assert.equal(requests[1]?.method, "PUT");
      assert.equal(requests[1]?.body, JSON.stringify({ sha: "reviewed-head", merge_method: "squash" }));
    } else {
      await assert.rejects(() => client.merge(1, "reviewed-head"), /method-preserving/);
      assert.equal(requests.length, 1);
    }
  });
}

for (const mode of ["missing-location", "network-failure", "aborted"] as const) {
  test(`redirect outage ${mode} cannot produce accepted evidence`, async t => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let requests = 0;
    globalThis.fetch = async () => {
      requests += 1;
      if (mode === "network-failure") throw new TypeError("synthetic connection failure");
      if (mode === "aborted") throw new DOMException("synthetic abort", "AbortError");
      return new Response(null, { status: 301 });
    };
    const client = new GitHubClient("fixture/repository", async () => "synthetic-fixture-token");
    await assert.rejects(() => client.getPullRequest(1));
    assert.equal(requests, 1);
  });
}
