# Portable release fixtures

The shared release accepts either an existing service fixture or a private
version-one bundle in that service's protected `ADP_L5_*_LIVE_CONFIG_JSON` secret.
The bundle carries client session data and paths without depending on an
operator's filesystem. It does not grant Product or provider access: each
service still validates its current sessions, source selections and authority.

```json
{
  "bundle_version": 1,
  "source_sha": "0000000000000000000000000000000000000000",
  "fixture": {
    "source_sha": "0000000000000000000000000000000000000000",
    "session_file": "fixture://clients/session.json",
    "journal_file": "fixture://evidence/run.jsonl",
    "cli_executable": "checkout://modules/gateway/cli/adp"
  },
  "files": {
    "clients/session.json": {"json": {"session": "REPLACE_PRIVATELY"}}
  },
  "directories": ["clients/home"],
  "environment": {
    "ADP_L5_FIXTURE_PRIMARY_TOKEN": "REPLACE_PRIVATELY"
  }
}
```

`fixture` contains the selected service's complete fixture, with its existing
schema. The short example shows only transport fields. Both the bundle and the
service fixture must match the reviewed source revision; transport never
repins an older authorization automatically.

`fixture://` references resolve under the runner's private service directory.
Files contain JSON data and use mode `0600`; directories use `0700`. Parent
directories are created automatically. `checkout://` may select only the
reviewed `modules/gateway/cli/` tree. Executable code comes from that checkout,
not the private bundle. Existing service-specific embedded client formats can
continue to be used without the bundle.

Optional environment entries must start with `ADP_L5_FIXTURE_`, contain a
single line, and cannot replace existing runner variables. GitHub masks each
session value before it is made available to subsequent steps. Security's
existing separate credentials secret remains supported.

The materializer rejects traversal, symlink redirection, conflicting paths and
files under `evidence/`. It does not overwrite previous files or supply completed
runs, provider results or publication receipts. The exported fixture digest
identifies the rendered service fixture. Retain and reconcile an interrupted
attempt through the service driver rather than deleting its journal and
resubmitting provider work.

Release order is: verify the selected driver, publish the immutable package,
install paused service roots, materialize and validate client prerequisites,
resume compatible consumers, perform normal Product setup and execution, then
independently read the original native results. Preflight must work on a fresh
installation and cannot require an already completed native run. The release
renews its AWS deployment session before final readback; that does not extend
or replace a Product user's authorization. Browser dependencies are installed
for the service UI checks. Full qualification still requires the service's
original acceptance and downstream reuse evidence.
