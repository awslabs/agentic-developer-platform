import assert from "node:assert/strict";
import test from "node:test";
import {
  formatFixesPushedComment,
  formatIssueReviewComment,
  formatReviewComment,
  GitHubClient,
} from "./github.js";

test("merge and queue mutations carry the exact reviewed head", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const requests: Array<{ url: string; body: any }> = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url: String(url), body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify(String(url).endsWith("/graphql")
      ? { data: { enqueuePullRequest: { mergeQueueEntry: { id: "entry" } } } }
      : { merged: true, sha: "c".repeat(40) }));
  };
  const github = new GitHubClient("org/repo", async () => "test-token");
  await github.merge(7, "a".repeat(40), "rebase");
  await github.enqueue("PR_7", "a".repeat(40), "operation");
  assert.deepEqual(requests[0]?.body, { sha: "a".repeat(40), merge_method: "rebase" });
  assert.deepEqual(requests[1]?.body.variables.input,
    { pullRequestId: "PR_7", expectedHeadOid: "a".repeat(40), clientMutationId: "operation" });
});

test("review comments identify the exact reviewed head and engine", () => {
  const body = formatReviewComment(
    { verdict: "approve", summary: "Verified.", findings: [], validationGaps: [] },
    "a".repeat(40),
    "Codex SDK 0.155.1",
  );
  assert.match(body, /Reviewed head/);
  assert.match(body, /Codex SDK 0\.155\.1/);
  assert.match(body, /Blockers:\*\* 0/);
});

test("a repair push identifies the repaired tree as reviewed and approved", () => {
  const body = formatFixesPushedComment(
    { verdict: "approve", summary: "Local repair review passed.", findings: [], validationGaps: [] },
    "b".repeat(40),
    "Codex SDK 0.155.1",
  );
  assert.match(body, /FIXES PUSHED AND APPROVED/);
  assert.doesNotMatch(body, /— APPROVE/);
});

test("issue review comments identify the issue without claiming a PR merge", () => {
  const body = formatIssueReviewComment(
    { verdict: "approve", summary: "The issue is ready.", findings: [], validationGaps: [] },
    5499,
    "Codex SDK 0.155.1",
  );
  assert.match(body, /ISSUE READY/);
  assert.match(body, /Reviewed issue:\*\* #5499/);
  assert.doesNotMatch(body, /Reviewed head|merge/i);
});

test("a verdict is posted as a PR comment with the default GitHub identity", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  let payload = "";
  globalThis.fetch = async (input, init) => {
    requested = String(input);
    payload = String(init?.body ?? "");
    return new Response(JSON.stringify({ id: 1 }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    await github.comment(5471, "Reviewed current head.");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.match(requested, /\/repos\/aws-e\/adp\/issues\/5471\/comments$/);
  assert.deepEqual(JSON.parse(payload), { body: "Reviewed current head." });
});

test("an issue verdict marker prevents a duplicate redelivery comment", async () => {
  const originalFetch = globalThis.fetch;
  let posts = 0;
  globalThis.fetch = async (input, init) => {
    if ((init?.method ?? "GET") === "POST") posts += 1;
    return new Response(
      JSON.stringify([{ body: "<!-- agent-codex-reviewer:m-1 -->\nprior verdict" }]),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.equal(
      await github.commentOnce(5499, "<!-- agent-codex-reviewer:m-1 -->", "new verdict"),
      false,
    );
    assert.equal(posts, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("shared live-fleet diagnostics do not block an otherwise ready merge", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    const body = url.endsWith("/check-runs?per_page=100")
      ? {
          check_runs: [
            { name: "Codex Adapter Unit Tests", status: "completed", conclusion: "success" },
            { name: "GitLab Live Fleet", status: "completed", conclusion: "failure" },
          ],
        }
      : { state: "success", statuses: [] };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.deepEqual(await github.checks("a".repeat(40)), {
      ready: true,
      failing: [],
      pending: [],
      total: 1,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("unavailable legacy statuses do not hide accessible check runs", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/status?per_page=100")) {
      return new Response(JSON.stringify({ message: "Resource not accessible by integration" }), {
        status: 403,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify({
        check_runs: [
          { name: "Codex Adapter Unit Tests", status: "completed", conclusion: "success" },
        ],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.deepEqual(await github.checks("a".repeat(40)), {
      ready: true,
      failing: [],
      pending: [],
      total: 1,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

/**
 * Outbound URL safety (issue #5604, work-package S05).
 *
 * Semgrep rated the `fetch` in `request` as SSRF (node_ssrf, result_index
 * 2418). `request` is private and every caller passes a `/`-rooted template,
 * so the escape is not reachable today — these tests pin the invariant that
 * keeps it unreachable, and the origin check on the response.
 */

test("every GitHub operation stays on the api.github.com origin", async () => {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = async (input) => {
    requested.push(String(input));
    const url = String(input);
    const body = url.includes("/check-runs")
      ? { check_runs: [] }
      : url.includes("/status")
        ? { state: "success", statuses: [] }
        : url.includes("/merge")
          ? { merged: true, message: "ok", sha: "c".repeat(40) }
          : url.includes("/comments")
            ? []
            : { number: 1, title: "t", body: null };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    // A repository name is the only caller-supplied part of these paths.
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    await github.getPullRequest(5471);
    await github.getIssue(5499);
    await github.comment(5471, "body");
    await github.commentOnce(5499, "<!-- marker -->", "body");
    await github.checks("a".repeat(40));
    await github.merge(5471, "b".repeat(40));
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(requested.length >= 6, "expected every operation to issue a request");
  for (const url of requested) {
    assert.equal(
      new URL(url).origin,
      "https://api.github.com",
      `request escaped the GitHub origin: ${url}`,
    );
  }
});

test("a request path that is not rooted at / is refused before any fetch", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  };
  // Each of these would otherwise change the origin the token is sent to:
  //   "@evil.com/x"  -> https://evil.com
  //   ".evil.com/x"  -> https://api.github.com.evil.com
  //   ":8080/x"      -> https://api.github.com:8080
  //   "//evil.com/x" -> protocol-relative, https://evil.com
  const hostile = ["@evil.com/x", ".evil.com/x", ":8080/x", "//evil.com/x", "repos/a/b"];
  try {
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const request = (github as any).request.bind(github);
    for (const path of hostile) {
      await assert.rejects(
        () => request(path),
        /must start with "\/"/,
        `expected ${path} to be refused`,
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(calls, 0, "no hostile path may reach fetch");
});

test("a response redirected off api.github.com is refused", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    // Node follows the redirect and strips Authorization, so the token is safe —
    // but this body is the attacker's, and checks() would treat it as the gate.
    new Response(JSON.stringify({ check_runs: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  try {
    const response = new Response("{}", { status: 200 });
    Object.defineProperty(response, "url", { value: "https://evil.example/repos/aws-e/adp" });
    globalThis.fetch = async () => response;
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    await assert.rejects(() => github.getPullRequest(5471), /redirected off api\.github\.com/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a same-origin redirect, as GitHub issues for renamed repositories, is accepted", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => {
      const response = new Response(JSON.stringify({ number: 5471, title: "t", body: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
      Object.defineProperty(response, "url", {
        value: "https://api.github.com/repos/aws-e/adp-renamed/pulls/5471",
      });
      return response;
    };
    const github = new GitHubClient("aws-e/adp", async () => "default-token");
    assert.equal((await github.getPullRequest(5471)).number, 5471);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("authentication rejection refreshes reads once and stops on repeated rejection", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const forced: boolean[] = [];
  let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response("Bad credentials", { status: 401 }); };
  const github = new GitHubClient("org/repo", async force => { forced.push(force === true); return "token"; });
  await assert.rejects(github.getPullRequest(7), /401/);
  assert.equal(calls, 2);
  assert.deepEqual(forced, [false, true]);
});

test("fresh credentials recover explicit rejection without replaying uncertain mutations", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  let calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++;
    if (init?.method === "POST" || (init?.headers as Record<string, string>).authorization === "Bearer old") {
      if (init?.method === "POST") throw new Error("connection reset after write");
      return new Response("Bad credentials", { status: 401 });
    }
    return Response.json({ number: 7 });
  };
  const github = new GitHubClient("org/repo", async force => force ? "fresh" : "old");
  assert.equal((await github.getPullRequest(7)).number, 7);
  assert.equal(calls, 2);
  await assert.rejects(github.comment(7, "review"), /connection reset/);
  assert.equal(calls, 3);
});


test("explicitly unauthorized mutation can refresh once without changing its payload", async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const bodies: unknown[] = [];
  globalThis.fetch = async (_url, init) => {
    bodies.push(init?.body);
    return bodies.length === 1 ? new Response("Bad credentials", { status: 401 }) : Response.json({ merged: true });
  };
  const forced: boolean[] = [];
  const github = new GitHubClient("org/repo", async force => { forced.push(force === true); return "token"; });
  await github.merge(7, "a".repeat(40), "rebase");
  assert.deepEqual(forced, [false, true]);
  assert.equal(bodies[0], bodies[1]);
});

test('retry context reads recent persisted checklists beyond the first comment page', async t => {
  const original = globalThis.fetch;
  t.after(() => { globalThis.fetch = original; });
  const urls: string[] = [];
  globalThis.fetch = async url => {
    urls.push(String(url));
    return new Response(JSON.stringify(String(url).endsWith('page=2') ? [
      { body: 'Earlier run\n### Task checklist\n\n- ☑ Implement history\n- ☐ Verify integration\n### Agent explanation\nDo not include this.' },
    ] : [{ body: 'Unrelated comment' }]));
  };
  const github = new GitHubClient('org/repo', async () => 'token');
  assert.deepEqual(await github.taskChecklists(7, 201), [
    '### Task checklist\n\n- ☑ Implement history\n- ☐ Verify integration',
  ]);
  assert.equal(urls.length, 2);
  assert.ok(urls[0]!.endsWith('page=2'));
  assert.ok(urls[1]!.endsWith('page=3'));
});

for (const repository of [
  "owner/../other/repo", "owner/.", "owner/..", "owner/repo?x=1",
  "owner/repo#fragment", "//attacker.invalid/repo", "owner/@attacker.invalid",
]) {
  test(`malformed repository ${repository} cannot acquire or send an installation token`, async t => {
    const original = globalThis.fetch;
    t.after(() => { globalThis.fetch = original; });
    let tokenRequests = 0;
    let fetchRequests = 0;
    globalThis.fetch = async () => {
      fetchRequests += 1;
      return new Response(JSON.stringify({ number: 1 }));
    };
    const github = new GitHubClient(repository, async () => {
      tokenRequests += 1;
      return "synthetic-fixture-token";
    });
    await assert.rejects(() => github.getPullRequest(1), /owner\/name pair/);
    assert.equal(tokenRequests, 0);
    assert.equal(fetchRequests, 0);
  });
}
