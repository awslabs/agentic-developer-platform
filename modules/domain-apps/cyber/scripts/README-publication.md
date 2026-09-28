# Manual browser overlay publication

`codebuild/bs-direct-browser-worker.yml` builds a distinct overlay recipe in
`adp-agent-runtime`. Its publisher derives the tag
`direct-browser-v1-<40-character source SHA>-<64-character base digest>`.
Changing either committed recipe inputs or the base image changes the tag; it
never claims the generic runtime's source-SHA tag.

Provide `ADP_SOURCE_SHA`, `AWS_REGION`, `REGISTRY`, and a digest-pinned
`WORKER_BASE_IMAGE`. Use a source archive of that exact commit, as produced by
`platform/scripts/codebuild-run.sh` with `ADP_RELEASE_BUILD=true` and
`SOURCE_SHA` set. Select this buildspec explicitly for manual CodeBuild use.
Leave `IMAGE_TAG` unset, or supply the exact derived recipe tag. Generic SHA,
short SHA, and `latest` overrides fail before publication.

The publisher verifies the base digest in ECR. Existing recipe tags are pulled
by digest and must have matching source, recipe and base provenance labels;
they are reused without overwriting. New images run both existing contract and
browser-import checks before publication. The final `Verified image:` output
is a verified ECR digest reference. Feed that digest through the normal
reviewed rollout and authority checks; the publisher does not update workloads.

No configured CodeBuild project or tracked source caller selected this manual
buildspec in the 25 September 2026 inventory. The most recent 100-build sample
also contained no invocation. That bounded observation is not a deprecation or
a claim that the manual entry point is unused.
