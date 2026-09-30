# Superplane API build candidate

The [machine-readable receipt](superplane-api-candidate-b21d96858.json) records
a verified build from reviewed integration `b21d968586747748f3b605247a33fad328d4697a`.
CodeBuild `adp-dev-superplane-api:4606048a-2734-453d-be1d-89181659c8fb`
succeeded and published
`sha256:51c213cedc4d729c4f4ab4c056bf33bcc8e669bedbe0e6c81cb606b021340355`.

The canonical release archive path supplied only the selected Git commit. The
S3 source ZIP is byte-identical to an independently generated Git archive, SHA-256
`ecbbd8d58d08cb38791cd1f40ef3be34916d381acd046871b2ad5ddba22a61ac`.
The maintained build lane staged its sibling auth, contracts, account, workspace
and executor packages, and built the API Dockerfile. ECR manifest/config hashes,
Linux/amd64 platform, exact source tag and OCI source/origin labels were verified.
No runtime rollout, migration, route cutover, paid task, worker or IAM change occurred.
The reviewed release lock has not been promoted by this receipt.

This separate image contains the provider-connection operation-ID recovery and
capability advertisement needed by the updated CLI. Publishing the gateway image
does not update this domain runtime. The currently observed API on 2026-09-26
remained management-ready on older digest
`sha256:9e194dfb5ac7189c304ce618597983c95eb487018a5bd3dd3e65fb1c2bf01466`;
its internal `/capabilities` returned 404. Gateway transport version 2 was
configured, but its S3 route registration was absent. The legacy enabled SSM
registration does not activate the version-2 route.

## Installer requirements for the later upgrade

The maintained installer is `modules/domain-apps/superplane/deploy.sh`.
A fresh upgrade in a new private output directory does **not** require an old
receipt. It requires a verified environment, an immutable release lock, a clean
checkout matching the lock's source revision, and an authorized deployment
identity. Its ownership identity hashes account, region, environment and cluster;
those known dev values produce `27b8d766438ba597eca705d4`, matching the legacy
registration. Existing Kubernetes objects must carry that owner label and are
updated with observed UID/resource-version conditions.

The environment must preserve verified namespace, organization/authentication,
database/schema/backup/owner and secret-reference inputs. The full release lock
also pins controller, monitor and SkyPilot images. Reusing an older component
image requires identical component Git trees at its build and release revisions.
The installer has no API-only rollout flag. Its preflight verifies target,
network policy, images, secrets, database, gateway and the saved Terraform plan;
execution requires the exact approved plan hash and a verification token, then
migrates/bootstrap-checks, rolls out the management components, verifies them and
conditionally registers the S3 route. Do not copy the legacy SSM route over it.

`--resume` requires the exact previous environment, release and run receipt.
`--recover-lock` additionally requires the retained lock/attempt or temporary
namespace evidence, confirmed stopped run and terminal owned migration/bootstrap
Jobs. Rollback and cleanup require their original receipts. No installation lock
was present at `dev/modules/superplane/installation.lock` during this read-only
inspection, so a missing historical receipt alone does not establish a recovery
block. Deployment permissions and current environment inputs still require
verification before any later installer execution.
