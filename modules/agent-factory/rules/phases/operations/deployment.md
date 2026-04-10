# Deployment - Detailed Steps

## Purpose
Deploy the system to target environment and establish operational readiness.

## Prerequisites
- Build and Test phase complete
- Deployment checklist approved
- Infrastructure code ready

## Primary Agent
@agent-operations (deploys) → @agent-developer (support)

---

# PART 1: DEPLOYMENT PLANNING

## Step 1: Load Context
- Read deployment-checklist.md
- Read architecture.md for infrastructure requirements
- Check infrastructure code exists

## Step 2: Create Deployment Plan
Generate `aidlc-docs/operations/plans/deployment-plan.md`:

```markdown
# Deployment Plan

## Target Environment
- **Environment**: [dev/staging/production]
- **Cloud Provider**: [AWS/GCP/Azure]
- **Region**: [Primary region]
- **Deployment Method**: [EKS/ECS/Lambda/EC2]

## Pre-Deployment Questions

### Environment Configuration
Is the target environment already provisioned?
- A) Yes, fully configured
- B) Partially configured (describe what's missing)
- C) No, needs full provisioning

[Answer]:

### Deployment Strategy
Which deployment strategy should be used?
- A) Blue-Green (zero downtime, instant rollback)
- B) Rolling (gradual replacement)
- C) Canary (small percentage first)
- D) Recreate (downtime acceptable)

[Answer]:

### Database Changes
Are there database migrations?
- A) No database changes
- B) Additive only (safe to apply)
- C) Breaking changes (requires coordination)

[Answer]:

### External Dependencies
Are all external dependencies ready?
- A) Yes, all configured
- B) Some need setup (list below)

[Answer]:

---

## Infrastructure Checklist

### Networking
- [ ] VPC configured
- [ ] Subnets (public/private) created
- [ ] Security groups defined
- [ ] Load balancer configured

### Compute
- [ ] EKS cluster ready (or target compute)
- [ ] Node groups configured
- [ ] Auto-scaling configured

### Data
- [ ] Database provisioned
- [ ] Backup configured
- [ ] Connection strings secured

### Security
- [ ] IAM roles created
- [ ] Secrets in Secrets Manager
- [ ] TLS certificates ready

### Monitoring
- [ ] CloudWatch configured
- [ ] Alarms defined
- [ ] Dashboards created
```

---

# PART 2: INFRASTRUCTURE PROVISIONING

## Step 3: Execute Infrastructure Code

@agent-operations runs infrastructure provisioning:

```bash
# Terraform example
cd infrastructure/

# Initialize
terraform init

# Plan
terraform plan -out=tfplan

# Apply (after review)
terraform apply tfplan
```

## Step 4: Verify Infrastructure
Create `aidlc-docs/operations/deployment/infrastructure-status.md`:

```markdown
# Infrastructure Status

## Provisioned Resources

### Networking
| Resource | ID | Status |
|----------|-----|--------|
| VPC | vpc-xxx | Active |
| Public Subnet | subnet-xxx | Active |
| Private Subnet | subnet-xxx | Active |
| NAT Gateway | nat-xxx | Active |

### Compute
| Resource | ID | Status |
|----------|-----|--------|
| EKS Cluster | [name] | Active |
| Node Group | [name] | 3/3 nodes ready |

### Data
| Resource | ID | Status |
|----------|-----|--------|
| RDS Instance | [name] | Available |
| ElastiCache | [name] | Available |

### Security
| Resource | ID | Status |
|----------|-----|--------|
| IAM Role (App) | [name] | Active |
| Secret (DB) | [name] | Active |

## Endpoints
- **API**: https://api.[domain]
- **Database**: [internal endpoint]
- **Cache**: [internal endpoint]

## Verification
- [ ] kubectl can connect to cluster
- [ ] Database connection works
- [ ] Secrets accessible
```

---

# PART 3: APPLICATION DEPLOYMENT

## Step 5: Deploy Application

@agent-operations deploys application:

```bash
# Kubernetes deployment
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secrets.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/ingress.yaml

# Verify deployment
kubectl rollout status deployment/[name] -n [namespace]
```

## Step 6: Run Database Migrations

```bash
# Run migrations
kubectl exec -it [pod] -- npm run db:migrate

# Verify
kubectl exec -it [pod] -- npm run db:status
```

## Step 7: Verify Deployment
Create `aidlc-docs/operations/deployment/deployment-status.md`:

```markdown
# Deployment Status

## Application Status

### Pods
| Pod | Status | Restarts | Age |
|-----|--------|----------|-----|
| app-xxx-1 | Running | 0 | 5m |
| app-xxx-2 | Running | 0 | 5m |

### Services
| Service | Type | External IP |
|---------|------|-------------|
| app-service | LoadBalancer | xxx.xxx.xxx.xxx |

### Ingress
| Host | Path | Backend |
|------|------|---------|
| api.domain.com | / | app-service:80 |

## Health Checks
- [ ] `/health` returns 200
- [ ] `/ready` returns 200
- [ ] Database connection verified
- [ ] External services reachable

## Smoke Tests
```bash
# Health check
curl https://api.[domain]/health
# Expected: {"status": "healthy"}

# Basic functionality
curl https://api.[domain]/api/[endpoint]
# Expected: [valid response]
```

## Deployment Complete
- **Deployed At**: [timestamp]
- **Version**: [version/commit]
- **Deployed By**: @agent-operations
```

---

# PART 4: MONITORING SETUP

## Step 8: Configure Monitoring
Create `aidlc-docs/operations/deployment/monitoring-setup.md`:

```markdown
# Monitoring Setup

## Dashboards

### Application Dashboard
- Request rate
- Error rate
- Response time (p50, p95, p99)
- Active connections

### Infrastructure Dashboard
- CPU utilization
- Memory utilization
- Network I/O
- Disk I/O

## Alarms

### Critical (Page immediately)
| Alarm | Condition | Action |
|-------|-----------|--------|
| High Error Rate | >5% errors for 5min | Page on-call |
| Service Down | Health check fails 3x | Page on-call |

### Warning (Notify)
| Alarm | Condition | Action |
|-------|-----------|--------|
| High Latency | p99 > 1s for 10min | Slack notify |
| High CPU | >80% for 15min | Slack notify |

## Log Aggregation
- **Tool**: CloudWatch Logs / ELK
- **Retention**: 30 days
- **Log Groups**: [list]

## Tracing
- **Tool**: X-Ray / Jaeger
- **Sampling Rate**: 10%
```

---

# PART 5: RUNBOOKS

## Step 9: Create Runbooks
Generate `aidlc-docs/operations/deployment/runbooks/`:

### common-operations.md
```markdown
# Common Operations Runbook

## Scaling

### Scale Up
```bash
kubectl scale deployment/[name] --replicas=5 -n [namespace]
```

### Scale Down
```bash
kubectl scale deployment/[name] --replicas=2 -n [namespace]
```

## Restarting

### Rolling Restart
```bash
kubectl rollout restart deployment/[name] -n [namespace]
```

### Force Restart
```bash
kubectl delete pod -l app=[name] -n [namespace]
```

## Logs

### View Logs
```bash
kubectl logs -f deployment/[name] -n [namespace]
```

### Search Logs
```bash
kubectl logs deployment/[name] -n [namespace] | grep "ERROR"
```
```

### incident-response.md
```markdown
# Incident Response Runbook

## Service Down

### Symptoms
- Health check failing
- 5xx errors in logs
- Alerts firing

### Diagnosis
1. Check pod status: `kubectl get pods -n [namespace]`
2. Check recent events: `kubectl get events -n [namespace]`
3. Check logs: `kubectl logs -l app=[name] -n [namespace]`

### Resolution
1. If pods crashing: Check resource limits, OOM
2. If pods pending: Check node capacity
3. If startup failing: Check configs, secrets

### Rollback
```bash
kubectl rollout undo deployment/[name] -n [namespace]
```
```

## Step 10: Final Documentation
Update project README with:
- Deployment status
- Access instructions
- Monitoring links
- Runbook links

## Step 11: Update State
```markdown
## Current Status
- Phase: Operations (Complete)
- Stage: Deployed and Monitored
- Completed: All AIDLC phases
- Status: Production Ready
```

## Step 12: Close Project
- Update all project board items to Done
- Create summary issue/document
- Archive AIDLC docs (or keep for reference)
- Notify stakeholders

---

# Background Tasks

| Task | Agent | Purpose |
|------|-------|---------|
| Monitor deployment | @agent-operations | Watch for issues |
| Documentation cleanup | @agent-reviewer | Final doc review |
| Handoff preparation | @agent-pm | Knowledge transfer |
