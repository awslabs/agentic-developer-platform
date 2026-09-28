/**
 * Mediated GitHub operations — the agent-facing instructions (issue #5223).
 *
 * When a run's accepted policy keeps `merge` as a human decision, the worker is
 * given no GitHub token at all: `entrypoint.py` withholds every token variable and
 * removes the on-disk token file, and GitHub writes go through the gateway's typed
 * operations instead (`lib/mediated_github.py`). See
 * `docs/security/mediated-github-operations.md` for the operation contract.
 *
 * Why this text has to exist. Withholding the token makes the *unsafe* path
 * impossible, not the *safe* path discoverable. An agent that has been told, by its
 * persona and by this prompt's own "Branch naming" and checkpoint sections, to
 * `git push` and `gh pr create` will find those commands failing to authenticate and
 * has no way to guess that a mediated helper exists. The predictable failure modes
 * are the bad ones: burning the run retrying `git push`, or "fixing" the auth error
 * by hunting for a credential — which is the exfiltration attempt the withholding
 * exists to prevent. So the instructions must name the replacement.
 *
 * Feature-flagged on the same variable the worker keys off, so a run without
 * mediation sees no reference to a path it cannot use. Default off.
 */

/**
 * Whether this run publishes GitHub writes through mediation.
 *
 * Parsed to match `entrypoint._mediated_github_enabled` exactly. If these two ever
 * disagree, the agent is told to use a path that is not active (or holds a token
 * while being told it has none) — both of which are worse than either state alone.
 */
export const MEDIATED_GITHUB_ENABLED = ['1', 'true', 'yes'].includes(
  (process.env.ADP_MEDIATED_GITHUB_ENABLED ?? '').toLowerCase(),
);

/**
 * Whether mediation is on, evaluated NOW rather than at module load.
 *
 * `MEDIATED_GITHUB_ENABLED` is a load-time constant because the prompt is built
 * once. The refresh guards cannot use it: they run minutes into the process and
 * must observe the environment the run is actually in.
 *
 * Reads `ADP_TOKEN_MODE` as well as the feature variable, because
 * `entrypoint._withhold_write_token` sets *both* (`ADP_TOKEN_MODE="mediated"`,
 * `ADP_MEDIATED_GITHUB_ENABLED="true"`). Either alone is sufficient evidence that
 * a token was deliberately withheld from this process, and treating them as
 * independent means a future change to one does not silently reopen the refresh
 * path.
 *
 * @param env Environment to inspect (injectable for tests).
 * @returns true when no GitHub token may be minted or restored for this run.
 */
export function isMediatedRun(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.ADP_TOKEN_MODE === 'mediated') return true;
  return ['1', 'true', 'yes'].includes((env.ADP_MEDIATED_GITHUB_ENABLED ?? '').toLowerCase());
}

/**
 * Instructions for the agent, injected only when mediation is on.
 *
 * Run-invariant: it interpolates nothing, so it stays inside the prompt's stable
 * prefix (#4183) and cannot break prompt-cache reuse.
 *
 * Deliberately states the refusals as well as the calls. An agent that knows *why*
 * merge is absent does not spend the run trying to route around it, and one that
 * knows a refusal is authoritative does not retry it as if it were a transient error.
 */
export const MEDIATED_GITHUB_PROMPT = `
<mediated-github>
## Publishing your work (this run has NO GitHub token)

This run's accepted policy keeps merge as a human decision. GitHub cannot express
"may push a branch, may not merge" — the \`contents: write\` permission grants both —
so instead of a narrower token you have been given **no token**. \`git push\`,
\`gh pr create\`, \`gh pr merge\` and any other authenticated \`git\`/\`gh\` network call
**will fail to authenticate**, by design. That is not a misconfiguration, and there is
no credential anywhere in this environment to find. Do not look for one, do not try to
mint one, and do not report the auth failure as a bug.

Publish through the gateway instead. It performs each action for you, re-checking your
authorization immediately before every call:

\`\`\`bash
python3 -c "
from lib import mediated_github as mg
c = mg.publish_commit(repo='.', message='Your commit message')
print(c.sha, c.branch)
pr = mg.upsert_pull_request(title='Your PR title', body='Your PR description')
print(pr.get('html_url'))
"
\`\`\`

- \`publish_commit(repo='.', message=...)\` — commits **everything in your work tree**
  to your assigned branch. It reads changes the way git recorded them, so binary
  files, deletions and file-mode changes are preserved. You do not stage first, and
  you do not pass a branch: the gateway derives it from protected records.
- \`upsert_pull_request(title=..., body=...)\` — opens your PR, or updates it if it
  already exists. Head and base are not yours to choose.
- \`publish_review(pull_number=..., body=..., event='COMMENT')\` — posts a review.
- \`read_repository()\` — reads the assigned repository without a token.

Local git still works normally, and you should keep using it: \`git add\`,
\`git commit\`, \`git diff\`, \`git log\`, \`git status\` are all unaffected. Only
operations that talk to GitHub over the network go through the helper.
The helper includes unpublished local commits and remembers its last confirmed
publication between calls, so subsequent checkpoints publish only newer changes.

### Handling its errors

- **\`MediatedConflict\`** — the branch moved underneath you. Reconcile against the
  new head, then publish again. Do not retry unchanged.
- **\`MediatedUnavailable\`** — transient. The gateway has already checked whether
  your commit landed before reporting this, so retrying is safe and will not
  double-publish.
- **\`MediatedRefused\`** — authorization refused, and retrying the same request will
  not help. Two common causes are worth recognising: your grant expired mid-run, or
  your change includes content that could itself perform a gated action (a workflow
  definition, for instance), which needs separately accepted authority. Report it as
  a blocker with the message; do not attempt to work around it.

**Merge is not available to you** — not through the helper, not through \`gh\`, not by
any route. It is a separate operation that requires an authority this run does not
have, and that is the human gate your work is meant to preserve. Open the PR, report
it, and leave merging to the human who owns that decision.
</mediated-github>`;
