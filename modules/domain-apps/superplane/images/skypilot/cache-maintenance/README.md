# Source-backed Python cache cleanup

`clean_oci.py` implements the finite `remove-bytecode/v1` transition from the
exact-image Python component verifier. It accepts an already reviewed OCI
producer, removes only supported CPython 3.10 caches with existing source siblings
under the standard library, cryptography and the supported distutils hook, and
removes precisely their RECORD rows. Other RECORD bytes, replacement RECORD
ownership/modes/timestamps, inherited layers, source/native files and image
runtime settings remain unchanged unless the explicit source-label option below
is used. Unknown or orphan caches fail closed.

The recipe reconstructs both images with the explicitly selected verifier and
requires the entire resulting filesystem to equal its computed transition. It
writes an OCI archive and a receipt retaining input/output hashes, verifier and
recipe hashes, every removed cache/source pair and changed RECORD hashes. The
output directory must not exist. The input archive is never changed.

```bash
python3 modules/domain-apps/superplane/images/skypilot/cache-maintenance/clean_oci.py \
  --archive /path/to/reviewed-producer.tar \
  --platform sha256:EXACT_PRODUCER_PLATFORM_DIGEST \
  --verifier codebuild/exact_image_disposition.py \
  --output /path/to/new-cleanup-evidence
```

Replace the illustrative digest with the independently selected platform digest.
Use a reviewed checkout; the recipe needs the optional Python component verifier
introduced in PR #7110. It has no network access or container execution step and
preserves labels by default. For a final composition, add `--source-revision`
with the full actual checkout HEAD. The recipe requires the selected verifier
and component implementation in that same clean tracked checkout, compares their
executed bytes with Git, and rechecks the source context after composition. Only
`org.opencontainers.image.revision` may change; every other label and runtime
setting is preserved. The receipt retains the explicit revision and source hashes.
The external composition process must still retain honest source/build provenance
for the producer and this recipe; a revision label does not establish that chain.

The receipt is a transformation record, not a vulnerability disposition or source
attestation. The final image still needs normal-context runtime checks, new raw
scanner results, complete producer/source evidence and independent approval.
Bytecode deletion does not assert that historical caches implement reviewed source.
