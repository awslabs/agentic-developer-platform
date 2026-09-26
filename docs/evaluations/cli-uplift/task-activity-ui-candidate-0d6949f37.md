# Task Activity UI candidate

Build-only evidence for `0d6949f37ea33de2d6113302167737262bcea647`, combining the current direct-ID release
`41aea17eda2652576eeab70ec6c10d0d7fb739b9` with reviewed Task list/stream UI #6308
(`8fc5faa70`). The only cherry-pick conflict appended independent CLI tests; both
sets were preserved and all 62 combined CLI tests passed. Production UI/list files
are unchanged from the reviewed slice. Deployment requires #6308 to pass and merge.

- CodeBuild: `adp-dev-gateway-build:9042bafa-77f7-4535-93f9-be864d485e69` — SUCCEEDED.
- Source: `s3://adp-terraform-state-879318057152/codebuild/src/adp-dev-gateway-build/0d6949f37ea33de2d6113302167737262bcea647-1790394738-3619594.zip`.
- Downloaded S3 archive equals the source commit's Git archive byte for byte;
  SHA256 `a75cce1ea4ec1b8a9627486659848cd361ab033cef68c85aaf94fac499de9959`.
- Gateway image: `sha256:f987919546a32077779204be0bce75462a4ea8015885ef41bd70e0a246ba8866`. Manifest/config hashes, Linux amd64,
  and `GATEWAY_RELEASE=0d6949f37ea33de2d6113302167737262bcea647` verified.
- Frontend: canonical gateway workflow build (`npm ci`, `npm run build`) with
  current dev SSM VITE settings; settings hash `4851831fc9cd6ff6f5e114ca0ce8462b62e531d5d48a31556751996dfdd7aa0f`.
- Frontend archive SHA256: `1b1c7542de27fc9811e07307d205c60c5d8ef20a87185873967831c29b38a164`; 130 files,
  individually hashed in [the frontend receipt](task-activity-ui-frontend-0d6949f37.json).
  The artifact is prepared locally and has not been published.

The candidate adds owner-only new Task discovery, `adp agent list --tasks`, Activity
Task stream links, retained-report labels and stricter E42 replay evidence. The
admission transaction remains in the gateway; the forwarding edge Lambda is unchanged.
Historical Tasks remain direct-ID-only. No worker IAM, Terraform, live configuration,
model calls, or deployment changes were made during preparation. The current
release state was not modified. Independent root review approved the artifacts after fresh AWS archive/ECR checks,
all 130 frontend file hashes, and 223 frontend source-map entries matched to Git.
