/**
 * UserList Component Tests
 *
 * Issue #4019: role management was a stub. The "Assign Role" button never
 * rendered (it was gated on an `onAssignRole` prop nothing passed) and the
 * "Remove" button called an optional callback nothing passed — a silent no-op
 * with no confirmation in front of an irreversible privilege change.
 *
 * These tests assert the two things that were broken: that the actions exist
 * and are reachable per-row, and that each one goes through a confirmation step
 * before its callback fires.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UserList } from '@/components/org/UserList';
import { AdminRole, type UserRole } from '@/types';

const ROLES = ['member', 'dept_admin', 'org_admin'];

const orgAdmin: UserRole = {
  userId: 'user-admin',
  role: AdminRole.ORG_ADMIN,
  orgId: 'org-001',
  deptId: null,
  permissions: [],
  createdAt: '2026-01-01T00:00:00Z',
};

const member: UserRole = {
  userId: 'user-member',
  role: AdminRole.MEMBER,
  orgId: 'org-001',
  deptId: null,
  permissions: [],
  createdAt: '2026-01-02T00:00:00Z',
};

function renderList(props: Partial<Parameters<typeof UserList>[0]> = {}) {
  return render(
    <UserList
      users={[orgAdmin, member]}
      canManage
      availableRoles={ROLES}
      onChangeRole={vi.fn()}
      onRemoveRole={vi.fn()}
      {...props}
    />
  );
}

function rowFor(userId: string): HTMLElement {
  const row = screen.getByText(userId).closest('tr');
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

/** A row-scoped action button — the table and the dialog share button labels. */
function rowAction(userId: string, name: RegExp) {
  return within(rowFor(userId)).getByRole('button', { name });
}

function dialogAction(name: RegExp) {
  return within(screen.getByRole('dialog')).getByRole('button', { name });
}

describe('UserList', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('action visibility', () => {
    it('renders a per-row Change Role action when the caller can manage users', () => {
      renderList();

      // The pre-#4019 bug: one header-level "Assign Role" button that never
      // rendered. Actions must be per-row so they can name a target user.
      expect(rowAction('user-admin', /change role/i)).toBeInTheDocument();
      expect(rowAction('user-member', /change role/i)).toBeInTheDocument();
    });

    it('hides all actions from a caller who cannot manage users', () => {
      renderList({ canManage: false });

      expect(screen.queryByRole('button', { name: /change role/i })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /remove/i })).not.toBeInTheDocument();
    });

    it('offers no Remove action on a user who is already a member', () => {
      // Demoting a member to member is a no-op request; offering it invites the
      // admin to believe they revoked something.
      renderList();

      expect(rowAction('user-admin', /remove/i)).toBeInTheDocument();
      expect(
        within(rowFor('user-member')).queryByRole('button', { name: /remove/i })
      ).not.toBeInTheDocument();
    });
  });

  describe('change role flow', () => {
    it('does not call onChangeRole until the dialog is confirmed', async () => {
      const onChangeRole = vi.fn();
      renderList({ onChangeRole });

      await userEvent.click(rowAction('user-member', /change role/i));

      expect(screen.getByRole('dialog')).toBeInTheDocument();
      expect(onChangeRole).not.toHaveBeenCalled();
    });

    it('submits the selected role for the row that was clicked', async () => {
      const onChangeRole = vi.fn().mockResolvedValue(undefined);
      renderList({ onChangeRole });

      await userEvent.click(rowAction('user-member', /change role/i));
      await userEvent.selectOptions(screen.getByLabelText('Role'), 'dept_admin');
      await userEvent.click(dialogAction(/^change role$/i));

      expect(onChangeRole).toHaveBeenCalledTimes(1);
      expect(onChangeRole).toHaveBeenCalledWith(member, 'dept_admin');
    });

    it('offers only the roles the backend said this caller may assign', async () => {
      // The backend ceiling-filters GET /admin/users/roles, so an org admin is
      // offered no platform_admin. Rendering it anyway guarantees a 403.
      renderList();

      await userEvent.click(rowAction('user-member', /change role/i));

      const select = screen.getByLabelText('Role') as HTMLSelectElement;
      expect(Array.from(select.options).map((o) => o.value)).toEqual(ROLES);
    });

    it('disables submit while the new role still equals the current one', async () => {
      renderList();

      await userEvent.click(rowAction('user-admin', /change role/i));

      // The dialog opens preselected to the target's current role.
      expect(dialogAction(/^change role$/i)).toBeDisabled();

      await userEvent.selectOptions(screen.getByLabelText('Role'), 'dept_admin');
      expect(dialogAction(/^change role$/i)).toBeEnabled();
    });

    it('closes the dialog on success', async () => {
      const onChangeRole = vi.fn().mockResolvedValue(undefined);
      renderList({ onChangeRole });

      await userEvent.click(rowAction('user-member', /change role/i));
      await userEvent.selectOptions(screen.getByLabelText('Role'), 'org_admin');
      await userEvent.click(dialogAction(/^change role$/i));

      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    });

    it('keeps the dialog open when the request fails', async () => {
      // A 403 ("Cannot change your own role") must not read as a success.
      const onChangeRole = vi.fn().mockRejectedValue({ message: 'Cannot change your own role' });
      renderList({ onChangeRole });

      await userEvent.click(rowAction('user-member', /change role/i));
      await userEvent.selectOptions(screen.getByLabelText('Role'), 'org_admin');
      await userEvent.click(dialogAction(/^change role$/i));

      await waitFor(() => expect(onChangeRole).toHaveBeenCalled());
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('abandons the change when the dialog is cancelled', async () => {
      const onChangeRole = vi.fn();
      renderList({ onChangeRole });

      await userEvent.click(rowAction('user-member', /change role/i));
      await userEvent.click(dialogAction(/cancel/i));

      expect(onChangeRole).not.toHaveBeenCalled();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    });
  });

  describe('remove role flow', () => {
    it('requires confirmation before calling onRemoveRole', async () => {
      const onRemoveRole = vi.fn().mockResolvedValue(undefined);
      renderList({ onRemoveRole });

      await userEvent.click(rowAction('user-admin', /remove/i));

      // The pre-#4019 button fired straight through with no confirm step.
      expect(onRemoveRole).not.toHaveBeenCalled();
      expect(screen.getByRole('dialog')).toBeInTheDocument();

      await userEvent.click(dialogAction(/^remove role$/i));

      expect(onRemoveRole).toHaveBeenCalledTimes(1);
      expect(onRemoveRole).toHaveBeenCalledWith(orgAdmin);
    });

    it('names the user and the role being revoked in the confirmation', async () => {
      renderList();

      await userEvent.click(rowAction('user-admin', /remove/i));

      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveTextContent('user-admin');
      expect(dialog).toHaveTextContent(/org admin/i);
      expect(dialog).toHaveTextContent(/demoted to member/i);
    });

    it('does not call onRemoveRole when cancelled', async () => {
      const onRemoveRole = vi.fn();
      renderList({ onRemoveRole });

      await userEvent.click(rowAction('user-admin', /remove/i));
      await userEvent.click(dialogAction(/cancel/i));

      expect(onRemoveRole).not.toHaveBeenCalled();
    });
  });
});
