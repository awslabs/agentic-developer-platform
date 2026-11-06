# Knowledge publication completion and remaining checks

Content write and independent Asset readback leave the original binding operation
`waiting`. They do not prove later native use. The Gateway evidence contract below
can complete that same operation from attributed human evidence and explicit owner
review. It never completes the whole DA19 release or grants a native action.

The original DA07 acceptance requires exact source publication, refresh/recovery
and revocation. DA19 additionally requires meaningful later use of the correct
source and excludes revoked/irrelevant material. Neither requires an undocumented
provider-attested session-to-asset-version receipt. A genuine human observation,
unchanged pinned Asset readbacks and source-dependent behavior may establish the
manual part of that evidence, with its provenance retained.

## Original delivery-board tasks

These task IDs remain unfinished on the original story until their implementation,
review, installation and actual qualification evidence are complete. Source tests
are not live acceptance.

- [ ] **DA07-K1 — Same-operation evidence ledger and inspection.** The implementation
  adds append-only tenant/operation records, current-owner inspection, versioned
  private artifacts, source/effect/native-version pins, and shared human identity
  checks. Review, installation and real evidence collection remain required.
- [ ] **DA07-K2 — New native session and meaningful source use.** Use a real person's
  ADP-to-DevOps SSO observation from the existing bounded human framework. Prepare
  the scenario before a new conversation, then retain the actual transcript and
  exact before/after Asset readbacks. The expected source-dependent behavior must
  be absent from every user prompt. Record the source citation and actual selected
  skill invocation when applicable. Native behavior must still be observed; an
  active skill is not proof of use.
- [ ] **DA07-K3 — Isolation, revocation and non-use.** Real second-principal/Product
  denial, ADP authority revocation before a later provider call and owned native
  cleanup/non-use remain separate evidence. Revoking ADP consent does not remove
  published assets or existing native session context. Inactive skills require
  non-use evidence; they cannot be qualified through the positive-use endpoint.
  This change adds no delete, deactivation or cleanup authority.
- [ ] **DA07-K4 — Qualified completion of the original operation.** The implementation
  adds current-owner human review, ETag/idempotency, complete selected-source
  coverage, immutable evidence references and configuration → binding → work locks.
  It transitions only the original operation to `succeeded`, preserving its IDs,
  snapshots, effects and lifetime dispatch accounting. Review and installed
  qualification remain required.
- [ ] **DA07-K5 — Expired authority after an uncertain effect.** The implementation
  adds explicit current-human-owner abandonment after any native effect starts,
  preserving immutable effects, original work/snapshots and Asset reservations.
  Expired/revoked source or native consent does not prevent this local terminal
  acknowledgement. Review and installation remain required. Abandonment neither
  makes an uncertain effect writable nor replaces expired bytes or claims cleanup.

## Bounded manual evidence contract

All paths extend the original owner route:
`/v1/products/{product}/projects/{project}/services/devops/binding-operations/{operation}/knowledge-evidence`.

| Action | Path | Meaning |
| --- | --- | --- |
| GET | service `/knowledge-operations` | Discover current-owner original operations with optional binding filter and bounded pagination. |
| POST | `/prepare` | Select a source, existing native UI challenge and predeclared scenario. |
| POST | `/{evidence}/observe` | The same selected human records actual native artifacts. |
| GET | the base path | Inspect bounded immutable evidence metadata. |
| GET | `/{evidence}/artifacts/{prepared-or-observed-or-completed}` | Read private bytes with current source access and retention. |
| POST | `/complete` | Current Product/configuration owner reviews each selected source's evidence. |

Inspection returns the current operation revision/ETag, original Product,
configuration and binding selection, canonical source keys and bounded display
metadata before any evidence exists. Source bytes, prompts and transcripts remain
private artifacts. Asset ID/version is included only when current publication and
source scope validates. `eligibility` reports preparation/review availability and
a blocking code; actual mutation endpoints still revalidate every requirement.
Operation discovery uses `binding_id`, `offset` and `limit` (maximum 100), returning
`items` and `next_offset`. It never substitutes a new operation for the original.

Every route uses the shared interactive human-session boundary: current JWT and
workspace membership, logout revocation, and fixture/automation-credential refusal.
The observer must be the current legitimate Product/configuration owner and the
same person on the selected existing native UI challenge. That challenge supplies
its original release, Product, binding and actual SSO launch/arrival provenance;
this API neither invents a parallel human identity nor starts a browser.

Preparation requires `Idempotency-Key` and the original operation's `If-Match`.
It binds the exact intent, configuration, native space, selected source snapshot,
source revision/hash, mapping, publication effect and native Asset ID/version.
The human declares the prompt, source excerpt and distinctive expected behavior
before the new session. The window is at most 30 minutes and no longer than the
human session, original challenge or source retention. Store the real readback
contents and transcript, not an arbitrary URL or a boolean success claim.

Observation retains the session identity, actual agent type, start time, transcript,
source citation and response quote. Both observed Asset versions, metadata and
content must match the worker's independently verified publication receipt. The
session must start after preparation and publication. Global instructions may be
used by any supported concrete agent type; scoped skills must target that agent.
All quoted behavior/citations must occur in the retained assistant transcript.
These checks establish consistent artifacts; semantic correctness requires the
owner's explicit explanation of how the selected source affected the decision.
The records retain `authenticated-human-native-use-observation`, never provider
attestation. The original native UI evidence keeps its partial flags unchanged.

Completion requires a reviewed observation hash for every selected publication,
exact retained source/effect pins and current owner/source access. It rereads the
versioned artifacts and validates collection at its historical time. Expiry of a
human collection window does not erase a fact recorded within that window; the
reviewer must authenticate anew. Source retention expiry still prevents content
review/completion. Current human local read authority is separate from the expired
service/native write grant: completion does not renew that grant or call AWS.
A source change, revocation, foreign evidence or incomplete coverage is actionable
failure, never inferred success.

The resulting operation records `native_use_qualified: true` with method
`authenticated-human-observation-and-owner-review`, while retaining
`provider_attested_native_use: false` and `release_check_completed: false`.
The release's independent evaluator still checks all original criteria, including
isolation, source refresh/revocation, native cleanup and both real Products/users.

[Asset APIs](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-managing-assets.html)
expose exact IDs, versions and content.
[Instructions](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-agent-instructions.html)
load at the beginning of new sessions; existing sessions retain prior context.
[Skills](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-devops-agent-skills.html)
load on demand. The manual contract uses these documented facts without inventing
machine-readable provider receipts or enabling UI automation.

## Abandonment after a native effect starts

The current Product/configuration owner may POST to the original operation's
`/abandon` action with `reason`, `native_effects_may_remain: true`, its quoted
operation `If-Match` and an `Idempotency-Key`. The shared interactive human-session
boundary is revalidated immediately before the terminal mutation, including after
lock waits. No source bytes, source grant renewal, native session or provider call
is needed. Never-started work uses `/cancel` instead.

An active worker lease prevents abandonment. After the lease expires, the action
validates complete retained effect chains, their original work/source/native-space
pins and matching reservations under configuration → binding → work locks. It
marks the original operation `abandoned`, never `succeeded`, and retains the same
work ID, generation, fence, lifetime dispatch accounting, snapshots, effects and
native receipts. The append-only evidence ledger records the acknowledgement.

An expired lease does not prove a remote call stopped. Native effects may remain
or their outcome may remain unknown. Ownership reservations remain `abandoned`,
preventing a later publication from treating these Assets as free or safely
reusable. Source/native permission expiry cannot bypass that reservation. Native
remediation needs its own separately authorized, attributable process; this action
adds no cleanup, delete, deactivation or ownership-release authority. The full
DA19 release acceptance and real K2/K3 observations remain unfinished.

## Product UI

In the Product's DevOps service, select the original binding and open **Review
published knowledge use**. Choose a retained publication operation, read its
evidence and select the named publication. Attach your recorded native arrival,
declare the scenario, then start the new native conversation yourself. Record
the actual Asset readbacks, conversation and source citations in the observation
form. Read the retained artifacts and explain each source's observed effect
before choosing **Complete knowledge review**.

Older configuration revisions and binding generations remain readable for the
same Product, configuration and binding, subject to current source access. Their
history cannot authorize new evidence writes. Unconfirmed requests retain the
original body, revision and request key; readback is required before a retry.

The separate **Abandon this knowledge publication** panel reads the original
operation and its retained acknowledgement. The current owner can supply a
reason and explicitly acknowledge remaining native effects, including after
source or configuration expiry. This closes the local operation without
releasing native ownership or claiming successful release acceptance. A worker
lease or inconsistent retained effects still blocks the request with the
server's action message.
