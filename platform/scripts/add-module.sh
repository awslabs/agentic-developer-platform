#!/bin/bash
#
# Scaffold a new ADP module
#
# Usage: ./add-module.sh <module-name>
#

set -e

MODULE_NAME="$1"

if [ -z "$MODULE_NAME" ]; then
    echo "Usage: $0 <module-name>"
    exit 1
fi

MODULE_DIR="modules/$MODULE_NAME"

if [ -d "$MODULE_DIR" ]; then
    echo "ERROR: Module $MODULE_NAME already exists"
    exit 1
fi

echo "Creating module: $MODULE_NAME"

# Create directory structure
mkdir -p "$MODULE_DIR"/{src,tests,infra/modules,k8s,docs}

# Create basic files
cat > "$MODULE_DIR/README.md" << INNER_EOF
# ADP $MODULE_NAME

## Overview

Description of $MODULE_NAME module.

## Development

\`\`\`bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run locally
uvicorn src.main:app --reload
\`\`\`

## Deployment

Deployed via GitHub Actions on push to main.
INNER_EOF

cat > "$MODULE_DIR/Dockerfile" << INNER_EOF
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY src/ src/

EXPOSE 8080
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
INNER_EOF

cat > "$MODULE_DIR/pyproject.toml" << INNER_EOF
[project]
name = "adp-$MODULE_NAME"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.100",
    "uvicorn>=0.23",
    "adp-common",
]

[project.optional-dependencies]
dev = ["pytest>=7.0", "ruff>=0.1"]
INNER_EOF

# Create K8s manifests
cat > "$MODULE_DIR/k8s/deployment.yaml" << INNER_EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $MODULE_NAME
  namespace: adp-$MODULE_NAME
spec:
  replicas: 2
  selector:
    matchLabels:
      app: $MODULE_NAME
  template:
    metadata:
      labels:
        app: $MODULE_NAME
    spec:
      containers:
        - name: $MODULE_NAME
          image: adp-$MODULE_NAME:latest
          ports:
            - containerPort: 8080
INNER_EOF

# Add namespace to platform
echo "---
apiVersion: v1
kind: Namespace
metadata:
  name: adp-$MODULE_NAME
  labels:
    app.kubernetes.io/part-of: adp
    app.kubernetes.io/component: $MODULE_NAME" >> platform/k8s/namespaces.yaml

echo ""
echo "Module $MODULE_NAME created at $MODULE_DIR"
echo ""
echo "Next steps:"
echo "  1. Add your code to $MODULE_DIR/src/"
echo "  2. Create GitHub workflow at .github/workflows/${MODULE_NAME}-ci.yml"
echo "  3. Add module-specific infra to $MODULE_DIR/infra/"
