package controllers

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/provisioner"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
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

	// runningForFirstNChecks models an asynchronous teardown: TerminateNode only
	// submits the request, so the provider still reports the cluster for the
	// first few probes before it becomes terminated.
	runningForFirstNChecks int
	statusChecks           int
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
	m.statusChecks++
	if m.statusChecks <= m.runningForFirstNChecks {
		return &adapters.NodeInfo{Status: adapters.NodeStatusRunning}, nil
	}
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

// ---------------------------------------------------------------------------
// R15: the provisioner must not drop unreleased provider resources
//
// Cleanup of a failed cluster used to be fire-and-forget: a failed
// TerminateNode was logged and the loop moved on, leaving a possibly-live GPU
// cluster with nothing recording its existence.
// ---------------------------------------------------------------------------

func TestVerifyClusterReleased(t *testing.T) {
	tests := []struct {
		name      string
		adapter   *mockProvisionerAdapter
		wantError bool
		reason    string
	}{
		{
			name:      "no provider record means released",
			adapter:   &mockProvisionerAdapter{nodeInfo: nil},
			wantError: false,
			reason:    "a cluster absent from the provider has been released",
		},
		{
			name: "terminated is released",
			adapter: &mockProvisionerAdapter{
				nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusTerminated},
			},
			wantError: false,
		},
		{
			name: "running is not released",
			adapter: &mockProvisionerAdapter{
				nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusRunning},
			},
			wantError: true,
			reason:    "a running cluster is still billing",
		},
		{
			name: "stopped is not released",
			adapter: &mockProvisionerAdapter{
				nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusStopped},
			},
			wantError: true,
			reason:    "a stopped cluster retains disks and still incurs storage cost",
		},
		{
			// An unknown outcome is not a negative one: we must not conclude the
			// resource is gone because the provider could not be queried.
			name:      "query failure is unknown, not released",
			adapter:   &mockProvisionerAdapter{nodeErr: context.DeadlineExceeded},
			wantError: true,
			reason:    "an unreachable provider leaves the outcome unknown",
		},
		{
			name: "unknown status is not released",
			adapter: &mockProvisionerAdapter{
				nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusUnknown},
			},
			wantError: true,
			reason:    "an unknown status is not evidence of release",
		},
	}

	// Short verification window: verifyClusterReleased polls until an async
	// teardown converges, so the "not released" cases would otherwise wait out
	// the production timeout.
	r := &ProvisionerReconciler{
		ReleaseVerifyTimeout:  50 * time.Millisecond,
		ReleaseVerifyInterval: 5 * time.Millisecond,
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := r.verifyClusterReleased(context.Background(), tt.adapter, "sp-cluster-1")
			if tt.wantError && err == nil {
				t.Errorf("expected an error: %s", tt.reason)
			}
			if !tt.wantError && err != nil {
				t.Errorf("unexpected error (%s): %v", tt.reason, err)
			}
		})
	}
}

// TestProvisionerReconcileSkipsRetiringNode: a node can be marked for retirement
// while still Pending (an operator cancels a request that has not launched yet).
// Provisioning it anyway would create exactly the capacity that was refused.
func TestProvisionerReconcileSkipsRetiringNode(t *testing.T) {
	for _, tt := range []struct {
		name   string
		mutate func(*superplanev1.SuperplaneNode)
	}{
		{
			name: "retiring phase",
			mutate: func(n *superplanev1.SuperplaneNode) {
				n.Status.Phase = superplanev1.SuperplaneNodePhaseRetiring
			},
		},
		{
			name: "pending but annotated for retirement",
			mutate: func(n *superplanev1.SuperplaneNode) {
				n.Status.Phase = superplanev1.SuperplaneNodePhasePending
				n.Annotations = map[string]string{
					superplanev1.AnnotationRetirement: "request cancelled by operator",
				}
			},
		},
	} {
		t.Run(tt.name, func(t *testing.T) {
			spNode := makeSuperplaneNode("sp-1", "default", "pool1", "", "",
				superplanev1.SuperplaneNodePhasePending, nil)
			tt.mutate(spNode)

			fc := newFakeClient(spNode)
			// No adapters and no onboarder: if the reconcile were to attempt
			// provisioning it could not silently succeed.
			r := NewProvisionerReconciler(fc, nil, nil)

			result, err := r.Reconcile(context.Background(), ctrl.Request{
				NamespacedName: types.NamespacedName{Name: "sp-1", Namespace: "default"},
			})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if result.RequeueAfter != 0 {
				t.Errorf("expected no requeue for retiring node, got %v", result.RequeueAfter)
			}

			// The node must not have been advanced toward provisioning.
			var updated superplanev1.SuperplaneNode
			if err := fc.Get(context.Background(),
				types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
				t.Fatalf("get node: %v", err)
			}
			if updated.Status.Phase == superplanev1.SuperplaneNodePhaseProvisioning {
				t.Error("retiring node was moved to Provisioning")
			}
		})
	}
}

// TestUpdateFailedWithHandleRetainsProviderHandle: an unresolved allocation is
// retained and reported, never erased. The cluster name is the only pointer a
// later reconciliation has to a possibly-live, billing GPU cluster.
func TestUpdateFailedWithHandleRetainsProviderHandle(t *testing.T) {
	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "", "",
		superplanev1.SuperplaneNodePhaseProvisioning, nil)
	// No cluster recorded yet — the failure happened during provisioning.
	spNode.Status.SkypilotCluster = ""

	fc := newFakeClient(spNode)
	r := NewProvisionerReconciler(fc, nil, nil)

	message := "All cloud options exhausted. Cleanup NOT confirmed for cluster(s) sp-orphan-1"
	if err := r.updateFailedWithHandle(context.Background(), spNode, message, "sp-orphan-1"); err != nil {
		t.Fatalf("updateFailedWithHandle() error = %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}

	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
		t.Errorf("expected phase ReleaseFailed, got %s", updated.Status.Phase)
	}
	if updated.Status.SkypilotCluster != "sp-orphan-1" {
		t.Errorf("provider handle was not retained, got %q", updated.Status.SkypilotCluster)
	}
	if !strings.Contains(updated.Status.Message, "NOT confirmed") {
		t.Errorf("expected the unconfirmed cleanup reported in the message, got %q", updated.Status.Message)
	}
}

// failingSkyClient makes Onboarder.Onboard return a non-successful result with a
// nil error: the SkyPilot health check fails, which is the real shape of "the
// launch did not work" and is what drives provisionAsync's cleanup path.
type failingSkyClient struct{}

func (failingSkyClient) Health(_ context.Context) (*skypilot.HealthResponse, error) {
	return nil, fmt.Errorf("dial tcp: connection refused")
}
func (failingSkyClient) Launch(_ context.Context, _ skypilot.LaunchRequest) (string, error) {
	return "", fmt.Errorf("not reached")
}
func (failingSkyClient) StreamProgress(_ context.Context, _ string) (<-chan skypilot.StreamEvent, <-chan error) {
	events := make(chan skypilot.StreamEvent)
	errs := make(chan error)
	close(events)
	close(errs)
	return events, errs
}
func (failingSkyClient) Status(_ context.Context, _ ...string) ([]skypilot.ClusterInfo, error) {
	return nil, nil
}

// newFailingOnboarder builds a real Onboarder whose every attempt fails.
func newFailingOnboarder() *provisioner.Onboarder {
	return provisioner.NewOnboarder(provisioner.OnboarderConfig{
		EKSClusterName: "test-eks",
		AWSRegion:      "us-east-1",
	}, provisioner.WithSkyClient(failingSkyClient{}))
}

func selectionFor(adapter adapters.CloudAdapter) []adapters.SelectionResult {
	return []adapters.SelectionResult{{
		Price: adapters.PriceInfo{
			Cloud:      "aws",
			Region:     "us-east-1",
			GPUType:    "H100",
			GPUCount:   1,
			HourlyCost: 4.5,
			Available:  true,
		},
		Adapter: adapter,
	}}
}

// TestProvisionAsyncFailedLaunchWithUnconfirmedCleanup: when a launch fails and
// the cleanup of the partially-created cluster cannot be confirmed, the node must
// land in ReleaseFailed with the cluster handle retained. Marking it plain Failed
// would drop the only pointer to a cluster that may still be billing.
func TestProvisionAsyncFailedLaunchWithUnconfirmedCleanup(t *testing.T) {
	for _, tt := range []struct {
		name    string
		adapter *mockProvisionerAdapter
		why     string
	}{
		{
			name: "terminate call itself fails",
			adapter: &mockProvisionerAdapter{
				name:    "aws",
				termErr: fmt.Errorf("ThrottlingException: rate exceeded"),
			},
			why: "the teardown request never succeeded",
		},
		{
			name: "terminate succeeds but provider still reports the cluster",
			adapter: &mockProvisionerAdapter{
				name:     "aws",
				termID:   "i-123",
				nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusRunning},
			},
			why: "provider truth contradicts the successful terminate call",
		},
		{
			name: "terminate succeeds but provider is unreachable",
			adapter: &mockProvisionerAdapter{
				name:    "aws",
				termID:  "i-123",
				nodeErr: fmt.Errorf("ExpiredToken: credentials could not be refreshed"),
			},
			why: "an unreachable provider is UNKNOWN, never released",
		},
	} {
		t.Run(tt.name, func(t *testing.T) {
			spNode := makeSuperplaneNode("sp-1", "default", "pool1", "", "",
				superplanev1.SuperplaneNodePhaseProvisioning, nil)
			pool := makeNodePool("pool1", nil, nil, nil)
			fc := newFakeClient(spNode, pool)

			r := NewProvisionerReconciler(fc, []adapters.CloudAdapter{tt.adapter}, newFailingOnboarder())
			r.ReleaseVerifyTimeout = 50 * time.Millisecond
			r.ReleaseVerifyInterval = 5 * time.Millisecond
			r.provisionAsync(context.Background(), spNode, pool, selectionFor(tt.adapter))

			var updated superplanev1.SuperplaneNode
			if err := fc.Get(context.Background(),
				types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
				t.Fatalf("get node: %v", err)
			}

			if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReleaseFailed {
				t.Errorf("expected ReleaseFailed (%s), got %s", tt.why, updated.Status.Phase)
			}
			if updated.Status.SkypilotCluster != "sp-sp-1" {
				t.Errorf("expected the unresolved cluster handle retained, got %q",
					updated.Status.SkypilotCluster)
			}
			if !strings.Contains(updated.Status.Message, "NOT confirmed") {
				t.Errorf("expected the message to state cleanup was not confirmed, got %q",
					updated.Status.Message)
			}
			// The in-flight marker must be released regardless of outcome, or the
			// node can never be retried.
			if _, stillTracked := r.inFlight["sp-1"]; stillTracked {
				t.Error("node left marked in-flight after provisionAsync returned")
			}
		})
	}
}

// TestProvisionAsyncFailedLaunchWithConfirmedCleanup: the same failure, but with
// the provider confirming the cluster is gone, is a plain Failed — no unresolved
// handle to chase. This is the contrast case that shows ReleaseFailed above is
// driven by provider truth and not just by "the launch failed".
func TestProvisionAsyncFailedLaunchWithConfirmedCleanup(t *testing.T) {
	adapter := &mockProvisionerAdapter{
		name:     "aws",
		termID:   "i-123",
		nodeInfo: &adapters.NodeInfo{Status: adapters.NodeStatusTerminated},
	}

	spNode := makeSuperplaneNode("sp-1", "default", "pool1", "", "",
		superplanev1.SuperplaneNodePhaseProvisioning, nil)
	pool := makeNodePool("pool1", nil, nil, nil)
	fc := newFakeClient(spNode, pool)

	r := NewProvisionerReconciler(fc, []adapters.CloudAdapter{adapter}, newFailingOnboarder())
	r.ReleaseVerifyTimeout = 50 * time.Millisecond
	r.ReleaseVerifyInterval = 5 * time.Millisecond
	r.provisionAsync(context.Background(), spNode, pool, selectionFor(adapter))

	var updated superplanev1.SuperplaneNode
	if err := fc.Get(context.Background(),
		types.NamespacedName{Name: "sp-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}

	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseFailed {
		t.Errorf("expected Failed for a confirmed cleanup, got %s", updated.Status.Phase)
	}
	if strings.Contains(updated.Status.Message, "NOT confirmed") {
		t.Errorf("confirmed cleanup must not report an unresolved handle, got %q",
			updated.Status.Message)
	}
}

// TestVerifyClusterReleasedWaitsForAsyncTeardown pins the provisioner half of the
// same asynchronous-teardown correction: TerminateNode only submits the teardown
// request, so the provider still reports the cluster on the first probes. A single
// immediate probe read that as an unreleased resource and parked a node in
// ReleaseFailed with a handle that was in fact already being cleaned up.
func TestVerifyClusterReleasedWaitsForAsyncTeardown(t *testing.T) {
	adapter := &mockProvisionerAdapter{
		name:                   "aws",
		runningForFirstNChecks: 2,
		nodeInfo:               &adapters.NodeInfo{Status: adapters.NodeStatusTerminated},
	}
	r := &ProvisionerReconciler{
		ReleaseVerifyTimeout:  time.Second,
		ReleaseVerifyInterval: 5 * time.Millisecond,
	}

	if err := r.verifyClusterReleased(context.Background(), adapter, "sp-cluster-1"); err != nil {
		t.Fatalf("an in-progress teardown that then terminates must count as released, got %v", err)
	}
	if adapter.statusChecks < 2 {
		t.Errorf("expected the provider to be re-checked while teardown was in flight, got %d probe(s)",
			adapter.statusChecks)
	}
}
