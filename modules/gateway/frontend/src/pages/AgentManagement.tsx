/**
 * Agent Management Page
 *
 * Issue #119: Unified Cognito JWT Auth
 * - View and manage agents (Cognito App Clients) for M2M authentication
 * - Create new agents with client_credentials grant
 * - View credentials (one-time) for configuration
 * - Delete agents to revoke access
 */

import { useState, useEffect, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Table, type Column } from '@/components/ui/Table';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { useToast } from '@/contexts/ToastContext';
import { useAuthContext } from '@/contexts/AuthContext';
import {
  type Agent,
  type AgentCredentials,
  type CreateAgentRequest,
  listAgents,
  createAgent,
  getAgentCredentials,
  deleteAgent,
} from '@/services/agents';

// Create Agent Modal Component
function CreateAgentModal({
  isOpen,
  onClose,
  onSuccess,
  orgId,
}: {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: (agent: Agent) => void;
  orgId: string;
}) {
  const toast = useToast();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [formData, setFormData] = useState<CreateAgentRequest>({
    name: '',
    org_id: orgId,
    description: '',
    scopes: ['bedrockgw/invoke'],
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);

    try {
      const agent = await createAgent(formData);
      toast.success('Agent created successfully');
      onSuccess(agent);
      onClose();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to create agent';
      toast.error(message);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Create New Agent">
      <form onSubmit={handleSubmit} className="space-y-4">
        <Input
          label="Agent Name"
          value={formData.name}
          onChange={(e) => setFormData({ ...formData, name: e.target.value })}
          placeholder="e.g., my-agent, data-pipeline"
          required
        />
        <Input
          label="Description"
          value={formData.description || ''}
          onChange={(e) => setFormData({ ...formData, description: e.target.value })}
          placeholder="What is this agent used for?"
        />
        <Input
          label="Team ID (optional)"
          value={formData.team_id || ''}
          onChange={(e) => setFormData({ ...formData, team_id: e.target.value })}
          placeholder="Team ID for budget attribution"
        />

        <div className="flex justify-end gap-2 pt-4">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" isLoading={isSubmitting}>
            Create Agent
          </Button>
        </div>
      </form>
    </Modal>
  );
}

// Credentials Modal Component
function CredentialsModal({
  isOpen,
  onClose,
  credentials,
}: {
  isOpen: boolean;
  onClose: () => void;
  credentials: AgentCredentials | null;
}) {
  const toast = useToast();

  const copyToClipboard = (text: string, label: string) => {
    navigator.clipboard.writeText(text);
    toast.success(`${label} copied to clipboard`);
  };

  if (!credentials) return null;

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Agent Credentials" size="lg">
      <div className="space-y-4">
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
          <p className="text-yellow-800 font-medium">Important</p>
          <p className="text-yellow-700 text-sm">
            Store these credentials securely. The client secret cannot be retrieved again
            after closing this dialog.
          </p>
        </div>

        <div className="space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Client ID
            </label>
            <div className="flex gap-2">
              <code className="flex-1 bg-gray-100 px-3 py-2 rounded text-sm font-mono break-all">
                {credentials.client_id}
              </code>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => copyToClipboard(credentials.client_id, 'Client ID')}
              >
                Copy
              </Button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Client Secret
            </label>
            <div className="flex gap-2">
              <code className="flex-1 bg-gray-100 px-3 py-2 rounded text-sm font-mono break-all">
                {credentials.client_secret}
              </code>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => copyToClipboard(credentials.client_secret, 'Client Secret')}
              >
                Copy
              </Button>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Token Endpoint
            </label>
            <code className="block bg-gray-100 px-3 py-2 rounded text-sm font-mono break-all">
              {credentials.token_endpoint}
            </code>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Allowed Scopes
            </label>
            <div className="flex gap-2">
              {credentials.scopes.map((scope) => (
                <Badge key={scope} variant="info">
                  {scope}
                </Badge>
              ))}
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Example: Get Token
            </label>
            <pre className="bg-gray-900 text-gray-100 px-3 py-2 rounded text-xs overflow-x-auto">
              {credentials.example_curl}
            </pre>
          </div>
        </div>

        <div className="flex justify-end pt-4">
          <Button onClick={onClose}>Close</Button>
        </div>
      </div>
    </Modal>
  );
}

// Delete Confirmation Modal
function DeleteAgentModal({
  isOpen,
  onClose,
  agent,
  onConfirm,
}: {
  isOpen: boolean;
  onClose: () => void;
  agent: Agent | null;
  onConfirm: () => void;
}) {
  const [isDeleting, setIsDeleting] = useState(false);

  const handleDelete = async () => {
    setIsDeleting(true);
    await onConfirm();
    setIsDeleting(false);
  };

  if (!agent) return null;

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Delete Agent">
      <div className="space-y-4">
        <p className="text-gray-700">
          Are you sure you want to delete the agent <strong>{agent.name}</strong>?
        </p>
        <p className="text-red-600 text-sm">
          This will immediately revoke the agent&apos;s ability to obtain new tokens.
          Any existing tokens will remain valid until they expire.
        </p>

        <div className="flex justify-end gap-2 pt-4">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="danger"
            isLoading={isDeleting}
            onClick={handleDelete}
          >
            Delete Agent
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// Main Component
export function AgentManagement() {
  const { user } = useAuthContext();
  const toast = useToast();

  const [agents, setAgents] = useState<Agent[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  // Modal states
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCredentialsModal, setShowCredentialsModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [credentials, setCredentials] = useState<AgentCredentials | null>(null);

  const loadAgents = useCallback(async () => {
    setIsLoading(true);
    try {
      const response = await listAgents(user?.orgId, page);
      setAgents(response.items);
      setTotal(response.total);
      setHasMore(response.has_more);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to load agents';
      toast.error(message);
    } finally {
      setIsLoading(false);
    }
  }, [page, user?.orgId, toast]);

  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

  const handleViewCredentials = async (agent: Agent) => {
    try {
      const creds = await getAgentCredentials(agent.client_id);
      setCredentials(creds);
      setShowCredentialsModal(true);
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to load credentials';
      toast.error(message);
    }
  };

  const handleDeleteAgent = async () => {
    if (!selectedAgent) return;

    try {
      await deleteAgent(selectedAgent.client_id);
      toast.success('Agent deleted successfully');
      setShowDeleteModal(false);
      setSelectedAgent(null);
      loadAgents();
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to delete agent';
      toast.error(message);
    }
  };

  const columns: Column<Agent>[] = [
    {
      key: 'name',
      header: 'Name',
      render: (agent: Agent) => (
        <div>
          <div className="font-medium">{agent.name}</div>
          {agent.description && (
            <div className="text-sm text-gray-500">{agent.description}</div>
          )}
        </div>
      ),
    },
    {
      key: 'client_id',
      header: 'Client ID',
      render: (agent: Agent) => (
        <code className="text-sm bg-gray-100 px-2 py-1 rounded">
          {agent.client_id.substring(0, 12)}...
        </code>
      ),
    },
    {
      key: 'team_id',
      header: 'Team',
      render: (agent: Agent) => agent.team_id || '-',
    },
    {
      key: 'status',
      header: 'Status',
      render: (agent: Agent) => (
        <Badge variant={agent.status === 'active' ? 'success' : 'warning'}>
          {agent.status}
        </Badge>
      ),
    },
    {
      key: 'created_at',
      header: 'Created',
      render: (agent: Agent) => new Date(agent.created_at).toLocaleDateString(),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (agent: Agent) => (
        <div className="flex gap-2">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => handleViewCredentials(agent)}
          >
            Credentials
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => {
              setSelectedAgent(agent);
              setShowDeleteModal(true);
            }}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
            Agent Management
          </h1>
          <p className="text-gray-600 dark:text-gray-400">
            Manage machine-to-machine authentication for agents and services
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>Create Agent</Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Agents ({total})</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="flex justify-center py-8">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600" />
            </div>
          ) : agents.length === 0 ? (
            <div className="text-center py-8 text-gray-500">
              <p>No agents configured yet.</p>
              <p className="text-sm mt-2">
                Create an agent to enable M2M authentication for your services.
              </p>
            </div>
          ) : (
            <>
              <Table
                data={agents}
                columns={columns}
                keyExtractor={(agent) => agent.client_id}
              />
              {hasMore && (
                <div className="flex justify-center mt-4">
                  <Button variant="secondary" onClick={() => setPage(page + 1)}>
                    Load More
                  </Button>
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      {/* Modals */}
      <CreateAgentModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSuccess={(agent) => {
          loadAgents();
          // Show credentials immediately after creation
          handleViewCredentials(agent);
        }}
        orgId={user?.orgId || ''}
      />

      <CredentialsModal
        isOpen={showCredentialsModal}
        onClose={() => {
          setShowCredentialsModal(false);
          setCredentials(null);
        }}
        credentials={credentials}
      />

      <DeleteAgentModal
        isOpen={showDeleteModal}
        onClose={() => {
          setShowDeleteModal(false);
          setSelectedAgent(null);
        }}
        agent={selectedAgent}
        onConfirm={handleDeleteAgent}
      />
    </div>
  );
}

export default AgentManagement;
