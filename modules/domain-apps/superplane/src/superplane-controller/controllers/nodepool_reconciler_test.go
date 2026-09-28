package controllers

import (
	"context"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

// ---------------------------------------------------------------------------
// Unit tests: computeDesiredPhase
// ---------------------------------------------------------------------------

func TestComputeDesiredPhase(t *testing.T) {
	tests := []struct {
		name     string
		pool     *superplanev1.NodePool
		expected superplanev1.NodePoolPhase
	}{
		{
			name: "valid spec -> Active",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					Clouds:   []string{"aws"},
					GPUTypes: []string{"H100"},
					MaxNodes: 5,
				},
			},
			expected: superplanev1.NodePoolPhaseActive,
		},
		{
			name: "multiple clouds and GPU types -> Active",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					Clouds:   []string{"aws", "nebius", "lambda"},
					GPUTypes: []string{"H100", "A100", "A10G"},
					MaxNodes: 10,
				},
			},
			expected: superplanev1.NodePoolPhaseActive,
		},
		{
			name: "no clouds -> Inactive",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					Clouds:   []string{},
					GPUTypes: []string{"H100"},
					MaxNodes: 5,
				},
			},
			expected: superplanev1.NodePoolPhaseInactive,
		},
		{
			name: "nil clouds -> Inactive",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					GPUTypes: []string{"H100"},
					MaxNodes: 5,
				},
			},
			expected: superplanev1.NodePoolPhaseInactive,
		},
		{
			name: "no GPU types -> Inactive",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					Clouds:   []string{"aws"},
					GPUTypes: []string{},
					MaxNodes: 5,
				},
			},
			expected: superplanev1.NodePoolPhaseInactive,
		},
		{
			name: "maxNodes zero -> Inactive",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{
					Clouds:   []string{"aws"},
					GPUTypes: []string{"H100"},
					MaxNodes: 0,
				},
			},
			expected: superplanev1.NodePoolPhaseInactive,
		},
		{
			name: "completely empty spec -> Inactive",
			pool: &superplanev1.NodePool{
				Spec: superplanev1.NodePoolSpec{},
			},
			expected: superplanev1.NodePoolPhaseInactive,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := computeDesiredPhase(tt.pool)
			if got != tt.expected {
				t.Errorf("computeDesiredPhase() = %q, want %q", got, tt.expected)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Integration tests: NodePoolReconciler.Reconcile
// ---------------------------------------------------------------------------

func TestNodePoolReconcile_SetsActiveOnValidSpec(t *testing.T) {
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: "test-pool",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"aws"},
			GPUTypes: []string{"A10G"},
			MaxNodes: 1,
		},
		// Phase is empty — this is the bug scenario.
	}

	c := newPodWatcherFakeClient(pool)
	r := &NodePoolReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "test-pool"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}

	// Verify the phase was set to Active.
	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "test-pool"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	if updated.Status.Phase != superplanev1.NodePoolPhaseActive {
		t.Errorf("expected phase Active, got %q", updated.Status.Phase)
	}
}

func TestNodePoolReconcile_SetsInactiveOnInvalidSpec(t *testing.T) {
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: "bad-pool",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{},
			GPUTypes: []string{"H100"},
			MaxNodes: 5,
		},
	}

	c := newPodWatcherFakeClient(pool)
	r := &NodePoolReconciler{Client: c}

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "bad-pool"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "bad-pool"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	if updated.Status.Phase != superplanev1.NodePoolPhaseInactive {
		t.Errorf("expected phase Inactive, got %q", updated.Status.Phase)
	}
}

func TestNodePoolReconcile_NoUpdateWhenPhaseUnchanged(t *testing.T) {
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: "already-active",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"aws"},
			GPUTypes: []string{"H100"},
			MaxNodes: 5,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseActive,
		},
	}

	c := newPodWatcherFakeClient(pool)
	r := &NodePoolReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "already-active"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}

	// Phase should still be Active.
	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "already-active"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	if updated.Status.Phase != superplanev1.NodePoolPhaseActive {
		t.Errorf("expected phase Active, got %q", updated.Status.Phase)
	}
}

func TestNodePoolReconcile_NotFoundIsIgnored(t *testing.T) {
	c := newPodWatcherFakeClient() // No objects.
	r := &NodePoolReconciler{Client: c}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "nonexistent"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}
}

func TestNodePoolReconcile_TransitionsFromActiveToInactive(t *testing.T) {
	// Simulate a pool that was Active but spec changed to invalid.
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name: "downgraded-pool",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"aws"},
			GPUTypes: []string{}, // No GPU types — invalid.
			MaxNodes: 5,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseActive,
		},
	}

	c := newPodWatcherFakeClient(pool)
	r := &NodePoolReconciler{Client: c}

	_, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "downgraded-pool"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "downgraded-pool"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	if updated.Status.Phase != superplanev1.NodePoolPhaseInactive {
		t.Errorf("expected phase Inactive after spec downgrade, got %q", updated.Status.Phase)
	}
}
