package monitors

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"go.uber.org/zap"

	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/config"
	"github.com/aws-innovate/AISuperPlane/src/superplane-platform-monitor/db"
)

// HealthDimension represents a single health check result.
type HealthDimension struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Message string `json:"message,omitempty"`
}

// HealthCheckResult is the aggregated result of all health dimensions.
type HealthCheckResult struct {
	OverallStatus string            `json:"overall_status"`
	Dimensions    []HealthDimension `json:"dimensions"`
	CheckedAt     time.Time         `json:"checked_at"`
}

// ClusterHealthMonitor monitors cluster health by evaluating heartbeat data.
//
// It checks six health dimensions per design doc section 4.4:
//  1. Heartbeat freshness — is the heartbeat arriving on time?
//  2. SkyPilot health — skypilot_healthy field from heartbeat payload
//  3. Vault sync status — vault_sync_status field from heartbeat payload
//  4. Node health — node_summary.not_ready count
//  5. EKS API reachability — can we reach the cluster's K8s API?
//  6. Cost anomaly — cost_hourly vs rolling average, spike >2x triggers alert
//
// The health_status column in Aurora reflects the worst dimension:
//   - All green -> Healthy
//   - SkyPilot down or vault failed -> Degraded
//   - Heartbeat missing >5 min -> Degraded
//   - Heartbeat missing >30 min or EKS unreachable -> Unreachable
type ClusterHealthMonitor struct {
	DB         db.Querier
	Config     *config.Config
	Logger     *zap.Logger
	Clock      Clock
	EKSProber  EKSProber
}

// Name returns the monitor name.
func (r *ClusterHealthMonitor) Name() string {
	return "ClusterHealth"
}

// Check runs one health check cycle across all active clusters.
func (r *ClusterHealthMonitor) Check(ctx context.Context) error {
	clusters, err := r.DB.ListActiveClusters(ctx)
	if err != nil {
		return fmt.Errorf("list active clusters: %w", err)
	}

	r.Logger.Info("Checking cluster health",
		zap.Int("cluster_count", len(clusters)),
	)

	var errs []error
	for i := range clusters {
		if err := r.checkCluster(ctx, &clusters[i]); err != nil {
			r.Logger.Error("Failed to check cluster",
				zap.String("cluster_id", clusters[i].ID),
				zap.String("cluster_name", clusters[i].Name),
				zap.Error(err),
			)
			errs = append(errs, err)
		}
	}

	if len(errs) > 0 {
		return fmt.Errorf("monitor errors: %d of %d clusters failed", len(errs), len(clusters))
	}
	return nil
}

// checkCluster checks a single cluster's health across all dimensions.
func (r *ClusterHealthMonitor) checkCluster(ctx context.Context, cluster *db.Cluster) error {
	// Acquire distributed lock.
	acquired, err := r.DB.AcquireLock(ctx, "cluster_health", cluster.ID, r.Config.MonitorID, r.Config.LockTTL)
	if err != nil {
		return fmt.Errorf("acquire lock for cluster %s: %w", cluster.ID, err)
	}
	if !acquired {
		r.Logger.Debug("Lock held by another instance, skipping",
			zap.String("cluster_id", cluster.ID),
		)
		return nil
	}
	defer func() {
		if err := r.DB.ReleaseLock(ctx, "cluster_health", cluster.ID, r.Config.MonitorID); err != nil {
			r.Logger.Warn("Failed to release lock",
				zap.String("cluster_id", cluster.ID),
				zap.Error(err),
			)
		}
	}()

	// Parse heartbeat payload.
	var payload db.HeartbeatPayload
	if len(cluster.ActualStateJSON) > 0 {
		if err := json.Unmarshal(cluster.ActualStateJSON, &payload); err != nil {
			r.Logger.Warn("Failed to parse heartbeat payload, treating as unknown",
				zap.String("cluster_id", cluster.ID),
				zap.Error(err),
			)
		}
	}

	// Run all health dimensions.
	now := r.Clock.Now()
	result := r.checkAllDimensions(ctx, cluster, &payload, now)

	logger := r.Logger.With(
		zap.String("cluster_id", cluster.ID),
		zap.String("cluster_name", cluster.Name),
		zap.String("overall_status", result.OverallStatus),
	)

	// Detect health status transition.
	previousStatus := ""
	if cluster.HealthStatus != nil {
		previousStatus = *cluster.HealthStatus
	}
	healthChanged := previousStatus != result.OverallStatus

	// Update cluster health in DB.
	detailsJSON, _ := json.Marshal(result)
	if err := r.DB.UpdateClusterHealth(ctx, cluster.ID, result.OverallStatus, detailsJSON); err != nil {
		return fmt.Errorf("update health for cluster %s: %w", cluster.ID, err)
	}

	// Log and emit event on transition.
	if healthChanged {
		logger.Info("Cluster health status changed",
			zap.String("previous_status", previousStatus),
			zap.String("new_status", result.OverallStatus),
		)

		eventDetails, _ := json.Marshal(map[string]interface{}{
			"previous_status": previousStatus,
			"new_status":      result.OverallStatus,
			"dimensions":      result.Dimensions,
			"monitor":         r.Config.MonitorID,
		})
		if err := r.DB.InsertEvent(
			ctx, cluster.OrgID, cluster.ID,
			"monitor_health_changed",
			fmt.Sprintf("Monitor health check: %s -> %s", previousStatus, result.OverallStatus),
			eventDetails,
		); err != nil {
			logger.Warn("Failed to insert health transition event", zap.Error(err))
		}
	} else {
		logger.Debug("Cluster health unchanged", zap.String("status", result.OverallStatus))
	}

	return nil
}

// checkAllDimensions evaluates all 6 health dimensions and returns the aggregated result.
func (r *ClusterHealthMonitor) checkAllDimensions(
	ctx context.Context,
	cluster *db.Cluster,
	payload *db.HeartbeatPayload,
	now time.Time,
) HealthCheckResult {
	dimensions := []HealthDimension{
		r.checkHeartbeatFreshness(cluster, now),
		r.checkSkyPilotHealth(payload),
		r.checkVaultSyncStatus(payload),
		r.checkNodeHealth(payload),
		r.checkEKSReachability(ctx, cluster),
		r.checkCostAnomaly(ctx, cluster, payload),
	}

	// Overall status is the worst (highest severity) dimension.
	overall := HealthStatusHealthy
	for _, dim := range dimensions {
		if HealthDimensionSeverity[dim.Status] > HealthDimensionSeverity[overall] {
			overall = dim.Status
		}
	}

	return HealthCheckResult{
		OverallStatus: overall,
		Dimensions:    dimensions,
		CheckedAt:     now,
	}
}

// checkHeartbeatFreshness checks whether the heartbeat is arriving on time.
//
// Rules:
//   - No heartbeat ever -> Unknown
//   - Heartbeat age > 30 min -> Unreachable
//   - Heartbeat age > 5 min -> Degraded
//   - Otherwise -> Healthy
func (r *ClusterHealthMonitor) checkHeartbeatFreshness(cluster *db.Cluster, now time.Time) HealthDimension {
	dim := HealthDimension{Name: "heartbeat_freshness"}

	if cluster.LastHeartbeat == nil {
		dim.Status = HealthStatusUnknown
		dim.Message = "No heartbeat received yet"
		return dim
	}

	age := now.Sub(*cluster.LastHeartbeat)

	switch {
	case age > r.Config.HeartbeatUnreachableThreshold:
		dim.Status = HealthStatusUnreachable
		dim.Message = fmt.Sprintf("Heartbeat missing for %s (threshold: %s)",
			age.Round(time.Second), r.Config.HeartbeatUnreachableThreshold)
	case age > r.Config.HeartbeatDegradedThreshold:
		dim.Status = HealthStatusDegraded
		dim.Message = fmt.Sprintf("Heartbeat stale for %s (threshold: %s)",
			age.Round(time.Second), r.Config.HeartbeatDegradedThreshold)
	default:
		dim.Status = HealthStatusHealthy
		dim.Message = fmt.Sprintf("Heartbeat received %s ago", age.Round(time.Second))
	}

	return dim
}

// checkSkyPilotHealth checks the skypilot_healthy field from the heartbeat payload.
//
// Rules:
//   - Field not present -> NotChecked (nothing was reported, so nothing is known)
//   - skypilot_healthy=true -> Healthy
//   - skypilot_healthy=false -> Degraded (triggers SkyPilot pod restart)
func (r *ClusterHealthMonitor) checkSkyPilotHealth(payload *db.HeartbeatPayload) HealthDimension {
	dim := HealthDimension{Name: "skypilot_health"}

	if payload.SkyPilotHealthy == nil {
		// #5056: was Healthy / "assuming OK". A cluster whose agent never
		// reported SkyPilot at all is not evidence that SkyPilot is up.
		dim.Status = HealthStatusNotChecked
		dim.Message = "SkyPilot health not reported"
		return dim
	}

	if *payload.SkyPilotHealthy {
		dim.Status = HealthStatusHealthy
		dim.Message = "SkyPilot pod is healthy"
	} else {
		dim.Status = HealthStatusDegraded
		dim.Message = "SkyPilot pod is unhealthy — restart recommended via cross-account K8s API"
	}

	return dim
}

// checkVaultSyncStatus checks the vault_sync_status field from the heartbeat payload.
//
// Rules:
//   - Field not present or empty -> NotChecked (nothing reported)
//   - vault_sync_status=ok -> Healthy
//   - vault_sync_status=pending -> Healthy (sync reported, in progress)
//   - vault_sync_status=failed -> Degraded (re-trigger credential sync)
//   - any other value -> Unknown (reported, but not a value this monitor understands)
//
// #5056 note: every branch here used to end in Healthy, so the function could
// only ever say "fine" — including for the value the controller actually sends.
// The controller emits `vault_sync_status: "synced"`, which is not in the API
// schema's `^(ok|failed|pending)$` and so lands in `default`. Under the old code
// that unrecognised value was reported as a healthy vault sync; it is now
// Unknown, which is what "I was told something I cannot interpret" means. The
// producer/schema mismatch itself is a live defect that this story only records
// (see docs/runbooks/superplane-monitor-grant-withdrawal.md).
func (r *ClusterHealthMonitor) checkVaultSyncStatus(payload *db.HeartbeatPayload) HealthDimension {
	dim := HealthDimension{Name: "vault_sync_status"}

	switch payload.VaultSyncStatus {
	case "":
		dim.Status = HealthStatusNotChecked
		dim.Message = "Vault sync status not reported"
	case "ok":
		dim.Status = HealthStatusHealthy
		dim.Message = "Vault credential sync OK"
	case "pending":
		dim.Status = HealthStatusHealthy
		dim.Message = "Vault credential sync in progress"
	case "failed":
		dim.Status = HealthStatusDegraded
		dim.Message = "Vault credential sync failed — re-trigger recommended"
	default:
		dim.Status = HealthStatusUnknown
		dim.Message = fmt.Sprintf("Unrecognised vault sync status: %s", payload.VaultSyncStatus)
	}

	return dim
}

// checkNodeHealth checks the node_summary.not_ready count from the heartbeat payload.
//
// Rules:
//   - No node summary -> NotChecked (nothing reported)
//   - total == 0 -> NotChecked (a summary listing no nodes says nothing about node health)
//   - not_ready == 0 -> Healthy
//   - not_ready > 0 but < 50% of total -> Degraded (warning, controller handles auto-repair)
//   - not_ready >= 50% of total -> Degraded (critical warning)
func (r *ClusterHealthMonitor) checkNodeHealth(payload *db.HeartbeatPayload) HealthDimension {
	dim := HealthDimension{Name: "node_health"}

	if payload.NodeSummary == nil {
		// #5056: was Healthy / "assuming OK".
		dim.Status = HealthStatusNotChecked
		dim.Message = "Node health not reported"
		return dim
	}

	ns := payload.NodeSummary
	if ns.Total == 0 {
		// #5056: was Healthy. Zero nodes examined is zero evidence — this is the
		// shape a partially-initialised or failed node-listing produces.
		dim.Status = HealthStatusNotChecked
		dim.Message = "No nodes reported"
		return dim
	}

	if ns.NotReady == 0 {
		dim.Status = HealthStatusHealthy
		dim.Message = fmt.Sprintf("All %d nodes ready", ns.Total)
		return dim
	}

	notReadyPct := float64(ns.NotReady) / float64(ns.Total) * 100

	if notReadyPct >= 50 {
		dim.Status = HealthStatusDegraded
		dim.Message = fmt.Sprintf("CRITICAL: %d/%d nodes not ready (%.0f%%) — controller auto-repair in progress",
			ns.NotReady, ns.Total, notReadyPct)
	} else {
		dim.Status = HealthStatusDegraded
		dim.Message = fmt.Sprintf("WARNING: %d/%d nodes not ready (%.0f%%) — controller handles auto-repair",
			ns.NotReady, ns.Total, notReadyPct)
	}

	return dim
}

// checkEKSReachability probes the cluster's K8s API to verify network/IAM connectivity.
//
// Rules:
//   - No prober wired, or the prober reports ErrProbeNotPerformed -> NotChecked
//   - API reachable -> Healthy
//   - API unreachable -> Unreachable (network or IAM issue)
//
// #5056: both "no prober" branches used to report Healthy / "EKS API reachable",
// which is the acceptance-4 defect at its most direct — the shipped default
// wiring probed nothing and reported every cluster's K8s API as reachable.
func (r *ClusterHealthMonitor) checkEKSReachability(ctx context.Context, cluster *db.Cluster) HealthDimension {
	dim := HealthDimension{Name: "eks_reachability"}

	if r.EKSProber == nil {
		dim.Status = HealthStatusNotChecked
		dim.Message = "EKS probing not configured"
		return dim
	}

	if err := r.EKSProber.ProbeEKS(ctx, cluster); err != nil {
		// A prober that declines to probe is not a failed probe: distinguish
		// "did not look" from "looked and could not reach it".
		if errors.Is(err, ErrProbeNotPerformed) {
			dim.Status = HealthStatusNotChecked
			dim.Message = "EKS probing not configured"
			return dim
		}
		dim.Status = HealthStatusUnreachable
		dim.Message = fmt.Sprintf("EKS API unreachable: %v", err)
	} else {
		dim.Status = HealthStatusHealthy
		dim.Message = "EKS API reachable"
	}

	return dim
}

// checkCostAnomaly compares current hourly cost against the rolling average.
//
// Rules:
//   - No cost data, or no average to compare against -> NotChecked
//   - cost_hourly / cost_hourly_avg > CostAnomalyMultiplier -> Degraded (alert)
//   - Otherwise -> Healthy
//
// #5056: the three "nothing to compare" branches reported Healthy, i.e. "no cost
// anomaly", on the strength of never having performed the comparison.
func (r *ClusterHealthMonitor) checkCostAnomaly(ctx context.Context, cluster *db.Cluster, payload *db.HeartbeatPayload) HealthDimension {
	dim := HealthDimension{Name: "cost_anomaly"}

	if payload.CostHourly == nil {
		dim.Status = HealthStatusNotChecked
		dim.Message = "Cost data not reported"
		return dim
	}

	currentCost := *payload.CostHourly

	// Use the rolling average from the payload if available.
	var avgCost float64
	if payload.CostHourlyAvg != nil && *payload.CostHourlyAvg > 0 {
		avgCost = *payload.CostHourlyAvg
	} else {
		// Fall back to DB-based cost history if payload doesn't include average.
		costs, err := r.DB.GetCostHistory(ctx, cluster.ID, 24*time.Hour)
		if err != nil || len(costs) == 0 {
			dim.Status = HealthStatusNotChecked
			dim.Message = fmt.Sprintf("Current cost: $%.2f/hr (no history for comparison)", currentCost)
			return dim
		}

		var sum float64
		for _, c := range costs {
			sum += c
		}
		avgCost = sum / float64(len(costs))
	}

	if avgCost <= 0 {
		dim.Status = HealthStatusNotChecked
		dim.Message = fmt.Sprintf("Current cost: $%.2f/hr (average not available)", currentCost)
		return dim
	}

	ratio := currentCost / avgCost
	threshold := r.Config.CostAnomalyMultiplier

	if ratio > threshold {
		dim.Status = HealthStatusDegraded
		dim.Message = fmt.Sprintf("COST ALERT: $%.2f/hr is %.1fx the rolling average of $%.2f/hr (threshold: %.1fx)",
			currentCost, ratio, avgCost, threshold)
	} else {
		dim.Status = HealthStatusHealthy
		dim.Message = fmt.Sprintf("Cost OK: $%.2f/hr (%.1fx average of $%.2f/hr)",
			currentCost, ratio, avgCost)
	}

	return dim
}

// WorstStatus returns the worst health status from a list of statuses.
func WorstStatus(statuses ...string) string {
	worst := HealthStatusHealthy
	for _, s := range statuses {
		if HealthDimensionSeverity[s] > HealthDimensionSeverity[worst] {
			worst = s
		}
	}
	return worst
}
