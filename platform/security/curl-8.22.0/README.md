# curl 8.22 repair overlays

This recipe builds signed upstream curl 8.22.0 into Debian-compatible curl,
OpenSSL libcurl and GnuTLS libcurl packages, then installs them over a selected
immutable ADP candidate. It preserves that candidate's entrypoint, command,
environment and non-root identity. The supported targets are Linux amd64 Debian
Trixie worker, DeepWiki and SkyPilot images listed in `bases.json`.

```sh
python3 platform/security/curl-8.22.0/build-overlay.py deepwiki \
  --tag adp-deepwiki:curl-8.22 --docker-config /path/to/isolated/docker-config
```

Use `worker` or `skypilot` for the other images. Authenticate to the existing
account registry first. The script only builds locally. Publication, rollout,
application acceptance and observed-digest scanning are separate actions.
These emergency repair overlays must be selected explicitly for deployment;
the ordinary component build workflows do not automatically apply this overlay.
When rebasing onto a newer component build, update its exact reference, rebuild,
and repeat scanner/runtime verification; never replace the base with a moving tag.

The curl source archive checksum and detached signature are verified in the
build. Both TLS libraries retain Debian SONAMEs and versioned symbol namespaces,
including `CURL_GNUTLS_3`. NTLM, SMB, HTTP/2 and HTTP/3 remain enabled. Upstream
8.22 removed TLS-SRP and RTMP; repository caller searches found no use, but users
of these external protocols require compatibility review before rollout.

HTTP/3 uses statically linked PIC ngtcp2 1.25.0 and nghttp3 1.18.0. Their release
checksums, identities and licenses are retained. The supplemental CycloneDX SBOM
and upstream advisory review cover these libraries even if a filesystem scanner
cannot infer them. All source records and licenses also live under
`/opt/adp-curl-security`, because minimal base images can exclude dpkg doc paths.
`binary-receipt.json` binds the tested build's three exact executables/libraries;
a source rebuild that changes those bytes requires a new receipt and validation.

For upstream validation build the `build` target and execute
`verify-upstream.sh` inside it with `--network none`. This runs curl's nonflaky
OpenSSL/GnuTLS suites and the static HTTP/3 library unit suites. The upstream
root/unsupported-feature skips remain visible in the log. `verify_https_git.py`
runs non-root against each application image: trusted TLS and Git clone/fetch,
untrusted certificates and wrong hostnames rejected, isolated loopback only.
Application-specific runtime tests remain necessary.

`review-candidate.py SCAN_DIRECTORY --output review.json` reads a full raw Grype
scan receipt, verifies its hashes, the candidate image identity, actual package
versions/binary hashes/source locks and the 18 curl vendor advisories. It emits
54 exact candidate-only fixed dispositions. It never changes raw scanner files,
live totals, unrelated versions or unresolved findings. Debian's unfixed package
ranges still flag these genuine 8.22 packages; retain that scanner disagreement.
The script deliberately fails when any expected package/advisory/binary is absent.

Evidence and the remaining findings are in
`docs/security/runs/2026-09-27/curl-upstream/`. Restoring the prior base image is a
rollback and also restores its vulnerability exposure.
