package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

// fakeSkyChecker implements SkyPilotHealthChecker for tests.
type fakeSkyChecker struct {
	healthy bool
	err     error
}

func (f *fakeSkyChecker) HealthCheck(_ context.Context) (bool, error) {
	return f.healthy, f.err
}

// newTestScheme returns a runtime.Scheme with SuperplaneNode registered.
func newTestScheme() *runtime.Scheme {
	s := runtime.NewScheme()
	_ = superplanev1.AddToScheme(s)
	return s
}

// TestCollect_NodeSummary verifies that Collect correctly aggregates node phases.
func TestCollect_NodeSummary(t *testing.T) {
	scheme := newTestScheme()

	nodes := []superplanev1.SuperplaneNode{
		makeNode("node-1", superplanev1.SuperplaneNodePhaseReady, "H100", 8, 4.50, "k8s-node-1"),
		makeNode("node-2", superplanev1.SuperplaneNodePhaseReady, "H100", 8, 4.50, "k8s-node-2"),
		makeNode("node-3", superplanev1.SuperplaneNodePhaseProvisioning, "A10G", 4, 0, ""),
		makeNode("node-4", superplanev1.SuperplaneNodePhaseDegraded, "H100", 8, 4.50, "k8s-node-4"),
		makeNode("node-5", superplanev1.SuperplaneNodePhaseDraining, "A10G", 4, 1.20, "k8s-node-5"),
		makeNode("node-6", superplanev1.SuperplaneNodePhaseTerminated, "H100", 8, 0, ""),
	}

	objs := make([]runtime.Object, len(nodes))
	for i := range nodes {
		objs[i] = &nodes[i]
	}

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objs...).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: true},
		ClusterID:  "test-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	// Terminated nodes are excluded from total.
	if hb.NodeSummary.Total != 5 {
		t.Errorf("expected Total=5, got %d", hb.NodeSummary.Total)
	}
	if hb.NodeSummary.Ready != 2 {
		t.Errorf("expected Ready=2, got %d", hb.NodeSummary.Ready)
	}
	if hb.NodeSummary.Provisioning != 1 {
		t.Errorf("expected Provisioning=1, got %d", hb.NodeSummary.Provisioning)
	}
	if hb.NodeSummary.NotReady != 1 {
		t.Errorf("expected NotReady=1, got %d", hb.NodeSummary.NotReady)
	}
	if hb.NodeSummary.Draining != 1 {
		t.Errorf("expected Draining=1, got %d", hb.NodeSummary.Draining)
	}
}

// TestCollect_GPUSummary verifies GPU aggregation.
func TestCollect_GPUSummary(t *testing.T) {
	scheme := newTestScheme()

	nodes := []superplanev1.SuperplaneNode{
		makeNode("node-1", superplanev1.SuperplaneNodePhaseReady, "H100", 8, 4.50, "k8s-node-1"),
		makeNode("node-2", superplanev1.SuperplaneNodePhaseReady, "A10G", 4, 1.20, "k8s-node-2"),
		makeNode("node-3", superplanev1.SuperplaneNodePhaseProvisioning, "H100", 8, 0, ""),
	}

	objs := make([]runtime.Object, len(nodes))
	for i := range nodes {
		objs[i] = &nodes[i]
	}

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objs...).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: true},
		ClusterID:  "test-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	if hb.GPUSummary.Total != 20 {
		t.Errorf("expected GPU Total=20, got %d", hb.GPUSummary.Total)
	}
	// Ready nodes with k8sNodeName: 8 (H100) + 4 (A10G) = 12 allocated
	if hb.GPUSummary.Allocated != 12 {
		t.Errorf("expected GPU Allocated=12, got %d", hb.GPUSummary.Allocated)
	}
	// Provisioning: 8 idle
	if hb.GPUSummary.Idle != 8 {
		t.Errorf("expected GPU Idle=8, got %d", hb.GPUSummary.Idle)
	}
	if hb.GPUSummary.Types["H100"] != 16 {
		t.Errorf("expected H100=16, got %d", hb.GPUSummary.Types["H100"])
	}
	if hb.GPUSummary.Types["A10G"] != 4 {
		t.Errorf("expected A10G=4, got %d", hb.GPUSummary.Types["A10G"])
	}
}

// TestCollect_CostHourly verifies cost aggregation.
func TestCollect_CostHourly(t *testing.T) {
	scheme := newTestScheme()

	nodes := []superplanev1.SuperplaneNode{
		makeNode("node-1", superplanev1.SuperplaneNodePhaseReady, "H100", 8, 4.50, "k8s-node-1"),
		makeNode("node-2", superplanev1.SuperplaneNodePhaseReady, "A10G", 4, 1.20, "k8s-node-2"),
	}

	objs := make([]runtime.Object, len(nodes))
	for i := range nodes {
		objs[i] = &nodes[i]
	}

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objs...).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: true},
		ClusterID:  "test-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	expected := 5.70
	if hb.CostHourly < expected-0.01 || hb.CostHourly > expected+0.01 {
		t.Errorf("expected CostHourly=%.2f, got %.2f", expected, hb.CostHourly)
	}
}

// TestCollect_SkyPilotDown verifies that a SkyPilot failure results in
// skypilot_healthy=false and a degraded status (not a crash).
func TestCollect_SkyPilotDown(t *testing.T) {
	scheme := newTestScheme()

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: false, err: fmt.Errorf("connection refused")},
		ClusterID:  "test-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	if hb.SkyPilotHealthy {
		t.Error("expected SkyPilotHealthy=false when SkyPilot is down")
	}
	if hb.Status != "degraded" {
		t.Errorf("expected status=degraded, got %s", hb.Status)
	}

	// Should have a warning alert for SkyPilot.
	found := false
	for _, a := range hb.Alerts {
		if a.Resource == "skypilot-api" {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected alert for skypilot-api")
	}
}

// TestCollect_DegradedNodes verifies alerts are generated for degraded/failed nodes.
func TestCollect_DegradedNodes(t *testing.T) {
	scheme := newTestScheme()

	nodes := []superplanev1.SuperplaneNode{
		makeNode("node-1", superplanev1.SuperplaneNodePhaseReady, "H100", 8, 4.50, "k8s-node-1"),
		makeNodeWithMessage("node-2", superplanev1.SuperplaneNodePhaseFailed, "H100", 8, 0, "", "GPU memory error"),
	}

	objs := make([]runtime.Object, len(nodes))
	for i := range nodes {
		objs[i] = &nodes[i]
	}

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithRuntimeObjects(objs...).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: true},
		ClusterID:  "test-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	if hb.Status != "degraded" {
		t.Errorf("expected status=degraded with failed node, got %s", hb.Status)
	}

	criticalFound := false
	for _, a := range hb.Alerts {
		if a.Severity == "critical" && a.Resource == "node-2" {
			criticalFound = true
		}
	}
	if !criticalFound {
		t.Error("expected critical alert for failed node-2")
	}
}

// TestSend_Success verifies successful heartbeat POST.
func TestSend_Success(t *testing.T) {
	var received ClusterHeartbeat
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
		}
		if r.URL.Path != "/internal/heartbeat" {
			t.Errorf("expected /internal/heartbeat, got %s", r.URL.Path)
		}
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Errorf("failed to decode body: %v", err)
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	h := &HeartbeatSender{
		APIURL:     server.URL,
		ClusterID:  "test-cluster",
		HTTPClient: server.Client(),
	}

	hb := ClusterHeartbeat{
		ClusterID: "test-cluster",
		Status:    "healthy",
		Timestamp: time.Now(),
	}

	if err := h.Send(context.Background(), hb); err != nil {
		t.Fatalf("Send failed: %v", err)
	}

	if received.ClusterID != "test-cluster" {
		t.Errorf("expected cluster_id=test-cluster, got %s", received.ClusterID)
	}
}

// TestSend_RetryOnServerError verifies retry behavior on 5xx errors.
func TestSend_RetryOnServerError(t *testing.T) {
	var attempts int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := atomic.AddInt32(&attempts, 1)
		if n < 3 {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	h := &HeartbeatSender{
		APIURL:     server.URL,
		ClusterID:  "test-cluster",
		HTTPClient: server.Client(),
	}

	hb := ClusterHeartbeat{ClusterID: "test-cluster", Status: "healthy"}

	if err := h.Send(context.Background(), hb); err != nil {
		t.Fatalf("Send should succeed after retries: %v", err)
	}

	if atomic.LoadInt32(&attempts) != 3 {
		t.Errorf("expected 3 attempts, got %d", atomic.LoadInt32(&attempts))
	}
}

// TestSend_NoRetryOn4xx verifies that 4xx client errors are not retried.
func TestSend_NoRetryOn4xx(t *testing.T) {
	var attempts int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&attempts, 1)
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte("bad request"))
	}))
	defer server.Close()

	h := &HeartbeatSender{
		APIURL:     server.URL,
		ClusterID:  "test-cluster",
		HTTPClient: server.Client(),
	}

	hb := ClusterHeartbeat{ClusterID: "test-cluster"}

	err := h.Send(context.Background(), hb)
	if err == nil {
		t.Fatal("expected error on 400 response")
	}

	if atomic.LoadInt32(&attempts) != 1 {
		t.Errorf("expected 1 attempt (no retry on 4xx), got %d", atomic.LoadInt32(&attempts))
	}
}

// TestSend_AllRetriesFail verifies error when all retries are exhausted.
func TestSend_AllRetriesFail(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()

	h := &HeartbeatSender{
		APIURL:     server.URL,
		ClusterID:  "test-cluster",
		HTTPClient: server.Client(),
	}

	hb := ClusterHeartbeat{ClusterID: "test-cluster"}

	err := h.Send(context.Background(), hb)
	if err == nil {
		t.Fatal("expected error when all retries fail")
	}
}

// TestComputeStatus verifies status computation logic.
func TestComputeStatus(t *testing.T) {
	h := &HeartbeatSender{}

	tests := []struct {
		name     string
		hb       ClusterHeartbeat
		expected string
	}{
		{
			name: "healthy cluster",
			hb: ClusterHeartbeat{
				SkyPilotHealthy: true,
				NodeSummary:     NodeSummary{Total: 4, Ready: 4},
			},
			expected: "healthy",
		},
		{
			name: "degraded - skypilot down",
			hb: ClusterHeartbeat{
				SkyPilotHealthy: false,
				NodeSummary:     NodeSummary{Total: 4, Ready: 4},
			},
			expected: "degraded",
		},
		{
			name: "degraded - critical alert",
			hb: ClusterHeartbeat{
				SkyPilotHealthy: true,
				Alerts:          []HeartbeatAlert{{Severity: "critical", Message: "node failed"}},
			},
			expected: "degraded",
		},
		{
			name: "recovering - provisioning with failures",
			hb: ClusterHeartbeat{
				SkyPilotHealthy: true,
				NodeSummary:     NodeSummary{Total: 4, Ready: 2, Provisioning: 1, NotReady: 1},
			},
			expected: "recovering",
		},
		{
			name: "degraded - majority not ready",
			hb: ClusterHeartbeat{
				SkyPilotHealthy: true,
				NodeSummary:     NodeSummary{Total: 4, Ready: 1, NotReady: 3},
			},
			expected: "degraded",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := h.computeStatus(tt.hb)
			if result != tt.expected {
				t.Errorf("expected %s, got %s", tt.expected, result)
			}
		})
	}
}

// TestCollect_EmptyCluster verifies behavior with no nodes.
func TestCollect_EmptyCluster(t *testing.T) {
	scheme := newTestScheme()

	k8sClient := fake.NewClientBuilder().
		WithScheme(scheme).
		Build()

	h := &HeartbeatSender{
		Client:     k8sClient,
		SkyChecker: &fakeSkyChecker{healthy: true},
		ClusterID:  "empty-cluster",
		APIURL:     "http://localhost",
	}

	hb := h.Collect(context.Background())

	if hb.ClusterID != "empty-cluster" {
		t.Errorf("expected ClusterID=empty-cluster, got %s", hb.ClusterID)
	}
	if hb.Status != "healthy" {
		t.Errorf("expected status=healthy for empty cluster, got %s", hb.Status)
	}
	if hb.NodeSummary.Total != 0 {
		t.Errorf("expected Total=0, got %d", hb.NodeSummary.Total)
	}
	if hb.ControllerVersion != ControllerVersion {
		t.Errorf("expected ControllerVersion=%s, got %s", ControllerVersion, hb.ControllerVersion)
	}
}

// --- helpers ---

func makeNode(name string, phase superplanev1.SuperplaneNodePhase, gpuType string, gpuCount int32, hourlyCost float64, k8sName string) superplanev1.SuperplaneNode {
	return superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: "default-pool",
			Cloud:       "aws",
			GPUType:     gpuType,
			GPUCount:    gpuCount,
		},
		Status: superplanev1.SuperplaneNodeStatus{
			Phase:       phase,
			HourlyCost:  hourlyCost,
			K8sNodeName: k8sName,
		},
	}
}

func makeNodeWithMessage(name string, phase superplanev1.SuperplaneNodePhase, gpuType string, gpuCount int32, hourlyCost float64, k8sName, message string) superplanev1.SuperplaneNode {
	n := makeNode(name, phase, gpuType, gpuCount, hourlyCost, k8sName)
	n.Status.Message = message
	return n
}
