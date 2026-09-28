#!/bin/bash
# Deploy Unit 2: Networking & Auth Configuration
# Script: scripts/deploy-unit-2.sh

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
NAMESPACE="agent-mail"
CLUSTER_NAME="bedrockgw-dev-eks-cluster"
AWS_REGION="us-east-1"

echo -e "${BLUE}=== Unit 2: Networking & Auth Configuration Deployment ===${NC}"
echo "Namespace: $NAMESPACE"
echo "Cluster: $CLUSTER_NAME"
echo "Region: $AWS_REGION"
echo ""

# Function to print status
print_status() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check prerequisites
echo -e "${BLUE}Checking Prerequisites...${NC}"

# Check kubectl
if ! command -v kubectl &> /dev/null; then
    print_error "kubectl not found. Please install kubectl."
    exit 1
fi

# Check AWS CLI
if ! command -v aws &> /dev/null; then
    print_error "AWS CLI not found. Please install aws cli."
    exit 1
fi

# Update kubeconfig
echo -e "${BLUE}Updating kubeconfig for cluster: $CLUSTER_NAME${NC}"
if aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME"; then
    print_status "Kubeconfig updated successfully"
else
    print_error "Failed to update kubeconfig"
    exit 1
fi

# Verify cluster connectivity
echo -e "${BLUE}Verifying cluster connectivity...${NC}"
if kubectl cluster-info &> /dev/null; then
    print_status "Connected to Kubernetes cluster"
else
    print_error "Cannot connect to Kubernetes cluster"
    exit 1
fi

# Check if namespace exists
if kubectl get namespace "$NAMESPACE" &> /dev/null; then
    print_status "Namespace '$NAMESPACE' exists"
else
    print_error "Namespace '$NAMESPACE' does not exist. Run Unit 1 deployment first."
    exit 1
fi

# Verify Unit 1 is deployed
echo -e "${BLUE}Checking Unit 1 Prerequisites...${NC}"
REQUIRED_RESOURCES=("serviceaccount/agent-mail-sa" "pvc/agent-mail-data")

for resource in "${REQUIRED_RESOURCES[@]}"; do
    if kubectl get "$resource" -n "$NAMESPACE" &> /dev/null; then
        print_status "Found $resource"
    else
        print_error "Missing $resource. Please deploy Unit 1 first."
        exit 1
    fi
done

# Pre-deployment warnings
echo -e "${YELLOW}=== PRE-DEPLOYMENT WARNINGS ===${NC}"
print_warning "Please ensure you have updated the following before continuing:"
print_warning "1. Bearer token in k8s/agent-mail/secret.yaml (replace default values)"
print_warning "2. Domain name in k8s/agent-mail/ingress.yaml"
print_warning "3. ACM certificate ARN in k8s/agent-mail/ingress.yaml"
print_warning "4. CIDR restrictions in k8s/agent-mail/ingress.yaml"
echo ""

read -p "Have you updated the configuration files? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Please update the configuration files and run the script again."
    exit 1
fi

# Deploy Unit 2 resources
echo -e "${BLUE}=== Deploying Unit 2 Resources ===${NC}"

# Deploy ConfigMap
echo "Deploying ConfigMap..."
if kubectl apply -f k8s/agent-mail/configmap.yaml; then
    print_status "ConfigMap deployed successfully"
else
    print_error "Failed to deploy ConfigMap"
    exit 1
fi

# Deploy Secret
echo "Deploying Secret..."
if kubectl apply -f k8s/agent-mail/secret.yaml; then
    print_status "Secret deployed successfully"
else
    print_error "Failed to deploy Secret"
    exit 1
fi

# Update Service (add metrics port)
echo "Updating Service..."
if kubectl apply -f k8s/agent-mail/service.yaml; then
    print_status "Service updated successfully"
else
    print_error "Failed to update Service"
    exit 1
fi

# Update Deployment (add ConfigMap/Secret references)
echo "Updating Deployment..."
if envsubst < k8s/agent-mail/deployment.yaml | kubectl apply -f -; then
    print_status "Deployment updated successfully"
else
    print_error "Failed to update Deployment"
    exit 1
fi

# Wait for deployment rollout
echo "Waiting for deployment rollout..."
if kubectl rollout status deployment/agent-mail -n "$NAMESPACE" --timeout=300s; then
    print_status "Deployment rollout completed"
else
    print_error "Deployment rollout failed or timed out"
    exit 1
fi

# Deploy Ingress (after deployment is ready)
echo "Deploying ALB Ingress..."
if kubectl apply -f k8s/agent-mail/ingress.yaml; then
    print_status "Ingress created successfully"
else
    print_error "Failed to create Ingress"
    exit 1
fi

# Verification
echo -e "${BLUE}=== Verification ===${NC}"

# Check pod status
echo "Checking pod status..."
kubectl get pods -n "$NAMESPACE" -l app=mcp-agent-mail

# Check service status
echo "Checking service status..."
kubectl get svc -n "$NAMESPACE"

# Check ingress status
echo "Checking ingress status..."
kubectl get ingress -n "$NAMESPACE"

# Check secrets and configmaps
echo "Checking configuration resources..."
kubectl get secrets,configmaps -n "$NAMESPACE"

# Wait for ALB provisioning
echo -e "${BLUE}Waiting for ALB provisioning (this may take 2-3 minutes)...${NC}"
sleep 30

# Get ALB information
INGRESS_ADDRESS=""
for i in {1..10}; do
    INGRESS_ADDRESS=$(kubectl get ingress agent-mail-ingress -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || echo "")
    if [[ -n "$INGRESS_ADDRESS" ]]; then
        break
    fi
    echo "Waiting for ALB... (attempt $i/10)"
    sleep 20
done

if [[ -n "$INGRESS_ADDRESS" ]]; then
    print_status "ALB provisioned successfully: $INGRESS_ADDRESS"
    echo -e "${GREEN}ALB Endpoint: https://$INGRESS_ADDRESS/mail/health${NC}"
else
    print_warning "ALB not yet available. Check ALB controller logs:"
    echo "kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller"
fi

# Success message
echo ""
echo -e "${GREEN}=== Unit 2 Deployment Complete ===${NC}"
print_status "ConfigMap deployed with environment configuration"
print_status "Secret deployed with authentication tokens"
print_status "Service updated with metrics port (9090)"
print_status "Deployment updated with ConfigMap/Secret integration"
print_status "ALB Ingress deployed with HTTPS and CIDR restrictions"

echo ""
echo -e "${BLUE}=== Next Steps ===${NC}"
echo "1. Test health endpoint: kubectl port-forward -n $NAMESPACE svc/agent-mail-svc 8765:8765"
echo "   Then: curl http://localhost:8765/health"
echo ""
echo "2. Get bearer token for API testing:"
echo "   kubectl get secret agent-mail-auth -n $NAMESPACE -o jsonpath='{.data.bearer-token}' | base64 -d"
echo ""
echo "3. Test authenticated endpoint (once ALB is ready):"
echo "   curl -H 'Authorization: Bearer <token>' https://$INGRESS_ADDRESS/mail/api/status"
echo ""
echo "4. Monitor ALB provisioning:"
echo "   kubectl describe ingress agent-mail-ingress -n $NAMESPACE"
echo ""
echo -e "${YELLOW}⚠ Remember to implement bearer token authentication in your application code!${NC}"
echo "See: aidlc-docs/operations/networking-auth-implementation.md"

exit 0