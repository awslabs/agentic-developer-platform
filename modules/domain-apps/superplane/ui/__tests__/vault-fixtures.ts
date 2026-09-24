/**
 * Vault credential fixtures shaped like the REAL server response — #5730 AC-09.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The provider-connection tests previously built credential rows in the shape the
 * client happened to expect — `{credential_id, service, label}` — and asserted the
 * bind body equalled `{provider, credential_id}`. Both passed. Both were wrong:
 *
 *  - `GET /vault/credentials` returns `CredentialResponse` rows, which have NO
 *    `credential_id` field at all. They carry `id` (the domain registry's own row
 *    key) and `adp_credential_id` (the vault handle). The server matches a
 *    reference against `adp_credential_id`, so a client reading `id` binds a value
 *    the registry lookup cannot find.
 *  - `accept_connection_request` requires `credential_id`, `service` AND `label`,
 *    so the two-field body was a guaranteed 400.
 *
 * A stub written from the client's assumption cannot catch either fault: it agrees
 * with the bug. So the fixture here is derived from the server's Pydantic model and
 * documents its provenance, and `vault-contract.test.ts` reads the model off disk
 * and asserts this fixture still matches it. If the schema changes, that test fails
 * rather than these fixtures quietly describing a server that no longer exists.
 *
 * Field provenance — app/schemas/account.py::CredentialResponse, populated by
 * routers/accounts.py::_credential_to_response:
 *
 *     id                 uuid     registry row key   (NOT the bind reference)
 *     org_id             uuid
 *     name               str      friendly_name      -> the reference `label`
 *     provider           str                         -> the reference `service`
 *     credential_type    str
 *     adp_credential_id  str      vault handle       -> the reference `credential_id`
 *     status             str
 *     created_at         datetime
 *     updated_at         datetime
 */

/** Exactly the fields `CredentialResponse` declares, in its order. */
export const CREDENTIAL_RESPONSE_FIELDS = [
  'id',
  'org_id',
  'name',
  'provider',
  'credential_type',
  'adp_credential_id',
  'status',
  'created_at',
  'updated_at',
] as const;

/**
 * One vault credential row as the server really emits it.
 *
 * `id` and `adp_credential_id` are deliberately DIFFERENT values. When they match,
 * a client reading the wrong one still passes — which is exactly how the original
 * defect survived its tests.
 */
export function vaultCredentialRow(overrides: Record<string, unknown> = {}) {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    org_id: '22222222-2222-4222-8222-222222222222',
    name: 'Prod Bedrock',
    provider: 'bedrock',
    credential_type: 'iam_role',
    adp_credential_id: 'adp-cred-prod-bedrock',
    status: 'Active',
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

/** `CredentialListResponse`: rows plus a total. */
export function vaultCredentialList(rows?: unknown[]) {
  const credentials = rows ?? [
    vaultCredentialRow(),
    vaultCredentialRow({
      id: '33333333-3333-4333-8333-333333333333',
      name: 'Sandbox Bedrock',
      adp_credential_id: 'adp-cred-sandbox-bedrock',
    }),
  ];
  return { credentials, total: credentials.length };
}

/**
 * The reference the server will accept for a given row.
 *
 * Kept here rather than inline in each test so there is one statement of the
 * mapping, and so a test cannot assert a body that only its own author believes in.
 *
 * `service` equals `provider` deliberately and is not a redundancy in the fixture:
 * `provider_connections.py::_registry_reference` raises `CredentialProviderMismatch`
 * when `credential_service != provider`, so any other pairing is a guaranteed 400.
 */
export function expectedBindBody(row: ReturnType<typeof vaultCredentialRow>) {
  return {
    provider: row.provider,
    credential_id: row.adp_credential_id,
    service: row.provider,
    label: row.name,
  };
}

/**
 * A validation reading as `emission.py::validation_response` emits one.
 *
 * Four readings and no aggregate — the separation AC-04 requires exists at the wire
 * boundary, not only in the Python object. `observed_capacity: null` means *not
 * measured* and is distinct from measured-as-zero, which is why it is nullable here.
 */
export const VALIDATION_RESPONSE_FIELDS = [
  'credential_valid',
  'permissions_sufficient',
  'quota_available',
  'observed_capacity',
  'checked_at',
  'detail',
] as const;

export function validationResponse(overrides: Record<string, unknown> = {}) {
  return {
    credential_valid: true,
    permissions_sufficient: true,
    quota_available: true,
    observed_capacity: 4,
    checked_at: new Date().toISOString(),
    detail: '',
    ...overrides,
  };
}

/**
 * A provider connection as `emission.py::connection_response` emits one.
 *
 * Two details a hand-written stub gets wrong, and both matter:
 *
 *  - The nested `credential` block carries the reference field names
 *    (`credential_id`/`service`/`label`) — NOT the vault row's names. The row and
 *    the reference are different shapes, and the client has to map between them.
 *    `credential_id` here is therefore the row's `adp_credential_id`.
 *  - `register_connection` returns **201**, not 200, and the connection is created
 *    PENDING with no `validation` key at all. A fixture that returns 200/Active with
 *    a validation block describes a server state that binding cannot produce.
 */
export function connectionResponse(
  row: ReturnType<typeof vaultCredentialRow>,
  overrides: Record<string, unknown> = {},
) {
  return {
    connection_id: 'conn-1',
    provider: row.provider,
    status: 'Pending',
    workspace_id: 'ws-1',
    credential: {
      credential_id: row.adp_credential_id,
      service: row.provider,
      label: row.name,
    },
    binding: {
      credential_id: row.adp_credential_id,
      workspace_id: 'ws-1',
      bound_by: 'user:admin@example.test',
      bound_at: '2026-09-23T00:00:00+00:00',
    },
    // Fail-closed: a PENDING connection admits no work. The server computes these
    // from the readings it holds, and with no validation report both are false.
    admits_new_work: false,
    allows_renewal: true,
    ...overrides,
  };
}
