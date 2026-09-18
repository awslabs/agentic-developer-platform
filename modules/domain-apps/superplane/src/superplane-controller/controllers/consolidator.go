// Package controllers implements the control loops for the Superplane platform.
package controllers

import (
	"context"
	"errors"
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
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/skypilot"
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

	// releaseVerifyTimeout bounds how long we wait for the provider to converge
	// on "cluster gone" after a teardown request.
	//
	// Down() is asynchronous: it returns a request ID and the provider tears the
	// cluster down afterwards, so for a short window /status legitimately still
	// reports the cluster as UP or INIT. A single immediate probe would therefore
	// read a perfectly normal release as a failure. We poll until the provider
	// converges, and only a still-present cluster at the deadline (or a query
	// error) counts as not released.
	releaseVerifyTimeout = 10 * time.Minute

	// releaseVerifyInterval is the gap between provider re-checks while waiting
	// for a teardown to converge.
	releaseVerifyInterval = 10 * time.Second
)

// SkyPilotClient is the interface for the SkyPilot API operations needed by the Consolidator.
type SkyPilotClient interface {
	Down(ctx context.Context, clusterNames []string, purge bool) (string, error)

	// Status returns the provider's view of the named clusters. A cluster that
	// is absent from the response no longer exists at the provider. This is the
	// provider truth used to confirm a release actually happened; an internal
	// status column is not evidence of release.
	Status(ctx context.Context, clusterNames ...string) ([]skypilot.ClusterInfo, error)
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

	// ReleaseVerifyTimeout and ReleaseVerifyInterval bound the wait for an
	// asynchronous teardown to be confirmed at the provider. Zero means use the
	// package defaults; tests shorten them to keep runs fast.
	ReleaseVerifyTimeout  time.Duration
	ReleaseVerifyInterval time.Duration
}

// releaseVerifyTiming returns the polling window for release verification,
// falling back to the package defaults when unset.
func (c *Consolidator) releaseVerifyTiming() (time.Duration, time.Duration) {
	timeout, interval := c.ReleaseVerifyTimeout, c.ReleaseVerifyInterval
	if timeout <= 0 {
		timeout = releaseVerifyTimeout
	}
	if interval <= 0 {
		interval = releaseVerifyInterval
	}
	return timeout, interval
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
			// Node is carrying workload pods, so it is in use right now. Stamp
			// status.lastPodScheduledAt with the current time: it is the "node
			// was last busy" mark that the TTL below measures from once the node
			// goes empty.
			//
			// Nothing used to write this field, which made the TTL removal branch
			// unreachable for every node the controller provisions — idle GPUs
			// were only ever reclaimed by SkyPilot's 120-minute autostop default.
			if err := c.markNodeBusy(ctx, node, now); err != nil {
				logger.Error(err, "failed to record last-busy timestamp", "node", node.Name)
			}
			continue
		}

		// Node is empty. Check TTL.
		emptyTime := c.getEmptyTimestamp(node)
		if emptyTime.IsZero() {
			// The node has never carried a workload pod (so no last-busy stamp
			// exists). Start the TTL clock now by stamping it, otherwise a node
			// that never receives work stays unstamped forever and is never
			// reclaimed.
			logger.V(1).Info("node is newly empty, recording timestamp", "node", node.Name)
			if err := c.markNodeBusy(ctx, node, now); err != nil {
				logger.Error(err, "failed to record last-busy timestamp", "node", node.Name)
			}
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
	var releaseFailures []error

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
			// Count the attempt against the disruption budget: the node was
			// cordoned and possibly drained, so it is not serving normally and
			// must not be treated as spare headroom for further removals.
			poolRemovalCount[poolRef]++
			releaseFailures = append(releaseFailures, fmt.Errorf("node %s: %w", node.Name, err))
			logger.Error(err, "failed to remove node", "node", node.Name)
			continue
		}

		poolRemovalCount[poolRef]++
		removed++
		logger.Info("successfully removed idle node", "node", node.Name)
	}

	logger.Info("consolidation pass complete",
		"readyNodes", len(readyNodes),
		"removed", removed,
		"releaseFailures", len(releaseFailures),
	)

	// A failed or unconfirmed teardown is reported as a failure, not swallowed.
	// Callers (Start's loop, and tests) see a non-nil error, which is this
	// controller's equivalent of the teardown script's non-zero exit.
	if len(releaseFailures) > 0 {
		return fmt.Errorf("%d node release(s) failed or unconfirmed: %w",
			len(releaseFailures), errors.Join(releaseFailures...))
	}

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

// markNodeBusy stamps status.lastPodScheduledAt with the given time, recording
// that the node was carrying workload (or is starting its idle clock).
//
// This is the write that was missing from the codebase: getEmptyTimestamp reads
// this field to decide whether an empty node has outlived its TTL, so without a
// writer the TTL comparison was never reached.
func (c *Consolidator) markNodeBusy(ctx context.Context, spNode *superplanev1.SuperplaneNode, now time.Time) error {
	// Re-fetch to avoid clobbering a concurrent status update.
	var current superplanev1.SuperplaneNode
	key := types.NamespacedName{Name: spNode.Name, Namespace: spNode.Namespace}
	if err := c.client.Get(ctx, key, &current); err != nil {
		return fmt.Errorf("get node %q: %w", spNode.Name, err)
	}

	stamp := metav1.NewTime(now)
	current.Status.LastPodScheduledAt = &stamp
	if err := c.client.Status().Update(ctx, &current); err != nil {
		return fmt.Errorf("update lastPodScheduledAt for %q: %w", spNode.Name, err)
	}

	// Keep the caller's copy consistent.
	spNode.Status.LastPodScheduledAt = &stamp
	return nil
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
		switch node.Status.Phase {
		case superplanev1.SuperplaneNodePhaseDraining,
			superplanev1.SuperplaneNodePhaseTerminated,
			// Retiring capacity is already leaving, and a ReleaseFailed node has
			// been cordoned/drained with its release unresolved. Both are
			// unavailable, so counting them keeps the disruption budget honest —
			// otherwise the consolidator could drain further nodes on top of an
			// in-progress retirement and breach maxUnavailable.
			superplanev1.SuperplaneNodePhaseRetiring,
			superplanev1.SuperplaneNodePhaseReleaseFailed:
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

	// Step 4: Sky down, then confirm the release against the provider.
	if spNode.Status.SkypilotCluster != "" {
		cluster := spNode.Status.SkypilotCluster

		requestID, err := c.skypilot.Down(ctx, []string{cluster}, false)
		if err != nil {
			// If sky down fails, try with purge.
			logger.Error(err, "sky down failed, retrying with purge")
			if _, err := c.skypilot.Down(ctx, []string{cluster}, true); err != nil {
				return c.failRelease(ctx, spNode, cluster,
					fmt.Errorf("sky down (purge): %w", err))
			}
		} else {
			logger.Info("sky down initiated", "requestID", requestID)
		}

		// Provider truth determines cleanup, not a success status column. If the
		// cluster is still present — or if we cannot tell — the resource may
		// still exist and still be billing, so the release is not complete.
		if err := c.verifyClusterReleased(ctx, cluster); err != nil {
			return c.failRelease(ctx, spNode, cluster, err)
		}
		logger.Info("release confirmed against provider", "cluster", cluster)
	}

	// Step 5: Delete the K8s node object.
	// A node object left behind is an unresolved handle, not a cosmetic problem:
	// the scheduler may keep placing pods against it. Report it as a failure
	// rather than marking the removal successful.
	if spNode.Status.K8sNodeName != "" {
		if err := c.deleteK8sNode(ctx, spNode.Status.K8sNodeName); err != nil {
			return c.failRelease(ctx, spNode, spNode.Status.SkypilotCluster,
				fmt.Errorf("delete K8s node %q: %w", spNode.Status.K8sNodeName, err))
		}
		logger.Info("K8s node deleted")
	}

	// Step 6: Update phase to Terminated — only now, with release confirmed.
	if err := c.updateNodePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseTerminated, "Node removed by consolidator; release confirmed against provider"); err != nil {
		return fmt.Errorf("update phase to Terminated: %w", err)
	}
	logger.Info("phase updated to Terminated")

	return nil
}

// verifyClusterReleased re-checks the provider for the named cluster and returns
// an error unless the provider confirms it is gone.
//
// Two distinct failures are reported, and neither counts as released:
//   - the query itself failed, so the outcome is UNKNOWN. An unknown outcome is
//     not a negative one: we must not conclude the resource is gone because we
//     could not reach the provider (this is also what "cleanup is not claimed
//     after credential loss" means in practice — a rejected call reads as an
//     error here, never as success).
//   - the cluster is still present, so the resource demonstrably still exists.
//
// A STOPPED cluster is NOT released: it retains its disks and keeps incurring
// storage cost, so treating it as terminated would clear the accounting early.
// Because Down() is asynchronous, the check is retried until the provider
// converges or releaseVerifyTimeout elapses: a cluster still reported moments
// after the teardown request is normal in-progress teardown, not a failure.
// Only the state at the deadline is a verdict.
func (c *Consolidator) verifyClusterReleased(ctx context.Context, cluster string) error {
	timeout, interval := c.releaseVerifyTiming()
	deadline := time.Now().Add(timeout)

	for {
		err := c.checkClusterReleased(ctx, cluster)
		if err == nil {
			return nil
		}

		// Out of time: the last observation is the verdict.
		if !time.Now().Before(deadline) {
			return err
		}

		// Wait before re-checking, but never outlive the caller's context.
		select {
		case <-ctx.Done():
			return fmt.Errorf("verify release of cluster %q: %w (last observation: %v)",
				cluster, ctx.Err(), err)
		case <-time.After(interval):
		}
	}
}

// checkClusterReleased performs a single provider probe. A nil error means the
// provider confirms the cluster is gone.
func (c *Consolidator) checkClusterReleased(ctx context.Context, cluster string) error {
	infos, err := c.skypilot.Status(ctx, cluster)
	if err != nil {
		return fmt.Errorf("verify release of cluster %q: provider status unknown: %w", cluster, err)
	}

	for i := range infos {
		if infos[i].Name != cluster {
			continue
		}
		return fmt.Errorf(
			"verify release of cluster %q: provider still reports the cluster (status %q); resources may still be billing",
			cluster, infos[i].Status)
	}

	return nil
}

// failRelease records an unconfirmed or failed teardown and returns an error so
// the caller reports it as a failure.
//
// The node is moved to ReleaseFailed rather than Terminated, and
// status.skypilotCluster is left intact: that handle is the only way a later
// reconciliation can find and finish releasing the resource. Erasing it would
// strand a live, billing GPU cluster with nothing pointing at it.
func (c *Consolidator) failRelease(
	ctx context.Context,
	spNode *superplanev1.SuperplaneNode,
	cluster string,
	cause error,
) error {
	logger := log.FromContext(ctx).WithName("consolidator")

	message := fmt.Sprintf(
		"Release NOT confirmed: %v. Provider resources may still exist for cluster %q and are retained for reconciliation; this node is not Terminated.",
		cause, cluster)

	if err := c.updateNodePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseReleaseFailed, message); err != nil {
		// Report both problems: the release failure is the important one, but a
		// failed status write means the operator cannot see it on the object.
		logger.Error(err, "failed to record ReleaseFailed phase", "node", spNode.Name)
		return fmt.Errorf("%w (and recording ReleaseFailed failed: %v)", cause, err)
	}

	logger.Error(cause, "node release failed or unconfirmed; retaining provider handle",
		"node", spNode.Name,
		"cluster", cluster,
	)
	return cause
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
