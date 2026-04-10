# Dev Environment Setup — Agent Instructions

These are executable instructions for an AI agent running inside a GitHub Actions runner pod on EKS (via ARC). The runner pod is an `actions-runner` container with IRSA-based AWS credentials.

## Execution Context

- Runner: `arc-runner-bedrock-gateway` on EKS Auto Mode cluster
- Container: `ghcr.io/actions/actions-runner:latest` (Ubuntu-based)
- IAM: IRSA role with Bedrock, Secrets Manager, CloudWatch, S3, RDS, ECR, EKS access
- Network: Private subnet with NAT gateway (outbound internet access)
- Working directory: `$GITHUB_WORKSPACE` (cloned repo root)
- Tools pre-installed by workflow: `node 24`, `kubectl`, `aws cli`, `gh cli`, `python3`, `pip3`
- Docker: Available via DinD sidecar or host socket (check `docker info`)

---

## STEP 0: DEPENDENCY PREFLIGHT CHECK (RUN THIS FIRST)

Before doing ANY work, run this preflight check. If any critical check fails, fix it immediately before proceeding. Past agent failures have been caused by skipping this step.

### 0A: Configuration & Secrets Resolution

The app depends on secrets and config values stored in AWS. Resolve these FIRST — without them, the backend won't start or auth won't work.

```bash
#!/bin/bash
set -e
echo "============================================"
echo "  CONFIGURATION DEPENDENCY RESOLUTION"
echo "============================================"

ENV="${DEPLOY_ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"

# --- 1. Cognito User Pool ID ---
echo -n "Cognito User Pool ID: "
COGNITO_POOL_ID=$(aws cognito-idp list-user-pools --max-results 50 \
  --query "UserPools[?Name=='bedrockgw-${ENV}'].Id | [0]" \
  --output text 2>/dev/null) || COGNITO_POOL_ID=""
if [ -z "$COGNITO_POOL_ID" ] || [ "$COGNITO_POOL_ID" = "None" ]; then
  echo "❌ NOT FOUND — auth endpoints will not work"
  echo "   Expected: bedrockgw-${ENV} user pool in ${AWS_REGION}"
  COGNITO_POOL_ID=""
else
  echo "✅ $COGNITO_POOL_ID"
fi
export BG_COGNITO_USER_POOL_ID="$COGNITO_POOL_ID"

# --- 2. Cognito Client ID ---
echo -n "Cognito Client ID: "
if [ -n "$COGNITO_POOL_ID" ]; then
  COGNITO_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
    --user-pool-id "$COGNITO_POOL_ID" \
    --query "UserPoolClients[?ClientName=='bedrockgw-${ENV}-web'].ClientId | [0]" \
    --output text 2>/dev/null) || COGNITO_CLIENT_ID=""
  if [ -z "$COGNITO_CLIENT_ID" ] || [ "$COGNITO_CLIENT_ID" = "None" ]; then
    # Fallback: try first client
    COGNITO_CLIENT_ID=$(aws cognito-idp list-user-pool-clients \
      --user-pool-id "$COGNITO_POOL_ID" \
      --query "UserPoolClients[0].ClientId" \
      --output text 2>/dev/null) || COGNITO_CLIENT_ID=""
  fi
fi
if [ -z "$COGNITO_CLIENT_ID" ] || [ "$COGNITO_CLIENT_ID" = "None" ]; then
  echo "❌ NOT FOUND"
  COGNITO_CLIENT_ID=""
else
  echo "✅ $COGNITO_CLIENT_ID"
fi
export BG_COGNITO_CLIENT_ID="$COGNITO_CLIENT_ID"

# --- 3. Cognito Domain ---
echo -n "Cognito Domain: "
COGNITO_DOMAIN="bedrockgw-${ENV}-auth"
if [ -n "$COGNITO_POOL_ID" ]; then
  RESOLVED_DOMAIN=$(aws cognito-idp describe-user-pool \
    --user-pool-id "$COGNITO_POOL_ID" \
    --query "UserPool.Domain" --output text 2>/dev/null) || true
  if [ -n "$RESOLVED_DOMAIN" ] && [ "$RESOLVED_DOMAIN" != "None" ]; then
    COGNITO_DOMAIN="$RESOLVED_DOMAIN"
  fi
fi
echo "✅ $COGNITO_DOMAIN"
export BG_COGNITO_DOMAIN="$COGNITO_DOMAIN"

# --- 4. Token Secret Key (JWT signing) ---
echo -n "Token Secret Key: "
TOKEN_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id "bedrockgw-${ENV}-token-secret" \
  --query SecretString --output text 2>/dev/null) || TOKEN_SECRET=""
if [ -z "$TOKEN_SECRET" ]; then
  # Generate a random one for dev/testing
  TOKEN_SECRET="${ENV}-secret-key-$(date +%s | sha256sum | head -c 16)"
  echo "⚠️  Not in Secrets Manager — generated ephemeral: ${TOKEN_SECRET:0:8}..."
else
  echo "✅ Retrieved from Secrets Manager"
fi
export BG_TOKEN_SECRET_KEY="$TOKEN_SECRET"

# --- 5. Agent Cognito Credentials (client_credentials flow) ---
echo -n "Agent Cognito Credentials: "
AGENT_CREDS=$(aws secretsmanager get-secret-value \
  --secret-id "bedrockgw-${ENV}-agent-cognito-credentials" \
  --query SecretString --output text 2>/dev/null) || AGENT_CREDS=""
if [ -z "$AGENT_CREDS" ]; then
  echo "⚠️  Not found — agent OAuth flow won't work"
else
  echo "✅ Retrieved"
fi

# --- 6. Database URL ---
echo -n "Database: "
# Option A: RDS in VPC (production-like)
RDS_HOST=$(aws rds describe-db-instances \
  --query "DBInstances[?DBInstanceIdentifier=='bedrockgw-${ENV}'].Endpoint.Address | [0]" \
  --output text 2>/dev/null) || RDS_HOST=""
if [ -n "$RDS_HOST" ] && [ "$RDS_HOST" != "None" ]; then
  echo "✅ RDS found: $RDS_HOST"
  export BG_DATABASE_URL="postgresql+asyncpg://bgadmin@${RDS_HOST}:5432/bedrockgateway"
  export BG_RDS_IAM_AUTH="true"
  export BG_RDS_HOST="$RDS_HOST"
else
  # Option B: SQLite for ephemeral dev
  echo "⚠️  No RDS found — using SQLite (ephemeral)"
  pip3 install aiosqlite -q 2>/dev/null
  export BG_DATABASE_URL="sqlite+aiosqlite:///./dev-local.db"
  export BG_RDS_IAM_AUTH="false"
fi

# --- 7. Redis URL (optional) ---
echo -n "Redis: "
REDIS_ENDPOINT=$(aws elasticache describe-cache-clusters \
  --query "CacheClusters[?starts_with(CacheClusterId,'bedrockgw-${ENV}')].CacheNodes[0].Endpoint.Address | [0]" \
  --output text 2>/dev/null) || REDIS_ENDPOINT=""
if [ -n "$REDIS_ENDPOINT" ] && [ "$REDIS_ENDPOINT" != "None" ]; then
  REDIS_AUTH=$(aws secretsmanager get-secret-value \
    --secret-id "bedrockgw-${ENV}-redis-auth" \
    --query SecretString --output text 2>/dev/null) || REDIS_AUTH=""
  if [ -n "$REDIS_AUTH" ]; then
    export BG_REDIS_URL="redis://:${REDIS_AUTH}@${REDIS_ENDPOINT}:6379/0"
    echo "✅ $REDIS_ENDPOINT (with auth)"
  else
    export BG_REDIS_URL="redis://${REDIS_ENDPOINT}:6379/0"
    echo "✅ $REDIS_ENDPOINT (no auth)"
  fi
else
  echo "⚠️  Not found — rate limiting will be disabled"
  export BG_REDIS_URL=""
fi

# --- 8. ECR Repository URL ---
echo -n "ECR Repository: "
ECR_REPO="bedrockgw-${ENV}-backend"
ECR_URL=$(aws ecr describe-repositories \
  --repository-names "$ECR_REPO" \
  --query "repositories[0].repositoryUri" \
  --output text 2>/dev/null) || ECR_URL=""
if [ -n "$ECR_URL" ] && [ "$ECR_URL" != "None" ]; then
  echo "✅ $ECR_URL"
else
  echo "⚠️  Not found — docker push will fail"
fi

# --- 9. EKS Cluster ---
echo -n "EKS Cluster: "
EKS_CLUSTER="bedrockgw-${ENV}-eks-cluster"
if aws eks describe-cluster --name "$EKS_CLUSTER" &>/dev/null 2>&1; then
  echo "✅ $EKS_CLUSTER"
  aws eks update-kubeconfig --name "$EKS_CLUSTER" --region "$AWS_REGION" 2>/dev/null || true
else
  echo "⚠️  Not found — kubectl operations will fail"
fi

# --- 10. CloudFront Domain (frontend redirect URI) ---
echo -n "CloudFront Domain: "
CF_DOMAIN=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='bedrockgw-${ENV}'].DomainName | [0]" \
  --output text 2>/dev/null) || CF_DOMAIN=""
if [ -n "$CF_DOMAIN" ] && [ "$CF_DOMAIN" != "None" ]; then
  echo "✅ $CF_DOMAIN"
  export VITE_REDIRECT_URI="https://${CF_DOMAIN}/auth/callback"
else
  echo "⚠️  Not found — using localhost for redirect URI"
  export VITE_REDIRECT_URI="http://localhost:5173/auth/callback"
fi

# --- 11. GitHub PAT (for agent operations) ---
echo -n "GitHub PAT: "
if [ -n "$GITHUB_TOKEN" ]; then
  echo "✅ Set via GITHUB_TOKEN env var"
elif [ -n "$GH_TOKEN" ]; then
  echo "✅ Set via GH_TOKEN env var"
else
  PAT=$(aws secretsmanager get-secret-value \
    --secret-id "github-ccsdk-agent-pat" \
    --query SecretString --output text 2>/dev/null | \
    python3 -c "import json,sys; print(json.load(sys.stdin).get('token',''))" 2>/dev/null) || PAT=""
  if [ -n "$PAT" ]; then
    export GITHUB_TOKEN="$PAT"
    export GH_TOKEN="$PAT"
    echo "✅ Retrieved from Secrets Manager"
  else
    echo "❌ No GitHub token available — git push/PR creation will fail"
  fi
fi

# --- Set remaining defaults ---
export BG_AWS_REGION="$AWS_REGION"
export BG_LOG_LEVEL="DEBUG"
export BG_LOG_FORMAT="text"
export BG_OTEL_ENABLED="false"
export BG_BUDGET_ENFORCEMENT_ENABLED="false"
export BG_RATELIMIT_ENFORCEMENT_ENABLED="false"
export CORS_ALLOWED_ORIGINS="https://${CF_DOMAIN:-localhost},http://localhost:5173"

echo ""
echo "============================================"
echo "  RESOLVED CONFIGURATION SUMMARY"
echo "============================================"
echo "BG_DATABASE_URL       = ${BG_DATABASE_URL}"
echo "BG_COGNITO_USER_POOL_ID = ${BG_COGNITO_USER_POOL_ID:-NOT SET}"
echo "BG_COGNITO_CLIENT_ID  = ${BG_COGNITO_CLIENT_ID:-NOT SET}"
echo "BG_COGNITO_DOMAIN     = ${BG_COGNITO_DOMAIN}"
echo "BG_TOKEN_SECRET_KEY   = ${BG_TOKEN_SECRET_KEY:0:8}..."
echo "BG_REDIS_URL          = ${BG_REDIS_URL:-DISABLED}"
echo "BG_RDS_IAM_AUTH       = ${BG_RDS_IAM_AUTH}"
echo "ECR_URL               = ${ECR_URL:-NOT FOUND}"
echo "EKS_CLUSTER           = ${EKS_CLUSTER}"
echo "CF_DOMAIN             = ${CF_DOMAIN:-NOT FOUND}"
echo "VITE_REDIRECT_URI     = ${VITE_REDIRECT_URI}"
echo "============================================"
```

### Configuration Dependency Matrix

| Config | Source | Required For | Failure If Missing |
|--------|--------|-------------|-------------------|
| `BG_COGNITO_USER_POOL_ID` | `aws cognito-idp list-user-pools` | Auth endpoints, token validation | Auth returns 401/500, login flow broken |
| `BG_COGNITO_CLIENT_ID` | `aws cognito-idp list-user-pool-clients` | OAuth flow, frontend login | Login redirect fails |
| `BG_COGNITO_DOMAIN` | Convention: `bedrockgw-{env}-auth` | OAuth hosted UI URL | Login page 404 |
| `BG_TOKEN_SECRET_KEY` | Secrets Manager: `bedrockgw-{env}-token-secret` | JWT signing/verification | All authenticated requests fail |
| `BG_DATABASE_URL` | RDS endpoint or SQLite fallback | All data persistence | App crashes on startup |
| `BG_REDIS_URL` | ElastiCache endpoint + `bedrockgw-{env}-redis-auth` | Rate limiting, caching | Rate limiting disabled (app still works) |
| `BG_RDS_IAM_AUTH` | Set `true` if using RDS, `false` for SQLite | DB authentication method | Connection refused if wrong |
| `BG_RDS_HOST` | `aws rds describe-db-instances` | IAM auth token generation | DB connection fails with IAM auth |
| ECR Repository URL | `aws ecr describe-repositories` | Docker image push | Deploy pipeline fails |
| EKS Cluster | `aws eks describe-cluster` | kubectl operations, deployment | Cannot deploy to K8s |
| CloudFront Domain | `aws cloudfront list-distributions` | Frontend redirect URI, CORS | OAuth callback fails |
| `GITHUB_TOKEN` | Workflow env or Secrets Manager: `github-ccsdk-agent-pat` | Git push, PR creation, issue comments | Agent can't push code or create PRs |
| Agent Cognito Creds | Secrets Manager: `bedrockgw-{env}-agent-cognito-credentials` | Agent OAuth client_credentials flow | Agent can't authenticate to API |

### Secrets Manager Keys Reference

| Secret ID | Contents | Used By |
|-----------|----------|---------|
| `bedrockgw-{env}-token-secret` | JWT signing key (plain string) | Backend `BG_TOKEN_SECRET_KEY` |
| `bedrockgw-{env}-agent-cognito-credentials` | JSON: `{client_id, client_secret}` | Agent OAuth flow |
| `bedrockgw-{env}-redis-auth` | Redis AUTH password (plain string) | Backend `BG_REDIS_URL` |
| `github-ccsdk-agent-pat` | JSON: `{token}` | Agent git operations |
| `github-ccsdk-agent/{secret_name}` | Various agent secrets | Agent infrastructure |

### Path Reference

| Path | Description |
|------|-------------|
| `src/` | Backend Python source (FastAPI) |
| `src/app.py` | Backend entry point (`create_app()`) |
| `src/shared/config.py` | Settings class — all `BG_` env vars defined here |
| `src/auth/` | Cognito auth routes and middleware |
| `src/proxy/` | Bedrock proxy routes |
| `src/admin/` | Admin API routes |
| `src/budget/` | Budget enforcement |
| `src/ratelimit/` | Rate limit enforcement |
| `tests/` | Backend test suite |
| `frontend/` | React + Vite frontend |
| `frontend/src/` | Frontend source |
| `frontend/.env.example` | Frontend env var template |
| `infra/` | Terraform infrastructure (VPC, EKS, RDS, Cognito, etc.) |
| `alembic/` | Database migrations |
| `alembic.ini` | Alembic config (points to `BG_DATABASE_URL`) |
| `pyproject.toml` | Python project config, dependencies, ruff/pytest settings |
| `Dockerfile` | Backend container image |
| `.github-agent/` | Agent SDK code (TypeScript) |
| `.github/workflows/` | CI/CD pipelines |
| `k8s/` | Kubernetes manifests (generated during deploy) |

### 0B: Package & Tool Preflight

Run this after 0A. If any critical check fails, fix it before proceeding.

```bash
#!/bin/bash
set -e
FAIL=0

echo "============================================"
echo "  DEPENDENCY PREFLIGHT CHECK"
echo "============================================"

# --- 1. Python version (must be >= 3.12) ---
echo -n "Python 3.12+: "
PYVER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
if python3 -c "import sys; exit(0 if sys.version_info >= (3,12) else 1)" 2>/dev/null; then
  echo "✅ $PYVER"
else
  echo "❌ Found $PYVER — need >= 3.12"
  FAIL=1
fi

# --- 2. pip3 available ---
echo -n "pip3: "
if command -v pip3 &>/dev/null; then
  echo "✅ $(pip3 --version | head -1)"
else
  echo "❌ pip3 not found. Fix: sudo apt-get update && sudo apt-get install -y python3-pip"
  FAIL=1
fi

# --- 3. Node.js (must be >= 20) ---
echo -n "Node.js 20+: "
if command -v node &>/dev/null; then
  NODEVER=$(node --version)
  NODEMAJOR=$(echo "$NODEVER" | sed 's/v//' | cut -d. -f1)
  if [ "$NODEMAJOR" -ge 20 ]; then
    echo "✅ $NODEVER"
  else
    echo "❌ Found $NODEVER — need >= 20"
    FAIL=1
  fi
else
  echo "❌ node not found. Fix: curl -fsSL https://deb.nodesource.com/setup_24.x | sudo -E bash - && sudo apt-get install -y nodejs"
  FAIL=1
fi

# --- 4. npm available ---
echo -n "npm: "
if command -v npm &>/dev/null; then
  echo "✅ $(npm --version)"
else
  echo "❌ npm not found (should come with node)"
  FAIL=1
fi

# --- 5. AWS CLI + credentials ---
echo -n "AWS CLI: "
if command -v aws &>/dev/null; then
  echo "✅ $(aws --version 2>&1 | head -1)"
else
  echo "❌ aws cli not found. Fix: curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o /tmp/awscliv2.zip && unzip -q /tmp/awscliv2.zip -d /tmp && sudo /tmp/aws/install"
  FAIL=1
fi

echo -n "AWS credentials (IRSA): "
if aws sts get-caller-identity &>/dev/null; then
  ROLE=$(aws sts get-caller-identity --query Arn --output text)
  echo "✅ $ROLE"
else
  echo "❌ No AWS credentials. Check IRSA service account annotation."
  FAIL=1
fi

# --- 6. git configured ---
echo -n "git user.email: "
if git config user.email &>/dev/null; then
  echo "✅ $(git config user.email)"
else
  echo "⚠️  Not set. Fix: git config --global user.email 'agent@bedrock-gateway.local' && git config --global user.name 'Agent'"
fi

# --- 7. gh CLI authenticated ---
echo -n "gh CLI: "
if command -v gh &>/dev/null; then
  echo "✅ $(gh --version | head -1)"
else
  echo "⚠️  gh not found. Fix: sudo apt-get install -y gh"
fi

echo -n "gh auth: "
if gh auth status &>/dev/null 2>&1; then
  echo "✅ authenticated"
else
  echo "⚠️  Not authenticated. Fix: export GH_TOKEN=\$GITHUB_TOKEN"
fi

# --- 8. Docker (optional but check) ---
echo -n "Docker: "
if docker info &>/dev/null 2>&1; then
  echo "✅ available"
else
  echo "⚠️  Not available (optional — use SQLite instead of PostgreSQL containers, skip docker build steps)"
fi

# --- 9. kubectl (optional) ---
echo -n "kubectl: "
if command -v kubectl &>/dev/null; then
  echo "✅ $(kubectl version --client --short 2>/dev/null || kubectl version --client 2>&1 | head -1)"
else
  echo "⚠️  Not found. Fix: curl -sLO 'https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl' && chmod +x kubectl && sudo mv kubectl /usr/local/bin/"
fi

# --- 10. Key Python packages installable (test pip can reach PyPI) ---
echo -n "PyPI reachable: "
if pip3 install --dry-run fastapi &>/dev/null 2>&1; then
  echo "✅"
else
  echo "❌ Cannot reach PyPI. Check network/NAT gateway."
  FAIL=1
fi

# --- 11. npm registry reachable ---
echo -n "npm registry: "
if npm ping &>/dev/null 2>&1; then
  echo "✅"
else
  echo "❌ Cannot reach npm registry. Check network/NAT gateway."
  FAIL=1
fi

echo ""
echo "============================================"
if [ $FAIL -ne 0 ]; then
  echo "  ❌ PREFLIGHT FAILED — fix issues above before proceeding"
  echo "============================================"
else
  echo "  ✅ ALL CHECKS PASSED — safe to proceed"
  echo "============================================"
fi
```

### Common Failures and Fixes

| Failure | Root Cause | Fix |
|---------|-----------|-----|
| `python3: command not found` | Runner image missing Python | `sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv` |
| `pip3 install` fails with SSL errors | Missing CA certs | `sudo apt-get install -y ca-certificates` |
| `pip3 install` fails with permission denied | System Python locked | Use `pip3 install --user` or `python3 -m venv .venv && source .venv/bin/activate` |
| `node: command not found` | Node not installed by workflow | `curl -fsSL https://deb.nodesource.com/setup_24.x \| sudo -E bash - && sudo apt-get install -y nodejs` |
| `npm ci` fails with EACCES | npm cache permissions | `npm ci --cache /tmp/.npm` |
| AWS `Unable to locate credentials` | IRSA not configured on service account | Check `kubectl describe sa github-runner-sa -n <namespace>` for `eks.amazonaws.com/role-arn` annotation |
| AWS `ExpiredTokenException` | Pod running too long, token expired | IRSA tokens auto-refresh — restart the step |
| `Cannot reach PyPI` / `npm registry` | NAT gateway down or security group blocking | Check VPC NAT gateway status in AWS console |
| `aiosqlite` import error | Forgot to install SQLite driver | `pip3 install aiosqlite` (not in default deps) |
| `ruff: command not found` | Dev deps not installed | `pip3 install -e ".[dev]"` (includes ruff) |
| `asyncpg` build fails | Missing `libpq-dev` for PostgreSQL driver | `sudo apt-get install -y libpq-dev` or use SQLite instead |
| Docker `permission denied` | No Docker socket or DinD not running | Skip Docker steps — use direct installs and SQLite |
| `gh: command not found` | gh CLI not pre-installed | See fix in preflight script above |
| Frontend `vite: not found` | `npm ci` not run | `cd frontend && npm ci` |

### Critical Dependencies by Component

**Backend (must have):**
- Python >= 3.12
- pip3
- `aiosqlite` (if using SQLite) OR `libpq-dev` + running PostgreSQL (if using asyncpg)
- Network access to PyPI

**Frontend (must have):**
- Node.js >= 20
- npm
- Network access to npm registry

**AWS operations (must have):**
- AWS CLI v2
- Valid IRSA credentials (auto-provided by pod service account)

**Git operations (must have):**
- git with user.email/user.name configured
- `GITHUB_TOKEN` or `GH_TOKEN` env var set

**Optional (degrade gracefully if missing):**
- Docker — skip container builds, use SQLite instead of PostgreSQL containers
- kubectl — skip any K8s deployment verification steps
- Redis — rate limiting features won't work but app starts fine

---

## Step 1: Verify Environment

```bash
# Confirm AWS identity (should show the IRSA runner role)
aws sts get-caller-identity

# Confirm tools
node --version        # >= 20
python3 --version     # >= 3.10
aws --version
kubectl version --client
gh --version
docker info 2>/dev/null && echo "Docker available" || echo "Docker NOT available"
```

## Step 2: Backend Setup

### 2.1 Install Python Dependencies

```bash
pip3 install -e ".[dev]"
```

### 2.2 Configure Environment Variables

The backend uses `BG_` prefixed env vars. For dev, use SQLite to avoid needing a PostgreSQL instance:

```bash
pip3 install aiosqlite

export BG_DATABASE_URL="sqlite+aiosqlite:///./dev-local.db"
export BG_TOKEN_SECRET_KEY="dev-agent-secret-key-$(date +%s)"
export BG_LOG_LEVEL="DEBUG"
export BG_LOG_FORMAT="text"
export BG_OTEL_ENABLED="false"
export BG_BUDGET_ENFORCEMENT_ENABLED="false"
export BG_RATELIMIT_ENFORCEMENT_ENABLED="false"
export BG_AWS_REGION="us-east-1"
```

To use the real Cognito config (for auth-dependent testing), pull from Secrets Manager or Terraform outputs:

```bash
# If Terraform state is accessible:
# cd infra && terraform output -json | python3 -c "
#   import json,sys; o=json.load(sys.stdin)
#   print(f'export BG_COGNITO_USER_POOL_ID={o[\"cognito_user_pool_id\"][\"value\"]}')
#   print(f'export BG_COGNITO_CLIENT_ID={o[\"cognito_user_pool_client_id\"][\"value\"]}')
#   print(f'export BG_COGNITO_DOMAIN={o[\"cognito_domain\"][\"value\"]}')
# "

# Or hardcode known dev values:
export BG_COGNITO_USER_POOL_ID=""
export BG_COGNITO_CLIENT_ID=""
export BG_COGNITO_DOMAIN=""
```

### 2.3 Run Database Migrations (if using PostgreSQL)

Skip this if using SQLite — tables are auto-created by SQLAlchemy.

```bash
# Only for PostgreSQL:
# alembic upgrade head
```

### 2.4 Start the Backend

```bash
# Start on port 8080 (background for agent use)
uvicorn src.app:create_app --factory --host 0.0.0.0 --port 8080 &
BACKEND_PID=$!
sleep 3

# Verify it's running
curl -s http://localhost:8080/health | python3 -c "import json,sys; print(json.load(sys.stdin))"
# Expected: {"status": "healthy"}

curl -s http://localhost:8080/ready | python3 -c "import json,sys; print(json.load(sys.stdin))"
# Expected: {"status": "ready"}
```


### 2.5 Run Backend Tests

```bash
# Run all tests (single execution, no watch mode)
python3 -m pytest tests/ -v --timeout=60

# Run specific module tests
python3 -m pytest tests/auth/ -v
python3 -m pytest tests/proxy/ -v
python3 -m pytest tests/admin/ -v

# With coverage
python3 -m pytest tests/ --cov=src --cov-report=term-missing
```

### 2.6 Lint Backend Code

```bash
ruff check src/ tests/ --fix
ruff format src/ tests/
```

Both must pass with zero errors — CI will reject otherwise.

---

## Step 3: Frontend Setup

### 3.1 Install Dependencies

```bash
cd frontend
npm ci
```

### 3.2 Configure Frontend Environment

```bash
cat > .env.local << 'EOF'
VITE_API_URL=http://localhost:8080
VITE_COGNITO_USER_POOL_ID=
VITE_COGNITO_CLIENT_ID=
VITE_COGNITO_DOMAIN=
VITE_COGNITO_REGION=us-east-1
VITE_REDIRECT_URI=http://localhost:5173/auth/callback
EOF
```

Fill in Cognito values if auth testing is needed (same values as backend).

### 3.3 Build Frontend (CI validation)

```bash
npm run build
# Output goes to frontend/dist/
```

### 3.4 Run Frontend Tests

```bash
npx vitest --run
```

### 3.5 Lint Frontend

```bash
npm run lint
```

### 3.6 Start Frontend Dev Server (interactive testing only)

```bash
# Only if you need to visually test — not for CI
npm run dev -- --host 0.0.0.0 &
# Runs on http://localhost:5173
```

---

## Step 4: Full Stack Verification

After both backend and frontend are running:

```bash
# Backend health
curl -s http://localhost:8080/health
# → {"status": "healthy"}

# Backend API docs (FastAPI auto-generated)
curl -s http://localhost:8080/docs -o /dev/null -w "%{http_code}"
# → 200

# Frontend build exists
ls -la frontend/dist/index.html
```

---

## Step 5: Docker Build (if Docker is available)

```bash
if docker info >/dev/null 2>&1; then
  # Build backend image
  docker build -t bedrockgateway:dev .

  # Run it
  docker run -d --name bg-dev \
    -p 8080:8080 \
    -e BG_DATABASE_URL="sqlite+aiosqlite:///./dev.db" \
    -e BG_TOKEN_SECRET_KEY="docker-dev-key" \
    -e BG_LOG_FORMAT="text" \
    -e BG_OTEL_ENABLED="false" \
    -e BG_BUDGET_ENFORCEMENT_ENABLED="false" \
    -e BG_RATELIMIT_ENFORCEMENT_ENABLED="false" \
    bedrockgateway:dev

  sleep 5
  curl -s http://localhost:8080/health
  docker logs bg-dev --tail 20

  # Cleanup
  docker stop bg-dev && docker rm bg-dev
else
  echo "Docker not available — skip container build"
fi
```

---

## Step 6: Cleanup

```bash
# Stop backend if running in background
kill $BACKEND_PID 2>/dev/null || true

# Remove SQLite dev database
rm -f dev-local.db

# Remove frontend build artifacts
rm -rf frontend/dist frontend/.env.local
```

---

## Environment Variable Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BG_DATABASE_URL` | Yes | `postgresql+asyncpg://postgres:postgres@localhost:5432/bedrockgw` | DB connection string. Use `sqlite+aiosqlite:///./dev.db` for local dev |
| `BG_TOKEN_SECRET_KEY` | Yes | (empty) | JWT signing key. Any random string for dev |
| `BG_COGNITO_USER_POOL_ID` | For auth | (empty) | Cognito User Pool ID |
| `BG_COGNITO_CLIENT_ID` | For auth | (empty) | Cognito App Client ID |
| `BG_COGNITO_DOMAIN` | For auth | (empty) | Cognito domain prefix |
| `BG_AWS_REGION` | No | `us-east-1` | AWS region |
| `BG_REDIS_URL` | No | (none) | Redis URL for rate limiting. Optional |
| `BG_LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARN`, `ERROR` |
| `BG_LOG_FORMAT` | No | `json` | `json` or `text` |
| `BG_OTEL_ENABLED` | No | `false` | Enable OpenTelemetry tracing |
| `BG_BUDGET_ENFORCEMENT_ENABLED` | No | `true` | Enable budget enforcement middleware |
| `BG_RATELIMIT_ENFORCEMENT_ENABLED` | No | `true` | Enable rate limit enforcement middleware |

## Constraints & Notes

- The runner pod has no persistent storage — all state is lost when the job ends
- SQLite is the simplest DB option for ephemeral agent runs; PostgreSQL requires the RDS instance in the VPC
- IRSA provides AWS credentials automatically — no `aws configure` needed
- The runner has outbound internet via NAT gateway but no inbound access
- `pip3 install aiosqlite` is needed if using SQLite (not in the default deps)
- Always run `ruff check` and `ruff format` before committing Python changes
- Frontend uses Vite on port 5173, backend uses uvicorn on port 8080
- CORS is pre-configured to allow `http://localhost:5173` → `http://localhost:8080`
