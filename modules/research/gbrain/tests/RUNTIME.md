# Container runtime regression

Build the actual image and run the opt-in disposable database test:

```bash
docker build -t gbrain-runtime-test -f modules/research/gbrain/docker/Dockerfile modules/research/gbrain
GBRAIN_RUNTIME_IMAGE=gbrain-runtime-test python3 -m unittest discover -s modules/research/gbrain/tests -p test_container_runtime.py
```

The test uses `pgvector/pgvector:pg15` (override with `GBRAIN_TEST_POSTGRES_IMAGE`),
an internal Docker network, disposable database credentials, and no published
ports. It removes its containers/network on success or failure. Two fresh app
containers share one disposable database to verify migrations, writable non-root
HOME, cross-container health, missing/invalid bearer rejection, legacy bearer
`tools/list`, and authenticated `put_page`/`get_page` persistence across replacement.
No production database, secret or runtime profile is used.

The default entrypoint uses upstream `--db-only` because Fargate home directories
are ephemeral. Existing canonical filesystem roots are not detached: upstream
refuses that conversion and requires an explicit storage migration. Existing
private deployment commands must be reviewed separately; a custom command that
calls `serve` needs `--bind 0.0.0.0` for container peers. Keep the known-good rollback
profile separate from any proposed new profile.

The dedicated scheduled task uses `terraform/modules/fargate/dream-command.sh`.
It initializes an ephemeral HOME from the injected database credentials, retains
1024-dimensional Titan embeddings by default, then runs the upstream finite
`gbrain dream` command. It respects configured phase/model safeguards. The task
has no HTTP port/health check or MCP credential, and EventBridge invokes its exact
immutable revision without a command override. The daily 03:00 UTC schedule is
unchanged.

Test the actual scheduled command against disposable PostgreSQL and a mock
OpenAI-compatible embedding endpoint on an internal Docker network:

```bash
GBRAIN_RUNTIME_IMAGE=gbrain-runtime-test python3 -m unittest discover -s modules/research/gbrain/tests -p test_container_dream.py
```

The first task migrates an empty database and exits. A second fresh task embeds a
seeded chunk through the mock model; the test checks the stored vector has 1024
dimensions. A third fresh task with existing vectors and missing model identity
must refuse initialization without changing those vectors, recording a new
identity, or calling the model. All fixture containers/networks are removed. No production dream run
is part of this test. Upstream `dream --dry-run` may call models and cache triage
results, so it is not a read-only production acceptance check.

Deployment requires a separate reviewed saved plan for the dedicated task,
EventBridge target, and exact `ecs:RunTask` resource. Verify the active immutable
revision and target/IAM agreement after applying; source merge alone does not
repair an existing scheduler pinned to an inactive revision.

Existing vector provenance must be inspected before activation. Matching vector
widths or per-chunk default model labels do not establish model identity. If
upstream refuses initialization because identity is absent/different, preserve
that guard and review an explicit migration separately; do not relabel vectors
or add override flags to make initialization pass.
