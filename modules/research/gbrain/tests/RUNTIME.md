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
