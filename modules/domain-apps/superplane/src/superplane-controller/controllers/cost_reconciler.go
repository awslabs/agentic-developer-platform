package controllers

import (
	"context"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

const (
	// CostReconcileInterval is how often the cost reconciler runs.
	CostReconcileInterval = 60 * time.Second

	// BudgetWarningThresholdPct triggers a warning condition on the NodePool.
	BudgetWarningThresholdPct = 80.0

	// BudgetExceededThresholdPct triggers enforcement action.
	BudgetExceededThresholdPct = 100.0

	// ConditionTypeBudget is the condition type for budget status.
	ConditionTypeBudget = "BudgetCompliant"
)

// CostAggregation holds aggregated cost data for a NodePool.
type CostAggregation struct {
	// NodePoolName is the name of the NodePool.
	NodePoolName string

	// TotalNodes is the number of active (non-terminated) nodes.
	TotalNodes int

	// TotalGPUs is the total GPU count across active nodes.
	TotalGPUs int32

	// HourlyCostUSD is the sum of all active nodes' hourly costs.
	HourlyCostUSD float64

	// DailyCostEstimateUSD is 24h projection of the hourly cost.
	DailyCostEstimateUSD float64
}

// CostReconciler watches SuperplaneNodes and aggregates cost data per NodePool.
// It sets a BudgetCompliant condition on NodePools when cost thresholds are breached.
type CostReconciler struct {
	client.Client
	Clock Clock
}

// SetupWithManager registers the CostReconciler with the controller manager.
func (r *CostReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Clock == nil {
		r.Clock = RealClock{}
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&superplanev1.NodePool{}).
		Owns(&superplanev1.SuperplaneNode{}).
		Named("cost-reconciler").
		Complete(r)
}

// Reconcile aggregates costs for all SuperplaneNodes belonging to a NodePool
// and updates the NodePool status with cost information and budget compliance.
func (r *CostReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithName("cost-reconciler")

	// Fetch the NodePool.
	var pool superplanev1.NodePool
	if err := r.Get(ctx, req.NamespacedName, &pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Skip deleted pools.
	if pool.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}

	// Skip inactive pools.
	if pool.Status.Phase != superplanev1.NodePoolPhaseActive {
		return ctrl.Result{RequeueAfter: CostReconcileInterval}, nil
	}

	// Aggregate costs from SuperplaneNodes belonging to this pool.
	agg, err := r.aggregateCosts(ctx, &pool)
	if err != nil {
		logger.Error(err, "Failed to aggregate costs", "nodePool", pool.Name)
		return ctrl.Result{RequeueAfter: CostReconcileInterval}, err
	}

	// Update NodePool status with cost data and budget compliance.
	updated := r.updatePoolStatus(ctx, &pool, agg)

	if updated {
		if err := r.Status().Update(ctx, &pool); err != nil {
			return ctrl.Result{}, fmt.Errorf("update NodePool status: %w", err)
		}
		logger.Info("Updated NodePool cost status",
			"nodePool", pool.Name,
			"totalNodes", agg.TotalNodes,
			"totalGPUs", agg.TotalGPUs,
			"hourlyCostUSD", agg.HourlyCostUSD,
			"dailyEstimateUSD", agg.DailyCostEstimateUSD,
		)
	}

	return ctrl.Result{RequeueAfter: CostReconcileInterval}, nil
}

// aggregateCosts lists all SuperplaneNodes for a NodePool and sums their costs.
func (r *CostReconciler) aggregateCosts(ctx context.Context, pool *superplanev1.NodePool) (*CostAggregation, error) {
	var nodeList superplanev1.SuperplaneNodeList
	if err := r.List(ctx, &nodeList, client.InNamespace(pool.Namespace), client.MatchingLabels{
		"superplane.ai/nodepool": pool.Name,
	}); err != nil {
		return nil, fmt.Errorf("list SuperplaneNodes: %w", err)
	}

	agg := &CostAggregation{
		NodePoolName: pool.Name,
	}

	for i := range nodeList.Items {
		node := &nodeList.Items[i]

		// Only count active nodes (not Terminated or Failed).
		phase := node.Status.Phase
		if phase == superplanev1.SuperplaneNodePhaseTerminated ||
			phase == superplanev1.SuperplaneNodePhaseFailed {
			continue
		}

		agg.TotalNodes++
		agg.TotalGPUs += node.Spec.GPUCount
		agg.HourlyCostUSD += node.Status.HourlyCost
	}

	agg.DailyCostEstimateUSD = agg.HourlyCostUSD * 24

	return agg, nil
}

// updatePoolStatus updates the NodePool status fields with cost data and
// sets the BudgetCompliant condition. Returns true if status was changed.
func (r *CostReconciler) updatePoolStatus(_ context.Context, pool *superplanev1.NodePool, agg *CostAggregation) bool {
	now := r.Clock.Now()
	updated := false

	// Update CurrentCostPerHour on the pool status.
	if pool.Status.CurrentCostPerHour != agg.HourlyCostUSD {
		pool.Status.CurrentCostPerHour = agg.HourlyCostUSD
		updated = true
	}

	// Determine budget compliance from both node count and hourly cost limits.
	var budgetStatus metav1.ConditionStatus
	var budgetReason, budgetMessage string

	nodeExceeded := false
	nodeWarning := false
	costExceeded := false
	costWarning := false

	// Check node count vs MaxNodes.
	if pool.Spec.MaxNodes > 0 {
		usagePct := float64(agg.TotalNodes) / float64(pool.Spec.MaxNodes) * 100
		if usagePct >= BudgetExceededThresholdPct {
			nodeExceeded = true
		} else if usagePct >= BudgetWarningThresholdPct {
			nodeWarning = true
		}
	}

	// Check hourly cost vs MaxCostPerHour.
	if pool.Spec.MaxCostPerHour != nil && *pool.Spec.MaxCostPerHour > 0 {
		costPct := agg.HourlyCostUSD / *pool.Spec.MaxCostPerHour * 100
		if costPct >= BudgetExceededThresholdPct {
			costExceeded = true
		} else if costPct >= BudgetWarningThresholdPct {
			costWarning = true
		}
	}

	hasBudget := pool.Spec.MaxNodes > 0 || (pool.Spec.MaxCostPerHour != nil && *pool.Spec.MaxCostPerHour > 0)

	switch {
	case !hasBudget:
		budgetStatus = metav1.ConditionTrue
		budgetReason = "NoBudgetConfigured"
		budgetMessage = "No budget limits configured"
	case nodeExceeded || costExceeded:
		budgetStatus = metav1.ConditionFalse
		budgetReason = "BudgetExceeded"
		budgetMessage = fmt.Sprintf(
			"Node count %d/%d, hourly cost $%.2f, daily estimate $%.2f",
			agg.TotalNodes, pool.Spec.MaxNodes,
			agg.HourlyCostUSD, agg.DailyCostEstimateUSD,
		)
	case nodeWarning || costWarning:
		budgetStatus = metav1.ConditionFalse
		budgetReason = "BudgetWarning"
		budgetMessage = fmt.Sprintf(
			"Approaching limits — nodes %d/%d, hourly cost $%.2f, daily estimate $%.2f",
			agg.TotalNodes, pool.Spec.MaxNodes,
			agg.HourlyCostUSD, agg.DailyCostEstimateUSD,
		)
	default:
		budgetStatus = metav1.ConditionTrue
		budgetReason = "WithinBudget"
		budgetMessage = fmt.Sprintf(
			"Node count %d/%d, hourly cost $%.2f, daily estimate $%.2f",
			agg.TotalNodes, pool.Spec.MaxNodes,
			agg.HourlyCostUSD, agg.DailyCostEstimateUSD,
		)
	}

	updated = setCondition(&pool.Status.Conditions, metav1.Condition{
		Type:               ConditionTypeBudget,
		Status:             budgetStatus,
		ObservedGeneration: pool.Generation,
		LastTransitionTime: metav1.NewTime(now),
		Reason:             budgetReason,
		Message:            budgetMessage,
	}) || updated

	return updated
}
