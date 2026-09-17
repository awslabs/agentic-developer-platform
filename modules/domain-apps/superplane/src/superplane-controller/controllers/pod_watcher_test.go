package controllers

import (
	"context"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

// newPodWatcherFakeClient creates a fake client with both core and superplane types
// registered, including status subresources for Pod, NodePool, and SuperplaneNode.
func newPodWatcherFakeClient(objs ...client.Object) client.Client {
	s := newScheme()
	// Ensure corev1 is registered (newScheme from consolidator_test uses clientgoscheme).
	return fake.NewClientBuilder().
		WithScheme(s).
		WithObjects(objs...).
		WithStatusSubresource(&corev1.Pod{}, &superplanev1.NodePool{}, &superplanev1.SuperplaneNode{}).
		Build()
}

func gpuPod(name, namespace string, gpuCount int64, pending, unschedulable bool) *corev1.Pod {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{
				{
					Name:  "gpu-workload",
					Image: "nvidia/cuda:12.0-base",
					Resources: corev1.ResourceRequirements{
						Requests: corev1.ResourceList{
							GPUResourceName: *resource.NewQuantity(gpuCount, resource.DecimalSI),
						},
					},
				},
			},
		},
	}
	if pending {
		pod.Status.Phase = corev1.PodPending
	}
	if unschedulable {
		pod.Status.Conditions = []corev1.PodCondition{
			{
				Type:   corev1.PodScheduled,
				Status: corev1.ConditionFalse,
				Reason: "Unschedulable",
			},
		}
	}
	return pod
}

func activeNodePool(name string, clouds []string, gpuTypes []string, maxNodes int32) *superplanev1.NodePool {
	return &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: name,
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   clouds,
			GPUTypes: gpuTypes,
			MaxNodes: maxNodes,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseActive,
		},
	}
}

func readySuperplaneNode(name, namespace, poolRef, gpuType string, gpuCount int32) *superplanev1.SuperplaneNode {
	return &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: poolRef,
			Cloud:       "aws",
			GPUType:     gpuType,
			GPUCount:    gpuCount,
		},
		Status: superplanev1.SuperplaneNodeStatus{
			Phase: superplanev1.SuperplaneNodePhaseReady,
		},
	}
}

// ---------------------------------------------------------------------------
// Unit tests: isPendingUnschedulable
// ---------------------------------------------------------------------------

func TestIsPendingUnschedulable(t *testing.T) {
	tests := []struct {
		name     string
		pod      *corev1.Pod
		expected bool
	}{
		{
			name:     "pending and unschedulable",
			pod:      gpuPod("test", "default", 1, true, true),
			expected: true,
		},
		{
			name:     "pending but schedulable",
			pod:      gpuPod("test", "default", 1, true, false),
			expected: false,
		},
		{
			name: "running pod",
			pod: func() *corev1.Pod {
				p := gpuPod("test", "default", 1, false, false)
				p.Status.Phase = corev1.PodRunning
				return p
			}(),
			expected: false,
		},
		{
			name: "pending with PodScheduled=True",
			pod: func() *corev1.Pod {
				p := gpuPod("test", "default", 1, true, false)
				p.Status.Conditions = []corev1.PodCondition{
					{Type: corev1.PodScheduled, Status: corev1.ConditionTrue},
				}
				return p
			}(),
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := isPendingUnschedulable(tt.pod)
			if got != tt.expected {
				t.Errorf("isPendingUnschedulable() = %v, want %v", got, tt.expected)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Unit tests: extractGPURequest
// ---------------------------------------------------------------------------

func TestExtractGPURequest(t *testing.T) {
	tests := []struct {
		name     string
		pod      *corev1.Pod
		expected int64
	}{
		{
			name:     "single container with 1 GPU",
			pod:      gpuPod("test", "default", 1, true, true),
			expected: 1,
		},
		{
			name:     "single container with 4 GPUs",
			pod:      gpuPod("test", "default", 4, true, true),
			expected: 4,
		},
		{
			name: "multiple containers with GPUs",
			pod: func() *corev1.Pod {
				p := gpuPod("test", "default", 2, true, true)
				p.Spec.Containers = append(p.Spec.Containers, corev1.Container{
					Name:  "sidecar",
					Image: "nvidia/cuda:12.0-base",
					Resources: corev1.ResourceRequirements{
						Requests: corev1.ResourceList{
							GPUResourceName: *resource.NewQuantity(1, resource.DecimalSI),
						},
					},
				})
				return p
			}(),
			expected: 3,
		},
		{
			name: "no GPU request",
			pod: &corev1.Pod{
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{
						{
							Name:  "cpu-workload",
							Image: "busybox",
							Resources: corev1.ResourceRequirements{
								Requests: corev1.ResourceList{
									corev1.ResourceCPU: *resource.NewQuantity(1, resource.DecimalSI),
								},
							},
						},
					},
				},
			},
			expected: 0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			qty := extractGPURequest(tt.pod)
			got := qty.Value()
			if got != tt.expected {
				t.Errorf("extractGPURequest() = %d, want %d", got, tt.expected)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Unit tests: PodFilter
// ---------------------------------------------------------------------------

func TestPodFilter(t *testing.T) {
	tests := []struct {
		name     string
		obj      client.Object
		expected bool
	}{
		{
			name:     "pending unschedulable GPU pod",
			obj:      gpuPod("test", "default", 1, true, true),
			expected: true,
		},
		{
			name:     "pending schedulable GPU pod",
			obj:      gpuPod("test", "default", 1, true, false),
			expected: false,
		},
		{
			name: "pending unschedulable non-GPU pod",
			obj: func() client.Object {
				p := &corev1.Pod{
					ObjectMeta: metav1.ObjectMeta{Name: "test", Namespace: "default"},
					Spec: corev1.PodSpec{
						Containers: []corev1.Container{
							{Name: "c", Image: "busybox"},
						},
					},
					Status: corev1.PodStatus{
						Phase: corev1.PodPending,
						Conditions: []corev1.PodCondition{
							{Type: corev1.PodScheduled, Status: corev1.ConditionFalse},
						},
					},
				}
				return p
			}(),
			expected: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := PodFilter(tt.obj)
			if got != tt.expected {
				t.Errorf("PodFilter() = %v, want %v", got, tt.expected)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Integration tests: Reconcile
// ---------------------------------------------------------------------------

func TestReconcile_IgnoresNonPendingPod(t *testing.T) {
	pod := gpuPod("running-pod", "default", 1, false, false)
	pod.Status.Phase = corev1.PodRunning

	c := newPodWatcherFakeClient(pod)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "running-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}
}

func TestReconcile_IgnoresNonGPUPod(t *testing.T) {
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "cpu-pod", Namespace: "default"},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{
				{Name: "c", Image: "busybox"},
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodPending,
			Conditions: []corev1.PodCondition{
				{Type: corev1.PodScheduled, Status: corev1.ConditionFalse},
			},
		},
	}

	c := newPodWatcherFakeClient(pod)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "cpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}
}

func TestReconcile_IgnoresDeletedPod(t *testing.T) {
	c := newPodWatcherFakeClient() // No pods in the store
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gone", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}
}

func TestReconcile_SkipsIfExistingNodeCanFit(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	node := readySuperplaneNode("existing-node", "default", "gpu-pool", "H100", 1)
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 5)

	c := newPodWatcherFakeClient(pod, node, pool)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue (existing node fits), got %v", result.RequeueAfter)
	}
}

func TestReconcile_DebouncesBeforeProvisioning(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 5)

	c := newPodWatcherFakeClient(pod, pool)

	now := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	r := &PodWatcherReconciler{
		Client: c,
		Clock:  func() time.Time { return now },
	}

	// First reconcile — should set annotation and requeue for debounce.
	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != DebounceDuration {
		t.Errorf("expected requeue after %v, got %v", DebounceDuration, result.RequeueAfter)
	}

	// Verify annotation was set.
	var updatedPod corev1.Pod
	if err := c.Get(context.Background(), types.NamespacedName{Name: "gpu-pod", Namespace: "default"}, &updatedPod); err != nil {
		t.Fatalf("get pod: %v", err)
	}
	if updatedPod.Annotations[annotationFirstSeen] != now.Format(time.RFC3339) {
		t.Errorf("expected first-seen annotation %q, got %q",
			now.Format(time.RFC3339), updatedPod.Annotations[annotationFirstSeen])
	}
}

func TestReconcile_CreatesNodeAfterDebounce(t *testing.T) {
	firstSeen := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	pod := gpuPod("gpu-pod", "default", 2, true, true)
	pod.Annotations = map[string]string{
		annotationFirstSeen: firstSeen.Format(time.RFC3339),
	}
	pool := activeNodePool("gpu-pool", []string{"nebius", "aws"}, []string{"H100", "L40S"}, 10)

	c := newPodWatcherFakeClient(pod, pool)

	// Time is now 15s after first seen — past the debounce window.
	now := firstSeen.Add(15 * time.Second)
	r := &PodWatcherReconciler{
		Client: c,
		Clock:  func() time.Time { return now },
	}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue after provisioning, got %v", result.RequeueAfter)
	}

	// Verify a SuperplaneNode was created.
	var nodes superplanev1.SuperplaneNodeList
	if err := c.List(context.Background(), &nodes); err != nil {
		t.Fatalf("list nodes: %v", err)
	}
	if len(nodes.Items) != 1 {
		t.Fatalf("expected 1 SuperplaneNode, got %d", len(nodes.Items))
	}

	spNode := nodes.Items[0]
	if spNode.Spec.NodePoolRef != "gpu-pool" {
		t.Errorf("expected nodePoolRef 'gpu-pool', got %q", spNode.Spec.NodePoolRef)
	}
	if spNode.Spec.Cloud != "nebius" {
		t.Errorf("expected cloud 'nebius' (first in pool), got %q", spNode.Spec.Cloud)
	}
	if spNode.Spec.GPUType != "H100" {
		t.Errorf("expected gpuType 'H100' (first in pool), got %q", spNode.Spec.GPUType)
	}
	if spNode.Spec.GPUCount != 2 {
		t.Errorf("expected gpuCount 2, got %d", spNode.Spec.GPUCount)
	}
	if spNode.Labels[labelManagedBy] != "pod-watcher" {
		t.Errorf("expected managed-by label 'pod-watcher', got %q", spNode.Labels[labelManagedBy])
	}

	// Verify pod was marked as provisioning-triggered.
	var updatedPod corev1.Pod
	if err := c.Get(context.Background(), types.NamespacedName{Name: "gpu-pod", Namespace: "default"}, &updatedPod); err != nil {
		t.Fatalf("get pod: %v", err)
	}
	if updatedPod.Annotations[annotationProvisioningTriggered] != "true" {
		t.Errorf("expected provisioning-triggered annotation, got %q",
			updatedPod.Annotations[annotationProvisioningTriggered])
	}
}

func TestReconcile_SkipsAlreadyTriggeredPod(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	pod.Annotations = map[string]string{
		annotationProvisioningTriggered: "true",
	}
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 5)

	c := newPodWatcherFakeClient(pod, pool)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}

	// Verify no SuperplaneNode was created.
	var nodes superplanev1.SuperplaneNodeList
	if err := c.List(context.Background(), &nodes); err != nil {
		t.Fatalf("list nodes: %v", err)
	}
	if len(nodes.Items) != 0 {
		t.Errorf("expected 0 SuperplaneNodes, got %d", len(nodes.Items))
	}
}

func TestReconcile_NoMatchingNodePool(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	// NodePool is inactive — won't match.
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{Name: "gpu-pool"},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"aws"},
			GPUTypes: []string{"H100"},
			MaxNodes: 5,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseInactive,
		},
	}

	c := newPodWatcherFakeClient(pod, pool)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 30*time.Second {
		t.Errorf("expected 30s requeue for no match, got %v", result.RequeueAfter)
	}
}

func TestReconcile_RespectsMaxNodes(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 2)
	pool.Status.ReadyNodes = 2 // At capacity.

	c := newPodWatcherFakeClient(pod, pool)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// No matching pool (at max) -> 30s requeue.
	if result.RequeueAfter != 30*time.Second {
		t.Errorf("expected 30s requeue when at max nodes, got %v", result.RequeueAfter)
	}
}

func TestReconcile_RespectsBudget(t *testing.T) {
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	maxCost := float64(10.0)
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 10)
	pool.Spec.MaxCostPerHour = &maxCost
	pool.Status.CurrentCostPerHour = 10.0 // At budget.

	c := newPodWatcherFakeClient(pod, pool)
	r := &PodWatcherReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 30*time.Second {
		t.Errorf("expected 30s requeue when at budget, got %v", result.RequeueAfter)
	}
}

func TestReconcile_RespectsMaxConcurrentProvisioning(t *testing.T) {
	firstSeen := time.Date(2026, 3, 28, 12, 0, 0, 0, time.UTC)
	pod := gpuPod("gpu-pod", "default", 1, true, true)
	pod.Annotations = map[string]string{
		annotationFirstSeen: firstSeen.Format(time.RFC3339),
	}

	maxConcurrent := int32(2)
	pool := activeNodePool("gpu-pool", []string{"aws"}, []string{"H100"}, 10)
	pool.Spec.MaxConcurrentProvisioning = &maxConcurrent
	pool.Status.ProvisioningNodes = 2 // At max concurrent.

	c := newPodWatcherFakeClient(pod, pool)

	now := firstSeen.Add(15 * time.Second)
	r := &PodWatcherReconciler{
		Client: c,
		Clock:  func() time.Time { return now },
	}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pod", Namespace: "default"},
	})

	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 15*time.Second {
		t.Errorf("expected 15s requeue when at max provisioning, got %v", result.RequeueAfter)
	}
}

// ---------------------------------------------------------------------------
// Unit test: matchNodePool
// ---------------------------------------------------------------------------

func TestMatchNodePool(t *testing.T) {
	activePool := activeNodePool("active-pool", []string{"aws"}, []string{"H100"}, 5)
	inactivePool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{Name: "inactive-pool"},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"gcp"},
			GPUTypes: []string{"A100"},
			MaxNodes: 3,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseInactive,
		},
	}
	fullPool := activeNodePool("full-pool", []string{"aws"}, []string{"H100"}, 2)
	fullPool.Status.ReadyNodes = 2

	c := newPodWatcherFakeClient(activePool, inactivePool, fullPool)
	r := &PodWatcherReconciler{Client: c}

	pool, err := r.matchNodePool(context.Background(), 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if pool == nil {
		t.Fatal("expected a matching pool, got nil")
	}
	if pool.Name != "active-pool" {
		t.Errorf("expected 'active-pool', got %q", pool.Name)
	}
}
