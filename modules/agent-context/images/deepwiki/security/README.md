DeepWiki retains upstream commit `d92819a9c9f3b99416e3580ff235fc9d3adf8b89`
and runtime image digest `730b72494f407397c896c586af0a31af9671d509af420e6792d834ff094a9c8f`.
The UI is rebuilt from that commit with Next.js15.5.24; the complete standalone
output replaces the prior UI. The archive and package/lock bytes have SHA256
guards. npm generated the retained lock update in upstream's
`--legacy-peer-deps` mode; only Next and its env/SWC package versions change.
That mode also removes an optional nested next-intl SWC peer. Development
dependencies are included in the builder despite the base's production ENV.

The runtime keeps Node, Python, Git, upstream API code, and `/app/start.sh`.
Available Debian updates and GitPython3.1.59 install through normal package
resolvers and `pip check`. npm/npx are retained at checksum-pinned11.20.0: although start.sh launches
`node server.js` and `python -m api.main` directly, Next's cold SWC download
fallback calls a registry helper that executes `npm config get registry`.
The earlier API-only consumer audit missed this library fallback.

Next15.5.24's compiled tar bundle matches its published source byte-for-byte
and declares tar6.1.15. Replace only that exact bundle with locked tar7.5.21
and its complete runtime dependencies/licenses. The adapter preserves Next's
plain CommonJS default import; tar7's non-enumerable `__esModule` marker must
not make Next expect a nonexistent `.default` export. Installation refuses
unreviewed Next versions or original bundle hashes.

`tests/container/deepwiki_runtime.py` must run with network disabled, read-only
root, UID/GID10001, and disposable home, `/tmp`, and `.next/cache`. It tests
actual API/UI startup, current/legacy cache save/read/delete, GitPython local
commit and API branch detection, Node/Next loading, denied protected writes,
and the pinned npm/npx tools. The tar fixture additionally exercises the actual Next cold-download and cached-extraction consumer against a loopback registry, plus bounded malformed archive controls. Exact runtime scans retain all original findings for per-component review. A build or fixture alone is not production acceptance.
