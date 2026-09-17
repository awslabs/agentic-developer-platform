// Package controllers implements Kubernetes controllers for the Superplane platform.
package controllers

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

const (
	// HealthCheckInterval is how often the health monitor reconciles.
	HealthCheckInterval = 30 * time.Second

	// DegradedThreshold is the duration after which an unhealthy node is marked Degraded.
	DegradedThreshold = 5 * time.Minute

	// AutoRepairThreshold is the duration after which an unhealthy node triggers auto-repair.
	AutoRepairThreshold = 15 * time.Minute

	// ConditionTypeHealthy is the condition type for node health status.
	ConditionTypeHealthy = "Healthy"

	// ConditionTypeAutoRepair is the condition type tracking auto-repair actions.
	ConditionTypeAutoRepair = "AutoRepair"
)

// Clock abstracts time.Now for testability.
type Clock interface {
	Now() time.Time
}

// RealClock uses the real system time.
type RealClock struct{}

// Now returns the current time.
func (RealClock) Now() time.Time { return time.Now() }

// NodeGetter abstracts fetching a Kubernetes core Node by name.
// This allows tests to inject fake node lookups without a full API server.
type NodeGetter interface {
	GetNode(ctx context.Context, name string) (*corev1.Node, error)
}

// K8sNodeGetter fetches nodes via a controller-runtime client.
type K8sNodeGetter struct {
	Client client.Client
}

// GetNode retrieves a Kubernetes Node object by name.
func (g *K8sNodeGetter) GetNode(ctx context.Context, name string) (*corev1.Node, error) {
	var node corev1.Node
	if err := g.Client.Get(ctx, types.NamespacedName{Name: name}, &node); err != nil {
		return nil, err
	}
	return &node, nil
}

// HealthMonitorReconciler monitors SuperplaneNode health and triggers auto-repair.
type HealthMonitorReconciler struct {
	Client     client.Client
	Clock      Clock
	NodeGetter NodeGetter
}

// SetupWithManager registers the HealthMonitorReconciler with the controller manager.
// It reconciles all SuperplaneNodes every HealthCheckInterval (30s).
func (r *HealthMonitorReconciler) SetupWithManager(mgr ctrl.Manager) error {
	if r.Clock == nil {
		r.Clock = RealClock{}
	}
	if r.NodeGetter == nil {
		r.NodeGetter = &K8sNodeGetter{Client: r.Client}
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&superplanev1.SuperplaneNode{}).
		Complete(r)
}

// Reconcile checks a single SuperplaneNode's health and takes action if necessary.
//
// Health check logic:
//  1. Skip nodes not in Ready or Degraded phase (only monitor active nodes).
//  2. Look up the corresponding K8s Node by status.k8sNodeName.
//  3. Check the K8s Node's "Ready" condition and last heartbeat time.
//  4. If healthy: ensure the node is in Ready phase and clear any unhealthy condition.
//  5. If unhealthy > 5 min: mark SuperplaneNode as Degraded.
//  6. If unhealthy > 15 min: provision a replacement and drain the unhealthy node.
func (r *HealthMonitorReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("superplanenode", req.NamespacedName)

	// Fetch the SuperplaneNode.
	var spNode superplanev1.SuperplaneNode
	if err := r.Client.Get(ctx, req.NamespacedName, &spNode); err != nil {
		if errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	// Only monitor nodes in Ready or Degraded phase.
	// Nodes in other phases (Pending, Provisioning, Joining, Draining, Terminated, Failed)
	// are handled by other controllers.
	phase := spNode.Status.Phase
	if phase != superplanev1.SuperplaneNodePhaseReady && phase != superplanev1.SuperplaneNodePhaseDegraded {
		return ctrl.Result{RequeueAfter: HealthCheckInterval}, nil
	}

	// Need a K8s node name to check health.
	k8sNodeName := spNode.Status.K8sNodeName
	if k8sNodeName == "" {
		logger.V(1).Info("SuperplaneNode has no k8sNodeName, skipping health check")
		return ctrl.Result{RequeueAfter: HealthCheckInterval}, nil
	}

	// Look up the K8s node and assess health.
	healthy, reason := r.checkNodeHealth(ctx, k8sNodeName)
	now := r.Clock.Now()

	if healthy {
		return r.handleHealthy(ctx, &spNode, now, logger)
	}

	return r.handleUnhealthy(ctx, &spNode, now, reason, logger)
}

// checkNodeHealth inspects the K8s Node's Ready condition and heartbeat.
// Returns (healthy bool, reason string).
func (r *HealthMonitorReconciler) checkNodeHealth(ctx context.Context, nodeName string) (bool, string) {
	node, err := r.NodeGetter.GetNode(ctx, nodeName)
	if err != nil {
		if errors.IsNotFound(err) {
			return false, fmt.Sprintf("K8s node %q not found", nodeName)
		}
		return false, fmt.Sprintf("failed to get K8s node %q: %v", nodeName, err)
	}

	// Check the Ready condition.
	now := r.Clock.Now()
	for _, cond := range node.Status.Conditions {
		if cond.Type == corev1.NodeReady {
			if cond.Status != corev1.ConditionTrue {
				return false, fmt.Sprintf("K8s node %q condition Ready=%s: %s", nodeName, cond.Status, cond.Message)
			}

			// Check heartbeat freshness: if last heartbeat is older than 2 minutes, consider unhealthy.
			if cond.LastHeartbeatTime.Time.IsZero() {
				return true, ""
			}
			heartbeatAge := now.Sub(cond.LastHeartbeatTime.Time)
			if heartbeatAge > 2*time.Minute {
				return false, fmt.Sprintf("K8s node %q heartbeat stale (%s ago)", nodeName, heartbeatAge.Round(time.Second))
			}

			return true, ""
		}
	}

	return false, fmt.Sprintf("K8s node %q has no Ready condition", nodeName)
}

// handleHealthy processes a healthy node: restores Ready phase if Degraded and clears unhealthy condition.
func (r *HealthMonitorReconciler) handleHealthy(
	ctx context.Context,
	spNode *superplanev1.SuperplaneNode,
	now time.Time,
	logger interface{ Info(string, ...interface{}) },
) (ctrl.Result, error) {
	updated := false

	// If node was degraded, restore to Ready.
	if spNode.Status.Phase == superplanev1.SuperplaneNodePhaseDegraded {
		spNode.Status.Phase = superplanev1.SuperplaneNodePhaseReady
		spNode.Status.Message = "Node recovered, health checks passing"
		updated = true
		logger.Info("Node recovered from Degraded, restoring to Ready")
	}

	// Set healthy condition.
	updated = setCondition(&spNode.Status.Conditions, metav1.Condition{
		Type:               ConditionTypeHealthy,
		Status:             metav1.ConditionTrue,
		ObservedGeneration: spNode.Generation,
		LastTransitionTime: metav1.NewTime(now),
		Reason:             "HealthCheckPassed",
		Message:            "All health checks passing",
	}) || updated

	if updated {
		if err := r.Client.Status().Update(ctx, spNode); err != nil {
			return ctrl.Result{}, fmt.Errorf("update status (healthy): %w", err)
		}
	}

	return ctrl.Result{RequeueAfter: HealthCheckInterval}, nil
}

// handleUnhealthy processes an unhealthy node based on how long it has been unhealthy.
func (r *HealthMonitorReconciler) handleUnhealthy(
	ctx context.Context,
	spNode *superplanev1.SuperplaneNode,
	now time.Time,
	reason string,
	logger interface {
		Info(string, ...interface{})
	},
) (ctrl.Result, error) {
	// Determine when the node first became unhealthy.
	unhealthySince := getUnhealthySince(spNode.Status.Conditions, now)
	unhealthyDuration := now.Sub(unhealthySince)

	// Update unhealthy condition.
	setCondition(&spNode.Status.Conditions, metav1.Condition{
		Type:               ConditionTypeHealthy,
		Status:             metav1.ConditionFalse,
		ObservedGeneration: spNode.Generation,
		LastTransitionTime: metav1.NewTime(unhealthySince),
		Reason:             "HealthCheckFailed",
		Message:            reason,
	})

	switch {
	case unhealthyDuration >= AutoRepairThreshold:
		// Guard against duplicate auto-repair: if AutoRepair condition is already set,
		// a replacement was already created. Skip to avoid creating multiple replacements
		// (e.g., if a previous status update failed after creating the replacement).
		if hasCondition(spNode.Status.Conditions, ConditionTypeAutoRepair, metav1.ConditionTrue) {
			logger.Info("Auto-repair already triggered, skipping duplicate",
				"unhealthySince", unhealthySince,
			)
			break
		}

		// Auto-repair: provision replacement and drain this node.
		logger.Info("Node unhealthy for >15 min, triggering auto-repair",
			"unhealthySince", unhealthySince,
			"duration", unhealthyDuration.Round(time.Second),
			"reason", reason,
		)

		if err := r.triggerAutoRepair(ctx, spNode, now, reason); err != nil {
			return ctrl.Result{}, fmt.Errorf("auto-repair: %w", err)
		}

	case unhealthyDuration >= DegradedThreshold:
		// Mark as Degraded.
		if spNode.Status.Phase != superplanev1.SuperplaneNodePhaseDegraded {
			logger.Info("Node unhealthy for >5 min, marking Degraded",
				"unhealthySince", unhealthySince,
				"duration", unhealthyDuration.Round(time.Second),
				"reason", reason,
			)
			spNode.Status.Phase = superplanev1.SuperplaneNodePhaseDegraded
			spNode.Status.Message = fmt.Sprintf("Node degraded: %s", reason)
		}

	default:
		// Unhealthy but within tolerance. Just update the condition/message.
		spNode.Status.Message = fmt.Sprintf("Node unhealthy (monitoring): %s", reason)
	}

	if err := r.Client.Status().Update(ctx, spNode); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status (unhealthy): %w", err)
	}

	return ctrl.Result{RequeueAfter: HealthCheckInterval}, nil
}

// triggerAutoRepair provisions a replacement SuperplaneNode and sets the unhealthy node to Draining.
func (r *HealthMonitorReconciler) triggerAutoRepair(
	ctx context.Context,
	spNode *superplanev1.SuperplaneNode,
	now time.Time,
	reason string,
) error {
	logger := log.FromContext(ctx)

	// Create a replacement SuperplaneNode with the same spec.
	replacement := &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			GenerateName: fmt.Sprintf("%s-repair-", spNode.Spec.NodePoolRef),
			Namespace:    spNode.Namespace,
			Labels: map[string]string{
				"superplane.ai/nodepool":    spNode.Spec.NodePoolRef,
				"superplane.ai/auto-repair": "true",
				"superplane.ai/replaced":    spNode.Name,
			},
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: spNode.Spec.NodePoolRef,
			Cloud:       spNode.Spec.Cloud,
			GPUType:     spNode.Spec.GPUType,
			GPUCount:    spNode.Spec.GPUCount,
			Region:      spNode.Spec.Region,
		},
	}

	if err := r.Client.Create(ctx, replacement); err != nil {
		return fmt.Errorf("create replacement node: %w", err)
	}

	logger.Info("Auto-repair: created replacement SuperplaneNode",
		"replacement", replacement.Name,
		"unhealthyNode", spNode.Name,
		"reason", reason,
	)

	// Mark the unhealthy node as Draining.
	spNode.Status.Phase = superplanev1.SuperplaneNodePhaseDraining
	spNode.Status.Message = fmt.Sprintf("Auto-repair: draining unhealthy node, replacement %s provisioning. Reason: %s",
		replacement.Name, reason)

	// Record the auto-repair action as a condition.
	setCondition(&spNode.Status.Conditions, metav1.Condition{
		Type:               ConditionTypeAutoRepair,
		Status:             metav1.ConditionTrue,
		ObservedGeneration: spNode.Generation,
		LastTransitionTime: metav1.NewTime(now),
		Reason:             "AutoRepairTriggered",
		Message:            fmt.Sprintf("Replacement node %s created, draining this node", replacement.Name),
	})

	return nil
}

// getUnhealthySince returns the time the node first became unhealthy.
// If there's an existing Healthy=False condition, use its LastTransitionTime.
// Otherwise, return now (first observation).
func getUnhealthySince(conditions []metav1.Condition, now time.Time) time.Time {
	for _, c := range conditions {
		if c.Type == ConditionTypeHealthy && c.Status == metav1.ConditionFalse {
			return c.LastTransitionTime.Time
		}
	}
	return now
}

// hasCondition checks if a condition with the given type and status exists.
func hasCondition(conditions []metav1.Condition, condType string, status metav1.ConditionStatus) bool {
	for _, c := range conditions {
		if c.Type == condType && c.Status == status {
			return true
		}
	}
	return false
}

// setCondition sets or updates a condition in the conditions slice.
// Returns true if the condition was changed.
func setCondition(conditions *[]metav1.Condition, condition metav1.Condition) bool {
	if conditions == nil {
		return false
	}
	for i, existing := range *conditions {
		if existing.Type == condition.Type {
			if existing.Status == condition.Status &&
				existing.Reason == condition.Reason &&
				existing.Message == condition.Message {
				return false
			}
			(*conditions)[i] = condition
			return true
		}
	}
	*conditions = append(*conditions, condition)
	return true
}
