# Select the first administrator

Portable releases default to `create_test_users = false`. Before a **fresh
installation**, select a real operator email:

```bash
export ADP_BOOTSTRAP_ADMIN_EMAIL='operator@your-company.example'
./deploy.sh --aws-profile customer --release <published-tag>
```

The installer validates that selection before provisioning. After the gateway
is healthy, it resolves the Cognito pool from SSM, invites the selected user if
necessary, seeds the administrator's database records, and verifies its Cognito
role and organization claims. Cognito emails a temporary password; complete the
required password change through the hosted sign-in or `adp admin setup`. The
installer never generates a permanent password, stores an operator password in
Secrets Manager, or prints one. Cognito email delivery must be configured and
able to reach this address. Existing users are not reinvited or password-reset.

To retry bootstrap after a partial installation, use the same identity:

```bash
modules/gateway/scripts/bootstrap-admin.sh --env dev --region us-east-1 \
  --email 'operator@your-company.example'
```

`--pool-id` (or `ADP_BOOTSTRAP_ADMIN_POOL_ID`) overrides SSM pool discovery.
`--dry-run` performs read-only discovery; `--check-config` validates the selection
without AWS access. An existing database identity with a different role or
organization is rejected rather than given conflicting
administrator claims. Use normal administrator management for such users.
Updates skip first-admin bootstrap and do not require this email selection.
`--skip-admin-bootstrap` remains available when an operator manages bootstrap
separately; that operator must complete it before the platform is usable.

## Explicit development fixtures

For isolated dev/test automation only, set `create_test_users = true` in the
reviewed target-specific gateway inputs **after** portable preparation and set
`ADP_BOOTSTRAP_USE_TEST_ADMIN=true` (or pass `--use-test-admin` to the bootstrap
script). Do not also select an operator email. Terraform rejects test users in
all other environments, including staging and production. Portable templates
must always disable test users. Existing platform dev E2E fixtures remain an
explicit opt-in; they are not customer release defaults.

## Existing deployments with test administrators

Changing release defaults does not retire an existing account. Portable upgrades
retain recorded dev/test inputs. If an older state contains test users without
recorded inputs, the upgrade stops before applying: first perform a reviewed
direct upgrade with the original target-specific inputs to record them.

Before disabling existing test users, establish a real administrator, verify a
fresh sign-in and administrator access, and update any E2E automation using the
test identity. Review a Terraform plan showing the intended test users,
permanent-password resources and credential secrets to be retired. For a
production/staging deployment, complete this migration through a reviewed direct
upgrade before using portable upgrades; do not bypass the environment check.
Revoke the retired test user's sessions and remove its remaining database
administrator access as part of the approved retirement. Preserve an independently
verified recovery administrator. Never blindly apply `create_test_users=false`
to a live deployment whose only administrator is the test account.
