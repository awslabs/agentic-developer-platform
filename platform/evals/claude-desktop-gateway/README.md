# Claude Desktop-shaped gateway evaluation

This evaluation sends a two-turn, Anthropic Messages-shaped tool conversation
through the deployed **dev** gateway. It checks more than HTTP status: the
streamed `tool_use` delta must reconstruct to `add(2, 3)`, the same tool ID must
appear in `tool_result`, and the final streamed and non-streamed replies must
contain `5`. It also checks model denial, token-count mode, CLI refresh, usage
attribution, and pod cleanup. It does **not** drive the Claude Desktop GUI or
prove that a Desktop build supports custom gateway settings; that remains a
separate manual acceptance check.

The ARC runner holds the protected AWS role, database access, and the existing
smoke-user refresh token. A short-lived stock pod acts as the client. The pod
has no service-account token or inherited runner environment, and runs the
shared contamination gate **before** installing CLI tools. The smoke token
reaches `adp import` on stdin from a private runner file, never on an exec
command line. Only request IDs and verified attribution, not tokens or prompt
bodies, enter the run summary.

## Before running

- Merge and deploy the gateway and CLI prerequisites; confirm the protected
  `adp-deploy-dev` environment supplies `ADP_DEPLOY_ROLE_ARN`,
  `ADP_SMOKE_REFRESH_TOKEN_ARN`, and `ADP_DB_USER`. The smoke-token ARN must
  name the existing dev gateway secret. An unavailable or expired secret is a
  **failure**, not a passing skip; the operator renews it through the existing
  `platform/scripts/capture-smoke-token.sh` procedure.
- Confirm the already-approved ARC runner has access to the existing eval pod
  namespace and can read the usage database and invoke direct CountTokens.
  This workflow does not create namespace, IAM, or validation infrastructure.
  The running account is checked against the existing SSM dev platform account
  parameter before gateway requests and standalone cleanup.
- The operations executor must pin the reviewed source and workflow-definition
  revisions at the readiness gate. The context artifact uses the existing
  `cli-evaluation` / `dev` target convention; the accepted operations target
  must match it. A mismatch blocks engine completion rather than being silently
  treated as acceptance.

## Run and inspect

On a pull request, `.github/workflows/eval-claude-desktop-gateway.yml` runs
actionlint, ShellCheck, and the local dry-run suite **only**. It has no AWS
credential step. The suite needs Bash, Python 3, jq, yq v4, Node.js 20+ and AWS
CLI v2. Its live-command regressions use synthetic tokens and an unsigned
loopback HTTP server to verify AWS CLI request serialization; they need no
cloud credentials. To run the same offline checks in a repo checkout:

```bash
actionlint .github/workflows/eval-claude-desktop-gateway.yml
shellcheck -x platform/evals/claude-desktop-gateway/run-eval.sh platform/evals/claude-desktop-gateway/tests/test-run-eval-dry-run.sh
bash platform/evals/claude-desktop-gateway/tests/test-run-eval-dry-run.sh
```

The checked-in fixtures are synthetic Anthropic-shaped events. A clean dry run
checks parsing and ordering, but explicitly **SKIPs** cloud identity, pod
creation, deployed CLI, and live gateway access with reasons. A missing or
invalid fixture **FAILS** with `FIXTURE DRIFT`; do not turn that into a pass.
The injected failures `missing-tool-delta`, `wrong-tool-id`, `refresh-401`,
`denied-model`, and `cleanup-leftover` each yield one layer-named FAIL and a
nonzero exit. For example:

```bash
bash platform/evals/claude-desktop-gateway/run-eval.sh --environment dev --dry-run --inject-failure wrong-tool-id
```

The engine dispatches the workflow with exactly three inputs: correlation,
source revision, and definition revision. For an **authorized manual dev run**
after the readiness gate, use the reviewed, merged SHA of the main branch for
both revisions. In a checkout with `origin/main` already fetched:

```bash
REV=$(git rev-parse origin/main)
gh workflow run eval-claude-desktop-gateway.yml -r main -f adp_correlation=manual -f adp_source_revision="$REV" -f adp_definition_revision="$REV"
```

The protected live job rejects a mismatched dispatch definition, unreviewed
source, wrong AWS account or region, and an unverified deployed image digest.
It writes the single-file `deployment-context.json` artifact under
`adp-deployment-context-eval-claude-desktop-gateway.yml-<attempt>`, so the
engine can bind the observed run to those revisions and the dev target.
Follow the job summary, not merely its green status: check account, region,
image digest, model, inference request IDs, event checklist, count mode,
smoke-user/org attribution, and cleanup. A live, correlated engine run and
post-run pod check are owned by the separate qualification work; merging this
workflow alone deploys nothing.

| Phase | Live evidence |
|---|---|
| 0 | SSM-held expected account, region, ready gateway pod image digest, valid fixtures |
| 1 | New credential-free pod and first-exec contamination gate |
| 2 | Deployed `adp update`, `claude-desktop setup --check`, token-only helper, Desktop settings untouched |
| 3 | Entitled Desktop model list and synthetic `adp-eval.denied-control` rejected with gateway `403 model_not_allowed` for that model, not a provider-side 403 |
| 4–5 | Streamed and non-streamed two-turn local `add`, stable tool ID, final result and request IDs |
| 6 | Positive count and `X-ADP-Token-Count-Mode`; exact mode equals direct Bedrock CountTokens; estimate explicitly skips that comparison |
| 7 | Pod-only cached access data invalidated, refresh succeeds, refreshed token authorizes model read |
| 8 | Usage rows for **all** inference IDs match the scoped smoke user and org |
| 9 | Eval pod deleted and no pod with its app label remains |

Each run makes four normal inference calls, one denied control call, one gateway
CountTokens call, and at most one direct CountTokens comparison. That remains
below the 24-inference/8-count ceiling; every Messages request caps output at
256 tokens. Workflow concurrency is one live dev run, with a 60-minute job
timeout. Usage rows remain as evidence; no new persistent infrastructure or
usage fixtures are created. There is no nightly schedule yet.

## Recover a failed run

The harness checks cleanup on exit; the workflow also performs an independent
recovery sweep. If an interrupted run leaves an eval pod, use the same approved
dev runner identity and kubeconfig (not an unverified local AWS profile):

```bash
bash platform/evals/claude-desktop-gateway/run-eval.sh --environment dev --cleanup-only
kubectl get pods -n adp-gateway-evals -l app=eval-claude-desktop-gateway
```

`--cleanup-only` verifies the SSM-held account and dev region, sweeps by the
eval label, and exits nonzero if it cannot confirm zero remaining pods. If
account, role, namespace access, or Kubernetes readback fails, stop and have
the platform operator restore that access; do not retry against another
account. Disable workflow dispatch if cleanup remains unreliable. Do not delete
retained usage rows. A green dry run or PR CI run is not deployed acceptance:
the qualification owner must inspect the live summary, token-free logs,
context artifact, and leftover pod count after deployment.
