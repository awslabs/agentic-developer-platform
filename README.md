# ADP - Agentic Developer Platform

A comprehensive platform for building and deploying AI-powered developer tools.

## Modules

| Module | Description | Status |
|--------|-------------|--------|
| [gateway](modules/gateway/) | Bedrock proxy with auth, budgets, rate limiting | Active |
| agent-runtime | Agent execution runtime | Planned |
| skill-registry | Skill/tool registry | Planned |
| observability | Metrics and tracing dashboard | Planned |

## Architecture

```
                          Internet
                              |
                    +---------v---------+
                    |    CloudFront     |
                    +----+----+----+----+
                         |    |    |
            +------------+    |    +------------+
            |                 |                 |
   /api/gateway/*      /api/agents/*    /api/skills/*
            |                 |                 |
            v                 v                 v
    +-------+-------+ +-------+-------+ +-------+-------+
    |    Gateway    | | Agent Runtime | | Skill Registry|
    |   (FastAPI)   | |   (FastAPI)   | |   (FastAPI)   |
    +-------+-------+ +-------+-------+ +-------+-------+
            |                 |                 |
            +--------+--------+---------+-------+
                     |                  |
              +------v------+    +------v------+
              |    EKS      |    |    RDS      |
              |  (Shared)   |    | (Per-module)|
              +-------------+    +-------------+
```

## Quick Start

### Prerequisites

- AWS CLI configured with admin access
- Terraform >= 1.5
- kubectl
- Node.js >= 18
- Python >= 3.11

### 1. Bootstrap Platform

```bash
# Set up Terraform state backend
./platform/scripts/bootstrap.sh

# Deploy shared infrastructure (VPC, EKS, ECR)
cd platform/infra
terraform init -backend-config="../../environments/dev/backend.tfvars"
terraform apply -var-file="../../environments/dev/platform.tfvars"
```

### 2. Deploy Gateway Module

```bash
# Deploy gateway infrastructure (RDS, Redis, Cognito, CloudFront)
cd modules/gateway/infra
terraform init -backend-config="../../../environments/dev/modules/gateway-backend.tfvars"
terraform apply -var-file="../../../environments/dev/modules/gateway.tfvars"

# Deploy to EKS
kubectl apply -f ../k8s/ -n adp-gateway
```

### 3. Add New Module

```bash
./platform/scripts/add-module.sh my-new-module
```

## Directory Structure

```
adp/
├── platform/           # Shared platform infrastructure
│   ├── infra/          # Terraform (VPC, EKS, ECR)
│   ├── k8s/            # Cluster-wide K8s resources
│   └── scripts/        # Platform scripts
│
├── libs/               # Shared libraries
│   ├── python/         # Python packages
│   └── typescript/     # TypeScript/React packages
│
├── modules/            # Application modules
│   ├── gateway/        # Bedrock Gateway
│   ├── agent-runtime/  # (Planned)
│   └── ...
│
├── environments/       # Environment configs (dev/staging/prod)
│
└── .github/workflows/  # CI/CD pipelines
```

## Development

See individual module READMEs for development instructions:

- [Gateway Development](modules/gateway/README.md)

## License

Private - Internal use only.
