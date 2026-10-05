# Usage Task lookup and Codex help release — 26 September 2026

Gateway source `37af0ea914a85274ea6c5e0010c57251eeabc5a7` is deployed in dev. Image `000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:ace7376985bb5716b308491e20870a8e84011ae79be726e9bb9559978d1d63ed` contains the reviewed Task usage readthrough fix (#6334) and the Codex-generated tenant help patch (#6333).

The usage `--run` filter previously returned 404 for an owned Task invocation even when its usage rows existed. It now reuses the existing owner-authorized Task Activity adapter and preserves exact tenant/run ledger filtering. Native Activity and managed-tenant authorization remain unchanged. Independent gateway tests passed 59 cases, including foreign owner/tenant, revoked policy, stale generation and storage failure. All applicable PR CI passed before merge.

CodeBuild `adp-dev-gateway-build:31d7090a-b527-4fba-8373-45bfb0fbb166` succeeded. Independent verification matched the exact git archive (SHA-256 `2759df29da79f68eeb542dac629541176f6e3d620e981028f94a3c5eabedadd1`), ECR manifest/config and packaged runtime files to source. Migration job `gateway-migrate-68b843ed5900` completed before the guarded image-only rollout.

All seven current gateway replicas were verified ready on the actual image digest. Both worker allowlists retain the same 13 digests. The scheduled engine was synchronized and verified against the same gateway image. All 35 served CLI file hashes match source; health returned 200. The worker image/service account/reduced IAM role and frontend were unchanged. Deployment state was saved only in the isolated maintenance worktree.

Nightly regression now has explicit existing-workspace and optional Task-invocation fixtures for E21 (#6335/#6338), and the parent nightly workflow forwards optional non-secret fixture JSON through the existing child validator (#6339). The default scheduled suite remains unchanged. The cancellation harness UUID contract fix (#6336) is also merged; the original failed run remains retained separately.

Fresh combined evaluation `adp-e2e-20260926-055844-1ef044`, [workflow 36222363812](https://github.com/aws-e/adp/actions/runs/36222363812), was dispatched after release verification. Its live acceptance outcome is recorded separately; this release receipt does not assert that it passed.

The first combined evaluation attempt (36222363812) stopped in preflight: the new digest was missing from the maintained `gateway-deployment-receipts.json` catalog. The release itself and packaged source had been independently verified, but this separate machine-readable record was omitted. Durable state records exactly `Gateway digest has no reviewed build receipt`; both E21/E42 were not run. No EC2, diagnostic intent or Task was created, and cleanup completed. The catalog now includes the verified receipt; the failed attempt remains retained. Artifact ID `10899691121`, SHA-256 `2ca6c0ef53afa4868b3c550e9d23ffb636a8c78a847046ed3f80ecbe8b3555d5`.
