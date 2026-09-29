import type { ReviewObserver } from "./review-observer.js";
import { selectedModel } from "./reviewer.js";
import { parseEnvelope } from "./contracts.js";
import { runReview } from "./reviewer.js";
import { runEngineReview } from "./engine-review.js";
import { withGitHubTokenRenewal } from "./token-lifecycle.js";

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
  const result = await withGitHubTokenRenewal(async (getGitHubToken, githubToken) => {
    const moduleUrl = new URL("../../dist/codex-developer-reporting.js", import.meta.url).href;
    const shared = await import(moduleUrl);
    const observer: ReviewObserver = await (shared.default ?? shared).createCodexDeveloperReporter({
      repository: envelope.repository, issue: Number(process.env.ISSUE_NUMBER),
      model: selectedModel(), persona: 'agent-codex-reviewer',
    });
    const proxyPort = process.env.SIGV4_PROXY_PORT ?? "9090";
    const runtime = {
      workspace: process.cwd(),
      observer,
      githubToken,
      getGitHubToken,
      proxyBaseUrl: `http://127.0.0.1:${proxyPort}/openai/v1`,
    };
    try {
      const result = envelope.kind === "codex_engine_review"
        ? await runEngineReview(envelope, runtime)
        : await runReview(envelope, runtime);
      await observer.finish({ summary: 'Reviewer finished: ' + result.status +
        ('merged' in result && result.merged ? ' (merged)' : '') });
      return result;
    } catch (error) {
      await observer.fail(error);
      throw error;
    }
  });
  // The delivery adapter consumes the final line, after renewal has stopped.
  console.log(JSON.stringify(result));
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  main().catch((error) => {
    console.error("agent-codex-reviewer failed", error);
    // The worker reads this only on nonzero exit. It is not a review receipt.
    // Renewal/logging can otherwise obscure the cause in a stderr tail.
    console.log(JSON.stringify({ status: "review_failed", error: {
      name: error instanceof Error ? error.name : "Error",
      message: (error instanceof Error ? error.message : String(error)).slice(0, 8192),
    } }));
    process.exitCode = 1;
  });
}
