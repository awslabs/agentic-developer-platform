# S01 — Superplane controller runtime image: disposition

Work package **S01** of the 2026-09-21 security scan (parent #5599, issue #5600).

Scanner: Grype 0.80.2, SARIF
`superplane/grype/modules-domain-apps-superplane-src-superplane-controller.sarif`,
run 35546057969. **4 critical / 4 high** source-rated occurrences, plus three findings
whose CVSS is ≥7 while Grype's native severity is lower.

Image: `modules/domain-apps/superplane/src/superplane-controller/Dockerfile`.

## Summary

Six of the eight findings were in software the controller **cannot execute**, and are
resolved by removing it rather than upgrading it. One is a **scanner package-identity
error** (a Ruby advisory matched against a Python package). One — c-ares — was a genuine
stale-OS-package finding and is fixed by moving off an end-of-life base image.

| Severity | Advisory | Package (as scanned) | Disposition |
|---|---|---|---|
| CRITICAL | CVE-2022-32511 | py3-jmespath 1.0.1-r3 | **False positive** — Ruby `jmespath.rb` advisory matched against the Python package. Package also removed. |
| CRITICAL | CVE-2025-3277 | sqlite-libs 3.45.3-r3 | **Resolved — package removed** (came only from `aws-cli`) |
| CRITICAL | GHSA-v23v-6jw2-98fq | github.com/docker/docker v24.0.7+incompatible | **Resolved — package removed**. Embedded in `/usr/bin/helm`, not the controller binary. |
| CRITICAL | GHSA-v778-237x-gjrc | golang.org/x/crypto v0.17.0 | **Resolved — package removed**. Embedded in `/usr/bin/helm`, not the controller binary. |
| HIGH | CVE-2025-31498 | c-ares 1.33.1-r0 | **Resolved — version upgraded** to 1.34.8-r0 via supported base |
| HIGH | CVE-2025-53547 | helm 3.14.3-r4 | **Resolved — package removed** |
| HIGH | GHSA-hcg3-q754-cr77 | golang.org/x/crypto v0.17.0 | **Resolved — package removed**. Embedded in `/usr/bin/helm`. |
| HIGH | GHSA-r6ph-v2qm-q3c2 | cryptography 42.0.7 | **Resolved — package removed** (came only from `aws-cli`) |

No suppression was used. `.grype.yaml` and `.github/security/grype-baseline.json` are
unchanged, and neither contained any of these CVEs beforehand — all eight were live
findings, not previously-accepted ones. The one false positive is documented here rather
than added as an ignore rule, so it stays visible in the next scan.

## Layer attribution — which findings were ever ours to fix

This is the first acceptance item, and it is what determines each remedy. The report lists
eight findings against "the controller image" as though they were one kind of thing. They
are three kinds, and only one is the controller's own code.

**The controller binary's own dependencies.** `go.mod` requires exactly four modules
(`k8s.io/api`, `k8s.io/apimachinery`, `k8s.io/client-go`, `sigs.k8s.io/controller-runtime`)
plus their transitive set. It requires **no `golang.org/x/crypto`**, **no
`github.com/docker/docker`**, and no Python anything. **Zero of the eight findings are in
the controller binary.** No `go.mod`/`go.sum` change was needed or made.

One detail to pre-empt, because it looks like a contradiction: `golang.org/x/crypto` *does*
appear in `go.sum`, and `go list -deps` *does* print `x/crypto` import paths. Neither means
it is a dependency of this binary.

```bash
grep -E '^golang.org/x/crypto v[^ ]+ h1:' go.sum   # no output
```

Every `x/crypto` line in `go.sum` is a `/go.mod`-suffixed hash only. Those record the module
*graph* — checksums of go.mod files consulted during version selection — with no `h1:` module
zip hash, which means the module was never downloaded and no package from it was compiled.
The `go list -deps` hits resolve to `vendor/golang.org/x/crypto/...` reported as
`Module: <nil>, Standard: true`: the Go **standard library's** own internal copy, used by
`crypto/tls`, whose version tracks the Go toolchain and not any advisory range here. And
`grep -rn 'golang.org/x/crypto'` over the controller source returns zero imports. Had MVS
ever needed to select the module it would have picked `v0.31.0`, not the `v0.17.0` in the
report — further confirmation that the reported version came from helm's binary, not ours.

**Go modules compiled into `/usr/bin/helm`.** Three findings — both `x/crypto` advisories
and the `docker/docker` one — are Grype reading the module list embedded in the Alpine
`helm` binary. Confirmed against helm's own manifest:

```
$ curl -s https://raw.githubusercontent.com/helm/helm/v3.14.3/go.mod | grep -E 'x/crypto|docker/docker '
	golang.org/x/crypto v0.17.0
	github.com/docker/docker v24.0.7+incompatible // indirect
```

Those are the exact versions the scan reported, which confirms the attribution: the
versions are helm's vendored pins, and nothing in this repository selected them. They could
only be changed by changing the `helm` package version — or by not shipping `helm`.

**Alpine runtime packages.** `c-ares` (via `curl`), and `sqlite-libs` + the whole `py3-*`
chain (via `aws-cli`). Attribution here was resolved against Alpine's APKINDEX dependency
closure rather than inferred, because "which `apk add` line is responsible" decides the fix:

| Vulnerable package | Pulled in by |
|---|---|
| `sqlite-libs`, `py3-jmespath`, `py3-cryptography`, `py3-certifi`, `python3` | `aws-cli` (only) |
| `c-ares` | `curl` (only) |
| `py3-protobuf` | **nothing** — not in any dependency closure of this image |

## Why the packages were removed rather than upgraded

**The controller starts no subprocess.** It contains no `os/exec` import and no
`exec.Command` call anywhere in its non-test source:

```bash
cd modules/domain-apps/superplane/src/superplane-controller
grep -rn 'os/exec' --include='*.go' .      # no matches
grep -rn 'exec\.Command' --include='*.go' . # no matches
```

Everything external is reached over HTTP instead: the SkyPilot REST API
(`skypilot/client.go` uses `net/http`) and the operator-supplied control-plane API. The
Kubernetes probes in `deploy/controller.yaml` are `httpGet`, not `exec`. The cloud-side work
that made these tools look necessary — SSM activation, WireGuard tunnels, node onboarding —
is performed **by SkyPilot**, from parameters the controller passes to that API
(`provisioner/onboarder.go` builds an env map and POSTs it); it does not happen in this
container. `ONBOARD_SCRIPT_PATH` is set in the deployment manifest but read by no Go code.

So `helm`, `aws-cli`, `openssh-client` and `wireguard-tools` were unreachable software. For
them, "upgrade to the fixed version" would have carried six findings to a newer version of
programs the image never runs, and left the surface in place for the next advisory.
Removing them resolves the findings at the source.

A note on what this claim does and does not assert: the findings are **real reports about
files genuinely present in the shipped image**, and that is why they needed action rather
than a "not exploitable" dismissal. What the no-subprocess evidence establishes is narrower
— that no code path in this controller reaches them — which is what makes *removal* safe.
The distinction matters for two of the helm findings in particular: both `x/crypto`
advisories are in SSH **server** code paths, which nothing here ever ran even when `helm`
was installed. They were shipped, unreachable, and are now absent.

**The base image was genuinely out of support.** Alpine 3.20 reached end of life on
**2026-04-01**, which is why its `c-ares` was stale. `curl` is retained as the operator
debugging surface, so `c-ares` stays — it needed a real upgrade, and a supported base is
what provides it. Alpine's security database records the fix and shows the shipped version
is current, not merely past the reported CVE:

```
c-ares secfixes (v3.24/main):
  1.34.5-r0: CVE-2025-31498   <- the reported finding
  1.34.6-r0: CVE-2025-62408
  1.34.8-r0: CVE-2026-33630   <- version actually shipped
```

Alpine 3.24 (latest stable) is the chosen base, on the general principle that a supported
base should be the current one. A secondary reason is recorded for the future: 3.22 would
also have fixed `c-ares` and `helm`, but its `py3-cryptography` is `44.0.3-r0`, still inside
GHSA-r6ph-v2qm-q3c2's affected range (`≤ 46.0.4`), whereas 3.24 ships `47.0.0-r0`. That
difference does not affect this image — `aws-cli` is gone, so no `py3-cryptography` is
installed at any version — but it is the reason not to treat 3.22 as equivalent if the
Python runtime ever returns. `alpine:3.24` is present on the registry the Dockerfile already
uses (`public.ecr.aws/docker/library/alpine`).

## CVE-2022-32511 — the Ruby/Python mismatch, resolved

The issue asked specifically whether this Ruby advisory applies to `py3-jmespath`, and
required that a Ruby fix version not be installed as a Python fix. **It does not apply, and
no such version was installed.** Three independent pieces of evidence:

**1. The advisory's own metadata scopes it to Ruby.** Its CPE ends in `:ruby:*:*` and its
only affected range is a Git range on the Ruby repository:

```
$ curl -s https://api.osv.dev/v1/vulns/CVE-2022-32511
ranges: repo https://github.com/jmespath/jmespath.rb
        cpe:2.3:a:jmespath:jmespath:*:*:*:*:*:ruby:*:*
        extracted_events: introduced 0, fixed 1.6.1
```

The vulnerability is `JSON.load` used where `JSON.parse` was appropriate — a Ruby-specific
unsafe-deserialization idiom. The Python library has no equivalent construct.

**2. The prescribed fix version does not exist for the Python package.** This is the
decisive check, because a fix version that was never published cannot be the fix:

```
$ curl -s https://pypi.org/pypi/jmespath/json   # all released versions
0.0.1 … 0.9.5, 0.10.0, 1.0.0, 1.0.1, 1.1.0      # latest: 1.1.0
1.6.1 exists? False      any 1.6.x? []
```

PyPI `jmespath` has never released 1.6.1, or any 1.6.x. RubyGems `jmespath` has:
`1.6.0, 1.6.1, 1.6.2`. The "fix 1.6.1" is unambiguously the gem's.

**3. No advisory exists for the Python package at this version.**

```
$ curl -s -X POST https://api.osv.dev/v1/query \
    -d '{"package":{"name":"jmespath","ecosystem":"PyPI"},"version":"1.0.1"}'
vulns: 0
```

**Mechanism of the false positive.** Grype matched on package *name* and a numeric version
comparison across an ecosystem boundary: Alpine's `py3-jmespath` normalizes to the name
`jmespath`, and `1.0.1 < 1.6.1` is true numerically. Both operands are real; they just
belong to different packages in different languages. Python 1.0.1 is **newer** than the
Ruby line's start, not older than its fix.

This is why a version bump was never the right response, and it is not a theoretical
concern: Alpine still ships upstream `1.0.1` of `py3-jmespath` in **every** current release —
`1.0.1-r4` in both 3.22 and 3.23, `1.0.1-r6` in 3.24 (the `-rN` suffix is Alpine's packaging
revision; the upstream version never moves). **No base image upgrade could have cleared this
finding**, because there is no newer Python `jmespath` for Alpine to package. Removing `aws-cli` drops the package from the image entirely, which does clear
it — but the disposition stands on its own: had the package been required, the correct
outcome would have been this documented false positive, not an upgrade.

## Per-advisory applicability

**CVE-2025-3277** (sqlite-libs 3.45.3-r3, reported critical). Integer overflow in SQLite's
`concat_ws()`: a separator string over 2 MB truncates the computed allocation size while the
write uses the untruncated length, giving a heap overflow (CWE-122). Introduced in 3.44.0,
fixed in 3.49.1, so the scanned 3.45.3 was genuinely in range. Upstream CVSS v4.0 is
**6.9 medium** (`AV:N/AC:L/AT:N/PR:N/UI:N` with Low across VC/VI/VA) — materially lower than
the critical rating carried into the issue, which is worth recording since the severity drove
the prioritisation. Reachability here was nil regardless: `sqlite-libs` arrived only as a
transitive dependency of the AWS CLI's Python runtime, nothing ever invoked the AWS CLI, and
triggering the defect additionally requires executing SQL that calls `concat_ws()`. Also
note that no Alpine 3.20, 3.22 or 3.23 package is marked as **fixing** it — the secdb
`secfixes` records carry no entry, and Alpine's tracker lists those versions as "possibly
vulnerable" with no fixed release — so waiting for an Alpine fix was not an available remedy.
Removed with `aws-cli`.

**CVE-2025-31498** (c-ares 1.33.1-r0, high). Use-after-free in `read_answers()`. This is
the one finding in a package the image still ships, because `curl` needs it. Fixed in
Alpine `1.34.5-r0`; now shipping `1.34.8-r0`. **Genuinely upgraded, not removed.**

**CVE-2025-53547** (helm 3.14.3-r4, high). Missing symlink check in helm's dependency
resolver: a chart with a `Chart.lock` symlink can cause writes through it to arbitrary
files when `helm dependency update` runs, giving local code execution. CVSS 8.5, vector
`AV:L/...UI:R` — it requires a local actor and a victim running a helm command on an
untrusted chart. Nothing in this container ran any helm command. The fix is helm
`3.18.4`, which Alpine carries from 3.22 (`3.18.4-r5` in 3.22, `3.19.0-r8` in 3.24), so a
base bump alone would have cleared it; `helm` was removed instead, since it was never invoked.

**GHSA-v778-237x-gjrc** (golang.org/x/crypto v0.17.0, critical — CVE-2024-45337). Improper
authorization via misuse of `ssh.PublicKeyCallback`; CVSS 9.1. Applies to **SSH server**
implementations that cache callback keys and authorize from them. Embedded in `/usr/bin/helm`
(helm's direct `x/crypto` pin). Neither helm nor this controller runs an SSH server.
Fixed in `x/crypto 0.31.0`; helm reaches `0.39.0` by v3.18.4 and `0.41.0` by v3.19.0.
Removed with `helm`.

**GHSA-hcg3-q754-cr77** (golang.org/x/crypto v0.17.0, high — CVE-2025-22869). Memory
exhaustion in SSH servers supporting file transfer, from unsent buffered data; CVSS 7.5,
availability-only. Same package and same reasoning as above — SSH **server** code, never
run here. Fixed in `0.35.0`. Removed with `helm`.

**GHSA-v23v-6jw2-98fq** (github.com/docker/docker v24.0.7+incompatible, critical —
CVE-2024-41110). AuthZ-plugin bypass via `Content-Length: 0`; CVSS 9.4. Requires a Docker
Engine API relying on authorization plugins. This is an **indirect** module inside helm's
binary, not a Docker daemon, and no Docker API is involved in this image. Helm dropped the
dependency entirely by v3.18.4. Removed with `helm`.

**GHSA-r6ph-v2qm-q3c2** (cryptography 42.0.7, high — CVE-2026-26007). Missing prime-order
subgroup validation when loading EC public keys — `load_pem_public_key()`,
`load_der_public_key()` and `EllipticCurvePublicNumbers.public_key()` accepted points from a
small-order subgroup, leaking private-key bits under ECDH or allowing signature forgery under
ECDSA (CWE-345, CVSS v4.0 8.2, `AC:H`, confidentiality-only). Affected `≤ 46.0.4`, so the
scanned 42.0.7 was in range. The advisory scopes it explicitly: **"Only SECT curves are
impacted"** — the SECG binary-field (F₂ᵐ) curves, which NIST SP 800-186 has deprecated and
which no mainstream protocol negotiates; the fix only calls OpenSSL's `check_key()` for
curves with cofactor > 1. Exposure therefore needs an application that loads untrusted EC
public keys *on a binary-field curve*, which the AWS CLI does neither — it does not import
`cryptography` at all (the package is a transitive dependency of `SecretStorage`), and AWS
signing and TLS use RSA and the NIST prime curves. Removed with `aws-cli`. (Worth recording:
Alpine 3.22 ships only `44.0.3`, still in range, so this finding is the reason the base
target is 3.24 rather than 3.22.)

### The three CVSS / native-severity disagreements

The issue asked for these to be covered and kept separate from the 4/4 total.

**CVE-2019-25210** (helm). Helm prints Kubernetes Secret values in `--dry-run` output.
CVSS 6.5 medium, confidentiality-only. The Helm maintainers **formally reject** this as a
vulnerability (<https://helm.sh/blog/response-cve-2019-25210/>): the output is intentional
for debugging chart rendering, and exposure depends on a CI system showing that output to
unauthorized viewers. Fixed in 3.13.3 regardless. Moot here — no helm binary and no helm
invocation. Resolved by removal.

**GHSA-248v-346w-9cwc** (certifi — CVE-2024-39689). Not a code defect: certifi 2024.7.4
**removes** GLOBALTRUST roots after Mozilla distrusted that CA for compliance failures.
Older bundles keep trusting them. Native severity **low**, and the higher CVSS reflects
trust-store hygiene rather than an exploitable flaw. Reached this image only through
`aws-cli` → `py3-certifi`; that bundle was never used for any TLS connection the controller
made, because the controller uses Go's crypto/tls with the system store from
`ca-certificates` (now `20260909-r0`), not Python's. Resolved by removal.

**GHSA-8r3f-844c-mc37** (protobuf — CVE-2024-24786). Infinite loop in `protojson.Unmarshal`
on malformed JSON; CVSS 7.5 high but GitHub-native **moderate**, availability-only. This is
a **Go** advisory — `google.golang.org/protobuf`, fixed in **1.33.0** — and the controller's
own `go.mod` pins `google.golang.org/protobuf v1.35.1`, already past the fix. It does not
apply to Python's `protobuf`, which is a separate ecosystem and package. Also worth noting
for the attribution, since the finding names "protobuf" without an ecosystem: `py3-protobuf`
was pulled in by nothing in the previous package set either, so no Python protobuf was
installed at any point. **No action required, and none taken** — this one needed no change
rather than a removal.

## The rebuilt image

Resulting runtime stage: base `alpine:3.24`, three explicit packages
(`ca-certificates`, `bash`, `curl`), non-root `USER 65532:65532` unchanged.

Resolving the three `apk add` packages against Alpine 3.24's APKINDEX gives this closure —
21 packages on top of whatever the base layer already carries:

```
bash 5.3.9-r1                brotli-libs 1.2.0-r1         busybox 1.37.0-r31
busybox-binsh 1.37.0-r31     c-ares 1.34.8-r0             ca-certificates 20260909-r0
ca-certificates-bundle 20260909-r0                        curl 8.22.0-r0
libcrypto3 3.5.8-r0          libcurl 8.22.0-r0            libidn2 2.3.8-r0
libncursesw 6.6_p20260516-r0 libpsl 0.21.5-r3             libssl3 3.5.8-r0
libunistring 1.4.2-r0        musl 1.2.6-r2                nghttp2-libs 1.69.0-r0
ncurses-terminfo-base 6.6_p20260516-r0                    readline 8.3.3-r1
zlib 1.3.2-r0                zstd-libs 1.5.7-r2
```

Treat the exact package count as indicative rather than exact: a few of these dependencies
are expressed as virtuals (`/bin/sh`, `so:*`) that more than one package can satisfy, so the
precise set `apk` selects — and the base image's own preinstalled packages — are confirmed
only by building the image. What the index does establish unambiguously is the part that
matters for this disposition: **no dependency path from `ca-certificates`, `bash` or `curl`
reaches `python3`, any `py3-*`, `sqlite-libs`, `helm`, `openssh-client`, `wireguard-tools` or
any `docker` package.** Every package named in the eight findings is therefore absent except
`c-ares`, which is present at fixed `1.34.8-r0`.

For contrast, the same resolution on the *previous* package list is what produced the
findings: `aws-cli` pulls `python3`, `py3-jmespath`, `py3-cryptography`, `py3-certifi` and
`sqlite-libs`; those five disappear with it.

## Validation

Controller source is unchanged by this work; these confirm the change did not break it.
Go 1.23.8 — the toolchain `go.mod` declares — per `src/TRANSFER-MANIFEST.md`.

```bash
cd modules/domain-apps/superplane/src/superplane-controller
go vet ./...
go test ./... -count=1
# ok  adapters 0.003s | controllers 7.388s | management 0.046s
# ok  provisioner 0.003s | skypilot 0.310s   -- all packages pass, no failures

# The runtime stage's exact build command:
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-w -s" -o /tmp/superplane-controller .
# exit 0; 40,812,696-byte static binary
```

Regression guard (`modules/domain-apps/superplane/tests/test_controller_runtime_surface.py`):

```bash
cd modules/domain-apps/superplane && python3 -m pytest tests/test_controller_runtime_surface.py -q
# 7 passed
```

All five guards verified by mutation — each fails when its property is broken:

| Mutation applied | Test that failed |
|---|---|
| re-add `helm` to `apk add` | `test_removed_runtime_packages_stay_removed[helm]` |
| re-add `aws-cli` to `apk add` | `test_removed_runtime_packages_stay_removed[aws-cli]` |
| revert base to `alpine:3.20` | `test_runtime_base_is_a_supported_alpine` |
| change `USER` to `root` | `test_controller_still_runs_as_non_root` |
| add a file importing `os/exec` + `exec.Command` | `test_controller_starts_no_subprocess` |

The last guard is the load-bearing one: the package removals are only safe while the
controller never execs anything, so a future subprocess call fails a test rather than
becoming a runtime `helm: not found` inside a reconcile loop.

### Rebuilt-image evidence

The image for the reviewed implementation commit `060a3c2764a408b6c3207db8795e0c8159ef28ea`
was built by [controller build 35663521595](https://github.com/aws-e/adp/actions/runs/35663521595):

`879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-controller@sha256:d87cf6355d66432337aafbd3194f51b93b53744c0c1bae0f015bc0e156ce38ed`

[Validation run 35669683518](https://github.com/aws-e/adp/actions/runs/35669683518)
verified that image's source-revision label and collected Syft 1.52.0 and Grype
0.119.0 output with the 2026-09-21 vulnerability database. Scanner downloads were
checked against their official release SHA256 checksums. The raw `syft.json`,
`grype.json`, `runtime.txt`, `image-inspect.json`, `startup.json` and `summary.json`
are retained in that workflow's artifact and its recorded S3 evidence prefix.
[The committed evidence index](S01-image-evidence.json) records the source build,
artifact hashes, actual APK inventory, original advisory matches, and all residual
critical/high matches. No scanner ignore or baseline was applied.
The controller build-context Git tree is
`198928e65616641c273b71e10beeff2f81c10ba1` at both the built implementation and this
evidence update. This update changes documentation and test formatting only; its
Dockerfile, Go sources, dependency lock and runtime build inputs are identical.

All eight primary advisories and the three lower-native-severity/CVSS disagreements
listed in this story have **zero matches**, including related advisory aliases,
in the rebuilt-image scan. The actual installed `c-ares` is `1.34.8-r0`; the Helm,
AWS CLI, SSH and WireGuard package families removed by this change are absent.
The JMESPath finding disappears with its Python package; no Ruby version was
installed and no exception was used to hide the match.

Runtime validation on the exact digest established UID/GID 65532, DNS resolution,
and binary startup/help. A local authenticated empty-registry fixture also ran
`--management-only`: readiness and health became HTTP 200, unauthenticated status
was HTTP 401, registry credential revocation changed readiness to HTTP 503, and
SIGTERM produced exit code 0. The fixture used no cloud or Kubernetes credentials.
This verifies the supported management startup path; it does **not** claim Kubernetes
workspace reconciliation or governed provisioning (the latter remains unavailable
in this controller independently of the image-package change).

The full current scan is **not clean**: 1 critical, 28 high, 33 medium and 2 low
matches remain. They include the unchanged controller's Go 1.23.12 standard library,
`golang.org/x/net v0.33.0`, `golang.org/x/text v0.21.0`, and `zlib 1.3.2-r0`.
These are distinct from the 11 advisories assigned to S01; their exact IDs, paths
and published fixes are retained in the evidence index, with no blanket exception
or acceptance claim. S21 must retain these residuals in the aggregate scan and
coordinate any further remediation/disposition. No shared release lock was changed.
The digest and provenance are available to S21 without requiring a lock-file edit
to perform this validation.

## Scope

**Owned and changed:** `src/superplane-controller/Dockerfile` (runtime stage) and
`tests/test_controller_runtime_surface.py`. No `go.mod`/`go.sum` change — none of the original eight primary findings was in
the controller binary. The current scanner also reports the residual binary findings
listed above. The evidence index and this validation record accompany the source change.

**Not touched:** `releases/superplane.lock.yaml` (shared release pins, S21), `.grype.yaml`
and `.github/security/grype-baseline.json` (global scanner disposition, S21), SkyPilot
manifests, and the other images' Dockerfiles.

**Referred to S21:** the built image digest, provenance and unsuppressed residual scan
above. **No narrow scanner disposition is requested for the original 11 advisories** — the CVE-2022-32511 false positive
needs no ignore rule, because removing `aws-cli` removes the matched package, so the finding
disappears on its true merits. If `py3-jmespath` ever returns to this image, this document
is the location/package-specific evidence for a narrow disposition at that point.

**Not covered:** `src/superplane-platform-monitor/Dockerfile` pins `alpine:3.19`, also
end-of-life, and `golang:1.23-alpine` without a registry prefix. That is a different image
and a different work package's scope; flagging it here rather than editing it. It was not
assessed for findings.
