/**
 * Organization identifier derivation — Issue #4841 (#4839 · T2a).
 *
 * The canonical org-create route (`POST /api/admin/identity/organizations`) requires a
 * **caller-supplied `id`** (`src/admin/identity/schemas.py`: `id` is a required field,
 * 1–255 chars). Ruling D4=A settled that this panel targets that route, which makes the
 * identifier a real UX decision rather than a hidden field: the operator has to produce a
 * value, and the value is permanent.
 *
 * This module implements the "derive from name, then confirm" affordance the UI contract
 * (`docs/mockups/4839-tenancy-admin.html`) specifies — a suggestion the admin can edit,
 * never a silent auto-assignment. Two reasons it is a suggestion and not a computation:
 * the id is immutable after create, and it is the segment that appears in every routing
 * scope string for the org (`org:<org_id>` — see `bedrockRouting.ts`), so an admin who
 * wanted `acme` and silently got `acme-corp-inc` is stuck with it.
 *
 * **This does not change id semantics.** The slug-vs-UUID question is explicitly deferred
 * to the Wave 2 design pass as a C9 input (#4841 Non-goals). The server accepts any
 * non-empty string ≤255 chars; this is a client-side *suggestion* generator for the
 * default value of an editable field, not a new constraint. `isValidOrgIdentifier` below
 * deliberately mirrors what the server would accept rather than inventing a stricter rule
 * — a client that rejected a value the server allows would be a second, undocumented
 * schema.
 */

/** Max length the server's `OrganizationCreateRequest.id` field accepts. */
export const ORG_IDENTIFIER_MAX_LENGTH = 255;

/**
 * Suggest a URL-safe identifier from a display name.
 *
 * Lowercases, strips accents so "Björn Industries" yields `bjorn-industries` rather than
 * dropping the character entirely, collapses every run of non-alphanumerics to a single
 * hyphen, and trims leading/trailing hyphens. Returns `''` for a name with no usable
 * characters (e.g. only punctuation, or only CJK text, which has no ASCII form to derive)
 * — the caller renders that as "type an identifier" rather than submitting an empty id the
 * server would reject with a bare 422.
 *
 * Truncation trims a trailing hyphen so a name cut mid-word cannot produce `foo-bar-`.
 */
export function deriveOrgIdentifier(name: string): string {
  const slug = name
    .normalize('NFKD')
    // Strip combining marks left behind by NFKD (é -> e + U+0301). Written as escapes,
    // not literal combining characters, which are invisible in a diff.
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');

  if (slug.length <= ORG_IDENTIFIER_MAX_LENGTH) return slug;
  return slug.slice(0, ORG_IDENTIFIER_MAX_LENGTH).replace(/-+$/, '');
}

/**
 * Whether a typed identifier is submittable.
 *
 * Mirrors the server's own rule (non-empty, ≤255) and nothing more. In particular it does
 * NOT require the derived slug's shape: an admin who deliberately types `Acme_Corp` is
 * naming their own tenant in a form the server stores as given, and refusing it here would
 * be this panel inventing id semantics the deferred C9 pass owns.
 */
export function isValidOrgIdentifier(id: string): boolean {
  const trimmed = id.trim();
  return trimmed.length > 0 && trimmed.length <= ORG_IDENTIFIER_MAX_LENGTH;
}
