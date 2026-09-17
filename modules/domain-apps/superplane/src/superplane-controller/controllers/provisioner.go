package controllers

import (
	"context"
	"fmt"
	"sync"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/adapters"
	"github.com/aws-innovate/AISuperPlane/src/superplane-controller/provisioner"
)

const (
	// DefaultMaxConcurrentProvisioning is the default max concurrent provisioning operations.
	DefaultMaxConcurrentProvisioning int32 = 3

	// DefaultDiskSizeGB is the default disk size for provisioned nodes.
	DefaultDiskSizeGB int32 = 256

	// DefaultK8sVersion is the default Kubernetes version.
	DefaultK8sVersion = "1.33"

	// requeueDelayProvisioning is how long to wait before re-checking a provisioning node.
	requeueDelayProvisioning = 30 * time.Second

	// requeueDelayBudgetExhausted is how long to wait when provisioning budget is exhausted.
	requeueDelayBudgetExhausted = 15 * time.Second
)

// ProvisionerReconciler watches SuperplaneNode CRs in Pending phase and
// provisions GPU nodes using cloud adapters, then onboards them to EKS.
type ProvisionerReconciler struct {
	client.Client

	// Adapters is the list of cloud adapters for pricing and provisioning.
	Adapters []adapters.CloudAdapter

	// Onboarder handles the node onboarding process.
	Onboarder *provisioner.Onboarder

	// mu protects inFlight.
	mu sync.Mutex

	// inFlight tracks SuperplaneNode names currently being provisioned.
	inFlight map[string]struct{}
}

// NewProvisionerReconciler creates a new ProvisionerReconciler.
func NewProvisionerReconciler(c client.Client, cloudAdapters []adapters.CloudAdapter, onboarder *provisioner.Onboarder) *ProvisionerReconciler {
	return &ProvisionerReconciler{
		Client:    c,
		Adapters:  cloudAdapters,
		Onboarder: onboarder,
		inFlight:  make(map[string]struct{}),
	}
}

// Reconcile handles a single SuperplaneNode event.
// It provisions nodes in Pending phase and monitors Provisioning nodes.
func (r *ProvisionerReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	_ = log.FromContext(ctx).WithName("provisioner")

	// 1. Fetch the SuperplaneNode.
	var spNode superplanev1.SuperplaneNode
	if err := r.Get(ctx, req.NamespacedName, &spNode); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	// 2. Skip deleted nodes.
	if spNode.DeletionTimestamp != nil {
		return ctrl.Result{}, nil
	}

	switch spNode.Status.Phase {
	case superplanev1.SuperplaneNodePhasePending, "":
		// New node needs provisioning.
		return r.handlePending(ctx, &spNode)

	case superplanev1.SuperplaneNodePhaseProvisioning:
		// Already being provisioned - requeue to check later.
		return ctrl.Result{RequeueAfter: requeueDelayProvisioning}, nil

	default:
		// Not our concern (Ready, Failed, Draining, etc.).
		return ctrl.Result{}, nil
	}
}

// handlePending processes a SuperplaneNode in Pending phase.
// It checks the provisioning budget, selects the cheapest cloud, and kicks off provisioning.
func (r *ProvisionerReconciler) handlePending(ctx context.Context, spNode *superplanev1.SuperplaneNode) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithName("provisioner")

	// 1. Atomically check and mark in-flight to prevent TOCTOU races.
	// If another reconcile is already processing this node, skip it.
	r.mu.Lock()
	if _, ok := r.inFlight[spNode.Name]; ok {
		r.mu.Unlock()
		logger.V(1).Info("node already in-flight", "node", spNode.Name)
		return ctrl.Result{RequeueAfter: requeueDelayProvisioning}, nil
	}
	// Mark in-flight immediately under the same lock to prevent races.
	r.inFlight[spNode.Name] = struct{}{}
	r.mu.Unlock()

	// If we fail before launching the goroutine, clean up the in-flight entry.
	launched := false
	defer func() {
		if !launched {
			r.mu.Lock()
			delete(r.inFlight, spNode.Name)
			r.mu.Unlock()
		}
	}()

	// 2. Fetch the NodePool for budget checks.
	pool, err := r.getNodePool(ctx, spNode.Spec.NodePoolRef)
	if err != nil {
		logger.Error(err, "failed to get NodePool", "nodePoolRef", spNode.Spec.NodePoolRef)
		return ctrl.Result{RequeueAfter: requeueDelayProvisioning}, nil
	}

	// 3. Check maxConcurrentProvisioning budget.
	maxConcurrent := DefaultMaxConcurrentProvisioning
	if pool.Spec.MaxConcurrentProvisioning != nil {
		maxConcurrent = *pool.Spec.MaxConcurrentProvisioning
	}

	currentProvisioning, err := r.countProvisioningNodes(ctx, spNode.Spec.NodePoolRef)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("count provisioning nodes: %w", err)
	}

	if currentProvisioning >= maxConcurrent {
		logger.Info("provisioning budget exhausted, requeuing",
			"node", spNode.Name,
			"current", currentProvisioning,
			"max", maxConcurrent,
		)
		return ctrl.Result{RequeueAfter: requeueDelayBudgetExhausted}, nil
	}

	// 4. Get all available clouds sorted by price.
	allOptions, err := adapters.SelectAllAvailable(ctx, r.filterAdapters(pool), spNode.Spec.GPUType, pool.Spec.PreferSpot)
	if err != nil {
		logger.Error(err, "no GPU availability", "gpuType", spNode.Spec.GPUType)
		if updateErr := r.updatePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseFailed,
			fmt.Sprintf("No available %s GPUs: %v", spNode.Spec.GPUType, err)); updateErr != nil {
			logger.Error(updateErr, "failed to update phase to Failed")
		}
		return ctrl.Result{}, nil
	}

	// 5. Update phase to Provisioning.
	if err := r.updatePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseProvisioning,
		fmt.Sprintf("Provisioning on %s (%s)", allOptions[0].Price.Cloud, allOptions[0].Price.Region)); err != nil {
		return ctrl.Result{}, fmt.Errorf("update phase to Provisioning: %w", err)
	}

	// 6. Start async provisioning. The goroutine owns the in-flight entry cleanup.
	launched = true
	go r.provisionAsync(context.Background(), spNode.DeepCopy(), pool, allOptions)

	return ctrl.Result{RequeueAfter: requeueDelayProvisioning}, nil
}

// provisionAsync runs the provisioning and onboarding flow in a background goroutine.
// It tries each cloud option in order (cheapest first), falling back on failure.
func (r *ProvisionerReconciler) provisionAsync(ctx context.Context, spNode *superplanev1.SuperplaneNode, pool *superplanev1.NodePool, options []adapters.SelectionResult) {
	logger := log.FromContext(ctx).WithName("provisioner").WithValues("node", spNode.Name)

	defer func() {
		r.mu.Lock()
		delete(r.inFlight, spNode.Name)
		r.mu.Unlock()
	}()

	// Build the NodeSpec from the SuperplaneNode and NodePool template.
	diskSize := DefaultDiskSizeGB
	k8sVersion := DefaultK8sVersion
	wireGuard := true

	if pool.Spec.Template != nil {
		if pool.Spec.Template.DiskSizeGB > 0 {
			diskSize = pool.Spec.Template.DiskSizeGB
		}
		if pool.Spec.Template.K8sVersion != "" {
			k8sVersion = pool.Spec.Template.K8sVersion
		}
		wireGuard = pool.Spec.Template.Wireguard
	}

	// Try each option in order (cheapest first).
	for i, option := range options {
		cloud := option.Price.Cloud
		region := option.Price.Region
		costPerHour := option.Price.HourlyCost
		if pool.Spec.PreferSpot && option.Price.SpotCost > 0 && option.Price.SpotCost < costPerHour {
			costPerHour = option.Price.SpotCost
		}

		logger.Info("attempting provision",
			"attempt", i+1,
			"cloud", cloud,
			"region", region,
			"gpuType", spNode.Spec.GPUType,
			"cost", costPerHour,
		)

		// Update status with current attempt info.
		if err := r.updateProvisioningStatus(ctx, spNode, cloud, region, costPerHour, i+1, len(options)); err != nil {
			logger.Error(err, "failed to update provisioning status")
		}

		// Generate a SkyPilot cluster name for this node.
		clusterName := fmt.Sprintf("sp-%s", spNode.Name)

		spec := adapters.NodeSpec{
			Cloud:      cloud,
			GPUType:    spNode.Spec.GPUType,
			GPUCount:   int(spNode.Spec.GPUCount),
			DiskSizeGB: int(diskSize),
			K8sVersion: k8sVersion,
			Region:     region,
			WireGuard:  wireGuard,
			UseSpot:    pool.Spec.PreferSpot,
			ClusterName: clusterName,
		}

		// Run onboarding.
		result, err := r.Onboarder.Onboard(ctx, spec, clusterName, func(line string) {
			logger.V(1).Info("onboard", "output", line)
		})

		if err != nil {
			logger.Error(err, "onboarding error", "cloud", cloud, "attempt", i+1)
			continue
		}

		if result.Success {
			// Update SuperplaneNode to Joining, then Ready.
			now := metav1.Now()
			if err := r.updateNodeSuccess(ctx, spNode, clusterName, result, costPerHour, cloud, region, &now); err != nil {
				logger.Error(err, "failed to update node to Ready")
				return
			}
			logger.Info("node provisioned and onboarded successfully",
				"cloud", cloud,
				"region", region,
				"cluster", clusterName,
			)
			return
		}

		// Onboarding failed - log and try next cloud.
		logger.Info("onboarding failed, trying next option",
			"cloud", cloud,
			"attempt", i+1,
			"error", result.Error,
		)

		// Attempt to terminate the failed SkyPilot cluster to clean up.
		if _, termErr := option.Adapter.TerminateNode(ctx, clusterName); termErr != nil {
			logger.Error(termErr, "failed to terminate failed cluster", "cluster", clusterName)
		}
	}

	// All options exhausted - mark as Failed.
	logger.Info("all cloud options exhausted, marking node as Failed", "node", spNode.Name)
	if err := r.updatePhase(ctx, spNode, superplanev1.SuperplaneNodePhaseFailed,
		"All cloud options exhausted after trying each available provider"); err != nil {
		logger.Error(err, "failed to update phase to Failed")
	}
}

// SetupWithManager registers the reconciler with the controller manager.
func (r *ProvisionerReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&superplanev1.SuperplaneNode{}).
		Named("provisioner").
		Complete(r)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// getNodePool retrieves a NodePool by name (cluster-scoped).
func (r *ProvisionerReconciler) getNodePool(ctx context.Context, name string) (*superplanev1.NodePool, error) {
	var pool superplanev1.NodePool
	if err := r.Get(ctx, types.NamespacedName{Name: name}, &pool); err != nil {
		return nil, fmt.Errorf("get nodepool %q: %w", name, err)
	}
	return &pool, nil
}

// countProvisioningNodes counts SuperplaneNode CRs in Provisioning or Joining phase
// for a given NodePool.
func (r *ProvisionerReconciler) countProvisioningNodes(ctx context.Context, nodePoolRef string) (int32, error) {
	var nodeList superplanev1.SuperplaneNodeList
	if err := r.List(ctx, &nodeList); err != nil {
		return 0, err
	}

	var count int32
	for _, node := range nodeList.Items {
		if node.Spec.NodePoolRef != nodePoolRef {
			continue
		}
		if node.Status.Phase == superplanev1.SuperplaneNodePhaseProvisioning ||
			node.Status.Phase == superplanev1.SuperplaneNodePhaseJoining {
			count++
		}
	}
	return count, nil
}

// filterAdapters returns only the adapters whose cloud name appears in the NodePool's
// Clouds list.
func (r *ProvisionerReconciler) filterAdapters(pool *superplanev1.NodePool) []adapters.CloudAdapter {
	if len(pool.Spec.Clouds) == 0 {
		return r.Adapters
	}

	cloudSet := make(map[string]bool, len(pool.Spec.Clouds))
	for _, c := range pool.Spec.Clouds {
		cloudSet[c] = true
	}

	var filtered []adapters.CloudAdapter
	for _, a := range r.Adapters {
		if cloudSet[a.Name()] {
			filtered = append(filtered, a)
		}
	}
	return filtered
}

// updatePhase updates the SuperplaneNode status phase and message.
func (r *ProvisionerReconciler) updatePhase(ctx context.Context, spNode *superplanev1.SuperplaneNode, phase superplanev1.SuperplaneNodePhase, message string) error {
	// Re-fetch to avoid update conflicts.
	var current superplanev1.SuperplaneNode
	key := types.NamespacedName{
		Name:      spNode.Name,
		Namespace: spNode.Namespace,
	}
	if err := r.Get(ctx, key, &current); err != nil {
		return fmt.Errorf("get node %q: %w", spNode.Name, err)
	}

	current.Status.Phase = phase
	current.Status.Message = message
	if err := r.Status().Update(ctx, &current); err != nil {
		return fmt.Errorf("update status for %q: %w", spNode.Name, err)
	}

	// Update caller's copy.
	spNode.Status.Phase = phase
	spNode.Status.Message = message
	return nil
}

// updateProvisioningStatus updates the status with current provisioning attempt details.
func (r *ProvisionerReconciler) updateProvisioningStatus(ctx context.Context, spNode *superplanev1.SuperplaneNode, cloud, region string, costPerHour float64, attempt, total int) error {
	var current superplanev1.SuperplaneNode
	key := types.NamespacedName{
		Name:      spNode.Name,
		Namespace: spNode.Namespace,
	}
	if err := r.Get(ctx, key, &current); err != nil {
		return fmt.Errorf("get node %q: %w", spNode.Name, err)
	}

	current.Status.Phase = superplanev1.SuperplaneNodePhaseProvisioning
	current.Status.Message = fmt.Sprintf("Provisioning attempt %d/%d: %s/%s ($%.2f/hr)", attempt, total, cloud, region, costPerHour)
	current.Status.HourlyCost = costPerHour

	if err := r.Status().Update(ctx, &current); err != nil {
		return fmt.Errorf("update provisioning status for %q: %w", spNode.Name, err)
	}
	return nil
}

// updateNodeSuccess updates the SuperplaneNode after successful provisioning and onboarding.
func (r *ProvisionerReconciler) updateNodeSuccess(
	ctx context.Context,
	spNode *superplanev1.SuperplaneNode,
	skypilotCluster string,
	result *provisioner.OnboardResult,
	costPerHour float64,
	cloud, region string,
	provisionedAt *metav1.Time,
) error {
	var current superplanev1.SuperplaneNode
	key := types.NamespacedName{
		Name:      spNode.Name,
		Namespace: spNode.Namespace,
	}
	if err := r.Get(ctx, key, &current); err != nil {
		return fmt.Errorf("get node %q: %w", spNode.Name, err)
	}

	current.Spec.Cloud = cloud
	current.Spec.Region = region

	current.Status.Phase = superplanev1.SuperplaneNodePhaseReady
	current.Status.Message = "Node provisioned and onboarded successfully"
	current.Status.SkypilotCluster = skypilotCluster
	current.Status.HourlyCost = costPerHour
	current.Status.ProvisionedAt = provisionedAt

	if result.K8sNodeName != "" {
		current.Status.K8sNodeName = result.K8sNodeName
	}
	if result.SSMInstanceID != "" {
		current.Status.SSMInstanceID = result.SSMInstanceID
	}

	// Update the spec first (cloud/region may have changed due to fallback).
	if err := r.Update(ctx, &current); err != nil {
		return fmt.Errorf("update spec for %q: %w", spNode.Name, err)
	}

	// Then update status.
	if err := r.Status().Update(ctx, &current); err != nil {
		return fmt.Errorf("update status for %q: %w", spNode.Name, err)
	}

	return nil
}
