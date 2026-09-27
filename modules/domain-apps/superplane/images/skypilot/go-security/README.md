# SkyPilot Go tool security builds

Build from the repository root:

```sh
docker build -t skypilot-go-tools modules/domain-apps/superplane/images/skypilot/go-security
```

The image is a distribution artifact. Copy its three `/bin/` executables into
the matching SkyPilot SDK/plugin locations, and copy
`/usr/share/licenses/skypilot-go-tools/` into the runtime's license directory.
It is not a standalone SkyPilot server.

The GKE authentication plugin stays at source revision
`395432f6b23de7465d1a0ebb9b384cf96e3a04b3`, with locked patched x/net and x/text
modules. The AWS Session Manager plugin stays at release 1.2.814.0, including
AWS's release-time VERSION injection. Both compile with Go 1.26.8. CRC32C uses
Google's checksum-verified 20260821073916 component, built with Go 1.26.6.
All sources and downloads are checksum pinned; source and dependency notices
are included.

The published artifact in `evidence/publication.json` has zero native scanner
matches (35 cataloged packages) against the frozen September 27 database.
This does not describe the entire SkyPilot image; rescan the assembled runtime.
SSM retains the upstream GOPATH/vendor build, whose dependency metadata is
less detailed than the authentication plugin's module metadata.

Validation: the GKE plugin unit tests and the SSM upstream suite pass. SSM tests
use the upstream makefile's `-test.paniconexit0=false` requirement. CRC32C passes
82 parity cases against the old binary, covering decimal/base64 output, empty
input, random file ranges, invalid arguments and files/offsets over 4 GiB.
The full source Docker build passes. CRC32C and GKE reproduce the exact published
executable hashes. SSM differs only in Go/GNU ELF build-ID notes; executable
content is equal after normalizing those notes, as recorded in the parity receipt.
Full local validation logs live under
`/workspaces/projects/security27-high-zoekt/skypilot-tools/`.
