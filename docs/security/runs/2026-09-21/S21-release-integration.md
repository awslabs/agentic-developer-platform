# S21 release artifact and scanner integration

This record promotes previously validated artifacts; it does not assert live deployment or whole-image vulnerability cleanliness. The final one-off scan and predecessor acceptance are still required for S21 closure.

| Component | Immutable artifact | Evidence |
|---|---|---|
| SkyPilot | `adp-superplane-skypilot@sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec` | S03 recipe reproduced;13 OCI layers uploaded unchanged; ECR manifest bytes hashed and matched on2026-09-25 |
| Controller | `adp-superplane-controller@sha256:d87cf6355d66432337aafbd3194f51b93b53744c0c1bae0f015bc0e156ce38ed` | S01 existing validation; ECR digest/tag re-read2026-09-25 |
| API | `adp-superplane-api@sha256:f1897a677fa7a5e21b7a1aa8c0ab6a2a03b37b66a98443a06b200d72f4a9b259` | S02 build35847435952 and advisory evidence; ECR digest/tag re-read2026-09-25 |
| Executor | `adp-superplane-executor@sha256:89504aee46365b40c67226ef73bb4224add695f27a0dfbb59ae57eeeddd91891` | Reviewed build36102456290, repository-root packaging, pinned Python3.12.14 base; ECR digest/tag re-read2026-09-25 |

Registry for these artifacts: `879318057152.dkr.ecr.us-east-1.amazonaws.com`. This is actual publication provenance, not an invented deployment account. A different deployment account requires reviewed pull access or digest-preserving replication.

SkyPilot publication receipt: `evidence/S21-skypilot-publication.json`. The new repository is immutable, AES256-encrypted, and selected by the lock's existing Terraform ECR discovery. Before the next control-plane apply in this existing account, adopt the published repository into the owning state with the reviewed import address `aws_ecr_repository.superplane["adp-superplane-skypilot"]`; do not recreate it. Its normal lifecycle policy is applied by that owning module. No control-plane apply was performed here.

The lock retains original build metadata for promoted images, so later builds still resolve their maintained source. The monitor remains explicitly pending final reviewed build acceptance; it is still mandatory in the five-image scanner inventory. The final source scan builds current maintained images, while these release pins identify the particular scoped artifacts whose S01/S02/S03 acceptance is recorded. Later source fixes are not claimed deployed by these older pins.

Four S20 `CKV_AWS_51` occurrences map to the shared platform ECR module. Its default now selects `IMMUTABLE`, consistent with the dedicated Superplane repositories. Explicit `MUTABLE` remains a visible compatibility override, not the default. This is a code-only default repair. Before applying to existing shared repositories, migrate any publisher relying on moving aliases such as `latest` to unique revision tags and digest-addressed consumption; changing the live setting without that coordination can reject subsequent pushes. No live shared repository settings changed.

The final scanner must use Bandit's YAML config through `--configfile`, not `--ini`. Global B101/B311 skips were removed so production occurrences remain individually visible. A pinned Bandit1.7.9 inert fixture confirms both rule results are emitted. The source inventory and on-demand-only workflow remain intact; no schedule was enabled.

Validation:157 release/build/startup/manifest tests pass,11 not-applicable tests skip;63 scanner-contract tests pass,2 environment-specific checks skip;3 mocked Terraform lock plans pass. Publication verifies exact bytes rather than trusting a tag. Existing source-specific S01/S02/S03 findings and current residuals remain available in their disposition records; the final scan must report its own new outcomes.

Private ECR runtime pulls authenticate with the existing scan role before Docker pull. The region is derived from the exact ECR registry hostname; login credentials are passed on stdin and never included in argv or scanner evidence. Public registries do not request ECR credentials. Authentication failure fails the target rather than falling back to another image.

The current all-source inventory contains 23 targets, including the five mandatory Superplane components and its additional CUDA acceptance fixture. Preflight repaired the parser and two cyber build contexts and admitted the two exact issue-authoring files already copied by the agent Dockerfile. No target was removed. The CUDA fixture and automation-infra image now receive explicit digest-pinned build inputs through the same validated transport as the executor. Missing, mutable or malformed bases fail their targets.

`evidence/S21-pytorch-base-provenance.json` records the official PyTorch organization's 2.8.0 CUDA 12.8 runtime manifest and linux/amd64 config hashes. `evidence/S21-runner-base-provenance.json` records the existing S14 runner recovery build selected by immutable ECR digest; it does not misidentify that recovery artifact as a new full runner build. These inputs make the selected recipes buildable for scanning; they do not assert vulnerability cleanliness or deploy either image. Private ECR build bases authenticate before Docker build as well as before external pulls.

Follow-up validation: 52 scanner/context/transport tests passed. They retain all targets, reject unpinned required bases, check each of the three base inputs before legacy shell transport, and cover failed registry authentication before pull. The final scan remains required.

Grype is now pinned to 0.119.0 in both the dispatch workflow and CodeBuild default. The former 0.80.2 binary uses schema 5, whose newest published database is dated 2026-03-09; it cannot establish current scan evidence on 2026-09-25. The selected release binary's SHA-256 was verified and reports schema 6. A fresh database update succeeded against the active 2026-09-25 feed. An inert CycloneDX fixture containing cryptography 42.0.7 produced six valid SARIF findings; the repository filter retained them and the existing severity resolver read three native HIGH results. This verifies tool/config/report compatibility, not repository cleanliness. See `evidence/S21-grype-tool-provenance.json`. Existing ignore rules were not changed.
