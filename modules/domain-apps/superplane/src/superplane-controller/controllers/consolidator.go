// Package controllers implements the control loops for the Superplane platform.
package controllers

import (
	"context"
	"fmt"
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	policyv1 "k8s.io/api/policy/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

const (
	// DefaultConsolidationInterval is how often the consolidator runs.
	DefaultConsolidationInterval = 60 * time.Second

	// DefaultTTLSecondsAfterEmpty is the default TTL before an empty node is removed.
	DefaultTTLSecondsAfterEmpty int64 = 300

	// DefaultMaxUnavailable is the default maximum number of nodes that can be
	// unavailable during disruption.
	DefaultMaxUnavailable int32 = 1

	// drainTimeout is the maximum time to wait for pod eviction during drain.
	drainTimeout = 5 * time.Minute

	// defaultEvictionGracePeriod is the grace period for pod eviction in seconds.
	defaultEvictionGracePeriod = 30
)

// SkyPilotClient is the interface for the SkyPilot API operations needed by the Consolidator.
type SkyPilotClient interface {
	Down(ctx context.Context, clusterNames []string, purge bool) (string, error)
}

// ConsolidatorConfig holds configuration for the Consolidator.
type ConsolidatorConfig struct {
	// Interval is how often the consolidator reconciles. Defaults to 60s.
	Interval time.Duration

	// Namespace is the namespace to watch for SuperplaneNode CRs.
	// Empty string means all namespaces.
	Namespace string
}

// Consolidator removes idle nodes after their TTL expires.
// It runs as a periodic loop, checking SuperplaneNode CRs with phase=Ready
// and removing those that have been empty for longer than ttlSecondsAfterEmpty.
type Consolidator struct {
	client    client.Client
	skypilot  SkyPilotClient
	config    ConsolidatorConfig
	mu        sync.Mutex
	running   bool
	cancelFn  context.CancelFunc
}

// NewConsolidator creates a new Consolidator.
func NewConsolidator(c client.Client, sky SkyPilotClient, cfg ConsolidatorConfig) *Consolidator {
	if cfg.Interval == 0 {
		cfg.Interval = DefaultConsolidationInterval
	}
	return &Consolidator{
		client:   c,
		skypilot: sky,
		config:   cfg,
	}
}

// Start begins the consolidation loop. It blocks until the context is cancelled.
func (c *Consolidator) Start(ctx context.Context) error {
	c.mu.Lock()
	if c.running {
		c.mu.Unlock()
		return fmt.Errorf("consolidator is already running")
	}
	ctx, cancel := context.WithCancel(ctx)
	c.cancelFn = cancel
	c.running = true
	c.mu.Unlock()

	defer func() {
		c.mu.Lock()
		c.running = false
		c.cancelFn = nil
		c.mu.Unlock()
	}()

	logger := log.FromContext(ctx).WithName("consolidator")
	logger.Info("starting consolidator", "interval", c.config.Interval)

	ticker := time.NewTicker(c.config.Interval)
	defer ticker.Stop()

	// Run immediately on start, then on interval.
	if err := c.Reconcile(ctx); err != nil {
		logger.Error(err, "reconciliation failed")
	}

	for {
		select {
		case <-ctx.Done():
			logger.Info("stopping consolidator")
			return nil
		case <-ticker.C:
			if err := c.Reconcile(ctx); err != nil {
				logger.Error(err, "reconciliation failed")
			}
		}
	}
}

// Stop stops the consolidation loop.
func (c *Consolidator) Stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cancelFn != nil {
		c.cancelFn()
	}
}

// Reconcile performs a single consolidation pass.
func (c *Consolidator) Reconcile(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("consolidator")

	// 1. List all SuperplaneNode CRs with phase=Ready.
	readyNodes, err := c.listReadyNodes(ctx)
	if err != nil {
		return fmt.Errorf("list ready nodes: %w", err)
	}

	if len(readyNodes) == 0 {
		logger.V(1).Info("no ready nodes found")
		return nil
	}

	// 2. Load NodePool configs for TTL and disruption budgets.
	nodePoolCache := make(map[string]*superplanev1.NodePool)

	// 3. Determine which nodes are candidates for removal.
	var candidates []superplanev1.SuperplaneNode
	now := time.Now()

	for i := range readyNodes {
		node := &readyNodes[i]

		// Lookup the NodePool for this node.
		pool, err := c.getNodePool(ctx, node.Spec.NodePoolRef, nodePoolCache)
		if err != nil {
			logger.Error(err, "failed to get nodepool", "nodePoolRef", node.Spec.NodePoolRef, "node", node.Name)
			continue
		}

		// Check consolidation policy - skip if consolidation is explicitly disabled.
		if pool.Spec.Consolidation != nil && !pool.Spec.Consolidation.Enabled {
			continue
		}

		// Determine TTL.
		ttl := DefaultTTLSecondsAfterEmpty
		if pool.Spec.TTLSecondsAfterEmpty != nil {
			ttl = *pool.Spec.TTLSecondsAfterEmpty
		}

		// Check if node is empty (no non-DaemonSet pods).
		empty, err := c.isNodeEmpty(ctx, node.Status.K8sNodeName)
		if err != nil {
			logger.Error(err, "failed to check if node is empty", "node", node.Name, "k8sNode", node.Status.K8sNodeName)
			continue
		}

		if !empty {
			// Node has workload pods; update LastPodScheduledAt if not set.
			continue
		}

		// Node is empty. Check TTL.
		emptyTime := c.getEmptyTimestamp(node)
		if emptyTime.IsZero() {
			// First time we see this node empty — record it by updating LastPodScheduledAt.
			// We use the current time as the "last time pods were scheduled".
			// The TTL starts from LastPodScheduledAt.
			logger.V(1).Info("node is newly empty, recording timestamp", "node", node.Name)
			continue
		}

		elapsed := now.Sub(emptyTime.Time)
		if elapsed.Seconds() >= float64(ttl) {
			logger.Info("node exceeded TTL, marking for removal",
				"node", node.Name,
				"k8sNode", node.Status.K8sNodeName,
				"emptyFor", elapsed,
				"ttl", time.Duration(ttl)*time.Second,
			)
			candidates = append(candidates, *node)
		}
	}

	if len(candidates) == 0 {
		logger.V(1).Info("no nodes eligible for removal")
		return nil
	}

	// 4. Enforce disruption budget per NodePool.
	removed := 0
	poolRemovalCount := make(map[string]int)

	for i := range candidates {
		node := &candidates[i]
		poolRef := node.Spec.NodePoolRef

		pool, err := c.getNodePool(ctx, poolRef, nodePoolCache)
		if err != nil {
			logger.Error(err, "failed to get nodepool for disruption check", "nodePoolRef", poolRef)
			continue
		}

		maxUnavailable := DefaultMaxUnavailable
		if pool.Spec.Disruption != nil {
			maxUnavailable = pool.Spec.Disruption.MaxUnavailable
		}

		// Count currently unavailable (draining/terminated) nodes for this pool.
		currentUnavailable, err := c.countUnavailableNodes(ctx, poolRef)
		if err != nil {
			logger.Error(err, "failed to count unavailable nodes", "nodePoolRef", poolRef)
			continue
		}

		totalUnavailable := currentUnavailable + int32(poolRemovalCount[poolRef])
		if totalUnavailable >= maxUnavailable {
			logger.Info("disruption budget exhausted, skipping node",
				"node", node.Name,
				"nodePoolRef", poolRef,
				"currentUnavailable", totalUnavailable,
				"maxUnavailable", maxUnavailable,
			)
			continue
		}

		// 5. Remove the node.
		if err := c.removeNode(ctx, node); err != nil {
			logger.Error(err, "failed to remove node", "node", node.Name)
			continue
		}

		poolRemovalCount[poolRef]++
		removed++
		logger.Info("successfully removed idle node", "node", node.Name)
	}

	logger.Info("consolidation pass complete", "readyNodes", len(readyNodes), "removed", removed)
	return nil
}

// listReadyNodes returns all SuperplaneNode CRs with phase=Ready.
func (c *Consolidator) listReadyNodes(ctx context.Context) ([]superplanev1.SuperplaneNode, error) {
	var nodeList superplanev1.SuperplaneNodeList
	opts := []client.ListOption{}
	if c.config.Namespace != "" {
		opts = append(opts, client.InNamespace(c.config.Namespace))
	}

	if err := c.client.List(ctx, &nodeList, opts...); err != nil {
		return nil, err
	}

	var ready []superplanev1.SuperplaneNode
	for _, node := range nodeList.Items {
		if node.Status.Phase == superplanev1.SuperplaneNodePhaseReady {
			ready = append(ready, node)
		}
	}
	return ready, nil
}

// getNodePool retrieves a NodePool by name, using a cache.
func (c *Consolidator) getNodePool(ctx context.Context, name string, cache map[string]*superplanev1.NodePool) (*superplanev1.NodePool, error) {
	if pool, ok := cache[name]; ok {
		return pool, nil
	}

	var pool superplanev1.NodePool
	// NodePool is cluster-scoped, so no namespace needed.
	if err := c.client.Get(ctx, types.NamespacedName{Name: name}, &pool); err != nil {
		return nil, fmt.Errorf("get nodepool %q: %w", name, err)
	}
	cache[name] = &pool
	return &pool, nil
}

// isNodeEmpty checks if a K8s node has no non-DaemonSet pods running.
// Returns true if the node has zero non-DaemonSet pods (or if the node name is empty).
func (c *Consolidator) isNodeEmpty(ctx context.Context, k8sNodeName string) (bool, error) {
	if k8sNodeName == "" {
		return true, nil
	}

	var podList corev1.PodList
	if err := c.client.List(ctx, &podList, &client.ListOptions{
		FieldSelector: fields.OneTermEqualSelector("spec.nodeName", k8sNodeName),
	}); err != nil {
		return false, fmt.Errorf("list pods on node %q: %w", k8sNodeName, err)
	}

	for _, pod := range podList.Items {
		if isDaemonSetPod(&pod) {
			continue
		}
		// Skip completed/failed pods.
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		// Found an active non-DaemonSet pod.
		return false, nil
	}

	return true, nil
}

// isDaemonSetPod checks if a pod is owned by a DaemonSet.
func isDaemonSetPod(pod *corev1.Pod) bool {
	for _, ref := range pod.OwnerReferences {
		if ref.Kind == "DaemonSet" {
			return true
		}
	}
	return false
}

// getEmptyTimestamp returns the time since the node has been empty.
// Uses LastPodScheduledAt as the reference. If nil, the node is newly empty.
func (c *Consolidator) getEmptyTimestamp(node *superplanev1.SuperplaneNode) metav1.Time {
	if node.Status.LastPodScheduledAt != nil {
		return *node.Status.LastPodScheduledAt
	}
	return metav1.Time{}
}

// countUnavailableNodes counts nodes in Draining or Terminated phase for a given NodePool.
func (c *Consolidator) countUnavailableNodes(ctx context.Context, nodePoolRef string) (int32, error) {
	var nodeList superplanev1.SuperplaneNodeList
	opts := []client.ListOption{}
	if c.config.Namespace != "" {
		opts = append(opts, client.InNamespace(c.config.Namespace))
	}

	if err := c.client.List(ctx, &nodeList, opts...); err != nil {
		return 0, err
	}

	var count int32
	for _, node := range nodeList.Items {
		if node.Spec.NodePoolRef != nodePoolRef {
			continue
		}
		if node.Status.Phase == superplanev1.SuperplaneNodePhaseDraining ||
			node.Status.Phase == superplanev1.SuperplaneNodePhaseTerminated {
			count++
		}
	}
	return count, nil
}

// removeNode performs the full node removal sequence:
// 1. Update SuperplaneNode phase to Draining
// 2. Cordon the K8s node
// 3. Drain pods (respect PDBs)
// 4. Run sky down
// 5. Delete the K8s node object
// 6. Update SuperplaneNode phase to Terminated
func (c *Consolidator) removeNode(ctx context.Context, spNode *superplanev1.SuperplaneNode) error {
	logger := log.FromContext(ctx).WithName("consolidator").
		WithValues("node", spNode.Name, "k8sNode", spNode.Status.K8sNodeName)

	// Step 1: Update phase to Draining.
	if err := c.updateNodePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseDraining, "Node marked for removal by consolidator"); err != nil {
		return fmt.Errorf("update phase to Draining: %w", err)
	}
	logger.Info("phase updated to Draining")

	// Step 2: Cordon the K8s node.
	if spNode.Status.K8sNodeName != "" {
		if err := c.cordonNode(ctx, spNode.Status.K8sNodeName); err != nil {
			return fmt.Errorf("cordon node: %w", err)
		}
		logger.Info("node cordoned")

		// Step 3: Drain pods (respect PDBs).
		if err := c.drainNode(ctx, spNode.Status.K8sNodeName); err != nil {
			return fmt.Errorf("drain node: %w", err)
		}
		logger.Info("node drained")
	}

	// Step 4: Sky down.
	if spNode.Status.SkypilotCluster != "" {
		requestID, err := c.skypilot.Down(ctx, []string{spNode.Status.SkypilotCluster}, false)
		if err != nil {
			// If sky down fails, try with purge.
			logger.Error(err, "sky down failed, retrying with purge")
			if _, err := c.skypilot.Down(ctx, []string{spNode.Status.SkypilotCluster}, true); err != nil {
				return fmt.Errorf("sky down (purge): %w", err)
			}
		} else {
			logger.Info("sky down initiated", "requestID", requestID)
		}
	}

	// Step 5: Delete the K8s node object.
	if spNode.Status.K8sNodeName != "" {
		if err := c.deleteK8sNode(ctx, spNode.Status.K8sNodeName); err != nil {
			logger.Error(err, "failed to delete K8s node, continuing anyway")
		} else {
			logger.Info("K8s node deleted")
		}
	}

	// Step 6: Update phase to Terminated.
	if err := c.updateNodePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseTerminated, "Node removed by consolidator"); err != nil {
		return fmt.Errorf("update phase to Terminated: %w", err)
	}
	logger.Info("phase updated to Terminated")

	return nil
}

// updateNodePhase updates the SuperplaneNode status phase and message.
func (c *Consolidator) updateNodePhase(ctx context.Context, spNode *superplanev1.SuperplaneNode, phase superplanev1.SuperplaneNodePhase, message string) error {
	// Re-fetch to avoid conflicts.
	var current superplanev1.SuperplaneNode
	key := types.NamespacedName{
		Name:      spNode.Name,
		Namespace: spNode.Namespace,
	}
	if err := c.client.Get(ctx, key, &current); err != nil {
		return fmt.Errorf("get node %q: %w", spNode.Name, err)
	}

	current.Status.Phase = phase
	current.Status.Message = message
	if err := c.client.Status().Update(ctx, &current); err != nil {
		return fmt.Errorf("update status for %q: %w", spNode.Name, err)
	}

	// Update the caller's copy.
	spNode.Status.Phase = phase
	spNode.Status.Message = message
	return nil
}

// cordonNode marks a K8s node as unschedulable.
func (c *Consolidator) cordonNode(ctx context.Context, nodeName string) error {
	var node corev1.Node
	if err := c.client.Get(ctx, types.NamespacedName{Name: nodeName}, &node); err != nil {
		return fmt.Errorf("get node %q: %w", nodeName, err)
	}

	if node.Spec.Unschedulable {
		return nil // Already cordoned.
	}

	node.Spec.Unschedulable = true
	if err := c.client.Update(ctx, &node); err != nil {
		return fmt.Errorf("cordon node %q: %w", nodeName, err)
	}
	return nil
}

// drainNode evicts all non-DaemonSet pods from a K8s node, respecting PDBs.
func (c *Consolidator) drainNode(ctx context.Context, nodeName string) error {
	logger := log.FromContext(ctx).WithName("consolidator").WithValues("drain", nodeName)

	var podList corev1.PodList
	if err := c.client.List(ctx, &podList, &client.ListOptions{
		FieldSelector: fields.OneTermEqualSelector("spec.nodeName", nodeName),
	}); err != nil {
		return fmt.Errorf("list pods on node %q: %w", nodeName, err)
	}

	// Collect pods to evict.
	var toEvict []corev1.Pod
	for _, pod := range podList.Items {
		if isDaemonSetPod(&pod) {
			continue
		}
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		toEvict = append(toEvict, pod)
	}

	if len(toEvict) == 0 {
		return nil
	}

	logger.Info("evicting pods", "count", len(toEvict))

	// Evict each pod. Eviction API respects PDBs.
	for _, pod := range toEvict {
		gracePeriod := int64(defaultEvictionGracePeriod)
		eviction := &policyv1.Eviction{
			ObjectMeta: metav1.ObjectMeta{
				Name:      pod.Name,
				Namespace: pod.Namespace,
			},
			DeleteOptions: &metav1.DeleteOptions{
				GracePeriodSeconds: &gracePeriod,
			},
		}
		if err := c.client.SubResource("eviction").Create(ctx, &pod, eviction); err != nil {
			logger.Error(err, "failed to evict pod", "pod", pod.Name, "namespace", pod.Namespace)
			// Continue evicting other pods even if one fails.
		}
	}

	// Wait for pods to be evicted.
	return c.waitForDrain(ctx, nodeName, drainTimeout)
}

// waitForDrain waits until all non-DaemonSet pods are gone from the node.
func (c *Consolidator) waitForDrain(ctx context.Context, nodeName string, timeout time.Duration) error {
	deadline := time.After(timeout)
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline:
			return fmt.Errorf("drain timeout exceeded for node %q", nodeName)
		case <-ticker.C:
			empty, err := c.isNodeEmpty(ctx, nodeName)
			if err != nil {
				return fmt.Errorf("check drain progress: %w", err)
			}
			if empty {
				return nil
			}
		}
	}
}

// deleteK8sNode deletes the K8s Node object.
func (c *Consolidator) deleteK8sNode(ctx context.Context, nodeName string) error {
	node := &corev1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name: nodeName,
		},
	}
	if err := c.client.Delete(ctx, node); err != nil {
		return fmt.Errorf("delete node %q: %w", nodeName, err)
	}
	return nil
}
