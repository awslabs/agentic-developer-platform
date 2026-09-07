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
export interface DestinationSummary {
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
    'The IAM role is pinned to the person who created it, so it cannot serve other people. Re-run the routing CloudFormation template in that account.',
  routing_probe_inconclusive:
    'The check could not reach a verdict — this is not a permissions failure. Try Re-verify again.',
  role_missing_bedrock_permission:
    'The role can be assumed but is not allowed to invoke Bedrock. Add bedrock:InvokeModel (including inference profiles) to its policy.',
  assume_role_failed: 'The role could not be assumed. Check its trust policy and the external ID.',
  account_unlinked: 'That destination is not linked to this scope’s organization.',
  personal_credential_for_shared_scope:
    'That connection belongs to one person, so it cannot serve a whole team or organization. Register the account as a platform destination instead.',
  connection_not_verified: 'That connection has not been verified yet.',
  connection_not_found: 'That connection no longer exists.',
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
