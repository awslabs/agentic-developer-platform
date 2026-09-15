/**
 * API client for the self-service Bedrock account selector — Issue #4746 (#4692 · R5).
 *
 * The person-facing half of what `bedrockRouting.ts` does for platform admins: pointing
 * the caller's **own** Bedrock traffic at one of their **own** connected AWS accounts
 * (design note §6.4).
 *
 * **A separate module from `bedrockRouting.ts`, deliberately.** That file's docstring
 * declares every function in it platform-admin-only, which is true of every route on
 * `routes.py`. These three talk to a surface an ordinary member is *meant* to reach.
 * Mixing them would make that file's central claim false, and a reader who trusted it
 * would draw the wrong conclusion about whichever function they happened to open — the
 * same reason `me_routes` is not a route on `budget/routes.py` server-side.
 *
 * **No function here takes a target, at any position.** Not a person, not a scope, not a
 * destination id. The anchor is derived from the caller's token server-side, so there is
 * nothing for a caller to pass and nothing for a component to accidentally pass — the
 * `personCap.ts` argument for `getMyPersonCap`, and the property `self_routes.py` is
 * built around. A target parameter reachable from a self-service component is how
 * authority leaks; the absence of one is why it cannot.
 *
 * Snake_case wire shapes are kept verbatim so the types stay diffable against
 * `src/admin/bedrock_routing/schemas.py`.
 *
 * The `/api` prefix is supplied by `apiClient` and stripped again by CloudFront before
 * the origin, so the paths here are `/me/bedrock-routing/...`, matching the router's
 * declaration.
 */

import { apiClient } from './api';
import type { MySelectionResponse } from '@/types/bedrockRouting';

const SELECTION_PATH = '/me/bedrock-routing/selection';

/**
 * Which AWS account serves the caller's Bedrock calls, and which of theirs they may pick.
 *
 * **Takes no argument.** The caller always gets their own answer.
 *
 * Returns the **effective** destination, not merely the row the caller stored — §6.4:
 * *"showing the user's own stale pick as active is the inert-config defect."* Read
 * `own_selection_active` for whether their choice is what is in force; it is false both
 * when an admin has pinned them (§1.4) and when their own pick has silently stopped being
 * usable (§4.4), and those need different copy.
 *
 * A backend failure **raises** rather than resolving to an unconfigured-looking shape.
 * "The lookup was unreachable" must never render as "your calls go to the platform
 * account", because that is a specific, plausible, wrong answer about who is billed.
 */
export async function getMySelection(): Promise<MySelectionResponse> {
  return apiClient.get<MySelectionResponse>(SELECTION_PATH);
}

/**
 * Point the caller's own Bedrock traffic at one of their own connected AWS accounts.
 *
 * Takes a **credential id** — one of the caller's own connections, as listed by
 * {@link getMySelection}. Not a destination id: a person owns connections, not registry
 * rows. The server looks the id up scoped to the caller, so another person's connection
 * id does not resolve at all.
 *
 * **The save runs a real assume-role + Bedrock probe and can legitimately be refused**
 * (§6.7, the #4511 never-store-inert discipline). A `422` carries `{reason, message}`
 * with a code shared by the runtime path, and callers must display it rather than
 * swallow it — the refusal *is* the feature working: an account that cannot serve a call
 * has not become a rule that fails one.
 *
 * The refusals worth branching on: `connection_not_found` (not the caller's),
 * `connection_not_verified` (§4.4, the stack has not finished), `pinned_by_platform_admin`
 * (§1.4 — an admin's choice governs and this write would reverse it), and the probe codes.
 *
 * Idempotent: re-selecting the same account re-points the existing row.
 *
 * Resolves with the full selection payload, so a caller re-renders from the server's own
 * answer. That matters beyond tidiness — a save can succeed while something else still
 * governs, which an optimistic local update would get wrong.
 */
export async function setMySelection(credentialId: string): Promise<MySelectionResponse> {
  return apiClient.put<MySelectionResponse>(SELECTION_PATH, { credential_id: credentialId });
}

/**
 * Stop routing the caller's Bedrock calls to their own account.
 *
 * **This does not make the caller unroutable.** It un-shadows the ladder beneath their
 * row: their traffic falls to their team's rule, then their org's, then the platform
 * account. Copy on the calling surface has to say that, because the opposite reading —
 * "my calls will now fail" — is the natural one and is wrong.
 *
 * Resolves with the new effective destination rather than nothing, which is why it is not
 * a `void`: "you are back on your team's account" and "you are back on the platform's"
 * are different outcomes and the person should not have to guess which happened.
 *
 * **Refused with `pinned_by_platform_admin` when an admin has pinned the caller.** The
 * pinned row is the admin's, not theirs, and clearing it would be the same override
 * reversal a `PUT` is refused for.
 *
 * Idempotent when nothing is stored — the outcome asked for already holds.
 */
export async function clearMySelection(): Promise<MySelectionResponse> {
  return apiClient.delete<MySelectionResponse>(SELECTION_PATH);
}
