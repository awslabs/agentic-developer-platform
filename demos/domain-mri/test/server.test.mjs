import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { randomUUID, createHash } from "node:crypto";
import { createDemo, normalizeDomain } from "../server.mjs";
process.env.MRI_CLIENT_SECRET = "test-only-not-a-live-secret";
test("domain input is constrained to public domain names", () => {
  assert.equal(normalizeDomain("https://Example.com"), "example.com");
  for (const value of [
    "localhost",
    "127.0.0.1",
    "https://example.com/path",
    "https://user:secret@example.com",
    "https://a.internal",
    "javascript:alert(1)",
    "https://example.com/?x=1",
  ])
    assert.throws(() => normalizeDomain(value));
});
test("live submission preserves idempotency on uncertain delivery and verifies report bytes", async () => {
  const dir = await mkdtemp(tmpdir() + "/mri-test-");
  let calls = 0,
    badHash = false;
  const html = "<html>Verified report</html>",
    keys = [];
  const server = await createDemo({
    stateFile: dir + "/state.json",
    fetcher: async (url, options) => {
      if (url.includes("/oauth2/token"))
        return Response.json({ access_token: "fixture", expires_in: 3600 });
      assert.equal(options.headers.Authorization, "Bearer fixture");
      if (url.endsWith("/v1/tasks")) {
        keys.push(options.headers["Idempotency-Key"]);
        calls++;
        if (calls === 1) throw Error("Network interrupted after sending");
        return Response.json(
          { task_id: "tsk_fixture", status: "accepted" },
          { status: 202 },
        );
      }
      if (url.endsWith("/artifacts/art_fixture"))
        return new Response(html, {
          headers: {
            "content-type": "text/html",
            "x-adp-content-sha256": badHash
              ? "invalid"
              : createHash("sha256").update(html).digest("hex"),
          },
        });
      return Response.json({
        task_id: "tsk_fixture",
        status: "completed",
        result: { artifact_ids: ["art_fixture"] },
      });
    },
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`,
    id = randomUUID();
  const post = (body) =>
    fetch(base + "/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  try {
    assert.equal(
      (await post({ domain: "example.com", request_id: id })).status,
      502,
    );
    assert.equal(
      (await post({ domain: "example.net", request_id: randomUUID() })).status,
      409,
    );
    assert.equal(
      (await post({ domain: "example.com", request_id: id })).status,
      201,
    );
    assert.equal(keys[0], keys[1]);
    const report = await fetch(base + `/api/runs/${id}/report`);
    assert.equal(report.status, 200);
    assert.equal(await report.text(), html);
    assert.match(report.headers.get("content-security-policy"), /sandbox/);
    badHash = true;
    assert.equal((await fetch(base + `/api/runs/${id}/report`)).status, 502);
    const csrf = await fetch(base + "/api/runs", {
      method: "POST",
      headers: { Origin: "https://untrusted.example" },
      body: "{}",
    });
    assert.equal(csrf.status, 403);
  } finally {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await rm(dir, { recursive: true, force: true });
  }
});

test("remote demo link grants an HttpOnly session and guards every API route", async () => {
  const dir = await mkdtemp(tmpdir() + "/mri-public-test-");
  const key = "a".repeat(64);
  const server = await createDemo({
    stateFile: dir + "/state.json",
    publicOrigin: "https://demo.example.com",
    accessKey: key,
    fetcher: () => {
      throw Error("Replay must not call upstream");
    },
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal((await fetch(base + "/")).status, 403);
    assert.equal((await fetch(base + "/api/config")).status, 403);
    const entry = await fetch(base + "/?demo_key=" + key, {
      redirect: "manual",
    });
    assert.equal(entry.status, 303);
    assert.equal(entry.headers.get("location"), "/");
    assert.match(
      entry.headers.get("set-cookie"),
      /HttpOnly; Secure; SameSite=Lax/,
    );
    const cookie = "mri_demo=" + key;
    assert.equal(
      (await fetch(base + "/", { headers: { Cookie: cookie } })).status,
      200,
    );
    const r = await fetch(base + "/api/runs", {
      method: "POST",
      headers: {
        Cookie: cookie,
        Origin: "https://demo.example.com",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        domain: "malware.wicar.org",
        request_id: randomUUID(),
        replay: true,
      }),
    });
    assert.equal(r.status, 201);
    assert.equal(
      (
        await fetch(base + "/api/runs", {
          method: "POST",
          headers: { Cookie: cookie, Origin: "https://elsewhere.example" },
          body: "{}",
        })
      ).status,
      403,
    );
  } finally {
    server.closeAllConnections();
    await new Promise((r) => server.close(r));
    await rm(dir, { recursive: true, force: true });
  }
});
