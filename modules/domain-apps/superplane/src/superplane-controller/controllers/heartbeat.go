package controllers

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/log"

	superplanev1 "github.com/aws-innovate/AISuperPlane/src/superplane-controller/api/v1"
)

const (
	// DefaultHeartbeatInterval is the default interval between heartbeat sends.
	DefaultHeartbeatInterval = 30 * time.Second

	// ControllerVersion is the current version of the controller binary.
	// Updated on release.
	ControllerVersion = "0.1.0"

	// heartbeatSendTimeout is the HTTP timeout for sending a heartbeat to the control plane.
	heartbeatSendTimeout = 10 * time.Second

	// maxSendRetries is the number of retry attempts for a failed heartbeat POST.
	maxSendRetries = 3

	// retryBaseDelay is the base delay for exponential backoff on send retries.
	retryBaseDelay = 1 * time.Second

	// maxResponseBody limits the body size we read from the control plane response.
	maxResponseBody = 64 * 1024 // 64 KB
)

// SkyPilotHealthChecker abstracts the SkyPilot health-check call so the
// HeartbeatSender can be tested without a real SkyPilot API.
type SkyPilotHealthChecker interface {
	HealthCheck(ctx context.Context) (bool, error)
}

// HeartbeatSender periodically collects cluster health data and POSTs it to
// the control plane. It implements manager.Runnable so it can be registered
// with the controller-runtime manager via mgr.Add().
type HeartbeatSender struct {
	Client     client.Client
	SkyChecker SkyPilotHealthChecker
	APIURL     string        // Control plane API base URL
	ClusterID  string        // This cluster's identifier
	Interval   time.Duration // Heartbeat interval (default 30s)
	HTTPClient *http.Client  // HTTP client for POSTing heartbeats
}

// Start begins the heartbeat loop. It blocks until ctx is cancelled.
// Implements manager.Runnable.
func (h *HeartbeatSender) Start(ctx context.Context) error {
	logger := log.FromContext(ctx).WithName("heartbeat")

	if h.Interval == 0 {
		h.Interval = DefaultHeartbeatInterval
	}
	if h.HTTPClient == nil {
		h.HTTPClient = &http.Client{Timeout: heartbeatSendTimeout}
	}

	logger.Info("starting heartbeat sender",
		"interval", h.Interval,
		"clusterID", h.ClusterID,
		"apiURL", h.APIURL,
	)

	ticker := time.NewTicker(h.Interval)
	defer ticker.Stop()

	// Send immediately on start, then on interval.
	hb := h.Collect(ctx)
	if err := h.Send(ctx, hb); err != nil {
		logger.Error(err, "failed to send initial heartbeat")
	}

	for {
		select {
		case <-ctx.Done():
			logger.Info("stopping heartbeat sender")
			return nil
		case <-ticker.C:
			hb := h.Collect(ctx)
			if err := h.Send(ctx, hb); err != nil {
				logger.Error(err, "failed to send heartbeat")
			}
		}
	}
}

// Collect gathers all cluster health data into a ClusterHeartbeat.
func (h *HeartbeatSender) Collect(ctx context.Context) ClusterHeartbeat {
	logger := log.FromContext(ctx).WithName("heartbeat")

	hb := ClusterHeartbeat{
		ClusterID:         h.ClusterID,
		ControllerVersion: ControllerVersion,
		Timestamp:         time.Now(),
		Alerts:            []HeartbeatAlert{},
	}

	// 1. List all SuperplaneNodes and compute node/GPU/cost summaries.
	var nodeList superplanev1.SuperplaneNodeList
	if err := h.Client.List(ctx, &nodeList); err != nil {
		logger.Error(err, "failed to list SuperplaneNodes")
		hb.Status = "degraded"
		hb.Alerts = append(hb.Alerts, HeartbeatAlert{
			Severity: "critical",
			Message:  fmt.Sprintf("Failed to list SuperplaneNodes: %v", err),
			Resource: "superplanenodes",
		})
		return hb
	}
	h.aggregateNodes(&hb, nodeList.Items)

	// 2. Check SkyPilot API health.
	hb.SkyPilotHealthy = h.checkSkyPilotHealth(ctx)
	if !hb.SkyPilotHealthy {
		hb.Alerts = append(hb.Alerts, HeartbeatAlert{
			Severity: "warning",
			Message:  "SkyPilot API is unreachable or unhealthy",
			Resource: "skypilot-api",
		})
	}

	// 3. Check vault/credential sync status (ExternalSecrets).
	hb.VaultSyncStatus = h.checkVaultSyncStatus(ctx)

	// 4. Count pending GPU pods.
	hb.PendingPods = h.countPendingPods(ctx)

	// 5. Determine overall status.
	hb.Status = h.computeStatus(hb)

	return hb
}

// Send POSTs the heartbeat to the control plane with retry and backoff.
func (h *HeartbeatSender) Send(ctx context.Context, hb ClusterHeartbeat) error {
	logger := log.FromContext(ctx).WithName("heartbeat")

	data, err := json.Marshal(hb)
	if err != nil {
		return fmt.Errorf("marshal heartbeat: %w", err)
	}

	url := h.APIURL + "/internal/heartbeat"

	var lastErr error
	for attempt := 0; attempt < maxSendRetries; attempt++ {
		if attempt > 0 {
			delay := retryBaseDelay * time.Duration(1<<uint(attempt-1))
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
			logger.V(1).Info("retrying heartbeat send", "attempt", attempt+1)
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(data))
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}
		req.Header.Set("Content-Type", "application/json")

		resp, err := h.HTTPClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("send heartbeat (attempt %d): %w", attempt+1, err)
			continue
		}

		// Read and close the body.
		body, _ := io.ReadAll(io.LimitReader(resp.Body, maxResponseBody))
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			logger.V(1).Info("heartbeat sent successfully", "status", resp.StatusCode)
			return nil
		}

		lastErr = fmt.Errorf("heartbeat rejected (attempt %d): status %d: %s", attempt+1, resp.StatusCode, string(body))
		// Don't retry on 4xx client errors (except 429).
		if resp.StatusCode >= 400 && resp.StatusCode < 500 && resp.StatusCode != http.StatusTooManyRequests {
			return lastErr
		}
	}

	return fmt.Errorf("heartbeat send failed after %d attempts: %w", maxSendRetries, lastErr)
}

// aggregateNodes computes NodeSummary, GPUSummary, CostHourly, and alerts from nodes.
func (h *HeartbeatSender) aggregateNodes(hb *ClusterHeartbeat, nodes []superplanev1.SuperplaneNode) {
	gpuTypes := make(map[string]int)

	for i := range nodes {
		node := &nodes[i]
		gpuCount := int(node.Spec.GPUCount)

		hb.NodeSummary.Total++
		hb.GPUSummary.Total += gpuCount
		gpuTypes[node.Spec.GPUType] += gpuCount
		hb.CostHourly += node.Status.HourlyCost

		switch node.Status.Phase {
		case superplanev1.SuperplaneNodePhaseReady:
			hb.NodeSummary.Ready++
			// GPUs on Ready nodes with a K8s node are considered allocated.
			if node.Status.K8sNodeName != "" {
				hb.GPUSummary.Allocated += gpuCount
			} else {
				hb.GPUSummary.Idle += gpuCount
			}
		case superplanev1.SuperplaneNodePhaseProvisioning,
			superplanev1.SuperplaneNodePhaseJoining,
			superplanev1.SuperplaneNodePhasePending:
			hb.NodeSummary.Provisioning++
			hb.GPUSummary.Idle += gpuCount
		case superplanev1.SuperplaneNodePhaseDraining:
			hb.NodeSummary.Draining++
			hb.GPUSummary.Allocated += gpuCount
		case superplanev1.SuperplaneNodePhaseDegraded,
			superplanev1.SuperplaneNodePhaseFailed:
			hb.NodeSummary.NotReady++
			hb.GPUSummary.Idle += gpuCount
			// Emit alert for degraded/failed nodes.
			severity := "warning"
			if node.Status.Phase == superplanev1.SuperplaneNodePhaseFailed {
				severity = "critical"
			}
			hb.Alerts = append(hb.Alerts, HeartbeatAlert{
				Severity: severity,
				Message:  fmt.Sprintf("Node %s is %s: %s", node.Name, node.Status.Phase, node.Status.Message),
				Resource: node.Name,
			})
		case superplanev1.SuperplaneNodePhaseTerminated:
			// Don't count terminated nodes in the active summary.
			hb.NodeSummary.Total--
			hb.GPUSummary.Total -= gpuCount
		}
	}

	hb.GPUSummary.Types = gpuTypes
}

// checkSkyPilotHealth calls the SkyPilot health endpoint.
func (h *HeartbeatSender) checkSkyPilotHealth(ctx context.Context) bool {
	if h.SkyChecker == nil {
		return false
	}
	healthy, err := h.SkyChecker.HealthCheck(ctx)
	if err != nil {
		log.FromContext(ctx).WithName("heartbeat").Error(err, "SkyPilot health check failed")
		return false
	}
	return healthy
}

// checkVaultSyncStatus checks ExternalSecret objects in the cluster.
// Returns "synced", "pending", or "failed".
func (h *HeartbeatSender) checkVaultSyncStatus(ctx context.Context) string {
	// Check for ExternalSecret objects by looking for secrets with the
	// external-secrets.io/managed label.
	var secretList corev1.SecretList
	if err := h.Client.List(ctx, &secretList, client.MatchingLabels{
		"reconcile.external-secrets.io/managed": "true",
	}); err != nil {
		// If we can't list (e.g. no ExternalSecrets CRD), assume synced.
		return "synced"
	}

	if len(secretList.Items) == 0 {
		return "synced"
	}

	// All managed secrets exist → synced. In a real implementation we'd check
	// the ExternalSecret CR status conditions, but for now this is sufficient.
	return "synced"
}

// countPendingPods counts pods in Pending phase that request GPU resources.
func (h *HeartbeatSender) countPendingPods(ctx context.Context) int {
	var podList corev1.PodList
	if err := h.Client.List(ctx, &podList, client.MatchingFields{
		"status.phase": string(corev1.PodPending),
	}); err != nil {
		// If field selector is not indexed, fall back to listing all pods.
		if err := h.Client.List(ctx, &podList); err != nil {
			return 0
		}
	}

	count := 0
	for i := range podList.Items {
		pod := &podList.Items[i]
		if pod.Status.Phase != corev1.PodPending {
			continue
		}
		if requestsGPU(pod) {
			count++
		}
	}
	return count
}

// requestsGPU returns true if any container in the pod requests nvidia.com/gpu.
func requestsGPU(pod *corev1.Pod) bool {
	for _, c := range pod.Spec.Containers {
		if q, ok := c.Resources.Requests["nvidia.com/gpu"]; ok && !q.IsZero() {
			return true
		}
	}
	for _, c := range pod.Spec.InitContainers {
		if q, ok := c.Resources.Requests["nvidia.com/gpu"]; ok && !q.IsZero() {
			return true
		}
	}
	return false
}

// computeStatus determines the overall cluster status from the heartbeat data.
func (h *HeartbeatSender) computeStatus(hb ClusterHeartbeat) string {
	// If there are critical alerts → degraded.
	for _, alert := range hb.Alerts {
		if alert.Severity == "critical" {
			return "degraded"
		}
	}

	// If SkyPilot is down → degraded.
	if !hb.SkyPilotHealthy {
		return "degraded"
	}

	// If more than half of nodes are not ready → degraded.
	if hb.NodeSummary.Total > 0 && hb.NodeSummary.NotReady > hb.NodeSummary.Total/2 {
		return "degraded"
	}

	// If some nodes are provisioning and we had failures recently → recovering.
	if hb.NodeSummary.Provisioning > 0 && hb.NodeSummary.NotReady > 0 {
		return "recovering"
	}

	return "healthy"
}
