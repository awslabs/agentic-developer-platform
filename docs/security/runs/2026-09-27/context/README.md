# Context and chat security candidates

| Component | Frozen live raw Critical / High | Candidate raw Critical / High |
|---|---|---|
| DeepWiki | 52 / 197 | 25 / 123 |
| LiteLLM | 13 / 90 | 0 / 50 |
| Chat agent | 0 / 9 | 0 / 0 |

No new Critical/High advisory-package pairs remain in any candidate. Exact local,
registry and config digests, SBOM/scan hashes and publication receipts are included.
The frozen scanner database is unchanged; no suppressions were used. These are
raw package matches, not unique CVE totals or live deductions.

DeepWiki advances the official Node release to 22.23.3 in builder and runtime,
removing three High findings on the previously accepted 22.23.0 recipe. Its real
API/UI, current/legacy cache and Git fixtures pass under UID/GID10001, a read-only
root and no network. Installed Next tar and CPython backport checks pass. Exact
OpenSSH client compatibility passes using disposable keys and private loopback:
strict host-key refusal, encrypted/invalid keys, rekey, Git clone/fetch/push and
SCP/SFTP. Remaining curl and OS findings are still open.

LiteLLM is rebuilt from the accepted recipe. Real offline HTTP tests cover
master-key authentication, denied missing/wrong keys, a local provider roundtrip,
malformed input, provider outage and protected path writes. `config.env` now
preserves an explicit LITELLM_IMAGE override, allowing an immutable image to be
selected by the existing deployment script.

Remove unused apk/libapk/system-zlib from the chat runtime; Node does not link
that library and its built-in compression check passes. npm/bash/dumb-init remain.
The chat recipe is `agent/Dockerfile`, as confirmed by publish-shared-image.sh;
`agent-worker-image/Dockerfile` builds the separate worker runtime. The chat
fixture executes the packaged orchestrator, persona loading, context/memory,
tool assembly, delivery response and acknowledgment logic with cloud/model
transports mocked. Success and failure run as10001, read-only, network none,
with BG_CONFIG_DIR isolated; failures retain the message for retry and do not
expose the synthetic secret. This does not prove live Bedrock routing or SQS.

Images were published using unique tags and remote digest checks. No live rollout
has been verified. To deploy, use the receipt references for DEEPWIKI_IMAGE,
LITELLM_IMAGE and the chat ScaledJob's AGENT_IMAGE through the existing reviewed
component workflows, preserving workload settings and credentials. Require real
provider/embedding/chat checks, existing DeepWiki cache compatibility, observed
imageIDs and a fresh register before closure. Save old objects/image references
for rollback; reverting restores prior exposure. The initial DeepWiki tag
security27-489f17109 is superseded and must not be promoted.
