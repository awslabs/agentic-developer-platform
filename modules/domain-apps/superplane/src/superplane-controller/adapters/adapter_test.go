package adapters

import (
	"context"
	"testing"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
)

// mockAdapter is a test-only CloudAdapter with configurable pricing.
type mockAdapter struct {
	name   string
	prices map[string][]PriceInfo
}

func (m *mockAdapter) Name() string { return m.name }

func (m *mockAdapter) ListGPUPricing(_ context.Context, gpuType string) ([]PriceInfo, error) {
	prices, ok := m.prices[gpuType]
	if !ok {
		return nil, nil
	}
	return prices, nil
}

func (m *mockAdapter) CheckAvailability(_ context.Context, _ string, _ string) (bool, error) {
	return true, nil
}

func (m *mockAdapter) ProvisionNode(_ context.Context, _ NodeSpec) (string, error) {
	return "req-123", nil
}

func (m *mockAdapter) TerminateNode(_ context.Context, _ string) (string, error) {
	return "req-456", nil
}

func (m *mockAdapter) GetNodeStatus(_ context.Context, _ string) (*NodeInfo, error) {
	return &NodeInfo{Status: NodeStatusRunning, Cloud: m.name}, nil
}

func TestSelectCheapest_BasicSelection(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{
			name: "expensive-cloud",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "expensive-cloud", Region: "us-east-1", GPUType: "H100", GPUCount: 1, InstanceType: "big", HourlyCost: 10.00, Available: true}},
			},
		},
		&mockAdapter{
			name: "cheap-cloud",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cheap-cloud", Region: "eu-north1", GPUType: "H100", GPUCount: 1, InstanceType: "small", HourlyCost: 2.50, Available: true}},
			},
		},
	}

	result, err := SelectCheapest(context.Background(), adapters, "H100", false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Price.Cloud != "cheap-cloud" {
		t.Errorf("expected cheap-cloud, got %s", result.Price.Cloud)
	}
	if result.Price.HourlyCost != 2.50 {
		t.Errorf("expected cost 2.50, got %f", result.Price.HourlyCost)
	}
	if result.Adapter.Name() != "cheap-cloud" {
		t.Errorf("expected adapter cheap-cloud, got %s", result.Adapter.Name())
	}
}

func TestSelectCheapest_PreferSpot(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{
			name: "on-demand",
			prices: map[string][]PriceInfo{
				"A100": {{Cloud: "on-demand", Region: "us-east-1", GPUType: "A100", GPUCount: 1, HourlyCost: 3.00, SpotCost: 0, Available: true}},
			},
		},
		&mockAdapter{
			name: "spot-available",
			prices: map[string][]PriceInfo{
				"A100": {{Cloud: "spot-available", Region: "us-west-2", GPUType: "A100", GPUCount: 1, HourlyCost: 6.88, SpotCost: 2.00, Available: true}},
			},
		},
	}

	result, err := SelectCheapest(context.Background(), adapters, "A100", true)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.Price.Cloud != "spot-available" {
		t.Errorf("expected spot-available, got %s", result.Price.Cloud)
	}
}

func TestSelectCheapest_NoAvailableGPUs(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{
			name: "cloud-a",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-a", GPUType: "H100", HourlyCost: 5.00, Available: false}},
			},
		},
	}

	_, err := SelectCheapest(context.Background(), adapters, "H100", false)
	if err == nil {
		t.Fatal("expected error for no available GPUs")
	}
}

func TestSelectCheapest_NoAdapters(t *testing.T) {
	_, err := SelectCheapest(context.Background(), nil, "H100", false)
	if err == nil {
		t.Fatal("expected error for empty adapters")
	}
}

func TestSelectCheapest_UnknownGPUType(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{name: "cloud-a", prices: map[string][]PriceInfo{}},
	}

	_, err := SelectCheapest(context.Background(), adapters, "H200", false)
	if err == nil {
		t.Fatal("expected error for unknown GPU type")
	}
}

func TestSelectCheapest_TieBreaking(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{
			name: "cloud-b",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-b", GPUType: "H100", HourlyCost: 2.50, Available: true}},
			},
		},
		&mockAdapter{
			name: "cloud-a",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-a", GPUType: "H100", HourlyCost: 2.50, Available: true}},
			},
		},
	}

	result, err := SelectCheapest(context.Background(), adapters, "H100", false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Tie should be broken alphabetically.
	if result.Price.Cloud != "cloud-a" {
		t.Errorf("expected cloud-a (alphabetical tie break), got %s", result.Price.Cloud)
	}
}

func TestSelectAllAvailable(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{
			name: "cloud-expensive",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-expensive", GPUType: "H100", HourlyCost: 10.00, Available: true}},
			},
		},
		&mockAdapter{
			name: "cloud-cheap",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-cheap", GPUType: "H100", HourlyCost: 2.50, Available: true}},
			},
		},
		&mockAdapter{
			name: "cloud-mid",
			prices: map[string][]PriceInfo{
				"H100": {{Cloud: "cloud-mid", GPUType: "H100", HourlyCost: 5.00, Available: true}},
			},
		},
	}

	results, err := SelectAllAvailable(context.Background(), adapters, "H100", false)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(results) != 3 {
		t.Fatalf("expected 3 results, got %d", len(results))
	}
	if results[0].Price.Cloud != "cloud-cheap" {
		t.Errorf("expected cheapest first, got %s", results[0].Price.Cloud)
	}
	if results[2].Price.Cloud != "cloud-expensive" {
		t.Errorf("expected most expensive last, got %s", results[2].Price.Cloud)
	}
}

func TestAdapterForCloud(t *testing.T) {
	adapters := []CloudAdapter{
		&mockAdapter{name: "nebius"},
		&mockAdapter{name: "lambda"},
		&mockAdapter{name: "aws"},
	}

	a, err := AdapterForCloud(adapters, "lambda")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if a.Name() != "lambda" {
		t.Errorf("expected lambda, got %s", a.Name())
	}

	_, err = AdapterForCloud(adapters, "gcp")
	if err == nil {
		t.Fatal("expected error for unknown cloud")
	}
}

func TestBuildSkyPilotTask(t *testing.T) {
	spec := NodeSpec{
		Cloud:             "nebius",
		GPUType:           "H100",
		GPUCount:          2,
		DiskSizeGB:        256,
		K8sVersion:        "1.33",
		ClusterName:       "test-cluster",
		Region:            "eu-north1",
		SSMActivationID:   "ssm-123",
		SSMActivationCode: "ssm-code-456",
		UseSpot:           true,
	}

	task := buildSkyPilotTask(spec)

	resources, ok := task["resources"].(map[string]interface{})
	if !ok {
		t.Fatal("expected resources map in task")
	}
	if resources["cloud"] != "nebius" {
		t.Errorf("expected cloud nebius, got %v", resources["cloud"])
	}
	if resources["accelerators"] != "H100:2" {
		t.Errorf("expected accelerators H100:2, got %v", resources["accelerators"])
	}
	if resources["disk_size"] != 256 {
		t.Errorf("expected disk_size 256, got %v", resources["disk_size"])
	}
	if resources["region"] != "eu-north1" {
		t.Errorf("expected region eu-north1, got %v", resources["region"])
	}
	if resources["use_spot"] != true {
		t.Errorf("expected use_spot true, got %v", resources["use_spot"])
	}

	envs, ok := task["envs"].(map[string]string)
	if !ok {
		t.Fatal("expected envs map in task")
	}
	if envs["K8S_VERSION"] != "1.33" {
		t.Errorf("expected K8S_VERSION 1.33, got %v", envs["K8S_VERSION"])
	}
	if envs["SSM_ACTIVATION_ID"] != "ssm-123" {
		t.Errorf("expected SSM_ACTIVATION_ID ssm-123, got %v", envs["SSM_ACTIVATION_ID"])
	}
}

func TestMapClusterStatus(t *testing.T) {
	tests := []struct {
		input    string
		expected NodeStatus
	}{
		{"INIT", NodeStatusProvisioning},
		{"UP", NodeStatusRunning},
		{"STOPPED", NodeStatusStopped},
		{"UNKNOWN", NodeStatusUnknown},
	}

	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := mapClusterStatus(skypilot.ClusterStatus(tt.input))
			if got != tt.expected {
				t.Errorf("mapClusterStatus(%q) = %q, want %q", tt.input, got, tt.expected)
			}
		})
	}
}
