package adapters

import (
	"context"
	"fmt"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// awsGPUPricing holds static pricing data for AWS GPU instances.
// AWS is typically the most expensive but has the widest region coverage
// and most reliable availability.
var awsGPUPricing = map[string][]PriceInfo{
	"H100": {
		{Cloud: "aws", Region: "us-east-1", GPUType: "H100", GPUCount: 8, InstanceType: "p5.48xlarge", HourlyCost: 98.32, SpotCost: 40.00, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "H100", GPUCount: 8, InstanceType: "p5.48xlarge", HourlyCost: 98.32, SpotCost: 40.00, Available: true},
	},
	"A100": {
		{Cloud: "aws", Region: "us-east-1", GPUType: "A100", GPUCount: 1, InstanceType: "p4d.24xlarge", HourlyCost: 6.88, SpotCost: 2.75, Available: true},
		{Cloud: "aws", Region: "us-east-1", GPUType: "A100", GPUCount: 8, InstanceType: "p4d.24xlarge", HourlyCost: 32.77, SpotCost: 13.11, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "A100", GPUCount: 1, InstanceType: "p4d.24xlarge", HourlyCost: 6.88, SpotCost: 2.75, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "A100", GPUCount: 8, InstanceType: "p4d.24xlarge", HourlyCost: 32.77, SpotCost: 13.11, Available: true},
	},
	"A10G": {
		{Cloud: "aws", Region: "us-east-1", GPUType: "A10G", GPUCount: 1, InstanceType: "g5.xlarge", HourlyCost: 1.006, SpotCost: 0.40, Available: true},
		{Cloud: "aws", Region: "us-east-1", GPUType: "A10G", GPUCount: 4, InstanceType: "g5.12xlarge", HourlyCost: 5.672, SpotCost: 2.27, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "A10G", GPUCount: 1, InstanceType: "g5.xlarge", HourlyCost: 1.006, SpotCost: 0.40, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "A10G", GPUCount: 4, InstanceType: "g5.12xlarge", HourlyCost: 5.672, SpotCost: 2.27, Available: true},
	},
	"L4": {
		{Cloud: "aws", Region: "us-east-1", GPUType: "L4", GPUCount: 1, InstanceType: "g6.xlarge", HourlyCost: 0.80, SpotCost: 0.32, Available: true},
		{Cloud: "aws", Region: "us-east-1", GPUType: "L4", GPUCount: 4, InstanceType: "g6.12xlarge", HourlyCost: 4.60, SpotCost: 1.84, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "L4", GPUCount: 1, InstanceType: "g6.xlarge", HourlyCost: 0.80, SpotCost: 0.32, Available: true},
	},
	"T4": {
		{Cloud: "aws", Region: "us-east-1", GPUType: "T4", GPUCount: 1, InstanceType: "g4dn.xlarge", HourlyCost: 0.526, SpotCost: 0.16, Available: true},
		{Cloud: "aws", Region: "us-west-2", GPUType: "T4", GPUCount: 1, InstanceType: "g4dn.xlarge", HourlyCost: 0.526, SpotCost: 0.16, Available: true},
	},
}

// AWSAdapter implements CloudAdapter for Amazon Web Services.
type AWSAdapter struct {
	client *skypilot.Client
}

// NewAWSAdapter creates a new AWS adapter backed by the given SkyPilot API client.
func NewAWSAdapter(client *skypilot.Client) *AWSAdapter {
	return &AWSAdapter{client: client}
}

// Name returns "aws".
func (a *AWSAdapter) Name() string { return "aws" }

// ListGPUPricing returns known pricing for the requested GPU type on AWS.
// If the GPU type is not in the static pricing map, it queries SkyPilot to
// check if AWS is an enabled cloud and returns a dynamic entry allowing
// SkyPilot to handle the actual provisioning and pricing.
func (a *AWSAdapter) ListGPUPricing(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	prices, ok := awsGPUPricing[gpuType]
	if ok {
		result := make([]PriceInfo, len(prices))
		copy(result, prices)
		return result, nil
	}

	// GPU type not in static map — check SkyPilot for dynamic availability.
	return a.dynamicGPULookup(ctx, gpuType)
}

// CheckAvailability checks GPU availability on AWS.
// It first checks the static pricing map, then falls back to querying SkyPilot.
func (a *AWSAdapter) CheckAvailability(ctx context.Context, gpuType string, _ string) (bool, error) {
	_, err := a.client.Health(ctx)
	if err != nil {
		return false, fmt.Errorf("aws: skypilot health check failed: %w", err)
	}

	// Check static pricing first.
	if prices, ok := awsGPUPricing[gpuType]; ok && len(prices) > 0 {
		return true, nil
	}

	// Fall back to SkyPilot dynamic check.
	return isCloudEnabled(ctx, a.client, "aws")
}

// dynamicGPULookup queries SkyPilot to check if AWS is enabled and returns
// a synthetic PriceInfo entry for the requested GPU type.
func (a *AWSAdapter) dynamicGPULookup(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	enabled, err := isCloudEnabled(ctx, a.client, "aws")
	if err != nil || !enabled {
		return nil, err
	}

	// Return a dynamic entry — SkyPilot will handle actual pricing and placement.
	return []PriceInfo{
		{Cloud: "aws", Region: "us-east-1", GPUType: gpuType, GPUCount: 1, InstanceType: "dynamic", HourlyCost: 0, Available: true},
	}, nil
}

// ProvisionNode provisions a GPU node on AWS via SkyPilot.
func (a *AWSAdapter) ProvisionNode(ctx context.Context, spec NodeSpec) (string, error) {
	clusterName := fmt.Sprintf("sp-%s-%s-%d", spec.Cloud, spec.GPUType, spec.GPUCount)
	if spec.ClusterName != "" {
		clusterName = spec.ClusterName
	}

	task := buildSkyPilotTask(spec)

	reqID, err := a.client.Launch(ctx, skypilot.LaunchRequest{
		Task:        task,
		ClusterName: clusterName,
	})
	if err != nil {
		return "", fmt.Errorf("aws: launch failed: %w", err)
	}
	return reqID, nil
}

// TerminateNode terminates an AWS node via SkyPilot.
func (a *AWSAdapter) TerminateNode(ctx context.Context, clusterName string) (string, error) {
	reqID, err := a.client.Down(ctx, []string{clusterName}, false)
	if err != nil {
		return "", fmt.Errorf("aws: terminate failed: %w", err)
	}
	return reqID, nil
}

// GetNodeStatus returns the current status of an AWS node.
func (a *AWSAdapter) GetNodeStatus(ctx context.Context, clusterName string) (*NodeInfo, error) {
	clusters, err := a.client.Status(ctx, clusterName)
	if err != nil {
		return nil, fmt.Errorf("aws: status check failed: %w", err)
	}

	if len(clusters) == 0 {
		return &NodeInfo{
			InstanceID: clusterName,
			Status:     NodeStatusTerminated,
			Cloud:      "aws",
		}, nil
	}

	c := clusters[0]
	return &NodeInfo{
		InstanceID: c.Name,
		PublicIP:   c.Handle.HeadIP,
		Status:     mapClusterStatus(c.Status),
		Cloud:      "aws",
		Region:     clusterRegion(c),
	}, nil
}
