# GitHub Actions Runner Controller (ARC) on AWS EKS - Setup Guide

This guide covers setting up self-hosted GitHub Actions runners on AWS EKS using Actions Runner Controller (ARC).

## Prerequisites

- GitHub repository with access to create Personal Access Token (PAT)
- AWS EKS Cluster
- Helm installed on your system
- AWS CLI configured
- kubectl configured to access your cluster

## Step 1: Install Cert Manager on EKS Cluster

Cert-manager is required for ARC to function properly.

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.2/cert-manager.yaml
```

Verify the installation:

```bash
kubectl get pods --namespace cert-manager
```

## Step 2: Create GitHub Personal Access Token (PAT)

1. Go to GitHub: **Settings → Developer settings → Personal access tokens**
2. Select **Tokens (classic)** and click **Generate new token**
3. Give your token a name and select the required scopes:
   - `repo` scope for repository-level runners
   - `admin:org` scope for organization-level runners
4. Click **Generate token** and copy the token

## Step 3: Create Kubernetes Namespace and Secret

Create the namespace and secret for the actions-runner-controller:

```bash
kubectl create ns actions-runner-system

kubectl create secret generic controller-manager \
  -n actions-runner-system \
  --from-literal=github_token=<YOUR_GITHUB_PAT>
```

Replace `<YOUR_GITHUB_PAT>` with your actual token.

## Step 4: Install Actions Runner Controller (ARC)

Add the Helm repository and install ARC:

```bash
helm repo add actions-runner-controller https://actions-runner-controller.github.io/actions-runner-controller

helm repo update

helm upgrade --install --namespace actions-runner-system \
  --create-namespace --wait actions-runner-controller \
  actions-runner-controller/actions-runner-controller \
  --set syncPeriod=1m
```

Verify the installation:

```bash
kubectl get all -n actions-runner-system
```

## Step 5: Deploy Runner

Create a file named `runner.yml`:

```yaml
apiVersion: actions.summerwind.dev/v1alpha1
kind: RunnerDeployment
metadata:
  name: k8s-action-runner
  namespace: actions-runner-system
spec:
  replicas: 1
  template:
    spec:
      repository: <your-org>/<your-repo>
      labels:
        - "eks_runner"
```

Replace `<your-org>/<your-repo>` with your GitHub repository path.

Deploy the runner:

```bash
kubectl create -f runner.yml
```

Verify the runner pod is running:

```bash
kubectl get pod -n actions-runner-system | grep -i "k8s-action-runner"
```

Check GitHub: **Settings → Actions → Runners** to confirm the runner is registered.

## Step 6: Test the Runner with a Workflow

Create `.github/workflows/test.yml` in your repository:

```yaml
name: Testing

on:
  push:
    branches:
      - main

jobs:
  build:
    runs-on: eks_runner
    container:
      image: ubuntu:latest
    steps:
    - name: Checkout Repository
      uses: actions/checkout@v2
      with:
        ref: main

    - name: Echo Message
      run: echo "Hello World"
```

Push changes to trigger the workflow and verify it runs on your self-hosted runner.

---

## Alternative: Using Runner Scale Sets (Newer Approach)

For automatic scaling, you can use the newer Runner Scale Set approach:

### Install the Controller

```bash
export NAMESPACE="arc-systems"

helm install arc \
  --namespace "${NAMESPACE}" \
  --create-namespace \
  --set authSecret.create=true \
  --set authSecret.github_token="<YOUR_GITHUB_PAT>" \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller
```

### Configure Runner Scale Set

```bash
export INSTALLATION_NAME="arc-runner-set"
export RUNNERS_NAMESPACE="arc-runners"
export GITHUB_CONFIG_URL="https://github.com/<your_org_or_repo>"

helm install ${INSTALLATION_NAME} \
  --namespace "${RUNNERS_NAMESPACE}" \
  --create-namespace \
  --set githubConfigUrl="${GITHUB_CONFIG_URL}" \
  --set githubConfigSecret.github_token="${GITHUB_PAT}" \
  --set minRunners=1 \
  --set maxRunners=10 \
  oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set
```

### Verify Installation

```bash
# Check controller deployment
kubectl get pods -n arc-systems

# Check runner pods
kubectl get pods -n arc-runners

# View all Helm releases
helm list -A
```

---

## Sources

- [DevOpsCube - How to Setup GitHub Actions Runner on AWS EKS](https://devopscube.com/github-actions-runner-aws-eks/)
- [Kenny Brast - Setting up GitHub Actions Runner Controller (ARC) on Amazon EKS](https://kennybrast.medium.com/setting-up-github-actions-runner-controller-arc-on-amazon-eks-36a686ee6030)
