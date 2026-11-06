# Automation runner input review

The automation recipe requires an explicit immutable `RUNNER_IMAGE`. Repository scans obtain that input from the GitHub Actions variable `ADP_SECURITY_RUNNER_IMAGE`; rebuilding the ARC Dockerfile does not advance the variable automatically.

The 20 October 2026 refresh selects the rebuilt runner with patched Go tools and AWS CLI Python 3.14.8. The runner and automation derivative both have zero native Critical and 16 native High findings in fresh scans. Exact digests, source recipe hash, report hashes and compatibility checks are recorded in `runner-base-review.json`. Raw reports remain private.

The Ubuntu feed still reports CVE-2026-19445 as Medium against eight packages from the system Python 3.12 source package, with no fixed package listed. That finding remains open alongside the High findings. These records cover the reviewed scan inputs; production runner rollout is separate.

When updating the runner, build and test the automation derivative against its immutable digest, scan both exact images, retain the artifact, and update the repository variable to the full registry reference plus the reviewed digest. Keep the caller-supplied build argument so installations can use their own registry; a platform-specific registry must not become a universal fallback.
