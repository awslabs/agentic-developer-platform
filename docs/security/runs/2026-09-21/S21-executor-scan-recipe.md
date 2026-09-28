# Executor image scan prerequisites

The maintained executor Dockerfile copies shared packages from the repository root and requires an explicit Python 3.12 base image. The image scanner now supplies that context and passes `PYTHON_IMAGE` as a build argument. It continues to require the executor target; missing prerequisites fail coverage rather than omitting the image.

Before the next authorized one-off scan, the release owner must select the reviewed Python 3.12 base used for the intended executor build and set repository variable `ADP_SECURITY_EXECUTOR_PYTHON_IMAGE` to its immutable image reference (`registry/repository@sha256:<64 hex digits>`). The workflow forwards it as `SECURITY_EXECUTOR_PYTHON_IMAGE` to both Grype and Syft CodeBuild jobs. A direct CodeBuild invocation must supply the same environment variable. There is no default tag or placeholder digest. Validation establishes digest syntax, not the base's Python version or release approval; selecting and reviewing the correct base remains a release prerequisite.

The supplied build arguments are recorded in coverage and per-artifact provenance. A future scan must verify all five Superplane targets, actual build results, immutable image IDs and usable scanner artifacts. Offline tests inspect real staging inputs and the Docker command contract, with Docker/AWS/scanners mocked; they are not image-build or scan evidence.

This source change does not set the repository variable, publish an image, alter release pins, start a scan or deploy anything. S21 #5620 and the security epics retain their final integration and live evidence requirements.
