/**
 * Provider connection management — #5730 AC-03.
 *
 * Driven through MSW at the real HTTP boundary, because the properties under test
 * are properties of what crosses the wire: which body is sent, what is done with
 * the reply, and above all what is *never* present. Mocking the client would make
 * the secret-absence assertions vacuous — they would be checking a mock's
 * arguments rather than a request.
 *
 * The central assertion of this file is a negative one. Every request the panel
 * issues is captured and checked for secret material, and the rendered DOM plus
 * browser storage are checked too. A negative assertion can pass for the wrong
 * reason (nothing happened at all), so each is paired with a positive control
 * proving the action really occurred.
 *
 * Those DOM-level checks are necessary but NOT sufficient, and the last describe
 * block exists because of it: a secret can sit in component state while never
 * being rendered or re-sent, and no assertion here would notice. See that block
 * for the leak this file originally missed and how it is now pinned.
 */

import { HttpResponse, http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { server } from '@/mocks/server';
import { ProviderConnectionPanel } from '@superplane-ui/ProviderConnectionPanel';
import { parseCredentialList, parseCredentialRef } from '@superplane-ui/client';
import {
  DOMAIN_BASE,
  SECRET_MATERIAL_FIELDS,
  assertNoSecretMaterial,
} from '@superplane-ui/contract';

import {
  connectionResponse,
  expectedBindBody,
  vaultCredentialList,
  vaultCredentialRow,
  validationResponse,
} from './vault-fixtures';

const API = (path: string) => `/api${DOMAIN_BASE}${path}`;
const WORKSPACE = 'ws-1';
const CONNECTIONS = API(`/workspaces/${WORKSPACE}/provider-connections`);
const CONNECTION = `${CONNECTIONS}/conn-1`;

/**
 * A realistic secret, used to prove the panel cannot leak one.
 *
 * Deliberately not a plausible-looking real credential: it is a fixed marker
 * string whose only purpose is to be searched for. If it ever appears in a
 * request, the DOM or storage, the test that looks for it fails.
 */
const SECRET_MARKER = 'tripwire-not-a-real-secret-value';

/** Every request the panel made, recorded for after-the-fact inspection. */
let sent: Array<{ method: string; url: string; body: unknown }> = [];

beforeEach(() => {
  sent = [];
  window.localStorage.clear();
  window.sessionStorage.clear();
  server.events.removeAllListeners();
  server.events.on('request:start', async ({ request }) => {
    // Cloned because reading the body consumes it; the handler still needs it.
    const clone = request.clone();
    let body: unknown = null;
    try {
      body = await clone.json();
    } catch {
      body = null;
    }
    sent.push({ method: request.method, url: request.url, body });
  });
});

afterEach(() => {
  server.events.removeAllListeners();
  vi.restoreAllMocks();
});

/**
 * The row the panel binds, in the shape `GET /vault/credentials` really returns.
 *
 * Its `id` and `adp_credential_id` differ, which is what makes these tests able to
 * fail. The previous fixtures were `{credential_id, service, label}` — the shape
 * the client assumed — so a client reading the wrong field still passed. See
 * vault-fixtures.ts.
 */
const ROW = vaultCredentialRow();

function credentialRows(rows?: unknown[]) {
  server.use(
    http.get(API('/vault/credentials'), () => HttpResponse.json(vaultCredentialList(rows))),
    http.get('/api/auth/credentials', () => HttpResponse.json(vaultCredentialList(rows).credentials.map((raw) => {
      const row = raw as ReturnType<typeof vaultCredentialRow>;
      return { id: row.adp_credential_id, service: row.provider, label: row.name };
    }))),
    http.put('/api/auth/credentials/:credential/workspaces/:workspace', () => HttpResponse.json({ delegated: true })),
  );
}

/** 201 and PENDING, as `register_connection` actually answers. */
function connectionBody(overrides: Record<string, unknown> = {}) {
  return connectionResponse(ROW, overrides);
}

function bindHandler(overrides: Record<string, unknown> = {}) {
  return http.post(CONNECTIONS, () =>
    HttpResponse.json(connectionBody(overrides), { status: 201 }),
  );
}

function renderPanel(props: Partial<Parameters<typeof ProviderConnectionPanel>[0]> = {}) {
  return render(
    <ProviderConnectionPanel
      workspaceId={WORKSPACE}
      providers={['bedrock', 'anthropic']}
      mayManage
      {...props}
    />,
  );
}

/**
 * Pick a credential and bind it.
 *
 * There is no provider step, and its absence is deliberate: the server requires the
 * connection's provider to equal the credential's own service, so a second picker
 * only lets a user assemble a pair the server always refuses. The option value is
 * the vault handle, because that is the reference the registry lookup matches.
 */
async function bindCredential(
  user: ReturnType<typeof userEvent.setup>,
  credentialId: string = ROW.adp_credential_id,
) {
  await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
  await screen.findByLabelText('Vault credential');
  await user.selectOptions(screen.getByLabelText('Vault credential'), credentialId);
  await user.click(screen.getByRole('button', { name: /bind credential to workspace/i }));
}

describe('AC-03: binding a vault credential', () => {
  it('sends the complete reference the server requires', async () => {
    credentialRows();
    server.use(bindHandler());
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    const bind = sent.find((request) => request.method === 'POST');
    expect(bind).toBeDefined();
    // The exact body, asserted as a whole rather than field-by-field: an extra
    // field is precisely what this must catch, and a subset check would not.
    //
    // Expected value comes from the fixture's own mapping rather than being typed
    // here. Typing it was the original defect: this asserted
    // `{provider, credential_id}` and passed, while `accept_connection_request`
    // requires `credential_id`, `service` AND `label` and 400s on a body without
    // them. A test that states the body it wants cannot notice the server wanting
    // a different one.
    expect(bind?.body).toEqual(expectedBindBody(ROW));
  });

  it('references the vault handle, not the registry row key', async () => {
    // The decisive mapping, pinned on its own because getting it wrong is silent:
    // `_registry_reference` matches `CredentialRegistry.adp_credential_id`, so a
    // client that sends the row's `id` gets "credential is not registered" for a
    // credential plainly visible in the picker. Both ids are present in the row and
    // only one is correct, which is why the fixture keeps them different.
    credentialRows();
    server.use(bindHandler());
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    const bind = sent.find((request) => request.method === 'POST');
    expect((bind?.body as { credential_id: string }).credential_id).toBe(ROW.adp_credential_id);
    expect(JSON.stringify(bind?.body)).not.toContain(ROW.id);
  });

  it('derives the provider from the credential instead of asking twice', async () => {
    // `_registry_reference` raises CredentialProviderMismatch when the submitted
    // provider differs from the credential's own service, and the refusal names
    // neither field. Two independent pickers therefore let a user build a
    // guaranteed-400 pair with no way to see why. One picker makes it
    // unrepresentable.
    credentialRows();
    server.use(bindHandler());
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await screen.findByLabelText('Vault credential');
    // There is no provider input to disagree with the credential.
    expect(screen.queryByLabelText('Provider')).toBeNull();

    await user.selectOptions(screen.getByLabelText('Vault credential'), ROW.adp_credential_id);
    // It is shown, read-only, so the user still knows what they are binding.
    expect(screen.getByText(ROW.provider)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /bind credential to workspace/i }));
    await screen.findByRole('heading', { name: /bound credential/i });

    const bind = sent.find((request) => request.method === 'POST');
    const body = bind?.body as { provider: string; service: string };
    expect(body.provider).toBe(ROW.provider);
    expect(body.service).toBe(body.provider);
  });

  it('warns when the credential\'s provider is not one this environment lists', async () => {
    // Fail-loud rather than fail-closed: the capability report may simply be
    // incomplete, so the bind is not blocked — but the user is told before they
    // spend a request finding out.
    credentialRows([vaultCredentialRow({ provider: 'unlisted-provider' })]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await screen.findByLabelText('Vault credential');
    await user.selectOptions(screen.getByLabelText('Vault credential'), ROW.adp_credential_id);

    expect(
      screen.getByText(/does not list unlisted-provider as a\s+supported provider family/i),
    ).toBeInTheDocument();
  });

  it('puts no secret material in any request, at any depth', async () => {
    // The server volunteers secret-looking fields. A client that spread the
    // response into its state could echo them back on a later request; this
    // asserts nothing of the sort reaches the wire.
    credentialRows([
      { ...vaultCredentialRow(), label: ROW.name,
        secret_value: SECRET_MARKER,
        nested: { api_key: SECRET_MARKER },
      },
    ]);
    server.use(bindHandler({ token: SECRET_MARKER }));
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    // Positive control: without it this passes when no request was made at all.
    // One POST, not two — the panel issues no validation call, so the control is
    // the bind alone. See the validation describe block for why.
    expect(sent.filter((request) => request.method === 'POST')).toHaveLength(1);
    for (const request of sent) {
      assertNoSecretMaterial(request.body, `${request.method} ${request.url}`);
      expect(JSON.stringify(request.body ?? {})).not.toContain(SECRET_MARKER);
      // The URL matters as much as the body: a secret in a query string lands in
      // access logs and browser history.
      expect(request.url).not.toContain(SECRET_MARKER);
    }
  });

  it('renders no secret material and stores none, even when the server sends it', async () => {
    credentialRows([vaultCredentialRow({ secret: SECRET_MARKER })]);
    server.use(bindHandler({ private_key: SECRET_MARKER }));
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    // Positive control: the bind is visibly complete, so the absences below are
    // absences from a rendered screen and not from an empty one.
    await screen.findByRole('heading', { name: /bound credential/i });
    expect(screen.getByText(ROW.name)).toBeInTheDocument();

    expect(document.body.textContent ?? '').not.toContain(SECRET_MARKER);
    // Every key, not one guessed name: the panel must not have written a secret
    // anywhere in either store.
    for (const store of [window.localStorage, window.sessionStorage]) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index) ?? '';
        expect(key).not.toContain(SECRET_MARKER);
        expect(store.getItem(key) ?? '').not.toContain(SECRET_MARKER);
      }
    }
  });

  it('offers credentials by label without exposing the id as the visible name', async () => {
    credentialRows();
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    const select = await screen.findByLabelText('Vault credential');

    expect(select).toHaveDisplayValue('Select a credential');
    // `name` is the registry's `friendly_name`, which is what an admin recognises.
    expect(screen.getByRole('option', { name: `${ROW.name} (${ROW.provider})` })).toBeInTheDocument();
    // The option's value is the vault handle, and neither id is on screen as a label.
    expect(screen.getByRole('option', { name: `${ROW.name} (${ROW.provider})` })).toHaveValue(
      ROW.adp_credential_id,
    );
    expect(document.body.textContent ?? '').not.toContain(ROW.id);
  });

  it('keeps bind disabled until a credential is chosen', async () => {
    // Guards the fail-closed direction: submitting half a form produces a 400
    // the user cannot act on.
    credentialRows();
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await screen.findByLabelText('Vault credential');
    const bind = screen.getByRole('button', { name: /bind credential to workspace/i });
    expect(bind).toBeDisabled();

    await user.selectOptions(screen.getByLabelText('Vault credential'), ROW.adp_credential_id);
    expect(bind).toBeEnabled();
  });

  it('stays disabled for a selection that resolved to no row', async () => {
    // The enabled state and the submitted body are derived from the SAME lookup,
    // so the button cannot be live while the body would have nothing to build from.
    credentialRows([vaultCredentialRow({ adp_credential_id: '' })]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    // A row with no vault handle is unbindable, so it is dropped entirely rather
    // than offered as an option that fails on submit.
    expect(await screen.findByText(/no vault credentials available/i)).toBeInTheDocument();
  });

  it('drops a malformed credential row instead of blanking the whole list', async () => {
    // Blanking would read as "you have no credentials" and invite the user to
    // create a duplicate of one that already exists.
    const good = vaultCredentialRow({
      name: 'Sandbox Bedrock',
      adp_credential_id: 'adp-cred-sandbox-bedrock',
    });
    credentialRows([
      // Has the registry key but no vault handle: the field the server matches on
      // is missing, so there is nothing bindable to offer.
      { ...vaultCredentialRow({ name: 'Missing its handle' }), adp_credential_id: undefined },
      good,
    ]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    await screen.findByLabelText('Vault credential');

    expect(
      screen.getByRole('option', { name: `${good.name} (${good.provider})` }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /missing its handle/i })).toBeNull();
  });

  it('says so when the vault has no credentials rather than showing an empty picker', async () => {
    credentialRows([]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));
    expect(await screen.findByText(/no vault credentials available/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('Vault credential')).toBeNull();
  });
});

describe('AC-03: validation is the service\'s answer, never the client\'s', () => {
  /**
   * WHAT THIS BLOCK USED TO ASSERT, AND WHY IT WAS WRONG
   * ---------------------------------------------------
   * Every test here previously clicked a "Validate with provider" button and read
   * the readings out of a stubbed 200 from `POST .../validation`. All of them
   * passed. The button could not work:
   *
   *  - That route RECORDS a reading an attesting service already produced. Its
   *    handler defaults every unsupplied reading to `False`, so the empty body the
   *    button sent was not a request for a check — it was a complete four-way
   *    FAILING report filed against the user's own credential, which
   *    `record_failed_validation` then persists, demoting ACTIVE to PENDING.
   *  - `_vault_evidence` additionally requires the vault to have attested the
   *    sha256 of the exact readings submitted, so a browser cannot satisfy it at
   *    all. The real reply would have been 403, never the stub's 200.
   *
   * The stub was what hid both facts: it answered a call the server would refuse,
   * with a shape the server would never send. So the readings are now exercised
   * through the served GET, whose body really can contain them, and the absence of
   * any probe is asserted rather than assumed.
   */
  beforeEach(() => {
    credentialRows();
    server.use(bindHandler());
  });

  it('never posts to the validation route, because doing so files a false failure', async () => {
    // No MSW handler is registered for the validation path anywhere in this block.
    // setup.ts runs with `onUnhandledRequest: 'error'`, so a probe would fail this
    // test loudly rather than pass on a stub. Belt and braces, the recorded
    // requests are checked too.
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));
    await waitFor(() => expect(screen.getByRole('heading', { name: /bound credential/i })).toBeInTheDocument());

    expect(sent.some((request) => request.url.endsWith('/validation'))).toBe(false);
    // Positive control: the refresh really did happen, as a GET of the connection.
    expect(sent.some((request) => request.method === 'GET' && request.url.endsWith('/conn-1'))).toBe(
      true,
    );
  });

  it('offers Gateway validation to credential managers', async () => {
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    expect(screen.getByRole('button', { name: /validate credential/i })).toBeInTheDocument();
    // And says why, in terms of who does the checking — with no story number.
    expect(document.body.textContent ?? '').not.toMatch(/#\d+/);
  });

  it('does not claim the credential is valid just because binding succeeded', async () => {
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    // The bind returned 201 PENDING with no validation. That is "not validated
    // yet", not "valid" — a credential can bind and still be unusable. This is the
    // server's real answer shape, not a stub's: `service.register` creates the row
    // PENDING and `connection_response` omits `validation` when there is none.
    expect(screen.getByText(/has not been validated by the service yet/i)).toBeInTheDocument();
    expect(screen.getByText('Admits new work').parentElement?.textContent).toContain(
      'Unknown; refresh validation',
    );
  });

  it('reports each of the three readings separately, including not-reported', async () => {
    // A provider that answered two questions and not the third. Collapsing this
    // into one verdict is what AC-04 forbids; "not reported" is its own answer.
    //
    // The reading is delivered the way the server really delivers one — inside the
    // connection body, keyed `validation`, in `validation_response`'s field shape.
    server.use(
      http.get(CONNECTION, () =>
        HttpResponse.json(
          connectionBody({
            status: 'Pending',
            validation: validationResponse({
              permissions_sufficient: false,
              quota_available: false,
              observed_capacity: null,
              detail: 'iam:PassRole missing',
            }),
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));

    const valid = await screen.findByText('Credential valid');
    expect(valid.parentElement?.textContent).toContain('Yes');
    expect(screen.getByText('Permissions sufficient').parentElement?.textContent).toContain('No');
    expect(screen.getByText('Quota available').parentElement?.textContent).toContain('No');
    // No aggregate verdict anywhere: three readings are three facts.
    expect(screen.queryByText(/^(?:valid|ready|ok)$/i)).toBeNull();
  });

  it('renders an unmeasured capacity as unmeasured, not as zero', async () => {
    // `observed_capacity: null` means "we did not look"; `0` means "we looked and
    // there is nothing free". The server keeps them distinct deliberately
    // (`validation_response` never coerces null to 0), so the screen must too —
    // one invites waiting, the other invites raising a quota request.
    server.use(
      http.get(CONNECTION, () =>
        HttpResponse.json(
          connectionBody({ validation: validationResponse({ observed_capacity: null }) }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));

    await screen.findByText('Credential valid');
    expect(screen.queryByText('Observed capacity')).toBeNull();
  });

  it('clears a previous reading when a later read fails', async () => {
    // The dangerous case: a stale "valid" outliving the evidence for it. A failed
    // *request* says nothing about the credential, so the reading must go.
    let calls = 0;
    server.use(
      http.get(CONNECTION, () => {
        calls += 1;
        if (calls === 1) {
          return HttpResponse.json(
            connectionBody({ validation: validationResponse({ observed_capacity: 2 }) }),
          );
        }
        return new HttpResponse(null, { status: 503 });
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });

    const check = screen.getByRole('button', { name: /check for a new reading/i });
    await user.click(check);
    expect(await screen.findByText('Credential valid')).toBeInTheDocument();

    await user.click(check);
    await waitFor(() =>
      expect(screen.getByText(/has not been validated by the service yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText('Credential valid')).toBeNull();
  });

  it('distinguishes a permission refusal from a service failure', async () => {
    server.use(http.get(CONNECTION, () => new HttpResponse(null, { status: 403 })));
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));

    // 403 is an authority problem: a retry cannot fix it, so it must not read as
    // a transient failure.
    expect(await screen.findByText(/do not have access/i)).toBeInTheDocument();
  });

  it('treats a reading that answers nothing as unknown, never as valid', async () => {
    // A `validation` block with none of the three readings. The fail-closed
    // requirement is only that this must not read as approval; each reading
    // therefore comes back "Not reported", which is the truthful rendering of an
    // answer that contained no answers.
    server.use(
      http.get(CONNECTION, () => HttpResponse.json(connectionBody({ validation: {} }))),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));

    await screen.findByText(/last validation is stale/i);
    expect(screen.getByText('Admits new work').parentElement?.textContent).toContain('Unknown');
    // Nothing anywhere on the screen claims a working credential.
    expect(document.body.textContent ?? '').not.toMatch(/\bYes\b/);
  });

  it('does not upgrade the screen past what the server says', async () => {
    // The server owns the status and `admits_new_work`; both are recomputed from
    // the readings it holds. A client that inferred "Active" from a passing reading
    // would show a workspace as able to take work while the server refuses it.
    server.use(
      http.get(CONNECTION, () =>
        HttpResponse.json(
          connectionBody({
            status: 'Pending',
            admits_new_work: false,
            validation: validationResponse(),
          }),
        ),
      ),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));

    await screen.findByText('Credential valid');
    expect(screen.getByText('Status').parentElement?.textContent).toContain('Pending');
    expect(screen.getByText('Admits new work').parentElement?.textContent).toContain(
      'No, or not reported',
    );
  });
});

describe('AC-03: revoking a connection', () => {
  beforeEach(() => {
    credentialRows();
    server.use(bindHandler());
  });

  it('clears the binding and its readings together', async () => {
    server.use(
      http.get(CONNECTION, () =>
        HttpResponse.json(
          connectionBody({ validation: validationResponse({ observed_capacity: 1 }) }),
        ),
      ),
      http.delete(CONNECTION, () => new HttpResponse(null, { status: 204 })),
    );
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /check for a new reading/i }));
    expect(await screen.findByText('Credential valid')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /revoke connection/i }));

    expect(await screen.findByText(/connection revoked/i)).toBeInTheDocument();
    // Both gone. A reading left on screen would assert validity for a binding
    // that no longer exists.
    expect(screen.queryByRole('heading', { name: /bound credential/i })).toBeNull();
    expect(screen.queryByText('Credential valid')).toBeNull();
  });

  it('says the vault credential itself is untouched', async () => {
    // Otherwise revoke reads as "delete my credential", which stops people from
    // using it and leaves stale bindings in place.
    server.use(http.delete(CONNECTION, () => new HttpResponse(null, { status: 204 })));
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /revoke connection/i }));

    expect(await screen.findByText(/untouched in your vault/i)).toBeInTheDocument();
  });

  it('keeps the binding on screen when the revoke fails', async () => {
    // Showing it as revoked would be a lie that leaves a live binding invisible.
    server.use(http.delete(CONNECTION, () => new HttpResponse(null, { status: 500 })));
    const user = userEvent.setup();
    renderPanel();

    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    await user.click(screen.getByRole('button', { name: /revoke connection/i }));

    await waitFor(() => expect(screen.getByText(/something went wrong/i)).toBeInTheDocument());
    expect(screen.getByRole('heading', { name: /bound credential/i })).toBeInTheDocument();
    expect(screen.queryByText(/connection revoked/i)).toBeNull();
  });

  it('hides revoke from a user who may not manage but still shows the reading', async () => {
    // Read-only users need the state; they must not be offered the mutation.
    const user = userEvent.setup();
    const { unmount } = renderPanel();
    await bindCredential(user);
    await screen.findByRole('heading', { name: /bound credential/i });
    unmount();

    renderPanel({ mayManage: false });
    expect(screen.queryByRole('button', { name: /revoke connection/i })).toBeNull();
  });
});

describe('AC-01: what a read-only user sees', () => {
  it('shows no mutating control but still explains the situation', async () => {
    renderPanel({ mayManage: false });

    expect(screen.queryByRole('button', { name: /choose a vault credential/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /bind credential/i })).toBeNull();
    expect(
      screen.getByText(/requires an organization or platform administrator/i),
    ).toBeInTheDocument();
  });
});

describe('honest reporting of what this screen cannot know', () => {
  it('says connections from earlier sessions are not listed', async () => {
    // The API serves no list route. Silence here would read as "nothing is
    // bound", and the user would bind a duplicate.
    renderPanel();
    expect(screen.getByText(/bound in an earlier session are not listed/i)).toBeInTheDocument();
  });

  it('says the supported provider list is unknown when capabilities could not be read', async () => {
    renderPanel({
      providers: [],
      capabilityUnavailable: {
        reason: 'not-deployed',
        detail: 'This environment does not report supported providers yet.',
      },
    });

    expect(screen.getByText(/supported providers are not known yet/i)).toBeInTheDocument();
    // And no story number in the remediation the user reads.
    expect(document.body.textContent ?? '').not.toMatch(/#\d+/);
  });

  it('reports a vault read failure instead of an empty credential list', async () => {
    server.use(http.get(API('/vault/credentials'), () => new HttpResponse(null, { status: 502 })));
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole('button', { name: /choose a vault credential/i }));

    // 502 is a reachability problem, and is titled as one — distinct from the
    // generic failure title, so the user knows a retry is worth attempting.
    expect(await screen.findByText(/could not reach superplane/i)).toBeInTheDocument();
    expect(screen.queryByText(/no vault credentials available/i)).toBeNull();
  });
});

describe('the parser is the barrier, not the renderer', () => {
  /**
   * These assert at the parse boundary rather than through the DOM, and that is
   * the point of them.
   *
   * The DOM/request/storage tests above miss one real leak: if the parser spread
   * the server's response, a secret would sit in component state while never
   * being rendered or re-sent. Nothing a user sees would change — but React
   * DevTools, a serialized error report and any future telemetry that walks
   * component state would all carry it. I verified this gap by making the parser
   * spread its input and confirming every test above still passed.
   *
   * So the guarantee has to be pinned where it actually lives: the parser
   * returns exactly its named fields and nothing else.
   */
  it('returns exactly the three reference fields and drops everything else', () => {
    // Input is a REAL vault row plus volunteered secret-looking fields. A row in
    // the client's preferred shape would prove nothing about the mapping.
    const parsed = parseCredentialRef(
      { ...vaultCredentialRow(), label: ROW.name,
        secret_value: SECRET_MARKER,
        api_key: SECRET_MARKER,
        nested: { token: SECRET_MARKER },
      },
    );

    expect(parsed).toEqual({
      credential_id: ROW.adp_credential_id,
      service: ROW.provider,
      label: ROW.name,
    });
    // The registry row key is dropped, not used as the reference.
    expect(JSON.stringify(parsed)).not.toContain(ROW.id);
    // Key-exact, so a future field addition is a deliberate decision rather than
    // something that arrives by spread.
    expect(Object.keys(parsed ?? {}).sort()).toEqual(['credential_id', 'label', 'service']);
    expect(JSON.stringify(parsed)).not.toContain(SECRET_MARKER);
  });

  it('drops secret-looking fields from every row of a list', () => {
    const parsed = parseCredentialList(
      vaultCredentialList([
        vaultCredentialRow({ name: 'One', label: 'One', password: SECRET_MARKER }),
        vaultCredentialRow({
          name: 'Two', label: 'Two',
          adp_credential_id: 'adp-cred-two',
          access_token: SECRET_MARKER,
        }),
      ]),
    );

    expect(parsed).toHaveLength(2);
    expect(JSON.stringify(parsed)).not.toContain(SECRET_MARKER);
    for (const credential of parsed ?? []) {
      expect(() => assertNoSecretMaterial(credential, 'parsed credential')).not.toThrow();
    }
  });

  it('rejects a row with no vault handle rather than falling back to the row key', () => {
    // The fallback this pins the absence of was the actual defect: reading
    // `id` when `adp_credential_id` was missing produced a reference the
    // registry lookup cannot match, so the bind failed with "not registered" for
    // a credential the user could see in the picker. Dropping the row surfaces the
    // gap at the vault instead of at a confusing 400.
    const withoutHandle: Record<string, unknown> = vaultCredentialRow();
    delete withoutHandle.adp_credential_id;
    expect(parseCredentialRef(withoutHandle)).toBeNull();

    expect(parseCredentialRef(vaultCredentialRow({ adp_credential_id: '' }))).toBeNull();
    // A row with no provider or no name is equally unbindable: the server requires
    // all three reference fields, so a partial row cannot become a partial request.
    expect(parseCredentialRef(vaultCredentialRow({ provider: '' }))).toBeNull();
    expect(parseCredentialRef(vaultCredentialRow({ name: '' }))).toBeNull();
    expect(parseCredentialRef('not an object')).toBeNull();
  });

  it('reports a non-list response as unreadable instead of as an empty vault', () => {
    // "No credentials" and "the reply made no sense" must not look the same: the
    // first invites creating one, the second is a client/server mismatch.
    expect(parseCredentialList({ credentials: 'nope' })).toBeNull();
    expect(parseCredentialList(null)).toBeNull();
    expect(parseCredentialList([])).toEqual([]);
  });
});

describe('the secret-material tripwire itself', () => {
  // A test whose absence would make every assertion above worthless: if
  // assertNoSecretMaterial could not detect a secret, it would pass on anything.
  it('detects each named field, at the top level and nested', () => {
    for (const field of SECRET_MATERIAL_FIELDS) {
      expect(() => assertNoSecretMaterial({ [field]: 'x' }, 'body')).toThrow(field);
      expect(() => assertNoSecretMaterial({ a: { b: [{ [field]: 'x' }] } }, 'body')).toThrow(field);
    }
  });

  it('accepts a reference-only body', () => {
    // The real bind body, from the real mapping: the tripwire must not fire on the
    // three reference fields, or the panel could not send anything at all.
    expect(() => assertNoSecretMaterial(expectedBindBody(ROW), 'body')).not.toThrow();
  });
});
