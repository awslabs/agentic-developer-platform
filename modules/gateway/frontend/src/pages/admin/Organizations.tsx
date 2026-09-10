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
 */

import { useCallback, useEffect, useState } from 'react';
import { Badge, Button, Card, Input, Modal, ModalFooter, Select, Spinner } from '@/components/ui';
import { DepartmentList } from '@/components/org/DepartmentList';
import { TeamManagement } from '@/components/department/TeamManagement';
import {
  createDepartment,
  createOrganizationCanonical,
  createTeam,
  deleteDepartment,
  deleteTeam,
  getDepartments,
  getOrganizations,
  getTeams,
  updateDepartment,
  updateTeam,
} from '@/services/admin';
import { deriveOrgIdentifier, isValidOrgIdentifier } from '@/utils/orgIdentifier';
import { usePermissions } from '@/hooks/usePermissions';
import { formatDate } from '@/utils/format';
import type { Department, Organization, Team } from '@/types';

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

  const canManage = canUpdateOrganizations();
  const canView = canViewOrganizations();

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
              Structure: departments and the teams inside them.
            </p>
          </div>

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
            Every organization starts with a default department and team — rename them or add
            your own.
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
    </div>
  );
}
