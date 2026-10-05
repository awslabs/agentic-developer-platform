import { DEVELOPMENT_TIMEOUT_MS } from "./timeouts.js";
import { runDeveloper, type DeveloperTask } from "./developer.js";
import { withGitHubTokenRenewal } from "./token-lifecycle.js";

async function main() {
  const embedded = process.argv.includes("--embedded");
  const value = (flag: string) => { const i = process.argv.indexOf(flag); return i >= 0 ? process.argv[i + 1] : undefined; };
  const task: DeveloperTask = {
    persona: process.env.AGENT_TYPE === "agent-codex-architect" ? "architect" : "developer",
    repository: value("--repo") ?? process.env.TARGET_REPO ?? "",
    baseBranch: value("--base"),
    issue: Number(value("--issue") ?? process.env.ISSUE_NUMBER),
    workspace: value("--workspace") ?? process.cwd(),
    model: process.env.ADP_MODEL_RESOLVED ?? (process.env.AGENT_TYPE === "agent-codex-architect"
      ? process.env.CODEX_ARCHITECT_MODEL ?? "openai.gpt-6-astra"
      : process.env.CODEX_DEVELOPER_MODEL ?? "openai.gpt-6-sol"),
    baseUrl: embedded ? `http://127.0.0.1:${process.env.SIGV4_PROXY_PORT ?? "9090"}/openai/v1` : process.env.OPENAI_BASE_URL,
    apiKey: embedded ? "sigv4-proxy-placeholder" : process.env.OPENAI_API_KEY,
    // The developer owns most of the story's model time; the reviewer finishes.
    // Same env family as CODEX_REVIEWER_TURN_TIMEOUT_MS on the ScaledJob.
    timeoutMs: Number(process.env.CODEX_DEVELOPER_TURN_TIMEOUT_MS ??
      (process.env.AGENT_TYPE === "agent-codex-architect" ? 180 * 60 * 1000 : DEVELOPMENT_TIMEOUT_MS)),
    maxTurns: Number(process.env.CODEX_DEVELOPER_MAX_TURNS ?? 24),
  };
  if (!embedded && !value("--workspace")) throw new Error("Standalone execution requires --workspace pointing to a new clone directory");
  // Use the same broker/token-file renewal already used by the Codex reviewer.
  // Mediated credentials require their typed tools; do not silently bypass them.
  if (process.env.ADP_TOKEN_MODE === "mediated") throw new Error("Codex shell developer requires the shared worker's scoped GitHub token mode");
  const result = embedded
    ? await withGitHubTokenRenewal(async () => {
        const moduleUrl = new URL("../../dist/codex-developer-reporting.js", import.meta.url).href;
        const shared = await import(moduleUrl);
        const reporter = await (shared.default ?? shared).createCodexDeveloperReporter({ ...task, persona: `agent-codex-${task.persona}` });
        try {
          const result = await runDeveloper(task, true, reporter);
          await reporter.finish(result);
          return result;
        } catch (error) {
          await reporter.fail(error);
          throw error;
        }
      })
    : await runDeveloper(task);
  console.log(JSON.stringify(result));
}
main().catch(error => { console.error("Codex developer failed:", error); process.exitCode = 1; });
