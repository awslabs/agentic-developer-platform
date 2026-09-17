// Package adapters provides cloud provider adapters for multi-cloud GPU provisioning.
// Each adapter wraps the SkyPilot API client to provide a uniform interface for
// pricing, availability checking, and node lifecycle management.
package adapters

import (
	"context"
	"fmt"
	"math"
	"sort"
)

// NodeStatus represents the current status of a provisioned node.
type NodeStatus string

const (
	NodeStatusPending      NodeStatus = "pending"
	NodeStatusProvisioning NodeStatus = "provisioning"
	NodeStatusRunning      NodeStatus = "running"
	NodeStatusStopped      NodeStatus = "stopped"
	NodeStatusTerminated   NodeStatus = "terminated"
	NodeStatusError        NodeStatus = "error"
	NodeStatusUnknown      NodeStatus = "unknown"
)

// PriceInfo holds pricing and availability information for a GPU instance type
// on a specific cloud/region.
type PriceInfo struct {
	Cloud        string  `json:"cloud"`
	Region       string  `json:"region"`
	GPUType      string  `json:"gpuType"`
	GPUCount     int     `json:"gpuCount"`
	InstanceType string  `json:"instanceType"`
	HourlyCost   float64 `json:"hourlyCost"`
	SpotCost     float64 `json:"spotCost"`
	Available    bool    `json:"available"`
}

// NodeSpec defines the specification for provisioning a GPU node.
type NodeSpec struct {
	Cloud             string `json:"cloud"`
	GPUType           string `json:"gpuType"`
	GPUCount          int    `json:"gpuCount"`
	DiskSizeGB        int    `json:"diskSizeGB"`
	K8sVersion        string `json:"k8sVersion"`
	ClusterName       string `json:"clusterName"`
	Region            string `json:"region,omitempty"`
	SSMActivationID   string `json:"ssmActivationId,omitempty"`
	SSMActivationCode string `json:"ssmActivationCode,omitempty"`
	WireGuard         bool   `json:"wireGuard,omitempty"`
	UseSpot           bool   `json:"useSpot,omitempty"`
}

// NodeInfo holds information about a provisioned node returned by adapters.
type NodeInfo struct {
	InstanceID string     `json:"instanceId"`
	PublicIP   string     `json:"publicIP,omitempty"`
	Status     NodeStatus `json:"status"`
	Cloud      string     `json:"cloud"`
	Region     string     `json:"region,omitempty"`
}

// CloudAdapter is the interface that all cloud provider adapters must implement.
// Adapters are compiled into the controller binary and delegate provisioning
// to the SkyPilot API server running in the cluster.
type CloudAdapter interface {
	// Name returns the cloud provider name (e.g. "nebius", "lambda", "aws").
	Name() string

	// ListGPUPricing returns pricing information for a given GPU type across
	// all regions supported by this cloud provider.
	ListGPUPricing(ctx context.Context, gpuType string) ([]PriceInfo, error)

	// CheckAvailability checks whether GPUs of the given type are currently
	// available in the specified region.
	CheckAvailability(ctx context.Context, gpuType string, region string) (bool, error)

	// ProvisionNode provisions a new GPU node according to the given spec.
	// It returns the SkyPilot request ID that can be used to track progress.
	ProvisionNode(ctx context.Context, spec NodeSpec) (string, error)

	// TerminateNode terminates a node identified by its SkyPilot cluster name.
	// It returns the SkyPilot request ID for tracking the teardown.
	TerminateNode(ctx context.Context, clusterName string) (string, error)

	// GetNodeStatus returns the current status of a node by its SkyPilot cluster name.
	GetNodeStatus(ctx context.Context, clusterName string) (*NodeInfo, error)
}

// SelectionResult holds the result of cheapest-cloud selection.
type SelectionResult struct {
	Price   PriceInfo
	Adapter CloudAdapter
}

// SelectCheapest queries all adapters for pricing on the given GPU type and
// returns the cheapest available option. If preferSpot is true, spot pricing
// is preferred when available and cheaper.
//
// Returns an error if no adapter has available GPUs of the requested type.
func SelectCheapest(ctx context.Context, adapters []CloudAdapter, gpuType string, preferSpot bool) (*SelectionResult, error) {
	if len(adapters) == 0 {
		return nil, fmt.Errorf("no cloud adapters configured")
	}

	type candidate struct {
		price   PriceInfo
		adapter CloudAdapter
		cost    float64
	}

	var candidates []candidate

	for _, adapter := range adapters {
		prices, err := adapter.ListGPUPricing(ctx, gpuType)
		if err != nil {
			// Log but continue — one cloud being unavailable shouldn't block others.
			continue
		}

		for _, p := range prices {
			if !p.Available {
				continue
			}

			cost := p.HourlyCost
			if preferSpot && p.SpotCost > 0 && p.SpotCost < cost {
				cost = p.SpotCost
			}

			candidates = append(candidates, candidate{
				price:   p,
				adapter: adapter,
				cost:    cost,
			})
		}
	}

	if len(candidates) == 0 {
		return nil, fmt.Errorf("no available %s GPUs across any cloud provider", gpuType)
	}

	// Sort by cost ascending; break ties by cloud name for determinism.
	sort.Slice(candidates, func(i, j int) bool {
		if math.Abs(candidates[i].cost-candidates[j].cost) < 0.001 {
			return candidates[i].price.Cloud < candidates[j].price.Cloud
		}
		return candidates[i].cost < candidates[j].cost
	})

	best := candidates[0]
	return &SelectionResult{
		Price:   best.price,
		Adapter: best.adapter,
	}, nil
}

// SelectAllAvailable queries all adapters and returns all available GPU
// options sorted by price (cheapest first). This is useful for the provisioner
// to try fallback clouds if the cheapest one fails.
func SelectAllAvailable(ctx context.Context, adapters []CloudAdapter, gpuType string, preferSpot bool) ([]SelectionResult, error) {
	if len(adapters) == 0 {
		return nil, fmt.Errorf("no cloud adapters configured")
	}

	var results []SelectionResult

	for _, adapter := range adapters {
		prices, err := adapter.ListGPUPricing(ctx, gpuType)
		if err != nil {
			continue
		}

		for _, p := range prices {
			if !p.Available {
				continue
			}

			results = append(results, SelectionResult{
				Price:   p,
				Adapter: adapter,
			})
		}
	}

	if len(results) == 0 {
		return nil, fmt.Errorf("no available %s GPUs across any cloud provider", gpuType)
	}

	sort.Slice(results, func(i, j int) bool {
		ci := results[i].Price.HourlyCost
		cj := results[j].Price.HourlyCost
		if preferSpot {
			if results[i].Price.SpotCost > 0 && results[i].Price.SpotCost < ci {
				ci = results[i].Price.SpotCost
			}
			if results[j].Price.SpotCost > 0 && results[j].Price.SpotCost < cj {
				cj = results[j].Price.SpotCost
			}
		}
		if math.Abs(ci-cj) < 0.001 {
			return results[i].Price.Cloud < results[j].Price.Cloud
		}
		return ci < cj
	})

	return results, nil
}
