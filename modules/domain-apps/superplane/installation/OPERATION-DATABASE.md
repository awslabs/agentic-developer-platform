# Shared operation database preparation

This explicit operator entry point prepares the shared Harness Jobs store in an
isolated schema of the selected database. It does not run through API startup or
API Alembic. It calls `harness_jobs.installation.prepare`, which applies the
canonical Harness Jobs schema in one transaction, with a separate NOLOGIN owner
and distinct Gateway/worker login roles. The API runtime, migration and SkyPilot
roles must have no access to the shared schema. Shared runtime roles must have no
access to either domain schema. Existing unmarked resources, changed privileges,
unknown grants and a newer shared schema refuse instead of being adopted.
The supplied database installation operator receives membership only in the
owned NOLOGIN migration role so a non-superuser RDS administrator can execute
the migration. Runtime/domain roles receive no such membership. Runtime ACLs
allow only table read/write and sequence read/use, with no grant option; extra
direct or default privileges are refused rather than silently removed.

Use the selected deployment connection, an exact clean merged checkout and a
qualified release lock containing the paid-worker image built from that source.
The environment is the existing installation's control-plane environment, with
its immutable organization/database identities and actual available backup. The
database and observation prerequisites must already be prepared by their owner.
Keep all inputs and output directories private.

From the Superplane module directory, prepare an offline plan:

```sh
python -m installation.operation_database \
  --environment /private/environment.yaml \
  --release-lock /private/release.yaml \
  --output /private/operation-database \
  --schema superplane_operations
```

Inspect `receipt.json`: exact database/schema, installation identity, roles,
secret names, source and release hashes, and backup. Supply its actual
`plan_sha256` only after reviewing it. Put the database administrator URL in
`SUPERPLANE_DATABASE_ADMIN_URL` for this one-shot process; never put it in an
argument, checked-in file, task description, or transcript.

```sh
python -m installation.operation_database \
  --environment /private/environment.yaml \
  --release-lock /private/release.yaml \
  --output /private/operation-database \
  --schema superplane_operations \
  --resume --execute --approved-plan-sha256 "$REVIEWED_OPERATION_PLAN_SHA256"
```

Execution freshly verifies selected STS/IAM role identity, source/image
provenance, the management cluster, database endpoint and backup lineage. It
uses the shared installation lock and existing temporary namespace probe with
no Kubernetes/AWS workload identity, restricted database egress and bounded
cleanup. Credentials travel through a temporary Secret, not argv. The persisted
runtime credentials are independently authenticated over verified TLS before
completion. The receipt reports no secret values.

The exact outputs are Secrets Manager references under the installation's
`adp/<environment>/superplane/` prefix:

- `operation-database`: Gateway shared-store `dsn`.
- `operation-worker-database`: worker shared-store `dsn`.
- `domain-database`: existing domain runtime `dsn` projected separately for the
  Gateway's domain ORM adapter. It is never given shared-store grants.
- Kubernetes `superplane-paid-worker-db`: `domain-dsn`, `execution-dsn`, `ca.pem`,
  created only in the installation namespace after role authentication.
- Kubernetes `superplane-operation-api-db`: `dsn` projected from the authenticated
  Gateway shared-store role for the API Harness adapter. It does not reuse the
  worker role. Both Kubernetes Secrets reconcile only when ownership and every
  data key match; conflicting Secrets are never replaced.

Configure Gateway `database_secret_id`/`database_schema` for the shared store,
and `domain_database_secret_id`/`domain_database_schema` for the domain adapter.
The paid worker likewise needs distinct domain and operation schemas. The
consumer split is owned by lifecycle integration; preparation alone does not
attest binding readiness, dispatch anything, activate a worker or publish a
route. Never compensate for an unsplit consumer by granting domain roles access
to shared tables or combining schemas in a search path.

Interrupted execution reuses the same private receipt and generated credentials.
Use `--resume` with the original inputs; different schemas, source or secrets are
refused. If the receipt retains a lock or temporary namespace, first confirm the
recorded process and children have stopped, then use `--resume --recover-lock
--confirm-stopped <recorded-run-id>` with the same inputs. This checks exact lock
ownership and recorded namespace UID through the existing installer recovery
path. Do not manually delete a lock, invent a receipt, rotate credentials, drop
schemas or remove roles to make a retry succeed. Schema downgrade and credential
rotation require separately reviewed recovery; this command implements neither.
