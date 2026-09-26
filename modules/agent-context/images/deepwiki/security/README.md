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
resolvers and `pip check`. npm/npx and their complete bundled dependency tree
are removed only from the runtime: start.sh launches `node server.js` and
`python -m api.main` directly. API subprocess consumers use Git; npm-related
API configuration entries are repository filenames to exclude, not commands.
The build stage retains npm.

`tests/container/deepwiki_runtime.py` must run with network disabled, read-only
root, UID/GID10001, and disposable home, `/tmp`, and `.next/cache`. It tests
actual API/UI startup, current/legacy cache save/read/delete, GitPython local
commit and API branch detection, Node/Next loading, denied protected writes,
and absent npm/npx. Runtime scan evidence must prove the removed dependency
paths are absent. A build or fixture alone is not production acceptance.
