# Zoekt High remediation candidate

The frozen database reports **0 Critical and 0 High** for the published image,
replacing the baseline's 216 High occurrences (20 canonical CVEs). The remaining
native matches are 1 Medium, 4 Low and 3 Unknown; none are suppressed. Unknowns
are GO-2026-5841 (compress dictionary encoder; GHSA-259r-337f-4rfw) and two
GO-2026-5932 matches (unmaintained x/crypto/openpgp).

The source revision and v16 shard format remain unchanged. The Go compiler and
locked modules are updated; the search runtime contains static binaries and CA
roots. Git, ctags and shell commands belong to ingestion, not this server.
The ingestion Dockerfile copies the matched static indexer from the same digest.

Validation: upstream `go test ./... -short` passes; 29 ADP shard-sync/mountpoint
checks pass. One upstream watcher timing case initially failed under concurrent
compilation, then passed five repetitions and the full suite rerun. HTTP search
returned the fixture in all three combinations: old shard/new server, new
shard/old server and new shard/new server. UID/GID remain 100:101.

`publication.json` binds registry index, amd64 manifest and config digests and
records executable hashes and rollback image. `scan-receipt.json` binds raw SBOM
and Grype output hashes. Full raw local evidence is in
`/workspaces/projects/security27-high-closure/zoekt-final-scan/` and
`/workspaces/projects/security27-high-zoekt/`. This is artifact evidence;
production rollout and live shard acceptance are separate.
