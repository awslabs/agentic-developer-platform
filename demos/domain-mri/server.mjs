import http from "node:http";
import { readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { randomUUID, createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { isIP } from "node:net";
const exec = promisify(execFile),
  root = fileURLToPath(new URL(".", import.meta.url));
const terminal = new Set(["completed", "failed", "cancelled", "expired"]);
export function normalizeDomain(value) {
  if (typeof value !== "string" || value.length > 253)
    throw Error("Enter a public domain, such as example.com.");
  let u;
  try {
    u = new URL(value.includes("://") ? value : `https://${value.trim()}`);
  } catch {
    throw Error("Enter a valid domain.");
  }
  if (
    !["https:", "http:"].includes(u.protocol) ||
    u.username ||
    u.password ||
    u.port ||
    u.pathname !== "/" ||
    u.search ||
    u.hash ||
    isIP(u.hostname) ||
    !u.hostname.includes(".") ||
    !/^([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}$/i.test(u.hostname) ||
    /\.(local|internal|localhost)$/i.test(u.hostname)
  )
    throw Error("Enter a public domain only, without a path or credentials.");
  return u.hostname.toLowerCase();
}
export function taskRequest(domain) {
  return {
    schema_version: "1.0",
    persona: "agent-task-cyber",
    instructions: `Assess https://${domain} using the URL-analysis skill, Common Crawl and isolated live browsing. Determine what the site serves and whether observations indicate malicious activity, deliberate security testing, or uncertainty. Read one relevant archived capture if available. Open a host-scoped browser, capture and inspect a screenshot, inspect network requests, and close the browser. Do not execute downloaded files or activate exploit/test links. Batch independent operations and finish within eight model turns. Submit a concise evidence-grounded report with at most four findings, an explicit Verdict and confidence, historical versus live observations, and clear coverage limitations. Qualify conclusions when capture is incomplete; do not infer absence of activity from missing evidence. Cite exact Task evidence references. The host creates the offline HTML report and recorded action timeline; do not invent steps or write HTML. Omit top-level evidence_refs metadata so the host derives it. Do not ask for input.`,
    inputs: { url: `https://${domain}`, browser_scope: "host" },
    acceptance_criteria: [
      "Assess the domain using actual tool evidence; separate observations from uncertainty.",
      "Capture screenshot/network evidence and close the browser session.",
      "Publish a downloadable offline HTML report with citations and limitations.",
    ],
    external_reference: "domain-mri-demo",
  };
}
export async function createDemo({
  stateFile = process.env.MRI_STATE_FILE ||
    "/tmp/adp-domain-mri-demo-state.json",
  fetcher = fetch,
} = {}) {
  const api =
    process.env.MRI_TASK_API_URL ||
    "https://59o2rakc50.execute-api.us-east-1.amazonaws.com/dev";
  const tokenURL =
    process.env.MRI_TOKEN_URL ||
    "https://bedrockgw-dev-auth-18057152.auth.us-east-1.amazoncognito.com/oauth2/token";
  const clientId = process.env.MRI_CLIENT_ID || "2iko53d963nkr0lrh0cm3prrum";
  let runs = {};
  try {
    runs = JSON.parse(await readFile(stateFile, "utf8"));
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  let token,
    secret = process.env.MRI_CLIENT_SECRET,
    submitting = false;
  let saving = Promise.resolve();
  const save = () => {
    const content = JSON.stringify(runs);
    saving = saving
      .catch(() => {})
      .then(() => writeFile(stateFile, content, { mode: 0o600 }));
    return saving;
  };
  async function accessToken() {
    if (token && token.until > Date.now() + 60000) return token.value;
    if (!secret) {
      const { stdout } = await exec(
        "aws",
        [
          "cognito-idp",
          "describe-user-pool-client",
          "--region",
          process.env.AWS_REGION || "us-east-1",
          "--user-pool-id",
          process.env.MRI_USER_POOL_ID || "us-east-1_JEhv9xSGG",
          "--client-id",
          clientId,
          "--output",
          "json",
        ],
        { maxBuffer: 1024 * 1024 },
      );
      secret = JSON.parse(stdout).UserPoolClient.ClientSecret;
    }
    const r = await fetcher(tokenURL, {
      method: "POST",
      headers: {
        Authorization:
          "Basic " + Buffer.from(`${clientId}:${secret}`).toString("base64"),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({
        grant_type: "client_credentials",
        scope:
          "adp-tasks/submit adp-tasks/read adp-tasks/cancel adp-tasks/artifacts",
      }),
      signal: AbortSignal.timeout(30000),
    });
    if (!r.ok)
      throw Error(
        "Service identity could not authenticate. Check the demo server configuration.",
      );
    const b = await r.json();
    token = { value: b.access_token, until: Date.now() + b.expires_in * 1000 };
    return token.value;
  }
  async function upstream(path, options = {}) {
    return fetcher(api + path, {
      ...options,
      headers: {
        ...options.headers,
        Authorization: "Bearer " + (await accessToken()),
      },
      signal: options.signal || AbortSignal.timeout(30000),
    });
  }
  const sample = JSON.parse(
    await readFile(root + "fixtures/replay.json", "utf8"),
  );
  function replaySnapshot(run) {
    const done = Date.now() - run.started >= sample.events.length * 1800;
    return {
      task_id: sample.snapshot.task_id,
      status: done ? "completed" : "running",
      result: done ? sample.snapshot.result : null,
      completed_at: done
        ? new Date(run.started + sample.events.length * 1800).toISOString()
        : null,
      replay: true,
    };
  }
  async function snapshot(run) {
    if (run.replay) return replaySnapshot(run);
    if (!run.task_id) return { status: "submission_unknown" };
    const r = await upstream("/v1/tasks/" + run.task_id);
    if (!r.ok)
      throw Error("Task status temporarily unavailable. Reconnecting…");
    const s = await r.json();
    run.status = s.status;
    run.snapshot = s;
    await save();
    return s;
  }
  const json = (res, status, body) => {
    res.writeHead(status, {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    });
    res.end(JSON.stringify(body));
  };
  const server = http.createServer(async (req, res) => {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "no-referrer");
    try {
      const u = new URL(req.url, "http://localhost");
      if (
        req.headers.host &&
        !/^(localhost|127\.0\.0\.1)(:\d+)?$/.test(req.headers.host)
      )
        return json(res, 403, { error: "Use localhost to open this demo." });
      if (
        req.method === "POST" &&
        req.headers.origin &&
        req.headers.origin !== `http://${req.headers.host}`
      )
        return json(res, 403, {
          error: "Use the demo page to submit investigations.",
        });
      if (req.method === "GET" && u.pathname === "/api/config")
        return json(res, 200, {
          model: "Opus 5",
          identity: "sophos-labs-hierarchy-opus5-check",
        });
      if (req.method === "POST" && u.pathname === "/api/runs") {
        let raw = "";
        for await (const c of req) {
          raw += c;
          if (raw.length > 2048)
            return json(res, 413, { error: "Submission too large." });
        }
        const body = JSON.parse(raw);
        if (!/^[a-f0-9-]{36}$/.test(body.request_id || ""))
          return json(res, 400, { error: "Missing submission identifier." });
        const domain = normalizeDomain(body.domain),
          id = body.request_id;
        if (body.replay && domain !== "malware.wicar.org")
          return json(res, 400, {
            error: "The recorded demo is for malware.wicar.org.",
          });
        if (submitting)
          return json(res, 409, {
            error: "A submission is already in progress.",
          });
        if (
          runs[id] &&
          (runs[id].domain !== domain || runs[id].replay !== !!body.replay)
        )
          return json(res, 409, {
            error:
              "This submission identifier belongs to another investigation.",
          });
        if (runs[id]?.task_id || runs[id]?.replay)
          return json(res, 200, { id, domain, replay: runs[id].replay });
        const other = Object.entries(runs).find(
          ([key, r]) => key !== id && !r.replay && !terminal.has(r.status),
        );
        if (!body.replay && other)
          return json(res, 409, {
            error:
              "An investigation is already active. Resume it before starting another.",
            id: other[0],
          });
        submitting = true;
        try {
          const run = runs[id] || {
            domain,
            replay: !!body.replay,
            started: Date.now(),
            status: "submission_unknown",
          };
          runs[id] = run;
          await save();
          if (run.replay) {
            if (domain !== "malware.wicar.org")
              throw Error("The recorded demo is for malware.wicar.org.");
            return json(res, 201, { id, domain, replay: true });
          }
          const r = await upstream("/v1/tasks", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": "mri-demo-" + id,
            },
            body: JSON.stringify(taskRequest(domain)),
          });
          if (!r.ok) {
            if (r.status >= 400 && r.status < 500) {
              run.status = "failed";
              await save();
            }
            return json(res, r.status >= 500 ? 502 : 400, {
              error:
                r.status >= 500
                  ? "Submission could not be confirmed. Retry safely with the same submission."
                  : "The Task API refused this submission. Check identity enrollment, model readiness and Task policy.",
            });
          }
          const b = await r.json();
          run.task_id = b.task_id;
          run.status = b.status;
          await save();
          return json(res, 201, { id, domain, replay: false });
        } finally {
          submitting = false;
        }
      }
      const m = u.pathname.match(
        /^\/api\/runs\/([a-f0-9-]{36})(?:\/(events|report|cancel))?$/,
      );
      if (m) {
        const run = runs[m[1]];
        if (!run)
          return json(res, 404, {
            error: "Investigation not found on this demo server.",
          });
        if (!m[2] && req.method === "GET")
          return json(res, 200, {
            ...(await snapshot(run)),
            domain: run.domain,
            replay: run.replay,
            started: run.started,
          });
        if (m[2] === "events" && req.method === "GET") {
          res.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
          });
          res.write(": connected\n\n");
          if (run.replay) {
            let cursor = Number(req.headers["last-event-id"] || 0);
            const timer = setInterval(() => {
              const available = Math.min(
                sample.events.length,
                Math.floor((Date.now() - run.started) / 1800),
              );
              while (cursor < available) {
                const event = sample.events[cursor++];
                res.write(
                  `id: ${cursor}\nevent: event\ndata: ${JSON.stringify(event)}\n\n`,
                );
              }
              if (cursor === sample.events.length) {
                clearInterval(timer);
                res.end();
              }
            }, 300);
            res.on("close", () => clearInterval(timer));
            return;
          }
          if (!run.task_id) {
            res.end();
            return;
          }
          const abort = new AbortController();
          res.on("close", () => abort.abort());
          try {
            const r = await upstream("/v1/tasks/" + run.task_id + "/events", {
              headers: req.headers["last-event-id"]
                ? { "Last-Event-ID": req.headers["last-event-id"] }
                : {},
              signal: abort.signal,
            });
            if (!r.ok) {
              res.write("event: reconnect\ndata: {}\n\n");
              res.end();
              return;
            }
            for await (const chunk of r.body) {
              if (res.destroyed) break;
              res.write(chunk);
            }
            res.end();
          } catch {
            if (!res.destroyed) res.end();
          }
          return;
        }
        if (m[2] === "report" && req.method === "GET") {
          const s = await snapshot(run);
          if (s.status !== "completed")
            return json(res, 409, {
              error: "The report is still being prepared.",
            });
          let html;
          if (run.replay) html = await readFile(root + "fixtures/report.html");
          else {
            for (const aid of s.result.artifact_ids) {
              const r = await upstream(
                `/v1/tasks/${run.task_id}/artifacts/${aid}`,
              );
              if (!r.ok)
                throw Error("Report download temporarily unavailable.");
              if (!r.headers.get("content-type")?.startsWith("text/html")) {
                await r.body.cancel();
                continue;
              }
              html = Buffer.from(await r.arrayBuffer());
              if (
                html.length > 16 * 1024 * 1024 ||
                createHash("sha256").update(html).digest("hex") !==
                  r.headers.get("x-adp-content-sha256")
              )
                throw Error("Report integrity check failed.");
              break;
            }
          }
          if (!html) throw Error("No HTML report was returned.");
          res.writeHead(200, {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy":
              "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
            "Content-Disposition": `${u.searchParams.has("download") ? "attachment" : "inline"}; filename="domain-mri-${run.domain}.html"`,
          });
          res.end(html);
          return;
        }
        if (m[2] === "cancel" && req.method === "POST") {
          if (run.replay) {
            run.started = 0;
            return json(res, 200, { ok: true });
          }
          if (!run.task_id)
            return json(res, 409, {
              error: "Retry the submission first to confirm its Task ID.",
            });
          const r = await upstream(`/v1/tasks/${run.task_id}/cancel`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              schema_version: "1.0",
              command_id: randomUUID(),
              reason: "Stopped from Domain MRI demo",
            }),
          });
          return json(res, r.ok ? 202 : 502, { ok: r.ok });
        }
      }
      const files = {
        "/": "index.html",
        "/app.js": "app.js",
        "/styles.css": "styles.css",
      };
      if (req.method === "GET" && files[u.pathname]) {
        const name = files[u.pathname];
        res.writeHead(200, {
          "Content-Type": name.endsWith(".js")
            ? "text/javascript"
            : name.endsWith(".css")
              ? "text/css"
              : "text/html",
          "Cache-Control": "no-cache",
        });
        res.end(await readFile(root + "public/" + name));
        return;
      }
      json(res, 404, { error: "Not found" });
    } catch (e) {
      if (res.headersSent) {
        res.end();
        return;
      }
      json(res, e.message.startsWith("Enter ") ? 400 : 502, {
        error:
          /^(Enter |Service identity|Task status|Report |No HTML|The recorded)/.test(
            e.message,
          )
            ? e.message
            : "The demo server could not reach the Task API. Check its configuration, then retry.",
      });
    }
  });
  return server;
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const server = await createDemo();
  server.listen(Number(process.env.PORT || 4318), "127.0.0.1", () =>
    console.log(`Domain MRI: http://localhost:${process.env.PORT || 4318}`),
  );
}
