# Security tools follow-up

The tools run 36393031888 successfully authenticated using the previously restored
`adp-dev-trusted-scan` setting. It scans merged source `02199115ab`. Security Agent
run 36392994870 reviews the same source. Both runs were still active when this
source repair was prepared; partial results are not a complete scan verdict.

Bandit reported 34 High findings. The coding-task snapshot verifier now compares
submitted content with the actual base64-decoded bytes from the authenticated
GitHub contents API at the pinned revision. Matching Git SHA-1 IDs alone cannot
establish content equality. Missing, malformed, mismatched, or unsupported content
responses fail closed. Git blob IDs remain protocol compatibility checks.

Other repaired SHA-1 calls compute existing Git/scanner-format identifiers, not
password hashes, signatures, or authentication tokens. `usedforsecurity=False`
records that purpose without changing the algorithm or stored identifiers. Native
source attestations continue to bind all bytes through SHA-256. The attached
finding map retains every original High location.

Validation: 219 snapshot-authority and security-evidence tests passed, including
matching object IDs with different content, missing content, invalid base64, and
unsupported encoding. Native source blob/tree IDs still match Git for files and
symlinks. A rerun with the workflow's Bandit 1.7.9 and configuration reports six
High findings: five controlled tarfile regression calls and Debian md5sums. No
baseline refresh, rule skip, or inline suppression was added. Those six raw
findings remain visible; this change does not assert a zero-High tools result.

Container findings are independent. Initial raw reports include codegraph
24 Critical/94 High and context-mcp 0 Critical/50 High matches. These are newly
built scan images, not a deployed-image attestation. OS-package remediation remains
subject to the automatic approval rejection recorded in the September 27 handover;
no blocked package build or live rollout was attempted here.

The running Grype job also exposed private-base pull failures (401). The scanner
previously logged in only for direct pull targets and build-argument images;
literal private ECR references in Dockerfiles were omitted. The scan roles already
permit pull access. The scanner now logs in for literal registry references before
building, passes passwords only on stdin, and refuses to build on login failure.
Scanner validation: 63 passed, 1 skipped. The source fix requires a new scan;
the already-running scan cannot acquire it retroactively.

The corrected run 36394633883 confirmed the ECR login repair and Bandit's six
remaining High results. It also exposed a source-archive mismatch: discovery
includes 40 targets, but deployment packaging omitted the tracked docs/ recipe.
Scanner CodeBuild jobs now archive the exact Git commit, including that recipe;
deployment jobs retain their existing archive mode. An archive parity check
confirmed every discovered target is represented. Scanner tests: 67 passed,
1 skipped, including committed-versus-dirty bytes and scanner-only mode selection.

Two pinned historical input manifests had disappeared from ECR. Exact local
copies were restored under retention tags without changing their digests or
rebuilding packages: ingestion `5c96c50f...` and API `9e194dfb...`. They are scan
inputs, not newly deployed images. The ingestion target had already failed before
restoration, so that run still requires a rerun; its failure is not concealed.

Reconciliation also now recognizes the pinned Checkov publisher's
`results_sarif.sarif` filename, rejecting missing or competing reports. The image
receipt verifier accepts the producer's extended provenance only after checking
its exact field set, build-input binding, and raw-report, suppression-summary and
scanner-metadata hashes against actual files. Legacy five-field provenance remains
supported; claiming extended evidence requires its full validation. No extra
fields are silently discarded. Contract tests: 46 passed, 2 skipped, including
altered evidence, mismatched build inputs, unknown fields and stripped provenance.

Further observed build failures were repaired without dropping targets: the
Codex detached-check fixture receives the existing reviewed Python base through
`BASE_IMAGE`, and Cyber tools use the repository root required by their COPY
instructions. The fixture passed its real read-only, network-disabled acceptance
check and rejected changed input. Scanner tests: 71 passed, 1 skipped.

Docker Hub returned 429 for the Python base. The scan's executor input now uses
`public.ecr.aws/docker/library/python` with the exact same manifest digest;
byte equality was verified. The Cyber browser uses a pinned AWS public Python
3.13-slim image and builds successfully. Cyber workers' signify project index
returned 503; the existing 0.9.2 release is now pinned directly to its verified
PyPI wheel hash. The complete worker image builds, parser imports succeed, and
41 worker validation/script-guard tests pass. No dependency was downgraded.

Syft run 36394633883 reached its 60-minute CodeBuild timeout while building the
acceptance workload. Its project timeout is now 90 minutes in Terraform and in
the live scan project, matching Grype and remaining within the 105-minute GitHub
job bound. After integrating main's new AgentCore/browser build contexts,
scanner tests pass (72 passed, 1 skipped), receipt tests pass (46 passed,
2 skipped), and the combined Cyber browser image builds successfully.

The diagnostic run also exposed a Validation Tools build-context mismatch and
a Docker Hub 429 for the SkyPilot Java-test base. Validation now builds from
the repository root; the Java base uses a public ECR mirror with byte-identical
manifest digest. Cyber's iocextract index independently returned 503 in both CI
legs; the same release is pinned to its verified PyPI wheel. The worker builds
and iocextract/signify imports and URL extraction pass. Scanner tests now pass
73 cases (1 skipped).

Parser's raw image report identifies three High Node matches at 22.23.0 and
one High x/text match in SCIP Python's embedded TypeScript compiler. The recipe
now uses official Node 22.23.3 and ingestion's reviewed native compiler/tool
build. The resulting non-root, network-disabled image indexes Python, TypeScript
and Go fixtures successfully (844, 722 and 445 byte SCIP outputs). Its five
stdlib regression checks also pass. A fresh Grype scan is still required to
confirm the remaining raw count; no suppressions were added.

The diagnostic scan exceeded 70 minutes despite eight build targets failing
early. Restored builds add work, so the final proposed bounds are 150 minutes
per image CodeBuild project and 165 minutes per GitHub job, preserving time
for receipts and cleanup. This supersedes the initial Syft-only 90-minute fix.
The dispatch must use a main workflow definition containing these bounds.
