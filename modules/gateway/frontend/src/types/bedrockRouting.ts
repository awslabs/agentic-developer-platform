/**
 * Wire types for the Bedrock account-routing admin surface — Issue #4745 (#4692 · R4).
 *
 * Snake_case field names are kept **verbatim** from `src/admin/bedrock_routing/schemas.py`
 * (the `personCap.ts` / `budgetSpend.ts` convention), so these stay diffable against the
 * Pydantic models. A camelCase transform layer is where a field silently becomes
 * `undefined` — and on this surface the fields that would go missing are the ones that
 * say whether a destination works.
 *
 * **No `role_arn` anywhere, deliberately.** The server has no such field on any response
 * model (`schemas.py`), following the redaction rule R3 enforces by omission: the role
 * ARN goes to the audit row and the server log, and the *account id* is what an admin
 * needs on screen. Declaring an optional `role_arn?` here would invite a component to
 * render whatever a future server leak put in it.
 *
 * **`reason` is R3's vocabulary, not free text.** The same codes appear in save-time
 * refusals and in runtime 502s (§6.7 item 2), which is what lets an admin connect the
 * two. `ROUTING_REASON_COPY` below is the one place they become prose; a component that
 * invented its own wording would break that correspondence.
 */

/**
 * The rungs an admin may author.
 *
 * `platform` is absent, and that is the ladder's shape rather than an oversight: rung 4
 * is the ABSENCE of a mapping (§1.2), so there is nothing to author. The mockup's
 * greyed "PLATFORM / everyone else" row is a rendered fact about that absence, not a
 * record.
 */
export type MappingScopeType = 'user' | 'team' | 'org';

/** The rung that answered an effective-mapping lookup, including the default. */
export type EffectiveRung = MappingScopeType | 'platform';

/**
 * Who authored the winning rule (§1.4).
 *
 * Only ever `self` on a `user` rung — a team or org row has no self author. Derived
 * server-side from `authored_by_user_id`, so the client never computes it.
 */
export type MappingSource = 'platform_admin' | 'self';

/** A scope as the API addresses it: one path segment. */
export interface MappingScope {
  scope_type: MappingScopeType;
  /** Required for `org` and `team`. A team id is unique only inside its org. */
  org?: string;
  /** Required for `team`. */
  team?: string;
  /** Required for `user`. The canonical `users.id`, never a Cognito sub or a login. */
  user?: string;
}

/** A registry row, as the destinations table renders it. */
export interface ExistingAwsConnection {
  credential_id: string;
  label: string;
  account_id: string | null;
  org_id: string;
  org_name: string;
  owner_scope: string;
  owner_name: string | null;
  status: string;
  selectable: boolean;
  reason: string | null;
}

export interface DestinationSummary {
  /** Present for explicit Bedrock links; the original connection retains its owner. */
  connection_id?: string | null;
  id: string;
  account_id: string;
  label: string;
  region: string;
  /**
   * `org-linked` (promoted from a tenant's own connection) or `admin-registered`
   * (platform-scoped, no owning tenant). An explicit string rather than a nullable
   * `owner_org_id` the reader has to interpret.
   */
  source: 'org-linked' | 'admin-registered';
  owner_org_id: string | null;
  routing_capable: boolean;
  verified_at: string | null;
  /**
   * `routing_capable && verified_at !== null`, computed server-side.
   *
   * Read this rather than re-deriving it from the two fields: the resolver uses the
   * model's own predicate, and a client that reimplemented it could offer a
   * destination the request path would refuse.
   */
  usable_for_routing: boolean;
  /** Why it is unusable, when it is. R3's codes — see `ROUTING_REASON_COPY`. */
  reason: string | null;
  /** Mappings still pointing here. Deleting a referenced destination is an outage (§8.3). */
  used_by: number;
}

/** A mapping row, as the rules table renders it. */
export interface MappingSummary {
  id: string;
  scope_type: MappingScopeType;
  scope_id_org: string | null;
  scope_id_team: string | null;
  scope_id_user: string | null;
  /**
   * The wire scope string this row is addressed by.
   *
   * Returned by the server and used verbatim for the subsequent PUT/DELETE, so the UI
   * never re-assembles a path the server would then 422.
   */
  scope: string;
  destination_id: string;
  destination_account_id: string;
  destination_label: string;
  /** False renders the mockup's warning: this rule exists and its traffic fails closed. */
  destination_usable: boolean;
  source: MappingSource;
  updated_at: string;
}

/** Answer to "who serves this person, and from which rule?" (§6.3 element 1). */
export interface EffectiveMappingResponse {
  user_id: string;
  /** `platform` means no rule matched — an answer, not an absence. */
  rung: EffectiveRung;
  account_id: string | null;
  destination_id: string | null;
  destination_label: string | null;
  /** None on the platform rung, which nobody authors. */
  source: MappingSource | null;
  /**
   * **The §1.4 display requirement.** True when an admin has pinned this individual,
   * overriding their own selection on the credentials page. §1.4 settled on "admin
   * wins" *and* required the UI to say so — showing the person's own stale pick as
   * active would be the #4511 inert-config defect one layer up.
   */
  overrides_self_selection: boolean;
  /** What would serve them if the winning rule were removed. */
  shadowed_rung: EffectiveRung | null;
  shadowed_account_id: string | null;
}

/** Promote one of a tenant's own verified connections into the registry. */
export interface RegisterConnectionDestinationRequest {
  source: 'connection';
  credential_id: string;
  label?: string;
}

/**
 * Register an account nobody has linked yet, via the Connect-AWS quick-create flow.
 *
 * `link_to_org_id` is required — the server's reconciliation of the mockup's "Link to
 * org" select against §4.2 requirement 2, so that the cross-tenant scope check stays
 * total. There is deliberately **no `role_name`**: the v2 template names the role
 * `ADP-Agent-${Nickname}` and declares no role-name parameter, so a field here would
 * configure nothing while appearing to.
 */
export interface RegisterNewDestinationRequest {
  source: 'new_account';
  account_id: string;
  label: string;
  link_to_org_id: string;
  region?: string;
}

export type RegisterDestinationRequest = RegisterConnectionDestinationRequest | RegisterNewDestinationRequest;

export interface RegisterDestinationResponse {
  destination: DestinationSummary;
  /** Only for the new-account path: the admin still has to create the role there. */
  launch_url: string | null;
}

export interface VerifyDestinationResponse {
  destination: DestinationSummary;
  verified: boolean;
  reason: string | null;
}

/**
 * One of the caller's own AWS connections, as the §6.4 self-service selector renders it
 * — Issue #4746 (#4692 · R5).
 *
 * **Unselectable rows are in this list, not filtered out of it.** Per §5.0b every
 * connection made with the v1 template is pinned to the person who created it and fails
 * the assumability probe, so on most installs *most* of a person's connections are
 * legitimately unselectable. Dropping them would leave an empty list and no explanation
 * — the dead end §6.4's honest-display requirement exists to prevent. `selectable` says
 * which, `reason` says why.
 *
 * No `role_arn` (§2.6) and no `secret_arn`: the account id is what identifies a
 * destination to a person.
 */
export interface SelectableConnection {
  credential_id: string;
  label: string;
  account_id: string | null;
  /**
   * The connection's own status — `verified` | `pending` | `failed`.
   *
   * Reported even when the row is unselectable, because "not verified yet" and "verified
   * but not routing-capable" need different actions from the person.
   */
  status: string;
  /**
   * May this connection be picked? Requires `verified` **and** routing-capable.
   *
   * Read this rather than re-deriving it from `status`: routing-capability is a separate
   * probe verdict (§5.0b), and a client that inferred selectability from verification
   * alone would offer a choice the server refuses with a 422.
   */
  selectable: boolean;
  /** Why not, when not. Same vocabulary as a save-time refusal and a runtime 502. */
  reason: string | null;
}

/**
 * What actually serves the caller's Bedrock calls, plus what they may pick (§6.4).
 *
 * **One response, because the screen must never show two answers.** The three facts here
 * — the effective destination, the caller's own selection, and whether the two agree —
 * are only meaningful together. A UI that read the stored pick from one endpoint and the
 * effective destination from another would render them side by side and leave the reader
 * to decide which is in force: the #4511 inert-config defect with an extra step.
 *
 * `own_selection_active` is therefore **stated by the server**, not derived here. It is
 * false in two distinct situations that look identical from a stored row alone:
 *
 * 1. A platform admin has pinned the caller (§1.4 "admin wins"), so `effective` reports
 *    the admin's destination and `overrides_self_selection` is true.
 * 2. The caller's own pick has stopped being usable — role deleted, probe now failing —
 *    so the resolver skips it and walks on (§4.4). The row still exists and governs
 *    nothing.
 *
 * Deriving it client-side would mean reimplementing §4.4's skip rule, and getting it
 * wrong means showing a stale pick as active.
 */
export interface MySelectionResponse {
  /** Where the caller's calls actually go. `rung: 'platform'` is an answer, not an absence. */
  effective: EffectiveMappingResponse;
  /**
   * The caller's own user-rung selection, when they have one and it is still theirs.
   *
   * Null when they have authored none, and null when a platform admin has since taken the
   * row over — there is only ever one row per scope, so an admin write *replaces* the
   * person's pick and there is no longer one to report.
   */
  own_selection_destination_id: string | null;
  own_selection_account_id: string | null;
  own_selection_label: string | null;
  /**
   * Which of `connections` the selection was made from, so the list can mark that row.
   *
   * Read this rather than comparing `own_selection_destination_id` to a
   * `credential_id`: they are different id spaces — a mapping references a *destination*,
   * the list renders *credentials* — so that comparison silently never matches. Matching
   * on account id instead would light up the wrong row when one account is connected
   * twice, which the registry allows.
   */
  own_selection_credential_id: string | null;
  /** True only when the caller's own selection is the destination in force. */
  own_selection_active: boolean;
  /**
   * True when a platform admin has taken the user rung.
   *
   * The caller may neither change nor clear the selection while this holds — both writes
   * are refused with `pinned_by_platform_admin` — and §1.4 requires the screen to say so
   * rather than leave a control that can only fail.
   */
  pinned_by_platform_admin: boolean;
  /** Everything the caller may pick, selectable or not. */
  connections: SelectableConnection[];
}

/**
 * Body for `PUT /me/bedrock-routing/selection`.
 *
 * **Names a connection and names no person.** There is no `user_id` field, and that
 * absence is the access control rather than a check that could be dropped: the anchor is
 * derived from the token server-side, so a request pointing at somebody else cannot be
 * formed.
 *
 * A `credential_id` rather than R4's `destination_id` because a person owns connections,
 * not registry rows — they have no way to learn a destination id and no reason to.
 */
export interface MySelectionRequest {
  credential_id: string;
}

/**
 * R3's reason codes as prose an admin can act on.
 *
 * Each string names the remediation, because that is the difference the codes exist to
 * carry: `role_user_pinned_needs_v2_template` means re-run a CloudFormation template,
 * `routing_probe_inconclusive` means try again, and
 * `role_missing_bedrock_permission` means edit an IAM policy. Collapsing them into
 * "verification failed" would send an operator to debug all three.
 *
 * Unknown codes fall through to the raw code (see `describeRoutingReason`) rather than
 * to a generic message: a code the UI has not learned yet is still more useful to
 * whoever is reading the logs than "something went wrong".
 */
export const ROUTING_REASON_COPY: Record<string, string> = {
  role_user_pinned_needs_v2_template:
    'The IAM role is pinned to the person who created it, so it cannot serve other people. Ask the AWS account administrator to update it using the routing CloudFormation template.',
  routing_probe_inconclusive:
    'The check could not reach a verdict — this is not a permissions failure. Try Re-verify again.',
  role_missing_bedrock_permission:
    'The role can be assumed but is not allowed to invoke Bedrock. Ask the AWS account administrator to add bedrock:InvokeModel and streaming invocation permissions (including inference profiles) to its policy.',
  assume_role_failed: 'The role could not be assumed. Check its trust policy and the external ID.',
  account_unlinked: 'That destination is not linked to this scope’s organization.',
  personal_credential_for_shared_scope:
    'That personal connection needs an explicit organization link for shared Bedrock use. A platform admin can select Use existing AWS connection to verify and link it.',
  connection_not_verified: 'That connection has not been verified yet.',
  connection_not_found: 'That connection no longer exists.',
  // #4746 (§1.4 "admin wins"). Addressed to the person, because on the self-service
  // selector they are who reads it — and it names who can change it, since the honest
  // answer to "how do I fix this?" here is a person, not a control.
  pinned_by_platform_admin:
    'A platform admin has chosen which AWS account serves your Bedrock calls, and that choice takes precedence over your own. Ask a platform admin to change or remove it.',
  scope_not_found: 'That organization, team, or person does not exist.',
  invalid_scope: 'That scope is not one this surface can author.',
  destination_not_found: 'That destination no longer exists.',
  model_not_enabled_in_destination: 'The model is not enabled in the destination account.',
};

/** Prose for a reason code, falling back to the code itself. */
export function describeRoutingReason(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return ROUTING_REASON_COPY[reason] ?? reason;
}
