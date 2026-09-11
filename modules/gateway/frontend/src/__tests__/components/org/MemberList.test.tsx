/**
 * MemberList component tests — Issue #4847 (#4839 · T2b).
 *
 * These cover the presentational half of the issue's Validation section: that a
 * person on two teams renders two chips with exactly ONE marked primary, that the
 * spend column distinguishes its three states (a figure against a limit, a figure
 * against no limit, and no figures at all) rather than collapsing them into a zero,
 * and that a rejected write keeps the dialog open so the caller's message is read in
 * context.
 *
 * The wiring half — which endpoint each action calls, and that the server's 409 text
 * survives verbatim — is asserted against the page in
 * `src/__tests__/pages/admin/Organizations.test.tsx`, since that is where the client
 * functions are actually called.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemberList, type OrgMember } from '@/components/org/MemberList';
import type { MemberBudget } from '@/services/admin';
import type { Team, TeamMembership } from '@/types';

const JANE: OrgMember = {
  id: 'user-jane',
  email: 'jane@sophos.test',
  name: 'Jane Doe',
  githubUsername: 'jdoe-gh',
  role: 'member',
};

const SAM: OrgMember = {
  id: 'user-sam',
  email: 'sam@sophos.test',
  name: 'Sam Field',
  githubUsername: null,
  role: 'org_admin',
};

const TEAMS: Team[] = [
  { id: 'team-ml', departmentId: 'dept-eng', name: 'ml-team', createdAt: '2026-01-01T00:00:00Z' },
  {
    id: 'team-platform',
    departmentId: 'dept-eng',
    name: 'platform-team',
    createdAt: '2026-01-01T00:00:00Z',
  },
  { id: 'team-web', departmentId: 'dept-design', name: 'web-team', createdAt: '2026-01-01T00:00:00Z' },
];

function membership(overrides: Partial<TeamMembership> & { teamId: string }): TeamMembership {
  return {
    id: `m-${overrides.teamId}`,
    userId: JANE.id,
    orgId: 'sophos',
    role: 'member',
    isPrimary: false,
    source: 'admin',
    externalId: null,
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: null,
    ...overrides,
  };
}

/** Jane is on two teams; ml-team is her primary. Sam is on one. */
const MEMBERSHIPS: Record<string, TeamMembership[]> = {
  'user-jane': [
    membership({ teamId: 'team-ml', isPrimary: true }),
    membership({ teamId: 'team-platform' }),
  ],
  'user-sam': [membership({ teamId: 'team-web', userId: SAM.id, isPrimary: true })],
};

// Labels are the server's THIRD-PERSON mapping for this surface (PR #4936 review,
// M1): `individual limit` / `team default` / `org default` / `platform default` —
// never the ladder's second-person prose ("a limit set for YOU by…"), which is
// addressed to the person the limit governs, not to the admin reading this panel.
const BUDGETS: Record<string, MemberBudget> = {
  'user-jane': {
    userId: 'user-jane',
    spendUsd: '38.500000',
    limitUsd: '75.00',
    source: 'admin',
    sourceLabel: 'individual limit',
    isCapped: true,
  },
  'user-sam': {
    userId: 'user-sam',
    spendUsd: '47.000000',
    limitUsd: '50.00',
    source: 'org_default',
    sourceLabel: 'org default',
    isCapped: true,
  },
};

function renderList(props: Partial<Parameters<typeof MemberList>[0]> = {}) {
  return render(
    <MemberList
      members={[JANE, SAM]}
      membershipsByUserId={MEMBERSHIPS}
      teams={TEAMS}
      canManage
      onAddTeam={vi.fn()}
      onSetPrimary={vi.fn()}
      onRemoveTeam={vi.fn()}
      {...props}
    />
  );
}

function memberRow(name: string): HTMLElement {
  return screen.getByText(name).closest('tr')!;
}

describe('MemberList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('multi-team display', () => {
    it('renders one chip per membership row with exactly one marked primary', () => {
      renderList();

      const row = memberRow('Jane Doe');
      // Two chips, because she holds two membership ROWS. The single-valued
      // `users.team_id` pointer this panel replaces could only ever show one.
      expect(within(row).getByTestId('team-chip-team-ml')).toBeInTheDocument();
      expect(within(row).getByTestId('team-chip-team-platform')).toBeInTheDocument();
      // Exactly one primary marker — the primary is the budget-bearing team, so two
      // (or none) would misstate which team's budget governs her.
      expect(within(row).getAllByText('primary')).toHaveLength(1);
      expect(
        within(within(row).getByTestId('team-chip-team-ml')).getByText('primary')
      ).toBeInTheDocument();
    });

    it('names teams from the server-supplied team list, not from raw ids', () => {
      renderList();

      const row = memberRow('Jane Doe');
      expect(within(row).getByText(/ml-team/)).toBeInTheDocument();
      expect(within(row).getByText(/platform-team/)).toBeInTheDocument();
    });

    it('says so when a member is on no team rather than rendering an empty cell', () => {
      renderList({ membershipsByUserId: {} });

      expect(screen.getAllByText('on no team — assign one')).toHaveLength(2);
    });
  });

  describe('person label', () => {
    it('labels a member by their GitHub login when one is linked', () => {
      renderList();

      expect(within(memberRow('Jane Doe')).getByText('jdoe-gh')).toBeInTheDocument();
    });

    it('states the absence of a GitHub link instead of leaving the cell blank', () => {
      renderList();

      // A legitimate permanent state for an email signup — a blank cell would read as
      // data that failed to load.
      expect(within(memberRow('Sam Field')).getByText('no GitHub linked')).toBeInTheDocument();
    });
  });

  describe('spend / limit column', () => {
    it('shows month spend against the applicable limit with its source label', () => {
      renderList({ budgets: BUDGETS });

      const row = memberRow('Jane Doe');
      expect(within(row).getByText('$38.5 / $75')).toBeInTheDocument();
      // Verbatim from the server, which composes third-person labels for this
      // surface: only the ladder resolver knows which rung won.
      expect(within(row).getByText('individual limit')).toBeInTheDocument();
      expect(within(memberRow('Sam Field')).getByText('org default')).toBeInTheDocument();
    });

    it('marks a member near their cap', () => {
      renderList({ budgets: BUDGETS });

      // $47 of $50 is 94% — amber, per the UI contract's near-cap treatment.
      const figure = within(memberRow('Sam Field')).getByText('$47 / $50');
      expect(figure.className).toContain('amber');
    });

    it('renders no limit as no limit, never as $0.00', () => {
      renderList({
        budgets: {
          'user-jane': {
            userId: 'user-jane',
            spendUsd: '12.000000',
            limitUsd: null,
            source: null,
            sourceLabel: null,
            isCapped: false,
          },
        },
      });

      const row = memberRow('Jane Doe');
      expect(within(row).getByText('$12')).toBeInTheDocument();
      expect(within(row).getByText('no limit')).toBeInTheDocument();
      // A zero here would describe somebody forbidden to spend anything — the inverse
      // of "nothing governs them".
      expect(within(row).queryByText(/\$0/)).not.toBeInTheDocument();
    });

    it('omits the column entirely when the caller may not read budgets', () => {
      renderList();

      expect(screen.queryByText('Spend / limit (month)')).not.toBeInTheDocument();
    });

    it('does not report a member with no budget row as having no spend', () => {
      // Sam is absent from the map: the panel must not draw him at $0.
      renderList({ budgets: { 'user-jane': BUDGETS['user-jane'] } });

      const row = memberRow('Sam Field');
      expect(within(row).getByText('—')).toBeInTheDocument();
      expect(within(row).queryByText(/\$0/)).not.toBeInTheDocument();
    });
  });

  describe('team management dialog', () => {
    async function openDialog(user: ReturnType<typeof userEvent.setup>, name = 'Jane Doe') {
      await user.click(within(memberRow(name)).getByRole('button', { name: 'Teams' }));
      await waitFor(() =>
        expect(screen.getByRole('heading', { name: /Teams for/ })).toBeInTheDocument()
      );
    }

    it('is not offered at all when the caller cannot manage the org', () => {
      renderList({ canManage: false });

      expect(screen.queryByRole('button', { name: 'Teams' })).not.toBeInTheDocument();
    });

    it('offers "make primary" only on the teams that are not already primary', async () => {
      const user = userEvent.setup();
      renderList();
      await openDialog(user);

      // One button, for platform-team. Offering it on ml-team would provoke the very
      // 409 the server raises for a redundant primary request.
      expect(screen.getAllByRole('button', { name: 'Make primary' })).toHaveLength(1);
    });

    it('sets the primary through the caller callback, naming the team', async () => {
      const onSetPrimary = vi.fn().mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderList({ onSetPrimary });
      await openDialog(user);

      await user.click(screen.getByRole('button', { name: 'Make primary' }));

      expect(onSetPrimary).toHaveBeenCalledWith(JANE, 'team-platform');
    });

    it('populates the picker from the org-wide team list, excluding teams already held', async () => {
      const user = userEvent.setup();
      renderList();
      await openDialog(user);

      const picker = screen.getByLabelText('Add to team');
      const options = within(picker).getAllByRole('option').map((o) => o.textContent);
      // web-team belongs to a DIFFERENT department and is still offered: assignment
      // chooses from the whole org, not from the department selected elsewhere.
      expect(options).toContain('web-team');
      // Her existing teams are not re-offered.
      expect(options).not.toContain('ml-team');
      expect(options).not.toContain('platform-team');
    });

    it('adds a team through the caller callback', async () => {
      const onAddTeam = vi.fn().mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderList({ onAddTeam });
      await openDialog(user);

      await user.selectOptions(screen.getByLabelText('Add to team'), 'team-web');
      await user.click(screen.getByRole('button', { name: 'Add' }));

      expect(onAddTeam).toHaveBeenCalledWith(JANE, 'team-web');
    });

    it('removes a membership through the caller callback', async () => {
      const onRemoveTeam = vi.fn().mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderList({ onRemoveTeam });
      await openDialog(user);

      const platformRow = screen.getByTestId('managed-team-team-platform');
      await user.click(within(platformRow).getByRole('button', { name: 'Remove' }));

      expect(onRemoveTeam).toHaveBeenCalledWith(JANE, 'team-platform');
    });

    it('keeps the dialog open when a write is refused, so the caller message has context', async () => {
      const onSetPrimary = vi.fn().mockRejectedValue({
        error: 'team_membership_second_primary',
        message: 'User already has a primary team.',
      });
      const user = userEvent.setup();
      renderList({ onSetPrimary });
      await openDialog(user);

      await user.click(screen.getByRole('button', { name: 'Make primary' }));

      await waitFor(() => expect(onSetPrimary).toHaveBeenCalled());
      // Still open — a 409 about "a different primary" is only interpretable while
      // you can see which team you were promoting.
      expect(screen.getByRole('heading', { name: /Teams for/ })).toBeInTheDocument();
    });

    it('discloses a truncated team list instead of presenting page 1 as complete', async () => {
      const user = userEvent.setup();
      renderList({ teamsTruncated: true });
      await openDialog(user);

      // M4: a picker showing 100 of 130 teams with nothing saying so reads as the
      // complete set, and the missing team reads as "does not exist".
      expect(screen.getByTestId('teams-truncated-warning')).toHaveTextContent(
        /Not every team is listed/
      );
    });

    it('says nothing about truncation when the team list is complete', async () => {
      const user = userEvent.setup();
      renderList();
      await openDialog(user);

      expect(screen.queryByTestId('teams-truncated-warning')).not.toBeInTheDocument();
    });
  });

  // The unassigned warning fires on the REAL unassigned population (PR #4936
  // review, M3): post-backfill everybody holds a row on the org's default team, so
  // "no rows at all" missed essentially everyone the warning was for.
  describe('unassigned warning', () => {
    const DEFAULT_TEAM: Team = {
      id: 'sophos-team-default',
      departmentId: 'dept-default',
      name: 'Default',
      createdAt: '2026-01-01T00:00:00Z',
    };

    it('warns when the only membership is the org default team, with the chip still rendered', () => {
      renderList({
        teams: [...TEAMS, DEFAULT_TEAM],
        defaultTeamId: 'sophos-team-default',
        membershipsByUserId: {
          'user-jane': [membership({ teamId: 'sophos-team-default', isPrimary: true })],
          'user-sam': MEMBERSHIPS['user-sam'],
        },
      });

      const row = memberRow('Jane Doe');
      // The membership is real, so its chip stays; the warning rides alongside.
      expect(within(row).getByTestId('team-chip-sophos-team-default')).toBeInTheDocument();
      expect(within(row).getByTestId('unassigned-warning-user-jane')).toHaveTextContent(
        '⚠ unassigned — default team'
      );
      // Sam holds a real team — no warning on his row.
      expect(within(memberRow('Sam Field')).queryByText(/unassigned/)).not.toBeInTheDocument();
    });

    it('does not warn when a real team accompanies the default membership', () => {
      renderList({
        teams: [...TEAMS, DEFAULT_TEAM],
        defaultTeamId: 'sophos-team-default',
        membershipsByUserId: {
          'user-jane': [
            membership({ teamId: 'sophos-team-default', isPrimary: true }),
            membership({ teamId: 'team-ml' }),
          ],
          'user-sam': MEMBERSHIPS['user-sam'],
        },
      });

      expect(within(memberRow('Jane Doe')).queryByText(/unassigned/)).not.toBeInTheDocument();
    });

    it('still warns on a member with no membership rows at all', () => {
      renderList({ defaultTeamId: 'sophos-team-default', membershipsByUserId: {} });

      expect(screen.getAllByText('on no team — assign one')).toHaveLength(2);
    });
  });

  // Role changes reuse UserList's client call via the caller callback (PR #4936
  // review, M2a); the select is the mockup's affordance for the same act.
  describe('role select', () => {
    it('renders the role as a select and fires the caller callback with the new role', async () => {
      const onChangeRole = vi.fn().mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderList({ onChangeRole });

      await user.selectOptions(screen.getByLabelText('Role for Jane Doe'), 'org_admin');

      expect(onChangeRole).toHaveBeenCalledWith(JANE, 'org_admin');
    });

    it('offers the ceiling-filtered roles the caller passed', async () => {
      renderList({ onChangeRole: vi.fn(), availableRoles: ['member', 'org_admin'] });

      const options = within(screen.getByLabelText('Role for Jane Doe'))
        .getAllByRole('option')
        .map((o) => o.textContent);
      expect(options).toEqual(['Member', 'Org Admin']);
    });

    it('falls back to read-only text when the caller cannot manage the org', () => {
      renderList({ canManage: false, onChangeRole: vi.fn() });

      expect(screen.queryByLabelText('Role for Jane Doe')).not.toBeInTheDocument();
      expect(within(memberRow('Jane Doe')).getByText('Member')).toBeInTheDocument();
    });
  });

  // Removal FROM THE ORGANIZATION — not a team removal and not UserList's
  // demote-to-member — behind the panel's persistent-confirmation convention
  // (PR #4936 review, M2b).
  describe('remove member', () => {
    it('requires a confirmation that states the real consequence before firing', async () => {
      const onRemoveMember = vi.fn().mockResolvedValue(undefined);
      const user = userEvent.setup();
      renderList({ onRemoveMember });

      await user.click(within(memberRow('Jane Doe')).getByRole('button', { name: 'Remove' }));

      const dialog = screen.getByRole('dialog');
      // The copy must say what actually happens: org-level deletion, not a role change.
      expect(dialog).toHaveTextContent(/from this organization/i);
      expect(dialog).toHaveTextContent(/not a role change/i);
      expect(dialog).toHaveTextContent(/cannot be undone/i);
      expect(onRemoveMember).not.toHaveBeenCalled();

      await user.click(within(dialog).getByRole('button', { name: 'Remove member' }));
      expect(onRemoveMember).toHaveBeenCalledWith(JANE);
    });

    it('keeps the confirmation open when the removal is refused', async () => {
      const onRemoveMember = vi.fn().mockRejectedValue({
        error: 'access_denied',
        message: 'Access denied: org:update required for sophos',
      });
      const user = userEvent.setup();
      renderList({ onRemoveMember });

      await user.click(within(memberRow('Jane Doe')).getByRole('button', { name: 'Remove' }));
      await user.click(
        within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove member' })
      );

      await waitFor(() => expect(onRemoveMember).toHaveBeenCalled());
      // Still open over a removal that did not happen; the caller's banner says why.
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('offers no Remove affordance without the callback', () => {
      renderList({ onRemoveMember: undefined });

      expect(
        within(memberRow('Jane Doe')).queryByRole('button', { name: 'Remove' })
      ).not.toBeInTheDocument();
    });
  });
});
