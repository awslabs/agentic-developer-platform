# Agent Mail container validation

Build the image from this directory, then run the disposable functional check:

```sh
docker build -t agent-mail:review .
docker run --rm --tmpfs /data:uid=10001,gid=10001,mode=0700 \
  -e HOME=/data -e DATABASE_URL=sqlite+aiosqlite:////data/mail.db \
  -e STORAGE_ROOT=/data/mailbox -e HTTP_HOST=0.0.0.0 \
  -v "$PWD/container_runtime_check.py:/tmp/check.py:ro" \
  --entrypoint python agent-mail:review /tmp/check.py
```

The check creates only disposable local agents/messages. It covers native imports,
non-root execution, absence of compilers, Git commits, SQLite FTS5, HTTP health,
and authenticated MCP project creation, registration, contact approval, messaging,
inbox retrieval and search. It does not connect to a deployed service.

SQLModel now interprets bare datetime annotations as timezone-aware. The pinned
upstream deliberately stores naive UTC timestamps, including existing SQLite data.
The hash-guarded build patch declares that existing storage contract explicitly
with Pydantic's `NaiveDatetime`; it does not rewrite stored timestamps or schemas.
An upstream source change requires reviewing the patch rather than silently
applying it to new source.

The runtime uses Python 3.14 on supported Alpine 3.24, with a pinned base digest.
Git (including remote transports) and SQLite remain installed. Compilation tools
are confined to the build stage. This distribution change requires functional
validation; lower cross-distribution scanner counts are not a count of CVEs fixed.
Original security finding selectors remain retained in the security ledger.
