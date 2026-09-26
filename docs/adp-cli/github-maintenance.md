# GitHub maintenance

Source implementation for #5634. Install the matching gateway/CLI before using these commands. Read state before reviewing a write; existing setup, connect, status and revalidate commands remain available.

```sh
adp github disconnect --installation 42 --dry-run
adp github disconnect --installation 42 --yes
adp admin github org-binding list --org TENANT_ID
adp admin github org-binding add --org TENANT_ID --installation 42 --dry-run
adp admin github org-binding remove --org TENANT_ID --installation 42 --dry-run
adp admin github status --maintenance --json
adp admin github disconnect --expect-app-id APP_ID --expect-key-version VERSION --dry-run
adp admin github rotate-key --expect-app-id APP_ID --expect-key-version VERSION \
  --operation-id UUID --credentials-file ./key.json --dry-run
```

Every mutation supports `--dry-run` and requires `--yes`. All commands support `--json`. User disconnect uses the canonical owner-authorized revocation saga: local denial, GitHub uninstall, and cleanup are separate outcomes. A pending response is incomplete. Organization removal only detaches local routing; it does not uninstall or delete the external App. `--restore-revoked` on binding add explicitly restores a completed local detach; it cannot undo provider uninstall or pending cleanup. Optional `--github-org-id` and `--github-org-login` provide canonical binding metadata.

App maintenance requires platform administrator privileges and both reviewed App ID and key version. Generate a new key explicitly in GitHub first. Supply JSON containing only `private_key` through an owned regular file with permissions `0600`, or `--credentials-stdin`. Preview does not open that file or read stdin. Preserve the UUID and exact input for reconciliation; keys are never stored locally or printed.

Rotation verifies the supplied key against GitHub's App endpoint before staging an AWS Secrets Manager version with that UUID. Activation checks the previous current version. Previous secret versions and GitHub keys remain intact; ADP does not revoke the old GitHub key. Existing OAuth and webhook configuration remains intact, and caches converge on their normal refresh. This is credential configuration evidence, not proof of working OAuth or webhook agents.

If acknowledgement is lost, read `status --maintenance`. Replay the same UUID and key to confirm an already-current operation or finish a pending operation against the original revision. A changed App/revision, different input, or superseded operation is refused. Never retry with a new UUID simply because an outcome is unknown. Deregistration removes deployment credentials through the existing service and can stop sign-in, repositories and agents; it does not delete the App on GitHub.

E28 joins `story-reads` and default nightly using the installed CLI. It discovers the selected deployment's App through maintenance status and checks previews plus invalid installation refusal without shared App writes. It needs no disposable App fixture; an unavailable maintenance endpoint or App fails the read regression with its actual error. The separate mutating GitHub journeys still require isolated fixtures. Full #5634 acceptance remains held for isolated binding/unbinding/disconnect, real rotation with lost-response recovery, ordinary/admin denial, and actual OAuth/repository/webhook continuation and cleanup. Shared production App resets are excluded.

Deployment prerequisite: apply the gateway workload's narrow `GitHubAppKeyActivation` policy before using rotation. It grants `secretsmanager:UpdateSecretVersionStage` only to this environment's App key secret (including the AWS six-character ARN suffix). An attached gateway permissions boundary must independently permit that same action/resource; the operator-owned workload inventory/ceiling is not changed by this story. The generic automation workload allowlist currently omits Secrets Manager mutations, so it cannot itself certify this gateway CRUD role. No runner permissions are needed for runtime rotation.
