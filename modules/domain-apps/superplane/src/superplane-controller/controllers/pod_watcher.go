// Package controllers implements the Superplane controller loops.
package controllers

import (
	"context"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

const (
	// GPUResourceName is the extended resource name for NVIDIA GPUs.
	GPUResourceName corev1.ResourceName = "nvidia.com/gpu"

	// DebounceDuration is the time to wait before provisioning to allow the
	// scheduler to catch up and to batch multiple pending pods.
	DebounceDuration = 10 * time.Second

	// annotationFirstSeen records when the pod was first observed as unschedulable.
	annotationFirstSeen = "superplane.ai/first-seen-unschedulable"

	// annotationProvisioningTriggered marks a pod for which provisioning has been triggered.
	annotationProvisioningTriggered = "superplane.ai/provisioning-triggered"

	// labelManagedBy is used on SuperplaneNode resources created by the pod watcher.
	labelManagedBy = "superplane.ai/managed-by"
)

// PodWatcherReconciler watches Pending pods requesting GPU resources and
// triggers provisioning of new nodes via SuperplaneNode CRs.
type PodWatcherReconciler struct {
	client.Client

	// Clock abstracts time for testing. If nil, time.Now is used.
	Clock func() time.Time
}

// Reconcile handles a single pod event.
// It checks whether the pod is unschedulable and requesting GPUs, matches it
// against NodePool policies, debounces, and creates a SuperplaneNode if needed.
func (r *PodWatcherReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx)

	// 1. Fetch the pod.
	var pod corev1.Pod
	if err := r.Get(ctx, req.NamespacedName, &pod); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// 2. Skip pods being deleted.
	if pod.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}

	// 3. Check if pod is pending and unschedulable.
	if !isPendingUnschedulable(&pod) {
		return ctrl.Result{}, nil
	}

	// 3b. Skip pods whose demand was deliberately retired.
	//
	// A deliberate release stops the owning workload's intent before deleting its
	// capacity, and marks the workload with the retirement annotation so its pods
	// inherit it. Without this check the pods evicted during drain go Pending and
	// unschedulable, and the pod watcher immediately provisions fresh GPU capacity
	// for them — recreating by the back door exactly what was just released.
	if reason := pod.Annotations[superplanev1.AnnotationRetirement]; reason != "" {
		logger.V(1).Info("Pod demand deliberately retired, not provisioning capacity",
			"pod", req.NamespacedName,
			"retirementReason", reason,
		)
		return ctrl.Result{}, nil
	}

	// 4. Extract GPU request.
	gpuQty := extractGPURequest(&pod)
	if gpuQty.IsZero() {
		// Not a GPU pod — nothing to do.
		return ctrl.Result{}, nil
	}
	gpuCount := int32(gpuQty.Value())

	logger.Info("Found unschedulable GPU pod",
		"pod", req.NamespacedName,
		"gpuCount", gpuCount,
	)

	// 5. Skip if provisioning was already triggered for this pod.
	if pod.Annotations != nil && pod.Annotations[annotationProvisioningTriggered] == "true" {
		logger.V(1).Info("Provisioning already triggered, skipping", "pod", req.NamespacedName)
		return ctrl.Result{}, nil
	}

	// 6. Check if an existing node can fit the pod.
	canFit, err := r.existingNodeCanFit(ctx, gpuCount)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("check existing nodes: %w", err)
	}
	if canFit {
		logger.V(1).Info("Existing node can fit pod, skipping provisioning", "pod", req.NamespacedName)
		return ctrl.Result{}, nil
	}

	// 7. Match against NodePool specs.
	pool, err := r.matchNodePool(ctx, gpuCount)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("match nodepool: %w", err)
	}
	if pool == nil {
		logger.Info("No matching NodePool for GPU pod", "pod", req.NamespacedName, "gpuCount", gpuCount)
		return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
	}

	// 8. Debounce: wait DebounceDuration since first seen.
	now := r.now()
	firstSeen, err := r.ensureFirstSeenAnnotation(ctx, &pod, now)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("set first-seen annotation: %w", err)
	}

	elapsed := now.Sub(firstSeen)
	if elapsed < DebounceDuration {
		remaining := DebounceDuration - elapsed
		logger.V(1).Info("Debouncing", "pod", req.NamespacedName, "remaining", remaining)
		return ctrl.Result{RequeueAfter: remaining}, nil
	}

	// 9. Check provisioning budget.
	if !r.hasProvisioningBudget(ctx, pool) {
		logger.Info("NodePool at max concurrent provisioning, requeuing",
			"nodePool", pool.Name)
		return ctrl.Result{RequeueAfter: 15 * time.Second}, nil
	}

	// 10. Create SuperplaneNode to trigger provisioning.
	logger.Info("Triggering provisioning",
		"pod", req.NamespacedName,
		"nodePool", pool.Name,
		"cloud", pool.Spec.Clouds[0],
		"gpuType", selectGPUType(pool),
		"gpuCount", gpuCount,
	)

	spNode, err := r.createSuperplaneNode(ctx, pool, gpuCount, &pod)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("create SuperplaneNode: %w", err)
	}

	logger.Info("Created SuperplaneNode", "name", spNode.Name, "nodePool", pool.Name)

	// 11. Mark pod as provisioning-triggered.
	if err := r.markProvisioningTriggered(ctx, &pod); err != nil {
		// Non-fatal: the node is already being created. Log and continue.
		logger.Error(err, "Failed to mark pod as provisioning-triggered", "pod", req.NamespacedName)
	}

	return ctrl.Result{}, nil
}

// SetupWithManager registers the reconciler with the controller manager.
func (r *PodWatcherReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&corev1.Pod{}).
		Named("pod-watcher").
		Complete(r)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// isPendingUnschedulable returns true if the pod is Pending with
// PodScheduled condition set to False.
func isPendingUnschedulable(pod *corev1.Pod) bool {
	if pod.Status.Phase != corev1.PodPending {
		return false
	}
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodScheduled && c.Status == corev1.ConditionFalse {
			return true
		}
	}
	return false
}

// extractGPURequest returns the nvidia.com/gpu resource request from the pod.
// It sums across all containers and init containers.
func extractGPURequest(pod *corev1.Pod) resource.Quantity {
	total := resource.Quantity{}
	for i := range pod.Spec.InitContainers {
		if qty, ok := pod.Spec.InitContainers[i].Resources.Requests[GPUResourceName]; ok {
			total.Add(qty)
		}
	}
	for i := range pod.Spec.Containers {
		if qty, ok := pod.Spec.Containers[i].Resources.Requests[GPUResourceName]; ok {
			total.Add(qty)
		}
	}
	return total
}

// existingNodeCanFit checks whether any Ready SuperplaneNode has capacity
// for the requested GPU count. This is a simplified check — a real
// implementation would query allocatable resources on Kubernetes nodes.
func (r *PodWatcherReconciler) existingNodeCanFit(ctx context.Context, gpuCount int32) (bool, error) {
	var nodes superplanev1.SuperplaneNodeList
	if err := r.List(ctx, &nodes); err != nil {
		return false, err
	}

	for i := range nodes.Items {
		node := &nodes.Items[i]

		// Retiring capacity is on its way out and must not be counted as
		// available. Counting it would hide a genuine shortage: this function
		// returning true suppresses provisioning entirely, so a retiring node
		// could leave a GPU pod pending indefinitely.
		if node.IsDeliberatelyRetiring() {
			continue
		}

		if node.Status.Phase == superplanev1.SuperplaneNodePhaseReady {
			// Check if the node has enough GPUs. For simplicity, we compare
			// against the node's total GPU count. A production implementation
			// would check allocatable - allocated.
			if node.Spec.GPUCount >= gpuCount {
				// Check that there are no pods currently scheduled on this
				// node by looking at LastPodScheduledAt. If a pod was
				// recently scheduled, the node might still have capacity.
				// For now, we assume any Ready node with enough GPUs can fit.
				return true, nil
			}
		}
	}
	return false, nil
}

// matchNodePool returns the best matching NodePool for a GPU request.
// A NodePool matches if:
//   - It is in Active phase
//   - It has at least one GPU type configured
//   - It has not reached maxNodes
//   - Its budget allows more nodes (if maxCostPerHour is set)
func (r *PodWatcherReconciler) matchNodePool(ctx context.Context, gpuCount int32) (*superplanev1.NodePool, error) {
	var pools superplanev1.NodePoolList
	if err := r.List(ctx, &pools); err != nil {
		return nil, err
	}

	var best *superplanev1.NodePool
	for i := range pools.Items {
		pool := &pools.Items[i]

		// Must be active.
		if pool.Status.Phase != superplanev1.NodePoolPhaseActive {
			continue
		}

		// Must have GPU types configured.
		if len(pool.Spec.GPUTypes) == 0 {
			continue
		}

		// Must have clouds configured.
		if len(pool.Spec.Clouds) == 0 {
			continue
		}

		// Must not have reached max nodes.
		currentNodes := pool.Status.ReadyNodes + pool.Status.ProvisioningNodes
		if currentNodes >= pool.Spec.MaxNodes {
			continue
		}

		// Budget check: if maxCostPerHour is set, verify we haven't exceeded it.
		// This is a simple check — we assume each new node costs roughly
		// currentCostPerHour / currentNodes (or a default if no nodes exist).
		if pool.Spec.MaxCostPerHour != nil && pool.Status.CurrentCostPerHour >= *pool.Spec.MaxCostPerHour {
			continue
		}

		// Pick the first matching pool. A more sophisticated implementation
		// would rank by cost, availability, etc.
		if best == nil {
			best = pool
		}
	}

	return best, nil
}

// selectGPUType picks the first GPU type from the NodePool spec.
// A production implementation would consider availability and pricing.
func selectGPUType(pool *superplanev1.NodePool) string {
	if len(pool.Spec.GPUTypes) > 0 {
		return pool.Spec.GPUTypes[0]
	}
	return ""
}

// hasProvisioningBudget checks if the NodePool allows more concurrent provisioning.
func (r *PodWatcherReconciler) hasProvisioningBudget(ctx context.Context, pool *superplanev1.NodePool) bool {
	maxConcurrent := int32(3) // default
	if pool.Spec.MaxConcurrentProvisioning != nil {
		maxConcurrent = *pool.Spec.MaxConcurrentProvisioning
	}
	return pool.Status.ProvisioningNodes < maxConcurrent
}

// createSuperplaneNode creates a SuperplaneNode CR to trigger provisioning.
func (r *PodWatcherReconciler) createSuperplaneNode(
	ctx context.Context,
	pool *superplanev1.NodePool,
	gpuCount int32,
	pod *corev1.Pod,
) (*superplanev1.SuperplaneNode, error) {
	gpuType := selectGPUType(pool)
	cloud := pool.Spec.Clouds[0]

	spNode := &superplanev1.SuperplaneNode{
		ObjectMeta: metav1.ObjectMeta{
			GenerateName: fmt.Sprintf("sp-%s-", pool.Name),
			Namespace:    pod.Namespace,
			Labels: map[string]string{
				labelManagedBy:           "pod-watcher",
				"superplane.ai/nodepool": pool.Name,
				"superplane.ai/gpu-type": gpuType,
			},
			Annotations: map[string]string{
				"superplane.ai/triggered-by-pod": fmt.Sprintf("%s/%s", pod.Namespace, pod.Name),
			},
		},
		Spec: superplanev1.SuperplaneNodeSpec{
			NodePoolRef: pool.Name,
			Cloud:       cloud,
			GPUType:     gpuType,
			GPUCount:    gpuCount,
		},
	}

	if err := r.Create(ctx, spNode); err != nil {
		return nil, err
	}
	return spNode, nil
}

// ensureFirstSeenAnnotation records when the pod was first observed as
// unschedulable. Returns the parsed first-seen time.
func (r *PodWatcherReconciler) ensureFirstSeenAnnotation(
	ctx context.Context,
	pod *corev1.Pod,
	now time.Time,
) (time.Time, error) {
	if pod.Annotations != nil {
		if ts, ok := pod.Annotations[annotationFirstSeen]; ok {
			parsed, err := time.Parse(time.RFC3339, ts)
			if err == nil {
				return parsed, nil
			}
			// If we can't parse, treat as newly seen.
		}
	}

	// Set the annotation.
	patch := client.MergeFrom(pod.DeepCopy())
	if pod.Annotations == nil {
		pod.Annotations = make(map[string]string)
	}
	pod.Annotations[annotationFirstSeen] = now.Format(time.RFC3339)
	if err := r.Patch(ctx, pod, patch); err != nil {
		return time.Time{}, err
	}
	return now, nil
}

// markProvisioningTriggered annotates the pod so we don't trigger again.
func (r *PodWatcherReconciler) markProvisioningTriggered(ctx context.Context, pod *corev1.Pod) error {
	patch := client.MergeFrom(pod.DeepCopy())
	if pod.Annotations == nil {
		pod.Annotations = make(map[string]string)
	}
	pod.Annotations[annotationProvisioningTriggered] = "true"
	return r.Patch(ctx, pod, patch)
}

// now returns the current time, using r.Clock if set (for testing).
func (r *PodWatcherReconciler) now() time.Time {
	if r.Clock != nil {
		return r.Clock()
	}
	return time.Now()
}

// PodFilter returns a predicate-compatible check for whether a pod object
// is relevant to the PodWatcher. Exported for use in setup.
func PodFilter(obj client.Object) bool {
	pod, ok := obj.(*corev1.Pod)
	if !ok {
		return false
	}
	gpuReq := extractGPURequest(pod)
	return isPendingUnschedulable(pod) && !gpuReq.IsZero()
}

// NamespacedName is a convenience helper to construct types.NamespacedName.
func NamespacedName(namespace, name string) types.NamespacedName {
	return types.NamespacedName{Namespace: namespace, Name: name}
}
