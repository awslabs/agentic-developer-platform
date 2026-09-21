import { parseEnvelope } from "./contracts.js";
import { runReview } from "./reviewer.js";
import { runEngineReview } from "./engine-review.js";

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

async function main(): Promise<void> {
  if (!process.argv.includes("--embedded")) {
    throw new Error("agent-codex-reviewer runs only through the shared worker entrypoint");
  }
  const envelope = parseEnvelope(await readStdin());
  const githubToken = process.env.GH_TOKEN ?? process.env.GITHUB_TOKEN ?? "";
  if (!githubToken) throw new Error("embedded Codex review requires the worker GitHub token");
  const proxyPort = process.env.SIGV4_PROXY_PORT ?? "9090";
  const runtime = {
    workspace: process.cwd(),
    githubToken,
    proxyBaseUrl: `http://127.0.0.1:${proxyPort}/openai/v1`,
  };
  const result = envelope.kind === "codex_engine_review"
    ? await runEngineReview(envelope, runtime)
    : await runReview(envelope, runtime);
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  main().catch((error) => {
    console.error("agent-codex-reviewer failed", error);
    process.exitCode = 1;
  });
}
