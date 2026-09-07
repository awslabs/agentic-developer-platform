/**
 * BedrockAccountSelector tests — Issue #4746 (#4692 · R5), §6.4 / §1.4.
 *
 * The read-only-display discipline of `PersonSpendingLimit` applied to a surface that also
 * writes: what is asserted is not that the controls work, but that **the screen never
 * claims a destination is serving the person when something else is**. Who is billed for
 * model calls is the fact this component exists to state, and every wrong way to state it
 * is silent.
 *
 * Five groups:
 *
 * 1. **The effective destination is what is rendered** — including the platform rung,
 *    which is an answer and not an absence.
 * 2. **An admin override is disclosed, and the person's own pick is never shown active**
 *    (§1.4, the #4511 inert-config defect). Two assertions, both required.
 * 3. **A stored selection that governs nothing says so** — §4.4's other, easily-missed
 *    way for a pick to stop being in force.
 * 4. **Fail-closed is disclosed unprompted** (ruling 1), and unselectable connections are
 *    listed with their remediation rather than hidden (§5.0b).
 * 5. **No target leaves the client, and a failure is not an answer.** The service module's
 *    functions take no person at any position; a load failure must not render as "your
 *    calls go to the platform account".
 *
 * The service module is mocked, the component is real, and `describeRoutingReason` is kept
 * real — stubbing the function that turns a reason code into remediation prose would make
 * the group-4 assertions vacuous.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BedrockAccountSelector } from '@/components/aws/BedrockAccountSelector';
import type { MySelectionResponse, SelectableConnection } from '@/types/bedrockRouting';

vi.mock('@/services/bedrockRoutingSelf', () => ({
  getMySelection: vi.fn(),
  setMySelection: vi.fn(),
  clearMySelection: vi.fn(),
}));

import { getMySelection, setMySelection, clearMySelection } from '@/services/bedrockRoutingSelf';

const mockGet = getMySelection as ReturnType<typeof vi.fn>;
const mockSet = setMySelection as ReturnType<typeof vi.fn>;
const mockClear = clearMySelection as ReturnType<typeof vi.fn>;

const OWN_ACCOUNT = '555555556677';
const ADMIN_ACCOUNT = '111111114821';
const TEAM_ACCOUNT = '222222227733';

/** A verified, routing-capable connection of the caller's own. */
const routable: SelectableConnection = {
  credential_id: 'cred-routable',
  label: 'jdoe-routable',
  account_id: OWN_ACCOUNT,
  status: 'verified',
  selectable: true,
  reason: null,
};

/**
 * The §5.0b case: verified, but its role is pinned to whoever created it.
 *
 * The realistic majority on any install that predates the v2 template — which is why the
 * component must render it with its reason rather than filter it out.
 */
const v1Pinned: SelectableConnection = {
  credential_id: 'cred-v1',
  label: 'jdoe-legacy',
  account_id: '999999991111',
  status: 'verified',
  selectable: false,
  reason: 'role_user_pinned_needs_v2_template',
};

/** Transcribed from `MySelectionResponse` in `src/admin/bedrock_routing/schemas.py`. */
function selectionOn(overrides: Partial<MySelectionResponse> = {}): MySelectionResponse {
  return {
    effective: {
      user_id: 'user-1',
      rung: 'platform',
      account_id: null,
      destination_id: null,
      destination_label: null,
      source: null,
      overrides_self_selection: false,
      shadowed_rung: null,
      shadowed_account_id: null,
      ...(overrides.effective ?? {}),
    },
    own_selection_destination_id: null,
    own_selection_account_id: null,
    own_selection_label: null,
    own_selection_credential_id: null,
    own_selection_active: false,
    pinned_by_platform_admin: false,
    connections: [routable, v1Pinned],
    ...overrides,
  };
}

/** The happy state: the caller's own pick is what governs. */
const ownActive = selectionOn({
  effective: {
    user_id: 'user-1',
    rung: 'user',
    account_id: OWN_ACCOUNT,
    destination_id: 'dest-own',
    destination_label: 'jdoe-routable',
    source: 'self',
    overrides_self_selection: false,
    shadowed_rung: 'platform',
    shadowed_account_id: null,
  },
  own_selection_destination_id: 'dest-own',
  own_selection_account_id: OWN_ACCOUNT,
  own_selection_label: 'jdoe-routable',
  own_selection_credential_id: 'cred-routable',
  own_selection_active: true,
});

/**
 * The §1.4 state: a platform admin has taken the user rung.
 *
 * `own_selection_*` is null rather than stale, matching the server: with one row per scope
 * the admin's write *replaced* the person's pick, so there is no longer one to report.
 */
const adminPinned = selectionOn({
  effective: {
    user_id: 'user-1',
    rung: 'user',
    account_id: ADMIN_ACCOUNT,
    destination_id: 'dest-admin',
    destination_label: 'acme-prod',
    source: 'platform_admin',
    overrides_self_selection: true,
    shadowed_rung: 'platform',
    shadowed_account_id: null,
  },
  pinned_by_platform_admin: true,
});

/**
 * The §4.4 state: the person's own row survives, and its destination has stopped working.
 *
 * Nobody overrode them — this is the *other* reason a stored pick is not in force, and it
 * is indistinguishable from the happy state if you only look at whether a row exists.
 */
const ownStale = selectionOn({
  effective: {
    user_id: 'user-1',
    rung: 'team',
    account_id: TEAM_ACCOUNT,
    destination_id: 'dest-team',
    destination_label: 'ml-research',
    source: 'platform_admin',
    overrides_self_selection: false,
    shadowed_rung: 'platform',
    shadowed_account_id: null,
  },
  own_selection_destination_id: 'dest-own',
  own_selection_account_id: OWN_ACCOUNT,
  own_selection_label: 'jdoe-routable',
  own_selection_credential_id: 'cred-routable',
  own_selection_active: false,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockGet.mockResolvedValue(selectionOn());
});

// ===========================================================================
// 1 — the effective destination is what is rendered
// ===========================================================================

describe('BedrockAccountSelector — what actually serves the caller', () => {
  it('the platform rung is stated as an answer, not as "unconfigured"', async () => {
    // No mapping anywhere is today's ambient-IRSA behaviour and is complete. Rendering it
    // as missing configuration would send somebody to fix a thing that is not broken.
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-platform')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-platform').textContent).toMatch(/nothing is misconfigured/i);
    expect(screen.queryByTestId('bedrock-selection-own-active')).not.toBeInTheDocument();
  });

  it("the caller's own active selection is named as theirs", async () => {
    mockGet.mockResolvedValue(ownActive);
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-own-active')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-own-active').textContent).toContain('…6677');
    expect(screen.getByTestId('bedrock-selection-active-cred-routable')).toBeInTheDocument();
    expect(screen.queryByTestId('bedrock-selection-stale')).not.toBeInTheDocument();
  });

  it('a team rule serving them is named as the team\'s, not as their own and not as absent', async () => {
    mockGet.mockResolvedValue(
      selectionOn({
        effective: {
          user_id: 'user-1',
          rung: 'team',
          account_id: TEAM_ACCOUNT,
          destination_id: 'dest-team',
          destination_label: 'ml-research',
          source: 'platform_admin',
          overrides_self_selection: false,
          shadowed_rung: 'platform',
          shadowed_account_id: null,
        },
      }),
    );
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-inherited')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-inherited').textContent).toMatch(/team/i);
    expect(screen.getByTestId('bedrock-selection-inherited').textContent).toContain('…7733');
    expect(screen.queryByTestId('bedrock-selection-platform')).not.toBeInTheDocument();
  });

  it('renders no role ARN (§2.6)', async () => {
    mockGet.mockResolvedValue(ownActive);
    const { container } = render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-own-active')).toBeInTheDocument());
    expect(container.textContent).not.toMatch(/arn:aws:iam::/);
  });
});

// ===========================================================================
// 2 — the §1.4 override, stated and never implied away
// ===========================================================================

describe('BedrockAccountSelector — an admin override (§1.4)', () => {
  beforeEach(() => {
    mockGet.mockResolvedValue(adminPinned);
  });

  it('says the destination is overridden by a platform mapping, and names the account', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-overridden')).toBeInTheDocument());
    const text = screen.getByTestId('bedrock-selection-overridden').textContent ?? '';
    expect(text).toMatch(/overridden by a platform mapping/i);
    expect(text).toContain('…4821');
  });

  it("does NOT show the person's own selection as active", async () => {
    // The central assertion of this file. A screen that showed a stale pick as in force
    // while an admin's mapping bills their calls is the #4511 inert-config defect on the
    // one surface built to answer "where do my calls go".
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-overridden')).toBeInTheDocument());
    expect(screen.queryByTestId('bedrock-selection-own-active')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bedrock-selection-active-cred-routable')).not.toBeInTheDocument();
  });

  it('offers no control that could only be refused', async () => {
    // The write is refused server-side with `pinned_by_platform_admin`, so a Use / Clear
    // button here can only 422. §1.4 asks the screen to state the override, not to imply
    // it is negotiable.
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-pinned')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-pinned').textContent).toMatch(/platform admin/i);
    expect(screen.queryByTestId('bedrock-selection-use-cred-routable')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bedrock-selection-clear')).not.toBeInTheDocument();
    expect(mockSet).not.toHaveBeenCalled();
  });
});

// ===========================================================================
// 3 — a stored selection that governs nothing (§4.4)
// ===========================================================================

describe('BedrockAccountSelector — a stale own selection (§4.4)', () => {
  beforeEach(() => {
    mockGet.mockResolvedValue(ownStale);
  });

  it('states that the chosen account is not in use, and what is', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-stale')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-stale').textContent).toContain('…6677');
    expect(screen.getByTestId('bedrock-selection-stale').textContent).toMatch(/not in use/i);
    // And the destination that IS serving them is named.
    expect(screen.getByTestId('bedrock-selection-inherited').textContent).toContain('…7733');
  });

  it('does not mark the stale connection as in use', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-stale')).toBeInTheDocument());
    expect(screen.queryByTestId('bedrock-selection-active-cred-routable')).not.toBeInTheDocument();
    expect(screen.getByTestId('bedrock-selection-use-cred-routable')).toBeInTheDocument();
  });

  it('does not call it an admin override — nobody overrode them', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-stale')).toBeInTheDocument());
    expect(screen.queryByTestId('bedrock-selection-overridden')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bedrock-selection-pinned')).not.toBeInTheDocument();
  });

  it('still offers to clear it — a selection that governs nothing is one to remove', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-clear')).toBeInTheDocument());
  });
});

// ===========================================================================
// 4 — fail-closed, and honest unselectable rows
// ===========================================================================

describe('BedrockAccountSelector — disclosure and selectability', () => {
  it('discloses fail-closed unprompted, before any choice is made', async () => {
    // Ruling 1 / §6.4. The behaviour is surprising if undisclosed: a person reasonably
    // assumes a broken destination falls back to however things worked before. It does
    // not, and they are the one who gets paged.
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-fail-closed')).toBeInTheDocument());
    const text = screen.getByTestId('bedrock-selection-fail-closed').textContent ?? '';
    expect(text).toMatch(/fails with an error/i);
    expect(text).toMatch(/does not fall back/i);
  });

  it('lists an unselectable connection with its remediation rather than hiding it (§5.0b)', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-connection-cred-v1')).toBeInTheDocument());
    // The prose comes from the shared reason vocabulary, which is kept real here — the
    // point of the code is that it names a remediation, and "verification failed" would
    // send somebody to debug the wrong thing.
    expect(screen.getByTestId('bedrock-selection-reason-cred-v1').textContent).toMatch(/re-run the routing cloudformation template/i);
    expect(screen.getByTestId('bedrock-selection-use-cred-v1')).toBeDisabled();
  });

  it('a selectable connection can be picked, and the screen re-renders from the response', async () => {
    mockSet.mockResolvedValue(ownActive);
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-use-cred-routable')).toBeInTheDocument());
    await userEvent.click(screen.getByTestId('bedrock-selection-use-cred-routable'));

    // The credential id, and nothing else. Not a destination id, not a person.
    await waitFor(() => expect(mockSet).toHaveBeenCalledWith('cred-routable'));
    expect(mockSet).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-own-active')).toBeInTheDocument());
  });

  it('a refused save shows the reason and says nothing changed', async () => {
    // A refusal is the never-store-inert discipline working, not a crash. The person must
    // learn the destination was NOT stored — believing traffic moved when it did not is
    // the inversion the probe exists to prevent.
    mockSet.mockRejectedValue({ detail: { reason: 'role_missing_bedrock_permission', message: 'The platform could not prove it can serve calls.' } });
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-use-cred-routable')).toBeInTheDocument());
    await userEvent.click(screen.getByTestId('bedrock-selection-use-cred-routable'));

    await waitFor(() => expect(screen.getByTestId('bedrock-selection-save-error')).toBeInTheDocument());
    const text = screen.getByTestId('bedrock-selection-save-error').textContent ?? '';
    expect(text).toContain('could not prove');
    expect(text).toMatch(/nothing was changed/i);
    // And the still-true effective destination survives the refusal.
    expect(screen.getByTestId('bedrock-selection-platform')).toBeInTheDocument();
  });

  it('clearing says traffic falls back rather than stops', async () => {
    // The natural reading of "stop using my account" is "my calls will fail". They will
    // not: removing a selection un-shadows the ladder beneath it.
    mockGet.mockResolvedValue(ownActive);
    mockClear.mockResolvedValue(selectionOn());
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-clear')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-clear').parentElement?.textContent).toMatch(/do not stop working/i);

    await userEvent.click(screen.getByTestId('bedrock-selection-clear'));
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-platform')).toBeInTheDocument());
    expect(mockClear).toHaveBeenCalledTimes(1);
  });

  it('says what to do when there are no connections at all', async () => {
    mockGet.mockResolvedValue(selectionOn({ connections: [] }));
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-no-connections')).toBeInTheDocument());
  });
});

// ===========================================================================
// 5 — no target leaves the client; a failure is not an answer
// ===========================================================================

describe('BedrockAccountSelector — the self surface, and failures', () => {
  it('no self function takes a target argument (the access control is the shape)', async () => {
    // `importActual` bypasses the mock above, so this pins the REAL module: the anchor is
    // derived from the token server-side, and a `user_id` parameter added here "for the
    // admin view" is how authority leaks out of a self-service component.
    const actual = await vi.importActual<typeof import('@/services/bedrockRoutingSelf')>('@/services/bedrockRoutingSelf');
    expect(actual.getMySelection.length).toBe(0);
    expect(actual.clearMySelection.length).toBe(0);
    // The one argument the write takes is a connection of the caller's own — never a person.
    expect(actual.setMySelection.length).toBe(1);
  });

  it('reads once on mount and takes no argument', async () => {
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-platform')).toBeInTheDocument());
    expect(mockGet).toHaveBeenCalledWith();
  });

  it('a load failure is NOT rendered as "your calls go to the platform account"', async () => {
    // An outage is the one moment we cannot know who is billed. The platform-account copy
    // would be a specific, plausible, wrong answer — the reason there is no fallback shape.
    mockGet.mockRejectedValue(new Error('boom'));
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-error')).toBeInTheDocument());
    expect(screen.getByTestId('bedrock-selection-error').textContent).toMatch(/not a statement that your calls go to the platform/i);
    expect(screen.queryByTestId('bedrock-selection-platform')).not.toBeInTheDocument();
    expect(screen.queryByTestId('bedrock-selection-fail-closed')).not.toBeInTheDocument();
  });

  it('Retry refetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(selectionOn());
    render(<BedrockAccountSelector />);
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-retry')).toBeInTheDocument());
    await userEvent.click(screen.getByTestId('bedrock-selection-retry'));
    await waitFor(() => expect(screen.getByTestId('bedrock-selection-platform')).toBeInTheDocument());
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});
