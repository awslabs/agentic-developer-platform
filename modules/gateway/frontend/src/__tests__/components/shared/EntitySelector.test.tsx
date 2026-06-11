/**
 * EntitySelector Component Tests
 *
 * Issue #220: Fix Admin UI Budget/RateLimit CRUD + Organization Page for Org Admins
 * Tests for the entity selector component that allows selecting entity types and IDs.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { EntitySelector } from '@/components/shared/EntitySelector';
import { EntityType } from '@/types';

// Mock the admin service. EntitySelector fetches org entities from Cognito via
// the backend, using the getCognito{Users,Teams,Departments} helpers.
vi.mock('@/services/admin', () => ({
  getCognitoUsers: vi.fn(),
  getCognitoTeams: vi.fn(),
  getCognitoDepartments: vi.fn(),
}));

import { getCognitoUsers, getCognitoTeams, getCognitoDepartments } from '@/services/admin';

const mockGetDepartments = getCognitoDepartments as ReturnType<typeof vi.fn>;
const mockGetTeams = getCognitoTeams as ReturnType<typeof vi.fn>;
const mockGetUsers = getCognitoUsers as ReturnType<typeof vi.fn>;

const defaultProps = {
  orgId: 'org-001',
  entityType: EntityType.TEAM,
  entityId: '',
  onEntityTypeChange: vi.fn(),
  onEntityIdChange: vi.fn(),
  disabled: false,
};

const renderComponent = (props: Partial<typeof defaultProps> = {}) => {
  return render(<EntitySelector {...defaultProps} {...props} />);
};

describe('EntitySelector', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockGetDepartments.mockResolvedValue({
      items: [
        { departmentId: 'dept-001', orgId: 'org-001' },
        { departmentId: 'dept-002', orgId: 'org-001' },
      ],
      total: 2,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetTeams.mockResolvedValue({
      items: [
        { groupName: 'Backend', description: 'Backend team' },
        { groupName: 'Frontend', description: 'Frontend team' },
      ],
      total: 2,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
    mockGetUsers.mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      pageSize: 100,
      hasMore: false,
    });
  });

  describe('Entity Type Selection', () => {
    it('renders entity type selector with label', () => {
      renderComponent();
      expect(screen.getByText('Entity Type')).toBeInTheDocument();
    });

    it('calls onEntityTypeChange when entity type is changed', async () => {
      const user = userEvent.setup();
      const onEntityTypeChange = vi.fn();
      renderComponent({ onEntityTypeChange });

      const selects = screen.getAllByRole('combobox');
      const entityTypeSelect = selects[0];
      await user.selectOptions(entityTypeSelect, EntityType.DEPARTMENT);

      expect(onEntityTypeChange).toHaveBeenCalledWith(EntityType.DEPARTMENT);
    });
  });

  describe('Entity ID Selection - User', () => {
    it('shows manual input for user type', async () => {
      renderComponent({ entityType: EntityType.USER });

      await waitFor(() => {
        // User type shows text input
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Entity ID Selection - Team', () => {
    it('fetches teams when entity type is team', async () => {
      renderComponent({ entityType: EntityType.TEAM });

      await waitFor(() => {
        expect(mockGetTeams).toHaveBeenCalledWith('org-001', { pageSize: 100 });
      });
    });
  });

  describe('Entity ID Selection - Department', () => {
    it('fetches departments when entity type is department', async () => {
      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        expect(mockGetDepartments).toHaveBeenCalledWith('org-001');
      });
    });
  });

  describe('Loading State', () => {
    it('shows loading state while fetching entities', () => {
      // Make the request hang
      mockGetDepartments.mockImplementation(() => new Promise(() => {}));

      renderComponent({ entityType: EntityType.DEPARTMENT });

      expect(screen.getByText(/loading entities/i)).toBeInTheDocument();
    });
  });

  describe('Error Handling', () => {
    it('falls back to manual input on fetch error', async () => {
      mockGetDepartments.mockRejectedValue(new Error('Fetch failed'));

      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        // Should show text input after error
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });
  });

  describe('Empty Results', () => {
    it('falls back to manual input when no entities found', async () => {
      mockGetDepartments.mockResolvedValue({
        items: [],
        total: 0,
        page: 1,
        pageSize: 100,
        hasMore: false,
      });

      renderComponent({ entityType: EntityType.DEPARTMENT });

      await waitFor(() => {
        // Should show text input when no entities found
        const inputs = screen.getAllByRole('textbox');
        expect(inputs.length).toBeGreaterThan(0);
      });
    });
  });
});
