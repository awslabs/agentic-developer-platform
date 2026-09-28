# Learnings — issue #5600 (S01: Superplane controller image CVEs)

2026-09-21 security scan, work package S01. Eight findings (4 critical / 4 high) against the
Superplane controller image, plus three CVSS/native-severity disagreements.

Outcome: base moved to a supported Alpine, four unused packages removed, one finding
identified as a scanner false positive with evidence, regression test added, per-advisory
disposition written. No suppression, no baseline edit.

## What generalises to other scan work packages

**A finding list groups by image, but remedies are decided by layer.** These eight looked
homogeneous in the issue. They were four different layers: the controller's compiled Go deps,
Alpine OS packages, Go modules vendored inside `/usr/bin/helm`, and Python packages arriving
transitively via `aws-cli`. Only after separating them did the right action become obvious —
and the answer for six of them was *remove*, which no amount of version-bumping would have
reached. Do the attribution before considering any fix. It is also usually the first
acceptance criterion, which is a hint that it is the real work.

**Resolve "which package pulled this in" from the package index, not by reasoning.** I built
dependency closures from Alpine's `APKINDEX.tar.gz` (parse `P:`/`V:`/`D:`/`p:` records). That
turned "probably the AWS CLI" into "`aws-cli` is the sole path to `sqlite-libs`,
`py3-jmespath`, `py3-cryptography`, `py3-certifi`, `python3`; `c-ares` comes from `curl`" —
which directly determined that `curl` stays and needs a genuine upgrade while everything else
goes. HTML-scraping `pkgs.alpinelinux.org` failed for every package and wasted a cycle; the
APKINDEX is authoritative, cheap, and enables closure analysis the web UI cannot.

**"Vulnerable package present" and "vulnerable code reachable" are different claims, and
conflating them in either direction is a mistake.** Reachability does not make a shipped
vulnerable file acceptable — it was still in the image and correctly reported. What it
establishes is which *remedy* is right. Here, proving the controller starts no subprocess
(no `os/exec` import, no `exec.Command`, `httpGet` probes, HTTP-only clients) is what made
removal provably safe rather than merely appealing. State the narrow claim.

**A scanner-reported version can itself be the attribution evidence.** The report listed
`x/crypto v0.17.0` and `docker/docker v24.0.7+incompatible`. Fetching helm's own `go.mod` at
tag `v3.14.3` showed those exact pins. Exact-version agreement upstream is strong proof the
finding belongs to the vendored binary and not the local module — much better than asserting
"that's helm's dependency".

**Check `go.sum` and `go list -deps` before claiming a Go module is absent, and explain the
false leads.** `go.mod` had no `x/crypto`, but `go.sum` contained three `x/crypto` lines and
`go list -deps` printed `x/crypto` paths — either would let a reviewer conclude my attribution
was wrong. Both are benign: the `go.sum` lines are `/go.mod`-suffixed hashes only (module-graph
metadata, no `h1:` zip hash ⇒ never downloaded or compiled), and the `go list` hits are
`vendor/golang.org/x/crypto/...` with `Module: <nil>, Standard: true` — the standard library's
internal copy used by `crypto/tls`. Anticipate the grep a reviewer will run and answer it in
the document.

**A cross-ecosystem version collision is a real and checkable false-positive class.**
CVE-2022-32511 is a Ruby `jmespath.rb` advisory matched against Alpine's Python
`py3-jmespath`, because the package normalises to the same name and `1.0.1 < 1.6.1` compares
true numerically. The decisive test was cheap and is reusable: **does the prescribed fix
version exist for the flagged ecosystem at all?** PyPI `jmespath` has never published any
1.6.x (releases stop at 1.1.0); RubyGems has 1.6.1. A fix version that was never published
cannot be the fix. Corroborate with the advisory's CPE (`...:ruby:*:*`), its Git range
(the `.rb` repo), and an OSV query for the flagged ecosystem+version returning zero vulns.

**Do not let a convenient fix hide a false positive.** Removing `aws-cli` made the jmespath
finding disappear, which could have been reported as "resolved" with no analysis. But Alpine
ships upstream `1.0.1` in *every* current release, so no base bump could ever have cleared it
— meaning had the package been needed, the only correct outcome was the documented false
positive. Write the disposition so it stands on the evidence, independent of the fix that
happened to also remove the package.

**Verify each severity against upstream rather than inheriting the issue's rating.** Two of
the four "criticals" were weaker upstream: CVE-2025-3277 is CVSS v4.0 **6.9 medium** and needs
SQL calling `concat_ws()` with a >2 MB separator; GHSA-r6ph-v2qm-q3c2 is scoped by its own
advisory to **SECT binary-field curves only** (NIST-deprecated, negotiated by nothing in this
stack). Neither changed my action, but both belong in the record because severity drives
prioritisation and an inflated rating misdirects the next reader.

**Also check whether the distro tracks the CVE at all.** Alpine's secdb has no `secfixes`
entry fixing CVE-2025-3277 for 3.20/3.22/3.23. "Wait for the distro fix" was therefore not an
available remedy — worth establishing before proposing it.

## Testing

**When the fix is a deletion, add a test that asserts the absence.** The Dockerfile now shows
a short `apk add` list with no signal that `helm` was removed deliberately. The natural
response to "the controller should run a helm command" is to add it back, silently reopening
four advisories until the next scheduled scan. The test names the advisories each removed
package carried, so the failure message teaches rather than just blocks.

**Test the premise, not only the artifact.** The load-bearing check scans the Go source for
`os/exec`/`exec.Command`. The removals are only safe while the no-subprocess invariant holds;
if someone adds a subprocess call, that must fail a test rather than surface as a runtime
`helm: not found` inside a reconcile loop. A Dockerfile-only test would miss exactly that case.

**Strip comments before substring-matching a file that documents itself.** The Dockerfile
legitimately *names* `helm` and `aws-cli` in its explanation. A raw-text check would fail on
the documentation it exists to protect, and the cheapest way to pass would be deleting the
explanation. Match executable lines only, with word boundaries so `aws-cli` isn't matched
inside `aws-cli-v2`. (Same balance as the S05 precedent.)

**Mutation-test the guards.** Each of the five was individually broken — re-add `helm`, re-add
`aws-cli`, revert to `alpine:3.20`, `USER root`, inject `exec.Command` — and each failed
exactly its own test before being restored. An untested guard that cannot fail is worse than
none, because it implies coverage.

## Honesty about evidence

**Do not present analysis as scanner output.** Docker, Syft and Grype were unavailable, so I
could not build the image or produce fresh scan evidence for a rebuilt digest — one of the
acceptance items. The disposition says so plainly, names which commands need the repository's
build lane, and marks the package closure as *indicative*: some dependencies are virtuals
(`/bin/sh`, `so:*`) satisfiable by more than one package, so the exact resolved set is
confirmed only by an actual build. I also corrected a package count I had first written from
an incomplete closure — worth re-deriving numbers before they harden into a claim.

**Check whether a "before" digest even exists.** The controller sits under `pending_images` in
the release lock with `blocked_by: no build has run yet` — no image has ever been built, so
there is no prior digest to compare against, and the scanned SARIF came from Grype's own
throwaway `docker build`. That reframes the digest deliverable as a handoff, not an omission.

## Scope discipline

Owner boundary was explicit: the controller Dockerfile and its tests. The release lock,
`.grype.yaml`, the baseline, SkyPilot manifests and other images belong to S21 or elsewhere.
Two things followed:

- The digest/provenance record goes to S21 as a proposal, not an edit — recording a digest
  means editing the shared lock.
- I found `superplane-platform-monitor/Dockerfile` pinning `alpine:3.19` (also EOL) and an
  unprefixed `golang:1.23-alpine`. Flagged in the disposition, not fixed — different image,
  different package, and I did not assess it for findings.

**A false positive did not need a scanner disposition after all.** My instinct was to propose
a narrow ignore rule for CVE-2022-32511. Unnecessary: removing `aws-cli` removes the matched
package, so the finding disappears on its true merits. Adding an ignore rule would have been
permanent config carrying risk (it hides future *real* jmespath findings) for no benefit. The
document serves as the location/package-specific evidence if the package ever returns.
