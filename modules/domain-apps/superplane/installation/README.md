# Complete domain installation (U23 / #5327)

`modules/domain-apps/superplane/deploy.sh` plans, checks and executes an installation of the API, controller, platform monitor and pinned SkyPilot server on an **existing** ADP installation. It never calls a platform deployment script. Actual installation, database mutation, feature activation and workload operation remain subject to the accepted installation authorization.

The current source **refuses production installation**: the production vault-evidence reader, B operation/allocation adapters and governed controller execution adapter are absent. The API image and controller report their actual compiled capabilities. Environment flags, healthy pods, caller reports and test doubles cannot satisfy those checks. B/#4912 remains planning-only. Code tests do not demonstrate a usable deployment.

## Inputs and prerequisites

Use Python 3.12 with `PyYAML` and `httpx`, Terraform matching the maintained module, AWS CLI v2 with conditional S3 PUT/DELETE support, kubectl and Docker. The operator needs network access to the selected EKS clusters and database. Keep output directories private and outside the checkout.

Supply an environment YAML based on `environment.example.yaml` and an immutable release lock based on `releases/superplane.lock.yaml`. The example deliberately leaves unresolved environment decisions blank; it is not an execution configuration. No credentials belong in either file.

The release lock must remove the three built images from `pending_images`, supply their observed ECR digests in `images`, and record `registry`, `repository` and `source_revision` under each `image_sources` entry. Set the root `source_revision` to the exact clean ADP checkout used by the maintained build lanes. Registry tags and OCI revision labels must agree with it. Preserve the reviewed SkyPilot 0.12.0 digest. This command consumes completed immutable builds; it does not treat a workflow dispatch as a completed build.

The supported first target is existing EKS with the managed VPC CNI's NetworkPolicy enforcement enabled. The workspace cluster must differ from the ADP management cluster. Its existing, approved CRDs, namespace, scoped controller identity and controller handover are prerequisites; this installer does not create cluster-wide access or take over a legacy controller. The management cluster receives only domain namespaces, namespaced service accounts, bounded deployments, services, secrets and network policies. SkyPilot binds to pod loopback and is reached through a bearer-authenticated sidecar built from the pinned API image.

Install the small Gateway transport integration through the normal reviewed platform release before running the domain installer. It exposes `/api/superplane/installation-support`. The existing Gateway IRSA policy already permits environment-scoped SSM reads. Subsequent domain operations write only `/adp/<environment>/superplane/public-route`; activation/removal takes at most five seconds and needs no Gateway restart. An explicit `FEATURE_SUPERPLANE_ENABLED=false` overrides registration. The public proxy forwards only inventoried domain methods/paths and the original ADP bearer token; the domain API performs token and workspace authorization. It does not publish local login/token minting, internal callbacks, OpenAPI or service diagnostics.

The selected existing RDS instance, database and **two isolated schemas** (API and SkyPilot) need named migration, backup and restore owners and an available matching snapshot. Runtime and migration roles must have the correct schema search path and no rights to mutate other schemas, no role memberships that can elevate authority, and no database-creation/superuser authority. Configure migration-role default privileges so the runtime role can use migrated domain tables and sequences. The API and migration use an explicit verifying SSL context from `ca-pem`; the pinned SkyPilot package uses both psycopg2 and asyncpg, so it receives `PGSSLMODE=verify-full` and a mounted `PGSSLROOTCERT` bundle with a driver-neutral URL. Runtime API and migration sessions explicitly configure asyncpg's `search_path`; they do not rely on the ignored libpq `PGOPTIONS` variable. The maintained Alembic chain runs from the API image and must reach `015_add_adp_org_binding`. An image rollback does not reverse a database migration.

Three environment-scoped Secrets Manager references contain these exact JSON string fields:

| Reference | Fields |
|---|---|
| `secrets.database` | `runtime-url`, `migration-url`, `skypilot-url`: distinct scoped `postgresql://` role URLs with no query parameters, all bound to the selected RDS endpoint/database; `ca-pem`: trusted PEM certificate bundle for that RDS endpoint |
| `secrets.observation` | `submitters` (serialized JSON array), `monitor-credential`, `monitor-signing-key`, `controller-credential`, `controller-signing-key`, `skypilot-token` (at least 32 characters) |
| `secrets.workspace_access` | `kubeconfig`: one workspace cluster and one static bearer/certificate identity; no exec plugins, local file references, impersonation or proxy/TLS overrides |

The submitter array has exactly two entries with distinct `submitter_id` and credentials, matching the two named credential/key pairs and the exact immutable workspace UUID. Only the monitor has `lease_scopes: ["budget_monitor/global"]`; the controller has no global lease scope. The installer reads values into memory, supplies Kubernetes Secret objects through stdin, and records only secret **version IDs**. Version changes restart the consuming pods. Output does not contain credentials.

For a fresh installation, `adp_org_id` names the actual ADP organization (for example `aws-e`); the three UUIDs identify the new domain organization, workspace and cluster. After migration, a one-shot bootstrap Job verifies the signed ADP access token and current `org_admin` membership through `/api/auth/workspaces`, checks both again before commit, and stores an explicit organization binding and one initial `administer` workspace grant for that human subject. The policy defines the permissions implied by this grant. Resume is idempotent and never restores a revoked grant, rebinds an existing organization, or adopts an unbound legacy organization. The temporary token Secret is deleted with UID preconditions after terminal bootstrap success; an interrupted or failed Job requires recovery inspection. U21's historical identity mapping, migration and cutover remain separate.

## Run

Use absolute paths for clarity. With complete inputs, the default command renders every action and manifest without contacting AWS:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run
```

Preflight performs reads, verifies both cluster identities and credential scope, checks real image capabilities, database privileges and backup identity, observes Gateway support and produces a structurally checked domain-only Terraform plan:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --preflight
```

Review the saved Terraform plan and its `plan_sha256`, then obtain the retained installation authorization for the exact environment/release, database, permitted operations/spend and recovery owners. Supply an authorized ADP access token through `SUPERPLANE_VERIFICATION_TOKEN` in the process environment, never argv or a file committed to Git.

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --execute \
  --approved-plan-sha256 <reviewed-plan-sha256>
```

Preflight is repeated, and a changed plan is refused. An atomic conditional S3 lock serializes installers across machines. Terraform applies only its saved, reviewed domain plan. The command installs foundations, runs and waits for the exact migration and fresh-tenancy bootstrap Jobs, waits for all four deployments, verifies runtime auth/schema and authenticated SkyPilot health, and waits for fresh authenticated **observations from both the controller and monitor** for the selected workspace. The controller must report healthy; a monitor result of `not_checked` is retained explicitly because an empty fleet and unavailable cost/vault probes cannot establish workload health. Degraded, unknown or unreachable reports refuse completion. Only the independently configured controller submitter can advance controller liveness; a monitor cannot refresh it. Only then does it register the public route and verify an authorized request through the existing ADP origin, invalid/missing credential denial, private-route denial and existing ADP availability. A dispatch, successful `apply`, or a ready pod alone cannot produce `installed-and-verified`.

The private JSON receipt records the source, release, environment, plan, run ID, migration Job, object UIDs, secret versions, authenticated observations and public endpoint. This is installation evidence; GPU/workload parity and the five live evaluations remain separate.

## Interruption, resume, rollback and cleanup

Every phase records its state before starting. `--resume` requires the identical environment/release and uses the same migration and bootstrap Job identities. A nonterminal Job is waited on, never duplicated. Failed/interrupted mutations retain the shared lock with status `recovery-required`; time passing never steals it.

After the named operator verifies that the prior installer and child processes have stopped, inspect the recorded migration Job and Terraform backend lock. A still-running migration or bootstrap Job prevents lock recovery. Release only the recorded lock version:

```sh
modules/domain-apps/superplane/deploy.sh \
  --environment /secure/environment.yaml --release-lock /secure/release.yaml \
  --output /secure/superplane-run --resume --recover-lock \
  --confirm-stopped <recorded-run-id>
```

Then rerun preflight and resume with the newly reviewed plan hash. A terminal failed migration Job is not silently deleted/retried: diagnose its schema state and follow the named restore owner's recovery decision. A new installation attempt requires the failure to be resolved first. Publication or verification failure disables the public route; it never silently claims the previous release remains available.

For image/configuration rollback, pass a prior **successful receipt from this same environment** using `--rollback /secure/previous/receipt.json` and a new output directory. The command verifies schema compatibility, restores pinned secret versions and the prior four-service release, and repeats private/public verification. It refuses cross-schema rollback and performs no downgrade or restore automatically.

`--cleanup /secure/failed-or-current/receipt.json`, with a new output directory, disables routing and removes only recorded, still-owned Kubernetes object UIDs, using API-server UID/resource-version preconditions. Workloads are removed before network policies. It preserves namespaces, Kubernetes Secrets, databases/backups, domain Terraform/ECR resources and external workspace/provider resources for recovery, and reports those retained resources explicitly. It does not destroy the core platform or pretend to reclaim billable external capacity.

## Code validation

The offline integration harness uses instrumented tool processes and HTTP responses. It exercises the real installer sequence, missing prerequisites, account/source/image/ownership boundaries, migration and rollout failure, public verification failure, lock retention, schema-incompatible rollback, and secret-free receipts. Separate API tests exercise schema confinement against disposable PostgreSQL. Gateway tests compare the public route list with the maintained API inventory and exercise authentication forwarding and private-route denial. Go tests validate signed controller observations and SkyPilot token/redirect handling. None is presented as live installation acceptance.
