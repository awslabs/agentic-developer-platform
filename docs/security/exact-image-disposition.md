# Exact-image security review tooling

`codebuild/exact_image_disposition.py` is an opt-in review tool. It is **not
connected to release workflows** and contains no real vulnerability decisions.
It does not change scanner policy, severity thresholds, baselines or ignore
files. A parent/integration review must approve wiring before release use.

The tool retains raw Grype JSON and SARIF findings. It can derive a separate
SARIF with `external`/`accepted` annotations on only exact independently approved
occurrences. Reports count **raw**, **fixed**, **not affected** and **active**
findings separately. A derived gate pass is not a raw zero-vulnerability scan.

## Trust inputs

Run trusted, reviewed code. Supply the expected repository/platform digest,
source revision and approved receipt SHA256 from the release authority, outside
the candidate bundle. Do not obtain the approval pin from a candidate-generated
file or substitute an `approved: true` field. The reviewer identity in the
receipt is informational: the independently supplied hash is the trust boundary.
The release authority must review the exact evidence and full receipt before
pinning its canonical JSON SHA256 (UTF-8, sorted keys, compact separators).

Retain authenticated source/patch and independent review evidence. Report hashes
bind the reports reviewed; report metadata does not prove a scanner actually
ran. The trusted collection process must also retain and review scanner command,
version, executable provenance, database identity and invocation receipts. This
tool does not manufacture those attestations or turn self-assertions into trust.

Use an OCI image archive that contains the actual registry platform manifest and
config blobs. The platform digest is distinct from the index, archive and config
digests. Docker archives whose reconstructed manifests differ from the registry
manifest are deliberately unsupported; export an OCI archive without inventing
registry identities in SBOM metadata.

## Bundle

`inputs.json` maps these exact keys to retained paths relative to the bundle:

```json
{
  "archive": "image.oci.tar",
  "sbom": "syft.json",
  "raw_json": "grype.raw.json",
  "raw_sarif": "grype.raw.sarif",
  "scanner_binary": "tools/grype",
  "scanner_database": "scanner/vulnerability.db",
  "scanner_config": "scanner/effective-config.json"
}
```

The effective config must equal the native report's descriptor configuration.
Generate native JSON and SARIF with the same scanner version/config/database.
Every reviewed Debian occurrence must map uniquely to SARIF through advisory
namespace, exact package PURL and package/version/type/severity metadata, and
the native package must match the SBOM artifact. The tool refuses missing
results, duplicate native bytes, unexplained SARIF suppressions and native
ignored matches. It requires one Grype **0.119.0** SARIF 2.1.0 run; another
presenter version requires separate review.

Grype 0.119.0 shares a rule across matches with the same advisory/package name,
including system/virtual-environment copies or different package versions, but
emits one result for every native match. Shared rules are retained as complete
unresolved groups. The collector binds every native member and validates the
complete multiset of image path/layer logical locations against the results.
All shared groups remain active and **ineligible for disposition**, even when
individual locations differ. Rule metadata must describe a real group member;
it is never transferred to every other member. Effective gate severity must
cover every member's native severity, so shared metadata cannot hide a native
High/Critical finding behind a lower band.

The pinned presenter's `severityText` function renders native Negligible and
Unknown as SARIF low. This behavior is retained explicitly for active/ineligible
entries only; the original native bands remain in accounting. An annotation
still requires exact native/effective severity equality on a unique Debian
occurrence. Summaries expose `native_raw`, `sarif_raw`, `native_active`, native
severity counts and shared-rule group count separately; no group-wide
suppression is supported. These semantics follow the version-matched upstream
[SARIF presenter](https://github.com/anchore/grype/blob/v0.119.0/grype/presenter/sarif/presenter.go)
and [Syft location model](https://github.com/anchore/syft/blob/v1.52.0/syft/file/location.go).

OCI collection verifies manifest/config/compressed layer hashes and sizes plus
config diffIDs. It reconstructs a file inventory in memory without extracting
paths to the host. Package identity and ownership come from the final image's
dpkg status and package `.list` files, cross-checked against the SBOM. Every owned
file is recorded, including absent paths removed from minimal container images.
Symlink resolution is bounded; hardlinks capture bytes at creation time.
Whole-filesystem and complete package inventories are part of the approved
observation. This release supports Linux OCI archives with gzip/uncompressed
layers. Unsupported compression, special files, duplicate layer paths and writes
through symlink parents fail closed.

```bash
python3 codebuild/exact_image_disposition.py collect \
  --bundle /path/to/bundle \
  --image registry.example/repository@sha256:EXACT_PLATFORM_DIGEST \
  --source-revision FULL_REVIEWED_COMMIT > observation.json
```

The placeholder digest/commit above must be replaced with real, independently
selected values. The tool rejects placeholders and mutable tags.

## Review receipt

The review receipt uses this shape (illustrative placeholders, not approval):

```json
{
  "schema": "adp-exact-image-review/v1",
  "observation_sha256": "CANONICAL_COMPLETE_OBSERVATION_SHA256",
  "reviewer": "independent reviewer identity",
  "evidence": {
    "review/applicability.md": "ACTUAL_FILE_SHA256",
    "review/independent-review.md": "ACTUAL_FILE_SHA256"
  },
  "decisions": [{
    "native_sha256": "EXACT_NATIVE_MATCH_CANONICAL_SHA256",
    "status": "not_affected",
    "rationale": "Specific independently reviewed reason for this exact image",
    "evidence": {
      "applicability": "review/applicability.md",
      "independent_review": "review/independent-review.md"
    },
    "files": {
      "/actual/package/owned/file": {
        "resolved": "/actual/resolved/file",
        "kind": "file",
        "sha256": "ACTUAL_INSTALLED_BYTES_SHA256",
        "mode": 420,
        "size": 123
      }
    },
    "package_artifact": null
  }]
}
```

Copy file entries from the collected package inventory without editing them.
For `fixed`, evidence roles must be exactly `source`, `patch`, `regression`,
`build`, `independent_review`; `package_artifact` must refer to a retained and
hash-bound `.deb`. The tool inspects its metadata and payload with `dpkg-deb`
without installing or running maintainer scripts. The package name/version/arch
and each selected installed file must match. `not_affected` requires exactly
`applicability` and `independent_review` roles and a null replacement artifact.
The independent reviewer evaluates whether the evidence actually supports the
decision; the tool enforces byte identity, required evidence and decision scope.

No wildcard, risk-acceptance, affected or under-investigation dispositions are
supported. Unreviewed occurrences remain active. Decisions cannot transfer to a
new image, source, file inventory, SBOM, scanner/config/database or raw report
without a new exact observation and approval. Non-Debian packages remain active;
their disposition support is outside this implementation's scope.

## Derive, verify and gate

`derive` requires the external `--approved-receipt-sha256` and a new `--derived`
output path. It uses exclusive creation to prevent overwriting existing reports.
`verify` recollects all inputs, rederives the expected report and rejects any
additional alteration in the supplied `--derived` report.

The intended release entry point is **`gate`**. It recollects and validates the
receipt, generates SARIF internally, then runs the repository's existing
`diff_security_findings.py --fail-on critical,high` against its maintained empty
baseline. It refuses a `--derived` input: an arbitrary annotated report cannot
be offered as the release gate input. It returns the gate exit code and includes
the gate summary plus exact image/receipt/observation/derived-report identities.
No baseline update path is exposed. The gate refuses a nonempty Grype baseline.

```bash
python3 codebuild/exact_image_disposition.py gate \
  --bundle /path/to/bundle \
  --image registry.example/repository@sha256:EXACT_PLATFORM_DIGEST \
  --source-revision FULL_REVIEWED_COMMIT \
  --approved-receipt-sha256 INDEPENDENTLY_APPROVED_RECEIPT_SHA256
```

Release integration must pin the deployed image to the verified digest and use
this complete entry point. Directly submitting annotations to the older generic
SARIF gate bypasses receipt validation and is not an approved integration. The
tool does not deploy or authorize AWS actions.

## Validation

`codebuild/tests/test_exact_image_disposition.py` uses harmless synthetic image,
package and report fixtures. It exercises exact collection, raw preservation,
fixed/not-affected accounting, cross-image and changed evidence rejection,
duplicate/omitted occurrences, existing suppressions, package-byte verification,
refusal to overwrite raw reports, forged derived reports and the actual existing
gate. Unreviewed High findings still fail that gate.

The existing Script Tests workflow executes these synthetic tests when the
verifier source or tests change. This is CI test coverage only; it does not
apply real review receipts or add the tool to any release/security gate path.

No real findings have been dispositioned by these tests.
