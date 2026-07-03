# Learnings: Undeploy Re-Orchestration Design (#2651)

**Date**: 2026-07-02  
**Agent**: @agent-architect  
**Task**: Design-only — produce architecture for thin re-orchestration undeploy layer  

## Key Technical Decisions

### Phase Order: Why Webhook-Ingress Before Agent Factory

The instinct is to destroy agent-factory before webhook-ingress (both are "agent execution" modules), but webhook-ingress's KEDA ScaledJob creates pods in `adp-agents` namespace that reference KEDA operator resources. The KEDA operator lives in the `keda` namespace (cleaned by platform-destroy). If agent-factory (which includes ARC) is destroyed first, the `arc-systems` namespace goes away but KEDA resources remain referenced by webhook-ingress ScaledJob finalizers — creating a hang.

Correct order: webhook-ingress → agent-factory → platform.

### CloudFront Dominates Teardown Time

CloudFront distribution disable takes 10-15 minutes (AWS propagation to all edge locations). This is the bottleneck in the entire destroy sequence. The design should print progress indicators during this wait rather than appearing hung.

### Protect-List Gap is Real and Critical

`force-delete-secrets.sh` protects `adp/*/gh-app-*` but NOT `adp/*/github-app/*`. The webhook-ingress module uses the latter path (`adp/dev/github-app/adp-agent-platform-id`, `adp/dev/github-app/adp-agent-platform-key`). If a future operator passes `adp/dev/` as a prefix to the helper, these secrets would be force-deleted. Fix is 2 lines but must ship before the orchestrator.

### deploy-all.sh --destroy Omits Webhook-Ingress Entirely

Confirmed: `grep -c "webhook" platform/scripts/deploy-all.sh` returns 0. This isn't just a destroy-path gap — webhook-ingress is also absent from the deploy path in deploy-all.sh. The script predates the webhook-ingress module. This validates the "thin orchestrator" approach: don't patch deploy-all.sh, build a new purpose-built entry point.

## What Worked

1. **Parallel research agents** — launching 3 Explore agents simultaneously for different aspects (destroy workflows, infrastructure dependencies, helpers) gave comprehensive coverage in one round.
2. **Reading actual workflow YAML** — the workflows have subtle differences (e.g., webhook-ingress uses `load-deploy-config` action while others hardcode env vars) that only show up by reading the source.
3. **Checking secret path conventions across modules** — revealed the `gh-app-*` vs `github-app/*` naming inconsistency that's the root cause of #2629.

## What Didn't Work / Gotchas

1. **Issue body references #2627-#2633 but these issues aren't in the repo as docs** — had to infer their content from the issue description's "Disposition" section and the actual code gaps found.
2. **The `gbrain-infra-destroy.yml` workflow exists** (6th destroy workflow) but isn't mentioned in the issue or CLAUDE.md's teardown section. The design doesn't include it because gbrain is a research experiment module not part of the #2562 deploy inventory, but future agents should be aware it exists.
3. **Reusable workflow calls vs workflow_dispatch** — GitHub Actions `uses: ./.github/workflows/foo.yml` (reusable workflow) requires the called workflow to have `workflow_call` trigger. The existing destroy workflows only have `workflow_dispatch`. The orchestrator workflow will need to either (a) add `workflow_call` triggers to per-module workflows, or (b) use `gh workflow run` + polling. Design recommends option (a) as cleaner.

## Files & Paths Future Agents Need

- Destroy workflows: `.github/workflows/{agent-context,agent-factory,gateway,platform,webhook-ingress}-infra-destroy.yml`
- Shared helpers: `platform/scripts/{empty-s3-buckets,delete-ingress-and-wait,force-delete-secrets,bootstrap-destroy}.sh`
- Deploy config loader: `platform/scripts/load-deploy-config.sh` + `.github/actions/load-deploy-config`
- Webhook-ingress secrets: `modules/agent-factory/webhook-ingress/infra/secrets.tf` (paths: `adp/<env>/github-app/adp-agent-platform-{id,key}`, `adp/<env>/webhook-ingress/github-webhook-secret`)
- State backend config: `environments/dev/backend.tfvars` (platform), `environments/dev/modules/*-backend.tfvars` (modules)
- Force-delete protect-list: `platform/scripts/force-delete-secrets.sh` lines 49-65

## Recommendations for Implementation

1. Ship #2629 protect-list fix as a standalone 2-line PR first — it's a safety fix that benefits all paths.
2. When implementing `undeploy.yml` workflow, add `workflow_call` trigger (with inputs) to each per-module destroy workflow. This is backward-compatible — `workflow_dispatch` still works for manual single-module destroys.
3. The `undeploy-phases.sh` shared functions should be structured identically to how `deploy-all.sh` structures its phases (step function, status reporting, same env var names) — operators familiar with one will recognize the other.
4. Test dry-run mode against a live account before merge — it queries real AWS state (`terraform plan -destroy`, `kubectl get ns`, `aws s3 ls`) so it needs valid credentials even though it doesn't mutate.
