# Zoekt security rebuild

The immutable upstream image retains the deployed Zoekt source revision and index
format. Only Alpine packages are upgraded. The published candidate repairs the
six Critical curl/OpenSSL matches; its raw scan reports 0 Critical and 216 High.
The BusyBox shard helpers are separately pinned to a scanned 1.37.0 image.

Build with `docker build -t zoekt-security modules/agent-context/images/zoekt`.
Re-scan and publish a new immutable digest before changing the manifest pin.
Evidence and native index/search, unchanged executable hashes and shard-sync
fixtures are in `docs/security/runs/2026-09-27/zoekt-critical/`.
The ingestion indexer remains compatible because the executable bytes are unchanged.
Live rollout and S3-backed shard synchronization require separate acceptance.
