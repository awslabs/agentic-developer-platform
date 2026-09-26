/**
 * Organizations — admin panel for organization / department / team structure.
 *
 * Issue #4841 (#4839 · T2a). Implements the first half of C2 of the accepted design
 * (`docs/design-notes/4828-platform-native-org-team-user.md`, ruling R1: orgs are created
 * without GitHub and a connection is attached later). UI contract:
 * `docs/mockups/4839-tenancy-admin.html`.
 *
 * ## Extend, do not fork
 *
 * The department and team lists here are the SHIPPED presentational components —
 * `components/org/DepartmentList` and `components/department/TeamManagement` — not new
 * ones. Both already expose exactly the create/edit/delete callback surface this panel
 * needs; `OrgDashboard` renders `DepartmentList` read-only and never wires its CRUD
 * callbacks up, and `DepartmentDashboard` already drives `TeamManagement` against these
 * same client functions. This panel supplies the wiring for both in one org-scoped place.
 * A parallel set of list components under `pages/admin/` is explicitly forbidden by the
 * operator direction on #4841, and would leave two divergent org screens to maintain.
 *
 * Departments render as a list with a department selector driving the team list, rather
 * than the mockup's single nested tree, for that reason: the tree's affordances (rename,
 * delete, per-department team CRUD) are all present, and reaching them through the two
 * existing components costs no functionality. The mockup is authoritative on affordances;
 * this is the same set.
 *
 * ## Authz — two DIFFERENT server rules, deliberately gated differently
 *
 * The panel gates affordances purely to keep admins out of dead ends. **The server is the
 * boundary**, and it is not one boundary here but two:
 *
 * - **List / read** (`GET /admin/organizations`, dept + team reads) gate on
 *   `Permission.ORG_READ` scoped to the org, and the list route additionally filters to
 *   `get_accessible_organizations(caller)`. An org admin legitimately sees their own org.
 * - **Department / team writes** gate on `Permission.ORG_UPDATE` with `target_org_id`
 *   (`src/admin/routes.py`). An org admin may write inside their own org.
 * - **Org create** does NOT follow that pattern. The canonical route's whole router mounts
 *   under `dependencies=[Depends(require_admin)]` (`src/admin/identity/router.py`), and
 *   `require_admin` checks `is_admin`, which deliberately EXCLUDES `org_admin`
 *   (`src/auth/dependencies.py`). So creation is **platform-admin-only**, and the button
 *   is gated on `isPlatformAdmin()` — matching the route, not the `ORG_CREATE` permission
 *   enum. Gating it on `canCreateOrganizations()` would be bug class #1 in #4841's own
 *   impact table: a button an org admin can see that returns an uninterpretable 403.
 *
 * A 403 that arrives anyway is rendered as its server message, never as a blank panel.
 *
 * ## Members (Issue #4847 · T2b)
 *
 * The Members tab assigns people to teams through T1's membership routes (#4840) and
 * NEVER through `users.team_id`: that column is a cache the server re-points to follow
 * the primary membership, and the membership row is the truth. Concretely, "add to
 * team" is `POST .../teams/{team_id}/members`, "make primary" is
 * `PUT .../users/{user_id}/teams` with the whole intended set, and "remove" is
 * `DELETE .../teams/{team_id}/members/{user_id}`. Every one of them is followed by a
 * refetch rather than a local mutation, because a write can move the primary
 * server-side (removing the primary promotes the oldest remaining membership).
 *
 * The team picker is sourced from the ORG-WIDE teams endpoint, not the
 * department-scoped list this page already loads for `TeamManagement`: assignment
 * chooses from every team in the org, and reusing the narrower list would silently
 * hide the teams outside whichever department is selected above.
 *
 * **A third authz rule, on top of the two above.** The batch spend/limit read
 * (`GET .../member-budgets`) is `require_platform_admin`, because a person limit can
 * come from a partition-free individual row and serving it to an org admin would
 * disclose a ceiling governing their members' spend in tenants they have no
 * membership in (the #4620 ruling). So the spend column is a platform-admin
 * affordance INSIDE a panel an org admin may otherwise use fully — the request is
 * skipped rather than fired-and-403'd, and the column simply does not render.
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Button,
  Card,
  Input,
  Modal,
  ModalFooter,
  Select,
  Spinner,
  Tab,
  TabPanel,
  Tabs,
  TabsList,
} from '@/components/ui';
import { DepartmentList } from '@/components/org/DepartmentList';
import { ServiceIdentityList } from '@/components/org/ServiceIdentityList';
import { MemberList, type OrgMember } from '@/components/org/MemberList';
import { TeamManagement } from '@/components/department/TeamManagement';
// The GitHub-ID-first person picker the mockup names ("Same searchable picker as
// Bedrock Account Routing") — reused, not re-implemented (#4830 pattern, #4827).
import { PersonPicker } from '@/components/bedrock/BedrockAccountRouting';
import {
  addOrgMember,
  addTeamMember,
  assignUserRole,
  createDepartment,
  createOrganizationCanonical,
  createTeam,
  deleteDepartment,
  deleteTeam,
  getAvailableRoles,
  getDepartments,
  getMemberBudgets,
  getOrgTeams,
  getOrgUsers,
  getOrganizations,
  getTeams,
  getUserTeams,
  removeOrgUser,
  removeTeamMember,
  replaceUserTeams,
  updateDepartment,
  updateTeam,
  type MemberBudget,
  type PlatformUser,
} from '@/services/admin';
import { deriveOrgIdentifier, isValidOrgIdentifier } from '@/utils/orgIdentifier';
import { usePermissions } from '@/hooks/usePermissions';
import { formatDate } from '@/utils/format';
import { AdminRole } from '@/types';
import type { Department, Organization, Team, TeamMembership } from '@/types';

/**
 * Pull the human-readable message out of whatever the API client threw.
 *
 * `apiClient` throws the PARSED error body — a plain object carrying `error` and
 * `message` — not an `Error`. So `error instanceof Error` misses the useful text, which on
 * this panel is exactly what the admin needs: the 403 reason, or the 409 that says the
 * identifier is already taken. Same helper shape as `OrgDashboard.tsx`, kept local for the
 * same reason it is local there.
 */
function errorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string' && message) return message;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

/**
 * Name the picked person for the confirmation step.
 *
 * GitHub login first, then name, then email — the order the picker's own label uses,
 * so the person the admin reads in the confirmation is recognisably the one they just
 * chose. Falls back to the id only when there is nothing else: an unrecognisable
 * confirmation is still better than an empty subject in a sentence granting org access.
 */
function describeAddPerson(person: PlatformUser | null, fallbackId: string): string {
  if (!person) return fallbackId;
  return person.githubUsername || person.name || person.email || person.id;
}

/**
 * True when a membership write was refused because the person is not in this org.
 *
 * Issue #4943. The team-add route resolves the person by `(users.id, org_id)` and
 * raises `resource_not_found` for anything outside the path org — deliberately, so a
 * caller cannot distinguish another tenant's member from a nonexistent one. Matched on
 * the stable `error` code rather than the message text, and NOT on the bare HTTP
 * status: a 404 from some other cause must not be answered with "shall I add them to
 * the organization?".
 */
function isNotInThisOrg(error: unknown): boolean {
  return (
    !!error &&
    typeof error === 'object' &&
    (error as { error?: unknown }).error === 'resource_not_found'
  );
}

export default function Organizations() {
  const { canViewOrganizations, canUpdateOrganizations, isPlatformAdmin } = usePermissions();

  const [orgs, setOrgs] = useState<Organization[]>([]);
  const [isLoadingOrgs, setIsLoadingOrgs] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const [selectedOrgId, setSelectedOrgId] = useState<string | null>(null);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [isLoadingDepartments, setIsLoadingDepartments] = useState(false);
  const [selectedDeptId, setSelectedDeptId] = useState<string>('');
  const [teams, setTeams] = useState<Team[]>([]);
  const [isLoadingTeams, setIsLoadingTeams] = useState(false);

  // Create-org modal state. `identifierEdited` is what makes the derive-from-name
  // affordance a SUGGESTION: once the admin types in the identifier field we stop
  // overwriting it from the name, so their deliberate value cannot be silently clobbered
  // by a later keystroke in the name field.
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [newOrgName, setNewOrgName] = useState('');
  const [newOrgId, setNewOrgId] = useState('');
  const [identifierEdited, setIdentifierEdited] = useState(false);
  const [isCreating, setIsCreating] = useState(false);

  // Department create/rename modal state.
  const [deptModal, setDeptModal] = useState<{ mode: 'create' | 'edit'; dept?: Department } | null>(
    null
  );
  const [deptName, setDeptName] = useState('');
  const [isSavingDept, setIsSavingDept] = useState(false);
  const [deletingDept, setDeletingDept] = useState<Department | null>(null);

  // Members tab state (#4847). `activeTab` mirrors the `Tabs` primitive's own
  // selection for ONE purpose: gating the member reads so opening an org does not
  // fetch a roster nobody is looking at. `TabPanel` still decides what renders.
  const [activeTab, setActiveTab] = useState<'structure' | 'members'>('structure');
  const [members, setMembers] = useState<OrgMember[]>([]);
  const [orgTeams, setOrgTeams] = useState<Team[]>([]);
  const [membershipsByUserId, setMembershipsByUserId] = useState<Record<string, TeamMembership[]>>(
    {}
  );
  const [memberBudgets, setMemberBudgets] = useState<Record<string, MemberBudget> | undefined>(
    undefined
  );
  const [isLoadingMembers, setIsLoadingMembers] = useState(false);

  // Roster truncation state (#4936 review, M4): the server pages at 50, so member
  // #51 exists without these — `total`/`hasMore` drive the "showing N of TOTAL"
  // line and the load-more button, and `membersPage` is the cursor it advances.
  const [membersTotal, setMembersTotal] = useState(0);
  const [membersHasMore, setMembersHasMore] = useState(false);
  const [membersPage, setMembersPage] = useState(1);
  const [isLoadingMoreMembers, setIsLoadingMoreMembers] = useState(false);
  /** True when the org has more teams than the 100-per-page picker read returned. */
  const [orgTeamsTruncated, setOrgTeamsTruncated] = useState(false);
  /** Client-side filter over the LOADED roster (name / GitHub username / email). */
  const [memberSearch, setMemberSearch] = useState('');
  /**
   * Roles the caller may assign, from the ceiling-filtered `GET /admin/users/roles`
   * (the same read `OrgDashboard` feeds `UserList` from). `undefined` = not yet
   * loaded; `[]` = the read failed, in which case `MemberList` falls back to its
   * static list and the server remains the boundary.
   */
  const [assignableRoles, setAssignableRoles] = useState<string[] | undefined>(undefined);

  // "+ Add member" modal state (#4936 review, M2d — the mockup's assign-member modal).
  const [isAddMemberOpen, setIsAddMemberOpen] = useState(false);
  const [addPersonId, setAddPersonId] = useState('');
  const [addTeamId, setAddTeamId] = useState('');
  const [addRole, setAddRole] = useState<'member' | 'org_admin'>('member');
  const [isAddingMember, setIsAddingMember] = useState(false);
  /**
   * The picked ROSTER ROW, not just its id (#4943).
   *
   * The row carries `orgId`, and that is the only thing on the client that can tell
   * "this person is already in the org" from "this person must be brought in first" —
   * the difference between a one-step add and the confirmation step below. `null` when
   * nothing is picked, or when the picker could not name the row; a null is "not
   * known", never "not in this org", so the confirm step is not offered on it and the
   * server's 404 remains the trigger.
   */
  const [addPerson, setAddPerson] = useState<PlatformUser | null>(null);
  /**
   * The add-member modal's OWN error line (#4943 · root cause 1).
   *
   * The page-level `error` banner renders at the top of the panel, behind the open
   * modal — so the 404 the server correctly returned three times was never visible,
   * and the admin saw a dialog that had simply stopped responding. An error produced
   * by a control inside the modal has to be rendered inside the modal, next to that
   * control.
   */
  const [addMemberError, setAddMemberError] = useState<string | null>(null);
  /**
   * Set when the picked person is not in this org yet, which turns "Add member" into
   * an explicit two-part decision — join the org AND join the team — rather than a
   * silent extra write behind a button whose label says neither.
   */
  const [addNeedsOrgJoin, setAddNeedsOrgJoin] = useState(false);

  const canManage = canUpdateOrganizations();
  const canView = canViewOrganizations();
  // The spend column's gate — narrower than the panel's, deliberately. See the header.
  const canReadMemberBudgets = isPlatformAdmin();
  // "+ Add member" needs BOTH gates: the writes are ORG_UPDATE, but the person
  // picker's roster read (`GET /admin/users`, #4827) is platform-admin-only —
  // showing the modal to an org admin would open on a picker whose one read 403s,
  // the known-dead-end affordance class this panel's header forbids.
  const canAddMembers = canManage && canReadMemberBudgets;

  const loadOrgs = useCallback(async () => {
    setIsLoadingOrgs(true);
    setError(null);
    try {
      const response = await getOrganizations({ page: 1, pageSize: 100 });
      setOrgs(response.items);
    } catch (err) {
      setError(errorMessage(err, 'Failed to load organizations.'));
    } finally {
      setIsLoadingOrgs(false);
    }
  }, []);

  // Guarded on ORG_READ. The permission early-return below sits after the hooks, as the
  // rules of hooks require, so without this guard a caller who cannot read organizations
  // would still fire the list request on mount — a guaranteed 403 whose response nothing
  // renders. Skip the request instead of making one we know the server will refuse.
  useEffect(() => {
    if (!canView) return;
    loadOrgs();
  }, [canView, loadOrgs]);

  const loadDepartments = useCallback(async (orgId: string) => {
    setIsLoadingDepartments(true);
    setError(null);
    try {
      const response = await getDepartments(orgId);
      setDepartments(response.items);
      // Select the first department so the team list is never an empty panel with no way
      // to populate it. A freshly created org always has one (`{id}-dept-default`).
      setSelectedDeptId((current) =>
        response.items.some((d) => d.id === current) ? current : (response.items[0]?.id ?? '')
      );
    } catch (err) {
      setDepartments([]);
      setError(errorMessage(err, 'Failed to load departments for this organization.'));
    } finally {
      setIsLoadingDepartments(false);
    }
  }, []);

  const loadTeams = useCallback(async (orgId: string, deptId: string) => {
    setIsLoadingTeams(true);
    try {
      const response = await getTeams(orgId, deptId);
      setTeams(response.items);
    } catch (err) {
      setTeams([]);
      setError(errorMessage(err, 'Failed to load teams for this department.'));
    } finally {
      setIsLoadingTeams(false);
    }
  }, []);

  useEffect(() => {
    if (!selectedOrgId) {
      setDepartments([]);
      setSelectedDeptId('');
      return;
    }
    loadDepartments(selectedOrgId);
  }, [selectedOrgId, loadDepartments]);

  useEffect(() => {
    if (!selectedOrgId || !selectedDeptId) {
      setTeams([]);
      return;
    }
    loadTeams(selectedOrgId, selectedDeptId);
  }, [selectedOrgId, selectedDeptId, loadTeams]);

  /**
   * Load everything the Members tab renders, for one org.
   *
   * Four reads, deliberately: the roster, the org-wide team list the picker needs,
   * one membership read per member, and — only for a platform admin — the batch
   * spend/limit page. The per-member membership read is the shape T1 ships (a user's
   * memberships are addressed per user); it is bounded by the roster page size, and
   * `Promise.all` keeps it one round-trip's latency rather than N.
   *
   * The budget read is skipped, not attempted-and-caught, when the caller is not a
   * platform admin: a known-403 request whose response nothing renders is exactly the
   * bug class #4841's impact table names, and `budgets` staying `undefined` is what
   * makes `MemberList` drop the column rather than draw blanks.
   */
  const loadMembers = useCallback(
    async (orgId: string, includeBudgets: boolean, page = 1, append = false) => {
      // A load-more APPENDS page+1 (roster, memberships, and budgets alike) while a
      // (re)load replaces from page 1 — after a write the roster is re-read from the
      // top because the write can have changed any page's contents.
      (append ? setIsLoadingMoreMembers : setIsLoadingMembers)(true);
      try {
        const [roster, teamsResponse] = await Promise.all([
          getOrgUsers(orgId, { page, pageSize: 50 }),
          // The team list is page-independent; re-read it only on a full (re)load.
          append ? Promise.resolve(null) : getOrgTeams(orgId, { page: 1, pageSize: 100 }),
        ]);

        const memberRows: OrgMember[] = roster.items.map((user) => ({
          id: user.id,
          email: user.email,
          name: user.name,
          githubUsername: user.githubUsername,
          role: user.role,
        }));
        setMembers((prev) => (append ? [...prev, ...memberRows] : memberRows));
        setMembersTotal(roster.total);
        setMembersHasMore(roster.hasMore);
        setMembersPage(page);
        if (teamsResponse) {
          setOrgTeams(teamsResponse.items);
          // Truncation is DISCLOSED, not silent (#4936 review, M4): a picker showing
          // 100 of 130 teams with nothing saying so reads as the complete set.
          setOrgTeamsTruncated(teamsResponse.hasMore);
        }

        const membershipLists = await Promise.all(
          memberRows.map((member) => getUserTeams(orgId, member.id))
        );
        setMembershipsByUserId((prev) => ({
          ...(append ? prev : {}),
          ...Object.fromEntries(memberRows.map((member, i) => [member.id, membershipLists[i]])),
        }));

        if (includeBudgets) {
          const budgets = await getMemberBudgets(orgId, { page, pageSize: 50 });
          setMemberBudgets((prev) => ({
            ...(append && prev ? prev : {}),
            ...Object.fromEntries(budgets.items.map((row) => [row.userId, row])),
          }));
        } else if (!append) {
          setMemberBudgets(undefined);
        }
      } catch (err) {
        setError(errorMessage(err, 'Failed to load members for this organization.'));
      } finally {
        (append ? setIsLoadingMoreMembers : setIsLoadingMembers)(false);
      }
    },
    []
  );

  useEffect(() => {
    if (!selectedOrgId || activeTab !== 'members') return;
    loadMembers(selectedOrgId, canReadMemberBudgets);
  }, [selectedOrgId, activeTab, canReadMemberBudgets, loadMembers]);

  // The role select's option list, fetched once when a managing caller first opens
  // the Members tab. On failure `[]` is stored (not retried) and `MemberList` falls
  // back to its static list — the server ceiling-checks the submission either way.
  useEffect(() => {
    if (activeTab !== 'members' || !canManage || assignableRoles !== undefined) return;
    getAvailableRoles()
      .then(setAssignableRoles)
      .catch(() => setAssignableRoles([]));
  }, [activeTab, canManage, assignableRoles]);

  /**
   * Membership writes — the story's central correctness assertion.
   *
   * Each writes a membership ROW through T1's routes and then refetches; none sends a
   * `team_id` on the user. Each rethrows so `MemberList` keeps its dialog open over a
   * write that did not happen, and the server's message — notably the 409
   * `team_membership_second_primary` — is surfaced verbatim by `errorMessage`, never
   * replaced with a client-side guess at the wording.
   */
  const handleAddTeam = async (member: OrgMember, teamId: string) => {
    if (!selectedOrgId) return;
    setError(null);
    try {
      await addTeamMember(selectedOrgId, teamId, { userId: member.id });
      setNotice(`Added ${member.name || member.email} to a team.`);
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      setError(errorMessage(err, 'Failed to add the member to that team.'));
      throw err;
    }
  };

  /**
   * Move the primary by sending the FULL intended set.
   *
   * Not `addTeamMember({isPrimary: true})`, which the server refuses with a 409 when a
   * different primary already exists — correctly, since re-pointing a claim is not a
   * side effect an add should have. The replace-set endpoint is where exactly one
   * primary is expressible, so the whole set is rebuilt with the flag moved.
   */
  const handleSetPrimary = async (member: OrgMember, teamId: string) => {
    if (!selectedOrgId) return;
    setError(null);
    try {
      const current = membershipsByUserId[member.id] ?? [];
      await replaceUserTeams(
        selectedOrgId,
        member.id,
        current.map((m) => ({ teamId: m.teamId, role: m.role, isPrimary: m.teamId === teamId }))
      );
      setNotice(`Primary team updated for ${member.name || member.email}.`);
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      setError(errorMessage(err, 'Failed to change the primary team.'));
      throw err;
    }
  };

  const handleRemoveTeam = async (member: OrgMember, teamId: string) => {
    if (!selectedOrgId) return;
    setError(null);
    try {
      await removeTeamMember(selectedOrgId, teamId, member.id);
      setNotice(`Removed ${member.name || member.email} from a team.`);
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      setError(errorMessage(err, 'Failed to remove the member from that team.'));
      throw err;
    }
  };

  /**
   * Change a member's org role — the SAME client call `UserList`/`OrgDashboard`
   * use (`assignUserRole` → `PUT /admin/organizations/{org}/users/{user}`), not a
   * fork. The server is the boundary: it refuses roles above the caller's ceiling
   * and self-changes, and that refusal surfaces here verbatim.
   */
  const handleChangeRole = async (member: OrgMember, role: string) => {
    if (!selectedOrgId) return;
    setError(null);
    try {
      await assignUserRole({ user_id: member.id, role: role as AdminRole, org_id: selectedOrgId });
      setNotice(`Role updated for ${member.name || member.email}.`);
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      setError(errorMessage(err, 'Failed to change the member role.'));
      throw err;
    }
  };

  /**
   * Remove a member from the ORGANIZATION (`DELETE .../users/{user}`) — deletes
   * their memberships and org account access. Deliberately NOT `removeUserRole`,
   * which is `UserList`'s demote-to-member and keeps the membership row; the two
   * acts must not be conflated. `MemberList`'s confirmation modal states this.
   */
  const handleRemoveMember = async (member: OrgMember) => {
    if (!selectedOrgId) return;
    setError(null);
    try {
      await removeOrgUser(selectedOrgId, member.id);
      setNotice(`Removed ${member.name || member.email} from the organization.`);
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      setError(errorMessage(err, 'Failed to remove the member from the organization.'));
      throw err; // keeps the confirmation open over a removal that did not happen
    }
  };

  /** Close the add-member modal and forget everything it was holding. */
  const resetAddMember = () => {
    setIsAddMemberOpen(false);
    setAddPersonId('');
    setAddPerson(null);
    setAddTeamId('');
    setAddRole('member');
    setAddMemberError(null);
    setAddNeedsOrgJoin(false);
  };

  /**
   * The assign-member modal's submit (#4936 review, M2d; fixed by #4943).
   *
   * Two grains, in order, because they are two different decisions:
   *
   * 1. **Into the ORG** (`addOrgMember`) — only when the person is not a member yet.
   *    The picker lists the PLATFORM roster while the team-add route is org-scoped, so
   *    for anybody from another org this step is what makes the next one possible at
   *    all: without it the server resolves `(users.id, org_id)`, finds nothing, and
   *    404s. That refusal was correct, which is why the fix is a missing step and not
   *    a loosened check. It also returns the person's id **in this org**, which is a
   *    different id from the roster's and the only one the team add can use.
   * 2. **Into the TEAM** (`addTeamMember`) — the membership row, as before.
   *
   * The org step is gated behind an explicit confirmation (`addNeedsOrgJoin`), never
   * performed silently: bringing somebody into an organization is a bigger act than
   * putting an existing member on a team, and the button said only the latter.
   *
   * Whichever call is refused, its message is rendered INSIDE the modal and the
   * submitting state is released, so the dialog can be corrected or dismissed. A 404
   * from the team add is also treated as "not in this org" — the confirmation step is
   * offered on it, so a stale roster row (or a person the picker could not describe)
   * still reaches the same recovery instead of dead-ending.
   *
   * **The org-admin refusal is the server's, surfaced verbatim.** `addOrgMember` is
   * platform-admin-only (its roster is), and its 403 names what an org admin *can* do
   * and the access-request path for what they cannot. It is not restated as a
   * client-side rule here: this modal is already gated on `canAddMembers`, so a
   * hand-written refusal would be unreachable copy that could drift from the boundary
   * actually enforcing it.
   */
  const handleAddMember = async () => {
    if (!selectedOrgId || !addPersonId || !addTeamId) return;
    setIsAddingMember(true);
    setError(null);
    setAddMemberError(null);
    try {
      let memberId = addPersonId;
      if (addNeedsOrgJoin) {
        const joined = await addOrgMember(selectedOrgId, { userId: addPersonId, role: addRole });
        memberId = joined.id;
      }
      await addTeamMember(selectedOrgId, addTeamId, { userId: memberId });
      if (addRole === 'org_admin' && !addNeedsOrgJoin) {
        // Already a member, so the role is a change to an existing membership. When
        // the org step ran it carried the role itself, and repeating it here would be
        // a second write asserting what already holds.
        await assignUserRole({
          user_id: memberId,
          role: AdminRole.ORG_ADMIN,
          org_id: selectedOrgId,
        });
      }
      setNotice('Member added.');
      resetAddMember();
      await loadMembers(selectedOrgId, canReadMemberBudgets);
    } catch (err) {
      // The modal stays OPEN over a write that did not (fully) land, and says why
      // where the admin is looking — the page banner behind it is what made this
      // read as a dead button.
      setAddMemberError(errorMessage(err, 'Failed to add the member.'));
      if (isNotInThisOrg(err)) setAddNeedsOrgJoin(true);
    } finally {
      setIsAddingMember(false);
    }
  };

  const handleNameChange = (value: string) => {
    setNewOrgName(value);
    if (!identifierEdited) setNewOrgId(deriveOrgIdentifier(value));
  };

  const handleCreateOrg = async () => {
    const id = newOrgId.trim();
    const name = newOrgName.trim();
    if (!name || !isValidOrgIdentifier(id)) return;

    setIsCreating(true);
    setError(null);
    try {
      const created = await createOrganizationCanonical({ id, name });
      setNotice(
        `Created "${created.name}" (${created.id}) with its default department and team. No GitHub connection is required.`
      );
      setIsCreateOpen(false);
      setNewOrgName('');
      setNewOrgId('');
      setIdentifierEdited(false);
      await loadOrgs();
      setSelectedOrgId(created.id);
    } catch (err) {
      setError(errorMessage(err, 'Failed to create the organization.'));
    } finally {
      setIsCreating(false);
    }
  };

  const openDeptCreate = () => {
    setDeptName('');
    setDeptModal({ mode: 'create' });
  };

  const openDeptEdit = (dept: Department) => {
    setDeptName(dept.name);
    setDeptModal({ mode: 'edit', dept });
  };

  const handleSaveDept = async () => {
    if (!selectedOrgId || !deptModal || !deptName.trim()) return;
    setIsSavingDept(true);
    setError(null);
    try {
      if (deptModal.mode === 'create') {
        await createDepartment(selectedOrgId, { name: deptName.trim() });
        setNotice(`Added department "${deptName.trim()}".`);
      } else if (deptModal.dept) {
        await updateDepartment(selectedOrgId, deptModal.dept.id, { name: deptName.trim() });
        setNotice(`Renamed department to "${deptName.trim()}".`);
      }
      setDeptModal(null);
      setDeptName('');
      await loadDepartments(selectedOrgId);
    } catch (err) {
      setError(errorMessage(err, 'Failed to save the department.'));
    } finally {
      setIsSavingDept(false);
    }
  };

  const handleDeleteDept = async () => {
    if (!selectedOrgId || !deletingDept) return;
    setIsSavingDept(true);
    setError(null);
    try {
      await deleteDepartment(selectedOrgId, deletingDept.id);
      setNotice(`Deleted department "${deletingDept.name}".`);
      setDeletingDept(null);
      await loadDepartments(selectedOrgId);
    } catch (err) {
      setError(errorMessage(err, 'Failed to delete the department.'));
    } finally {
      setIsSavingDept(false);
    }
  };

  // Team mutations run against the SHIPPED client functions, which is why
  // `TeamManagement` can be reused verbatim: its prop contract is already these calls.
  // Each rethrows so the component keeps its modal open on failure rather than closing
  // over a write that did not happen.
  const handleCreateTeam = async (data: { name: string; description?: string }) => {
    if (!selectedOrgId || !selectedDeptId) return;
    setError(null);
    try {
      await createTeam(selectedOrgId, selectedDeptId, data);
      setNotice(`Added team "${data.name}".`);
      await loadTeams(selectedOrgId, selectedDeptId);
    } catch (err) {
      setError(errorMessage(err, 'Failed to create the team.'));
      throw err;
    }
  };

  const handleUpdateTeam = async (
    teamId: string,
    data: { name?: string; description?: string }
  ) => {
    if (!selectedOrgId || !selectedDeptId) return;
    setError(null);
    try {
      await updateTeam(selectedOrgId, teamId, data);
      setNotice('Team updated.');
      await loadTeams(selectedOrgId, selectedDeptId);
    } catch (err) {
      setError(errorMessage(err, 'Failed to update the team.'));
      throw err;
    }
  };

  const handleDeleteTeam = async (teamId: string) => {
    if (!selectedOrgId || !selectedDeptId) return;
    setError(null);
    try {
      await deleteTeam(selectedOrgId, teamId);
      setNotice('Team deleted.');
      await loadTeams(selectedOrgId, selectedDeptId);
    } catch (err) {
      setError(errorMessage(err, 'Failed to delete the team.'));
      throw err;
    }
  };

  // Affordance gate only — every route below is enforced server-side regardless.
  if (!canViewOrganizations()) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Organizations</h1>
        <p className="mt-4 text-gray-600 dark:text-gray-400">
          You do not have permission to view organizations. Ask a platform administrator if
          you need access.
        </p>
      </div>
    );
  }

  const query = search.trim().toLowerCase();
  const visibleOrgs = query
    ? orgs.filter(
        (o) => o.name.toLowerCase().includes(query) || o.id.toLowerCase().includes(query)
      )
    : orgs;

  const selectedOrg = orgs.find((o) => o.id === selectedOrgId) ?? null;
  const selectedDept = departments.find((d) => d.id === selectedDeptId) ?? null;

  // The Members tab's search — same client-side idiom as the org filter above.
  const memberQuery = memberSearch.trim().toLowerCase();
  const visibleMembers = memberQuery
    ? members.filter(
        (m) =>
          (m.name ?? '').toLowerCase().includes(memberQuery) ||
          (m.githubUsername ?? '').toLowerCase().includes(memberQuery) ||
          m.email.toLowerCase().includes(memberQuery)
      )
    : members;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Organizations</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-1">
            Create organizations and structure them into departments and teams — no GitHub
            required.
          </p>
        </div>
        {isPlatformAdmin() && (
          <Button onClick={() => setIsCreateOpen(true)}>+ Create organization</Button>
        )}
      </div>

      {notice && (
        <div
          className="rounded-md border border-green-300 bg-green-50 p-3 text-sm text-green-800 dark:border-green-600 dark:bg-green-900/20 dark:text-green-200"
          role="status"
        >
          {notice}
        </div>
      )}

      {error && (
        <div
          className="rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-800 dark:border-red-600 dark:bg-red-900/20 dark:text-red-200"
          role="alert"
        >
          {error}
        </div>
      )}

      <Card padding="none">
        <div className="flex items-center justify-between gap-4 border-b border-gray-200 p-4 dark:border-gray-700">
          <h2 className="font-semibold text-gray-900 dark:text-white">
            All organizations {isLoadingOrgs ? '' : `(${orgs.length})`}
          </h2>
          <Input
            name="org-search"
            aria-label="Search organizations"
            placeholder="Search…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="max-w-xs"
          />
        </div>

        {isLoadingOrgs ? (
          <div className="flex justify-center p-8">
            <Spinner />
          </div>
        ) : visibleOrgs.length === 0 ? (
          <p className="p-6 text-sm text-gray-500 dark:text-gray-400">
            {orgs.length === 0
              ? 'No organizations yet.'
              : 'No organizations match your search.'}
          </p>
        ) : (
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-800">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                  Organization
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                  Connections
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                  Created
                </th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {visibleOrgs.map((org) => (
                <tr
                  key={org.id}
                  className={org.id === selectedOrgId ? 'bg-primary-50 dark:bg-primary-900/20' : ''}
                >
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-900 dark:text-white">{org.name}</div>
                    {/* The immutable identifier, shown in full. It is the value that
                        appears in every routing scope string for this org, so an admin
                        must be able to read it without opening another screen. */}
                    <div className="font-mono text-xs text-gray-500 dark:text-gray-400">
                      {org.id}
                    </div>
                  </td>
                  <td className="space-x-1 px-4 py-3 text-sm">
                    {/* Read-only: attach/detach lifecycle is T3 (#4842). A GitHub-free org
                        with no routing destinations is first-class and fully functional —
                        ruling R1 — so it gets an explicit "platform-native" label rather
                        than a blank cell that reads as missing data. */}
                    {(org.githubInstallationIds?.length ?? 0) === 0 &&
                    org.awsAccounts.length === 0 ? (
                      <span className="italic text-gray-400 dark:text-gray-500">
                        none — platform-native
                      </span>
                    ) : (
                      <>
                        {(org.githubInstallationIds?.length ?? 0) > 0 && (
                          <Badge>
                            GitHub: {org.githubInstallationIds!.length}{' '}
                            {org.githubInstallationIds!.length === 1
                              ? 'installation'
                              : 'installations'}
                          </Badge>
                        )}
                        {org.awsAccounts.length > 0 && (
                          <Badge>
                            {org.awsAccounts.length} routing{' '}
                            {org.awsAccounts.length === 1 ? 'destination' : 'destinations'}
                          </Badge>
                        )}
                      </>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                    {formatDate(org.createdAt)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSelectedOrgId(org.id === selectedOrgId ? null : org.id)}
                    >
                      {org.id === selectedOrgId ? 'Close' : 'Open'}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="border-t border-gray-200 p-4 text-xs text-gray-500 dark:border-gray-700 dark:text-gray-400">
          An organization works fully without any GitHub connection. Connect one later from
          its Connections tab.
        </p>
      </Card>

      {selectedOrg && (
        <div className="space-y-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
              {selectedOrg.name}{' '}
              <span className="font-mono text-xs font-normal text-gray-500 dark:text-gray-400">
                {selectedOrg.id}
              </span>
            </h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Departments, teams, people, and service accounts.
            </p>
          </div>

          {/* The `Tabs` primitive owns which panel renders; `activeTab` mirrors its
              `onChange` purely so the member reads can be gated on the tab being open —
              opening an org should not fetch a roster nobody is looking at. */}
          <Tabs
            defaultValue="structure"
            onChange={(value) => setActiveTab(value as 'structure' | 'members')}
          >
            <TabsList>
              <Tab value="structure">Structure</Tab>
              <Tab value="members">Members</Tab>
            </TabsList>

            <TabPanel value="structure" className="space-y-4">
              {isLoadingDepartments ? (
                <div className="flex justify-center p-8">
                  <Spinner />
                </div>
              ) : (
                <DepartmentList
                  orgId={selectedOrg.id}
                  departments={departments}
                  canManage={canManage}
                  onCreateDepartment={openDeptCreate}
                  onEditDepartment={openDeptEdit}
                  onDeleteDepartment={setDeletingDept}
                />
              )}

              {/* Every organization starts with a default department and team. Rendered as
                  ordinary rows above; this line is why they are there, so their presence never
                  reads as an error (#4841 Design, consequence 2). */}
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Every organization starts with a default department and team — rename them or
                add your own.
              </p>

              {departments.length > 0 && (
                <div className="space-y-3">
                  <Select
                    name="department-select"
                    label="Teams in department"
                    value={selectedDeptId}
                    onChange={(e) => setSelectedDeptId(e.target.value)}
                    options={departments.map((d) => ({ value: d.id, label: d.name }))}
                    className="max-w-sm"
                  />

                  {isLoadingTeams ? (
                    <div className="flex justify-center p-8">
                      <Spinner />
                    </div>
                  ) : (
                    selectedDept && (
                      <TeamManagement
                        teams={teams}
                        canManage={canManage}
                        onCreateTeam={handleCreateTeam}
                        onUpdateTeam={handleUpdateTeam}
                        onDeleteTeam={handleDeleteTeam}
                      />
                    )
                  )}
                </div>
              )}
              <ServiceIdentityList key={selectedOrg.id} orgId={selectedOrg.id} />
            </TabPanel>

            <TabPanel value="members" className="space-y-3">
              <div className="flex items-center justify-between gap-4">
                {/* Client-side over the LOADED roster (name / GitHub ID / email) —
                    acceptable per the review; the load-more below is how the rest
                    of a large org gets into the filterable set. */}
                <Input
                  name="member-search"
                  aria-label="Search members"
                  placeholder="Search name or GitHub ID…"
                  value={memberSearch}
                  onChange={(e) => setMemberSearch(e.target.value)}
                  className="max-w-xs"
                />
                {canAddMembers ? (
                  <Button onClick={() => setIsAddMemberOpen(true)}>+ Add member</Button>
                ) : (
                  canManage && (
                    /* The org admin's refusal, stated rather than implied by an absent
                       button (#4943). Bringing a person INTO an organization is
                       platform-admin-only — both writes it needs are — so the affordance
                       stays hidden; but "no button and no explanation" is the same
                       dead end as the modal that never spoke. What an org admin CAN do
                       (assign people who are already members to teams, below) and how
                       to get somebody new in are both named. */
                    <p
                      className="text-xs text-gray-500 dark:text-gray-400"
                      data-testid="add-member-org-admin-refusal"
                    >
                      Only a platform administrator can add a person to this organization.
                      You can assign the members below to teams; to have somebody new added,
                      use an access request.
                    </p>
                  )
                )}
              </div>
              <MemberList
                members={visibleMembers}
                membershipsByUserId={membershipsByUserId}
                teams={orgTeams}
                budgets={memberBudgets}
                onAddTeam={handleAddTeam}
                onSetPrimary={handleSetPrimary}
                onRemoveTeam={handleRemoveTeam}
                onChangeRole={canManage ? handleChangeRole : undefined}
                onRemoveMember={canManage ? handleRemoveMember : undefined}
                availableRoles={assignableRoles}
                defaultTeamId={`${selectedOrg.id}-team-default`}
                teamsTruncated={orgTeamsTruncated}
                isLoading={isLoadingMembers}
                canManage={canManage}
              />
              {membersHasMore && (
                // Truncation is stated, not silent (M4): without this line member
                // #51 is invisible with nothing on screen saying the list is cut.
                <div className="flex items-center gap-3">
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    Showing {members.length} of {membersTotal} members
                  </span>
                  <Button
                    variant="secondary"
                    size="sm"
                    isLoading={isLoadingMoreMembers}
                    onClick={() =>
                      loadMembers(selectedOrg.id, canReadMemberBudgets, membersPage + 1, true)
                    }
                  >
                    Load more
                  </Button>
                </div>
              )}
            </TabPanel>
          </Tabs>
        </div>
      )}

      {/* Create-organization modal — GitHub-free, canonical route, caller-supplied id. */}
      <Modal
        isOpen={isCreateOpen}
        onClose={() => setIsCreateOpen(false)}
        title="Create organization"
      >
        <div className="space-y-4">
          <Input
            name="new-org-name"
            label="Name"
            required
            value={newOrgName}
            onChange={(e) => handleNameChange(e.target.value)}
            placeholder="e.g. Acme Corp"
          />
          <Input
            name="new-org-id"
            label="Identifier"
            required
            value={newOrgId}
            onChange={(e) => {
              setIdentifierEdited(true);
              setNewOrgId(e.target.value);
            }}
            className="font-mono"
            helperText="Suggested from the name — edit if needed. Cannot be changed later."
          />

          <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm text-blue-800 dark:border-blue-700 dark:bg-blue-900/20 dark:text-blue-200">
            No GitHub needed. The organization starts with a default department and team,
            ready for members — connect GitHub or AWS accounts later from its Connections
            tab.
          </div>

          <ModalFooter>
            <Button variant="secondary" onClick={() => setIsCreateOpen(false)} disabled={isCreating}>
              Cancel
            </Button>
            <Button
              onClick={handleCreateOrg}
              isLoading={isCreating}
              disabled={!newOrgName.trim() || !isValidOrgIdentifier(newOrgId)}
            >
              Create organization
            </Button>
          </ModalFooter>
        </div>
      </Modal>

      {/* Department create / rename modal. Teams get theirs from `TeamManagement`, which
          already owns that surface — duplicating it here would be the fork this story
          forbids. */}
      <Modal
        isOpen={deptModal !== null}
        onClose={() => setDeptModal(null)}
        title={deptModal?.mode === 'edit' ? 'Rename department' : 'Add department'}
      >
        <div className="space-y-4">
          <Input
            name="dept-name"
            label="Name"
            required
            value={deptName}
            onChange={(e) => setDeptName(e.target.value)}
            placeholder="e.g. Security"
          />
          <ModalFooter>
            <Button variant="secondary" onClick={() => setDeptModal(null)} disabled={isSavingDept}>
              Cancel
            </Button>
            <Button onClick={handleSaveDept} isLoading={isSavingDept} disabled={!deptName.trim()}>
              {deptModal?.mode === 'edit' ? 'Save' : 'Add department'}
            </Button>
          </ModalFooter>
        </div>
      </Modal>

      {/* Persistent confirmation, per the established admin-panel convention: deleting a
          department is not undoable and takes its teams with it. */}
      <Modal
        isOpen={deletingDept !== null}
        onClose={() => setDeletingDept(null)}
        title="Delete department"
        size="sm"
      >
        <div className="space-y-4">
          <p className="text-sm text-gray-700 dark:text-gray-300">
            Delete <strong>{deletingDept?.name}</strong>? This cannot be undone. A department
            with teams still in it may be refused by the server.
          </p>
          <ModalFooter>
            <Button variant="secondary" onClick={() => setDeletingDept(null)} disabled={isSavingDept}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleDeleteDept} isLoading={isSavingDept}>
              Delete
            </Button>
          </ModalFooter>
        </div>
      </Modal>

      {/* "+ Add member" — the mockup's assign-member modal (#4936 review, M2d).
          Person via the reused Bedrock-routing picker (GitHub-ID-first, server-side
          search over the platform roster), team from the org-wide list, role as the
          mockup's radio pair. The submit is T1's membership create plus, for "org
          admin", the same role call UserList uses — the server bounds both. */}
      <Modal
        isOpen={isAddMemberOpen}
        onClose={() => {
          if (!isAddingMember) resetAddMember();
        }}
        title={`Add member to ${selectedOrg?.name ?? 'organization'}`}
      >
        <div className="space-y-4">
          <PersonPicker
            label="Person"
            namePrefix="add-member-person"
            value={addPersonId}
            onChange={(userId, person) => {
              setAddPersonId(userId);
              setAddPerson(person);
              // A new pick invalidates whatever the last one was refused for — both the
              // message and the confirmation it may have raised.
              setAddMemberError(null);
              // The roster row's own org is the up-front signal, and only a row that
              // NAMES a different org counts: a missing row (`null`) or a blank `orgId`
              // means "not known", not "not in this org", and guessing "not a member"
              // from silence would put a grant-org-access question in front of an admin
              // who was only adding an existing member to a team. Those cases fall
              // through to the server's own 404, which is authoritative.
              setAddNeedsOrgJoin(
                !!person && !!person.orgId && !!selectedOrgId && person.orgId !== selectedOrgId
              );
            }}
            helperText="GitHub ID shown first when linked."
          />

          <div>
            <Select
              name="add-member-team"
              label="Team"
              value={addTeamId}
              onChange={(e) => setAddTeamId(e.target.value)}
              options={orgTeams.map((team) => ({ value: team.id, label: team.name }))}
              placeholder="Select a team…"
            />
            {orgTeamsTruncated && (
              <p
                className="mt-1 text-xs text-amber-600 dark:text-amber-400"
                data-testid="add-member-teams-truncated"
              >
                Not every team is listed — this organization has more teams than one page
                shows.
              </p>
            )}
          </div>

          <fieldset>
            <legend className="text-sm font-medium text-gray-700 dark:text-gray-300">Role</legend>
            <div className="mt-1 flex gap-4 text-sm text-gray-700 dark:text-gray-300">
              <label className="flex items-center gap-1">
                <input
                  type="radio"
                  name="add-member-role"
                  checked={addRole === 'member'}
                  onChange={() => setAddRole('member')}
                />
                Member
              </label>
              <label className="flex items-center gap-1">
                <input
                  type="radio"
                  name="add-member-role"
                  checked={addRole === 'org_admin'}
                  onChange={() => setAddRole('org_admin')}
                />
                Org admin
              </label>
            </div>
          </fieldset>

          {addNeedsOrgJoin && (
            /* The confirmation step (#4943). Naming the person, the org and the team is
               the whole point: this submit does something the button's old label did not
               say, and an admin has to be able to see that before it happens. */
            <Alert variant="warning" title="This person is not a member of this organization yet">
              <p data-testid="add-member-org-join-confirm">
                <strong>{describeAddPerson(addPerson, addPersonId)}</strong> is not a member of{' '}
                <strong>{selectedOrg?.name ?? 'this organization'}</strong> yet. Add them to the
                organization and to team{' '}
                <strong>{orgTeams.find((t) => t.id === addTeamId)?.name ?? 'the selected team'}</strong>?
              </p>
            </Alert>
          )}

          {addMemberError && (
            /* Inside the modal, not the page banner behind it — root cause 1 of #4943:
               the server's three 404s were rendered where the open dialog covered them,
               so a correct refusal read as a button that had stopped working. */
            <Alert variant="error" title="The member was not added">
              {/* `Alert` is itself the `role="alert"` live region, so the message is
                  announced without a second, nested one. */}
              <p data-testid="add-member-error">{addMemberError}</p>
            </Alert>
          )}

          <ModalFooter>
            <Button variant="secondary" onClick={resetAddMember} disabled={isAddingMember}>
              Cancel
            </Button>
            <Button
              onClick={handleAddMember}
              isLoading={isAddingMember}
              disabled={!addPersonId || !addTeamId}
            >
              {addNeedsOrgJoin ? 'Add to organization and team' : 'Add member'}
            </Button>
          </ModalFooter>
        </div>
      </Modal>
    </div>
  );
}
