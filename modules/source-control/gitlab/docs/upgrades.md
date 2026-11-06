# Upgrade ADP-hosted GitLab

GitLab runs in the separate source-control EC2 module. Upgrade the Linux package
on the existing host; changing Terraform user data does not upgrade an installed
package. Preserve the database, repositories, secrets, Product memberships, and
ADP browser and CLI sign-in configuration.

The module pins new installations to GitLab CE `19.5.1-ce.0`, the latest stable
package checked on 2026-10-28. Before a later upgrade, refresh the official APT
repository metadata and review the candidate and
[GitLab upgrade notes](https://docs.gitlab.com/update/versions/gitlab_19_changes/).
Do not use an unrestricted package update across required upgrade stops.

## Upgrade path and recovery

Follow the repository's [deployment guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md)
for target selection and private deployment receipts. For the upgrade from
`19.1.2` to `19.5.1`, the required path is:

1. Upgrade to `19.2.9`, the latest patch available for the required `19.2` stop.
2. Complete background migrations on every configured database.
3. Upgrade to `19.5.1` and complete its migrations.

See GitLab's [required stops](https://docs.gitlab.com/update/upgrade_paths/) and
[single-node package procedure](https://docs.gitlab.com/update/package/).
Review notes for the intermediate versions as well as the destination. Drain
running CI jobs and plan a maintenance interruption for each package upgrade.

Before changing packages:

- Verify there are no unfinished background migrations on either `main` or `ci`.
- Create a full `gitlab-backup create STRATEGY=copy` backup and a
  `gitlab-ctl backup-etc` configuration backup. Retain their exact source package.
- Separately back up `/opt/adp-gitlab` and these files beneath
  `/opt/gitlab/embedded/service/gitlab-rails/config/`:
  `adp-sso.json`, `initializers/adp_jwt_validation.rb`, and
  `initializers/adp_cli_oauth.rb`. They are outside the native application backup.
- Store the backups privately with encryption and take a recovery volume
  snapshot. Never commit configuration archives, package receipts, or target IDs.
- Record account settings and native user, identity, Product group, and project
  counts for comparison after the upgrade.

Database downgrades are not a rollback procedure. Recovery requires the original
GitLab version, its matching application backup and `gitlab-secrets.json`, or a
validated recovery volume. See GitLab's
[restore prerequisites](https://docs.gitlab.com/administration/backup_restore/restore_gitlab/).

## Preserve ADP sign-in during each package replacement

Stop GitLab NGINX, Puma, and Sidekiq before replacing the package. Leave
PostgreSQL, Redis, and Gitaly running for the native pre-install database backup.
Create `/etc/gitlab/skip-auto-reconfigure` for this maintenance operation so the
new package cannot start Rails before ADP's validation handlers are restored.
If that marker already exists, investigate its owner and purpose first.

Install the exact CE package version for the next stop using the official APT
repository. Restore the saved ADP initializers and `adp-sso.json` before starting
Rails. Preserve the existing CLI OAuth application ID and signing public key.
Remove only the `skip-auto-reconfigure` marker created for this operation, then
run `scripts/configure-portal-sso.py` as described in
[Product-home deployment](product-homes.md#deployment). It reinstalls both
handlers, reconfigures GitLab, preserves the OAuth application, and restarts Puma.
Start the remaining GitLab services afterward.

Read reconfiguration warnings about running component versions. Starting an
already-running service does not load its updated binary. Before completing
maintenance, restart packaged services that retained old executables, including
Redis or PostgreSQL when applicable, and verify their running versions. Preserve
the normal data directories and do not run an unrelated database major upgrade.

The `gitlab-home-reconciler` Deployment can enter restart backoff while GitLab's
API is unavailable. Pause it during the maintenance interruption, recording its
original replica count, and restore it once the GitLab API is healthy. If it was
left running and is still in backoff, restart that Deployment. Verify a successful
reconciliation before testing ADP sign-in: ADP deliberately refuses new handoffs
when the Product membership snapshot is stale.

Check background migrations across all configured databases before continuing.
For a small installation in a maintenance window, GitLab documents how to
[run migrations synchronously](https://docs.gitlab.com/update/background_migrations/#run-all-background-migrations-synchronously).
Run the migration work to completion; do not mark unfinished migrations complete
to bypass an upgrade check. Keep failure logs private and recover before moving
to the next package.

## Verify the deployed release

After each stop, confirm the native running version, service health, finished
background migrations, and retained identities, memberships, and repositories.
An NGINX health response alone does not verify Rails or its database migrations.

After the final stop, verify ADP browser sign-in and native `glab` authentication,
including OAuth refresh, HTTPS clone and branch push, and merge-request creation
in a temporary authorized repository. Confirm protected-branch and unauthorized
repository denials, CLI assertion replay rejection, and credential revocation.
Remove temporary test data and credentials afterward.

Check that public registration and visibility restrictions remain configured,
operational endpoints stay blocked, and the deployment's existing MFA settings
are unchanged. Package maintenance must not silently change authentication
policy. Record the final version, backups, snapshot, checks, and any remaining
work in the private deployment receipt.
