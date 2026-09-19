package controllers

import (
	"context"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

// NodePoolReconciler watches NodePool CRs and sets their status.phase
// based on spec validation. This ensures PodWatcher's matchNodePool()
// can find Active pools.
type NodePoolReconciler struct {
	client.Client
}

// Reconcile validates the NodePool spec and sets the status phase accordingly.
// A NodePool is Active when it has at least one cloud, one GPU type, and maxNodes > 0.
// Otherwise it is set to Inactive.
func (r *NodePoolReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithName("nodepool-reconciler")

	var pool superplanev1.NodePool
	if err := r.Get(ctx, req.NamespacedName, &pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// Skip deleted resources.
	if pool.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}

	// Determine the desired phase based on spec validation.
	desiredPhase := computeDesiredPhase(&pool)

	// Only update if the phase has changed.
	if pool.Status.Phase != desiredPhase {
		logger.Info("Updating NodePool phase",
			"nodePool", pool.Name,
			"oldPhase", pool.Status.Phase,
			"newPhase", desiredPhase,
		)

		pool.Status.Phase = desiredPhase
		if err := r.Status().Update(ctx, &pool); err != nil {
			return ctrl.Result{}, err
		}
	}

	return ctrl.Result{}, nil
}

// computeDesiredPhase returns the desired NodePoolPhase based on spec validation.
// A NodePool is Active when:
//   - It has at least one cloud configured
//   - It has at least one GPU type configured
//   - MaxNodes is greater than zero
func computeDesiredPhase(pool *superplanev1.NodePool) superplanev1.NodePoolPhase {
	if len(pool.Spec.Clouds) > 0 && len(pool.Spec.GPUTypes) > 0 && pool.Spec.MaxNodes > 0 {
		return superplanev1.NodePoolPhaseActive
	}
	return superplanev1.NodePoolPhaseInactive
}

// SetupWithManager registers the NodePoolReconciler with the controller manager.
func (r *NodePoolReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&superplanev1.NodePool{}).
		Named("nodepool").
		Complete(r)
}
