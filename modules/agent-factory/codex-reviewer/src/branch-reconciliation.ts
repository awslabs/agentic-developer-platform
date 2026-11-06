import { run } from "./process.js";

/** Standalone counterpart of the hosted worker's common branch preparation. */
export async function prepareStoryBranch(workspace: string, issue: number, baseBranch?: string) {
  const git = async (...args: string[]) => (await run("git", args, { cwd: workspace })).stdout.trim();
  if (await git("status", "--porcelain")) throw new Error("Story reconciliation requires a clean checkout");
  const base = baseBranch ?? (await git("symbolic-ref", "refs/remotes/origin/HEAD")).replace(/^refs\/remotes\/origin\//, "");
  await git("check-ref-format", `refs/heads/${base}`);
  // Adopt the canonical branch across runtimes. Retain older standalone Codex
  // branches when no canonical branch exists, rather than abandoning their work.
  const candidates = [`agent/issue-${issue}`, `agent/codex-issue-${issue}`];
  const existing: string[] = [];
  let branch = candidates[0]!;
  for (const candidate of candidates) {
    const exists = await run("git", ["show-ref", "--verify", "--quiet", `refs/remotes/origin/${candidate}`], { cwd: workspace, allowFailure: true });
    if (exists.exitCode === 0) {
      existing.push(candidate);
    } else if (exists.exitCode !== 1) {
      throw new Error("Cannot determine the existing story branch");
    }
  }
  if (existing.length === 2) {
    const contained = await run("git", ["merge-base", "--is-ancestor", `origin/${existing[1]}`, `origin/${existing[0]}`], { cwd: workspace, allowFailure: true });
    if (contained.exitCode !== 0) throw new Error("Both canonical and legacy story branches contain work; consolidate them before restarting so neither is abandoned");
  }
  if ((existing[0] ?? branch) === base) throw new Error("Story branch must differ from its base branch");
  if (existing[0]) {
    branch = existing[0];
    if (await git("branch", "--show-current") !== branch) {
      await git("switch", "--track", "-c", branch, `origin/${branch}`);
    }
  }
  if (await git("branch", "--show-current") !== branch) await git("switch", "-c", branch, `origin/${base}`);
  if (await git("status", "--porcelain")) throw new Error("Story reconciliation requires a clean checkout");
  const shallow = await git("rev-parse", "--is-shallow-repository");
  await git("fetch", "--no-tags", ...(shallow === "true" ? ["--unshallow"] : []), "origin", `refs/heads/${base}:refs/remotes/origin/${base}`);
  const head = await git("rev-parse", "HEAD"), target = await git("rev-parse", `origin/${base}`);
  const ancestor = await run("git", ["merge-base", "--is-ancestor", target, head], { cwd: workspace, allowFailure: true });
  if (ancestor.exitCode === 0) return { branch, status: "up_to_date", conflicts: [] };
  if (ancestor.exitCode !== 1) throw new Error("Cannot verify story/base ancestry");
  await git("update-ref", `refs/adp/before-base-reconciliation/${head}`, head);
  const identity = await run("git", ["var", "GIT_AUTHOR_IDENT"], { cwd: workspace, allowFailure: true });
  const fallback = identity.exitCode ? ["-c", "user.name=adp-agent[bot]", "-c", "user.email=adp-agent[bot]@users.noreply.github.com"] : [];
  const merged = await run("git", [...fallback, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "merge", "--no-edit", "--no-ff", target], { cwd: workspace, allowFailure: true });
  const conflicts = (await git("diff", "--name-only", "--diff-filter=U", "-z")).split("\0").filter(Boolean);
  if (merged.exitCode && !conflicts.length) throw new Error("Story/base reconciliation failed; original commits retained in the recovery ref");
  return { branch, status: conflicts.length ? "conflict" : "merged", conflicts };
}
