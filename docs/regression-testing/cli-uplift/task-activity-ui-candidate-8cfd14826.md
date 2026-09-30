# Corrected Task Activity UI candidate

This build-only candidate supersedes `0d6949f37` / `sha256:f987919546a32077779204be0bce75462a4ea8015885ef41bd70e0a246ba8866`,
which omitted the new `--tasks` flag from the checked CLI command manifest.
The corrected #6308 source adds that flag to the manifest and command inventory;
all 43 command-manifest/capability contracts pass on both the source branch and
combined candidate. There is no new dispatch, frontend behavior or Task API change.

- Source: `8cfd14826112915ee984ebf3b38d2c0a65bedb4c`.
- CodeBuild: `adp-dev-gateway-build:beba7c18-17a4-479f-a1e9-608432f481d5` — SUCCEEDED.
- Source archive: `s3://adp-terraform-state-000000000101/codebuild/src/adp-dev-gateway-build/8cfd14826112915ee984ebf3b38d2c0a65bedb4c-1790395072-3631035.zip`.
- Archive SHA256: `d963c97c8c36c7b8e3f65b0b8a6463ea46882cfe00b2fc1c7a24e5f492c53b69`; downloaded bytes equal Git archive.
- Image: `sha256:e8f3ecf087822bb694981eeb489f941c3808b67501eb3960101084ea15e1e04c`; manifest/config hashes verified,
  Linux amd64, exact `GATEWAY_RELEASE=8cfd14826112915ee984ebf3b38d2c0a65bedb4c`.
- Actual packaged `app/cli/command-manifest.json` byte-matches Git and includes
  `--tasks`; SHA256 `f234c035c4a971c5343dd5115253f7b60baaee400863203283af0720c5a6690f`. The bounded CLI layer was
  downloaded and its own digest verified before inspecting this file.

The frontend ZIP retains its original build source `0d6949f37ea33de2d6113302167737262bcea647`
and SHA256 `1b1c7542de27fc9811e07307d205c60c5d8ef20a87185873967831c29b38a164`. No frontend rebuild occurred.
The frontend Git tree is unchanged (`b0b26d69e5b65739f6be697fffcdf8da55cb046d`), and the entire
repository diff changes only the CLI manifest and command-inventory markdown,
so external frontend build inputs are also unchanged. Current dev SSM settings
match the original build. Original 130-file and 223-source-map review evidence
remains in the original frontend receipt; provenance is not relabelled.

The [machine receipt](task-activity-ui-candidate-8cfd14826.json) records the full
replacement and frontend-reuse evidence. Independent review approved fresh AWS archive/ECR identity, all 130 frontend
file hashes, source-tree equivalence and matching current SSM settings. No deployment
or frontend publication was performed. #6308 must pass and merge before rollout.
