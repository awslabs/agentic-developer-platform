package adapters

import (
	"context"
	"fmt"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// nebiusGPUPricing holds static pricing data for Nebius GPU instances.
// Prices are sourced from Nebius Cloud GPU pricing as of 2025.
// These serve as defaults; live pricing can be fetched via SkyPilot when available.
var nebiusGPUPricing = map[string][]PriceInfo{
	"H100": {
		{Cloud: "nebius", Region: "eu-north1", GPUType: "H100", GPUCount: 1, InstanceType: "gpu-h100-b", HourlyCost: 2.95, SpotCost: 0, Available: true},
		{Cloud: "nebius", Region: "eu-north1", GPUType: "H100", GPUCount: 2, InstanceType: "gpu-h100-b", HourlyCost: 5.90, SpotCost: 0, Available: true},
		{Cloud: "nebius", Region: "eu-north1", GPUType: "H100", GPUCount: 8, InstanceType: "gpu-h100-b", HourlyCost: 23.60, SpotCost: 0, Available: true},
	},
	"L40S": {
		{Cloud: "nebius", Region: "eu-north1", GPUType: "L40S", GPUCount: 1, InstanceType: "gpu-l40s-a", HourlyCost: 1.35, SpotCost: 0, Available: true},
		{Cloud: "nebius", Region: "eu-north1", GPUType: "L40S", GPUCount: 2, InstanceType: "gpu-l40s-a", HourlyCost: 2.70, SpotCost: 0, Available: true},
		{Cloud: "nebius", Region: "eu-north1", GPUType: "L40S", GPUCount: 8, InstanceType: "gpu-l40s-a", HourlyCost: 10.80, SpotCost: 0, Available: true},
	},
	"A100": {
		{Cloud: "nebius", Region: "eu-north1", GPUType: "A100", GPUCount: 1, InstanceType: "gpu-a100-b", HourlyCost: 2.20, SpotCost: 0, Available: true},
		{Cloud: "nebius", Region: "eu-north1", GPUType: "A100", GPUCount: 8, InstanceType: "gpu-a100-b", HourlyCost: 17.60, SpotCost: 0, Available: true},
	},
}

// NebiusAdapter implements CloudAdapter for Nebius Cloud.
// It uses the SkyPilot API client for provisioning and teardown, and maintains
// static pricing data as Nebius does not have a public pricing API.
type NebiusAdapter struct {
	client *skypilot.Client
}

// NewNebiusAdapter creates a new Nebius cloud adapter backed by the given
// SkyPilot API client.
func NewNebiusAdapter(client *skypilot.Client) *NebiusAdapter {
	return &NebiusAdapter{client: client}
}

// Name returns "nebius".
func (a *NebiusAdapter) Name() string { return "nebius" }

// ListGPUPricing returns known pricing for the requested GPU type on Nebius.
// Falls back to SkyPilot dynamic lookup if GPU type is not in static map.
func (a *NebiusAdapter) ListGPUPricing(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	prices, ok := nebiusGPUPricing[gpuType]
	if ok {
		// Return a copy to prevent mutation of the package-level map.
		result := make([]PriceInfo, len(prices))
		copy(result, prices)
		return result, nil
	}

	// GPU type not in static map — check SkyPilot for dynamic availability.
	return a.dynamicGPULookup(ctx, gpuType)
}

// CheckAvailability checks GPU availability on Nebius by querying the SkyPilot
// API for the cluster status. Since Nebius doesn't expose a real-time inventory
// API, this uses the pricing table as a heuristic and verifies SkyPilot health.
// Falls back to SkyPilot dynamic check if GPU type is not in static map.
func (a *NebiusAdapter) CheckAvailability(ctx context.Context, gpuType string, _ string) (bool, error) {
	// Verify SkyPilot is healthy before claiming availability.
	_, err := a.client.Health(ctx)
	if err != nil {
		return false, fmt.Errorf("nebius: skypilot health check failed: %w", err)
	}

	if prices, ok := nebiusGPUPricing[gpuType]; ok && len(prices) > 0 {
		return true, nil
	}

	return isCloudEnabled(ctx, a.client, "nebius")
}

// dynamicGPULookup queries SkyPilot to check if Nebius is enabled and returns
// a synthetic PriceInfo entry for the requested GPU type.
func (a *NebiusAdapter) dynamicGPULookup(ctx context.Context, gpuType string) ([]PriceInfo, error) {
	enabled, err := isCloudEnabled(ctx, a.client, "nebius")
	if err != nil || !enabled {
		return nil, err
	}

	return []PriceInfo{
		{Cloud: "nebius", Region: "eu-north1", GPUType: gpuType, GPUCount: 1, InstanceType: "dynamic", HourlyCost: 0, Available: true},
	}, nil
}

// ProvisionNode provisions a GPU node on Nebius via SkyPilot.
// It returns the SkyPilot request ID for tracking progress.
func (a *NebiusAdapter) ProvisionNode(ctx context.Context, spec NodeSpec) (string, error) {
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
		return "", fmt.Errorf("nebius: launch failed: %w", err)
	}
	return reqID, nil
}

// TerminateNode terminates a Nebius node via SkyPilot.
func (a *NebiusAdapter) TerminateNode(ctx context.Context, clusterName string) (string, error) {
	reqID, err := a.client.Down(ctx, []string{clusterName}, false)
	if err != nil {
		return "", fmt.Errorf("nebius: terminate failed: %w", err)
	}
	return reqID, nil
}

// GetNodeStatus returns the current status of a Nebius node via SkyPilot.
func (a *NebiusAdapter) GetNodeStatus(ctx context.Context, clusterName string) (*NodeInfo, error) {
	clusters, err := a.client.Status(ctx, clusterName)
	if err != nil {
		return nil, fmt.Errorf("nebius: status check failed: %w", err)
	}

	if len(clusters) == 0 {
		return &NodeInfo{
			InstanceID: clusterName,
			Status:     NodeStatusTerminated,
			Cloud:      "nebius",
		}, nil
	}

	c := clusters[0]
	return &NodeInfo{
		InstanceID: c.Name,
		PublicIP:   c.Handle.HeadIP,
		Status:     mapClusterStatus(c.Status),
		Cloud:      "nebius",
		Region:     clusterRegion(c),
	}, nil
}
