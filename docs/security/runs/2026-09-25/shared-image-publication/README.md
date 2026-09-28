# Shared image publication receipt

Four existing CodeBuild projects successfully published new immutable image
candidates from reviewed source `3f60504a9008276e24b072ba814fcf050739cd1b` in account
`879318057152`, region `us-east-1`. All four source tags were absent before the
builds. This exercises the publication contract reviewed in #6181 after the
repositories were set to `IMMUTABLE` without exclusions.

| Repository | CodeBuild project | Published digest |
| --- | --- | --- |
| `adp-gateway` | `adp-dev-gateway-build` | `sha256:18684248f944b1e5464d6ae323b9049b2f8aff0b184e36e2137ad462b47497d6` |
| `adp-agent-runtime` | `adp-dev-agent-runtime` | `sha256:7ebe1f235780d6d70ffa2adc8f4f84cc96455cdab25cbfbd6761d8773ce04386` |
| `adp-chat-agent` | `adp-dev-chat-agent` | `sha256:4dded4e6373a7e7d58afbc8f7958e8705dcaf1af6f2213a8b3dacbc5a285803d` |
| `adp-agent-gateway` | `adp-dev-agent-gateway` | `sha256:a3e4a7ae993738609c69c32b87ee194d1f7e5c5aa3fd686d3c3fd0291585f0e5` |

`receipt.json` records each complete build ID, exact source S3 object and version,
CodeBuild phase results, image URI, image size/media type, and source/build hashes.
The source ZIP SHA256 is
`641a8531f4372f7d5270c70e2333b1d16842e39f49957ba61278fda204542925`.
All 5,453 archived files were independently matched to Git blob identities at the
reviewed commit, and all four checked-in buildspecs matched the archive bytes.

The launch followed the source-archive and per-build override contract in
`.github/actions/codebuild-run/action.yml`: the reviewed `zip-source.sh` packaged
a clean detached worktree, each project received a unique S3 source key, and each
start request supplied its checked-in buildspec, full `ADP_SOURCE_SHA`/`IMAGE_TAG`,
registry, `PUBLISH_LATEST=false`, `PUBLISH_LOCAL_BUILD=false` and an idempotency
token. The existing CodeBuild role/project configuration was retained. Every build
ran the reviewed shared publisher, including its applicable gateway/worker image
selfchecks. All four builds reached `SUCCEEDED`.

Verification independently hashes the exact manifest returned by ECR
`batch-get-image`, compares it to the source tag's `describe-images` digest, and
matches the build's `Published` log line to that URI. No mutable alias was
published. This execution did not change repository configuration, workloads,
credentials, or scanner denominators.

These are reusable candidates for #6111/#6112. Publication is not evidence that
all vulnerabilities are remediated, and these images do not include commits made
after the stated source revision. Image rescans and runtime/rollout acceptance
remain separate; neither issue is closed by this receipt.
