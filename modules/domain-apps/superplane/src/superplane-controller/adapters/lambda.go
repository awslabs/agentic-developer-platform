package adapters

import (
	"context"
	"fmt"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// lambdaGPUPricing holds static pricing data for Lambda Labs GPU instances.
// Lambda is often the cheapest for H100 instances.
var lambdaGPUPricing = map[string][]PriceInfo{
	"H100": {
		{Cloud: "lambda", Region: "us-east-1", GPUType: "H100", GPUCount: 1, InstanceType: "gpu_1x_h100_sxm5", HourlyCost: 2.86, SpotCost: 0, Available: true},
		{Cloud: "lambda", Region: "us-east-1", GPUType: "H100", GPUCount: 8, InstanceType: "gpu_8x_h100_sxm5", HourlyCost: 22.88, SpotCost: 0, Available: true},
		{Cloud: "lambda", Region: "us-west-1", GPUType: "H100", GPUCount: 1, InstanceType: "gpu_1x_h100_sxm5", HourlyCost: 2.86, SpotCost: 0, Available: true},
		{Cloud: "lambda", Region: "us-west-1", GPUType: "H100", GPUCount: 8, InstanceType: "gpu_8x_h100_sxm5", HourlyCost: 22.88, SpotCost: 0, Available: true},
	},
	"A100": {
		{Cloud: "lambda", Region: "us-east-1", GPUType: "A100", GPUCount: 1, InstanceType: "gpu_1x_a100_sxm4", HourlyCost: 1.29, SpotCost: 0, Available: true},
		{Cloud: "lambda", Region: "us-east-1", GPUType: "A100", GPUCount: 8, InstanceType: "gpu_8x_a100_80gb_sxm4", HourlyCost: 10.32, SpotCost: 0, Available: true},
	},
	"A10": {
		{Cloud: "lambda", Region: "us-east-1", GPUType: "A10", GPUCount: 1, InstanceType: "gpu_1x_a10", HourlyCost: 0.75, SpotCost: 0, Available: true},
	},
}

// LambdaAdapter implements CloudAdapter for Lambda Labs.
type LambdaAdapter struct {
	client *skypilot.Client
}

// NewLambdaAdapter creates a new Lambda Labs adapter backed by the given
// SkyPilot API client.
func NewLambdaAdapter(client *skypilot.Client) *LambdaAdapter {
	return &LambdaAdapter{client: client}
}

// Name returns "lambda".
func (a *LambdaAdapter) Name() string { return "lambda" }

// ListGPUPricing returns known pricing for the requested GPU type on Lambda.
// Falls back to SkyPilot dynamic lookup if GPU type is not in static map.
func (a *LambdaAdapter) ListGPUPricing(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	prices, ok := lambdaGPUPricing[gpuType]
	if ok {
		result := make([]PriceInfo, len(prices))
		copy(result, prices)
		return result, nil
	}

	// GPU type not in static map — check SkyPilot for dynamic availability.
	return a.dynamicGPULookup(ctx, gpuType)
}

// CheckAvailability checks GPU availability on Lambda Labs.
// Falls back to SkyPilot dynamic check if GPU type is not in static map.
func (a *LambdaAdapter) CheckAvailability(ctx context.Context, gpuType string, _ string) (bool, error) {
	_, err := a.client.Health(ctx)
	if err != nil {
		return false, fmt.Errorf("lambda: skypilot health check failed: %w", err)
	}

	if prices, ok := lambdaGPUPricing[gpuType]; ok && len(prices) > 0 {
		return true, nil
	}

	return isCloudEnabled(ctx, a.client, "lambda")
}

// dynamicGPULookup queries SkyPilot to check if Lambda is enabled and returns
// a synthetic PriceInfo entry for the requested GPU type.
func (a *LambdaAdapter) dynamicGPULookup(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	enabled, err := isCloudEnabled(ctx, a.client, "lambda")
	if err != nil || !enabled {
		return nil, err
	}

	return []PriceInfo{
		{Cloud: "lambda", Region: "us-east-1", GPUType: gpuType, GPUCount: 1, InstanceType: "dynamic", HourlyCost: 0, Available: true},
	}, nil
}

// ProvisionNode provisions a GPU node on Lambda Labs via SkyPilot.
func (a *LambdaAdapter) ProvisionNode(ctx context.Context, spec NodeSpec) (string, error) {
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
		return "", fmt.Errorf("lambda: launch failed: %w", err)
	}
	return reqID, nil
}

// TerminateNode terminates a Lambda Labs node via SkyPilot.
func (a *LambdaAdapter) TerminateNode(ctx context.Context, clusterName string) (string, error) {
	reqID, err := a.client.Down(ctx, []string{clusterName}, false)
	if err != nil {
		return "", fmt.Errorf("lambda: terminate failed: %w", err)
	}
	return reqID, nil
}

// GetNodeStatus returns the current status of a Lambda Labs node.
func (a *LambdaAdapter) GetNodeStatus(ctx context.Context, clusterName string) (*NodeInfo, error) {
	clusters, err := a.client.Status(ctx, clusterName)
	if err != nil {
		return nil, fmt.Errorf("lambda: status check failed: %w", err)
	}

	if len(clusters) == 0 {
		return &NodeInfo{
			InstanceID: clusterName,
			Status:     NodeStatusTerminated,
			Cloud:      "lambda",
		}, nil
	}

	c := clusters[0]
	return &NodeInfo{
		InstanceID: c.Name,
		PublicIP:   c.Handle.HeadIP,
		Status:     mapClusterStatus(c.Status),
		Cloud:      "lambda",
		Region:     clusterRegion(c),
	}, nil
}
