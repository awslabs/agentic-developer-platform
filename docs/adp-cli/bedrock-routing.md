# Personal Bedrock routing and administrator mappings

These commands extend the existing Bedrock helper and routing APIs. Existing
`connect`, `list`, `verify`, and `status` forms remain available. No new IAM role,
AWS account connection, routing precedence, or destination-delete command is
introduced by this lifecycle surface.

## Personal selection

```sh
adp bedrock status --json
adp bedrock select --connection CONNECTION_ID --dry-run --json
adp bedrock select --connection CONNECTION_ID --expect-revision REVISION --operation-id UUID --yes --json
adp bedrock reset --dry-run --json
adp bedrock reset --expect-revision REVISION --operation-id UUID --yes --json
```

The preview returns the effective destination, billed AWS account, source rung,
administrator override, and the caller's own selection. A platform account with
an unreported account number stays explicitly unknown; it is not invented from
the CLI's local AWS credentials. Select accepts only the caller's own verified,
selectable connection. The server rechecks ownership and probes it before saving.
The chosen account is bound to the write so a changed connection cannot silently
substitute the billed account. An administrator-authored user rule cannot be
overwritten or removed through these commands.

Reset removes only the personal override and reports the inherited effective
route. It does not disconnect the AWS account or remove its IAM resources. The
canonical precedence remains user, team, organization, platform. Configured
routing is not evidence that an actual inference request used that account.

## Administrator mappings

```sh
adp admin bedrock mappings list --scope org --target ORG_ID --page 1 --page-size 20 --json
adp admin bedrock mappings show --scope user --target USER_ID --json
adp admin bedrock mappings show --scope team --org ORG_ID --target TEAM_ID --json
adp admin bedrock mappings set --scope user --target USER_ID --destination DESTINATION_ID --dry-run --json
adp admin bedrock mappings set --scope user --target USER_ID --destination DESTINATION_ID --expect-revision REVISION --operation-id UUID --yes --json
adp admin bedrock mappings delete --scope user --target USER_ID --expect-revision REVISION --operation-id UUID --yes --json
```

These routes retain the existing **platform administrator** guard; organization
administration alone is insufficient. Supply exact IDs. A team target also
requires its parent organization; missing targets never broaden to an
organization-wide change. `list` uses server pagination and an exact scope
filter; `has_more` describes the returned page. `show` returns `revision:
"absent"` when no rule exists at that exact scope. Destinations and mappings are
separate inventories. Use the existing `bedrock list` to inspect destinations and
`bedrock status --user USER_ID` for a person's effective route.

Set requires a verified destination, binds both the reviewed mapping revision
and the destination revision, and reuses the canonical save-time account probe.
A failed probe does not change the mapping. Deletion removes only that exact
mapping and reveals lower-precedence rules; it does not remove the destination,
connection, or IAM role. Team/organization mappings do not imply that people
with a narrower user rule changed effective accounts.

## Existing-connection links

```sh
adp admin bedrock connection-link add --destination DESTINATION_ID --connection CONNECTION_ID --dry-run --json
adp admin bedrock connection-link add --destination DESTINATION_ID --connection CONNECTION_ID --expect-revision REVISION --operation-id UUID --yes --json
adp admin bedrock connection-link remove --destination DESTINATION_ID --connection CONNECTION_ID --expect-revision REVISION --operation-id UUID --yes --json
```

The named destination must already be backed by that exact connection, owned by
the target organization, and match its account and role. The API extension binds
an existing destination rather than silently creating a different ID. A current
link for the same connection/organization at another destination conflicts.
Destination metadata distinguishes `source_connection_id` from `connection_id`
(the latter is the active shared-use link).

The canonical link endpoint performs a fresh capability probe before saving the
association. Remove refuses while any mapping references it. Its existing unlink
semantics remove the unused derived registry row and association while retaining
the original vault connection and AWS role. It does not provision, delete, or
rotate IAM roles or secrets. Unsupported arbitrary destination deletion remains
unavailable.

## Revision and retry behavior

Without `--yes`, mutations return a read-only preview. `--dry-run` always wins.
`--yes` additionally requires the revision from the reviewed preview and a stable
UUID operation ID; it cannot approve a revision the caller has not supplied.
The server serializes routing writes, including older browser callers, across
the absent-row case, the administrator-pin check, probe, and commit. New CLI
writes check revisions under that lock. A changed rule yields a fresh readback
and a conflict; the CLI does not silently overwrite it.

Before delivery the CLI records a private receipt, bound to deployment,
authenticated actor/tenant, exact request, and revision. Keep the same operation
ID after an uncertain response. Replay reads current state and never resends the
mutation, even if a previous write was acknowledged. A later writer may already
have changed the route, so matching readback alone is not treated as proof of
who won. Failed/malformed/lost acknowledgements remain pending (exit 4); review
readback before creating a new intent. CLI permission/usage refusals preserve
the shared structured error contract. Legacy servers without routing revisions
or paginated mappings are refused before a lifecycle write.

## API and acceptance coverage

Personal operations use `/me/bedrock-routing/selection`. Admin operations reuse
`/admin/bedrock-routing/mappings`, `/mappings/{scope}`, `/destinations`, and
`/connection-links`. Existing unpaginated mapping readers and unconditioned
browser writers retain their wire forms; optional revision parameters and
pagination are additive. No production CLI command accesses the database.

Nightly E34 uses the existing EC2 regression suite to exercise a self reset
preview (or administrator-pin refusal) and a missing team-parent refusal. It
performs no routing mutation or probe. Full CLI-20-AC-04 remains held until owned
fixtures demonstrate bounded local and hosted inference, recorded account
routing for each applicable actor, restoration of prior mappings, and verified
cleanup. E34 is a reusable regression, not that live acceptance claim.
