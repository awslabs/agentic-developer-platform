# CodeGraphContext protobuf compatibility patch

CodeGraphContext0.6.13 ships legacy generated SCIP bindings that instantiate
protobuf descriptors directly, and requires protobuf3.20.x. That runtime carries
GHSA-7gcm-g887-7qv7 and GHSA-8qvm-5x2c-j2w7. Raising the installed runtime without
repairing those generated bindings breaks actual SCIP decoding.

`patch-codegraph-wheel.py` accepts only the SHA256-verified upstream0.6.13 wheel.
It extracts the existing serialized FileDescriptor through Python AST literals
without executing upstream code, and emits the descriptor-pool/builder form used
by modern protoc. The serialized schema is byte-identical. Only generated
bindings, the protobuf dependency range, wheel RECORD hashes and an explicit
`adp-protobuf-migration.json` receipt change. The upstream package version remains
0.6.13, so other package observations are not hidden by a version bump. Pip still
resolves and checks every dependency.

The canonical build stages this directory as `security-build/` in the ingestion
and CodeGraph image contexts. Local image validation archives/stages the same
files. Direct Docker builds must stage this directory likewise; ingestion also
needs its existing door/pipeline/alembic/personal_context staging.

Tests under `tests/fixtures/scip-compat` retain synthetic wire and parser output
from the unpatched3.20.3 runtime, including an unknown protobuf field. The
container gate checks exact modern reserialization, expected symbol/reference
output through the actual CGC parser, malformed-wire rejection and embedded patch
provenance. The independent wheel tests refuse changed artifacts or requirements
and verify every rebuilt RECORD entry. Full six-image findings and deployment
acceptance remain tracked under #6122; the patch is not a family clearance.
