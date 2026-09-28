# Organization hierarchy and memberships

The hierarchy CLI uses the selected ADP deployment and its human login. Targets
are canonical IDs; names are create/update values, never global selectors.
Every managed operation names its organization explicitly.

```bash
adp admin org list --page-size 20 --max-pages 2 --json
adp admin org show --org ORG --json
adp admin department list --org ORG --json
adp admin team list --org ORG --department DEPARTMENT --json
adp admin member list --org ORG --json
adp admin team members list --org ORG --team TEAM --json
adp admin tenant org-links list --tenant PARENT --json
```

Lists have bounded page/page-size/max-pages controls. Organization hierarchy
lists are filtered on the server; department administrators remain inside their
department. Team member lists include secondary memberships, rather than only
users whose primary pointer names the team. Revoked memberships remain visible
as retained identity records with `membership_status: revoked`.

Create IDs are explicit so an uncertain outcome has a stable target to inspect:

```bash
adp admin org create --id OWNED_ORG --name 'Owned test org' --dry-run --json
adp admin org create --id OWNED_ORG --name 'Owned test org' --yes --json
adp admin department create --org OWNED_ORG --id OWNED_DEPT --name Engineering --yes --json
adp admin team create --org OWNED_ORG --department OWNED_DEPT --id OWNED_TEAM --name Platform --yes --json
adp admin member add --org OWNED_ORG --existing-user PLATFORM_USER_ID --role member --yes --json
adp admin member add --org OWNED_ORG --new-user --email new@example.com --name 'New person' --role member --yes --json
```

Organization creation calls the canonical identity API, which provisions the
default department/team and identity mappings. It never calls the retired
`POST /admin/organizations`. New-user creation is explicit, does not invent a
password, and sends no invitation. Existing-user placement remains platform-admin
only; organization admins request new members through the access-request flow.
Readback provides the canonical organization-local user ID for subsequent changes.

Inspect a revision before modifying existing state. Updates send only named fields:

```bash
adp admin department show --org ORG --id DEPT --json
adp admin department update --org ORG --id DEPT --name NewName --dry-run --json
adp admin department update --org ORG --id DEPT --name NewName --expected-revision REVISION --yes --json
adp admin member update --org ORG --user LOCAL_USER_ID --role member --expected-revision REVISION --yes --json
adp admin team members add --org ORG --team TEAM --user LOCAL_USER_ID --role member --expected-revision REVISION --yes --json
adp admin team members remove --org ORG --team TEAM --user LOCAL_USER_ID --expected-revision REVISION --yes --json
```

Member and team-membership commands expose the member revision through
`--dry-run`. Team add/remove affects only the named membership, preserving other
teams. Existing server rules determine primary-team promotion: removing the
primary promotes a surviving membership; requesting a second primary is refused.
Role ceilings and self-role-change restrictions remain server-enforced.

Delete previews show dependent table names and the permitted action. Deletions
refuse dependencies; there is no cascade option. Remove children and configuration
explicitly before deleting their parent. A concurrent change invalidates the
revision and requires fresh inspection.

```bash
adp admin team delete --org ORG --id TEAM --dry-run --json
adp admin team delete --org ORG --id TEAM --expected-revision REVISION --yes --json
adp admin member remove --org ORG --user LOCAL_USER_ID --dry-run --json
adp admin member remove --org ORG --user LOCAL_USER_ID --expected-revision REVISION --yes --json
adp admin tenant org-links add --tenant PARENT --github-org-id NUMERIC_GITHUB_ORG_ID --dry-run --json
adp admin tenant org-links add --tenant PARENT --github-org-id NUMERIC_GITHUB_ORG_ID --expected-revision REVISION --yes --json
adp admin tenant org-links remove --tenant PARENT --github-org-id NUMERIC_GITHUB_ORG_ID --expected-revision REVISION --yes --json
```

Organization-link revisions bind the parent and child relationship. The local
`--tenant` in `admin tenant org-links` names that relationship's parent; the global
`adp --tenant` flag still selects the authenticated workspace before the command.

Membership removal retains the global login, identity links, credentials, usage
and unrelated organization memberships. It stores a durable `revoked_at` tombstone
and removes only the selected organization's team memberships. Native-token,
workspace/lease and hosted-human authorization paths reject removed membership.
Workspace discovery remains available so a person can select another authorized
organization. Explicit authorized `member add` reactivates membership; routine
onboarding upserts cannot silently undo removal.

Migration `075_membership_revocation` must run before deploying the gateway code.
It adds a nullable timestamp, preserving existing legacy onboarding. Downgrade is
refused while tombstones exist because removing them could restore access. Run this migration
through the standard deployment process, not from the production CLI.

All writes use capability discovery, confirmation and JSON readback. Lost or
malformed acknowledgements report `pending` with the original request; no write
is replayed. Observed state after an uncertain acknowledgement does not prove
which request changed it.

E29 adds bounded administrator reads to the existing EC2 nightly workflow.
Offline tests cover revisions, canonical membership removal, identity retention,
secondary teams and protected API contracts. Live create/update/remove, subsequent
member access, foreign/insufficient-role contrasts and owned-fixture cleanup remain
separate acceptance evidence; E29 alone does not complete those criteria.
