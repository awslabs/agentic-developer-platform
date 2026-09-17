package controllers

import (
	"context"
	"testing"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
)

// mockAdapter implements adapters.CloudAdapter for testing.
type mockProvisionerAdapter struct {
	name       string
	prices     []adapters.PriceInfo
	priceErr   error
	available  bool
	availErr   error
	provID     string
	provErr    error
	termID     string
	termErr    error
	nodeInfo   *adapters.NodeInfo
	nodeErr    error
}

func (m *mockProvisionerAdapter) Name() string { return m.name }
func (m *mockProvisionerAdapter) ListGPUPricing(_ context.Context, _ string) ([]adapters.PriceInfo, error) {
	return m.prices, m.priceErr
}
func (m *mockProvisionerAdapter) CheckAvailability(_ context.Context, _ string, _ string) (bool, error) {
	return m.available, m.availErr
}
func (m *mockProvisionerAdapter) ProvisionNode(_ context.Context, _ adapters.NodeSpec) (string, error) {
	return m.provID, m.provErr
}
func (m *mockProvisionerAdapter) TerminateNode(_ context.Context, _ string) (string, error) {
	return m.termID, m.termErr
}
func (m *mockProvisionerAdapter) GetNodeStatus(_ context.Context, _ string) (*adapters.NodeInfo, error) {
	return m.nodeInfo, m.nodeErr
}

func TestFilterAdapters(t *testing.T) {
	nebius := &mockProvisionerAdapter{name: "nebius"}
	lambda := &mockProvisionerAdapter{name: "lambda"}
	aws := &mockProvisionerAdapter{name: "aws"}

	r := &ProvisionerReconciler{
		Adapters: []adapters.CloudAdapter{nebius, lambda, aws},
		inFlight: make(map[string]struct{}),
	}

	tests := []struct {
		name     string
		clouds   []string
		expected int
	}{
		{
			name:     "all clouds",
			clouds:   []string{"nebius", "lambda", "aws"},
			expected: 3,
		},
		{
			name:     "single cloud",
			clouds:   []string{"nebius"},
			expected: 1,
		},
		{
			name:     "two clouds",
			clouds:   []string{"lambda", "aws"},
			expected: 2,
		},
		{
			name:     "empty clouds returns all",
			clouds:   []string{},
			expected: 3,
		},
		{
			name:     "unknown cloud",
			clouds:   []string{"gcp"},
			expected: 0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			pool := &superplanev1.NodePool{}
			pool.Spec.Clouds = tc.clouds
			got := r.filterAdapters(pool)
			if len(got) != tc.expected {
				t.Errorf("filterAdapters() returned %d adapters, want %d", len(got), tc.expected)
			}
		})
	}
}

func TestNewProvisionerReconciler(t *testing.T) {
	r := NewProvisionerReconciler(nil, nil, nil)
	if r == nil {
		t.Fatal("NewProvisionerReconciler returned nil")
	}
	if r.inFlight == nil {
		t.Error("inFlight map not initialized")
	}
}

func TestInFlightTracking(t *testing.T) {
	r := &ProvisionerReconciler{
		inFlight: make(map[string]struct{}),
	}

	// Add to in-flight.
	r.mu.Lock()
	r.inFlight["node-1"] = struct{}{}
	r.mu.Unlock()

	// Check it's there.
	r.mu.Lock()
	_, exists := r.inFlight["node-1"]
	r.mu.Unlock()
	if !exists {
		t.Error("expected node-1 to be in-flight")
	}

	// Remove from in-flight.
	r.mu.Lock()
	delete(r.inFlight, "node-1")
	r.mu.Unlock()

	r.mu.Lock()
	_, exists = r.inFlight["node-1"]
	r.mu.Unlock()
	if exists {
		t.Error("expected node-1 to not be in-flight after removal")
	}
}

func TestDefaultConstants(t *testing.T) {
	if DefaultMaxConcurrentProvisioning != 3 {
		t.Errorf("DefaultMaxConcurrentProvisioning = %d, want 3", DefaultMaxConcurrentProvisioning)
	}
	if DefaultDiskSizeGB != 256 {
		t.Errorf("DefaultDiskSizeGB = %d, want 256", DefaultDiskSizeGB)
	}
	if DefaultK8sVersion != "1.33" {
		t.Errorf("DefaultK8sVersion = %q, want 1.33", DefaultK8sVersion)
	}
}
