export const mockOrganizations = [
  {
    id: 'org-001',
    name: 'Engineering Corp',
    aws_accounts: ['111111111111', '222222222222'],
    role_mappings: {
      admin: 'arn:aws:iam::111111111111:role/AdminRole',
      developer: 'arn:aws:iam::111111111111:role/DevRole',
    },
    settings: {
      maxRequestsPerMinute: 1000,
      enableCostAlerts: true,
    },
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 'org-002',
    name: 'Research Labs',
    aws_accounts: ['333333333333'],
    role_mappings: {
      researcher: 'arn:aws:iam::333333333333:role/ResearchRole',
    },
    settings: {
      maxRequestsPerMinute: 500,
    },
    created_at: '2024-01-15T00:00:00Z',
  },
  {
    id: 'org-003',
    name: 'Data Science Team',
    aws_accounts: ['444444444444'],
    role_mappings: {},
    settings: {},
    created_at: '2024-02-01T00:00:00Z',
  },
];

export const mockDepartments = [
  {
    id: 'dept-001',
    org_id: 'org-001',
    name: 'Backend Team',
    description: 'Backend services development',
    created_at: '2024-01-10T00:00:00Z',
  },
  {
    id: 'dept-002',
    org_id: 'org-001',
    name: 'Frontend Team',
    description: 'Frontend development',
    created_at: '2024-01-11T00:00:00Z',
  },
  {
    id: 'dept-003',
    org_id: 'org-002',
    name: 'ML Research',
    description: 'Machine learning research',
    created_at: '2024-01-20T00:00:00Z',
  },
];

export const mockTeams = [
  {
    id: 'team-001',
    department_id: 'dept-001',
    name: 'API Team',
    description: 'API development',
    created_at: '2024-01-12T00:00:00Z',
  },
  {
    id: 'team-002',
    department_id: 'dept-001',
    name: 'Database Team',
    description: 'Database administration',
    created_at: '2024-01-13T00:00:00Z',
  },
  {
    id: 'team-003',
    department_id: 'dept-002',
    name: 'React Team',
    description: 'React frontend development',
    created_at: '2024-01-14T00:00:00Z',
  },
];

export const mockPoolAccounts = [
  {
    id: 'pool-001',
    account_id: '555555555555',
    role_arn: 'arn:aws:iam::555555555555:role/BedrockAccess',
    region: 'us-east-1',
    is_healthy: true,
    last_health_check: new Date().toISOString(),
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 'pool-002',
    account_id: '666666666666',
    role_arn: 'arn:aws:iam::666666666666:role/BedrockAccess',
    region: 'us-west-2',
    is_healthy: true,
    last_health_check: new Date().toISOString(),
    created_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 'pool-003',
    account_id: '777777777777',
    role_arn: 'arn:aws:iam::777777777777:role/BedrockAccess',
    region: 'us-east-1',
    is_healthy: false,
    last_health_check: new Date(Date.now() - 60000).toISOString(),
    created_at: '2024-01-01T00:00:00Z',
  },
];
