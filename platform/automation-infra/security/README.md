# Automation runner input review

The automation recipe requires an explicit immutable `RUNNER_IMAGE`. Repository scans obtain that input from the GitHub Actions variable `ADP_SECURITY_RUNNER_IMAGE`; rebuilding the ARC Dockerfile does not advance the variable automatically.

The 6 October 2026 review found that the stored input still selected the old September runner. Both the reviewed replacement and the automation derivative have zero native Critical and 14 native High findings in fresh scans. The exact digests, source recipe hash and compatibility checks are recorded in `runner-base-review.json`. Raw SBOMs, scanner reports and receipts remain private. High findings remain unresolved; this review does not establish production rollout.

When updating the runner, build and test the automation derivative against its immutable digest, scan both exact images, retain the artifact, and update the repository variable to the full registry reference plus the reviewed digest. Keep the caller-supplied build argument so installations can use their own registry; a platform-specific registry must not become a universal fallback.
