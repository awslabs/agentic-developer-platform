# Task and Activity gateway candidate

Build-only evidence for `41aea17eda2652576eeab70ec6c10d0d7fb739b9`, combining reviewed integration #6282, Task fixes #6298/#6300 and the owner-only Activity direct-ID bridge #6305. Deployment and live acceptance remain separate.

- CodeBuild: `adp-dev-gateway-build:0b57cce5-ba8f-4a91-aaec-15a8c292fd96` — SUCCEEDED.
- Source: `s3://adp-terraform-state-000000000101/codebuild/src/adp-dev-gateway-build/41aea17eda2652576eeab70ec6c10d0d7fb739b9-1790394129-3592824.zip`.
- Fresh downloaded archive equals `git archive --format=zip` of the source commit byte for byte; SHA256 `7ee97f6fda84678ee2e5791ca3e88793d590f9a5c9fb29760c3c204f8f4c406c`.
- ECR image: `sha256:1a0eebffcb113f32dbd5f6afce3b14d3de0171fb3d0c6c266b9c49454ac06c67`; immutable source-SHA tag and manifest/config digests verified.
- Image environment `GATEWAY_RELEASE=41aea17eda2652576eeab70ec6c10d0d7fb739b9`; Linux amd64.
- Combined verification: 45 Task/Activity tests and 796 regression harness tests passed; all 55 workflow-scoped files passed Ruff lint and format.

The bridge resolves a returned invocation UUID through current Task ownership and authorization. It does not add Activity list/UI projection or legacy controls. The original failed Claude Task remains immutable; this receipt does not imply a successful rerun, new probe or counter reset.
