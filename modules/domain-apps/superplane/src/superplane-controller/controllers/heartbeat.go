package controllers

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
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
	Namespace             string
	WorkspaceID           string
	Credential            string
	SigningKey            string
	RequireAuthentication bool
	NativeNodes           bool // Governed native EKS health; never derives billing from nodes.
	Client                client.Client
	SkyChecker            SkyPilotHealthChecker
	APIURL                string        // Control plane API base URL
	ClusterID             string        // This cluster's identifier
	Interval              time.Duration // Heartbeat interval (default 30s)
	HTTPClient            *http.Client  // HTTP client for POSTing heartbeats
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
	if h.NativeNodes {
		return h.collectNativeNodes(ctx, hb)
	}

	// 1. List all SuperplaneNodes and compute node/GPU/cost summaries.
	var nodeList superplanev1.SuperplaneNodeList
	if err := h.Client.List(ctx, &nodeList, client.InNamespace(h.Namespace)); err != nil {
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

	// 3. Count pending GPU pods.
	hb.PendingPods = h.countPendingPods(ctx)

	// 4. Determine overall status.
	hb.Status = h.computeStatus(hb)

	return hb
}

// collectNativeNodes reads only nodes labelled for this workspace. The signed
// observation reports health; financial exposure comes from trusted inventory.
func (h *HeartbeatSender) collectNativeNodes(ctx context.Context, hb ClusterHeartbeat) ClusterHeartbeat {
	hb.Status = "degraded"
	if h.WorkspaceID == "" {
		return hb
	}
	var nodes corev1.NodeList
	if err := h.Client.List(ctx, &nodes, client.MatchingLabels{"superplane.ai/workspace": h.WorkspaceID}); err != nil {
		return hb
	}
	hb.NodeSummary.Total = len(nodes.Items)
	for _, node := range nodes.Items {
		ready := false
		for _, condition := range node.Status.Conditions {
			if condition.Type == corev1.NodeReady && condition.Status == corev1.ConditionTrue {
				ready = true
			}
		}
		if ready && node.Spec.ProviderID != "" && node.DeletionTimestamp == nil {
			hb.NodeSummary.Ready++
		} else {
			hb.NodeSummary.NotReady++
		}
	}
	hb.SkyPilotHealthy = h.checkSkyPilotHealth(ctx)
	if hb.SkyPilotHealthy && hb.NodeSummary.NotReady == 0 {
		hb.Status = "healthy"
	}
	return hb
}

// Send POSTs the heartbeat to the control plane with retry and backoff.
func (h *HeartbeatSender) Send(ctx context.Context, hb ClusterHeartbeat) error {
	logger := log.FromContext(ctx).WithName("heartbeat")

	var payload any = hb
	url := h.APIURL + "/internal/heartbeat"
	signed := h.Credential != "" || h.RequireAuthentication
	if signed {
		if h.WorkspaceID == "" || h.Credential == "" || h.SigningKey == "" || hb.Timestamp.IsZero() {
			return fmt.Errorf("authenticated observation configuration is incomplete")
		}
		status := "degraded"
		if hb.Status == "healthy" {
			status = "healthy"
		}
		payload = map[string]any{
			"contract_version": "v1", "kind": "fleet_health",
			"subject":     map[string]string{"cluster_id": h.ClusterID, "workspace": h.WorkspaceID},
			"reported_at": hb.Timestamp.UTC().Format(time.RFC3339Nano),
			"reporter":    "superplane-controller", "status": status,
			"checks": []map[string]string{{"name": "controller-heartbeat", "status": status, "observed_at": hb.Timestamp.UTC().Format(time.RFC3339Nano)}},
		}
		url = h.APIURL + "/internal/observations"
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal heartbeat: %w", err)
	}

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
		if signed {
			req.Header.Set("Authorization", h.Credential)
			req.Header.Set("x-superplane-contract-version", "v1")
			mac := hmac.New(sha256.New, []byte(h.SigningKey))
			mac.Write(data)
			req.Header.Set("x-superplane-signature", fmt.Sprintf("sha256=%x", mac.Sum(nil)))
		}

		hc := *h.HTTPClient
		hc.CheckRedirect = func(req *http.Request, via []*http.Request) error { return http.ErrUseLastResponse }
		resp, err := hc.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("send heartbeat (attempt %d): %w", attempt+1, err)
			continue
		}

		// Read and close the body.
		_, _ = io.ReadAll(io.LimitReader(resp.Body, maxResponseBody))
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			logger.V(1).Info("heartbeat sent successfully", "status", resp.StatusCode)
			return nil
		}

		lastErr = fmt.Errorf("heartbeat rejected (attempt %d): status %d", attempt+1, resp.StatusCode)
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

// Vault-sync reporting was REMOVED by A19 (#5684) together with the ClusterRole's
// cluster-wide `secrets: ["list"]` grant, which existed solely to serve it.
//
// The removed checkVaultSyncStatus listed every Secret in the cluster carrying the
// `reconcile.external-secrets.io/managed` label and then returned the constant
// "synced" on every branch — including the branch where the list itself FAILED. It
// never read a single field of what it fetched, so the broadest permission this
// controller held funded an answer that was already a constant.
//
// It was not merely useless, it was actively misleading in two directions:
//
//   - The one case where the answer was genuinely unknown (the list failed) reported
//     the most reassuring value available. That is the exact bug the domain's health
//     contract was built to make unrepresentable — see contracts/superplane_contracts/
//     health.py, which cites this function by name, and tests/test_probe_cannot_fake_health.py.
//   - "synced" is not in the receiver's `ok|pending|failed` vocabulary, so
//     superplane-platform-monitor/monitors/cluster_health.go classified it as
//     Unrecognised/Unknown on arrival. The field was discarded at the far end.
//
// Deleting only the RBAC grant would have been worse than leaving both: the List would
// then fail on a permission error and the function would still return "synced",
// converting a dead signal into a guaranteed lie. So the check and the grant were
// retired together, and the field was dropped from the payload rather than populated
// with a value the receiver cannot interpret. An absent field is read as "nothing was
// reported" (cluster_health.go's `case ""` → NotChecked), which is the truthful state
// and, per the health contract's severity ordering, never aggregates to healthy.
//
// Restoring a real vault-sync check means reading ExternalSecret CR status conditions
// and granting `externalsecrets` on `external-secrets.io` — NOT cluster-wide Secret
// reads, which this controller has never needed.

// countPendingPods counts pods in Pending phase that request GPU resources.
func (h *HeartbeatSender) countPendingPods(ctx context.Context) int {
	var podList corev1.PodList
	if err := h.Client.List(ctx, &podList, client.InNamespace(h.Namespace), client.MatchingFields{
		"status.phase": string(corev1.PodPending),
	}); err != nil {
		// If field selector is not indexed, fall back to listing all pods.
		if err := h.Client.List(ctx, &podList, client.InNamespace(h.Namespace)); err != nil {
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
