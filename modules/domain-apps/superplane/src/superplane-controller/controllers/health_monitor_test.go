package controllers

import (
	"context"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

// fakeClock is a controllable clock for testing.
type fakeClock struct {
	now time.Time
}

func (c *fakeClock) Now() time.Time { return c.now }

func (c *fakeClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

// fakeNodeGetter is a test double for NodeGetter.
type fakeNodeGetter struct {
	nodes map[string]*corev1.Node
}

func (f *fakeNodeGetter) GetNode(_ context.Context, name string) (*corev1.Node, error) {
	if node, ok := f.nodes[name]; ok {
		return node, nil
	}
	return nil, errors.NewNotFound(schema.GroupResource{Resource: "nodes"}, name)
}

// newHealthMonitorScheme returns a runtime scheme with SuperplaneNode types registered.
func newHealthMonitorScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := superplanev1.AddToScheme(s); err != nil {
		t.Fatalf("add superplane scheme: %v", err)
	}
	return s
}

// makeReadyK8sNode creates a corev1.Node with Ready=True and a recent heartbeat.
func makeReadyK8sNode(name string, heartbeat time.Time) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: corev1.NodeStatus{
			Conditions: []corev1.NodeCondition{
				{
					Type:              corev1.NodeReady,
					Status:            corev1.ConditionTrue,
					LastHeartbeatTime: metav1.NewTime(heartbeat),
				},
			},
		},
	}
}

// makeNotReadyK8sNode creates a corev1.Node with Ready=False.
func makeNotReadyK8sNode(name string, heartbeat time.Time) *corev1.Node {
	return &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{Name: name},
		Status: corev1.NodeStatus{
			Conditions: []corev1.NodeCondition{
				{
					Type:              corev1.NodeReady,
					Status:            corev1.ConditionFalse,
					LastHeartbeatTime: metav1.NewTime(heartbeat),
					Message:           "kubelet not ready",
				},
			},
		},
	}
}

// makeHealthMonitorNode creates a SuperplaneNode in the given phase for health monitor tests.
func makeHealthMonitorNode(name, namespace, k8sNodeName string, phase superplanev1.SuperplaneNodePhase, conditions []metav1.Condition) *superplanev1.SuperplaneNode {
	return &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: "gpu-pool",
			Cloud:       "aws",
			GPUType:     "A100",
			GPUCount:    1,
			Region:      "us-east-1",
		},
		Status: superplanev1.SuperplaneNodeStatus{
			Phase:       phase,
			K8sNodeName: k8sNodeName,
			Conditions:  conditions,
		},
	}
}

func setupReconciler(t *testing.T, objs []client.Object, nodeGetter *fakeNodeGetter, clock *fakeClock) (*HealthMonitorReconciler, client.Client) {
	t.Helper()
	scheme := newHealthMonitorScheme(t)
	fakeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(objs...).
		WithStatusSubresource(&superplanev1.SuperplaneNode{}).
		Build()

	r := &HealthMonitorReconciler{
		Client:     fakeClient,
		Clock:      clock,
		NodeGetter: nodeGetter,
	}
	return r, fakeClient
}

func TestReconcile_HealthyNodeRemainsReady(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", superplanev1.SuperplaneNodePhaseReady, nil)
	nodeGetter := &fakeNodeGetter{
		nodes: map[string]*corev1.Node{
			"k8s-node-1": makeReadyK8sNode("k8s-node-1", now.Add(-30*time.Second)),
		},
	}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != HealthCheckInterval {
		t.Errorf("expected requeue after %v, got %v", HealthCheckInterval, result.RequeueAfter)
	}

	// Verify the node remains Ready.
	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReady {
		t.Errorf("expected phase Ready, got %s", updated.Status.Phase)
	}

	// Verify healthy condition was set.
	found := false
	for _, c := range updated.Status.Conditions {
		if c.Type == ConditionTypeHealthy && c.Status == metav1.ConditionTrue {
			found = true
		}
	}
	if !found {
		t.Error("expected Healthy=True condition")
	}
}

func TestReconcile_SkipsNonReadyPhase(t *testing.T) {
	phases := []superplanev1.SuperplaneNodePhase{
		superplanev1.SuperplaneNodePhasePending,
		superplanev1.SuperplaneNodePhaseProvisioning,
		superplanev1.SuperplaneNodePhaseJoining,
		superplanev1.SuperplaneNodePhaseDraining,
		superplanev1.SuperplaneNodePhaseTerminated,
		superplanev1.SuperplaneNodePhaseFailed,
	}

	for _, phase := range phases {
		t.Run(string(phase), func(t *testing.T) {
			now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
			clock := &fakeClock{now: now}

			spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", phase, nil)
			nodeGetter := &fakeNodeGetter{nodes: map[string]*corev1.Node{}}

			r, _ := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

			result, err := r.Reconcile(context.Background(), ctrl.Request{
				NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
			})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if result.RequeueAfter != HealthCheckInterval {
				t.Errorf("expected requeue after %v, got %v", HealthCheckInterval, result.RequeueAfter)
			}
		})
	}
}

func TestReconcile_UnhealthyUnder5MinJustMonitors(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	// Node is not ready but this is the first time we see it.
	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", superplanev1.SuperplaneNodePhaseReady, nil)
	nodeGetter := &fakeNodeGetter{
		nodes: map[string]*corev1.Node{
			"k8s-node-1": makeNotReadyK8sNode("k8s-node-1", now),
		},
	}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != HealthCheckInterval {
		t.Errorf("expected requeue after %v, got %v", HealthCheckInterval, result.RequeueAfter)
	}

	// Node should still be Ready (within tolerance).
	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReady {
		t.Errorf("expected phase Ready (within tolerance), got %s", updated.Status.Phase)
	}
}

func TestReconcile_UnhealthyOver5MinMarksDegraded(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	// Node has been unhealthy since 6 minutes ago.
	unhealthySince := now.Add(-6 * time.Minute)
	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", superplanev1.SuperplaneNodePhaseReady, []metav1.Condition{
		{
			Type:               ConditionTypeHealthy,
			Status:             metav1.ConditionFalse,
			LastTransitionTime: metav1.NewTime(unhealthySince),
			Reason:             "HealthCheckFailed",
			Message:            "K8s node not ready",
		},
	})
	nodeGetter := &fakeNodeGetter{
		nodes: map[string]*corev1.Node{
			"k8s-node-1": makeNotReadyK8sNode("k8s-node-1", now),
		},
	}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseDegraded {
		t.Errorf("expected phase Degraded, got %s", updated.Status.Phase)
	}
	if !strings.Contains(updated.Status.Message, "degraded") {
		t.Errorf("expected message to contain 'degraded', got %q", updated.Status.Message)
	}
}

func TestReconcile_UnhealthyOver15MinTriggersAutoRepair(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	// Node has been unhealthy since 16 minutes ago, currently Degraded.
	unhealthySince := now.Add(-16 * time.Minute)
	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", superplanev1.SuperplaneNodePhaseDegraded, []metav1.Condition{
		{
			Type:               ConditionTypeHealthy,
			Status:             metav1.ConditionFalse,
			LastTransitionTime: metav1.NewTime(unhealthySince),
			Reason:             "HealthCheckFailed",
			Message:            "K8s node not ready",
		},
	})
	nodeGetter := &fakeNodeGetter{
		nodes: map[string]*corev1.Node{
			"k8s-node-1": makeNotReadyK8sNode("k8s-node-1", now),
		},
	}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// The original node should be set to Draining.
	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseDraining {
		t.Errorf("expected phase Draining, got %s", updated.Status.Phase)
	}
	if !strings.Contains(updated.Status.Message, "Auto-repair") {
		t.Errorf("expected message to contain 'Auto-repair', got %q", updated.Status.Message)
	}

	// Verify auto-repair condition was set.
	repairCondFound := false
	for _, c := range updated.Status.Conditions {
		if c.Type == ConditionTypeAutoRepair && c.Status == metav1.ConditionTrue {
			repairCondFound = true
		}
	}
	if !repairCondFound {
		t.Error("expected AutoRepair=True condition on drained node")
	}

	// Verify a replacement SuperplaneNode was created.
	var nodeList superplanev1.SuperplaneNodeList
	if err := fakeClient.List(context.Background(), &nodeList); err != nil {
		t.Fatalf("list nodes: %v", err)
	}

	if len(nodeList.Items) != 2 {
		t.Fatalf("expected 2 SuperplaneNodes (original + replacement), got %d", len(nodeList.Items))
	}

	var replacement *superplanev1.SuperplaneNode
	for i := range nodeList.Items {
		if nodeList.Items[i].Name != "node-1" {
			replacement = &nodeList.Items[i]
			break
		}
	}
	if replacement == nil {
		t.Fatal("replacement node not found")
	}

	// Verify replacement has same spec.
	if replacement.Spec.NodePoolRef != "gpu-pool" {
		t.Errorf("replacement nodePoolRef = %s, want gpu-pool", replacement.Spec.NodePoolRef)
	}
	if replacement.Spec.Cloud != "aws" {
		t.Errorf("replacement cloud = %s, want aws", replacement.Spec.Cloud)
	}
	if replacement.Spec.GPUType != "A100" {
		t.Errorf("replacement gpuType = %s, want A100", replacement.Spec.GPUType)
	}
	if replacement.Spec.GPUCount != 1 {
		t.Errorf("replacement gpuCount = %d, want 1", replacement.Spec.GPUCount)
	}

	// Verify replacement has auto-repair labels.
	if replacement.Labels["superplane.ai/auto-repair"] != "true" {
		t.Error("replacement missing auto-repair label")
	}
	if replacement.Labels["superplane.ai/replaced"] != "node-1" {
		t.Errorf("replacement 'replaced' label = %s, want node-1", replacement.Labels["superplane.ai/replaced"])
	}
}

func TestReconcile_DegradedNodeRecovery(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	// Node was Degraded but K8s node is now healthy.
	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-1", superplanev1.SuperplaneNodePhaseDegraded, []metav1.Condition{
		{
			Type:               ConditionTypeHealthy,
			Status:             metav1.ConditionFalse,
			LastTransitionTime: metav1.NewTime(now.Add(-7 * time.Minute)),
			Reason:             "HealthCheckFailed",
			Message:            "was not ready",
		},
	})
	nodeGetter := &fakeNodeGetter{
		nodes: map[string]*corev1.Node{
			"k8s-node-1": makeReadyK8sNode("k8s-node-1", now.Add(-10*time.Second)),
		},
	}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if updated.Status.Phase != superplanev1.SuperplaneNodePhaseReady {
		t.Errorf("expected phase Ready after recovery, got %s", updated.Status.Phase)
	}

	// Verify healthy condition.
	for _, c := range updated.Status.Conditions {
		if c.Type == ConditionTypeHealthy {
			if c.Status != metav1.ConditionTrue {
				t.Errorf("expected Healthy=True after recovery, got %s", c.Status)
			}
			return
		}
	}
	t.Error("Healthy condition not found after recovery")
}

func TestReconcile_K8sNodeNotFound(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	spNode := makeHealthMonitorNode("node-1", "default", "k8s-node-missing", superplanev1.SuperplaneNodePhaseReady, nil)
	nodeGetter := &fakeNodeGetter{nodes: map[string]*corev1.Node{}}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Should mark as unhealthy (first observation, so stays Ready).
	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}

	// Should have unhealthy condition.
	for _, c := range updated.Status.Conditions {
		if c.Type == ConditionTypeHealthy && c.Status == metav1.ConditionFalse {
			if !strings.Contains(c.Message, "not found") {
				t.Errorf("expected message about node not found, got %q", c.Message)
			}
			return
		}
	}
	t.Error("expected Healthy=False condition for missing K8s node")
}

func TestReconcile_NoK8sNodeName(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}

	// SuperplaneNode with no k8sNodeName yet.
	spNode := makeHealthMonitorNode("node-1", "default", "", superplanev1.SuperplaneNodePhaseReady, nil)
	nodeGetter := &fakeNodeGetter{nodes: map[string]*corev1.Node{}}

	r, fakeClient := setupReconciler(t, []client.Object{spNode}, nodeGetter, clock)

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-1", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != HealthCheckInterval {
		t.Errorf("expected requeue after %v, got %v", HealthCheckInterval, result.RequeueAfter)
	}

	// Should not modify the node.
	var updated superplanev1.SuperplaneNode
	if err := fakeClient.Get(context.Background(), types.NamespacedName{Name: "node-1", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get node: %v", err)
	}
	if len(updated.Status.Conditions) != 0 {
		t.Error("expected no conditions for node without k8sNodeName")
	}
}

func TestReconcile_NodeDeleted(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	clock := &fakeClock{now: now}
	nodeGetter := &fakeNodeGetter{nodes: map[string]*corev1.Node{}}

	// No SuperplaneNode in the store.
	r, _ := setupReconciler(t, []client.Object{}, nodeGetter, clock)

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "node-gone", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue for deleted node, got %v", result.RequeueAfter)
	}
}

func TestSetCondition(t *testing.T) {
	now := time.Now()

	tests := []struct {
		name      string
		existing  []metav1.Condition
		condition metav1.Condition
		changed   bool
		wantLen   int
	}{
		{
			name:     "add new condition",
			existing: nil,
			condition: metav1.Condition{
				Type:               "Test",
				Status:             metav1.ConditionTrue,
				LastTransitionTime: metav1.NewTime(now),
				Reason:             "Reason",
				Message:            "msg",
			},
			changed: true,
			wantLen: 1,
		},
		{
			name: "update existing condition",
			existing: []metav1.Condition{
				{Type: "Test", Status: metav1.ConditionFalse, Reason: "Old", Message: "old"},
			},
			condition: metav1.Condition{
				Type:               "Test",
				Status:             metav1.ConditionTrue,
				LastTransitionTime: metav1.NewTime(now),
				Reason:             "New",
				Message:            "new",
			},
			changed: true,
			wantLen: 1,
		},
		{
			name: "no change when identical",
			existing: []metav1.Condition{
				{Type: "Test", Status: metav1.ConditionTrue, Reason: "Same", Message: "same"},
			},
			condition: metav1.Condition{
				Type:    "Test",
				Status:  metav1.ConditionTrue,
				Reason:  "Same",
				Message: "same",
			},
			changed: false,
			wantLen: 1,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			conditions := make([]metav1.Condition, len(tt.existing))
			copy(conditions, tt.existing)

			got := setCondition(&conditions, tt.condition)
			if got != tt.changed {
				t.Errorf("setCondition() = %v, want %v", got, tt.changed)
			}
			if len(conditions) != tt.wantLen {
				t.Errorf("len(conditions) = %d, want %d", len(conditions), tt.wantLen)
			}
		})
	}
}

func TestGetUnhealthySince(t *testing.T) {
	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	past := now.Add(-10 * time.Minute)

	tests := []struct {
		name       string
		conditions []metav1.Condition
		want       time.Time
	}{
		{
			name:       "no conditions returns now",
			conditions: nil,
			want:       now,
		},
		{
			name: "healthy condition returns now",
			conditions: []metav1.Condition{
				{Type: ConditionTypeHealthy, Status: metav1.ConditionTrue},
			},
			want: now,
		},
		{
			name: "unhealthy condition returns transition time",
			conditions: []metav1.Condition{
				{
					Type:               ConditionTypeHealthy,
					Status:             metav1.ConditionFalse,
					LastTransitionTime: metav1.NewTime(past),
				},
			},
			want: past,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := getUnhealthySince(tt.conditions, now)
			if !got.Equal(tt.want) {
				t.Errorf("getUnhealthySince() = %v, want %v", got, tt.want)
			}
		})
	}
}
