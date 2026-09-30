# Recover an interrupted deployment

Use `deploy.sh` for fresh installs and upgrades. It runs all requested components
through the same orchestrator; `--skip-agents` selects gateway-only deployment.

After a failed deployment, repeat the original command with `--resume`:

```bash
./deploy.sh --aws-profile customer --region us-east-1 --resume
./deploy.sh --aws-profile customer --region us-east-1 --update --resume
```

Resume is explicit. Without `--resume` or `--from`, a new invocation performs a
full run. Completed phases are skipped only when the account, region,
environment, source revision, tracked source changes, configuration and scope
match the saved run. Change those inputs by starting a new run. A failed or
interrupted phase runs again; later phases run afterward. Preconditions and final
verification still run, and upgrade resumes retain the original preservation
snapshot. Keep the checkout, Terraform working directories and saved upgrade
evidence available until recovery finishes.

To deliberately rerun a completed phase and everything after it:

```bash
./deploy.sh --aws-profile customer --update --from webhook
```

Earlier phases must have completed in the same run. Phase names, in execution
order, are `bootstrap`, `platform`, `gateway-infra`, `gateway`, `gateway-alb`,
`broker`, `admin`, `webhook`, `factory`, `context`, `finalize`, `frontend`, `verify`.
Frontend publication runs near the end despite its historical “Step 6” label.
The gateway phase includes its image build, rollout and pricing finalization;
a failure inside that phase repeats that phase, not earlier infrastructure.

Checkpoints live in the ignored `.adp-deploy-checkpoints/` directory, separately
from the agent-maintained `.adp-deploy-state.json`. They contain status and input
hashes, not shell credential snapshots. The orchestrator requires `flock`
(util-linux) to prevent concurrent runs against the same local checkpoint.
Do not run deployments for the same target concurrently from different checkouts.

For a published release, the installer prints its retained checkout and receipt.
Resume **inside that checkout**, using `./deploy.sh --resume` with the original
`--update`, target and scope options, without `--release`. The selected release
must contain checkpoint support. Repeating `--release` creates a new checkout.

## Explicit recovery approvals

If an upgrade's reviewed Terraform plan intentionally deletes or replaces
resources, authorize it through the public entry point:

```bash
./deploy.sh --update --resume --confirm-destructive
```

`--confirm-destructive` requires `--update` and is forwarded through published
release upgrades too. It authorizes destructive plans encountered during that
invocation; inspect their resource lists before using it.

For the reviewed Claude pricing gap:

```bash
./deploy.sh --update --resume --allow-known-claude-gap
```

This explicitly accepts only the known seven-model, 264-retained-variant gap,
across accounts/environments/regions. Finalization still requires fresh prices,
no failed sources, the expected deployed image and the verified active pricing
generation. Retained prices stay stale and freshness/partial-refresh alarms stay
active. A changed gap requires a new review. Recovery approvals may be added
when resuming without invalidating otherwise completed checkpoints.

Tfvars account validation ignores HCL comments (`#`, `//`, `/* ... */`) while
continuing to check string values, template strings, heredocs and numeric values.
