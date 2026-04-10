# Unit of Work — Story Map

## Story-to-Unit Assignment

| Story | Title | Unit |
|-------|-------|------|
| US-1.1 | Platform Initial Deployment | Unit 8: Infrastructure |
| US-1.2 | Configure Bedrock Account Pool | Unit 6: Admin API + Usage |
| US-1.3 | Onboard a New Organization | Unit 6: Admin API + Usage |
| US-1.4 | Human User Authentication via AWS SSO | Unit 1: Auth |
| US-1.5 | Service Account Registration | Unit 1: Auth + Unit 6: Admin API |
| US-1.6 | Automated Agent Authentication (M2M) | Unit 1: Auth |
| US-2.1 | Set Budgets at All Hierarchy Levels | Unit 4: Budget |
| US-2.2 | Department Admin Budget Management | Unit 4: Budget |
| US-2.3 | Budget Enforcement on Requests | Unit 4: Budget |
| US-2.4 | Budget Enforcement for Service Accounts | Unit 4: Budget |
| US-3.1 | Configure Rate Limits | Unit 5: Rate Limiting |
| US-3.2 | Rate Limit Enforcement | Unit 5: Rate Limiting |
| US-3.3 | Service Account Rate Limits | Unit 5: Rate Limiting |
| US-4.1 | OpenAI-Compatible Chat Completions | Unit 3: Proxy |
| US-4.2 | Anthropic Messages Format | Unit 3: Proxy |
| US-4.3 | Bedrock InvokeModel Pass-Through | Unit 3: Proxy |
| US-5.1 | Round-Robin Request Distribution | Unit 2: Bedrock Pool |
| US-6.1 | Claude Code Setup with apiKeyHelper | Unit 10: CLI Tools |
| US-6.2 | Claude Code in EKS Containers (M2M) | Unit 10: CLI Tools |
| US-6.3 | Admin UI Helper Script Download | Unit 7: Admin UI |
| US-7.1 | Admin UI Authentication | Unit 7: Admin UI |
| US-7.2 | Platform Admin Dashboard | Unit 7: Admin UI |
| US-7.3 | Org Admin Dashboard | Unit 7: Admin UI |
| US-7.4 | Log Viewer | Unit 7: Admin UI |
| US-8.1 | Request Logging | Unit 6: Admin API + Usage |
| US-8.2 | Prometheus Metrics | Unit 6: Admin API + Usage |
| US-9.1 | Expired AWS Credentials | Unit 10: CLI Tools |
| US-9.2 | Unknown Organization | Unit 1: Auth |
| US-9.3 | Budget Exceeded | Unit 4: Budget |
| US-9.4 | All Bedrock Accounts Unhealthy | Unit 2: Bedrock Pool |
| US-9.5 | Unregistered Service Account | Unit 1: Auth |
| US-9.6 | Model Not Allowed | Unit 3: Proxy |

## Coverage Validation

| Unit | Story Count | Stories |
|------|------------|---------|
| Unit 1: Auth | 5 | US-1.4, US-1.5, US-1.6, US-9.2, US-9.5 |
| Unit 2: Bedrock Pool | 2 | US-5.1, US-9.4 |
| Unit 3: Proxy | 4 | US-4.1, US-4.2, US-4.3, US-9.6 |
| Unit 4: Budget | 5 | US-2.1, US-2.2, US-2.3, US-2.4, US-9.3 |
| Unit 5: Rate Limiting | 3 | US-3.1, US-3.2, US-3.3 |
| Unit 6: Admin API + Usage | 7 | US-1.2, US-1.3, US-7.2, US-7.3, US-7.4, US-8.1, US-8.2 |
| Unit 7: Admin UI | 5 | US-6.3, US-7.1, US-7.2, US-7.3, US-7.4 |
| Unit 8: Infrastructure | 1 | US-1.1 |
| Unit 9: DevOps Pipelines | 0 | (cross-cutting — supports deployment of all units) |
| Unit 10: CLI Tools | 3 | US-6.1, US-6.2, US-9.1 |
| Unit 11: Test Generation | 24 | All stories (integration & e2e tests from acceptance criteria) |
| Unit 12: Env Provisioning | 0 | (cross-cutting — creates Dev, Test, Prod environments) |
| Unit 13: Test Runner | 24 | All stories (validates acceptance criteria pass) |

**Note**: US-1.5 spans Unit 1 (auth logic) and Unit 6 (admin API endpoint). US-7.2/7.3/7.4 span Unit 6 (API) and Unit 7 (UI). This is expected — the API provides data, the UI renders it.

**All 24 stories assigned. No orphan stories.**
