package controllers

import (
	"context"
	"math"
	"testing"
	"time"

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

type costFakeClock struct {
	now time.Time
}

func (f *costFakeClock) Now() time.Time { return f.now }

func newCostReconcilerFakeClient(objs ...client.Object) client.Client {
	return fake.NewClientBuilder().
		WithScheme(newScheme()).
		WithObjects(objs...).
		WithStatusSubresource(&superplanev1.NodePool{}, &superplanev1.SuperplaneNode{}).
		Build()
}

func makeCostNodePool(name string, maxNodes int32, phase superplanev1.NodePoolPhase) *superplanev1.NodePool {
	return &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:   []string{"aws"},
			GPUTypes: []string{"H100"},
			MaxNodes: maxNodes,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: phase,
		},
	}
}

func makeCostNode(name, namespace, poolRef string, gpuCount int32, hourlyCost float64, phase superplanev1.SuperplaneNodePhase) *superplanev1.SuperplaneNode {
	return &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				"superplane.ai/nodepool": poolRef,
			},
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: poolRef,
			Cloud:       "aws",
			GPUType:     "H100",
			GPUCount:    gpuCount,
		},
		Status: superplanev1.SuperplaneNodeStatus{
			Phase:      phase,
			HourlyCost: hourlyCost,
		},
	}
}

// ---------------------------------------------------------------------------
// Tests: aggregateCosts
// ---------------------------------------------------------------------------

func TestAggregateCosts_NoNodes(t *testing.T) {
	pool := makeCostNodePool("test-pool", 5, superplanev1.NodePoolPhaseActive)
	c := newCostReconcilerFakeClient(pool)
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	agg, err := r.aggregateCosts(context.Background(), pool)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if agg.TotalNodes != 0 {
		t.Errorf("expected 0 nodes, got %d", agg.TotalNodes)
	}
	if agg.TotalGPUs != 0 {
		t.Errorf("expected 0 GPUs, got %d", agg.TotalGPUs)
	}
	if agg.HourlyCostUSD != 0 {
		t.Errorf("expected 0 hourly cost, got %f", agg.HourlyCostUSD)
	}
}

func TestAggregateCosts_ActiveNodes(t *testing.T) {
	pool := makeCostNodePool("gpu-pool", 10, superplanev1.NodePoolPhaseActive)
	node1 := makeCostNode("node-1", "default", "gpu-pool", 4, 6.88, superplanev1.SuperplaneNodePhaseReady)
	node2 := makeCostNode("node-2", "default", "gpu-pool", 2, 3.50, superplanev1.SuperplaneNodePhaseReady)

	c := newCostReconcilerFakeClient(pool, node1, node2)
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	agg, err := r.aggregateCosts(context.Background(), pool)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if agg.TotalNodes != 2 {
		t.Errorf("expected 2 nodes, got %d", agg.TotalNodes)
	}
	if agg.TotalGPUs != 6 {
		t.Errorf("expected 6 GPUs, got %d", agg.TotalGPUs)
	}
	expectedHourly := 6.88 + 3.50
	if math.Abs(agg.HourlyCostUSD-expectedHourly) > 1e-9 {
		t.Errorf("expected hourly cost %f, got %f", expectedHourly, agg.HourlyCostUSD)
	}
	expectedDaily := expectedHourly * 24
	if math.Abs(agg.DailyCostEstimateUSD-expectedDaily) > 1e-9 {
		t.Errorf("expected daily estimate %f, got %f", expectedDaily, agg.DailyCostEstimateUSD)
	}
}

func TestAggregateCosts_ExcludesTerminated(t *testing.T) {
	pool := makeCostNodePool("gpu-pool", 10, superplanev1.NodePoolPhaseActive)
	activeNode := makeCostNode("active-1", "default", "gpu-pool", 4, 6.88, superplanev1.SuperplaneNodePhaseReady)
	terminatedNode := makeCostNode("term-1", "default", "gpu-pool", 4, 6.88, superplanev1.SuperplaneNodePhaseTerminated)
	failedNode := makeCostNode("fail-1", "default", "gpu-pool", 2, 3.50, superplanev1.SuperplaneNodePhaseFailed)

	c := newCostReconcilerFakeClient(pool, activeNode, terminatedNode, failedNode)
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	agg, err := r.aggregateCosts(context.Background(), pool)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if agg.TotalNodes != 1 {
		t.Errorf("expected 1 active node (excluding terminated+failed), got %d", agg.TotalNodes)
	}
	if agg.TotalGPUs != 4 {
		t.Errorf("expected 4 GPUs, got %d", agg.TotalGPUs)
	}
}

// ---------------------------------------------------------------------------
// Tests: updatePoolStatus (budget compliance)
// ---------------------------------------------------------------------------

func TestUpdatePoolStatus_WithinBudget(t *testing.T) {
	pool := makeCostNodePool("test-pool", 10, superplanev1.NodePoolPhaseActive)
	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	agg := &CostAggregation{
		NodePoolName:         "test-pool",
		TotalNodes:           3,
		TotalGPUs:            12,
		HourlyCostUSD:        20.64,
		DailyCostEstimateUSD: 495.36,
	}

	updated := r.updatePoolStatus(context.Background(), pool, agg)
	if !updated {
		t.Error("expected status to be updated")
	}

	// Should have BudgetCompliant=True
	found := false
	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			found = true
			if c.Status != metav1.ConditionTrue {
				t.Errorf("expected BudgetCompliant=True, got %s", c.Status)
			}
			if c.Reason != "WithinBudget" {
				t.Errorf("expected reason WithinBudget, got %s", c.Reason)
			}
		}
	}
	if !found {
		t.Error("BudgetCompliant condition not found")
	}
}

func TestUpdatePoolStatus_WarningThreshold(t *testing.T) {
	pool := makeCostNodePool("test-pool", 10, superplanev1.NodePoolPhaseActive)
	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	// 8 out of 10 nodes = 80%
	agg := &CostAggregation{
		NodePoolName:         "test-pool",
		TotalNodes:           8,
		TotalGPUs:            32,
		HourlyCostUSD:        55.04,
		DailyCostEstimateUSD: 1320.96,
	}

	r.updatePoolStatus(context.Background(), pool, agg)

	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			if c.Reason != "BudgetWarning" {
				t.Errorf("expected reason BudgetWarning, got %s", c.Reason)
			}
			if c.Status != metav1.ConditionFalse {
				t.Errorf("expected BudgetCompliant=False for warning, got %s", c.Status)
			}
			return
		}
	}
	t.Error("BudgetCompliant condition not found")
}

func TestUpdatePoolStatus_ExceededThreshold(t *testing.T) {
	pool := makeCostNodePool("test-pool", 5, superplanev1.NodePoolPhaseActive)
	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	// 5 out of 5 nodes = 100%
	agg := &CostAggregation{
		NodePoolName:         "test-pool",
		TotalNodes:           5,
		TotalGPUs:            20,
		HourlyCostUSD:        34.40,
		DailyCostEstimateUSD: 825.60,
	}

	r.updatePoolStatus(context.Background(), pool, agg)

	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			if c.Reason != "BudgetExceeded" {
				t.Errorf("expected reason BudgetExceeded, got %s", c.Reason)
			}
			return
		}
	}
	t.Error("BudgetCompliant condition not found")
}

func TestUpdatePoolStatus_NoBudgetConfigured(t *testing.T) {
	pool := makeCostNodePool("test-pool", 0, superplanev1.NodePoolPhaseActive)
	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	agg := &CostAggregation{
		NodePoolName: "test-pool",
		TotalNodes:   3,
	}

	r.updatePoolStatus(context.Background(), pool, agg)

	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			if c.Reason != "NoBudgetConfigured" {
				t.Errorf("expected reason NoBudgetConfigured, got %s", c.Reason)
			}
			if c.Status != metav1.ConditionTrue {
				t.Errorf("expected BudgetCompliant=True when no budget, got %s", c.Status)
			}
			return
		}
	}
	t.Error("BudgetCompliant condition not found")
}

// ---------------------------------------------------------------------------
// Tests: Full Reconcile cycle
// ---------------------------------------------------------------------------

func TestCostReconciler_ReconcileActivePool(t *testing.T) {
	pool := makeCostNodePool("gpu-pool", 10, superplanev1.NodePoolPhaseActive)
	node1 := makeCostNode("node-1", "default", "gpu-pool", 4, 6.88, superplanev1.SuperplaneNodePhaseReady)

	c := newCostReconcilerFakeClient(pool, node1)
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "gpu-pool", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != CostReconcileInterval {
		t.Errorf("expected requeue after %v, got %v", CostReconcileInterval, result.RequeueAfter)
	}

	// Verify the NodePool has a BudgetCompliant condition now.
	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "gpu-pool", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	found := false
	for _, cond := range updated.Status.Conditions {
		if cond.Type == ConditionTypeBudget {
			found = true
			break
		}
	}
	if !found {
		t.Error("expected BudgetCompliant condition after reconcile")
	}
}

func TestCostReconciler_SkipsInactivePool(t *testing.T) {
	pool := makeCostNodePool("inactive-pool", 10, superplanev1.NodePoolPhaseInactive)
	c := newCostReconcilerFakeClient(pool)
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "inactive-pool", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != CostReconcileInterval {
		t.Errorf("expected requeue after %v, got %v", CostReconcileInterval, result.RequeueAfter)
	}

	// Should NOT have BudgetCompliant condition (skipped).
	var updated superplanev1.NodePool
	if err := c.Get(context.Background(), types.NamespacedName{Name: "inactive-pool", Namespace: "default"}, &updated); err != nil {
		t.Fatalf("get pool: %v", err)
	}
	for _, cond := range updated.Status.Conditions {
		if cond.Type == ConditionTypeBudget {
			t.Error("inactive pool should not have BudgetCompliant condition")
		}
	}
}

func TestCostReconciler_NotFoundIsIgnored(t *testing.T) {
	c := newCostReconcilerFakeClient()
	r := &CostReconciler{Client: c, Clock: &costFakeClock{now: time.Now()}}

	result, err := r.Reconcile(context.Background(), ctrl.Request{
		NamespacedName: types.NamespacedName{Name: "nonexistent", Namespace: "default"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.RequeueAfter != 0 {
		t.Errorf("expected no requeue, got %v", result.RequeueAfter)
	}
}

// ---------------------------------------------------------------------------
// Tests: Threshold constants
// ---------------------------------------------------------------------------

func TestBudgetThresholdConstants(t *testing.T) {
	if BudgetWarningThresholdPct != 80.0 {
		t.Errorf("expected warning threshold 80, got %f", BudgetWarningThresholdPct)
	}
	if BudgetExceededThresholdPct != 100.0 {
		t.Errorf("expected exceeded threshold 100, got %f", BudgetExceededThresholdPct)
	}
	if BudgetWarningThresholdPct >= BudgetExceededThresholdPct {
		t.Error("warning threshold should be less than exceeded threshold")
	}
}

func TestCostReconcileInterval(t *testing.T) {
	if CostReconcileInterval != 60*time.Second {
		t.Errorf("expected 60s reconcile interval, got %v", CostReconcileInterval)
	}
}

// ---------------------------------------------------------------------------
// Tests: MaxCostPerHour enforcement
// ---------------------------------------------------------------------------

func TestUpdatePoolStatus_CostExceeded(t *testing.T) {
	maxCost := 10.0
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "cost-pool",
			Namespace: "default",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:         []string{"aws"},
			GPUTypes:       []string{"H100"},
			MaxNodes:       100, // high node limit
			MaxCostPerHour: &maxCost,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseActive,
		},
	}

	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	// Hourly cost exceeds the $10/hr limit
	agg := &CostAggregation{
		NodePoolName:         "cost-pool",
		TotalNodes:           3,
		TotalGPUs:            12,
		HourlyCostUSD:        15.0,
		DailyCostEstimateUSD: 360.0,
	}

	r.updatePoolStatus(context.Background(), pool, agg)

	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			if c.Reason != "BudgetExceeded" {
				t.Errorf("expected BudgetExceeded when cost exceeds limit, got %s", c.Reason)
			}
			return
		}
	}
	t.Error("BudgetCompliant condition not found")
}

func TestUpdatePoolStatus_CostWarning(t *testing.T) {
	maxCost := 10.0
	pool := &superplanev1.NodePool{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "cost-pool",
			Namespace: "default",
		},
		Spec: superplanev1.NodePoolSpec{
			Clouds:         []string{"aws"},
			GPUTypes:       []string{"H100"},
			MaxNodes:       100,
			MaxCostPerHour: &maxCost,
		},
		Status: superplanev1.NodePoolStatus{
			Phase: superplanev1.NodePoolPhaseActive,
		},
	}

	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	// Hourly cost at 85% of the $10/hr limit
	agg := &CostAggregation{
		NodePoolName:         "cost-pool",
		TotalNodes:           2,
		TotalGPUs:            8,
		HourlyCostUSD:        8.5,
		DailyCostEstimateUSD: 204.0,
	}

	r.updatePoolStatus(context.Background(), pool, agg)

	for _, c := range pool.Status.Conditions {
		if c.Type == ConditionTypeBudget {
			if c.Reason != "BudgetWarning" {
				t.Errorf("expected BudgetWarning when cost approaches limit, got %s", c.Reason)
			}
			return
		}
	}
	t.Error("BudgetCompliant condition not found")
}

func TestUpdatePoolStatus_UpdatesCurrentCostPerHour(t *testing.T) {
	pool := makeCostNodePool("test-pool", 10, superplanev1.NodePoolPhaseActive)
	pool.Status.CurrentCostPerHour = 0

	r := &CostReconciler{Clock: &costFakeClock{now: time.Now()}}

	agg := &CostAggregation{
		NodePoolName:         "test-pool",
		TotalNodes:           2,
		TotalGPUs:            8,
		HourlyCostUSD:        13.76,
		DailyCostEstimateUSD: 330.24,
	}

	updated := r.updatePoolStatus(context.Background(), pool, agg)
	if !updated {
		t.Error("expected status update")
	}
	if pool.Status.CurrentCostPerHour != 13.76 {
		t.Errorf("expected CurrentCostPerHour 13.76, got %f", pool.Status.CurrentCostPerHour)
	}
}
