# Agent control signing-key rotation

This procedure rotates the gateway's Ed25519 envelope key while workers continue
running. It requires the #5028 listener with `ADP_CONTROL_ENVELOPE_KEYS_FILE` and
the platform-managed ConfigMap projection. It does not enable unsupported
controls or replace the story's live acceptance.

Follow the [deployment guide](../adp-platform-deployment/deploy-with-agent.md)
for account/environment confirmation and approval of an actual change. The
implementation has not been deployed as part of this PR.

## Key locations

The webhook-ingress Terraform state owns two Ed25519 slots:
`tls_private_key.agent_control_envelope` (primary) and
`tls_private_key.agent_control_envelope_secondary` (secondary). The active slot's
private key is delivered only to the gateway-namespace
`agent-authority-signing` Secret. Treat Terraform state and saved plans as
secret material. Do not print private keys or put them in issue comments.

Workers receive public PEM keys through the `adp-control-verification-keys`
ConfigMap in `adp-agents`, mounted as a directory at `/var/run/adp-control-keys`.
Do not use a `subPath` mount: Kubernetes would stop updating that file in existing
pods. Listeners read the projection on each verification and return the currently
loaded public IDs in authenticated `GET /agent/ping` as `verification_key_ids`.
The ID is the first 16 hexadecimal characters of SHA-256 over the PEM text.

## Handover

1. Record the current active slot, gateway rollout and all active worker
   invocation/generation pairs. Confirm each worker has the projection and the
   reload-capable listener. Workers that predate this implementation require a
   managed recovery; changing their startup environment will not update them.
2. Keep `agent_control_signing_key_slot` on the outgoing slot and set
   `agent_control_publish_both_keys = true` in the environment's Terraform
   configuration. Review and apply the plan. New installations already publish
   both slots. For later rotations, regenerate only the inactive slot while it
   is retired, then publish both. Never replace the active key during staging.
3. From the trusted gateway path, authenticate to each active worker's ping
   endpoint and record only invocation, generation and `verification_key_ids`.
   Confirm both IDs on every active listener. Do not assume a fixed ConfigMap
   propagation delay. An unavailable worker leaves the handover unproven.
4. Change `agent_control_signing_key_slot` to the incoming slot, keeping both
   public keys published. Review/apply that change, then roll the gateway using
   the normal deployment path so the new Secret value and SSM key ID reach every
   gateway process together. Existing workers keep their sockets and journals.
5. Verify an authorized request signed by the incoming slot and an identical
   command retry on the same worker generation. The retry must return its recorded
   outcome. Verify all gateway processes use the incoming slot. Start the
   retirement wait only when the last outgoing signer has stopped.
6. After at least the 30-second maximum forwarding validity window, set
   `agent_control_publish_both_keys = false`. Review/apply the change. Confirm
   every active listener reports only the incoming ID, rejects an otherwise-valid
   old-key assertion and accepts the incoming key. Only then record retirement
   as complete. A stale projection is an incomplete retirement, not proof of a
   bounded key-revocation delay.

The ordinary run-identity and control feature flags retain their approved values
throughout this procedure. Avoid applying unrelated Terraform changes in a key
handover plan. All plans and validation belong to the selected environment.

## Failure and recovery

Before switching the signer, a failed staging check means keep signing with the
outgoing slot. During overlap, a gateway rollout failure can return to the
outgoing slot while both public keys remain published. After retirement, a
rollback must first republish the required public key and verify every listener;
do not switch to a key that running workers no longer accept.

A missing or malformed projected keyring refuses signed control requests and
does not restore keys from the startup environment. The running task and journal
continue. Key removal is not instantaneous cryptographic revocation: already
forwarded assertions have their original short validity, and queued actions still
require the authorization recheck for their implementation before execution.

## Evidence

Record the two public IDs, active slot before/after, gateway rollout completion,
per-worker observed IDs and unchanged generations, allowed new-key request,
rejected retired-key request, preserved replay outcome, and final Terraform
configuration. Credentials, private keys and instruction bodies are excluded.
The source tests exercise real HTTP reload and retirement on one continuous
listener; actual multi-worker rotation remains a live acceptance requirement.
