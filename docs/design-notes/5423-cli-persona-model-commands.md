# Design Note: `adp models` — CLI persona-to-model mapping commands (Issue #5423)

> **Status**: **PROPOSED, pending the #5417 cross-story synthesis.** The command
> surface — human self, authorized human administration, and service-principal self —
> is designed, and **no decision in this note is open** (§9). It consumes two
> capabilities owned by #5419 (PMM-02): the canonical service-principal identity
> slice and the persona-mapping endpoints themselves (§9.3).
> **Author**: @agent-architect
> **Date**: 2026-09-18 · **Rev-4** (revised against the third-pass review at `b9e8e96e`,
> and reconciled against PMM-03 at its current head `25ece717` rather than the
> `6d5b2d10` Rev-3 read)
> **Issue**: #5423 — [PMM-05] CLI self and service-account persona-model mapping commands
> **Parent**: #5417 (EPIC) · **Binding decisions**: #5418 D1–D6, and the unified
> architecture rulings on #5417 (identity, compatibility, probes, authority, ARC, API),
> which **supersede conflicting story-local recommendations** including this note's
> earlier ones (recorded in §9)
> **Depends on**: #5419 (PMM-02 canonical identity + storage + API), #5420 (PMM-03
> catalogue and compatibility registry)
> **Coordinates shared CLI files with**: #5039, #5179, #5185 (see §7.2)
> **Mode**: Per-issue design. No runtime code in this story's artifact.
> **Revision checked**: default branch at `ae598410`; every citation re-verified for
> Rev-3 against the working tree.
>
> **Why "proposed" and not "ready".** The binding vocabulary and precedence
> baseline this note builds on is `docs/design-notes/5417-per-invoker-persona-model-mapping.md`,
> which is **open as PR #5436 and not merged**. Its §8.3 states it is the binding PMM-01
> baseline that the eight sibling stories extend. Until it merges, this note conforms to a
> contract that can still move, so it is proposed rather than final. That is a status
> caveat, not an open decision: nothing below waits on an operator answer.
>
> **Reconciled against that PR's current head, `8edc055c` (Rev-4), not the `78787bd1`
> Rev-3 this note's Rev-2 was written against.** Rev-4 adopts the unified rulings as
> U1–U6 and settles the one decision Rev-3 left open (the persona-to-class owner is
> PMM-03). Two Rev-4 changes bind this surface directly and are absorbed below: its §6.8
> requires PMM-04/PMM-05 to show **which compatibility class** a displayed default belongs
> to rather than one platform-wide value (§4.3), and to surface D2 refusals **verbatim
> through `explain`** (§4.3, §5.3). Rev-4 notes this story was "not re-read at head" when
> it was written, so those obligations arrive here unverified against this text; both are
> now satisfied explicitly rather than by assumption.
>
> **Rev-4 change summary.** Three corrections the third-pass review required, plus two
> findings that only surfaced on reading PMM-03 at its current head. All five change what
> the developer builds:
>
> 1. **"One handler set" was wrong for one of the three callers.** Human self and
>    service-principal self do share one handler — API Gateway strips `/agent`. Human
>    *administration* uses the distinct target-taking `/service-principals/{id}/…` routes,
>    which cannot be the self handler because the self handler's security property is that
>    no target can be expressed on it. What is common is the schema, the refusal vocabulary
>    and the exit codes. §1 and §9.4 are rewritten.
> 2. **A stale write response is a protocol violation, never a reportable success.** Rev-3
>    allowed the helper to report an acceptance-plus-staleness, which makes the CLI the
>    place a D3 violation becomes an exit 0 with a caveat. §5.3 now refuses to certify it
>    either way, and **every** stale-evidence refusal is exit 4 regardless of whether it
>    arrives as a 422 reason code or as a marker on a 2xx. §4.4 and new AC-06a/AC-06b pin
>    it, including the remap the transport's blanket non-401/403 → 5 would otherwise defeat.
> 3. **D4's identifier is a Claude-class candidate, not the canonical default** (§2.5).
> 4. **The catalogue publishes no alias, so `catalog` cannot tell a user what to type into
>    `set`** (§9.7 item 1). New, from PMM-03's `25ece717` §6.2. §5.2 forbids a local alias
>    table, so this is a contract gap to raise on #5420, not one to paper over.
> 5. **In PMM-03's shipped posture nothing is selectable and every save is refused**
>    (§9.7 item 2). §10's "after #5419" row for `set` was wrong; success paths reach live
>    proof only in PMM-09.
>
> **Rev-3's stated residual dependency is discharged and withdrawn.** It said PMM-03
> needed a persona-row class attribute, a candidate-not-default restatement, and a
> disabled-probe posture. At `25ece717` all three are in PMM-03's note (§2.4, §3.3, §4.7).
> Reporting it as live would have sent an operator to sequence work already designed.
>
> **Rev-3 change summary** (retained; these corrections still stand):
>
> 1. **Canonical identity is not `service_accounts.id`.** Rev-2 said the server must
>    join a signed caller's role ARN to a canonical Postgres `service_accounts.id`.
>    The identity ruling is that the canonical owner of a preference is an **opaque,
>    immutable ADP `canonical_service_principal_id`**, reached through a tenant-scoped,
>    source-qualified alias registry over **three** alias sources — including Cognito
>    M2M `client_id`, which Rev-2 missed entirely. Raw `service_accounts.id`,
>    `agent_name`, `client_id` and role ARN **never** own a preference. §2.7 and §9.3
>    are rewritten.
> 2. **No ambient provider chain.** Rev-2 claimed no static credential was possible
>    while specifying `session.get_credentials()`, which returns exactly that from
>    `AWS_ACCESS_KEY_ID` or a shared credentials file. §5.5.3 now states a verifiable
>    temporary-credential rule with no static and no bearer fallback.
> 3. **No duplicate backend route.** Rev-2's "P-1 `/agent/`-prefix mount" was wrong:
>    `/agent/{proxy+}` already **strips** `/agent` and forwards to the same pod path,
>    so the existing single `/me/persona-models` handler is already reachable by a
>    signed caller. P-1 is withdrawn; §5.5.1 records the mechanism.
> 4. **Three contradictions removed** — unfiltered `catalog` versus D6, "same schema"
>    versus a different transport path, and the §10 claim that signing was unblocked.

---

## 1. What this story delivers

A new command area, `adp models`, on the existing ADP command-line tool, serving
**three callers with one contract**:

| Caller | What it does | How it proves who it is |
|---|---|---|
| A signed-in person, acting on themselves | Read the catalogue, see which model each persona resolves to and why, change a selection, undo it, preview a change | The existing Cognito bearer token (§2.1) |
| A signed-in person, administering a service principal they are entitled to manage | The same, targeted at a named canonical service principal | The same bearer token; entitlement decided server-side |
| A registered service principal, acting on itself | The same, on its own mappings, unattended | A SigV4 signature over the request, from the role it already runs as (§5.5) |

All three share **one request/response schema, one refusal vocabulary and one set of
exit codes**. A script that reads `adp models mappings list --json` does not need to
know which of the three produced it — the envelope, the field names and the `source`
values are identical.

**What is shared is the contract, not the handler — and Rev-3 overstated this.**
Rev-3 said all three "reach one backend handler set", which is not what the API
ruling says and is wrong for one of the three:

| Callers | Handler | Why |
|---|---|---|
| Human self **and** service-principal self | **The same single handler**, at `/me/persona-models…` | The self routes exist once. A signed caller sends to the `execute-api` invoke URL at `/agent/me/persona-models…`; API Gateway **strips the `/agent` prefix** and forwards to that same handler (§5.5.1). One handler, two front doors, distinguished only by which credential arrived. |
| Human administering a service principal | **Distinct handlers**, at `/service-principals/{canonical_id}/persona-models[/{persona_key}]` | These take the target in the path, so they cannot be the same route as a self route that takes no target. They are human org-admin only and tenant-checked. |

The distinction is not pedantry, and getting it wrong cuts both ways. Collapsing
administration into the self handler would put a target parameter on a route whose
whole security property is that it has none (§4.1). Splitting the self handler into a
human one and a machine one would create the duplicate backend router the ruling
forbids and this epic exists to remove — the failure Rev-2's withdrawn P-1 would have
caused (§9.3).

So two things differ across the three callers, and neither is a schema difference:

- **The credential.** Bearer token or SigV4 signature.
- **The route shape and the external URL.** Self versus target-taking; gateway origin
  versus `execute-api` invoke URL.

What does **not** differ: the request and response schema, the refusal vocabulary and
the exit codes, across all three (§9.4).

It adds **no new stored credential** and no model invocation. The machine path adds
a second *transport* (request signing) but no secret at rest: the signature is
computed from credentials the role already holds, and nothing is written to disk.
It calls the endpoints #5419 builds and the catalogue #5420 builds, and nothing else.

**The property this surface exists to protect.** A command that acts on the caller's
own mapping must not accept "whose mapping" in any position. The cost of getting this
wrong is not an error message: a scripted typo rewrites a colleague's model policy,
their agents start running on a model they did not choose, and the spend is attributed
to them. §4.1 makes the absence of a target argument a structural property rather than
a validation rule.

Adding the machine caller gives that property a second, sharper edge, and §5.5.4
holds it: **`--service-principal ID` must never be accepted on the signed path.** On the
human path that flag means "administer this other principal, if the server says I may".
If the signed path also honoured it, any registered role could name any service
principal as "self" and the only thing standing between it and another machine's policy
would be a server-side check that the CLI had just invited it to try. The signed path
therefore reaches only `/me/*`-shaped endpoints, which take no target at any position,
and the flag is not defined on it at all.

---

## 2. Verified starting point

Everything in this section was read on the revision named above. Where the issue body
asserts something different, §3 says so.

### 2.1 The shared CLI contract is real and sufficient for most of this surface

| Facility | Location | Use here |
|---|---|---|
| `CliError(message, code, exit_code)` | `modules/gateway/cli/adp_common.py:24-27` | Every refusal |
| `Parser` (argparse subclass raising `CliError` with exit 1) | `adp_common.py:30-32` | Usage errors become exit 1 automatically |
| `Api.request` / `api()` | `adp_common.py:69-119` | All calls |
| `access_token()` | `adp_common.py:58-66` | Shells to `bg-cognito-auth.sh token`; single refresh-lock owner |
| `envelope(status, command, detail, next_action)` | `adp_common.py:196-197` | `--json` shape |
| `emit(result, as_json)` | `adp_common.py:200-211` | Returns 0 / 4 / 5 from `status` |
| `report_error(exc, command, as_json)` | `adp_common.py:214-220` | Returns `exc.exit_code` |

HTTP status → exit code is already correct for this story: `adp_common.py:113` maps
`{401: 2, 403: 3}` and everything else to 5. So **AC-04's "refused with the
authorization exit code" needs no new code** — a 403 from #5419's delegated-admin
endpoint arrives as exit 3 through the shared transport.

### 2.2 The dispatcher and its parity fence

`adp` is a bash dispatcher with a closed `case` (`cli/adp:985-1031`). A command area is
one arm delegating through `exec_python_helper` (`cli/adp:777-787`), which checks
`python3`, checks the helper file exists, and `exec`s it so the helper owns the exit
status. Arms are indented **eight spaces** — `tests/cli/test_superplane_dispatch.py:36`
parses them with `re.match(r"\s{8}([a-z|\-]+)\)", line)`, so a differently indented arm
is invisible to the parity test that is supposed to protect it.

`tests/cli/test_superplane_dispatch.py` is the model to mirror: it independently asserts
the `case` arm, the download allowlist, the media type, and both `CLI_FILES` lists.

### 2.3 The closest existing helper

`cli/adp-bedrock.py` (587 lines) is the right structural precedent, and three of its
behaviours transfer directly:

- **The re-read-before-write conflict guard** — `assign()` at `adp-bedrock.py:282-286`
  re-reads the current mapping and refuses when it changed during setup, rather than
  overwriting.
- **`--dry-run` returns before mutating** — `confirm()` at `adp-bedrock.py:138-140`
  returns `False` immediately on `--dry-run`, so no confirmation is sought and no write
  is attempted.
- **`--json` is sniffed from raw argv before parsing** (`adp-bedrock.py:572`,
  `as_json = "--json" in argv`), so a parse failure still emits JSON on stdout.

### 2.4 Packaging: where a new helper must be registered

Verified present today for `adp-superplane.py`, and all of these are load-bearing:

1. `cli/adp` — the `case` arm (`cli/adp:995`).
2. `cli/adp` — the `usage()` help text (`cli/adp:899-944`).
3. `cli/install.sh` — `CLI_FILES` (`install.sh:45`).
4. `cli/adp` — `CLI_FILES` **again**; `adp update` has its own copy (`cli/adp:50`).
   `tests/cli/test_superplane_dispatch.py` asserts the two lists agree.
5. `src/cli_download/routes.py` — `ALLOWED_SCRIPTS` (`routes.py:71-83`).
6. `src/cli_download/routes.py` — `SCRIPT_MEDIA_TYPES` (`routes.py:90-102`).
7. `tests/test_cli_download.py:86-107` — a pinned **exact-set** assertion
   (`assert set(ALLOWED_SCRIPTS) == {…}`) that deliberately fails when the allowlist
   grows, because the route is unauthenticated and nothing should become publicly
   downloadable by accident.

`gateway-deploy.yml` already covers `modules/gateway/cli/**` in both its push filter
(line 7) and its backend-changes regex (line 98), pinned by
`tests/cli/test_deploy_filter_covers_cli.py`. **No workflow edit is required** — the
issue asked for this to be confirmed rather than assumed, and it is confirmed.

### 2.5 Aliases and the Fable example

- `opus5` is a real alias: `model_validate.py:26` → `global.anthropic.claude-opus-5`.
- **There is no Fable alias in either map.** `model_validate.py:35` and
  `model_resolver.py:32` mention Fable only to *exclude* `claude-fable-5` as listed-but-
  not-invocable, pending a non-default data-retention mode.
- `anthropic.claude-fable-5-1` exists **only in pricing data**
  (`alembic/versions/047_claude_pricing_v2.py:1927` and following). A price row is not
  entitlement and not invocability — the whole lesson of #2300.

The parent epic's illustrative command is `adp models mappings set --persona architect
--model fable51`. That alias does not exist, and whether it ever will is #5420's probe
result. Consequence for this story in §5.2.

D4 names `us.anthropic.claude-sonnet-4-6`. **It is a Claude-class candidate, not a
canonical default** — Rev-3's wording here said "the canonical default" and was wrong in
the way the compatibility ruling names: the identifier is a candidate for one
compatibility class until PMM-09 records a bounded invocation using that class's actual
harness request shape, and defaults are keyed by class rather than platform-wide (§5.1).
The catalogue story agrees at its current head: it "records **no default at all**" and
seeds the identifier as a candidate with no invocability evidence
(`docs/design-notes/5420-persona-and-model-catalogue.md` §3.3 at #5434 `25ece717`).

Note also that **no alias resolves to the `us.` form today** — `sonnet46` maps to
`global.anthropic.claude-sonnet-4-6` in both maps (`model_validate.py:31`,
`model_resolver.py:28`), so the identifier is reachable by canonical ID only. That gap is
PMM-09's to close (D4 prefers `us.` for account portability), and PMM-03 agrees at head
that it "must not silently add the alias, because adding it would make an unproven
identifier look routine" (§2.4). This story neither depends on the gap nor fixes it,
because §5.2 forbids the helper from holding any alias mapping of its own.

### 2.6 How a machine principal actually reaches the gateway today

The Rev-1 note asserted that a machine CLI path was simply unavailable. The
transport facts behind that are real and are restated here because they *constrain*
the design in §5.5 — but they do not prevent it.

| Fact | Evidence |
|---|---|
| The CLI's only credential path is Cognito | `access_token()` shells to `bg-cognito-auth.sh token` (`adp_common.py:58-66`); `Api.request` sets `Authorization: Bearer …` (`adp_common.py:78`) |
| No CLI helper signs anything | No `boto3`/`botocore` import anywhere in `modules/gateway/cli/`; the sole textual mention is a prose comment at `adp-superplane.py:349` |
| Service accounts authenticate by SigV4 at the API Gateway edge, not in the app | `/agent` and `/agent/{proxy+}` carry `"x-amazon-apigateway-auth" = { type = "AWS_IAM" }` and inject `X-Caller-Identity` from `context.identity.userArn` (`modules/gateway/infra/modules/api-gateway/main.tf:249-267`, `:270-298`) |
| The ordinary human plane accepts no signature at all | `/` and `/{proxy+}` are `{ type = "NONE" }` — FastAPI validates the JWT instead (`main.tf:203-205`, `:220-222`, and the route map comment at `:22-23`) |
| A signed caller becomes `account_type="service"`, `auth_source="iam"` | `agent_entry_to_token_context()` (`agent_registry.py:245-278`, specifically `:260` and `:263`) |
| An unregistered role is refused, not admitted | `UnregisteredServiceAccountError` → 403 `agent_not_registered` (`middleware.py:413-415`, `:518-522`) |

**The two corrections to Rev-1's evidence.**

1. Rev-1's §9 attributed both `account_type` and `auth_source` to
   `src/auth/middleware.py:173-190`. That range is `require_service_account`, which
   reads `account_type` only; **`auth_source` does not appear in that file's
   dependency at all.** The field is set at `agent_registry.py:263` and declared at
   `src/shared/schemas/auth.py:57` (`auth_source: str = "jwt"  # "jwt" (Cognito) or
   "iam" (API Gateway)`). The conclusion Rev-1 drew is unaffected; the citation was
   wrong and is corrected here.
2. Rev-1 called `bg-auth.sh` "the legacy SigV4 helper", repeating the comment at
   `src/cli_download/routes.py:68-69`. **That description is wrong, and the error
   matters for this story.** `bg-auth.sh` does not sign: it extracts literal
   credentials via `aws configure export-credentials` (`bg-auth.sh:227`) and POSTs the
   access key, secret key and session token in a **JSON body** to `/auth/exchange`
   (`bg-auth.sh:288-296`). The receiving route is disabled by default and returns 410,
   and its own module text calls accepting raw credentials "a credential exposure
   risk" (`src/auth/routes.py:51-54`, `:115-125`, route `deprecated=True` at `:76`).
   So it is not a signer to revive; it is the anti-pattern the review named. §5.5
   signs the request and transmits no credential material.

**What is genuinely reusable.** The platform has one well-worn signing shape, used by
nine call sites. The reference implementation is `_sigv4_request()` at
`modules/agent-factory/agent-worker-image/adp_cred/client.py:88-148`: build a
`botocore.awsrequest.AWSRequest`, sign with `SigV4Auth(credentials, "execute-api",
region)` (`:131`), convert the signed headers onto a plain `urllib` request (`:135-137`).
Two companions matter as much as the signer:

- `gateway_signing_region(url)` (`adp_trigger/transport_identity.py:38-46`) derives the
  signing region from the `execute-api` hostname rather than from ambient `AWS_REGION`,
  so a caller in one region signs correctly for a gateway in another.
- `transport_identity.py:9-16` installs a log filter that drops `botocore.auth` DEBUG
  records, because **botocore's canonical-request debug output contains the signed
  headers**. Any signing code in the CLI inherits this obligation or it acquires a
  credential-disclosure path through `--verbose`.

### 2.7 Three disjoint machine-identity sources, and the canonical principal above them

This is the most consequential fact for the machine path, and it is not in any issue.

**What Rev-2 got wrong.** Rev-2 described two registers and concluded the server must
join a role ARN to a canonical Postgres `service_accounts.id`. Both halves are wrong.
There are **three** sources of machine identity, not two; and `service_accounts.id` is
**not** the canonical owner of a preference. The identity ruling on #5417 is explicit:
an opaque, immutable ADP `canonical_service_principal_id` owns the preference, and
"raw `service_accounts.id`, `agent_name`, `client_id`, ARN or caller-supplied text
never owns a preference." One canonical principal may carry several aliases.

The three alias sources a machine caller can arrive under, all verified:

| Alias source | Store and key | How a caller arrives under it | Evidence |
|---|---|---|---|
| Agent registry | DynamoDB, `by-role-arn` GSI, keyed `role_arn` | The **live** SigV4 path; `TokenContext.user_id` becomes `entry["agent_name"]` | `agent_registry.py:255` (`user_id=entry["agent_name"]`), `:260`, `:263`; `middleware.py:403-415` |
| `service_accounts` | Postgres, `id` PK, `iam_role_arn` unique | #5419's reuse table names this as its machine-principal reference | `src/shared/models/organization.py:209-218` |
| **Cognito M2M `client_id`** | Cognito app client; org-level approved-client list `Organization.cognito_client_ids` | A `client_credentials` token; `TokenContext.user_id` becomes `claims.client_id` when `account_type == "service"` and no username is present | `middleware.py:677-680`, `auth_service.py:309-312`; `organization.py:36` |

Rev-2 omitted the third entirely. It matters because a Cognito M2M caller is a machine
principal that reaches the **human** plane with a bearer token — so it is not a case the
signed transport covers, and a design that equates "machine" with "SigV4" would mis-file
it. Note the ruling's limit: `cognito_client_ids` is an **org-level approved-client
list, not a service-principal identity**; a client must be registered and tenant-bound
before it can reach service-self preferences at all.

These sources are disjoint and nothing joins them. `find_service_account_by_role_arn()`
exists (`src/auth/service_account_service.py:324-352`) but its **only** production caller
is `tenant_resolver.py:147-149`, reached exclusively through the deprecated
`/auth/exchange` flow that returns 410 by default. Verified: no code path maps a live
SigV4 caller ARN, or a Cognito `client_id`, to any shared canonical identifier.

**The consequence for this story.** A signed request arrives carrying a DynamoDB
`agent_name`; a Cognito M2M request arrives carrying a `client_id`. Neither is a
canonical principal, and the two can denote the *same* machine. If a mapping were
written under whichever label happened to arrive, the same service principal would
own two unrelated preference sets and a human administering it from the UI would see
neither — the #4744 identifier-mismatch class (comparing a Cognito `sub` against
`users.id` makes every row silently never match), which #5419 already warns about for
humans. Alias keys are also **tenant-scoped and source-qualified** —
`(org_id, alias_source, alias_id)` — so an alias name must never be looked up globally.

So the enabling work is canonical resolution in authentication: the server maps the
authenticated alias, qualified by its source and tenant, to the canonical principal
that owns the preference. **PMM-02 (#5419) owns that slice** — the canonical ID, the
alias registry, resolution in authentication, and the manageable-principals endpoint.
It is not the CLI's, and §9.3 records the consumption relationship. The CLI's obligation
is the negative one: it never supplies, guesses or defaults any principal identifier on
the signed path (§5.5.4).

One re-registration consequence the developer should not be surprised by: re-registering
a machine creates a **new** canonical principal by default, so its previous preferences
do not follow it. Re-linking to an existing principal is an explicit authorized, audited
operation. That is #5419's rule, not something the CLI can smooth over.

One further constraint the developer must not discover late: the registry's `scope`
field is constrained to `^(shared|personal)$` on both the create and update admin
schemas (`src/admin/agent_registry_schemas.py:98`, `:191`), so `internal` and
`platform` are written only by Terraform seeds
(`src/internal/auth_deps.py:29-53`). An ordinary tenant service principal is therefore
`shared` or `personal` and **cannot** reach `/internal/*`, whose scope allowlist is
`frozenset({"internal", "platform"})` (`auth_deps.py:54`). §5.5.1 selects the plane
accordingly.

---

## 3. Corrections to the issue's stated starting point

Three of the issue's design assumptions do not hold on the current default branch.
Each changes what the developer must build.

### 3.1 `test_io_contract.py` is not a shared contract test — it is superplane's

The issue lists `modules/gateway/tests/cli/test_io_contract.py` as "IO contract tests …
Extend", and AC-09 requires extending it.

It cannot be extended as written. The file binds itself to one helper at module scope:

```python
SCRIPT = Path(__file__).parents[2] / "cli/adp-superplane.py"
spec = importlib.util.spec_from_file_location("adp_superplane_io", SCRIPT)
```
(`test_io_contract.py:32-35`)

Every test then runs `adp-superplane.py` as a subprocess with superplane verbs
(`workspace use`, `provider add`). There is no parametrization over helpers and no
shared fixture. Extending the file means either rewriting it to be helper-generic — a
change to another story's test file, with its own review surface — or adding a sibling
`tests/cli/test_models_io_contract.py` that asserts the same four properties for this
helper.

**Recommendation:** add the sibling file. Do not rewrite superplane's. Generalising a
passing contract test for four helpers is worthwhile work, but it is a shared-file
change belonging to whoever owns the CLI foundation (#5185), not a side effect of this
story. The design note records the coupling so it is a deliberate choice.

### 3.2 The secret-argv guard does not live in the shared module, and this surface has no secret to guard

AC-09 requires "a secret-valued argument is refused, mirroring the existing
`--api-key VALUE` refusal". Two problems.

First, that guard is **not shared**. `SECRET_FLAGS` and `reject_secret_arguments` are
defined inside `cli/adp-superplane.py` (lines 72 and 153-170). No other helper imports
them — verified: `adp-aws.py`, `adp-github.py`, `adp-bedrock.py`, `adp-admin.py` and
`adp-github-admin.py` contain no reference. Mirroring it means copying it (a second
divergent list, the exact failure mode §5.2 exists to prevent) or promoting it to
`adp_common.py` (a shared-file change, §7.2).

Second, and more important: **`adp models` accepts no secret-valued argument at all.**
Its inputs are a persona key, a model alias, and a canonical service-principal identifier. None is
a credential. A test that passes `--api-key` to a parser that never defined it proves
only that argparse rejects unknown flags.

**Recommendation.** Keep the AC but change what it asserts, and say so in the PR:

- The helper's parser defines **no** flag whose value is a credential — assert this by
  inspecting the parser's actions, so a future flag addition trips the test.
- The bearer token never appears in stdout, stderr, or any request path or query
  string. This is the real disclosure risk on this surface and it is currently
  untested for any helper.
- Promoting `reject_secret_arguments` to `adp_common.py` is **out of scope** for this
  story unless #5185 wants it; if it is promoted, this helper calls it rather than
  copying it.

**The machine path does not change this, and that is the point.** §5.5 adds a signed
transport, not a credential-valued argument: the workload's own provider chain supplies
credentials, so there is still no flag, env var or file from which the helper reads key
material (§5.5.3). The signed path's equivalent obligations are folded into AC-09 (§8):
no credential material in argv, output or request body, and the `botocore.auth` DEBUG
log filter installed (§5.5.2) so a canonical-request log cannot leak signed headers. If a
future change *did* introduce a credential-valued flag, the parser-inspection assertion
above is what trips.

### 3.3 The conflict revision cannot reach the user through the shared transport

AC-06 requires that a stale-revision set "reports the conflict **and the current
revision**". The shared transport deliberately prevents that.

`Api.request`'s `HTTPError` arm (`adp_common.py:96-113`) extracts only a short
`[a-z_]{1,80}` reason code from the body and discards everything else, then raises a
fixed message. The comment and the §3.3 contract in `docs/design-notes/5180-cli-command-contract.md`
are explicit: "No response body is echoed verbatim — bodies can contain setup
parameters." So #5419's 409 body carrying the current revision is dropped before the
helper sees it. A 409 today produces only: `ADP returned HTTP 409 (…). Configuration
changed. Read its current status before retrying.`

Three ways to close this, and the design picks the third:

| Option | Consequence |
|---|---|
| Echo the 409 body | Reverses a deliberate security property of a shared file. Rejected. |
| Add a body-passthrough parameter to `Api.request` | Shared-file change; every helper inherits a new surface. Defer to #5185. |
| **Re-read after the 409 and report the revision from the read** | No shared-file change; matches `adp-bedrock.py:282-286`'s existing re-read idiom; the reported revision is freshly observed rather than parsed out of an error. **Chosen.** |

The re-read is a `GET` of the same mapping immediately after the 409. The helper then
reports: the conflict, the revision it sent, the revision now current, and that nothing
was written. The honest caveat, which the output must carry: between the 409 and the
re-read another change may have landed, so the reported revision is "current as of the
re-read", not "the revision that rejected you".

**This is now settled, not proposed.** The review locked it: a 409 may be followed by a
safe GET and reported as "current as of the re-read", and the response body is never
echoed. Recorded at §9.5. The wording is load-bearing — "current as of the re-read"
tells a script that the value is a fresh observation it may retry against, whereas "the
current revision" would imply a causal link to the refusal that the design cannot
supply.

Two properties of the re-read that follow from it being a plain GET: it must be a
**read of the same principal and persona** the write targeted (never a broadened list),
and a failure of the re-read must not mask the conflict — if the GET also fails, the
helper still reports the 409 and says the current revision could not be determined.

Note also that #5419 uses **two** refusal shapes — 422 `{reason, message}` for
validation refusals and 409 for concurrency (#5419's reuse table, and
`bedrock_routing/self_routes.py:108-115` for the 422 precedent). The helper must handle
both; `adp_common.py:101` already surfaces the `reason` code from a 422 body, so
validation refusals map cleanly and only the 409 needs the re-read.

---

## 4. Command surface and contracts

### 4.1 Commands

```
adp models catalog --persona KEY [--json]
adp models mappings list [--json]
adp models mappings set   --persona KEY --model ALIAS [--dry-run] [--yes] [--json]
adp models mappings reset --persona KEY [--yes] [--json]
adp models explain --persona KEY [--json]
adp models service-principals list [--json]
```

`--persona` on `catalog` is **required, not optional** — see §5.1 for why, and note that
Rev-2 listed an unfiltered `catalog` form on the line above it, which contradicted the
same note's D6 reasoning. The unfiltered form is **withdrawn**. There is no way to ask
this CLI for a catalogue that is not scoped to a persona's compatibility class.

Authorized human administration of a service principal adds `--service-principal ID` to
`mappings list`, `mappings set`, `mappings reset` and `explain`. The identifier it takes
is the **opaque canonical `canonical_service_principal_id`** (§2.7) — never a role ARN,
a `service_accounts.id`, a Cognito `client_id` or an `agent_name`. Rev-2 named this flag
`--service-account` and described its value as `service_accounts.id`; both are corrected,
because the identity ruling is that raw alias identifiers never own a preference.

`adp models service-principals list` is the discovery command. It returns the service
principals the **server** says this human may administer, with their canonical
identifiers, and it maps onto the ruled endpoint
`GET /me/persona-models/manageable-service-principals`. It exists because the canonical
ID is opaque and immutable by design: nothing about a machine's role ARN or name lets an
operator derive it, so without a listing the CLI path would require an identifier
obtainable only from the UI. The command is a read, and it lists nothing the caller is
not entitled to manage, so it is not an enumeration oracle over the tenant's machine
principals. Administered handlers accept **only** canonical IDs originating from this
discovery surface — that is the ruling, and it is why the flag has no other accepted
value form.

**The machine path adds no verbs.** A service principal running `adp models mappings list`
runs the same command a person does; §5.5 changes only which credential is attached and
which external URL is used. `service-principals list` and every `--service-principal`
form are human-administration commands and are **not** available on the signed path
(§5.5.4).

**No self command defines any principal argument.** Not `--user`, not `--user-id`, not
`--principal`, not a positional. The caller is resolved server-side from the credential
by the ruled self routes, which take no target at any position. Because `Parser`
raises `CliError(…, "usage_error", 1)` on an unknown argument (`adp_common.py:30-32`),
`adp models mappings set --user someone-else --persona architect --model opus5` exits 1
having sent no request. AC-03's refusal is therefore structural: it follows from the
argument not existing, not from a check that could be removed.

`--service-principal` is a *different* command shape on purpose, matching the ruling's
split between self routes and the target-taking
`/service-principals/{canonical_id}/persona-models` routes (human org-admin only,
tenant-checked). Entitlement is decided server-side; the helper performs **no**
client-side entitlement check, because a client-side check is either redundant or wrong
and teaches readers to trust it.

**Endpoint mapping.** Each command maps onto exactly one ruled surface, so a developer
never has to invent a path:

| Command | Endpoint (human path; signed path prefixes `/agent`, §5.5.1) |
|---|---|
| `mappings list` | `GET /me/persona-models` |
| `catalog --persona KEY` | `GET /me/persona-models/catalog?persona_key=KEY` |
| `explain --persona KEY` | `GET /me/persona-models/explain/KEY` |
| `mappings set --persona KEY` | `PUT /me/persona-models/KEY` |
| `mappings reset --persona KEY` | `DELETE /me/persona-models/KEY` |
| `service-principals list` | `GET /me/persona-models/manageable-service-principals` |
| any `--service-principal ID` form | `GET\|PUT\|DELETE /service-principals/ID/persona-models[/KEY]` |

**There is no `--org` / `--tenant` flag.** Mappings are tenant-scoped and D1 resolves
them against the caller's active tenant. `adp_common.py:192-193` does provide an
`organization_context()` helper, but using it here would let a caller aim a write at a
tenant other than their active one — a scope-selection surface D1 deliberately did not
create. Instead, see §4.3: the output **names** the tenant it acted in, because a
person who belongs to several organizations otherwise cannot tell which workspace they
just changed. Workspace selection persists on the Cognito login and other sessions
adopt it only at refresh (`docs/design-notes/org-workspace-switching.md`), so "which
tenant am I in right now" is genuinely not obvious from the terminal.

### 4.2 Deliberate exclusions

- **No one-run override command.** D2 makes an explicit `/model` request a direct-hop-
  only, non-persisting, audited override. Exposing it under `adp models` would imply it
  is a saved preference. If a scriptable one-run override is wanted later, it belongs
  with the invocation command, not the mapping command.
- **No client-side precedence.** `explain` renders the server's answer. If the response
  lacks a source field the helper reports that the server did not supply one; it never
  computes "personal mapping beats default" locally. A CLI that computes precedence is
  the third source of truth this epic exists to remove.
- **No local alias list.** Aliases come from `catalog` at runtime (§5.2).
- **No catalogue caching to disk.** `state_path`-backed state is "a hint, not proof"
  (5180 contract §3.6). A cached catalogue would let the CLI certify a selection on
  evidence the server has since marked stale, which D3 forbids.

### 4.3 Output

`--json` emits exactly one object from `envelope()` on stdout; all narration goes to
stderr. `detail` must carry, per command:

| Command | `detail` carries |
|---|---|
| `catalog` | tenant; the persona and its compatibility class; for each model: canonical ID, family and version, selectable, refusal reason where not, permitted, invocable (tri-state), the nested evidence record, retired flag, compatibility class, harness contract revision, price context — **and the alias where the server supplies one** (§9.7 item 1: PMM-03's row carries no alias field at its current head; the helper prints what is supplied and never derives one) |
| `mappings list` | tenant; one row per persona with saved value (or none), effective value, source, revision, **the persona's compatibility class**, and where the effective value is a class default, its **candidate/proven status** |
| `mappings set` | tenant; persona; requested alias; canonical ID; resulting revision; whether anything changed |
| `mappings reset` | tenant; persona; the now-effective class default, **the class it is the default for**, its candidate/proven status, and its source; whether anything changed |
| `explain` | tenant; persona; effective model; compatibility class; source, from the server; and on a refusal, **the server's refusal reason rendered verbatim as the server worded it** |
| `service-principals list` | tenant; one row per manageable principal: canonical ID and display name, as returned |

`detail` carries no token and no credential. The tenant identifier appears in every
`detail` and in the first line of human output for a mutation. On the signed path the
first line also names the **canonical principal the server resolved** (§5.5.3).

Where a mutation was made for a service principal — whether by a human administrator or
by the principal itself — the output names that canonical principal, not the human who
authorized it. Audit attribution of the acting human is the server's record, not a
substitute for saying whose policy changed.

Human output for `mappings list` must show a persona with no saved row as showing the
default **and say so** — but it must not call it "the platform default", because there is
no such thing. The default is keyed by compatibility class, so the line names the class:
"no saved selection; using the `claude-agent-sdk` class default". "Blank" reads as broken
and a bare "Sonnet 4.6" reads as a deliberate choice, but "the platform default" reads as
a single global value and is the specific wording the class-keyed default forbids.

**Two obligations the canonical note's Rev-4 §6.8 places on this surface**, both listed
above and called out here because Rev-4 records that this story was not re-read at head
when that ruling was written:

1. **A displayed default must name the class it belongs to**, never one platform-wide
   value. The failure it prevents is a user reading "the default is Sonnet 4.6", mapping a
   `codex-sdk` persona on that belief, and getting a refusal the CLI had already shown
   them the information to predict — the same predictable-refusal class §5.1 removes from
   `catalog`. Rev-2's wording here said "the platform default" and was wrong in exactly
   the way the ruling names.
2. **`explain` surfaces D2 refusals verbatim** — the server's refusal wording, not a
   CLI paraphrase. This is a narrow, deliberate exception in presentation only, and it
   does **not** loosen §9.5: what is rendered verbatim is the refusal *reason the server
   states for a resolution outcome*, reached through the normal success-shaped `explain`
   response. It is never an HTTP error body, and `Api.request`'s rule that no response
   body is echoed verbatim (`adp_common.py:96-113`) stands untouched. A developer who
   reads obligation 2 as licence to echo a 409 or 422 body has inverted it; §9.5 is the
   controlling rule on error paths.

### 4.4 Exit codes

Reusing the shared contract (5180 §3.4) with no additions:

| Situation | Code | Mechanism |
|---|---|---|
| Success; or a `--dry-run` / read that completed | 0 | `emit()` on an `ok`/`configured`/`verified` status |
| Unknown flag, missing argument, principal argument on a self command | 1 | `Parser.error` → `CliError(…, 1)` |
| Not signed in, session expired | 2 | `access_token()` failure, or 401 → `adp_common.py:113` |
| Authenticated but not entitled to the target service principal | 3 | 403 → `adp_common.py:113` |
| **Blocked by an unresolved external state: evidence stale or unrefreshed, probing disabled, a stale-marked write response (§5.3)** | **4** | `pending`/`unavailable` envelope (`adp_common.py:211`) or an explicit `CliError(…, 4)` (`adp-admin.py:155`), selected on the server's reason code — **not** the transport default |
| Refused and waiting will not help: unknown alias, model not permitted, incompatible compatibility class, retired model, conflict/stale revision | 5 | `CliError` default |
| Ctrl-C | 130 | `KeyboardInterrupt` → `report_error(CliError(…, "interrupted", 130))` |

**On the signed path (§5.5), codes 0, 1, 3, 4, 5 and 130 mean the same things.** Two
differences, both deliberate: code 2 is unreachable because there is no interactive
session to expire, and every machine-path setup failure — `botocore` unavailable, no
credentials resolvable, or credentials that are static rather than refreshable — exits
**5** with an actionable message rather than falling back to the bearer path. The full
table is §5.5.3; a silent fallback would make a workload act as whatever human happened
to be logged in on that machine.

**The exit-4 rule is not a transport default and must be implemented.** The shared
transport maps every non-401/403 status to 5 (`adp_common.py:113`), so exit 4 for a
stale-evidence refusal is a deliberate remap on the server's reason code, not something
the helper gets for free. §5.3 has the mechanism and the code list. The consequence of
skipping it is not cosmetic: in the posture PMM-03 actually ships (§9.7) *every* save is
refused for want of evidence, so an unimplemented remap turns the whole surface's normal
day-one state into exit 5 — "your request is wrong" — for a condition that resolves by
itself when PMM-09 enables probing.

**Idempotency (AC-07).** A second identical `set` must exit 0 and report
`changed: false`. This is only achievable if #5419's set endpoint is genuinely
idempotent — it says it is. If it instead returns 409 on an unchanged re-send, the
helper must not paper over it by treating 409 as success; it reports the conflict and
the story records the endpoint as not meeting its own contract. Same for `reset` on an
already-absent row.

Cancellation honesty: a Ctrl-C during `set` must say that the write may already have
been accepted by the gateway and name `adp models mappings list` as the way to check.
Nothing here holds a billable external resource, so this helper's message is narrower
than superplane's `CANCELLED` (`adp-superplane.py`, asserted by
`test_io_contract.py:176-200`).

### 4.5 `--dry-run`

`--dry-run` on `set` reports the requested alias, the canonical model it resolves to,
the effective destination, and the expected source — then exits 0 having written
nothing. Resolution and validation are **server-side**: the helper submits the alias to
#5420's validation read, which returns the canonical identifier without saving.

**No command in this surface can cause a model invocation, and that is now a ruled
property rather than this note's assumption.** The probe-safety ruling is that PMM-03
ships probing **disabled with a zero spend budget**, no page load invokes a model, and
probes may be enabled only in PMM-09 after a named target account and spend ceiling are
approved. So AC-08's cost bound holds for a stronger reason than Rev-2 gave: it is not
merely that probes are "cadenced server-side", it is that the probe machinery is off by
default and cannot be switched on by any client request. The corollary the helper must
respect: pricing, listing or agreement status **never counts as invocability proof**
(the #2300 lesson, §2.5), so `catalog` renders the server's invocability evidence and
never infers invocability from the presence of a price row.

`--yes` suppresses interactive confirmation only. A non-interactive `set` without
`--yes` refuses with exit 1 and points at `--dry-run`, matching
`adp-bedrock.py:143-144`.

---

## 5. Constraints that follow from the locked decisions

### 5.1 D6 — harness compatibility makes `catalog` persona-aware

D6: a model is selectable for a persona only where that persona's harness has a
registered, validated compatibility contract for it, and "the UI and CLI must
list/filter models by the selected persona's harness compatibility".

The epic's proposed `adp models catalog` takes no persona, so it cannot honour that. The
surface in §4.1 therefore **requires** `--persona`:

- `adp models catalog --persona architect` — the models selectable for that persona,
  each row carrying its compatibility-class marker and its invocability evidence.
- There is **no unfiltered form.** Rev-2 kept one, on the reasoning that "a person
  exploring their options wants the whole list, with the incompatible entries visibly
  marked rather than hidden." That contradicted the same section's D6 argument and is
  withdrawn.

Why the unfiltered form had to go rather than being kept as a convenience. The failure it
creates is precisely the one D6 exists to prevent: a script reads the catalogue, picks a
model that is in it, calls `set`, and is refused — a refusal the CLI had the information
to predict. A "visibly marked" incompatible row is exactly the kind of marker a script
does not read and a hurried human skims past, and the ruling is blunt that there is **no
cross-class fallback**, so an incompatible pick has no graceful degradation to fall back
on. Requiring the persona costs an exploring human one word and removes the whole class.

Two consequences from the compatibility ruling that the developer must not paper over:

- Compatibility is keyed by **compatibility class** (`claude-agent-sdk`, `codex-sdk`) —
  stable, unversioned IDs owned by PMM-03 (#5420), which now defines the persona→class
  registry and puts `compatibility_class` on every persona row at its current head
  (`docs/design-notes/5420-persona-and-model-catalogue.md` §2.4, §6.1 at #5434 `25ece717`).
  The harness contract revision is a **separate versioned field**. The helper renders both
  and derives neither; it holds no class table, exactly as it holds no alias table (§5.2).
- `us.anthropic.claude-sonnet-4-6` is a Claude-class **candidate, not an active proven
  default**, until PMM-09 records a bounded real invocation. So `mappings list` must not
  present the class default as proven when the catalogue marks it a candidate — the
  D3 staleness discipline in §5.3 is the same mechanism and covers it. §4.3 puts both the
  class name and the candidate/proven status in `detail`, and AC-05e pins the absence of
  the phrase "the platform default" in the helper, because that phrase is the compact form
  of the global-default error the class key exists to prevent.

### 5.2 Aliases come from the catalogue, never from the helper

The two existing alias maps drifted precisely because each was hand-maintained
(`model_resolver.py` still resolves `claude-sonnet-4` to an identifier
`model_validate.py:34-36` records as non-invocable). A third hand-maintained list in the
CLI would drift the same way, and it would drift *on users' laptops*, where it is
updated only when someone runs `adp update`.

So: `--model` accepts any string, submits it, and reports the server's answer. An
unknown alias is refused with the server's reason and a next action naming
`adp models catalog --persona KEY`. **No alias constant, no alias dict, and no alias
regex may appear in `adp-models.py`** — this is a reviewable property, and the PR should
assert it in a test rather than claim it in prose.

Concretely for the epic's example: `--model fable51` must be refused actionably today,
because no such alias exists (§2.5). Documentation and help text for this command must
use an alias verified present — `opus5` (`model_validate.py:26`) or a Sonnet 4.6 alias.
Shipping help text that recommends `fable51` would teach every user a command that
fails. If #5420's probe later establishes Fable 5.1 as invocable and an alias is added,
this helper needs no change — which is the point.

### 5.3 D3 — stale evidence must not be laundered into a confident success

D3: "Empty, stale or contradictory effective policy fails actionably." #5420 returns
entries marked stale when it cannot refresh.

The helper's obligations:

- `catalog` and `mappings list` on stale evidence **succeed** (exit 0) but carry the
  stale marker and its age in both output forms. These are reads; refusing them would
  leave an operator unable to see their own configuration during a catalogue outage.
- `set` against a model whose evidence is stale **must not report success — and a stale
  write response is a protocol violation, not a reportable outcome.** This is the Rev-3
  correction the third-pass review required, and it is a behaviour change, not a wording
  one. Rev-3 said that if the server accepts a write on stale evidence the helper
  "reports the acceptance *and* the staleness". That is wrong: it makes the CLI the place
  where a D3 violation gets laundered into an exit 0 with a caveat attached, and a script
  branching on the exit code — which is what scripts do — would read it as a clean save.
  The rule is now: **a write response that is marked stale is never rendered as success.**
  The helper reports it as a refusal with the resumable exit code (below), says that the
  gateway returned a stale-marked write response, and states that the saved state is
  unknown and must be read back with `adp models mappings list`. The developer files the
  server behaviour as a defect against #5419 in the PR. The helper does not decide whether
  the write landed, because it cannot — it declines to certify it either way.

**Every stale-evidence refusal is exit 4, from wherever it arrives.** This is the second
half of the same ruling, and it exists because Rev-3's §4.4 table could be read as
sorting the same condition into two codes: a stale-evidence refusal arriving as a 422
would fall into the "refused" row (exit 5), while one the helper detected from a stale
marker would be exit 4. A script cannot distinguish "wait for the catalogue to refresh"
from "your request is wrong" if the same cause produces two codes depending on which
layer noticed it. So:

- Any refusal whose reason is stale or unrefreshed evidence is exit **4**, whether it
  arrives as a 422 with a staleness reason code, as a stale marker on an otherwise
  successful-looking response, or as the stale-write violation above.
- Exit 5 remains for refusals that will not resolve by waiting: unknown alias, model not
  permitted, incompatible compatibility class, retired model, conflict.

**The mechanism, since the shared transport does not do this for free.** `Api.request`
maps every non-401/403 HTTP status to exit 5 (`adp_common.py:113`), so a 422 carrying a
staleness reason arrives as a `CliError` with `exit_code=5`. The helper must catch it and
re-raise or re-envelope on the reason code. Both halves of the mechanism already exist and
neither needs a shared-file change:

- `Api.request` surfaces the server's reason code on the `CliError` when the body's
  `reason` matches `[a-z_]{1,80}` (`adp_common.py:99-103`), and PMM-03's vocabulary codes
  — `evidence_stale`, `probing_disabled` — both match, verified.
- Branching on `exc.code` to change the outcome is an established helper idiom
  (`adp-superplane.py:423`, `vault = "already_absent" if exc.code == "not_found" else None`),
  and exit 4 is reachable either by an envelope whose status is `pending`/`unavailable`
  (`adp_common.py:211`) or by a `CliError` with an explicit `exit_code=4`
  (`adp-admin.py:155` does exactly this).

The reason-code set the helper maps to exit 4 must come from #5420's published refusal
vocabulary (§9.7), not from a list invented here — a locally-invented list is the same
drift failure as a local alias table (§5.2).

### 5.4 D5 — snapshots are not this surface's concern

D5's signed chain snapshot is created gateway-side at invocation. This helper reads and
writes saved policy; it never constructs, signs, verifies or displays a snapshot, and
it must not offer a command implying it can. `explain` answers "what will be chosen for
this persona", not "what did run X use" — chain forensics belong to PMM-06/PMM-08.

### 5.5 The machine (M2M) transport

A registered service principal must be able to manage its own persona mappings from the
CLI, unattended. This section specifies how, within the constraints §2.6 and §2.7
establish. The design principle throughout: **add a transport, not a credential store,
and not a second set of semantics.**

#### 5.5.1 Which plane the signed request reaches

A signature is only validated on the routes configured for it. Of the three planes:

- `/{proxy+}` — `auth = NONE` (`main.tf:220-222`). A SigV4 signature sent here is
  **ignored**; the app then finds no bearer token and 401s. Signing against the human
  plane silently does nothing.
- `/internal/{proxy+}` — `AWS_IAM`, but gated to `scope ∈ {internal, platform}`
  (`auth_deps.py:54`), which §2.7 shows an ordinary tenant service principal cannot hold.
  **Not available**, and deliberately so: that allowlist exists precisely to stop a role
  registered for some other purpose from reaching the internal plane (`auth_deps.py:29-53`).
- `/agent/{proxy+}` — `AWS_IAM`, injects `X-Caller-Identity` from
  `context.identity.userArn` (`main.tf:270-298`), resolves through the agent registry,
  and admits any **registered** role regardless of scope. **This is the plane.**

**No new backend route is needed, and adding one would be a defect.** Rev-2 asserted
that #5419's self endpoints "must be reachable under the `/agent/` prefix as well as the
human prefix" and filed that as prerequisite P-1. That was wrong, and P-1 is withdrawn.
The `/agent/{proxy+}` integration URI is `http://${internal_alb_dns}/{proxy}`
(`main.tf:285`) — `{proxy}` is the path *after* `/agent`, so the prefix is **stripped**
before the request reaches the pod. The repository states this in terms: "The Bedrock
`/agent` proxy strips its prefix because the pod serves Bedrock requests at root paths"
(`main.tf:317-318`, in the contrasting comment explaining why `/internal/{proxy+}`
deliberately *preserves* its prefix at `main.tf:324`).

So a signed caller sending `PUT {invoke_url}/agent/me/persona-models/{persona_key}`
arrives at the pod as `PUT /me/persona-models/{persona_key}` — **the same single FastAPI
handler the human path reaches.** The API ruling on #5417 states this as the contract:
the self routes exist once at `/me/persona-models`; human JWT calls use that path,
service SigV4 calls use external `/agent/me/persona-models`, and the existing proxy
reaches the same handler — *do not duplicate the backend router.*

The developer obligation is therefore a CLI-side one, not a server-side one: when the
signed transport is selected, prepend `/agent` to the external path and nothing else.
The `/api` prefix the human path appends (`adp_common.py:39-50`) is an ALB/CloudFront
routing artifact and is **not** part of the `execute-api` path.

A second consequence the developer must handle: the CLI's stored `gateway_url` is the
CloudFront/ALB origin used by browsers, and `gateway_url()` appends `/api`
(`adp_common.py:39-50`). Signing requires the **`execute-api` invoke URL** — that is
what `gateway_signing_region()` parses a region out of
(`transport_identity.py:38-46`) and what the signature's host header must match. These
are different hostnames. The machine path therefore reads its endpoint from an explicit
environment variable, mirroring the existing convention `ADP_GATEWAY_ENDPOINT` used by
`lib/gateway_credential_client.py:95-99`, and **refuses rather than guesses** if it is
absent — consistent with `gateway_url()`'s existing refusal to guess an origin.

#### 5.5.2 How the request is signed, without breaking the dependency-free CLI

Every signer in the platform uses `botocore`. The CLI is deliberately stdlib-only
(`adp_common.py:2`, "stdlib only"), and `install.sh:31-34` is explicit that requiring
the AWS CLI was a defect it removed, because "ordinary gateway users hold no AWS
credentials, and the point of the gateway-routed refresh (#4846) is that they need
none."

Both facts survive if signing is **conditional and lazily imported**, which is exactly
what the reference implementation already does: `adp_cred/client.py:93-100` imports
`botocore` inside the signing function and exits with an actionable message if it is
absent. Applied here:

- A human user's code path imports nothing new. `botocore` is imported only when the
  machine path is selected. A person who never uses it never needs it installed, so
  `install.sh` keeps its no-AWS-CLI property and its requirements do not change.
- A service principal runs in a pod or CI job that already has `boto3`/`botocore` and
  already holds refreshable role credentials, so the dependency is satisfied where it is
  used.
- If the machine path is selected and `botocore` is missing, the helper refuses with
  exit 5 naming the missing dependency. It must **not** fall back to the bearer path:
  silently degrading a machine caller to "not signed in" would produce a confusing 2
  where the real fault is a missing library.

The signing shape is reused, not reinvented — `SigV4Auth(credentials, "execute-api",
gateway_signing_region(url))`, signed headers copied onto a `urllib` request
(`adp_cred/client.py:126-137`). Two properties must be carried over deliberately:

- **The `botocore.auth` DEBUG log filter** (`transport_identity.py:9-16`). Canonical-request
  debug output contains the signed headers. Without the filter, enabling debug logging
  becomes a disclosure path. This is a security-relevant line of code, not a nicety.
- **Redirects stay refused.** `Api.request` already uses `NoRedirect`
  (`adp_common.py:53-55`). A SigV4 signature is bound to host, path and headers; a
  followed redirect would forward a signature to a host it was not computed for. The
  signed path reuses the same opener.

**Which credentials may be signed with — the Rev-2 correction.** Rev-2 said credentials
come "from the workload's own provider chain" via `session.get_credentials()`, while
simultaneously claiming no static key pair was reachable. Those two statements
contradict each other, and the review was right to reject the pair. Verified directly:

```
$ AWS_ACCESS_KEY_ID=AKIAX AWS_SECRET_ACCESS_KEY=s python3 -c \
  "import botocore.session; c=botocore.session.get_session().get_credentials(); \
   print(type(c).__name__, c.method, repr(c.token))"
Credentials env None
```

`session.get_credentials()` returns a **plain, non-refreshable `Credentials` object with
`method == "env"` and no session token** — a long-lived IAM user key pair, exactly the
static credential the design claims is impossible. The same holds for a shared
credentials file (`method == "shared-credentials-file"`).

The rule, which is **mechanically checkable rather than a prose aspiration**: the
machine path signs only with **refreshable temporary credentials**, and refuses
otherwise. Concretely, before signing:

1. The credential object must be an instance of `botocore.credentials.RefreshableCredentials`
   (`DeferredRefreshableCredentials` is a subclass — verified — so IRSA/web-identity,
   assume-role and container/IMDS providers all satisfy it).
2. The frozen credentials must carry a non-empty session `token`. A temporary credential
   always has one; a static key pair never does.

If either check fails, the helper exits 5 and names the reason: this command signs only
with temporary role credentials, and a static access key is not accepted. It does **not**
fall back to the bearer token (§5.5.3), and it does not sign anyway with a warning.

The reason to hard-refuse rather than accept-and-warn: a static key pair in a CI
environment is a credential that outlives the job, cannot be revoked by ending the
session, and is the thing most likely to have been copied from a human's laptop. Signing
with it would let a workload act as whatever IAM user someone pasted into the
environment, and the gateway would faithfully resolve that identity. The narrower rule
costs a correctly-configured workload nothing, because IRSA and CI OIDC both yield
refreshable credentials.

The existing platform precedent is stronger than Rev-2 described and should be followed
where available: `worker_credentials()` (`transport_identity.py:47-79`) constructs an
`AssumeRoleWithWebIdentityProvider` **explicitly**, with `disable_env_vars=True`, so
ambient `AWS_*` variables cannot displace the platform identity. It falls back to
`session.get_credentials()` only outside the worker authority mode. This helper does not
run in a pod with that preserved-identity contract, so it cannot reuse the function
directly — but it must reuse its posture: prefer an explicitly configured web-identity
provider, and never treat ambient environment keys as an acceptable signer.

The engineering shape that keeps this from becoming a second transport: `Api.request`
gains **no** new behaviour, and this story adds nothing to `adp_common.py` (§7.2). The
signing lives in `adp-models.py`, which selects a credential mode and then makes the
same request either way. If a later story wants M2M for other helpers, promoting the
signer to `adp_common.py` is #5185's call — and the review's instruction not to "revive
a deprecated whole helper" is satisfied either way: nothing in `bg-auth.sh` is reused,
because it contains no signing code at all (§2.6).

#### 5.5.3 How the machine caller is selected

Not by a flag that says "act as a machine". The mode is derived from the environment
the command is actually running in:

- If the machine endpoint variable is set, the machine path is used.
- Otherwise the bearer path is used.

The reason this is not a flag: a flag is something a *human's* shell history and CI
config can carry into the wrong context, and it invites `--as-service-account`, which
is the impersonation surface §1 exists to close. Deriving the mode from the endpoint
variable means the caller cannot assert a principal kind at all — it presents whatever
credential its execution context holds, and the **server** decides what that is. A
human with no role credentials who sets the variable gets a signature failure or a 403
`agent_not_registered`, never someone else's mappings.

**Selecting the machine path is a commitment, not a preference.** Once the endpoint
variable is set, there is **no fallback of any kind**:

| Failure after the machine path is selected | Result |
|---|---|
| `botocore` not importable | exit 5, names the missing dependency |
| No credentials resolvable | exit 5, names that no role credentials were found |
| Credentials are static (not refreshable, or no session token) — §5.5.2 | exit 5, names that a static access key is not accepted |
| Signature rejected at the edge | the edge's own 403, surfaced as exit 3 |
| Role not registered | 403 `agent_not_registered` → exit 3 |

In none of these does the helper retry with the bearer token, and in none does it
proceed unsigned. A fallback would mean a CI job that silently acted as whichever human
last logged in on that runner — writing that person's model policy instead of the
workload's, under that person's spend attribution. That is the failure this table
exists to prevent, and it is why every row is a terminal refusal.

`adp models` prints which mode and which principal it is acting as on the first line of
any mutation (§4.3), so an operator debugging a CI job can see that it acted as a
machine rather than as them. The principal it prints is the **canonical identifier the
server returned** (§2.7), never a label the CLI inferred from its own credentials.

#### 5.5.4 The impersonation boundary on the signed path

Three prohibitions, each a reviewable property of the code:

1. **No principal argument, same as the human self path.** `--user`, `--user-id`,
   `--principal`, `--as`, and any positional are undefined. `Parser` exits 1 on an
   unknown argument (`adp_common.py:30-32`) before a request is sent.
2. **`--service-principal ID` is not accepted on the signed path.** It is a human
   administration flag. If present while the machine path is selected, the helper
   refuses with exit 1 and says that a machine principal manages only its own mappings.
   This is the one place where the helper performs a *client-side* refusal that the
   server would also make, and it is justified: the flag's presence indicates the
   operator has misunderstood the surface, and the actionable message is the useful
   output. It is a usability refusal, not a security control — §6 records that the
   server remains the authority.
3. **No caller-supplied identity header.** The helper sends no `X-Caller-Identity`,
   no `X-Agent-OrgId`, and no other identity assertion. `X-Caller-Identity` is injected
   by API Gateway from the validated signature (`main.tf:293`); a client-supplied copy
   is exactly the forged-identity class that `middleware.py:286-312` records as removed
   in #3985, whose warning reads "Do NOT reintroduce a header that is trusted purely on
   presence." `X-Agent-OrgId` is honoured only for `scope == "internal"`
   (`middleware.py:434-438`), which this caller cannot hold — but the helper must not
   send it regardless.

#### 5.5.5 What is provable before review and what is not

The signature itself is testable offline: given fixed credentials and a fixed clock, a
`SigV4Auth`-signed request has a deterministic `Authorization` header, and the existing
harness at `modules/gateway/tests/e2e/conftest.py:291-334` (`_build_sigv4_headers`,
`IAMSignedClient`) is the precedent. What tests at this layer **cannot** prove:

- that API Gateway accepts the signature (edge behaviour, no local equivalent);
- that the role is registered and resolves to the right canonical service principal
  (needs #5419's canonical identity slice, §9.3, plus a seeded registry row);
- that the machine's mappings are the ones written (a database assertion, #5419's).

All three are live acceptance, and they belong to PMM-09 alongside the human path
(§8). The completion report must say so rather than reporting a passing signing unit
test as machine-path acceptance.

#### 5.5.6 Security review status

The review's instruction was a "security-reviewed M2M transport". This section is the
design input to that review, not its conclusion. What it deliberately does **not** do,
each of which was an available and worse option:

| Rejected option | Why |
|---|---|
| Revive `bg-auth.sh` / `/auth/exchange` | Transmits raw access key, secret and session token in a JSON body (`bg-auth.sh:288-296`); the route is disabled by default and its own text calls it a credential exposure risk (`src/auth/routes.py:115-125`) |
| Accept a reusable provider secret or API key for machines | Creates a secret at rest with rotation and revocation obligations the platform would then own. SigV4 over an existing role has neither |
| Let the caller name its own principal ID as "self" | §5.5.4 |
| Reach `/internal/*` by seeding the CLI's service principals as `internal` scope | Defeats `auth_deps.py:29-53`'s control platform-wide to serve one command area |
| Put signing in `adp_common.py` now | Every helper inherits a transport none of them needs; #5185's call (§7.2) |

Residual items a security reviewer should be pointed at: the `botocore.auth` log filter
(§5.5.2) is the one place where omitting a line creates a disclosure path; and the
`/agent/` prefix requirement (§5.5.1) widens what that plane serves from agent
inference traffic to tenant configuration writes, which is a change in that route's
blast radius and is #5419's to accept, not the CLI's to assume.

---

## 6. Security and tenancy boundaries

| Boundary | How it is held |
|---|---|
| Caller identity (human) | Server-side from the bearer token. The helper never sends a caller identifier. |
| Caller identity (machine) | Server-side from the SigV4 signature, via API-Gateway-injected `X-Caller-Identity` (`main.tf:293`) and the agent-registry lookup. The helper sends no identity header of its own (§5.5.4). |
| Self vs. target | Separate endpoints and separate command shapes. No self command has a principal argument (§4.1). `--service-principal` is refused on the signed path (§5.5.4). |
| Entitlement to a service principal | Server-side only; 403 → exit 3. No client-side check. The one client-side refusal (§5.5.4 item 2) is a usability refusal over a flag that does not apply, not an authorization decision. |
| Canonical machine identity | Resolved server-side: the authenticated alias, qualified by source and tenant, maps to the opaque canonical service-principal ID that owns the preference. The helper never supplies, guesses or defaults it, and never treats an `agent_name`, `client_id` or role ARN as canonical (§2.7, §9.3). |
| Tenant | The caller's active tenant, from the credential. No `--org` flag (§4.1). Named in output. |
| Credential handling | No credential stored, written or accepted as an argument. Humans: `access_token()` remains the only token path; no helper reads `tokens.json` (5180 §3.2 — two commands at once would race the refresh). Machines: credentials come from the ambient role via botocore's chain and are used to compute a signature, never transmitted (§5.5.2, contrast `bg-auth.sh:288-296`). |
| Secret disclosure | No credential-valued flag exists; token never printed, never in a path or query (§3.2). On the signed path the `botocore.auth` DEBUG filter (`transport_identity.py:9-16`) must be installed, or canonical-request logs disclose signed headers (§5.5.2). |
| Redirects | Refused, not followed — `NoRedirect`, `adp_common.py:53-55`. Load-bearing on the signed path: a signature is bound to host and path. |
| Gateway origin | From `~/.bedrock-gateway/config.json`, HTTPS or loopback HTTP, never guessed (`adp_common.py:39-50`). The signing endpoint comes from an explicit environment variable and is likewise never guessed (§5.5.1). |

One residual risk worth naming: the download route is **unauthenticated by design**
(`src/cli_download/routes.py:18-23`). `adp-models.py` becomes public content. It
contains no secret — every value it handles is prompted or read from the session store
at runtime — which is why the exact-set test at `tests/test_cli_download.py:86-107`
exists and why updating it is a deliberate act rather than a formality.

---

## 7. Deliverables, packaging and coordination

### 7.1 The complete edit list

The issue says "All four edits are required; three of them is a broken release." The
count is **eight files, nine edit sites**. Three are absent from the issue's list: the
second `CLI_FILES` copy inside `cli/adp`, the pinned exact-set test, and the #5180
command registry row locked at §9.3. Missing the first means `adp update` never places
the helper for existing users while a fresh install works. Missing the second is a red
test rather than a silent break — CI catches it — but a developer working from the
issue's list will be surprised by it. Missing the third recreates the two-registries
problem this epic exists to remove.

| # | File | Edit |
|---|---|---|
| 1 | `modules/gateway/cli/adp-models.py` | New helper; stdlib only on the human path, lazily-imported `botocore` on the signed path (§5.5.2); the verbatim §2.3 preamble from the 5180 contract |
| 2 | `modules/gateway/cli/adp` | `case` arm → `exec_python_helper "adp-models.py" "$@"`, **eight-space indent** (§2.2) |
| 3 | `modules/gateway/cli/adp` | `usage()` entry |
| 4 | `modules/gateway/cli/adp` | `CLI_FILES` (line 50) |
| 5 | `modules/gateway/cli/install.sh` | `CLI_FILES` (line 45) |
| 6 | `modules/gateway/src/cli_download/routes.py` | `ALLOWED_SCRIPTS` |
| 7 | `modules/gateway/src/cli_download/routes.py` | `SCRIPT_MEDIA_TYPES` → `PYTHON_SCRIPT_MEDIA_TYPE` |
| 8 | `modules/gateway/tests/test_cli_download.py` | The pinned exact set |
| 9 | `docs/design-notes/5180-cli-command-contract.md` | One row in the §1 command registry (lines 23-35) for the `adp models` area, owner #5423 (§9.3) |

Plus tests: `tests/cli/test_models_dispatch.py` mirroring
`test_superplane_dispatch.py` (which already pins items 2, 4, 5, 6, 7 independently),
`tests/cli/test_models_io_contract.py` per §3.1, and the command-behaviour tests for
AC-02 through AC-11.

AC-10 asks for the edit list to be "asserted in code". The dispatch-parity mirror does
that for items 2, 4, 5, 6 and 7; item 8 is self-asserting; item 3 (help text) is
asserted by nothing today for any helper and should get one line in the new dispatch
test.

### 7.2 Shared-file coordination

Items 2–5 touch `cli/adp` and `install.sh`, which #5039, #5179 and #5185 also edit;
items 6–8 touch files #5184 and #5039 recently edited. The 5180 contract §2.2 sets the
rule: keep the patch to shared files minimal and land the allowlist entry in the same
PR as the helper — "never before, or the CLI advertises a command that 404s on
download." This story adds nothing to `adp_common.py`. If §3.2's secret-guard promotion
or §3.3's body-passthrough are wanted, they are #5185's to make, and this story's
design works without either. The signer also stays out of `adp_common.py` (§5.5.2).

One deviation from §2.2 must be declared rather than absorbed. The contract says a
feature story's patch to shared files is "**exactly three edits, and nothing else**"
(5180 §2.2, line 76) and that the integration owner #5185 applies them. This story needs
those three plus items 4, 8 and 9 — the second `CLI_FILES` copy (which §2.2 does not
mention but `test_superplane_dispatch.py` requires to match), the pinned exact-set test
(which cannot be left red), and the registry row. Items 4 and 8 are mechanically forced
by existing tests; item 9 is the locked §9.3 decision. **#5185 should be named as
reviewer on the PR**, and `adp models` is a new command *area*, not a verb inside an
existing one, which is a registry change #5185 owns per 5180 §8.

### 7.3 Deployment, rollout and rollback

- **Publication**: `gateway-deploy.yml` fires on merge for `modules/gateway/cli/**`
  (line 7) and its backend regex matches (line 98), so the new helper reaches the
  served artifact with no workflow change. Confirmed, not assumed — this is what
  `tests/cli/test_deploy_filter_covers_cli.py` protects.
- **User delivery**: existing users run `adp update`, which re-pulls `install.sh` and
  every `CLI_FILES` entry from the download route. Users on an old copy see the
  dispatcher's "CLI helper missing. Run adp update." (`cli/adp:783-785`) — the correct
  message, and the reason item 4 matters.
- **Ordering**: this helper is inert until #5419's endpoints are deployed. A `set`
  against a gateway without them returns 404, which `adp_common.py:108` renders as
  "Check the target; the gateway may need an upgrade." That is an acceptable
  intermediate state, and it means this story may merge before #5419 deploys — but it
  must not be *announced* to users before then.
- **Machine-path ordering**: the signed path needs no server-side prerequisite of its
  own beyond #5419's endpoints — the route already exists and already strips `/agent`
  (§5.5.1), which is why Rev-2's P-1 is withdrawn (§9.3). What it does need is #5419's
  canonical service-principal identity slice: until that is deployed, a signed `set`
  either 404s (endpoints absent) or is refused with 403 → exit 3 (role not resolvable to
  a canonical principal). Neither degrades to the human plane, because the signed path
  uses a different external URL and never sends the bearer token (§5.5.3). The release
  note must not claim service-principal self-management works until PMM-09 has exercised
  it live (§8).
- **Rollback**: revert the PR. Saved mappings are untouched and remain effective;
  removing a read/write client cannot change any run's model. This claim is checkable
  because the helper holds no local state and performs no migration.

---

## 8. Acceptance: what tests can prove and what they cannot

Deterministic, provable in `tests/cli/` before review:

| AC | Provable claim at this layer |
|---|---|
| AC-01 | Envelope shape on stdout, narration on stderr, exit codes — as real subprocesses with the streams captured separately (the only way that assertion means anything; see `test_io_contract.py:47-56`) |
| AC-02 | A catalogue-returned alias resolves and reports its canonical identifier; an unknown one is refused with a next action; **no alias constant exists in the helper** |
| AC-03 | No self command defines a principal argument; such an argument exits 1 with no request sent |
| AC-04 | A 403 from the target-taking endpoint becomes exit 3 and no write is attempted afterwards |
| AC-06 | On 409 the helper re-reads, reports both revisions and states nothing was written (§3.3) |
| AC-07 | Identical re-`set` and re-`reset` exit 0 reporting `changed: false`, against a stub honouring #5419's idempotency |
| AC-08 | `--dry-run` issues no write request and no invocation request |
| AC-09 | No credential-valued flag in the parser; token absent from both streams and from every path and query (§3.2); on the signed path, no credential material in argv, output or request body, and the `botocore.auth` log filter installed (§5.5.2) |
| AC-10 | Items 2 and 4–7 of §7.1, via the dispatch-parity mirror |
| AC-11 | `explain` renders the server's source field; no precedence logic in the helper |
| AC-05a (new) | On the signed path: `--service-principal` exits 1; no principal argument is defined; no identity header is sent; the signature is deterministic for fixed credentials and clock (§5.5.4, §5.5.5) |
| AC-05b (new) | Human and machine callers produce the **same** envelope field names and `source` values for the same mapping state, asserted against one shared stub (§9.4) |
| AC-05c (new) | **Static credentials are refused.** With `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` set and no session token, the signed path exits 5 naming the static-credential refusal, signs nothing, and does not fall back to the bearer path (§5.5.2, §5.5.3). This is fully deterministic offline and is the machine path's most valuable local test |
| AC-05d (new) | The signed path's request URL carries the `/agent` prefix and **no** `/api` segment, and the human path's does the reverse — asserted on the request the helper would send, so a developer cannot satisfy it by adding a second backend route (§5.5.1) |
| AC-05e (new) | **A displayed default names its compatibility class and its candidate/proven status, and the string "the platform default" appears nowhere in the helper.** Asserted against a stub returning a class-keyed default: `mappings list` and `mappings reset` name the class; a candidate default is not rendered as proven (§4.3, §5.1; canonical note Rev-4 §6.8). Deterministic offline |
| AC-06a (new) | **A stale-marked write response is never rendered as success.** Against a stub returning a 2xx `set` response carrying a stale evidence marker, the helper exits **4**, does not print a success line, and states the saved state is unknown and must be read back (§5.3). Deterministic offline, and the highest-value new test on this surface: it is the one place where a correct-looking server response could be laundered into an exit 0 |
| AC-06b (new) | **Every stale-evidence refusal is exit 4 regardless of arrival shape.** A 422 with reason `evidence_stale`, a 422 with reason `probing_disabled`, and a stale-marked response all exit 4; `unknown_model`, `not_permitted`, `harness_incompatible` and `retired` all exit 5 (§5.3, §4.4). This pins the remap that the shared transport's blanket non-401/403 → 5 mapping (`adp_common.py:113`) would otherwise defeat silently |

**Not provable here, and the completion report must say so:**

- **AC-05** (service-principal targeting succeeds and names the principal) needs a real
  entitled caller. Against a stub it proves only that the helper sends the identifier
  and prints what comes back.
- **The machine path end to end.** Signature acceptance at the API Gateway edge, agent
  registry resolution, and canonical service-principal resolution (§9.3) are all
  live-only (§5.5.5). A passing signing unit test is not machine-path acceptance and
  must not be reported as one. In particular, AC-05c proves the helper **refuses** a
  static credential — it does not prove that a correctly-configured workload's signature
  is accepted.
- **AC-03's second half** — "prove the other principal's row is unchanged" — is a
  database assertion. It belongs to #5419 AC-04, which traces it through the real
  FastAPI route. At this layer the provable property is that no request carries another
  principal's identifier. Attributing the row-level proof to this story would let a CLI
  test stand in for an authorization test it cannot perform.
- **AC-07 (idempotency) cannot be exercised live in PMM-03's shipped posture.** Two
  successful saves are needed, and with probing disabled at a zero budget every save is
  refused for want of invocability evidence (§9.7). It stays provable against a stub and
  moves to PMM-09 for live proof, alongside the machine path. The same applies to any AC
  whose premise is a *successful* `set`.
- Every AC depending on #5419/#5420 response shapes is proven against a **stub** until
  those merge. A stub agreeing with the helper proves the two agree, not that either
  matches the deployed gateway. One shape to stub carefully: the catalogue row carries no
  alias field at PMM-03's current head (§9.7 item 1), so a stub that invents one would let
  `catalog`'s alias column pass a test against a contract that does not supply it.

**Live acceptance hooks for PMM-09.** #5423's boundary is a merged implementation, and
live proof is PMM-09's. To make that handoff executable rather than aspirational, the
precedent is `modules/gateway/scripts/test-cli-routing.py` — an adapter that drives the
real installed CLI against a fixture-owned scope and refuses to touch anything outside
it (`test-cli-routing.py:30-45`, fenced by `tests/cli/test_cli_routing_adapter.py`).
PMM-09 should mirror that shape: a fixture-owned principal and persona, the real
installed `adp models`, and a refusal to write a mapping for any principal outside the
fixture. This story does not build it; it names it so PMM-09 does not invent a new one.

**Execution:** `cd modules/gateway && ruff check src/ tests/ && ruff format --check
src/ tests/ && python3 -m pytest tests/ -q`. The wrong result that must fail: a helper
passing its own tests while the allowlist entry is missing — covered by the dispatch
mirror plus the pinned exact set.

---

## 9. Settled decisions, and the capability this story consumes

**No decision in this note is open, and nothing here waits on an operator.** Rev-1 listed
four open decisions; Rev-2 recorded them as settled by the first review; Rev-3 aligns them
to the unified architecture rulings on #5417, which **supersede conflicting story-local
recommendations** — including two of this note's own earlier ones, corrected in §9.1 and
§9.3. What remains is one **consumption dependency** on #5419, not a prerequisite this
story could have discharged itself.

### 9.1 A service principal manages its own mapping through the CLI — in this story

Rev-1's recommendation (defer machine self-management as "a new authentication path…
It does not belong in this story") is **rejected**. First-class ADP CLI support for
both human and service-principal invokers is a requirement, not a stretch goal.

The design is §5.5. Its shape, restated so this section stands alone:

- **Transport, not a credential.** The machine path adds request signing (SigV4 over
  `execute-api`) and no secret at rest.
- **Only refreshable temporary credentials may sign** — and this is the Rev-2
  correction. Rev-2 said credentials come from "the workload's own provider chain" while
  claiming a static key pair was impossible; verified, `session.get_credentials()`
  returns exactly a static `AWS_ACCESS_KEY_ID` pair with no session token. §5.5.2 now
  requires a `RefreshableCredentials` instance **and** a non-empty session token, and
  refuses otherwise. No ambient-env static key, no shared-credentials-file key, no
  bearer fallback, no sign-anyway-with-a-warning.
- **Reuse, don't revive.** The signing shape is extracted from the working M2M client
  at `modules/agent-factory/agent-worker-image/adp_cred/client.py:88-148` — the lazy
  `botocore` import, `SigV4Auth(credentials, "execute-api", region)`, signed headers
  copied onto a `urllib` request — together with the `botocore.auth` DEBUG log filter
  from `adp_trigger/transport_identity.py:9-16` and the explicit-provider posture of
  `worker_credentials()` (`transport_identity.py:47-79`, `disable_env_vars=True`). The
  deprecated helper `modules/gateway/cli/bg-auth.sh` is **not** revived, not re-added to
  `ALLOWED_SCRIPTS`, and not referenced: it is not a signer at all but a
  raw-credential poster (`bg-auth.sh:227`, `:288-296`), which is the exact pattern
  `src/auth/routes.py:115-125` returns 410 for. §5.5.6 records this as a rejected
  option with the reason.
- **No reusable provider secret is accepted.** There is no flag, env var or file from
  which the helper reads a long-lived access key pair.
- **Mode selection is not a caller-supplied principal.** The signed path is selected by
  environment (the workload's own context), never by a flag naming who to act as
  (§5.5.3).

**Security-review status:** §5.5.6 states the reviewable claims and the rejected
options. This design note is the request for that review; the review is a
merge prerequisite for the implementation PR, not for this note.

### 9.2 `adp models service-principals list` ships, and `catalog` requires a persona

Both surface additions are locked and are in §4.1:

- `adp models catalog --persona KEY` — required by D6, and **required rather than
  optional**. §5.1 derives it and withdraws Rev-2's unfiltered form: a caller cannot
  choose a model for a persona without seeing which models that persona's compatibility
  class accepts, the filter must come from the server's compatibility data rather than a
  local table, and the ruling allows **no cross-class fallback** for an incompatible pick.
- `adp models service-principals list [--json]` — the discovery command for **human
  administrators**, mapping onto `GET /me/persona-models/manageable-service-principals`.
  The canonical principal ID is opaque and immutable by design, so it cannot be derived
  from anything an operator already knows; the CLI must not require an identifier
  obtainable only from the UI. The server decides the set; the helper prints it and
  derives no entitlement locally. Rev-2 called this command `service-accounts list` and
  its identifier `service_accounts.id` — both corrected per §2.7.

The machine path adds no verbs: a signed caller runs the same `mappings`/`set`/`reset`/
`explain` commands, with self implied by the signature (§4.1, §5.5.4).

### 9.3 This epic owns `adp models`, and must update the #5180 registry in the same implementation

Locked: `adp models` is owned here, not folded into #5180. The obligation that comes
with ownership is that the implementation PR adds the `adp models` row to the command
registry in `docs/design-notes/5180-cli-command-contract.md` §1 (lines 23-35) — this is
item 9 of §7.1, and §7.2 declares the resulting deviation from that contract's
"exactly three edits, and nothing else" (line 76) with #5185 as reviewer.

**P-1 is withdrawn.** Rev-2 filed "the endpoints must be mounted under an `/agent/`
prefix" as a server-side prerequisite. It is not needed: `/agent/{proxy+}` **strips** the
prefix before forwarding (`main.tf:285`, and the explanatory comment at `:317-318`), so
the existing single handler is already reachable by a signed caller, and the API ruling
says explicitly not to duplicate the backend router. The residual obligation is
CLI-side — prepend `/agent` to the external path (§5.5.1) — and is in this story.

**One capability this story consumes, owned by #5419 (PMM-02):** the **canonical
service-principal identity slice** — the opaque immutable `canonical_service_principal_id`,
the tenant-scoped source-qualified alias registry over `(org_id, alias_source, alias_id)`,
canonical resolution performed **in authentication**, and the manageable-principals
endpoint behind `service-principals list`.

| Why the CLI cannot supply it | Evidence |
|---|---|
| Three alias sources exist and are disjoint: a signed caller arrives as a DynamoDB `agent_name`, a Cognito M2M caller as a `client_id`, and Postgres keys `service_accounts.id` separately. None is the canonical owner of a preference, and the same machine can appear under more than one. Only the server, holding the alias registry, can map an authenticated alias to the principal that owns the mapping — writing under whichever label arrived reproduces the #4744 identifier-mismatch class silently | §2.7; `src/auth/agent_registry.py:255`, `:260`, `:263`; `src/auth/middleware.py:677-680`; `src/auth/auth_service.py:309-312`; `src/shared/models/organization.py:36`, `:209-218` |

This is a consumption relationship, not a blocker this story can discharge: the CLI's
obligation is the negative one in §5.5.4 — it sends no principal identifier and no
identity header on the signed path, so there is no client-supplied value for the server
to trust. Until the slice is deployed, the machine path cannot be **accepted live**;
§8 states that as a live-only gap rather than papering it over with a passing unit test.

### 9.4 One schema across three callers; **two** route shapes, and one shared self handler

Locked, and stated normatively in §1: all three callers share the same request and
response semantics, the same JSON schema, the same refusal vocabulary and the same exit
codes.

**What is shared is the contract, not the handler set — Rev-3's heading and body were
wrong here** and the third-pass review was right to correct them. Rev-3 said all three
"reach the same handlers". The accurate statement, which is what the API ruling says:

- **The self routes exist once**, at `/me/persona-models…`. Human self and
  service-principal self reach **that one handler** — the signed caller's external path
  is `/agent/me/persona-models…` and API Gateway strips the `/agent` prefix before the
  request reaches the pod (§5.5.1). Two front doors, one handler. Duplicating it into a
  human router and a machine router is forbidden.
- **Human administration of a service principal uses distinct handlers**, at
  `/service-principals/{canonical_id}/persona-models[/{persona_key}]`. They take the
  target in the path and are human org-admin only and tenant-checked. They cannot be the
  self handler, because the self handler's security property is that no target can be
  expressed on it at all (§4.1).

Rev-2's earlier "only the credential differs" was also wrong, for a different reason the
second-pass review caught: the **external URL differs too**. Both errors have the same
root — treating "one contract" as "one route" — and the correction is the same: the schema
and refusal vocabulary are common; the route shape and the credential are not.

Consequences the implementation must honour:

- AC-05b (§8) asserts envelope equality across the human and machine paths against one
  shared stub. If a field is present for one caller and absent for another for the same
  mapping state, that is a bug in the server contract, to be raised on #5419 — not
  smoothed over in the helper.
- `--json` output is the same document for all three callers. No caller-type branch in
  the rendering code.
- The signed path must not acquire a parallel "machine" schema by accident, which is the
  main way a second contract gets born. **Two** caller-dependent branches are permitted
  anywhere in the helper, and they are both transport: which URL (self versus
  target-taking, `/agent`-prefixed versus not) and which credential. Nothing downstream of
  the response may branch on caller type.

### 9.5 A 409 is followed by a safe GET, reported as "current as of the re-read", and no response body is ever echoed

Locked; the mechanism and its two properties are in §3.3. Restated because it is the one
place a developer is most likely to "fix" the design by reversing a security property:

- On 409 the helper performs one **safe GET** of the same principal and persona, and
  reports the revision it reads as **current as of the re-read** — not as the revision
  that rejected the write.
- If that GET fails, the helper reports the conflict and the failed re-read. A failed
  re-read must never mask the conflict or turn it into a success.
- **The untrusted response body is never echoed.** `Api.request`
  (`adp_common.py:96-113`) already extracts only `re.fullmatch(r"[a-z_]{1,80}", reason)`
  from an error body, and "No response body is echoed verbatim" is a property of the
  shared contract. Widening that — including "just for the conflict message" — is out of
  bounds for this story.

### 9.6 Status of this note

**Proposed, pending the #5417 cross-story synthesis** (header, and §11). It is not
"ready", for one reason that survives Rev-4: the rulings this note conforms to are
binding, but the canonical design document carrying them is still the **open, unmerged**
PR #5436 (head `8edc055c`, Rev-4).

The *reason* it is proposed has narrowed. At Rev-3 there were two: the unmerged canonical
note, and a sibling story that had not absorbed the rulings this surface depends on. The
second is gone — PMM-03 absorbed them at `25ece717` (below). What remains is only that the
documents carrying the binding contract are open PRs rather than merged text.

Reconciled at current heads rather than the versions Rev-3 read:

| Sibling | Current head | Bearing on this note |
|---|---|---|
| #5436 (PMM-01 canonical note) | `8edc055c`, Rev-4, **open** | Adopts U1–U6 and settles the persona-to-class owner (PMM-03). Its §6.8 places two obligations on this surface, absorbed in §4.3 and pinned by AC-05e |
| #5434 (PMM-03 catalogue note) | **`25ece717`, rev-3, open** — *not* the `6d5b2d10` Rev-3 of this note cited | **The dependency Rev-3 described is discharged** (§9.7). Two *new* coordination items replace it, both from this head |
| #5420 (PMM-03 issue) | **open**; filed body still has no harness or compatibility-class mention (verified: zero occurrences) | The story's **note** now carries the class registry as new scope; the filed issue body does not. The issue should be amended so the AC list includes it |
| #5419 (PMM-02 issue) | **open** | Supplies the endpoints, the class-keyed default record, and U1's canonical identity slice this note consumes (§9.3) |

**Rev-3's stated dependency is out of date and is withdrawn.** Rev-3 said PMM-03 "needs a
persona-row class attribute it does not have, must restate its seeded default as a
candidate, and must change its probe posture" — and named the missing class attribute as
the one item an operator might want to sequence. At `25ece717` all three are closed in
PMM-03's note: its new §2.4 defines the persona→class registry with stable unversioned
class IDs and `compatibility_class` on every persona row; §3.3 records the D4 identifier as
a Claude-class candidate and seeds no default; §4.6/§4.7 ship probing disabled at a zero
budget. `catalog --persona` is no longer specified against an unowned, unspecified
registry. Reporting that dependency as live would have sent an operator to sequence work
that is already designed.

### 9.7 Two coordination items that replace it, both new at PMM-03's current head

Neither is an operator decision and neither blocks writing this story. Both change what
the developer builds, and both were invisible until PMM-03's current head was read.

**1. The catalogue row publishes no alias, so `catalog` cannot tell a user what to type
into `set`.** This is a contract gap between two stories that each behave correctly on
their own.

- §4.3 of this note requires `catalog`'s `detail` to carry each model's **alias**, and
  §5.2 forbids the helper from holding any alias table — the whole point being that
  aliases come from the server so a third hand-maintained list cannot drift on users'
  laptops.
- PMM-03's catalogue row at `25ece717` §6.2 carries `canonical_model_id`, `model_family`,
  `canonical_version`, `selectable`, `reason`, `permitted`, `invocable`, `evidence{…}`,
  `compatibility_class`, `harness_contract_revision`, `retired` and `price_context` —
  and **no alias field**. Verified: the word does not appear in that row contract.

The user-visible consequence: `adp models catalog --persona architect` lists models by
canonical ID and family, `set --model` takes an alias, and nothing in the output tells the
user which alias reaches which row. The command area's own §5.2 rationale — "an unknown
alias is refused with the server's reason and a next action naming
`adp models catalog --persona KEY`" — points the user at a command that cannot answer the
question. Note this is *not* a case for adding a local alias map; that is the failure §5.2
exists to prevent, and it would drift the same way the two existing maps did.

Recommended resolution, and it belongs to PMM-03 rather than here: add a nullable `alias`
to the catalogue row, null where a canonical ID has no alias pinned. Nullable matters and
is not a detail — the D4 candidate identifier `us.anthropic.claude-sonnet-4-6` has no
alias today (§2.5) and PMM-03 is explicit that it must not add one, so a non-nullable
field would force exactly the silent alias addition PMM-03 forbids. Until it exists, this
story's §4.3 `catalog` row prints the alias **where the server supplies one** and prints
nothing where it does not — it never derives one. Raise on #5420.

**2. In the posture PMM-03 ships, nothing is selectable and every save is refused.** This
is stated plainly in PMM-03's §4.7 at head: probing is disabled at a zero budget, every
model row reports `invocable: null` with reason `probing_disabled`, "**Nothing is
selectable**", and "PMM-02's save path refuses every save while probing is off". The
catalogue *read* still succeeds and still lists personas, classes, models and retirement
state — it is certification that is withheld, not the list.

This note's §10 and §8 read as though `set` works once #5419 deploys. It does not: on a
gateway with both #5419 and #5420 deployed and probing still disabled — which is the
shipped posture, not an edge case — `catalog`, `mappings list` and `explain` all work, and
every `set` is refused for want of invocability evidence. Three consequences the
developer must build to rather than discover:

- **The refusal is exit 4, not 5** (§5.3, §4.4). `probing_disabled` is the canonical
  example of a condition that resolves by waiting rather than by fixing the request.
- **The message must name the cause and the resolution**, or every user reads a
  correct fail-closed refusal as a broken command. PMM-03's vocabulary distinguishes
  `probing_disabled` from `not_invocable` for exactly this reason: "an operator reading
  'not invocable' would go looking for a Bedrock problem that does not exist." The
  helper must carry that distinction through rather than flattening both to "refused".
- **AC-07 (idempotency) cannot be exercised live in this posture**, because it needs two
  successful saves. It stays provable against a stub (§8) and moves to PMM-09 for live
  proof, alongside the machine path.

PMM-04 handles the same posture with a backend-served `agent_models` feature flag,
default false, enabled in PMM-09. The CLI has no equivalent and needs none — a CLI user
who runs a command and gets an actionable refusal is in a different position from a user
staring at a screen that offers a choice it cannot save. But the **release note must not
announce `adp models mappings set` as working** until PMM-09 has enabled probing, for the
same reason §7.3 says the surface must not be announced before #5419 deploys.

---

## 10. Parallelism

| Work | Depends on | Can start |
|---|---|---|
| Packaging edits + dispatch-parity mirror (§7.1 items 2–8) | Nothing | **Now**, independent of #5419/#5420 |
| Helper skeleton: parser, envelope, exit codes, `--json`, Ctrl-C | Nothing | **Now** |
| Signing module + its unit tests (§5.5.2): URL/prefix construction, the static-credential refusal (AC-05c), the negative `--service-principal` test, the log filter | Nothing — all four are deterministic offline with synthetic credentials | **Now** |
| Stale/refusal exit-code mapping (§5.3) and its tests (AC-06a, AC-06b) | #5420's published refusal vocabulary, which exists at its current head | **Now** — the codes are known (`evidence_stale`, `probing_disabled`, `harness_incompatible`, `retired`, …) and the assertions are stub-driven |
| `catalog --persona`, `mappings list`, `explain` against a stub | #5420 / #5419 response shapes agreed | On shape agreement, before either merges |
| `set` / `reset` / `--dry-run` / conflict re-read, **refusal paths** | #5419 endpoints merged | After #5419 |
| `set` / `reset` **succeeding**, and AC-07 idempotency, live | #5419 endpoints **and** probing enabled with approved spend (§9.7 item 2) | PMM-09 — not after #5419. With probing disabled every save is refused by design |
| `service-principals list` | #5419's manageable-principals endpoint | After #5419 |
| `--service-principal` (human administering a service principal) | #5419's target-taking endpoints | After #5419 |
| Machine path accepted end to end | #5419's canonical identity slice (§9.3), then a deployed gateway | PMM-09 |
| Live adapter for PMM-09 | Deployed gateway | PMM-09 |

Two honest qualifications on the "Now" rows, because Rev-2 overstated them. The signing
module's *unblocked* part is everything that does not need a server: the URL it builds,
the credential class it refuses, the headers it does not send, and the log filter it
installs. What it cannot establish locally is that a real signature is **accepted** —
that is the live gap in §8, and it is not a matter of waiting for a stub. And the
packaging edits are only unblocked in the sense that they can be written; they must not
merge ahead of the helper, because the 5180 contract requires the allowlist entry and the
helper in the same PR (§7.2).

**A third qualification, new at PMM-03's current head.** The last two rows split what
Rev-3 had as one. Rev-3 put `set`/`reset` and AC-07 after #5419, which reads as "once the
endpoints deploy, saving works". It does not: with probing disabled at a zero budget — the
posture PMM-03 ships — every save is refused for want of invocability evidence (§9.7 item
2). The *refusal* paths are buildable and testable after #5419; the *success* paths reach
live proof only in PMM-09. A developer working from Rev-3's table would have reported a
correct fail-closed refusal as a broken deployment.

Nothing in this table is gated on an operator decision. The packaging edits are where most
of the "looks broken to every user" risk lives, so sequencing them first is still the
useful order.

---

## 11. Verdict

**PROPOSED, pending the #5417 cross-story synthesis.** The design is complete for all
three callers — human self, human administering a service principal, and service-principal
self. **No decision in §9 is open and nothing waits on an operator.** What remains is one
consumption dependency on #5419's canonical service-principal identity slice (§9.3), which
gates live acceptance of the machine path, not the writing of this story.

What a reviewer should check first, in this order: **§5.3's stale-response rule** (the one
place a wrong reading turns a fail-closed refusal into a reported success), §5.5.2's
credential rule, and §5.5.1's plane mechanism. Those are the three places an earlier
revision was wrong in a way that would have shipped a defect — laundering a stale write
into an exit 0, signing with a static access key while claiming that was impossible, and
building a second backend route the existing prefix-stripping proxy makes unnecessary.

**Three Rev-3 errors are corrected and must not be reintroduced:**

| Rev-3 claim | Correction |
|---|---|
| All three callers "reach one backend handler set" | Only the two self callers share a handler. Human administration uses the distinct target-taking `/service-principals/{canonical_id}/…` routes. What is common is the schema, refusal vocabulary and exit codes — not the route (§1, §9.4) |
| If the server accepts a write on stale evidence, report the acceptance *and* the staleness | A stale-marked write response is a protocol violation and is **never** rendered as success. The helper exits 4, declines to certify the write either way, and names the read-back command. Every stale-evidence refusal is exit 4 whatever shape it arrives in (§5.3, §4.4, AC-06a/AC-06b) |
| `us.anthropic.claude-sonnet-4-6` is "the canonical default" | A Claude-class **candidate** pending a bounded invocation in PMM-09; defaults are class-keyed (§2.5, §5.1) |

**Five Rev-2 errors remain corrected and must not be reintroduced either:**

| Rev-2 claim | Correction |
|---|---|
| Canonical identity is Postgres `service_accounts.id`, reached by joining the role ARN | An opaque immutable `canonical_service_principal_id` owns the preference, reached through a tenant-scoped source-qualified alias registry over **three** sources including Cognito M2M `client_id`. No raw alias ever owns a preference (§2.7, §9.3) |
| Credentials come from the workload's provider chain; a static key is impossible | `session.get_credentials()` returns exactly a static key pair from `AWS_ACCESS_KEY_ID`, verified. Only refreshable credentials with a session token may sign; everything else exits 5 with no fallback (§5.5.2, AC-05c) |
| The endpoints must be mounted under an `/agent/` prefix (prerequisite P-1) | `/agent/{proxy+}` already strips the prefix (`main.tf:285`, `:317-318`), so the single existing handler is already reachable. P-1 withdrawn; duplicating the router is forbidden by the API ruling (§5.5.1, §9.3) |
| An unfiltered `catalog` form ships alongside the persona-filtered one | Withdrawn — it contradicted this note's own D6 reasoning and re-creates the predictable-refusal failure D6 exists to prevent, with no cross-class fallback available (§5.1) |
| `mappings list` narrates a no-saved-row persona as using "the platform default" | There is no platform-wide default. The default is keyed by compatibility class, so the output names the class and the candidate/proven status. This is the canonical note's Rev-4 §6.8 obligation on PMM-05, pinned by AC-05e (§4.3) |

Three corrections in §3 must still reach the developer: the IO-contract test cannot be
extended as the issue describes, the secret-argv guard is not shared and the human path
has no secret to guard (the signed path's equivalent guard is AC-09's signing clause), and
the conflict revision cannot reach the user through the shared transport without the
re-read. The third would most likely be "resolved" by echoing an error body — reversing a
deliberate security property of a shared file, and forbidden by §9.5.

Two Rev-1 citation errors corrected in §2.6 also stand: `auth_source` is set at
`src/auth/agent_registry.py:263` and declared at `src/shared/schemas/auth.py:57` (not at
`src/auth/middleware.py:173-190`, which reads only `account_type`), and `bg-auth.sh` is
not a SigV4 helper.

**Rev-3's residual dependency is discharged, and two narrower coordination items replace
it (§9.6 and §9.7 have the detail).** Rev-3 reported that PMM-03 had not absorbed the
rulings this surface depends on — no persona-row class attribute, a seeded default rather
than a candidate, and the wrong probe posture. At PMM-03's current head `25ece717` all
three are designed (its §2.4, §3.3, §4.7), so `catalog --persona` is no longer specified
against an unowned registry. Reporting that dependency as live would have sent an operator
to sequence work that is already done.

What replaces it, neither of which is an operator decision:

1. **The catalogue row carries no alias field**, so `catalog` cannot tell a user what to
   type into `set`, and §5.2 rightly forbids the helper from inventing a local alias table.
   Resolution is a nullable `alias` on PMM-03's row — nullable because the D4 candidate has
   no alias today and PMM-03 is explicit that it must not add one. **Raise on #5420.**
2. **In PMM-03's shipped posture nothing is selectable**, so reads work and every save is
   refused until PMM-09 enables probing with approved spend. This is correct fail-closed
   behaviour; the consequences for this story are the exit-4 mapping, a message that names
   the cause, and AC-07 moving to PMM-09 for live proof.

One issue-hygiene item for the operator: **#5420's filed body still does not mention
harness compatibility or a compatibility class** (verified: zero occurrences). The class
registry lives in that story's design note as new scope but not in its acceptance
criteria, so a developer working from the filed issue alone would not build it. Amending
#5420's body is the fix.

The note stays **proposed** for one reason only: the documents carrying the binding
contract — #5436 (`8edc055c`) and #5434 (`25ece717`) — are open PRs rather than merged
text. Nothing in this note waits on an operator answer.
