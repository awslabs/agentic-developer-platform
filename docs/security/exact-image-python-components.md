# Exact-image Python component review

The optional `--components` argument extends the exact-image collector with two
narrow Python providers. It does not change default v1 Debian collection,
scanner output, the empty baseline, the Critical/High threshold, or the external
independent approval requirement. This is verifier support, not an approval of
an image or its findings.

The supported layouts are CPython 3.10 under `/usr/local` and a single installed
`cryptography` distribution in its standard `site-packages`. Other runtimes,
editable installs, unknown providers, ambiguous package ownership, embedded
package notes and shared SARIF rules remain unsupported for disposition. The
providers recognize only the six advisories listed in
`codebuild/exact_image_components.py`; a new advisory needs a separate reviewed
scope change. Vendor-disputed and risk-accepted are not decision statuses.

## Binding manifest

Pass a relative file inside the evidence bundle:

```bash
python3 codebuild/exact_image_disposition.py collect \
  --bundle /path/to/bundle --components components.json \
  --image registry.example/repository@sha256:EXACT_PLATFORM_DIGEST \
  --source-revision FULL_REVIEWED_COMMIT
```

Replace the illustrative digest and revision with independently selected values.
`components.json` has exactly `schema: adp-python-component-bindings/v1` and a
nonempty `bindings` list. Each binding has exactly these fields:

| Field | Required content |
|---|---|
| `provider` | `cpython-runtime/v1` or `python-installed-distribution/v1` |
| `package` | Exact SBOM `id`, `name`, `version`, `type`, and `purl` |
| `producer` | Retained `archive`, immutable `platform_digest`, maintained `source_revision`, and `transition` |
| `evidence` | Bundle-relative `source`, `build`, `runtime`, and `independent_review` artifacts |
| `dependencies` | Root-dpkg dependency claims, each with `package_purl`, retained DEB `artifact`, and exact library `paths` |

The provider code determines the complete required file scope. The manifest
cannot select a convenient subset. It inventories interpreter, standard library,
both XML adapters, actual cryptography code/native extension, installed metadata,
and relevant import controls as applicable. It checks the entire corresponding
producer scope, not only the files named by a vulnerability report. Dependencies
must resolve to the actual package-owned file and match its retained DEB payload.
CPython requires the exact system Expat library; cryptography requires the exact
system OpenSSL libraries. Dependency versions alone do not establish a fix.

Cryptography metadata comes from actual image bytes. Its unique METADATA must
match the scanner identity; WHEEL must describe the supported native layout;
RECORD must account for every installed component file with exact SHA256 and
size, except its standard unhashed self row. Duplicate, escaping, stale,
unhashed or overlapping payload entries fail. The two conventional top-level
RST files are allowed only with unique ownership. Unrelated code cannot become
cryptography-owned merely because a RECORD claims it.

## Producer evidence and finite transitions

The verifier reconstructs the producer OCI archive and validates every blob and
diffID. Source, build and independent review evidence are mandatory and hash-bound
alongside that archive. A producer's revision label, when present, must agree;
an absent historical label does not itself invalidate independently established
source-to-output evidence. **A label, immutable digest or build log alone does
not authenticate a source-to-output chain.** The independent reviewer must
establish that chain before supplying an approval pin.

The `independent_review` artifact is JSON with exactly:

- `schema: adp-producer-source-review/v1`;
- the verified `archive_sha256`, `platform_digest`, `config_digest`, and
  `source_revision`;
- `source_evidence` and `build_evidence`, naming the same retained source/build
  artifacts as the binding;
- a nonempty `reviewer` and `conclusion: source-output-bound`.

This is an explicit review claim, not a machine-generated attestation. It has
no approval effect until the separate reviewer pins the complete final review
receipt externally. Relabeling or repacking the candidate does not establish
this evidence. The tool rejects directly substituting the candidate archive;
the reviewer must also reject disguised repacks with no actual build chain.

Only two producer transitions are supported:

1. `identical/v1`: all computed component and import-context files agree exactly.
2. `remove-bytecode/v1`: a finite virtual transformation removes only CPython
   3.10 caches under the standard library, cryptography and the supported
   distutils hook, each with an existing regular Python source sibling. It
   removes exactly their RECORD rows and preserves every other RECORD byte.
   No source, native extension, dependency or unrelated metadata change is
   permitted by this transition. Removed cache identities and source identities
   remain in the observation.

The final component must contain no `.pyc` or `.pyo`. The transition does not
claim that historical bytecode implements the reviewed source. Orphan caches,
unknown cache tags and source changes fail. A cache cleanup must be a separately
reviewed image change with ordinary runtime verification and a new scan; this
collector never mutates an image. Deterministic bytecode verification is outside
this version's scope.

## Default runtime context

A runtime record has schema `adp-python-runtime-evidence/v1` and exactly these
additional fields: final `platform_digest`, `config_digest`, `source_revision`,
`artifact_id`, `interpreter`, `imports`, `libraries`, `invocation`, and `execution`.
The interpreter and import entries carry exact paths and hashes; library entries
carry the corresponding resolved installed-file records. `invocation` references
the retained command/output evidence inspected by the independent reviewer.

`execution` records the actual `argv`, image `user`, `working_directory`,
`sys_path`, `isolated`, `no_site`, and `environment_overrides`. It must describe
the ordinary interpreter invocation with default import behavior, no isolated
mode or site bypass, no environment overrides and the supported default search
path. An isolated probe cannot stand in for product runtime behavior. The
provider rejects image-level Python/loader overrides, zipped standard libraries,
competing code in the working directory, unknown active `.pth` files and
site/user customization hooks. The one supported distutils path hook is bound
by its exact content plus its full implementation and producer context.

The collector does not execute images or trust JSON as proof that a command ran.
The independent reviewer verifies the retained invocation, loaded module origins,
actual consumers, code-selection context and source/build evidence. Runtime
records from another image or source revision fail mechanical checks.

## Review and gate

Component collection produces `adp-exact-image-observation/v2`. It includes full
component inventories, dependencies, import context, producer transitions and
all input hashes. Its receipt must use `adp-exact-image-review/v2`; schemas cannot
be mixed. The existing exact occurrence/status/evidence structure still applies.
A component decision's `files` must equal the complete computed owned inventory.
Its receipt must retain every component input at the observed hash. `fixed`
uses the verified producer archive as `package_artifact`; `not_affected` keeps
that field null but still requires the full producer and provenance binding.

Use the same `--components` manifest during `derive`, `verify` or `gate` so the
collector reconstructs the evidence again. The external approval pin remains a
command-line trust input. Shared rules, unknown findings and embedded ELF notes
cannot inherit a component decision. A change in source, image, dependency,
scanner, evidence or import context requires new collection and review.

CVE-2023-36632 is vendor-disputed in existing evidence. This implementation does
not turn that description into `fixed` or `not_affected`. Without an independently
supported allowed decision, it remains an active High and the unchanged gate
fails. Supporting the other five findings does not promise a six-finding pass.

Tests use harmless synthetic images and review records. No real dispositions or
release approvals are created by the test suite or the new workflow coverage.
