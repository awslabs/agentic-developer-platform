# SkyPilot Go tool security builds

Build from the repository root:

```sh
docker build -t skypilot-go-tools modules/domain-apps/superplane/images/skypilot/go-security
```

This distribution artifact supplies three `/bin/` executables and
`/usr/share/licenses/skypilot-go-tools/` to SkyPilot's maintained runtime.

The GKE authentication plugin retains source revision
`395432f6b23de7465d1a0ebb9b384cf96e3a04b3`, with x/net 0.60.0 and its compatible
module graph locked in `gcp-locks/`. AWS Session Manager retains release
1.2.814.0 and the upstream release-time VERSION injection. Both use Go 1.26.9.
CRC32C uses Google's checksum-pinned 20261016073856 component, built with
Go 1.27.2. These versions address GO-2026-6612 / CVE-2026-78663.

Run the assembled runtime's `../high-maintenance/verify_runtime.py` to check
CRC32C file/range results, the GKE credential contract using a synthetic local
gcloud, SSM version, and the provider environment. Rescan the assembled image
before claiming remediation. SSM retains its upstream GOPATH/vendor build,
whose dependency metadata is less detailed than Go module metadata.

The September publication, scanner and parity receipts in `evidence/` document
the previous artifact. They do not validate this refreshed build.
