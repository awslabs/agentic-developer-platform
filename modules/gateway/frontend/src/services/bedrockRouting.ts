/**
 * API client for the Bedrock account-routing admin surface — Issue #4745 (#4692 · R4).
 *
 * Authors the answer to "whose AWS account is billed for this principal's model calls?"
 * Every function here talks to a platform-admin-only surface: `require_platform_admin`
 * on every route, org admins included (design ruling 4, §6.5).
 *
 * **The UI gate is not the access control** — the same note `personCap.ts` carries, and
 * for a stronger reason. These functions are callable by anyone who can reach the
 * module; an org admin who called one gets a `403`. Hiding the panel keeps admins out
 * of a dead end; it must never be mistaken for the boundary, and widening the UI gate
 * would not widen the authority, only produce a 403 nobody can interpret.
 *
 * Snake_case wire shapes are kept verbatim so the types stay diffable against
 * `src/admin/bedrock_routing/schemas.py`.
 *
 * The `/api` prefix is supplied by `apiClient` and stripped again by CloudFront before
 * the origin, so the paths here are `/admin/bedrock-routing/...`, matching the router's
 * declaration.
 */

import { apiClient, buildQueryString } from './api';
import type {
  DestinationSetupResponse,
  DestinationSummary,
  ExistingAwsConnection,
  EffectiveMappingResponse,
  MappingScope,
  MappingSummary,
  RegisterDestinationRequest,
  RegisterDestinationResponse,
  VerifyDestinationResponse,
} from '@/types/bedrockRouting';

/**
 * Encode a scope as the single path segment the server parses.
 *
 * The wire forms are exactly `org:<org_id>`, `team:<org_id>:<team_id>`, and
 * `user:<user_id>` (`service.parse_scope`); anything else is a `422`. One segment
 * rather than query parameters because the scope IS the identity of the resource being
 * addressed — the same reasoning `personDefaultScopePath` uses.
 *
 * **The team form carries BOTH ids.** A `teams.id` is unique only inside its org
 * (`Team` carries `TenantMixin`), so a team scope naming only the team would be a rule
 * that could route an unrelated tenant's same-id team — the #4344 collision class.
 *
 * **There is no `platform` form.** Rung 4 is the absence of a mapping (§1.2); a
 * `platform` scope string would be a second, contradictory way to say "ambient IRSA",
 * and the server rejects it with `invalid_scope`.
 *
 * Ids are `encodeURIComponent`-escaped individually, before the `:` separators are
 * added — escaping the assembled string would encode the separators the server splits
 * on and turn every scope into a 422.
 *
 * @throws Error when a required id is missing. A local throw rather than a request: the
 *   server would reject it anyway, and a `422` from a client-side mistake surfaces as a
 *   "backend failure" the operator is told to retry — a request that can never succeed.
 */
export function mappingScopePath(scope: MappingScope): string {
  if (scope.scope_type === 'user') {
    if (!scope.user) {
      throw new Error('A user id is required for a user-scoped routing rule.');
    }
    return `user:${encodeURIComponent(scope.user)}`;
  }

  if (!scope.org) {
    throw new Error('An organization id is required for an org- or team-scoped routing rule.');
  }
  if (scope.scope_type === 'org') return `org:${encodeURIComponent(scope.org)}`;

  if (!scope.team) {
    throw new Error('A team id is required for a team-scoped routing rule.');
  }
  return `team:${encodeURIComponent(scope.org)}:${encodeURIComponent(scope.team)}`;
}

/**
 * Every authored routing rule, tightest rung first.
 *
 * Unlike the person-default surface — which has no list route, forcing that panel to
 * disclaim completeness — this IS a complete list. The mapping table is small by
 * construction (one row per governed scope) and platform-wide, so the panel can show
 * the real inventory rather than a set of looked-up scopes.
 *
 * A backend failure raises rather than resolving to `[]`: "the table was unreachable"
 * must never render as "no rules exist", which is the reading that gets a duplicate
 * rule authored over a live one, or a rule "fixed" on the wrong rung.
 */
export async function listMappings(): Promise<MappingSummary[]> {
  return apiClient.get<MappingSummary[]>('/admin/bedrock-routing/mappings');
}

/**
 * Author (or re-point) the rule for one scope. Platform admin only.
 *
 * Takes a `destinationId` — a reference to a registry row — and **never an account
 * id**. That is ruling 4a on the wire: an account number alone is unusable, because the
 * platform needs an assumable role *in* that account, so accepting one would invite a
 * rule that cannot possibly work.
 *
 * **The save runs a real test assume-role, and can legitimately fail** (ruling 4a, §6.7).
 * The server refuses a destination it cannot assume, or that can assume but cannot
 * invoke Bedrock, and stores nothing — the #4511 never-store-inert discipline. Callers
 * must treat a rejection as a normal outcome to display, not an exception to swallow:
 * the `422` body carries `{reason, message}` where `reason` is the same code the
 * runtime path uses (§6.7 item 2).
 *
 * Idempotent: re-authoring replaces the destination on the existing row.
 *
 * **On a user rung this is the admin override.** §1.4 settled on "admin wins", so this
 * write takes precedence over whatever the person chose on their own credentials page,
 * and the effective-mapping display reports it as such. Calling surfaces should say so
 * — silently overriding somebody's own selection is the surprise §1.4 required the UI
 * to disclose.
 */
export async function setMapping(scope: MappingScope, destinationId: string): Promise<MappingSummary> {
  return apiClient.put<MappingSummary>(`/admin/bedrock-routing/mappings/${mappingScopePath(scope)}`, {
    destination_id: destinationId,
  });
}

/**
 * Remove one scope's rule. Platform admin only.
 *
 * **This does not make anybody unroutable.** Removing a rule un-shadows the ladder
 * beneath it: the scope's principals fall back to their team's rule, or their org's, or
 * the platform account. Copy on the calling surface has to say that, because the
 * opposite reading — "their traffic will now fail" — is the natural one and wrong.
 *
 * `204` whether or not a rule existed, so a retried delete is not a failure.
 */
export async function deleteMapping(scope: MappingScope): Promise<void> {
  return apiClient.delete<void>(`/admin/bedrock-routing/mappings/${mappingScopePath(scope)}`);
}

/**
 * "Who serves this person, and from which rule?" (§6.3 element 1.)
 *
 * The source-rung answer is the point, not decoration: showing an effective destination
 * *without* saying which rung produced it invites the reader to "fix" the wrong row —
 * the #4511 discipline applied to a UI, and the labelled-source requirement #4691
 * reached for budget limits.
 *
 * `userId` is the canonical `users.id`. A Cognito sub or a GitHub login gets a `422`
 * with `scope_not_found` rather than a confident "platform" answer — a wrong id that
 * resolved would tell an admin the person has no rule when they may well have one.
 */
export async function getEffectiveMapping(userId: string): Promise<EffectiveMappingResponse> {
  return apiClient.get<EffectiveMappingResponse>(`/admin/bedrock-routing/effective/${encodeURIComponent(userId)}`);
}

/**
 * Registered destinations. Platform admin only.
 *
 * With `orgId`, returns what a rule for that tenant may legitimately name — the
 * tenant's own destinations plus platform-registered ones. **That filter is a usability
 * feature, not the control** (§4.2 requirement 1): the API refuses an out-of-scope
 * destination on save regardless, and a caller who ignores the dropdown gets no further
 * than one who uses it.
 *
 * Unfiltered, it returns everything including unusable rows — which the destinations
 * table needs. Hiding a failed destination would hide the row an operator has to act
 * on; the mockup renders it in red with its reason instead.
 */
export async function listDestinations(orgId?: string): Promise<DestinationSummary[]> {
  const query = buildQueryString({ org_id: orgId });
  return apiClient.get<DestinationSummary[]>(`/admin/bedrock-routing/destinations${query}`);
}

/**
 * Register a destination. Platform admin only.
 *
 * Two sources. `connection` promotes a tenant's existing verified AWS connection — the
 * tenant is read from the credential, so there is no parameter with which to mislabel
 * one. `new_account` is §6.6's flow for an account nobody has linked yet, and returns a
 * **v2-template** quick-create URL: per §5.0b a v1 destination is born pinned to one
 * user id and cannot serve the team/org rules it exists for.
 *
 * **The returned destination is not usable yet** on the `new_account` path — the role
 * does not exist in the destination account until the admin runs the stack. It becomes
 * selectable only once a probe passes.
 */
export async function registerDestination(request: RegisterDestinationRequest): Promise<RegisterDestinationResponse> {
  return apiClient.post<RegisterDestinationResponse>('/admin/bedrock-routing/destinations', request);
}

/**
 * Re-run the verification probe for one destination. Platform admin only (§6.7 item 5).
 *
 * **Always re-probes; never replays a stored verdict.** The caller is asking precisely
 * because they doubt the stored one — §6.7 item 4: a pass is a statement about now, not
 * a permanent guarantee, since the customer can delete the role or rotate its external
 * id afterwards.
 *
 * A failure **un-verifies** the destination so it stops being selectable, and returns
 * the reason. It does **not** delete the rules pointing at it: the resolver skips an
 * unusable destination and walks on (§4.4), so those rules stay meaningful, and
 * deleting an admin's rules on a transient probe failure would be a far larger and
 * irreversible action than they asked for.
 *
 * Resolves — it does not reject — when the probe says "not capable". That is a verdict,
 * not a transport failure; only an unreachable backend rejects.
 */
export async function verifyDestination(destinationId: string): Promise<VerifyDestinationResponse> {
  return apiClient.post<VerifyDestinationResponse>(
    `/admin/bedrock-routing/destinations/${encodeURIComponent(destinationId)}/verify`
  );
}

/** Fresh console link and portable template/parameters for a saved destination. */
export async function getDestinationSetup(destinationId: string): Promise<DestinationSetupResponse> {
  return apiClient.get<DestinationSetupResponse>(`/admin/bedrock-routing/destinations/${encodeURIComponent(destinationId)}/setup`);
}

/** Metadata only. Original AWS credentials remain with their current owners. */
export async function listExistingAwsConnections(): Promise<ExistingAwsConnection[]> {
  return apiClient.get<ExistingAwsConnection[]>('/admin/bedrock-routing/connections');
}

/** Verify the existing role, then grant an organization Bedrock use of it. */
export async function linkAwsConnection(credentialId: string, orgId: string): Promise<RegisterDestinationResponse> {
  return apiClient.post<RegisterDestinationResponse>('/admin/bedrock-routing/connection-links', {
    source: 'shared_connection', credential_id: credentialId, link_to_org_id: orgId,
  });
}

/** Refuses in-use links; never deletes the source connection or AWS role. */
export async function unlinkAwsConnection(destinationId: string): Promise<void> {
  return apiClient.delete<void>(`/admin/bedrock-routing/connection-links/${encodeURIComponent(destinationId)}`);
}
