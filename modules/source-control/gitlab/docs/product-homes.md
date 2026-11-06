# Product GitLab homes

ADP Products can have private GitLab subgroups on the optional shared GitLab
instance. The Product page shows preparation status and opens its subgroup
through an authenticated, short-lived login handoff. Ordinary members need no
GitLab password or personal access token to open the home.

The dedicated home reconciler creates stable namespaces, pre-provisions users,
and synchronizes current ADP team membership. `member` maps to GitLab Developer;
`lead` maps to Maintainer. Unknown team roles receive Reporter access. Parent
tenant groups are owned by the provisioning service; ordinary users belong to
Product subgroups, so access is not inherited across every Product in a tenant.

The worker reconciles existing and new Products automatically. After each pass
it waits 30 seconds. Launches require a successful reconciliation no older than
three minutes and a matching current membership snapshot. Revocation in an
existing GitLab session follows the next successful reconciliation; monitor
worker failures rather than treating the polling interval as a hard latency
guarantee. Retiring or archiving a Product removes managed human access while
retaining the group and repository data. A removed member's other Product
memberships and shared GitLab account are preserved.

## Install or recover

Follow the repository's [deployment guide](../../../../docs/adp-platform-deployment/deploy-with-agent.md)
and confirm the target account before applying. Keep account-specific values,
plans, backups, and deployment receipts outside public source control.

For package maintenance on an existing host, follow the
[GitLab upgrade procedure](upgrades.md), including required version stops and
preservation of the ADP sign-in handlers.

1. Inventory the existing GitLab module's state, live instance, volumes, and
   backups. A restore must start on the backup's exact GitLab version. Keep the
   portal route disabled until restoration and authentication are verified.
2. Plan the GitLab Terraform module with the existing target configuration.
   Set `gateway_role_name`, `portal_operator_role_arns`, `eks_oidc_provider_arn`, and the exact
   `reconciler_database_resource_arn`. The module creates secret containers and
   separate gateway/reconciler permissions. Its instance root volume is retained
   on termination. Review the saved plan before applying it.
   If the VPC uses a private Secrets Manager endpoint, supply its security groups
   through `secretsmanager_endpoint_security_group_ids`; bootstrap needs HTTPS
   ingress from the GitLab instance to that endpoint.
3. Run `scripts/provision-jwt-keys.py --environment ENV --region REGION` from an
   authorized operator environment with boto3 and cryptography installed. It
   preserves an existing signing key and publishes its public key to SSM.
4. Restore GitLab's data and its matching `gitlab-secrets.json` if needed.
   Install `python3-boto3` on the host, then run `scripts/configure-portal-sso.py` as root on
   the GitLab host, with `--environment`, `--region`, `--portal-url` (the HTTPS
   ADP origin), and `--guard-file` pointing to
   `infra/templates/adp_jwt_validation.rb`. Place `adp_cli_oauth.rb` beside that
   guard (or pass `--cli-guard-file`). The script installs both receivers and
   creates or preserves the dedicated public ADP CLI OAuth application. It
   reconfigures GitLab and restarts
   Puma. Re-run it after GitLab package upgrades so the validation initializer
   and public runtime configuration remain installed. `portal_sso_enabled` also
   supports installing this configuration at first boot when the key is already
   provisioned; leave it false during an existing-data restore.
5. Run `scripts/seed-reconciler-token.py --environment ENV --region REGION` on
   the GitLab host. It creates a dedicated administrator service identity and
   stores its token directly in the module's secret. It never prints the token.
   The token expires after 90 days; operators must renew it before expiry. The
   bootstrap helper deliberately preserves an existing credential.
6. Deploy the gateway code and migration `150_gitlab_product_homes`. Render
   `infra/home-reconciler.yaml` with the reviewed gateway image digest and
   Terraform's `home_reconciler_role_arn`, then apply it to the existing EKS
   cluster. The worker uses the existing gateway ConfigMap for database
   connection settings and its own service account for AWS authority.
7. Restore the existing gateway module's GitLab CloudFront origin and
   `/gitlab/*` behavior using the discovered ALB configuration. Preserve other
   origins and behaviors. Verify a real GitLab response through the public URL,
   not just a 200 response from the ADP frontend.
8. After configuration and reconciliation succeed, apply
   `infra/portal-runtime.yaml` in the target cluster and roll out the gateway.
   Its Deployment reads `FEATURE_GITLAB_ENABLED` from this GitLab-owned
   ConfigMap. An installation without this ConfigMap stays disabled; ordinary
   gateway releases preserve the optional module's activation. Avoid an
   imperative `kubectl set env` flag that the next gateway release can remove.
   Publish the matching frontend. Verify the full
   authenticated browser journey and cross-Product access before announcing
   availability. Record the deployed source/image, migrations, infrastructure
   plan, reconciliation results, and acceptance evidence privately.

## Identity and permissions

Users are resolved through ADP's current workspace membership and canonical
login links. Existing GitLab OIDC accounts are linked only by an exact Cognito
subject. Email collisions require operator resolution; no email-based account
takeover or duplicate identity workaround is performed. Blocked GitLab users
are not automatically unblocked.

GitLab just-in-time account creation and automatic email linking are disabled.
The receiver pins RS256, issuer, audience, timestamp bounds, and the signed
relative destination. Redis consumes each login identifier once. Repeated claim
reads within the same authentication request remain valid. Access logs omit
query strings and Rails filters the `jwt` parameter.

The gateway reads only the signing key and is explicitly denied the reconciler's
administrator secret. The dedicated worker can read that administrator secret
and connect to the configured database. Secret resource policies also deny
access from other principals, including legacy agent roles with broad secret
read permissions. Explicitly configured operator IAM roles retain recovery access.
Managed groups forbid subgroup creation by ordinary members. Reconciliation
removes unmanaged direct project grants and project/group shares in managed
homes; these namespaces are not intended for independent GitLab-only access
administration.

Repository connections and agent execution retain their existing explicit,
instance-qualified project approvals. Creating a Product home does not move
existing repositories, create a new repository, or authorize an agent producer.
ADP CLI login also bootstraps native GitLab OAuth for `glab` and HTTPS Git;
see [the terminal workflow](../../../../docs/adp-cli/gitlab.md#use-hosted-gitlab-after-adp-login).
Browser and terminal assertions have different audiences and cannot be exchanged
for one another. The terminal receiver issues credentials only for pre-provisioned,
active, external human users with managed group membership. Normal GitLab OAuth
refresh and revocation own the resulting grant lifecycle. The existing Product
source-binding workflow still requires its explicit project approvals.

## Verification

Backend tests cover Product scope, retries, removal, role downgrade, archive,
namespace ownership conflicts, and identity conflicts against a GitLab HTTP fixture.
PostgreSQL tests cover binding constraints and migration rollback/re-upgrade.
Browser tests cover the Product action, preparation/error states, navigation
after unmount, and prevention of redirect-following with ADP credentials.

Terraform tests use mocked providers. The standalone Ruby receiver test uses
the actual JWT library version shipped with GitLab 19.1.2:

```bash
gem install jwt -v 2.10.3 --no-document
gem install minitest -v 5.25.5 --no-document
ruby modules/source-control/gitlab/infra/tests/portal_sso_test.rb
```

These tests do not replace live GitLab acceptance: sign into ADP through GitHub,
open the selected Product home without another credential prompt, verify the
landing group and native permissions, then remove membership and verify access
is denied in an already-open GitLab session. Repeat with a second Product and
a second tenant. A failed or stale reconciliation must disable new launches.
