# Task UI and department scope candidate

Build-only evidence for `e93e592c7da5aa31cc3e882a7441815c547dd700`: verified Task Activity UI candidate
`8cfd14826112915ee984ebf3b38d2c0a65bedb4c` plus both dependent commits in #6313
(`1af276683` and `e07dd28ef`). No unrelated runtime source was added. The six scope
runtime files byte-match the reviewed #6313 head; the CLI and frontend byte-match
8cfd14826. All 33 focused scope/usage/budget/request tests pass; the ten affected
Python files pass Ruff lint and formatting.

- CodeBuild: `adp-dev-gateway-build:de39d1c8-7bf4-47e2-b746-24fea0f39992` — SUCCEEDED.
- Source: `s3://adp-terraform-state-000000000101/codebuild/src/adp-dev-gateway-build/e93e592c7da5aa31cc3e882a7441815c547dd700-1790396865-3663670.zip`.
- Downloaded source archive byte-equals Git; SHA256 `b897c2999a065facebce62ca969204427081a677a1a0be2e88a92ab58d6b6844`.
- Gateway: `sha256:5ca2b38120c45445c0a12d898819a5922db94bcc0f81604478dac9b523588d58`; manifest/config hashes, Linux amd64 and exact
  `GATEWAY_RELEASE=e93e592c7da5aa31cc3e882a7441815c547dd700` verified.
- Packaged CLI manifest byte-equals Git and retains `--tasks`;
  SHA256 `f234c035c4a971c5343dd5115253f7b60baaee400863203283af0720c5a6690f` verified from the digest-checked CLI layer.

The frontend is reused from its original build source
`0d6949f37ea33de2d6113302167737262bcea647`, ZIP SHA256
`1b1c7542de27fc9811e07307d205c60c5d8ef20a87185873967831c29b38a164`. Its 130-file artifact is not rebuilt or relabelled.
The frontend tree (`b0b26d69e5b65739f6be697fffcdf8da55cb046d`) and external build inputs are unchanged;
current dev SSM settings match the original build. The [machine receipt](task-ui-scope-candidate-e93e592c7.json)
records the exact changed paths and original provenance. Independent artifact review approved fresh AWS source/ECR identity, frontend
source-tree equivalence, all 130 ZIP file hashes and current SSM settings.

This candidate changes scope enforcement for department-admin reads, raw usage logs,
and canonical person budget/rate targets. It does not change worker permissions,
create model Tasks, deploy the gateway, publish frontend assets, or modify release
state. #6313 must pass and merge before rollout. Live Activity discovery also
requires the separately reviewed static gateway authority-table Query policy;
a successful image build does not establish that IAM prerequisite or live acceptance.
