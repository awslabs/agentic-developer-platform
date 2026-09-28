# S03 — SkyPilot replacement image and bundled-package disposition

Work package **S03** of the 2026-09-21 security scan (parent #5599, issue #5602).

## Result

Use the reproducibly derived linux/amd64 image with manifest digest:

```
sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec
```

The image preserves the compatible SkyPilot 0.12.3 base and replaces only the complete
setuptools-owned site-packages trees with setuptools 81.0.0. This is the minimum setuptools
release that bundles fixed jaraco-context and wheel copies. The exact OCI recipe is
`modules/domain-apps/superplane/images/skypilot/build_oci.py`; complete hashes and scan results
are in `evidence/S03-derived-image-provenance.json`.

| Source finding | Baseline | Exact replacement | Disposition |
| --- | --- | --- | --- |
| GHSA-58pv-8j8x-9vj2 | setuptools-vendored jaraco-context 5.3.0 | 6.1.0 in setuptools 81.0.0 | **Fixed** |
| GHSA-8rrh-rw8j-w5fx | setuptools-vendored wheel 0.45.1 | 0.46.3 in setuptools 81.0.0 | **Fixed** |
| GHSA-72hv-8253-57qq | jackson-core 2.16.1 in Ray 2.54.0 | 2.18.6 in Ray 2.55.1 | **Fixed** |
| GHSA-r6ph-v2qm-q3c2 | cryptography 43.0.3 | 46.0.5 | **Fixed** |
| CVE-2023-36632 | Python 3.10.19 CPE match | unchanged | **Disputed/not applicable; no fix listed** |

No S03 source finding is suppressed, ignored, or baselined away. The disputed Python result
remains visible; unrelated pre-existing global rules are enumerated in the rescan section.

## Why a derived image is required

A maintained upstream image was evaluated first. SkyPilot 0.12.3 is the compatible upstream
upgrade: it fixes cryptography and the Jackson copy nested inside `ray/jars/ray_dist.jar`, keeps
the server/client contract unchanged, and passed live state-store validation. SkyPilot 0.13.0
fixes no additional issue finding and changes the minor release line.

Both upstream images still ship setuptools 78.1.1. Its private copies remain vulnerable even
when the similarly named top-level packages are already fixed:

```
/usr/local/lib/python3.10/site-packages/setuptools/_vendor/jaraco.context-5.3.0.dist-info/
/usr/local/lib/python3.10/site-packages/setuptools/_vendor/wheel-0.45.1.dist-info/
```

The original recommendation to accept those copies as unreachable did not satisfy the story's
requirement to verify fixed copies inside `setuptools/_vendor`. The replacement therefore derives
from the immutable 0.12.3 index and amd64 manifest and installs setuptools 81.0.0 as one additive,
normalized OCI layer.

## Reproducible build and provenance

The builder verifies every downloaded byte before use:

- Base index: `berkeleyskypilot/skypilot@sha256:0c0c0db86ee31559f9c98f2331b7d91ee84b61e7ab97bb6cbcd89938c89d37e4`
- Base amd64 manifest: `sha256:46d3886317e1c38ac4d21c4b4c335d93d0320433dee86df985f396de8a9bee8f`
- setuptools wheel: 81.0.0, size 1,062,021,
  `sha256:fdd925d5c5d9f62e4b74b30d6dd7828ce236fd6ed998a08d81de62ce5a6310d6`
- Added compressed layer:
  `sha256:9511a164bddb468ecb3e0b83c6fb087b7b541c4f2f87ed274fd642d850c60605`
- Added layer diff ID:
  `sha256:9df9d434bbe595c81e2ee7cf3aab506bf667393b482ad33cc86ba0e03de632d2`
- Resulting config:
  `sha256:d05a57076d2c5c277b280d37d938716cedc908c25ae4e71931789ca7f7e0ef87`
- Resulting manifest:
  `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`

The layer uses OCI opaque whiteouts for `_distutils_hack`, `pkg_resources`, and `setuptools`,
plus a whiteout for `setuptools-78.1.1.dist-info`. It then installs only the verified wheel
contents. This prevents stale files from the vulnerable distribution surviving underneath the
new files. File ownership, modes, timestamps, gzip metadata, image history, JSON encoding, and
input order are normalized. The builder also refuses the wheel unless:

- the jaraco context implementation hashes to
  `sha256:6ebd727581a8d57aff3eed5a9ee11d77e42b8d48b3fc46392ffe44e02d372f8c`,
- vendored wheel metadata says 0.46.3, and
- the final manifest equals the recorded digest.

Two builds in independent empty directories produced byte-identical `index.json` and
`provenance.json` and the same manifest digest.

```bash
python3 modules/domain-apps/superplane/images/skypilot/build_oci.py /tmp/S03-skypilot-oci
# sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec
```

## Vendored-copy verification

Syft 1.11.1 inventoried the exact derived OCI manifest:

```
setuptools      81.0.0  /usr/local/lib/python3.10/site-packages/setuptools-81.0.0.dist-info/METADATA
jaraco-context   6.1.0  /usr/local/lib/python3.10/site-packages/setuptools/_vendor/jaraco_context-6.1.0.dist-info/METADATA
wheel           0.46.3  /usr/local/lib/python3.10/site-packages/setuptools/_vendor/wheel-0.46.3.dist-info/METADATA
jackson-core    2.18.6  /usr/local/lib/python3.10/site-packages/ray/jars/ray_dist.jar
cryptography    46.0.5  /usr/local/lib/python3.10/site-packages/cryptography-46.0.5.dist-info/METADATA
```

The old 5.3.0 and 0.45.1 locations are absent from the merged image inventory. Grype reports none
of GHSA-58pv-8j8x-9vj2, GHSA-8rrh-rw8j-w5fx, GHSA-72hv-8253-57qq, or
GHSA-r6ph-v2qm-q3c2. This verifies the private copies rather than inferring remediation from a
top-level package version.

The executable traversal reproduction remains in
`evidence/S03-jaraco-context-traversal-check.py`: the old vendored code escapes the extraction
destination, while the 6.1.0 implementation raises `OutsideDestinationError`.

## Exact replacement rescan

Commands (run outside the repository so `.grype.yaml` cannot hide a match):

```bash
syft oci-dir:/tmp/S03-skypilot-oci -o syft-json
cd /tmp
GRYPE_DB_AUTO_UPDATE=false grype oci-dir:/tmp/S03-skypilot-oci -o json
```

Versions were Syft 1.11.1 and Grype 0.80.2. The Grype database was built
2026-03-09 and had checksum
`sha256:a65e27aecbbb2cd6671f5da84c16db7e9c60f0114075e6ae9bcc71f466460a0c`.
The unsuppressed exact replacement scan produced **1 critical / 13 high / 33 medium / 11 low /
512 negligible / 10 unknown** occurrences. The complete occurrence list, including every path,
is in `evidence/S03-derived-image-provenance.json`.

| Severity | Advisory | Package occurrence(s) | Exact-replacement disposition |
| --- | --- | --- | --- |
| Critical | CVE-2025-68121 | Go stdlib 1.24.11 in `/usr/local/bin/kubectl` | Inherited from 0.12.3; fixed Go versions exist. Existing package-wide global ignore also applies here; S21 owns that rule and kubectl refresh. |
| High | CVE-2025-61726 | Go stdlib 1.24.11 in `kubectl` | Inherited; fixed Go versions exist. Existing S21-owned global stdlib disposition. |
| High | CVE-2025-61731, CVE-2025-61732 | Go stdlib 1.24.11 in `kubectl` | Build-time malicious-cgo issues in a prebuilt runtime binary; inherited and globally dispositioned. |
| High | CVE-2025-13151 | Debian `libtasn1-6` 4.20.0-2 | Inherited; repository disposition records no Debian fix for this version. |
| High (2 occurrences) | CVE-2025-15281 | Debian `libc-bin`, `libc6` 2.41-12+deb13u3 | Inherited; repository disposition records no Debian fix. |
| High | CVE-2025-59375 | Debian `libexpat1` 2.7.1-2 | Inherited; a package-wide global rule suppresses it, but that rule’s comment does not name SkyPilot. S21 must revalidate the shared disposition. |
| High (2 occurrences) | CVE-2026-0861 | Debian `libc-bin`, `libc6` 2.41-12+deb13u3 | Inherited; no Debian patch recorded; exploitation requires attacker-controlled extreme allocation size and alignment. |
| High (2 occurrences) | CVE-2026-0915 | Debian `libc-bin`, `libc6` 2.41-12+deb13u3 | Inherited; repository disposition records no Debian fix. |
| High | CVE-2025-13836 | Python 3.10.19 | Inherited CPE match for unbounded `HTTPResponse.read()` against a malicious server; fixes are listed only for 3.13.11, 3.14.1, and 3.15.0. Separate Python-base ownership. |
| High | CVE-2023-36632 | Python 3.10.19 | Original disputed CPE match; no fix listed and SkyPilot does not call the cited legacy `parseaddr` API. |

All non-story critical/high occurrences were already present in the upstream 0.12.3 comparison;
the setuptools layer introduces none. Running Grype from the repository auto-loads the existing
package-wide global rules and reports **0 critical / 2 high** (the two Python CPE matches). S03
does not edit those shared rules because S21 owns global scanner dispositions. Crucially, the
four remediated story advisories are absent from both the unsuppressed and configured scans.

The requested lower-native-severity dispositions are also explicit:

- **CVE-2020-15778** remains against openssh-client/server/sftp-server
  `1:10.0p1-7+deb13u4`, with Debian-native severity **Negligible**. Upstream intentionally does
  not change legacy `scp` argument handling. SkyPilot startup does not invoke `scp`; no
  suppression was added.
- **jsonwebtoken** is 10.3.0 inside both `/root/.local/bin/uv` and `/root/.local/bin/uvx`.
  The earlier jsonwebtoken CVSS disagreement does not match the replacement scan.

## Live compatibility and persistence

The exact OCI layout was loaded directly by udocker 1.3.17; no rebuild or flattened substitute
was used. Runtime import checks returned `setuptools=81.0.0` and `skypilot=0.12.3`.

The image then ran as uid 1000 against disposable PostgreSQL 16.15 using
`SKYPILOT_DB_CONNECTION_URI`. The required `clusters`, `storage`, and `users` tables were
confirmed in database `s03_skypilot`, schema `public`. The repository's authenticated proxy ran
in front of SkyPilot with a generated one-run token:

```
unauthenticated_health=401
authenticated_health_status=200
controller_health=healthy version=0.12.3 status_records=0
```

The maintained Go controller client called its real `Health` and `Status` methods through the
proxy. Health returned version 0.12.3, commit
`9578bbb678b88b24c8244893afe6a4f967750ff4`, and
`external_proxy_auth_enabled=true`.

An authenticated `POST /users/create` created `s03-restart-proof`; both authenticated
`GET /users` and direct PostgreSQL access returned `user_type=basic`. Only the SkyPilot process
was then stopped. The still-running proxy returned 503, proving the old server was gone. A new
SkyPilot process started from the same loaded exact image and returned the same database-backed
record:

```
stopped_health=503
restarted_health=200
persisted_api_record=s03-restart-proof user_type=basic
```

`evidence/S03-live-validation.sh` is the Docker reproduction for the S21-published manifest. It
requires `S03_SKYPILOT_REPOSITORY`, appends the exact derived digest itself, verifies the pulled
RepoDigest and amd64 platform, creates the config file in the writable HOME volume, checks the
required schema rather than a timing-dependent total table count, and exercises the same
401/200, controller, state creation, 503, restart, and retrieval flow. It generates all secrets
per run, never prints them, labels all disposable objects, and removes them on exit.

```bash
S03_SKYPILOT_REPOSITORY=<S21-owned-release-repository> \
S03_TRANSCRIPT=/tmp/S03-live-validation.txt \
  bash docs/security/runs/2026-09-21/evidence/S03-live-validation.sh
```

## Startup and deployment contract

The derived image preserves the complete 0.12.3 runtime config and SkyPilot package. The
following deployment contract values remain unchanged from the currently pinned image:

- command `python3 -m sky.server.server`, host/port 127.0.0.1:46580, metrics port 9090;
- health endpoint `/api/health`;
- `SKYPILOT_DB_CONNECTION_URI` and `SKYPILOT_GLOBAL_CONFIG` environment contracts;
- writable HOME paths and consolidation-mode behavior;
- no entrypoint, config `Cmd: ["python3"]`, no uid 1000 in `/etc/passwd`, and no pre-existing
  content under `/home`.

No manifest configuration change is required beyond replacing the image reference. The complete
replacement facts artifact is `evidence/S03-skypilot_image_facts-derived.json`.

## Handoff to S21

S21 remains the sole editor of the shared lock and final image references. This change therefore
does not edit `releases/superplane.lock.yaml`, `k8s/40-skypilot-api.yaml`, or
`infra/control-plane/config.tf`.

S21 must perform these changes together:

1. Publish the OCI manifest and blobs produced by the recipe to the S21-owned release repository
   without media-type conversion. Verify the published manifest digest is exactly
   `sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec`.
2. In `modules/domain-apps/superplane/releases/superplane.lock.yaml`, set image `skypilot-api` to
   that digest and set `image_sources.skypilot-api.registry` and `.repository` to the final
   S21-owned publication location. The control-plane's `local.skypilot_image` automatically
   combines those three lock values; no second executable image literal is needed in
   `infra/control-plane/config.tf`.
3. Refresh the provenance comments in
   `modules/domain-apps/superplane/k8s/40-skypilot-api.yaml`; its executable image remains the
   `REPLACE_WITH_SKYPILOT_IMAGE` value supplied from the SSM parameter generated by
   `infra/control-plane/config.tf`.
4. Replace `modules/domain-apps/superplane/tests/skypilot_image_facts.json` with
   `docs/security/runs/2026-09-21/evidence/S03-skypilot_image_facts-derived.json`, changing only
   its placeholder repository name to the final S21-owned repository.
5. Update the deliberate literal guard in
   `modules/domain-apps/superplane/infra/control-plane/tests/lock_pin.tftest.hcl` to the same
   digest.

`modules/domain-apps/superplane/tests/skypilot_vendored_packages.json` already carries the full
derived observation. Its lock-coupling test will select that observation when S21 applies the
pin. Publishing under another repository does not change the manifest digest, provided the
manifest bytes and referenced blobs are preserved.

## Validation commands

```bash
python3 -m pytest -q modules/domain-apps/superplane/tests/test_skypilot_vendored_packages.py
python3 -m pytest -q \
  modules/domain-apps/superplane/tests/test_skypilot_startup_contract.py \
  modules/domain-apps/superplane/tests/test_skypilot_manifests.py \
  modules/domain-apps/superplane/tests/test_skypilot_state_handover.py \
  modules/domain-apps/superplane/tests/test_skypilot_eks_integration.py
python3 docs/security/runs/2026-09-21/evidence/S03-jaraco-context-traversal-check.py
```

Results on the repaired revision:

- focused S03 record/build/harness tests: **24 passed**;
- adjacent startup, manifest, state-handover, and EKS contract tests: **175 passed**;
- disposable S21 handoff with the derived digest, release source, and replacement facts:
  **198 passed**;
- control-plane Python tests against that handoff: **382 passed**;
- control-plane Terraform tests against that handoff: **34 passed**;
- controller package test: passed;
- jaraco traversal reproduction: passed;
- Ruff check/format, shell syntax, Go format, JSON parsing, and diff checks: passed.

The exact build, inventory, scan, live runtime, and restart results are recorded above and in
`evidence/S03-derived-image-provenance.json`. The maintained upstream comparison remains in
`evidence/S03-grype-high-comparison.json`; the upstream 0.12.3 facts used as the rebuild base
remain in `evidence/S03-skypilot_image_facts-0.12.3.json`.
