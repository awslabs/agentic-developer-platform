# Slack test-channel qualification

This describes the scoped, **not yet deployed** Slack qualification for [#8077](https://github.com/aws-e/adp/issues/8077). Repository tests use synthetic providers; they do not prove live consent, Slack side effects or cleanup. The coordinator owns private dev fixture handoff and protected release; the account owner must select the OAuth app, explicitly re-consent to `chat:write`, designate the isolated channel and approve deletion. The developer repairs in-scope defects. A merged PR is not post-deploy acceptance.

## Private fixture and authority

Keep the real owner, workspace, channel, approvals, tokens and evidence in the protected operator store; never commit or log them. The following **fictional illustration is not an approved channel or deployable configuration**. Its zero-filled IDs and owner name must be replaced by the account owner's privately confirmed bindings. Exactly this JSON shape is required for `ADP_TOOLS_SLACK_POST_FIXTURE`; the separately set private `ADP_TOOLS_SLACK_POST_ENABLED=true` switch is also required. The flag and a JSON claim do not themselves grant OAuth consent or deletion authority.

```json
{
  "owner_id": "owner-example",
  "team_id": "T00000001",
  "channel_id": "C00000001",
  "message": "ADP isolated fixture message.",
  "run_marker": "7d787d28-5c0e-4c86-b56f-fc13bb332b00",
  "approved": true,
  "isolated": true,
  "cleanup_method": "chat.delete"
}
```

The owner must confirm that the `C` or `G` channel is an isolated test channel, never an ordinary channel or a person. The exact message must not contain mentions, markup or newlines. Generate a fresh UUIDv4 marker for **each run**, pin it before discovery, and send only `<message> [run:<marker>]` once. Reusing a marker is refused across receipts, team changes and reconnects. Do not infer a granted vendor scope from the saved requested scopes: verify the current owned user token and its Slack user/workspace and `X-OAuth-Scopes`. A read-only `users:read` connection remains useful and cannot discover or dispatch posting; `chat:write` requires the separate explicit user re-consent.

Before enabling posting, the owner must approve an executable `chat.delete` cleanup method for messages authored by this posting user, including the Slack timestamp obtained from an independent marker search if the reply was lost. Provision the separately authorized, read-only provider observer for exact channel/user/text/marker readback and deletion confirmation; the posting connection is **not** automatically granted channel-history scopes. Lack of channel read authority or a way to delete the user's message blocks the live write cell. The `cleanup_method` string is only an admission claim, not proof that cleanup works. Do not publish provider tokens, message bodies from private fixtures or raw receipts in the PR.

## Controlled run and cleanup

Run SCN-8077-01 (read-only denial), SCN-8077-02 (exactly one test-channel message) and SCN-8077-03 (truthful errors and uncertain no-resend) against isolated approved fixtures before review; their repository fixture tests are not live evidence. SCN-8077-04 runs **only after release** through the existing protected workflow for the installed CLI, fresh Claude Code and fresh Codex. Pin actual source/workflow revisions, client versions and images. Obtain a private, current all-in quote (including model, provider, readback and cleanup) before each native client cell; missing pricing or owner approval blocks that cell. Bound a cell to one stateful session, a 300-second TTL, a 600-second cleanup deadline and at most $20 for the full test run. Do not provision a validation service just to test.

For each run, save only safe source/run/attempt IDs and independent yes/no or count assertions in the protected evidence. Record provider post-call count and channel message count **before** the call. Check exactly one matching user/channel/text/marker message afterward; prove zero calls for denial, and do not post to another destination. An HTTP 200 `ok=false` is a tool error. If a reply is lost or a side effect is unknown, read the durable receipt and independently read back the channel; never resend the same marker or assume that an absent immediate read proves no post. Preserve the original receipt and uncertain status until independently resolved.

Use the owner's approved deletion mechanism only on this run's marked messages, including uncertain posts discovered by independent readback. Independently poll the isolated channel until those messages are absent within the original 600-second deadline. Keep durable receipts and infrastructure; remove only run-owned messages. Missing confirmation is **blocked**, not a cleanup success. On any failure, stop new posts, preserve attempt identity and hand the unresolved receipt and cleanup to the coordinator/account owner without pasting private evidence into public logs. The coordinator and developer report AC-01 through AC-04 separately; AC-04 stays pending until the deployed three-client evidence and cleanup have passed.
