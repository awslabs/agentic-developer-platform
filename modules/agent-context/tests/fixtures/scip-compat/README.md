# Legacy SCIP acceptance data

Synthetic `sample.py` has a function definition and a module-level call. The
retained binary/JSON and expected actual ScipIndexParser output were generated
with the verified upstream CodeGraphContext0.6.13 wheel and protobuf3.20.3.
`provenance.json` identifies the wheel, exact Docker config, schema and wire
hashes. `schema.pb` is the upstream serialized SCIP schema, not a reduced fixture
schema. It retains all message, enum and field definitions.

Index field127 (varint123) is deliberately appended as an unknown field so that
round-trip tests prove forward-compatible data survives reserialization.
`tests/container/codegraph_scip_compat.py` expects this directory read-only at
`/fixtures` and writable `/tmp`, in a network-disabled non-root candidate image.
No real source code, user data or credentials are included.
